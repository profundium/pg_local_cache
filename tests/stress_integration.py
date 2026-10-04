#!/usr/bin/env python3
"""Concurrent RESP stale-read stress test with rollback and parser noise."""

from __future__ import annotations

import json
import os
import random
import select
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone

from pipeline_integration import RespConnection, crud_key, wait_for_mapping


PSQL = os.environ.get("PG_LOCAL_CACHE_PSQL", "psql")
PGHOST = os.environ.get("PGHOST", "127.0.0.1")
PGPORT = os.environ.get("PGPORT", "5432")
PGDATABASE = os.environ.get("PGDATABASE", "postgres")
RESP_HOST = os.environ.get("PG_LOCAL_CACHE_RESP_HOST", "127.0.0.1")
RESP_PORT = int(os.environ.get("PG_LOCAL_CACHE_RESP_PORT", "6380"))
STRESS_KEYS = int(os.environ.get("PGLC_STRESS_KEYS", "2048"))
STRESS_WRITERS = int(os.environ.get("PGLC_STRESS_WRITERS", "4"))
STRESS_READERS = int(os.environ.get("PGLC_STRESS_READERS", "4"))
STRESS_SECONDS = float(os.environ.get("PGLC_STRESS_SECONDS", "30"))
PAD_BYTES = int(os.environ.get("PGLC_STRESS_PAD_BYTES", "32"))
ROLLBACK_MARKER = 1_000_000_000
PSQL_TIMEOUT = 8
SOCKET_TIMEOUT = 5
JOIN_TIMEOUT = 15
TERMINAL_HOT_KEYS = 256
KILL_SWITCH_WINDOW = 1.0
KILL_SWITCH_MIN_READS = 200
KILL_SWITCH_TIMEOUT = 15
EVENT_JOIN_TIMEOUT = 90

if STRESS_KEYS < 2 or STRESS_WRITERS < 1 or STRESS_READERS < 1:
    raise ValueError("stress keys, writers, and readers must be positive")
if STRESS_SECONDS <= 0 or PAD_BYTES < 1 or PAD_BYTES > 1024:
    raise ValueError("stress duration and pad size are out of range")


class PsqlSession:
    """One bounded, long-lived psql process used by one writer thread."""

    def __init__(self) -> None:
        self.process = subprocess.Popen(
            [
                PSQL,
                "-X",
                "-q",
                "-A",
                "-t",
                "-v",
                "ON_ERROR_STOP=1",
                "-h",
                PGHOST,
                "-p",
                PGPORT,
                "-d",
                PGDATABASE,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.pending = bytearray()

    def query(self, statement: str, timeout: float = PSQL_TIMEOUT) -> list[str]:
        if self.process.poll() is not None:
            raise RuntimeError(f"psql exited with status {self.process.returncode}")
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        marker = f"PGLC_STRESS_{uuid.uuid4().hex}".encode()
        statement = statement.strip().rstrip(";")
        try:
            self.process.stdin.write(
                f"{statement};\n\\echo {marker.decode()}\n".encode()
            )
        except BrokenPipeError as error:
            raise RuntimeError("psql writer pipe closed") from error

        output: list[str] = []
        deadline = time.monotonic() + timeout
        while True:
            while b"\n" in self.pending:
                line, _, remainder = self.pending.partition(b"\n")
                self.pending = bytearray(remainder)
                line = line.rstrip(b"\r")
                if line == marker:
                    return [item for item in output if item != ""]
                output.append(line.decode("utf-8", "replace"))

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise TimeoutError(f"psql statement timed out: {statement[:120]}")
            readable, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not readable:
                self.close()
                raise TimeoutError(f"psql statement timed out: {statement[:120]}")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                status = self.process.poll()
                detail = "\n".join(
                    output + [self.pending.decode("utf-8", "replace")]
                )
                raise RuntimeError(f"psql exited ({status}): {detail}")
            self.pending.extend(chunk)

    def close(self) -> None:
        if self.process.poll() is None:
            if self.process.stdin is not None:
                try:
                    self.process.stdin.write(b"\\q\n")
                    self.process.stdin.close()
                except OSError:
                    pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)


def sql(query: str, timeout: float = PSQL_TIMEOUT) -> str:
    result = subprocess.run(
        [
            PSQL,
            "-X",
            "-q",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            PGHOST,
            "-p",
            PGPORT,
            "-d",
            PGDATABASE,
            "-c",
            query,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def metrics(timeout: float = PSQL_TIMEOUT) -> dict[str, int]:
    payload = sql(
        "SELECT json_build_object('cache_hits', cache_hits_total, "
        "'evictions', evictions_total, 'worker_starts_total', worker_starts_total, "
        "'cache_capacity', cache_capacity)::text FROM local_cache.metrics()",
        timeout=timeout,
    )
    return json.loads(payload)


def health(timeout: float = PSQL_TIMEOUT) -> dict[str, object]:
    return json.loads(sql("SELECT local_cache.health()::text", timeout=timeout))


def wait_for_health_ready(timeout: float = 15) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = health(timeout=max(0.1, deadline - time.monotonic()))
        if state.get("ready") is True:
            return state
        time.sleep(0.1)
    raise TimeoutError("local_cache.health() did not become ready")


def wait_for_cache_enabled(enabled: bool, timeout: float = 6) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        actual = health(timeout=max(0.1, remaining)).get("cache_enabled")
        if actual is enabled:
            return
        time.sleep(0.1)
    raise TimeoutError(f"pg_local_cache.enabled={enabled} did not become visible")


def restore_cache_configuration() -> None:
    sql("ALTER SYSTEM RESET pg_local_cache.enabled")
    sql("SELECT pg_reload_conf()")
    wait_for_cache_enabled(True, timeout=15)


def record_violation(
    violations: list[dict[str, object]],
    lock: threading.Lock,
    *,
    key: str,
    expected_floor: int | None,
    observed_value: object,
    reason: str,
) -> None:
    violation = {
        "key": key,
        "expected_floor": expected_floor,
        "observed_value": observed_value,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    }
    with lock:
        violations.append(violation)


def extract_rows(response: object) -> list[bytes | None] | None:
    if not isinstance(response, list):
        return None
    if any(row is not None and not isinstance(row, bytes) for row in response):
        return None
    return response


def validate_rows(
    response: object,
    keys: list[int],
    floors: list[int],
    previous: dict[int, int] | None,
    violations: list[dict[str, object]],
    violation_lock: threading.Lock,
) -> None:
    rows = extract_rows(response)
    if rows is None:
        for key, floor in zip(keys, floors):
            record_violation(
                violations,
                violation_lock,
                key=crud_key("stress_items", key),
                expected_floor=floor,
                observed_value=repr(response),
                reason="MGET response is not an array of bulk rows",
            )
        return

    if len(rows) != len(keys):
        for index in range(min(len(rows), len(keys)), len(keys)):
            record_violation(
                violations,
                violation_lock,
                key=crud_key("stress_items", keys[index]),
                expected_floor=floors[index],
                observed_value=None,
                reason="MGET response cardinality is too small",
            )
        for row in rows[len(keys) :]:
            record_violation(
                violations,
                violation_lock,
                key="<unexpected-extra-row>",
                expected_floor=None,
                observed_value=repr(row),
                reason="MGET response cardinality is too large",
            )

    for index, key in enumerate(keys[: len(rows)]):
        expected_floor = floors[index]
        row = rows[index]
        wire_key = crud_key("stress_items", key)
        if row is None:
            record_violation(
                violations,
                violation_lock,
                key=wire_key,
                expected_floor=expected_floor,
                observed_value=None,
                reason="MGET returned a missing row",
            )
            continue
        try:
            decoded = json.loads(row)
        except (UnicodeDecodeError, json.JSONDecodeError):
            record_violation(
                violations,
                violation_lock,
                key=wire_key,
                expected_floor=expected_floor,
                observed_value=row.decode("utf-8", "replace"),
                reason="row JSON is invalid",
            )
            continue

        observed = decoded.get("version") if isinstance(decoded, dict) else decoded
        if not isinstance(decoded, dict) or type(decoded.get("id")) is not int or type(observed) is not int or not isinstance(decoded.get("pad"), str):
            record_violation(
                violations,
                violation_lock,
                key=wire_key,
                expected_floor=expected_floor,
                observed_value=observed,
                reason="row JSON lacks numeric id/version or string pad",
            )
            continue
        if decoded["id"] != key:
            record_violation(
                violations,
                violation_lock,
                key=wire_key,
                expected_floor=expected_floor,
                observed_value=observed,
                reason=f"row id mismatch: expected {key}, got {decoded['id']}",
            )
        if observed < expected_floor:
            record_violation(
                violations,
                violation_lock,
                key=wire_key,
                expected_floor=expected_floor,
                observed_value=observed,
                reason="stale read below committed floor",
            )
        if observed >= ROLLBACK_MARKER:
            record_violation(
                violations,
                violation_lock,
                key=wire_key,
                expected_floor=expected_floor,
                observed_value=observed,
                reason="rolled-back version marker became visible",
            )
        if previous is not None:
            old = previous.get(key)
            if old is not None and observed < old:
                record_violation(
                    violations,
                    violation_lock,
                    key=wire_key,
                    expected_floor=expected_floor,
                    observed_value=observed,
                    reason=f"reader version went backwards from {old}",
                )
            previous[key] = max(old if old is not None else observed, observed)


def validate_exact_rows(
    response: object,
    keys: list[int],
    versions: list[int],
    violations: list[dict[str, object]],
    violation_lock: threading.Lock,
    *,
    reason: str,
) -> None:
    validate_rows(
        response,
        keys,
        versions,
        None,
        violations,
        violation_lock,
    )
    rows = extract_rows(response)
    if rows is None:
        return
    for key, row, expected in zip(keys, rows, versions):
        if row is None:
            continue
        try:
            decoded = json.loads(row)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(decoded, dict) or type(decoded.get("version")) is not int:
            continue
        if decoded["version"] != expected:
            record_violation(
                violations,
                violation_lock,
                key=crud_key("stress_items", key),
                expected_floor=expected,
                observed_value=decoded["version"],
                reason=reason,
            )


def toggle_kill_switch(
    hot_keys: list[int],
    committed: dict[int, int],
    committed_lock: threading.Lock,
    violations: list,
    violation_lock: threading.Lock,
) -> None:
    def set_enabled(enabled: bool) -> None:
        value = "on" if enabled else "off"
        sql(f"ALTER SYSTEM SET pg_local_cache.enabled = '{value}'")
        sql("SELECT pg_reload_conf()")

    connection = RespConnection()
    try:
        def read_hot_keys() -> None:
            with committed_lock:
                floors = [committed[key] for key in hot_keys]
            response = connection.command(
                "MGET", *(crud_key("stress_items", key) for key in hot_keys)
            )
            validate_rows(
                response,
                hot_keys,
                floors,
                None,
                violations,
                violation_lock,
            )

        # Prime the same RESP worker and key set used for the off/on proof.
        read_hot_keys()
        read_hot_keys()
        set_enabled(False)
        wait_for_cache_enabled(False)

        deadline = time.monotonic() + KILL_SWITCH_TIMEOUT
        quiet_since = time.monotonic()
        quiet_hits = metrics()["cache_hits"]
        completed_reads = 0
        while time.monotonic() < deadline:
            read_hot_keys()
            completed_reads += 1
            remaining = deadline - time.monotonic()
            current_hits = metrics(timeout=max(0.5, min(PSQL_TIMEOUT, remaining)))[
                "cache_hits"
            ]
            now = time.monotonic()
            if current_hits != quiet_hits:
                # Allow reload propagation, then require a complete quiet window.
                quiet_hits = current_hits
                quiet_since = now
                completed_reads = 0
            elif (
                completed_reads >= KILL_SWITCH_MIN_READS
                and now - quiet_since >= KILL_SWITCH_WINDOW
            ):
                break
        else:
            raise TimeoutError(
                "cache_hits did not stay flat for 1s and 200 RESP reads while disabled"
            )

        set_enabled(True)
        wait_for_cache_enabled(True)
        deadline = time.monotonic() + KILL_SWITCH_TIMEOUT
        hits_before_resume = metrics()["cache_hits"]
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            read_hot_keys()
            current_hits = metrics(timeout=max(0.5, min(PSQL_TIMEOUT, remaining)))[
                "cache_hits"
            ]
            if current_hits > hits_before_resume:
                return
            if current_hits < hits_before_resume:
                hits_before_resume = current_hits
        raise TimeoutError("cache hits did not resume after enabling the cache")
    finally:
        connection.close()


def main() -> None:
    pad = "x" * PAD_BYTES
    sql(
        "DROP TABLE IF EXISTS public.stress_items CASCADE; "
        "CREATE TABLE public.stress_items ("
        "id bigint PRIMARY KEY, version bigint NOT NULL, pad text NOT NULL); "
        f"INSERT INTO public.stress_items SELECT i, 0, '{pad}' "
        f"FROM generate_series(1, {STRESS_KEYS}) AS i; "
        "GRANT SELECT ON public.stress_items TO local_cache_worker; "
        "SELECT local_cache.attach_table('public.stress_items'::regclass)"
    )
    namespace = sql(
        "SELECT namespace FROM local_cache.mapping "
        "WHERE relation = 'public.stress_items'::regclass"
    )
    if not namespace:
        raise AssertionError("stress_items mapping was not created")

    committed = {key: 0 for key in range(1, STRESS_KEYS + 1)}
    committed_lock = threading.Lock()
    violations: list[dict[str, object]] = []
    violation_lock = threading.Lock()
    exceptions: list[tuple[str, BaseException]] = []
    exception_lock = threading.Lock()
    counters = {
        "commits": 0,
        "rollbacks": 0,
        "reads": 0,
        "invalidations": 0,
        "toggle_events": 0,
        "garbage_inputs": 0,
    }
    counter_lock = threading.Lock()
    reader_counts = [0 for _ in range(STRESS_READERS)]
    capacity_metrics = metrics()
    if STRESS_KEYS <= capacity_metrics["cache_capacity"]:
        raise AssertionError(
            f"PGLC_STRESS_KEYS={STRESS_KEYS} must exceed cache capacity "
            f"{capacity_metrics['cache_capacity']} to test eviction churn"
        )
    if capacity_metrics["cache_capacity"] < TERMINAL_HOT_KEYS:
        raise AssertionError(
            f"terminal hot phase needs {TERMINAL_HOT_KEYS} cache entries; "
            f"capacity is {capacity_metrics['cache_capacity']}"
        )

    client = RespConnection()
    try:
        first = crud_key("stress_items", 1)
        initial = wait_for_mapping(client, first)
        decoded_initial = json.loads(initial)
        assert decoded_initial == {"id": 1, "version": 0, "pad": pad}
        wait_for_health_ready()
        baseline = metrics()

        # Deterministic hot-set phase: show cache hits before adding concurrency.
        hot_keys = list(range(1, 17))
        client.command("MGET", *(crud_key("stress_items", key) for key in hot_keys))
        hot_before = metrics()["cache_hits"]
        for _ in range(12):
            response = client.command(
                "MGET", *(crud_key("stress_items", key) for key in hot_keys)
            )
            validate_rows(
                response,
                hot_keys,
                [0] * len(hot_keys),
                None,
                violations,
                violation_lock,
            )
        if metrics()["cache_hits"] <= hot_before:
            raise AssertionError("hot-set phase did not increase cache_hits")

        # Deterministic full-keyspace phase: keyspace exceeds cache capacity.
        eviction_before = metrics()["evictions"]
        for start in range(1, STRESS_KEYS + 1, 16):
            keys = list(range(start, min(start + 16, STRESS_KEYS + 1)))
            response = client.command(
                "MGET", *(crud_key("stress_items", key) for key in keys)
            )
            validate_rows(
                response,
                keys,
                [0] * len(keys),
                None,
                violations,
                violation_lock,
            )
        if metrics()["evictions"] <= eviction_before:
            raise AssertionError("full-keyspace phase did not increase evictions")
    finally:
        client.close()

    abort = threading.Event()
    writer_stop = threading.Event()
    reader_stop = threading.Event()
    event_stop = threading.Event()
    garbage_stop = threading.Event()
    started_at = time.monotonic()

    def fail_thread(name: str, error: BaseException) -> None:
        with exception_lock:
            exceptions.append((name, error))
        abort.set()
        event_stop.set()

    def writer(index: int) -> None:
        session: PsqlSession | None = None
        rng = random.Random(0xA11CE + index)
        try:
            session = PsqlSession()
            if session.query("SELECT 1") != ["1"]:
                raise AssertionError("writer psql session did not start cleanly")
            while not writer_stop.is_set():
                key = rng.randint(1, STRESS_KEYS)
                if rng.random() < 0.8:
                    output = session.query(
                        "UPDATE public.stress_items SET version = version + 1 "
                        f"WHERE id = {key} RETURNING version"
                    )
                    if len(output) != 1 or not output[0].isdigit():
                        raise AssertionError(f"bad committed UPDATE result: {output!r}")
                    version = int(output[0])
                    if version >= ROLLBACK_MARKER:
                        raise AssertionError("committed version reached rollback marker")
                    with committed_lock:
                        committed[key] = max(committed[key], version)
                    with counter_lock:
                        counters["commits"] += 1
                else:
                    output = session.query(
                        "BEGIN; "
                        "UPDATE public.stress_items "
                        f"SET version = version + {ROLLBACK_MARKER} "
                        f"WHERE id = {key} RETURNING version; ROLLBACK"
                    )
                    if len(output) != 1 or not output[0].isdigit():
                        raise AssertionError(f"bad rolled-back UPDATE result: {output!r}")
                    if int(output[0]) < ROLLBACK_MARKER:
                        raise AssertionError("rollback marker was not applied in transaction")
                    with counter_lock:
                        counters["rollbacks"] += 1
        except BaseException as error:
            fail_thread(f"writer-{index}", error)
        finally:
            if session is not None:
                try:
                    session.close()
                except BaseException as error:
                    fail_thread(f"writer-{index}", error)

    def reader(index: int) -> None:
        rng = random.Random(0xB0B + index)
        previous: dict[int, int] = {}
        try:
            connection = RespConnection()
            try:
                while not reader_stop.is_set():
                    keys = [rng.randint(1, STRESS_KEYS) for _ in range(rng.randint(1, 16))]
                    with committed_lock:
                        floors = [committed[key] for key in keys]
                    response = connection.command(
                        "MGET",
                        *(crud_key("stress_items", key) for key in keys),
                    )
                    validate_rows(
                        response,
                        keys,
                        floors,
                        previous,
                        violations,
                        violation_lock,
                    )
                    reader_counts[index] += 1
                    with counter_lock:
                        counters["reads"] += 1
            finally:
                connection.close()
        except BaseException as error:
            fail_thread(f"reader-{index}", error)

    def event_worker() -> None:
        did_toggle = False
        next_invalidation = time.monotonic() + 1
        toggle_at = started_at + max(1, min(3, STRESS_SECONDS / 5))
        try:
            while not event_stop.is_set():
                now = time.monotonic()
                if now >= next_invalidation:
                    escaped_namespace = namespace.replace("'", "''")
                    sql(f"SELECT local_cache.invalidate('{escaped_namespace}')")
                    with counter_lock:
                        counters["invalidations"] += 1
                    next_invalidation = now + 2
                if not did_toggle and now >= toggle_at:
                    toggle_kill_switch(
                        hot_keys, committed, committed_lock, violations, violation_lock
                    )
                    did_toggle = True
                    with counter_lock:
                        counters["toggle_events"] += 1
                event_stop.wait(0.1)
        except BaseException as error:
            fail_thread("event-worker", error)

    malformed_cases = [
        None,
        b"*999999999\r\n",
        b"*-2\r\n",
        b"*x\r\n",
        b"*1\r\n$1048577\r\n",
        RespConnection.encode("MGET", f"CRUD:{PGDATABASE}.public.stress_items:{{broken"),
        RespConnection.encode("MGET", f"CRUD:{PGDATABASE}.public.no_such_table:{{\"id\":\"1\"}}"),
        RespConnection.encode("MGET", f"CRUD:other_database.public.stress_items:{{\"id\":\"1\"}}"),
        RespConnection.encode(
            "MGET",
            f"CRUD:{PGDATABASE}.public.stress_items:{{\"id\":{'[' * 100}0{']' * 100}}}",
        ),
        RespConnection.encode("MGET", f"CRUD:{PGDATABASE}.public.stress_items:{{\"id\":null}}"),
        RespConnection.encode("MGET", b"CRUD:" + PGDATABASE.encode() + b".public.stress_items:{\xff}"),
    ]

    def garbage_worker() -> None:
        index = 0
        rng = random.Random(0xF022)
        try:
            while not garbage_stop.is_set():
                authenticated = index % 2 == 0
                connection = RespConnection(authenticate=authenticated)
                try:
                    payload = malformed_cases[index % len(malformed_cases)]
                    if payload is None:
                        payload = os.urandom(rng.randint(1, 64))
                    connection.socket.sendall(payload)
                    connection.socket.settimeout(0.1)
                    try:
                        connection.socket.recv(1024)
                    except (OSError, TimeoutError):
                        pass
                    with counter_lock:
                        counters["garbage_inputs"] += 1
                except OSError:
                    # Malformed requests are allowed to close the connection.
                    with counter_lock:
                        counters["garbage_inputs"] += 1
                finally:
                    connection.close()
                index += 1
                garbage_stop.wait(0.05)
        except BaseException as error:
            fail_thread("garbage-worker", error)

    writer_threads = [
        threading.Thread(target=writer, args=(i,), name=f"writer-{i}")
        for i in range(STRESS_WRITERS)
    ]
    reader_threads = [
        threading.Thread(target=reader, args=(i,), name=f"reader-{i}")
        for i in range(STRESS_READERS)
    ]
    event_threads = [threading.Thread(target=event_worker, name="event-worker")]
    garbage_threads = [threading.Thread(target=garbage_worker, name="garbage-worker")]

    def start_group(group: list[threading.Thread]) -> None:
        for thread in group:
            thread.start()

    def drain_group(
        group: list[threading.Thread], timeout: float = JOIN_TIMEOUT
    ) -> list[str]:
        deadline = time.monotonic() + timeout
        for thread in group:
            if thread.ident is not None:
                thread.join(max(0, deadline - time.monotonic()))
        return [
            thread.name
            for thread in group
            if thread.ident is not None and thread.is_alive()
        ]

    alive: list[str] = []
    try:
        start_group(writer_threads)
        start_group(reader_threads)
        start_group(event_threads)
        start_group(garbage_threads)
        abort.wait(STRESS_SECONDS)
    finally:
        # Drain configuration-changing work while readers and writers remain live.
        event_stop.set()
        alive.extend(drain_group(event_threads, EVENT_JOIN_TIMEOUT))
        writer_stop.set()
        alive.extend(drain_group(writer_threads))
        reader_stop.set()
        alive.extend(drain_group(reader_threads))
        garbage_stop.set()
        alive.extend(drain_group(garbage_threads))
        restore_cache_configuration()

    if alive:
        raise TimeoutError(f"stress threads did not stop before deadline: {alive}")

    with exception_lock:
        thread_errors = list(exceptions)
    if thread_errors:
        for name, error in thread_errors:
            print(f"{name} failed: {type(error).__name__}: {error}")
        raise AssertionError(f"{len(thread_errors)} stress thread(s) failed")
    with counter_lock:
        totals = dict(counters)

    # Terminal cache set fits capacity and is checked before the full scan can evict it.
    terminal_keys = list(range(1, TERMINAL_HOT_KEYS + 1))
    terminal_writer = PsqlSession()
    try:
        updated_ids = terminal_writer.query(
            "UPDATE public.stress_items SET version = version + 1 "
            f"WHERE id BETWEEN 1 AND {TERMINAL_HOT_KEYS} RETURNING id"
        )
    finally:
        terminal_writer.close()
    if sorted(int(key) for key in updated_ids) != terminal_keys:
        raise AssertionError(
            f"terminal writer updated {len(updated_ids)} rows, expected {TERMINAL_HOT_KEYS}"
        )

    terminal_responses: list[tuple[list[int], object]] = []
    terminal_connection = RespConnection()
    try:
        for _ in range(2):
            for start in range(1, TERMINAL_HOT_KEYS + 1, 16):
                keys = list(range(start, min(start + 16, TERMINAL_HOT_KEYS + 1)))
                response = terminal_connection.command(
                    "MGET", *(crud_key("stress_items", key) for key in keys)
                )
                terminal_responses.append((keys, response))
    finally:
        terminal_connection.close()

    terminal_sql_rows = sql(
        "SELECT id || '|' || version FROM public.stress_items "
        f"WHERE id BETWEEN 1 AND {TERMINAL_HOT_KEYS} ORDER BY id",
        timeout=15,
    ).splitlines()
    terminal_latest: dict[int, int] = {}
    for row in terminal_sql_rows:
        key_text, version_text = row.split("|", 1)
        terminal_latest[int(key_text)] = int(version_text)
    if sorted(terminal_latest) != terminal_keys:
        raise AssertionError(
            f"terminal SQL scan returned {len(terminal_latest)} rows, "
            f"expected {TERMINAL_HOT_KEYS}"
        )
    for keys, response in terminal_responses:
        validate_exact_rows(
            response,
            keys,
            [terminal_latest[key] for key in keys],
            violations,
            violation_lock,
            reason="terminal hot read differs from stopped-writer SQL state",
        )

    # No writers or events remain; terminal hot entries were checked above.
    sql_rows = sql(
        "SELECT id || '|' || version FROM public.stress_items ORDER BY id",
        timeout=15,
    ).splitlines()
    latest: dict[int, int] = {}
    for row in sql_rows:
        key_text, version_text = row.split("|", 1)
        latest[int(key_text)] = int(version_text)
    if len(latest) != STRESS_KEYS:
        raise AssertionError(f"final SQL scan returned {len(latest)} rows")

    final_connection = RespConnection()
    try:
        for start in range(1, STRESS_KEYS + 1, 16):
            keys = list(range(start, min(start + 16, STRESS_KEYS + 1)))
            response = final_connection.command(
                "MGET", *(crud_key("stress_items", key) for key in keys)
            )
            validate_exact_rows(
                response,
                keys,
                [latest[key] for key in keys],
                violations,
                violation_lock,
                reason="final RESP scan differs from stopped-writer SQL state",
            )
    finally:
        final_connection.close()

    final_metrics = metrics()
    final_health = health()
    with violation_lock:
        recorded_violations = list(violations)

    if recorded_violations:
        print(json.dumps(recorded_violations, indent=2, sort_keys=True))
    if totals["commits"] <= 0 or totals["rollbacks"] <= 0:
        raise AssertionError(f"stress phase lacked commits or rollbacks: {totals}")
    if totals["invalidations"] <= 0 or totals["toggle_events"] != 1:
        raise AssertionError(f"stress phase lacked completed events: {totals}")
    if totals["garbage_inputs"] <= 0:
        raise AssertionError("malformed-input thread completed no requests")
    if any(count < max(5, int(STRESS_SECONDS // 2)) for count in reader_counts):
        raise AssertionError(f"readers completed too few MGETs: {reader_counts}")
    if final_metrics["cache_hits"] <= baseline["cache_hits"]:
        raise AssertionError("cache_hits did not increase")
    if final_metrics["evictions"] <= baseline["evictions"]:
        raise AssertionError("evictions did not increase")
    if final_metrics["worker_starts_total"] != baseline["worker_starts_total"]:
        raise AssertionError(
            "RESP worker restarted: "
            f"{baseline['worker_starts_total']} -> {final_metrics['worker_starts_total']}"
        )
    if final_health.get("ready") is not True:
        raise AssertionError(f"health() is not ready: {final_health}")
    if recorded_violations:
        raise AssertionError(f"stale-read stress found {len(recorded_violations)} violation(s)")

    print(
        "stress ok: "
        f"seconds={STRESS_SECONDS:g} keys={STRESS_KEYS} "
        f"writers={STRESS_WRITERS} readers={STRESS_READERS} "
        f"commits={totals['commits']} rollbacks={totals['rollbacks']} "
        f"reads={totals['reads']} invalidations={totals['invalidations']} "
        f"garbage={totals['garbage_inputs']} "
        f"cache_hits+={final_metrics['cache_hits'] - baseline['cache_hits']} "
        f"evictions+={final_metrics['evictions'] - baseline['evictions']}"
    )


if __name__ == "__main__":
    main()
