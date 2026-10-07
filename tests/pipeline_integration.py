#!/usr/bin/env python3
"""Adversarial black-box tests for the RESP pipeline/event-loop hot path.

The regular integration suite concentrates on cache semantics.  This script
keeps a single connection busy in the ways that are easy to regress while
optimising the worker: an incomplete suffix after complete commands, output
backpressure, a fairness yield, and close-after-flush.  It also verifies that
the optimised warm-hit path retains the transactional invalidation fence.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import select
import ssl
import socket
import subprocess
import threading
import time
from collections.abc import Callable


PSQL = os.environ.get("PG_LOCAL_CACHE_PSQL", "psql")
PGHOST = os.environ.get("PGHOST", "127.0.0.1")
PGPORT = os.environ.get("PGPORT", "5432")
PGDATABASE = os.environ.get("PGDATABASE", "postgres")
RESP_HOST = os.environ.get("PG_LOCAL_CACHE_RESP_HOST", "127.0.0.1")
RESP_PORT = int(os.environ.get("PG_LOCAL_CACHE_RESP_PORT", "6380"))
AUTH_TOKEN = os.environ.get("PG_LOCAL_CACHE_AUTH_TOKEN", "")
TLS_CA_FILE = os.environ.get("PG_LOCAL_CACHE_TLS_CA", "")
TLS_CERT_FILE = os.environ.get("PG_LOCAL_CACHE_TLS_CERT", "")
TLS_KEY_FILE = os.environ.get("PG_LOCAL_CACHE_TLS_KEY", "")
WORKER_ROLE = os.environ.get("PG_LOCAL_CACHE_TEST_ROLE", "")
WRITER_ROLE = os.environ.get("PG_LOCAL_CACHE_TEST_WRITER_ROLE", "")
WRITER_PASSWORD = os.environ.get("PG_LOCAL_CACHE_TEST_WRITER_PASSWORD", "")
WRITER_HOST = os.environ.get("PG_LOCAL_CACHE_TEST_WRITER_HOST", "127.0.0.1")
_NEXT_TEST_ROW_ID = 10_000_000_000 + (os.getpid() % 100_000) * 100_000
BACKPRESSURE_VALUE_BYTES = 3_900
MAX_PIPELINE_INPUT_BYTES = 65_536
MAX_RESPONSE_BYTES = 65_536 + 1_024
# Mirrors PGLC_OUTPUT_BUFFER_MAX in src/pg_local_cache_worker.c.
PGLC_OUTPUT_BUFFER_MAX_BYTES = MAX_RESPONSE_BYTES + 16 * 1024

if WORKER_ROLE and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,62}", WORKER_ROLE):
    raise ValueError("PG_LOCAL_CACHE_TEST_ROLE is not a safe SQL identifier")
if WRITER_ROLE and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,62}", WRITER_ROLE):
    raise ValueError("PG_LOCAL_CACHE_TEST_WRITER_ROLE is not a safe SQL identifier")
if bool(WRITER_ROLE) != bool(WRITER_PASSWORD):
    raise ValueError("writer role and password must be configured together")


class RespError(RuntimeError):
    """An error frame returned by the RESP server."""


class PsqlError(subprocess.CalledProcessError):
    """A psql failure whose exception text includes captured output."""

    def __str__(self) -> str:
        return (
            f"{super().__str__()}\nstdout:\n{self.stdout or ''}"
            f"\nstderr:\n{self.stderr or ''}"
        )


_WARM_HIT_ITERATION: contextvars.ContextVar[
tuple[str, str, str] | None
] = contextvars.ContextVar("warm_hit_iteration", default=None)
_PSQL_PROCESS_OUTPUT: dict[int, bytearray] = {}
_PGLC_TEST_HOOKS_AVAILABLE = False


def warm_hit_diagnostics(actual: object) -> str:
    iteration = _WARM_HIT_ITERATION.get()
    if iteration is None:
        return f"actual={actual!r}"
    try:
        stats: object = read_cache_stats()
    except Exception as error:
        stats = f"read_cache_stats failed: {error!r}"
    return (
        f"actual={actual!r}, iteration={iteration!r}, "
        f"read_cache_stats()={stats!r}"
    )


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class RespConnection:
    def __init__(
        self,
        *,
        authenticate: bool = True,
        receive_buffer: int | None = None,
        socket_timeout: float = 5,
    ) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if receive_buffer is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
        self.socket.settimeout(socket_timeout)
        self.socket.connect((RESP_HOST, RESP_PORT))
        if TLS_CA_FILE:
            if bool(TLS_CERT_FILE) != bool(TLS_KEY_FILE):
                raise ValueError("TLS client certificate and key must be configured together")
            context = ssl.create_default_context(cafile=TLS_CA_FILE)
            if TLS_CERT_FILE:
                context.load_cert_chain(TLS_CERT_FILE, TLS_KEY_FILE)
            self.socket = context.wrap_socket(
                self.socket, server_hostname="127.0.0.1"
            )
        self.buffer = bytearray()
        self.position = 0
        if authenticate and AUTH_TOKEN:
            response = self.command("AUTH", AUTH_TOKEN)
            assert response == "OK", warm_hit_diagnostics(response)

    def close(self) -> None:
        self.socket.close()

    @staticmethod
    def encode(*arguments: object) -> bytes:
        encoded = [
            argument if isinstance(argument, bytes) else str(argument).encode()
            for argument in arguments
        ]
        parts = [f"*{len(encoded)}\r\n".encode()]
        for argument in encoded:
            parts.extend((f"${len(argument)}\r\n".encode(), argument, b"\r\n"))
        return b"".join(parts)

    def command(self, *arguments: object) -> object:
        self.socket.sendall(self.encode(*arguments))
        return self.read_response()

    def _receive(self) -> None:
        chunk = self.socket.recv(65536)
        if not chunk:
            raise EOFError("RESP connection closed")
        if self.position == len(self.buffer):
            self.buffer.clear()
            self.position = 0
        self.buffer.extend(chunk)

    def _compact(self) -> None:
        if self.position == len(self.buffer):
            self.buffer.clear()
            self.position = 0
        elif self.position >= 65536 and self.position * 2 >= len(self.buffer):
            del self.buffer[: self.position]
            self.position = 0

    def _read_exact(self, length: int) -> bytes:
        while len(self.buffer) - self.position < length:
            self._receive()
        start = self.position
        self.position += length
        result = bytes(self.buffer[start : self.position])
        self._compact()
        return result

    def _read_line(self) -> bytes:
        while True:
            end = self.buffer.find(b"\r\n", self.position)
            if end >= 0:
                result = bytes(self.buffer[self.position : end])
                self.position = end + 2
                self._compact()
                return result
            self._receive()

    def read_response(self) -> object:
        prefix = self._read_exact(1)
        if prefix == b"+":
            return self._read_line().decode()
        if prefix == b"-":
            raise RespError(self._read_line().decode("utf-8", "replace"))
        if prefix == b":":
            return int(self._read_line())
        if prefix == b"$":
            length = int(self._read_line())
            if length == -1:
                return None
            value = self._read_exact(length)
            if self._read_exact(2) != b"\r\n":
                raise ValueError("bulk response is not terminated by CRLF")
            return value
        if prefix == b"*":
            length = int(self._read_line())
            if length == -1:
                return None
            return [self.read_response() for _ in range(length)]
        raise ValueError(f"unsupported RESP prefix {prefix!r}")


def mget_one(client: RespConnection, key: str) -> object:
    response = client.command("MGET", key)
    assert isinstance(response, list) and len(response) == 1, warm_hit_diagnostics(
        {"key": key, "response": response}
    )
    return response[0]


def psql_args(query: str) -> list[str]:
    return psql_base_args() + ["-c", query]


def psql_base_args() -> list[str]:
    return [
        PSQL,
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-h",
        PGHOST,
        "-p",
        PGPORT,
        "-d",
        PGDATABASE,
        "-Atq",
    ]


def run_psql(
    arguments: list[str], *, statement: str, timeout: float = 30
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        raise RuntimeError(
            f"psql timed out after {timeout}s for {statement!r}\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        ) from error


def checked_psql(arguments: list[str], *, statement: str, timeout: float = 30) -> str:
    result = run_psql(arguments, statement=statement, timeout=timeout)
    if result.returncode != 0:
        raise PsqlError(
            result.returncode,
            arguments,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result.stdout.strip()


def write_psql_input(process: subprocess.Popen[str], statement: str) -> None:
    if process.stdin is None:
        raise RuntimeError(
            "psql stdin unavailable: " + _psql_process_output_so_far(process)
        )
    try:
        process.stdin.write(statement)
        process.stdin.flush()
    except OSError as error:
        raise RuntimeError(
            f"psql input failed: {_psql_process_output_so_far(process)}"
        ) from error


def close_psql_input(process: subprocess.Popen[str]) -> None:
    if process.stdin is None:
        raise RuntimeError(
            "psql stdin unavailable: " + _psql_process_output_so_far(process)
        )
    try:
        process.stdin.close()
    except OSError as error:
        raise RuntimeError(
            f"psql close failed: {_psql_process_output_so_far(process)}"
        ) from error
    process.stdin = None


def sql(query: str) -> str:
    return checked_psql(psql_args(query), statement=query)


def guc_number(name: str) -> int:
    """Read a numeric GUC in pg_settings' base units."""
    return int(
        sql(
            "SELECT setting FROM pg_catalog.pg_settings "
            f"WHERE name = {sql_literal(name)}"
        )
    )


def sql_commands(*queries: str) -> str:
    args = psql_base_args()
    for query in queries:
        args.extend(("-c", query))
    return checked_psql(args, statement="\n".join(queries))


def allocate_test_row_ids(_table: str, count: int = 1) -> list[int]:
    """Return distinct IDs from this process's reserved test range."""
    global _NEXT_TEST_ROW_ID
    if count < 1:
        raise ValueError("count must be positive")
    first_id = _NEXT_TEST_ROW_ID
    _NEXT_TEST_ROW_ID += count
    return list(range(first_id, _NEXT_TEST_ROW_ID))


def wait_for_mapping(client: RespConnection, key: str) -> bytes:
    deadline = time.monotonic() + 10
    while True:
        try:
            value = mget_one(client, key)
            assert isinstance(value, bytes)
            return value
        except RespError as error:
            if "unknown KVik table mapping" not in str(error):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def assert_eventual_value(client: RespConnection, key: str, expected: bytes) -> None:
    deadline = time.monotonic() + 5
    while True:
        value = mget_one(client, key)
        if value == expected:
            return
        if time.monotonic() >= deadline:
            raise AssertionError((value, expected))
        time.sleep(0.01)


def row_bytes(row_id: int, value: str) -> bytes:
    return json.dumps(
        {"id": row_id, "value": value}, separators=(",", ":")
    ).encode()


def crud_key(
    table: str, row_id: int | str, *, database: str = PGDATABASE
) -> str:
    return (
        f"CRUD:{database}.public.{table}:"
        + json.dumps({"id": str(row_id)}, separators=(",", ":"))
    )


def crud_key_json_number(table: str, row_id: int) -> str:
    return (
        f"CRUD:{PGDATABASE}.public.{table}:"
        + json.dumps({"id": row_id}, separators=(",", ":"))
    )


def canonical_int8_key(value: int) -> str:
    decimal = str(value)
    return f"{len(decimal)}:{decimal};"


def composite_key(table: str, tenant: str, row_id: int | str) -> str:
    return (
        f"CRUD:{PGDATABASE}.public.{table}:"
        + json.dumps(
            {"tenant": tenant, "id": str(row_id)}, separators=(",", ":")
        )
    )


def composite_row_bytes(tenant: str, row_id: int, value: str) -> bytes:
    return json.dumps(
        {"tenant": tenant, "id": row_id, "value": value},
        separators=(",", ":"),
    ).encode()


def start_idle_transaction(
    statements: str, *, application_name: str, admin_connection: bool = False
) -> subprocess.Popen[str]:
    role = (
        f"SET ROLE {sql_identifier(WORKER_ROLE)};"
        if WORKER_ROLE and not WRITER_ROLE and not admin_connection
        else ""
    )
    arguments = psql_base_args()
    environment = None
    if WRITER_ROLE and not admin_connection:
        arguments.extend(("-h", WRITER_HOST, "-U", WRITER_ROLE))
        environment = os.environ.copy()
        environment["PGPASSWORD"] = WRITER_PASSWORD
    process = subprocess.Popen(
        arguments,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
    )
    _PSQL_PROCESS_OUTPUT[process.pid] = bytearray()
    write_psql_input(
        process,
        f"{role}BEGIN; {statements};"
        f"SET application_name = '{application_name}';\n",
    )
    deadline = time.monotonic() + 10
    writer_identity = (
        f"AND usename = '{WRITER_ROLE}' "
        if WRITER_ROLE and not admin_connection
        else ""
    )
    while sql(
        "SELECT count(*) FROM pg_catalog.pg_stat_activity "
        f"WHERE application_name = '{application_name}' "
        f"{writer_identity}"
        "AND state = 'idle in transaction'"
    ) != "1":
        if process.poll() is not None:
            output = _finish_tracked_psql(process)
            raise AssertionError(
                warm_hit_diagnostics(
                    {"writer": application_name, "output": output}
                )
            )
        if time.monotonic() >= deadline:
            process.terminate()
            output = _finish_tracked_psql(process)
            raise AssertionError(
                warm_hit_diagnostics(
                    {"writer": application_name, "output": output}
                )
            )
        time.sleep(0.02)
    return process


def start_idle_writer(
    table: str,
    value: str,
    *,
    application_name: str,
    row_id: int = 1,
) -> subprocess.Popen[str]:
    return start_idle_transaction(
        f"UPDATE public.{sql_identifier(table)} SET value = '{value}' "
        f"WHERE id = {row_id}",
        application_name=application_name,
    )


def start_table_locker(
    table: str, *, application_name: str
) -> subprocess.Popen[str]:
    return start_idle_transaction(
        f"LOCK TABLE public.{sql_identifier(table)} IN ACCESS EXCLUSIVE MODE",
        application_name=application_name,
    )


def finish_writer(process: subprocess.Popen[str], *, commit: bool) -> str:
    write_psql_input(process, "COMMIT;\n" if commit else "ROLLBACK;\n")
    close_psql_input(process)
    output = _finish_tracked_psql(process)
    assert process.returncode == 0, warm_hit_diagnostics(
        {"returncode": process.returncode, "output": output}
    )
    return output


def terminate_writer(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)


def test_fragmented_suffix_and_order(table: str) -> None:
    client = RespConnection()
    try:
        ping = client.encode("PING")
        mget = client.encode("MGET", crud_key(table, 1))
        echo = client.encode("ECHO", "after-fragment")
        split = len(mget) - 3

        # Reading PONG proves the server parsed the complete prefix while the
        # next frame was still incomplete and retained in its input buffer.
        client.socket.sendall(ping + mget[:split])
        assert client.read_response() == "PONG"
        for byte in mget[split:] + echo:
            client.socket.sendall(bytes((byte,)))
        assert client.read_response() == [row_bytes(1, "initial")]
        assert client.read_response() == b"after-fragment"
        assert client.command("PING") == "PONG"
    finally:
        client.close()


def test_command_error_does_not_poison_batch(table: str) -> None:
    client = RespConnection()
    try:
        client.socket.sendall(
            client.encode("PING")
            + client.encode("MGET", crud_key(table, "not-a-bigint"))
            + client.encode("MGET", crud_key(table, 1))
            + client.encode("ECHO", "after-error")
        )
        assert client.read_response() == "PONG"
        try:
            client.read_response()
            raise AssertionError("invalid key did not return a PostgreSQL error")
        except RespError as error:
            assert "PostgreSQL" in str(error)
        assert client.read_response() == [row_bytes(1, "initial")]
        assert client.read_response() == b"after-error"
        assert client.command("PING") == "PONG"
    finally:
        client.close()


def test_warm_pipeline_has_no_sql_reads(table: str) -> None:
    client = RespConnection()
    try:
        key = crud_key(table, 1)
        expected = row_bytes(1, "initial")
        assert mget_one(client, key) == expected
        before = json.loads(client.command("STAT"))
        count = 128
        client.socket.sendall(client.encode("MGET", key) * count)
        for _ in range(count):
            assert client.read_response() == [expected]
        after = json.loads(client.command("STAT"))
        assert after["cache_misses"] - before["cache_misses"] == 0
        assert after["database_reads"] - before["database_reads"] == 0
        assert after["cache_hits"] - before["cache_hits"] == count
    finally:
        client.close()


def test_fast_mget_hit_has_no_worker_palloc(table: str) -> None:
    client = RespConnection()
    try:
        expected = row_bytes(1, "initial")
        keys = (
            ("quoted decimal", crud_key(table, 1)),
            ("JSON number", crud_key_json_number(table, 1)),
        )

        for key_form, key in keys:
            for _ in range(8):
                assert mget_one(client, key) == expected

            def measure_allocations(hit_count: int) -> int:
                before = json.loads(client.command("STAT"))
                total = 0
                for _ in range(hit_count):
                    assert mget_one(client, key) == expected
                    total += int(
                        client.command("PGLC_TEST_LAST_REQUEST_PALLOC_COUNT")
                    )
                after = json.loads(client.command("STAT"))
                observed_hits = int(after["fast_path_hits"]) - int(
                    before["fast_path_hits"]
                )
                fallback_reasons = {
                    reason: int(after[f"fast_path_fallback_{reason}"])
                    - int(before[f"fast_path_fallback_{reason}"])
                    for reason in (
                        "key_form",
                        "mapping_shape",
                        "multi_key",
                        "cache_state",
                    )
                }
                fallback_total = int(after["fast_path_fallbacks"]) - int(
                    before["fast_path_fallbacks"]
                )
                assert fallback_total == sum(fallback_reasons.values()), (
                    "fast-path fallback total differs from reason counters: "
                    f"total={fallback_total}, reasons={fallback_reasons}"
                )
                assert observed_hits == hit_count, (
                    f"warm single-key MGET fast-path hits for {key_form}: "
                    f"expected {hit_count}, observed {observed_hits}; "
                    f"fallback reasons={fallback_reasons}"
                )
                return total

            short_run_hits = 64
            long_run_hits = 256
            short_run_allocations = measure_allocations(short_run_hits)
            long_run_allocations = measure_allocations(long_run_hits)
            assert long_run_allocations <= short_run_allocations, (
                "warm single-key MGET allocations grew with hit count "
                f"for {key_form}: {short_run_allocations} for "
                f"{short_run_hits}, {long_run_allocations} for {long_run_hits}"
            )
            assert max(short_run_allocations, long_run_allocations) <= 4, (
                "warm single-key MGET has too many constant allocations "
                f"for {key_form}: {short_run_allocations} for "
                f"{short_run_hits}, {long_run_allocations} for {long_run_hits}"
            )
    finally:
        client.close()


def test_hot_counter_shards_aggregate() -> None:
    before = read_cache_stats()
    client = RespConnection()
    try:
        assert client.command("PING") == "PONG"
    finally:
        client.close()
    after = read_cache_stats()
    expected = 2 if AUTH_TOKEN else 1  # AUTH, when enabled, is a RESP request too.
    assert int(after["client_requests"]) - int(before["client_requests"]) == expected


def test_mget(table: str, composite_table: str, scoped_table: str) -> None:
    unauthenticated = RespConnection(authenticate=False)
    try:
        try:
            unauthenticated.command("MGET", crud_key(table, 1))
            raise AssertionError("unauthenticated MGET did not fail")
        except RespError as error:
            assert "NOAUTH" in str(error)
    finally:
        unauthenticated.close()

    client = RespConnection()
    try:
        try:
            client.command("MGET")
            raise AssertionError("zero-key MGET did not fail")
        except RespError as error:
            assert "wrong number of arguments" in str(error)

        key_one = crud_key(table, 1)
        key_two = crud_key(table, 2)
        missing_id, cold_missing_id = allocate_test_row_ids(table, 2)
        missing = crud_key(table, missing_id)
        cold_missing = crud_key(table, cold_missing_id)
        composite = composite_key(composite_table, "tenant-a", 1)
        scoped = crud_key(scoped_table, 1)
        other_database = crud_key(table, 1, database=f"{PGDATABASE}_other")
        malformed = f"CRUD:{PGDATABASE}.public.unknown:{{\"id\":\"1\"}}"

        assert mget_one(client, key_one) == row_bytes(1, "initial")
        assert mget_one(client, scoped) == row_bytes(1, "scope-only")
        try:
            mget_one(client, other_database)
            raise AssertionError("RESP key crossed database scope")
        except RespError as error:
            assert "targets a different database" in str(error)

        before_invalid = json.loads(client.command("STAT"))
        try:
            client.command("MGET", key_one, malformed)
            raise AssertionError("partially valid MGET did not fail")
        except RespError as error:
            assert "unknown KVik table mapping" in str(error)
        after_invalid = json.loads(client.command("STAT"))
        for counter in (
            "client_mget_keys",
            "cache_hits",
            "cache_misses",
            "database_reads",
        ):
            assert after_invalid[counter] == before_invalid[counter], (
                counter,
                before_invalid,
                after_invalid,
            )

        before_duplicate_miss = json.loads(client.command("STAT"))
        assert client.command("MGET", cold_missing, cold_missing) == [None, None]
        after_duplicate_miss = json.loads(client.command("STAT"))
        assert (
            after_duplicate_miss["cache_misses"]
            - before_duplicate_miss["cache_misses"]
            == 1
        )
        assert (
            after_duplicate_miss["database_reads"]
            - before_duplicate_miss["database_reads"]
            == 1
        )
        assert after_duplicate_miss["loading_entries"] == 0
        assert client.command("MGET", cold_missing, cold_missing) == [None, None]
        after_duplicate_negative_hit = json.loads(client.command("STAT"))
        assert (
            after_duplicate_negative_hit["negative_hits"]
            - after_duplicate_miss["negative_hits"]
            == 1
        )
        assert (
            after_duplicate_negative_hit["database_reads"]
            == after_duplicate_miss["database_reads"]
        )

        mixed_arguments = (key_one, missing, key_one, composite, key_two)
        mixed_request = client.encode("MGET", *mixed_arguments)
        assert len(mixed_request) < MAX_PIPELINE_INPUT_BYTES
        before = json.loads(client.command("STAT"))
        client.socket.sendall(mixed_request)
        mixed = client.read_response()
        assert mixed == [
            row_bytes(1, "initial"),
            None,
            row_bytes(1, "initial"),
            composite_row_bytes("tenant-a", 1, "composite"),
            row_bytes(2, "x" * BACKPRESSURE_VALUE_BYTES),
        ], mixed
        after = json.loads(client.command("STAT"))
        assert after["client_mget_keys"] - before["client_mget_keys"] == 5
        assert after["cache_hits"] - before["cache_hits"] == 2
        assert after["cache_misses"] - before["cache_misses"] == 2
        assert after["database_reads"] - before["database_reads"] == 2

        repeated = client.command("MGET", *mixed_arguments)
        assert repeated == mixed
        repeated_stats = json.loads(client.command("STAT"))
        assert repeated_stats["client_mget_keys"] - after["client_mget_keys"] == 5
        assert repeated_stats["cache_hits"] - after["cache_hits"] == 4
        assert repeated_stats["negative_hits"] - after["negative_hits"] == 1
        assert repeated_stats["database_reads"] == after["database_reads"]

        maximum_arguments = [key_one] * 1_024
        maximum_request = client.encode("MGET", *maximum_arguments)
        assert len(maximum_request) < MAX_PIPELINE_INPUT_BYTES
        maximum = client.command("MGET", *maximum_arguments)
        assert maximum == [row_bytes(1, "initial")] * 1_024

        one_over_request = client.encode("MGET", *([key_one] * 1_025))
        assert len(one_over_request) < MAX_PIPELINE_INPUT_BYTES
        try:
            client.socket.sendall(one_over_request)
            client.read_response()
            raise AssertionError("1025-key MGET did not fail")
        except RespError as error:
            assert "at most 1024 keys" in str(error)
        assert client.command("PING") == "PONG"

        large_value = row_bytes(2, "x" * BACKPRESSURE_VALUE_BYTES)

        def encoded_array_size(count: int) -> int:
            element_size = len(f"${len(large_value)}\r\n") + len(large_value) + 2
            return len(f"*{count}\r\n") + count * element_size

        large_count = 1
        while encoded_array_size(large_count + 1) <= MAX_RESPONSE_BYTES:
            large_count += 1
        assert encoded_array_size(large_count) <= MAX_RESPONSE_BYTES
        assert encoded_array_size(large_count + 1) > MAX_RESPONSE_BYTES
        assert client.command("MGET", *([key_two] * large_count)) == [
            large_value
        ] * large_count
        try:
            client.command("MGET", *([key_two] * (large_count + 1)))
            raise AssertionError("oversized MGET response did not fail")
        except RespError as error:
            assert "response exceeds limit" in str(error)
        assert client.command("PING") == "PONG"

        # A cold oversized MGET must release its claim without publishing it.
        sql(
            f"UPDATE public.{sql_identifier(table)} "
            "SET value = repeat('x', 3900) WHERE id = 2"
        )
        before_cold_oversize = json.loads(client.command("STAT"))
        try:
            client.command("MGET", *([key_two] * (large_count + 1)))
            raise AssertionError("cold oversized MGET did not fail")
        except RespError as error:
            assert "response exceeds limit" in str(error)
        after_cold_oversize = json.loads(client.command("STAT"))
        assert after_cold_oversize["loading_entries"] == 0
        assert (
            after_cold_oversize["database_reads"]
            - before_cold_oversize["database_reads"]
            == 1
        )
        assert (
            after_cold_oversize["singleflight_leaders"]
            - before_cold_oversize["singleflight_leaders"]
            == 1
        )
        assert mget_one(client, key_two) == large_value
        after_cold_refill = json.loads(client.command("STAT"))
        assert after_cold_refill["loading_entries"] == 0
        assert (
            after_cold_refill["singleflight_leaders"]
            - after_cold_oversize["singleflight_leaders"]
            == 1
        )
    finally:
        client.close()


def read_cache_stats() -> dict[str, object]:
    return json.loads(sql("SELECT local_cache.stats()::text"))


def wait_for_stat_at_least(
    field: str,
    target: int,
    *,
    timeout: float = 6,
    poll_interval: float = 0.02,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = read_cache_stats()
        if int(last[field]) >= target:
            return last
        time.sleep(poll_interval)
    raise AssertionError(f"STAT {field} did not reach {target}: {last}")


def wait_for_blocked_relation_pid(
    table: str,
    *,
    application_name: str | None = None,
    psql_process: subprocess.Popen[str] | None = None,
    timeout: float = 6,
) -> int:
    relation = f"public.{sql_identifier(table)}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session_filter = (
            f"AND activity.application_name = {sql_literal(application_name)} "
            if application_name is not None
            else "AND activity.backend_type = 'pg_local_cache RESP worker' "
        )
        pid = sql(
            "SELECT lock.pid FROM pg_catalog.pg_locks AS lock "
            "JOIN pg_catalog.pg_stat_activity AS activity USING (pid) "
            f"WHERE lock.relation = '{relation}'::regclass "
            "AND lock.locktype = 'relation' "
            "AND lock.mode = 'AccessShareLock' AND NOT lock.granted "
            f"{session_filter}"
            "LIMIT 1"
        )
        if pid:
            return int(pid)
        time.sleep(0.01)
    raise AssertionError(
        warm_hit_diagnostics(
            {
                "blocked_relation": relation,
                "application_name": application_name,
                "psql_returncode": (
                    psql_process.poll() if psql_process is not None else None
                ),
                "psql_output": (
                    _psql_process_output_so_far(psql_process)
                    if psql_process is not None
                    else None
                ),
            }
        )
    )


def wait_for_blocked_worker_pid(table: str, *, timeout: float = 6) -> int:
    return wait_for_blocked_relation_pid(table, timeout=timeout)


_TEST_PAUSE_STARTED_AT: float | None = None
_TEST_PAUSE_POINTS: list[str] = []
_TEST_PAUSE_BEFORE_STATS: dict[str, object] | None = None
_TEST_PAUSE_RECORDS: list[dict[str, object]] = []


def set_test_pause(point: str | None, barrier_table: str | None = None) -> None:
    global _TEST_PAUSE_STARTED_AT, _TEST_PAUSE_POINTS, _TEST_PAUSE_BEFORE_STATS
    if point is None:
        sql("SELECT public.pglc_test_clear_pause()")
        if _TEST_PAUSE_STARTED_AT is not None:
            duration_seconds = round(
                time.monotonic() - _TEST_PAUSE_STARTED_AT, 3
            )
            try:
                after_stats: object = read_cache_stats()
            except Exception as error:
                after_stats = f"stats unavailable: {error!r}"
            _TEST_PAUSE_RECORDS.append(
                {
                    "points": list(_TEST_PAUSE_POINTS),
                    "duration_seconds": duration_seconds,
                    "before_stats": _TEST_PAUSE_BEFORE_STATS,
                    "after_stats": after_stats,
                }
            )
            _TEST_PAUSE_STARTED_AT = None
            _TEST_PAUSE_POINTS = []
            _TEST_PAUSE_BEFORE_STATS = None
        return
    assert barrier_table is not None, warm_hit_diagnostics(barrier_table)
    if _TEST_PAUSE_STARTED_AT is None:
        _TEST_PAUSE_BEFORE_STATS = read_cache_stats()
        _TEST_PAUSE_STARTED_AT = time.monotonic()
    _TEST_PAUSE_POINTS.append(point)
    sql(
        f"SELECT public.pglc_test_set_pause({sql_literal(point)}, "
        f"'public.{sql_identifier(barrier_table)}'::regclass)"
    )


def set_test_dirty_marker_limit(limit: int | None) -> None:
    guc_number("pg_local_cache.test_dirty_marker_limit")
    if limit is None:
        sql_commands(
            "ALTER SYSTEM RESET pg_local_cache.test_dirty_marker_limit",
            "SELECT pg_reload_conf()",
        )
    else:
        sql_commands(
            "ALTER SYSTEM SET pg_local_cache.test_dirty_marker_limit = "
            f"{limit}",
            "SELECT pg_reload_conf()",
        )
        assert guc_number("pg_local_cache.test_dirty_marker_limit") == limit


def set_test_max_dirty_keys(limit: int | None) -> None:
    guc_number("pg_local_cache.test_max_dirty_keys")
    if limit is None:
        sql_commands(
            "ALTER SYSTEM RESET pg_local_cache.test_max_dirty_keys",
            "SELECT pg_reload_conf()",
        )
    else:
        sql_commands(
            "ALTER SYSTEM SET pg_local_cache.test_max_dirty_keys = "
            f"{limit}",
            "SELECT pg_reload_conf()",
        )
        assert guc_number("pg_local_cache.test_max_dirty_keys") == limit


def reset_test_gucs_if_available() -> None:
    available = sql(
        "SELECT to_regprocedure('public.pglc_test_clear_pause()') IS NOT NULL"
    )
    if available == "t":
        set_test_pause(None)
        set_test_dirty_marker_limit(None)
        set_test_max_dirty_keys(None)


def test_collect_key_sql(table: str, namespace: str, key: str) -> str:
    relation = f"public.{sql_identifier(table)}"
    return (
        "SELECT public.pglc_test_collect_key("
        f"'{relation}'::regclass, {sql_literal(namespace)}, {sql_literal(key)})"
    )


def test_key_scanner_differential() -> None:
    domain = f"pglc_scan_domain_{os.getpid()}"
    cases: list[tuple[bytes, str, int]] = []

    def add(raw: bytes | str, type_name: str, typmod: int = -1) -> None:
        cases.append((raw.encode() if isinstance(raw, str) else raw, type_name, typmod))

    for type_name, minimum, maximum in (
        ("pg_catalog.int2", -32768, 32767),
        ("pg_catalog.int4", -2147483648, 2147483647),
        ("pg_catalog.int8", -9223372036854775808, 9223372036854775807),
    ):
        add(f'{{"id":{minimum}}}', type_name)
        add(f'{{"id":{maximum}}}', type_name)
        for value in range(-128, 129):
            add(f'{{ "id" : {value} }}', type_name)
        for spelling in (
            '1e2', '1.0', '-0', '01', '999999999999999999999999999999999999999999',
            'null', 'true', '[]',
        ):
            add(f'{{"id":{spelling}}}', type_name)
    add('{"id":32768}', "pg_catalog.int2")
    add('{"id":-32769}', "pg_catalog.int2")
    add('{"id":2147483648}', "pg_catalog.int4")
    add('{"id":-2147483649}', "pg_catalog.int4")
    add('{"id":9223372036854775808}', "pg_catalog.int8")
    add('{"id":-9223372036854775809}', "pg_catalog.int8")

    for raw in (
        '{"id":"plain"}',
        '{ "id" : "a:b;c" }',
        '{"id":""}',
        '{"id":"é"}',
        '{"id":"quote \\" mark"}',
        '{"id":"escaped \\\\ slash"}',
        '{"id":"\\u0061"}',
        '{"i\\u0064":"x"}',
        '{"id":"\\u0000"}',
        '{"id":"\\ud83d\\ude00"}',
        '{"id":"x","id":"y"}',
        '{"id":"x","extra":1}',
        '{"other":"x"}',
        '{"id":}',
        '{"id":"x"} trailing',
    ):
        add(raw, "pg_catalog.text")
    add('{"id":"bounded"}', "pg_catalog.varchar", 12)
    add('{"id":"trim  "}', "pg_catalog.bpchar", 8)
    add('{"id":7}', f"public.{domain}")
    add('{"id":1}', "pg_catalog.int4", 4)
    add(b'{"id":"\xc0\xaf"}', "pg_catalog.text")
    add(b'{"id":"\xed\xa0\x80"}', "pg_catalog.text")
    add(b'{"id":"x\x00y"}', "pg_catalog.text")

    values = ",\n".join(
        "(decode('%s', 'hex'), 'id'::text, '%s'::regtype, %d)"
        % (raw.hex(), type_name, typmod)
        for raw, type_name, typmod in cases
    )
    sql_commands(f"CREATE DOMAIN public.{domain} AS integer")
    try:
        matched = sql(
            "SELECT bool_and(public.pglc_test_key_scan_matches("
            "raw, column_name, key_type, typmod)) "
            f"FROM (VALUES {values}) AS cases(raw, column_name, key_type, typmod)"
        )
        assert matched == "t", "fast scanner differs from jsonb/type parser"
    finally:
        sql_commands(f"DROP DOMAIN IF EXISTS public.{domain}")


def install_test_hook_functions(table: str, namespace: str) -> bool:
    global _PGLC_TEST_HOOKS_AVAILABLE

    relation = f"public.{sql_identifier(table)}"
    definitions = (
        "CREATE OR REPLACE FUNCTION public.pglc_test_partition_lock_violations() "
        "RETURNS bigint AS '$libdir/pg_local_cache', "
        "'pg_local_cache_test_partition_lock_violations' LANGUAGE C",
        "CREATE OR REPLACE FUNCTION public.pglc_test_set_pause(text, regclass) "
        "RETURNS void AS '$libdir/pg_local_cache', "
        "'pg_local_cache_test_set_pause' LANGUAGE C STRICT",
        "CREATE OR REPLACE FUNCTION public.pglc_test_clear_pause() "
        "RETURNS void AS '$libdir/pg_local_cache', "
        "'pg_local_cache_test_clear_pause' LANGUAGE C",
        "CREATE OR REPLACE FUNCTION public.pglc_test_collect_key(regclass, text, text) "
        "RETURNS void AS '$libdir/pg_local_cache', 'pg_local_cache_test_collect_key' "
        "LANGUAGE C STRICT PARALLEL SAFE",
        "CREATE OR REPLACE FUNCTION local_cache._test_collect_key(oid, text, text) "
        "RETURNS void AS '$libdir/pg_local_cache', 'pg_local_cache_test_collect_key' "
        "LANGUAGE C STRICT PARALLEL SAFE",
        "CREATE OR REPLACE FUNCTION public.pglc_test_partition(oid, text, text) "
        "RETURNS integer AS '$libdir/pg_local_cache', "
        "'pg_local_cache_test_partition' LANGUAGE C STRICT",
        "CREATE OR REPLACE FUNCTION public.pglc_test_hash_bucket(oid, text, text) "
        "RETURNS integer AS '$libdir/pg_local_cache', "
        "'pg_local_cache_test_hash_bucket' LANGUAGE C STRICT",
        "CREATE OR REPLACE FUNCTION public.pglc_test_cache_bucket_count(oid, text, text) "
        "RETURNS integer AS '$libdir/pg_local_cache', "
        "'pg_local_cache_test_cache_bucket_count' LANGUAGE C STRICT",
        "CREATE OR REPLACE FUNCTION public.pglc_test_relation_incarnation(regclass, text) "
        "RETURNS bigint AS '$libdir/pg_local_cache', "
        "'pg_local_cache_test_relation_incarnation' LANGUAGE C STRICT",
        "CREATE OR REPLACE FUNCTION public.pglc_test_relation_identity_pins(regclass, text) "
        "RETURNS bigint AS '$libdir/pg_local_cache', "
        "'pg_local_cache_test_relation_identity_pins' LANGUAGE C STRICT",
        "CREATE OR REPLACE FUNCTION public.pglc_test_recreate_relation_state(regclass, text) "
        "RETURNS bigint AS '$libdir/pg_local_cache', "
        "'pg_local_cache_test_recreate_relation_state' LANGUAGE C STRICT",
        "CREATE OR REPLACE FUNCTION public.pglc_test_collect_global() "
        "RETURNS void AS '$libdir/pg_local_cache', "
        "'pg_local_cache_test_collect_global' LANGUAGE C",
        "CREATE OR REPLACE FUNCTION public.pglc_test_abort_after_reservation() "
        "RETURNS void AS '$libdir/pg_local_cache', "
        "'pg_local_cache_test_abort_after_reservation' LANGUAGE C",
        "CREATE OR REPLACE FUNCTION public.pglc_test_key_scan_matches(bytea, text, regtype, integer) "
        "RETURNS boolean AS '$libdir/pg_local_cache', "
        "'pg_local_cache_test_key_scan_matches' LANGUAGE C STRICT",
        "CREATE OR REPLACE FUNCTION local_cache._test_duplicate_refresh_finish() "
        "RETURNS void AS '$libdir/pg_local_cache', "
        "'pg_local_cache_test_duplicate_refresh_finish' LANGUAGE C",
        "CREATE OR REPLACE FUNCTION local_cache._test_refresh_capture_fail() "
        "RETURNS void AS '$libdir/pg_local_cache', "
        "'pg_local_cache_test_refresh_capture_fail' LANGUAGE C",
        "CREATE OR REPLACE FUNCTION local_cache._test_remove_reserved_entry(oid, text, text) "
        "RETURNS void AS '$libdir/pg_local_cache', "
        "'pg_local_cache_test_remove_reserved_entry' LANGUAGE C",
        "CREATE OR REPLACE FUNCTION local_cache._test_corrupt_refresh_crc(oid, text, text) "
        "RETURNS void AS '$libdir/pg_local_cache', "
        "'pg_local_cache_test_corrupt_refresh_crc' LANGUAGE C",
        "CREATE OR REPLACE FUNCTION local_cache._test_refresh_xid_boundary(oid, text, text) "
        "RETURNS void AS '$libdir/pg_local_cache', "
        "'pg_local_cache_test_refresh_xid_boundary' LANGUAGE C",
    )
    try:
        sql_commands(*definitions)
        sql(
            "SELECT public.pglc_test_relation_incarnation("
            f"'{relation}'::regclass, {sql_literal(namespace)})"
        )
    except subprocess.CalledProcessError as error:
        try:
            drop_test_hook_functions()
        except subprocess.CalledProcessError:
            pass
        if os.environ.get("PGLC_TEST_HOOKS_REQUIRED") == "1":
            raise AssertionError(
                "CI requires PGLC_TEST_HOOKS, but one or more test C functions are absent"
            ) from error
        _PGLC_TEST_HOOKS_AVAILABLE = False
        return False
    _PGLC_TEST_HOOKS_AVAILABLE = True
    return True


def drop_test_hook_functions() -> None:
    sql_commands(
        "DROP FUNCTION IF EXISTS public.pglc_test_partition_lock_violations()",
        "DROP FUNCTION IF EXISTS public.pglc_test_set_pause(text, regclass)",
        "DROP FUNCTION IF EXISTS public.pglc_test_clear_pause()",
        "DROP FUNCTION IF EXISTS public.pglc_test_collect_key(regclass, text, text)",
        "DROP FUNCTION IF EXISTS local_cache._test_collect_key(oid, text, text)",
        "DROP FUNCTION IF EXISTS public.pglc_test_partition(oid, text, text)",
        "DROP FUNCTION IF EXISTS public.pglc_test_hash_bucket(oid, text, text)",
        "DROP FUNCTION IF EXISTS public.pglc_test_cache_bucket_count(oid, text, text)",
        "DROP FUNCTION IF EXISTS public.pglc_test_relation_incarnation(regclass, text)",
        "DROP FUNCTION IF EXISTS public.pglc_test_relation_identity_pins(regclass, text)",
        "DROP FUNCTION IF EXISTS public.pglc_test_recreate_relation_state(regclass, text)",
        "DROP FUNCTION IF EXISTS public.pglc_test_collect_global()",
        "DROP FUNCTION IF EXISTS public.pglc_test_abort_after_reservation()",
        "DROP FUNCTION IF EXISTS public.pglc_test_key_scan_matches(bytea, text, regtype, integer)",
        "DROP FUNCTION IF EXISTS local_cache._test_duplicate_refresh_finish()",
        "DROP FUNCTION IF EXISTS local_cache._test_refresh_capture_fail()",
        "DROP FUNCTION IF EXISTS local_cache._test_remove_reserved_entry(oid, text, text)",
        "DROP FUNCTION IF EXISTS local_cache._test_corrupt_refresh_crc(oid, text, text)",
        "DROP FUNCTION IF EXISTS local_cache._test_refresh_xid_boundary(oid, text, text)",
    )


def start_updating_key_writer(
    table: str,
    row_id: int,
    *,
    application_name: str,
) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    environment["PGAPPNAME"] = application_name
    process = subprocess.Popen(
        psql_base_args(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
    )
    _PSQL_PROCESS_OUTPUT[process.pid] = bytearray()
    write_psql_input(
        process,
        "BEGIN;\n"
        f"UPDATE public.{sql_identifier(table)} SET value = value "
        f"WHERE id = {row_id};\n"
        "COMMIT;\n",
    )
    return process


def start_publishing_key_writer(
    table: str,
    namespace: str,
    key: str,
    *,
    application_name: str,
) -> subprocess.Popen[str]:
    return start_publishing_keys_writer(
        table, namespace, [key], application_name=application_name
    )


def start_publishing_keys_writer(
    table: str,
    namespace: str,
    keys: list[str],
    *,
    application_name: str,
) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    environment["PGAPPNAME"] = application_name
    process = subprocess.Popen(
        psql_base_args(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
    )
    _PSQL_PROCESS_OUTPUT[process.pid] = bytearray()
    write_psql_input(
        process,
        "BEGIN;\n"
        + "\n".join(
            f"{test_collect_key_sql(table, namespace, key)};" for key in keys
        )
        + "\nCOMMIT;\n"
    )
    return process


def finish_publishing_key_writer(process: subprocess.Popen[str]) -> str:
    close_psql_input(process)
    output = _finish_tracked_psql(process)
    assert process.returncode == 0, warm_hit_diagnostics(
        {"returncode": process.returncode, "output": output}
    )
    return output


def start_relation_or_global_fence_writer(
    fence_kind: str,
    namespace: str,
    *,
    application_name: str,
) -> subprocess.Popen[str]:
    if fence_kind == "relation":
        publish = f"SELECT local_cache.invalidate({sql_literal(namespace)});"
    elif fence_kind == "global":
        publish = "SELECT public.pglc_test_collect_global();"
    else:
        raise AssertionError(warm_hit_diagnostics(fence_kind))
    environment = os.environ.copy()
    environment["PGAPPNAME"] = application_name
    process = subprocess.Popen(
        psql_base_args(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
    )
    _PSQL_PROCESS_OUTPUT[process.pid] = bytearray()
    write_psql_input(process, f"BEGIN; {publish} COMMIT;\n")
    close_psql_input(process)
    return process


def _psql_process_output_so_far(process: subprocess.Popen[str]) -> str:
    captured = _PSQL_PROCESS_OUTPUT.setdefault(process.pid, bytearray())
    if process.stdout is not None:
        descriptor = process.stdout.fileno()
        while select.select([descriptor], [], [], 0)[0]:
            try:
                chunk = os.read(descriptor, 4096)
            except BlockingIOError:
                break
            if not chunk:
                break
            captured.extend(chunk)
    return captured.decode(errors="replace")


def wait_for_psql_output(
    process: subprocess.Popen[str], marker: str, *, timeout: float = 10
) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        output = _psql_process_output_so_far(process)
        if marker in output:
            return output
        if process.poll() is not None:
            break
        time.sleep(0.01)
    raise AssertionError(
        warm_hit_diagnostics(
            {
                "psql_marker": marker,
                "psql_returncode": process.poll(),
                "psql_output": _psql_process_output_so_far(process),
            }
        )
    )


def _finish_tracked_psql(process: subprocess.Popen[str]) -> str:
    _psql_process_output_so_far(process)
    captured = _PSQL_PROCESS_OUTPUT.pop(process.pid, bytearray())
    try:
        remainder = process.communicate(timeout=10)[0]
    except subprocess.TimeoutExpired as error:
        output = error.output or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        raise RuntimeError(
            f"psql process timed out; stdout/stderr:\n"
            f"{captured.decode(errors='replace')}{output}"
        ) from error
    return captured.decode(errors="replace") + remainder


def finish_fence_writer(process: subprocess.Popen[str]) -> str:
    output = _finish_tracked_psql(process)
    assert process.returncode == 0, warm_hit_diagnostics(
        {"returncode": process.returncode, "output": output}
    )
    return output


def test_partition_routing_and_opposite_order_writers(
    table: str,
    namespace: str,
    barrier_table: str,
    second_barrier_table: str,
) -> None:
    stats = read_cache_stats()
    partition_count = int(stats["lock_partitions"])
    database_oid = int(
        sql("SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database()")
    )
    probe_key = f"route-probe-{os.getpid()}"
    probe_result = sql(
        "SELECT public.pglc_test_partition("
        f"{database_oid}, {sql_literal(namespace)}, {sql_literal(probe_key)}), "
        "public.pglc_test_partition("
        f"{database_oid}, {sql_literal(namespace)}, {sql_literal(probe_key)})"
    )
    first_route, second_route = (int(value) for value in probe_result.split("|"))
    assert first_route == second_route
    assert 0 <= first_route < partition_count

    candidate_limit = partition_count * 128
    routed_keys = sql(
        "SELECT string_agg(key || ':' || partition, ',' ORDER BY partition) "
        "FROM (SELECT DISTINCT ON (partition) key, partition FROM ("
        "SELECT 'route-' || candidate::text AS key, "
        "public.pglc_test_partition("
        f"{database_oid}, {sql_literal(namespace)}, "
        "'route-' || candidate::text) AS partition "
        f"FROM generate_series(1, {candidate_limit}) AS candidates(candidate)"
        ") AS routes ORDER BY partition, key LIMIT 16) AS selected"
    )
    key_routes = [
        (item.rpartition(":")[0], int(item.rpartition(":")[2]))
        for item in routed_keys.split(",")
        if item
    ]
    assert len(key_routes) == min(16, partition_count), key_routes
    keys = [key for key, _partition in key_routes]
    assert len({partition for _key, partition in key_routes}) == len(key_routes)

    bucket_probe_limit = partition_count * 512
    bucket_diversity_margin = int(
        sql(
            "SELECT min(bucket_diversity - least(24, bucket_count)) FROM ("
            "SELECT partition_id, count(DISTINCT bucket_id) AS bucket_diversity, "
            "min(bucket_count) AS bucket_count FROM ("
            "SELECT public.pglc_test_partition("
            f"{database_oid}, {sql_literal(namespace)}, "
            "'bucket-' || candidate::text) AS partition_id, "
            "public.pglc_test_hash_bucket("
            f"{database_oid}, {sql_literal(namespace)}, "
            "'bucket-' || candidate::text) AS bucket_id, "
            "public.pglc_test_cache_bucket_count("
            f"{database_oid}, {sql_literal(namespace)}, "
            "'bucket-' || candidate::text) AS bucket_count "
            f"FROM generate_series(1, {bucket_probe_limit}) AS probes(candidate)"
            ") AS routed_buckets GROUP BY partition_id"
            ") AS bucket_counts"
        )
    )
    assert bucket_diversity_margin >= 0, bucket_diversity_margin

    first: subprocess.Popen[str] | None = None
    second: subprocess.Popen[str] | None = None
    first_locker: subprocess.Popen[str] | None = None
    second_locker: subprocess.Popen[str] | None = None
    try:
        first_locker = start_table_locker(
            barrier_table,
            application_name=f"pglc_partition_barrier_first_{os.getpid()}",
        )
        second_locker = start_table_locker(
            second_barrier_table,
            application_name=f"pglc_partition_barrier_second_{os.getpid()}",
        )
        set_test_pause("before_partition_acquire", barrier_table)
        first_name = f"pglc_partition_first_{os.getpid()}"
        first = start_publishing_keys_writer(
            table, namespace, keys, application_name=first_name
        )
        wait_for_blocked_relation_pid(
            barrier_table,
            application_name=first_name,
            psql_process=first,
            timeout=10,
        )

        set_test_pause("before_partition_acquire", second_barrier_table)
        second_name = f"pglc_partition_second_{os.getpid()}"
        second = start_publishing_keys_writer(
            table, namespace, list(reversed(keys)), application_name=second_name
        )
        wait_for_blocked_relation_pid(
            second_barrier_table,
            application_name=second_name,
            psql_process=second,
            timeout=10,
        )

        # Both writers reached first partition acquisition boundary. Release
        # both relation barriers together so publication starts overlap.
        set_test_pause(None)
        unlock_errors: list[BaseException] = []

        def unlock(process: subprocess.Popen[str]) -> None:
            try:
                finish_writer(process, commit=True)
            except BaseException as error:
                unlock_errors.append(error)

        unlockers = [
            threading.Thread(target=unlock, args=(first_locker,)),
            threading.Thread(target=unlock, args=(second_locker,)),
        ]
        for unlocker in unlockers:
            unlocker.start()
        for unlocker in unlockers:
            unlocker.join(timeout=10)
            assert not unlocker.is_alive(), "partition barrier release did not finish"
        if unlock_errors:
            raise AssertionError("partition barrier release failed") from unlock_errors[0]
        first_locker = None
        second_locker = None
        finish_publishing_key_writer(first)
        first = None
        finish_publishing_key_writer(second)
        second = None
        assert int(sql("SELECT public.pglc_test_partition_lock_violations()")) == 0
    finally:
        set_test_pause(None)
        for process in (first_locker, second_locker, first, second):
            terminate_writer(process)


def run_mget_thread(
    client: RespConnection,
    keys: list[str],
    results: dict[str, object],
    name: str,
) -> None:
    try:
        results[name] = client.command("MGET", *keys)
    except BaseException as error:
        results[name] = error


def finish_mget_threads(
    threads: list[threading.Thread],
    results: dict[str, object],
    *,
    timeout: float,
) -> None:
    for thread in threads:
        thread.join(timeout=timeout)
        assert not thread.is_alive(), f"MGET thread did not finish: {thread.name}"


def distinct_worker_connections(
    count: int, *, socket_timeout: float = 5, max_attempts: int = 64
) -> list[RespConnection]:
    clients: list[RespConnection] = []
    worker_ids: set[object] = set()
    try:
        for _ in range(max_attempts):
            candidate = RespConnection(socket_timeout=socket_timeout)
            try:
                worker_id = candidate.command("CLIENT", "ID")
            except BaseException:
                candidate.close()
                raise
            if worker_id in worker_ids:
                candidate.close()
                continue
            clients.append(candidate)
            worker_ids.add(worker_id)
            if len(clients) == count:
                return clients
        raise AssertionError(
            f"could not connect to {count} distinct RESP workers after "
            f"{max_attempts} attempts (found {len(clients)})"
        )
    except BaseException:
        for client in clients:
            client.close()
        raise


def test_mget_does_not_hold_claim_while_waiting(
    table: str, scoped_table: str
) -> None:
    clients = distinct_worker_connections(3, socket_timeout=45)
    stat_clients = [same_worker_peer(client) for client in clients[:2]]
    locker: subprocess.Popen[str] | None = None
    threads: list[threading.Thread] = []
    results: dict[str, object] = {}
    row_a_id = allocate_test_row_ids(table)[0]
    key_a = crud_key(table, row_a_id)
    key_b = crud_key(scoped_table, 1)
    try:
        locker = start_table_locker(
            scoped_table, application_name=f"pglc_mget_order_lock_{os.getpid()}"
        )
        sql(
            f"INSERT INTO public.{sql_identifier(table)} (id, value) "
            f"VALUES ({row_a_id}, 'deferred-a')"
        )
        before = read_cache_stats()
        before_owner = json.loads(stat_clients[0].command("STAT"))
        before_deferred = json.loads(stat_clients[1].command("STAT"))
        owner = threading.Thread(
            target=run_mget_thread,
            args=(clients[0], [key_b], results, "owner"),
            name="mget-blocked-owner",
        )
        threads.append(owner)
        owner.start()
        wait_for_worker_stat(
            stat_clients[0],
            "deferred_misses_current",
            int(before_owner["deferred_misses_current"]) + 1,
            timeout=10,
        )

        deferred = threading.Thread(
            target=run_mget_thread,
            args=(clients[1], [key_a, key_b], results, "deferred"),
            name="mget-deferred-owner",
        )
        threads.append(deferred)
        deferred.start()
        wait_for_worker_stat(
            stat_clients[1],
            "deferred_misses_current",
            int(before_deferred["deferred_misses_current"]) + 1,
            timeout=10,
        )

        assert clients[2].command("MGET", key_a) == [row_bytes(row_a_id, "deferred-a")]

        finish_writer(locker, commit=True)
        locker = None
        finish_mget_threads(threads, results, timeout=10)

        assert results["owner"] == [row_bytes(1, "scope-only")], results
        assert results["deferred"] == [
            row_bytes(row_a_id, "deferred-a"),
            row_bytes(1, "scope-only"),
        ], results
        after = read_cache_stats()
        assert after["loading_entries"] == 0
        assert after["database_reads"] - before["database_reads"] >= 2
        assert json.loads(stat_clients[0].command("STAT"))[
            "deferred_misses_current"
        ] == before_owner["deferred_misses_current"]
        assert json.loads(stat_clients[1].command("STAT"))[
            "deferred_misses_current"
        ] == before_deferred["deferred_misses_current"]
    finally:
        try:
            if locker is not None:
                finish_writer(locker, commit=True)
        finally:
            for thread in threads:
                if thread.is_alive():
                    thread.join(timeout=10)
            for client in clients:
                client.close()
            for client in stat_clients:
                client.close()


def test_mget_cancelled_owner_releases_claim(table: str) -> None:
    key = crud_key(table, allocate_test_row_ids(table)[0])
    client = RespConnection(socket_timeout=10)
    peer = same_worker_peer(client)
    locker: subprocess.Popen[str] | None = None
    try:
        before = json.loads(peer.command("STAT"))
        locker = start_table_locker(
            table, application_name=f"pglc_mget_cancel_lock_{os.getpid()}"
        )
        client.socket.sendall(client.encode("MGET", key))
        deferred = wait_for_worker_stat(
            peer,
            "deferred_misses_current",
            int(before["deferred_misses_current"]) + 1,
        )
        assert deferred["loading_entries"] == 0
        client.socket.shutdown(socket.SHUT_WR)
        client.close()
        cleared = wait_for_worker_stat(
            peer,
            "deferred_misses_current",
            int(before["deferred_misses_current"]),
        )
        assert cleared["loading_entries"] == 0
        finish_writer(locker, commit=True)
        locker = None
        assert peer.command("MGET", key) == [None]
    finally:
        if locker is not None:
            finish_writer(locker, commit=True)
        peer.close()
        client.close()


def test_mget_statement_timeout_cleanup(table: str) -> None:
    key = crud_key(table, allocate_test_row_ids(table)[0])
    client = RespConnection(socket_timeout=10)
    peer = same_worker_peer(client)
    locker: subprocess.Popen[str] | None = None
    try:
        before = json.loads(peer.command("STAT"))
        locker = start_table_locker(
            table, application_name=f"pglc_mget_error_lock_{os.getpid()}"
        )
        client.socket.sendall(
            client.encode("ECHO", "before-deadline")
            + client.encode("MGET", key)
            + client.encode("ECHO", "after-deadline")
        )
        wait_for_worker_stat(
            peer,
            "deferred_misses_current",
            int(before["deferred_misses_current"]) + 1,
        )
        assert client.read_response() == b"before-deadline"
        try:
            client.read_response()
            raise AssertionError("relation-locked MGET did not expire")
        except RespError as error:
            assert "ERR MGET deadline exceeded" in str(error)
        assert client.read_response() == b"after-deadline"
        after_error = json.loads(peer.command("STAT"))
        assert after_error["deferred_misses_current"] == before[
            "deferred_misses_current"
        ]
        assert after_error["deferred_timeouts_total"] == (
            before["deferred_timeouts_total"] + 1
        )
        assert after_error["loading_entries"] == 0
        finish_writer(locker, commit=True)
        locker = None
        assert mget_one(client, key) is None
    finally:
        if locker is not None:
            finish_writer(locker, commit=True)
        peer.close()
        client.close()


def test_mget_mapping_reload_cleanup(table: str) -> None:
    key = crud_key(table, allocate_test_row_ids(table)[0])
    client = RespConnection()
    peer = same_worker_peer(client)
    locker = start_table_locker(
        table, application_name=f"pglc_mget_reload_lock_{os.getpid()}"
    )
    before = json.loads(peer.command("STAT"))
    results: dict[str, object] = {}
    thread = threading.Thread(
        target=run_mget_thread,
        args=(client, [key], results, "reloaded"),
        name="mget-mapping-reload",
    )
    try:
        thread.start()
        wait_for_worker_stat(
            peer,
            "deferred_misses_current",
            int(before["deferred_misses_current"]) + 1,
        )
        sql("SELECT local_cache._reload()")
        finish_writer(locker, commit=True)
        locker = None
        thread.join(timeout=10)
        assert not thread.is_alive(), "mapping reload did not release deferred MGET"
        assert results["reloaded"] == [None], results["reloaded"]
        after_error = json.loads(peer.command("STAT"))
        assert after_error["loading_entries"] == 0
        assert mget_one(client, key) is None
        after_refill = json.loads(peer.command("STAT"))
        assert after_refill["loading_entries"] == 0
    finally:
        if locker is not None:
            finish_writer(locker, commit=True)
        if thread.is_alive():
            thread.join(timeout=6)
        peer.close()
        client.close()


def test_pipeline_budget_is_a_fairness_yield() -> None:
    limit = guc_number("pg_local_cache.max_pipeline_commands")
    client = RespConnection()
    try:
        # max_pipeline_commands is documented as an event-loop work budget,
        # not a wire protocol limit.  Buffered input must resume on a later
        # turn even when no additional POLLIN edge arrives.
        count = limit + 17
        client.socket.sendall(
            b"".join(
                client.encode("ECHO", f"fairness-{index}")
                for index in range(count)
            )
        )
        for index in range(count):
            assert client.read_response() == f"fairness-{index}".encode()
        assert client.command("ECHO", "fairness-after") == b"fairness-after"
    finally:
        client.close()


def test_interleaved_client_pipelines_preserve_order() -> None:
    clients = [RespConnection()]
    try:
        clients.extend(same_worker_peer(clients[0]) for _ in range(7))
        count = guc_number("pg_local_cache.max_pipeline_commands") + 17

        for client_index, client in enumerate(clients):
            client.socket.sendall(
                b"".join(
                    client.encode("ECHO", f"client-{client_index}-{index}")
                    for index in range(count)
                )
            )

        for index in range(count):
            for client_index, client in enumerate(clients):
                assert client.read_response() == (
                    f"client-{client_index}-{index}".encode()
                )
        for client_index, client in enumerate(clients):
            assert client.command("ECHO", f"after-{client_index}") == (
                f"after-{client_index}".encode()
            )
    finally:
        for client in clients:
            client.close()


def test_half_close_drains_final_pipeline(table: str) -> None:
    client = RespConnection()
    try:
        client.socket.sendall(
            client.encode("ECHO", "before-half-close")
            + client.encode("MGET", crud_key(table, 1))
            + client.encode("ECHO", "after-half-close")
        )
        client.socket.shutdown(socket.SHUT_WR)
        assert client.read_response() == b"before-half-close"
        assert client.read_response() == [row_bytes(1, "initial")]
        assert client.read_response() == b"after-half-close"
        try:
            client.read_response()
            raise AssertionError("half-closed connection remained open after flush")
        except (EOFError, ConnectionResetError):
            pass
    finally:
        client.close()


def same_worker_peer(reference: RespConnection) -> RespConnection:
    worker_id = reference.command("CLIENT", "ID")
    for _ in range(256):
        candidate = RespConnection()
        if candidate.command("CLIENT", "ID") == worker_id:
            return candidate
        candidate.close()
    raise AssertionError(f"could not connect twice to RESP worker {worker_id}")


def wait_for_worker_stat(
    client: RespConnection, field: str, target: int, *, timeout: float = 8
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = json.loads(client.command("STAT"))
        if int(last[field]) >= target:
            return last
        time.sleep(0.01)
    raise AssertionError(f"worker STAT {field} did not reach {target}: {last}")


def test_relation_locked_mget_defers(table: str, scoped_table: str) -> None:
    client = RespConnection(socket_timeout=15)
    peer = same_worker_peer(client)
    row_id = allocate_test_row_ids(table)[0]
    key_a = crud_key(table, row_id)
    key_b = crud_key(scoped_table, 1)
    key_b_missing = crud_key(scoped_table, allocate_test_row_ids(scoped_table)[0])
    expected_a = row_bytes(row_id, "deferred-result")
    expected_b = row_bytes(1, "scope-only")
    locker: subprocess.Popen[str] | None = None
    try:
        sql(
            f"INSERT INTO public.{sql_identifier(table)} (id, value) "
            f"VALUES ({row_id}, 'deferred-result')"
        )
        assert peer.command("MGET", crud_key(table, 1)) == [
            row_bytes(1, "initial")
        ]
        assert peer.command("MGET", key_b) == [expected_b]
        before = json.loads(peer.command("STAT"))
        locker = start_table_locker(
            table, application_name=f"pglc_deferred_order_{os.getpid()}"
        )
        client.socket.sendall(
            client.encode("ECHO", "before")
            + client.encode("MGET", key_a)
            + client.encode("ECHO", "after")
        )
        assert client.read_response() == b"before"
        wait_for_worker_stat(
            peer,
            "deferred_misses_current",
            int(before["deferred_misses_current"]) + 1,
        )

        lock_timeout_ms = guc_number("pg_local_cache.lock_timeout_ms")
        latency_limit = min(0.12, lock_timeout_ms / 2000)
        for key, expected in (
            (crud_key(table, 1), [row_bytes(1, "initial")]),
            (key_b, [expected_b]),
            (key_b_missing, [None]),
        ):
            started = time.monotonic()
            assert peer.command("MGET", key) == expected
            assert time.monotonic() - started < latency_limit

        assert client.position == len(client.buffer)
        assert select.select([client.socket], [], [], 0.05)[0] == [], (
            "later pipelined response overtook deferred MGET"
        )
        finish_writer(locker, commit=True)
        locker = None
        assert client.read_response() == [expected_a]
        assert client.read_response() == b"after"
        after = json.loads(peer.command("STAT"))
        assert after["deferred_misses_current"] == before[
            "deferred_misses_current"
        ]
        assert after["deferred_misses_total"] >= before["deferred_misses_total"] + 1
    finally:
        if locker is not None:
            finish_writer(locker, commit=True)
        peer.close()
        client.close()


def test_deferred_pipeline_backpressure_and_half_close(table: str) -> None:
    client = RespConnection(socket_timeout=30, receive_buffer=4096)
    peer = same_worker_peer(client)
    client.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
    row_id = allocate_test_row_ids(table)[0]
    key = crud_key(table, row_id)
    value = "x" * 2048
    encoded_echo = client.encode("ECHO", value)
    echo_count = max(
        2048, (MAX_PIPELINE_INPUT_BYTES // len(encoded_echo) + 1) * 128
    )
    payload = (
        client.encode("MGET", key)
        + encoded_echo * echo_count
    )
    assert len(payload) > MAX_PIPELINE_INPUT_BYTES * 8
    before = json.loads(peer.command("STAT"))
    locker: subprocess.Popen[str] | None = start_table_locker(
        table, application_name=f"pglc_deferred_full_input_{os.getpid()}"
    )
    sender_error: list[BaseException] = []
    reader_result: list[list[object]] = []
    reader_error: list[BaseException] = []
    sender: threading.Thread | None = None
    reader: threading.Thread | None = None
    try:
        def send_pipeline() -> None:
            try:
                client.socket.sendall(payload)
                client.socket.shutdown(socket.SHUT_WR)
            except BaseException as error:
                sender_error.append(error)

        def read_pipeline() -> None:
            try:
                reader_result.append(
                    [client.read_response() for _ in range(echo_count + 1)]
                )
            except BaseException as error:
                reader_error.append(error)

        sender = threading.Thread(
            target=send_pipeline, name="deferred-pipeline-send", daemon=True
        )
        sender.start()
        wait_for_worker_stat(
            peer,
            "deferred_misses_current",
            int(before["deferred_misses_current"]) + 1,
        )
        # Let the worker fill its retained request buffer while the first MGET
        # is still blocked by the relation lock.
        time.sleep(0.1)
        reader = threading.Thread(
            target=read_pipeline, name="deferred-pipeline-read", daemon=True
        )
        reader.start()
        finish_writer(locker, commit=True)
        locker = None
        sender.join(timeout=25)
        reader.join(timeout=25)
        assert not sender.is_alive(), "pipeline sender remained blocked"
        assert not reader.is_alive(), "pipeline reader remained blocked"
        assert not sender_error, sender_error
        assert not reader_error, reader_error
        assert len(reader_result) == 1
        assert reader_result[0][0] == [None]
        assert reader_result[0][1:] == [value.encode()] * echo_count
        try:
            client.read_response()
            raise AssertionError("half-closed pipeline remained open after drain")
        except (EOFError, ConnectionResetError):
            pass
    finally:
        if locker is not None:
            finish_writer(locker, commit=True)
        client.close()
        peer.close()
        if sender is not None:
            sender.join(timeout=2)
        if reader is not None:
            reader.join(timeout=2)


def test_deferred_queue_full_returns_busy(table: str) -> None:
    reference = RespConnection(socket_timeout=15)
    clients = [reference]
    peer: RespConnection | None = None
    overflow: RespConnection | None = None
    locker: subprocess.Popen[str] | None = None
    try:
        clients.extend(same_worker_peer(reference) for _ in range(7))
        peer = same_worker_peer(reference)
        overflow = same_worker_peer(reference)
        before = json.loads(peer.command("STAT"))
        locker = start_table_locker(
            table, application_name=f"pglc_deferred_full_{os.getpid()}"
        )
        for index, deferred_client in enumerate(clients):
            deferred_client.socket.sendall(
                deferred_client.encode(
                    "MGET", crud_key(table, allocate_test_row_ids(table)[0])
                )
            )
            wait_for_worker_stat(
                peer,
                "deferred_misses_current",
                int(before["deferred_misses_current"]) + index + 1,
            )

        overflow.socket.sendall(
            overflow.encode(
                "MGET", crud_key(table, allocate_test_row_ids(table)[0])
            )
            + overflow.encode("ECHO", "after-busy")
        )
        try:
            overflow.read_response()
            raise AssertionError("full deferred queue did not return busy")
        except RespError as error:
            assert "ERR busy: relation locked, retry" in str(error)
        assert overflow.read_response() == b"after-busy"
        busy_attempts = 8
        for index in range(busy_attempts - 1):
            try:
                overflow.command(
                    "MGET",
                    crud_key(table, allocate_test_row_ids(table)[0]),
                )
                raise AssertionError("full deferred queue did not return busy")
            except RespError as error:
                assert "ERR busy: relation locked, retry" in str(error)
            assert overflow.command("ECHO", f"after-busy-{index}") == (
                f"after-busy-{index}".encode()
            )
        after_rejection = json.loads(peer.command("STAT"))
        assert after_rejection["deferred_rejections_total"] == (
            before["deferred_rejections_total"] + busy_attempts
        )

        finish_writer(locker, commit=True)
        locker = None
        for deferred_client in clients:
            assert deferred_client.read_response() == [None]
    finally:
        if locker is not None:
            finish_writer(locker, commit=True)
        if peer is not None:
            peer.close()
        if overflow is not None:
            overflow.close()
        for deferred_client in clients:
            deferred_client.close()


def test_disconnect_clears_deferred_mget(table: str) -> None:
    client = RespConnection(socket_timeout=10)
    peer = same_worker_peer(client)
    key = crud_key(table, allocate_test_row_ids(table)[0])
    locker: subprocess.Popen[str] | None = None
    try:
        before = json.loads(peer.command("STAT"))
        locker = start_table_locker(
            table, application_name=f"pglc_deferred_disconnect_{os.getpid()}"
        )
        client.socket.sendall(client.encode("MGET", key))
        deferred = wait_for_worker_stat(
            peer,
            "deferred_misses_current",
            int(before["deferred_misses_current"]) + 1,
        )
        assert deferred["loading_entries"] == 0
        client.socket.shutdown(socket.SHUT_WR)
        client.close()
        cleared = wait_for_worker_stat(
            peer,
            "deferred_misses_current",
            int(before["deferred_misses_current"]),
        )
        assert cleared["loading_entries"] == 0
        finish_writer(locker, commit=True)
        locker = None
        assert peer.command("MGET", key) == [None]
    finally:
        if locker is not None:
            finish_writer(locker, commit=True)
        peer.close()
        client.close()


def test_ddl_lock_does_not_stall_other_relations(
    table: str, scoped_table: str
) -> None:
    client = RespConnection(socket_timeout=15)
    peer = same_worker_peer(client)
    locker: subprocess.Popen[str] | None = None
    try:
        key_b = crud_key(scoped_table, 1)
        assert client.command("MGET", key_b) == [row_bytes(1, "scope-only")]
        assert peer.command("MGET", key_b) == [row_bytes(1, "scope-only")]
        locker = start_idle_transaction(
            f"ALTER TABLE public.{sql_identifier(table)} "
            f"ADD COLUMN pglc_stall_{os.getpid()} integer",
            application_name=f"pglc_ddl_stall_{os.getpid()}",
            admin_connection=True,
        )
        samples: list[float] = []
        for index, row_id in enumerate(allocate_test_row_ids(scoped_table, 100)):
            reader = client if index % 2 == 0 else peer
            key = crud_key(scoped_table, row_id)
            started = time.monotonic()
            assert reader.command("MGET", key) == [None]
            samples.append(time.monotonic() - started)
        samples.sort()
        lock_timeout_ms = guc_number("pg_local_cache.lock_timeout_ms")
        assert samples[98] < min(0.12, lock_timeout_ms / 2000), (
            f"B miss p99 under DDL lock was {samples[98] * 1000:.1f} ms"
        )
        assert client.command("MGET", key_b) == [row_bytes(1, "scope-only")]
    finally:
        if locker is not None:
            finish_writer(locker, commit=False)
        peer.close()
        client.close()


def test_backpressure_preserves_every_response(table: str) -> None:
    client = RespConnection(receive_buffer=4096)
    peer: RespConnection | None = None
    try:
        client.socket.settimeout(20)
        row_ids = allocate_test_row_ids(table, 8)
        keys = [crud_key(table, row_id) for row_id in row_ids]
        value_bytes = 6_000
        sql(
            f"INSERT INTO public.{sql_identifier(table)} (id, value) VALUES "
            + ", ".join(
                f"({row_id}, repeat('x', {value_bytes}))" for row_id in row_ids
            )
        )
        expected = [row_bytes(row_id, "x" * value_bytes) for row_id in row_ids]
        assert client.command("MGET", *keys) == expected
        peer = same_worker_peer(client)
        before = json.loads(peer.command("STAT"))
        encoded_mget = client.encode("MGET", *keys)
        tail = (
            client.encode("DEL", crud_key(table, 3))
            + client.encode("MGET", crud_key(table, 3))
        )
        count = min(
            1024,
            (MAX_PIPELINE_INPUT_BYTES - len(tail) - 1) // len(encoded_mget),
        )
        expected_mget_reply_bytes = len(f"*{len(expected)}\r\n") + sum(
            len(value) + len(str(len(value))) + 5 for value in expected
        )
        expected_reply_bytes = count * expected_mget_reply_bytes
        assert expected_reply_bytes >= max(
            4 * PGLC_OUTPUT_BUFFER_MAX_BYTES, 4 * 1024 * 1024
        )
        batch = encoded_mget * count + tail
        assert len(batch) < MAX_PIPELINE_INPUT_BYTES
        client.socket.sendall(batch)
        if not TLS_CA_FILE:
            client.socket.shutdown(socket.SHUT_WR)

        # The response is much larger than the deliberately restricted receive
        # window. Require a short write or EAGAIN, then prove the same event
        # loop stays serviceable.
        deadline = time.monotonic() + 5
        while True:
            progress = json.loads(peer.command("STAT"))
            if (
                progress["output_backpressure_events"]
                > before["output_backpressure_events"]
            ):
                break
            if time.monotonic() >= deadline:
                raise AssertionError("server did not encounter output backpressure")
            time.sleep(0.01)
        assert peer.command("ECHO", "same-worker-live") == b"same-worker-live"
        for _ in range(count):
            assert client.read_response() == expected
        # The mutating command is deliberately placed after enough large
        # replies to trigger output backpressure.  Its input cursor may only
        # advance after the integer response is durably queued; replaying DEL
        # after EAGAIN would return 0 instead of 1.
        assert client.read_response() == 1
        assert client.read_response() == [None]
        assert sql(f"SELECT count(*) FROM public.{table} WHERE id = 3") == "0"
        after = json.loads(peer.command("STAT"))
        assert (
            after["output_backpressure_events"]
            > before["output_backpressure_events"]
        )
        assert after["cache_hits"] - before["cache_hits"] == count * len(keys)
        assert after["cache_misses"] - before["cache_misses"] == 1
        assert after["database_reads"] - before["database_reads"] == 1
        assert after["database_writes"] - before["database_writes"] == 1
        if TLS_CA_FILE:
            assert client.command("PING") == "PONG"
        else:
            try:
                client.read_response()
                raise AssertionError(
                    "backpressured half-close remained open after flush"
                )
            except (EOFError, ConnectionResetError):
                pass
    finally:
        if peer is not None:
            peer.close()
        client.close()


def test_close_after_flush(table: str) -> None:
    malformed = RespConnection()
    try:
        malformed.socket.sendall(
            malformed.encode("PING")
            + b"*x\r\n"
            + malformed.encode("DEL", crud_key(table, 4))
        )
        assert malformed.read_response() == "PONG"
        try:
            malformed.read_response()
            raise AssertionError("malformed request did not return an error")
        except RespError as error:
            assert "invalid decimal length" in str(error)
        try:
            malformed.read_response()
            raise AssertionError("close-after-flush processed a trailing command")
        except (EOFError, ConnectionResetError):
            pass
    finally:
        malformed.close()
    assert sql(f"SELECT count(*) FROM public.{table} WHERE id = 4") == "1"

    if not AUTH_TOKEN:
        return
    unauthenticated = RespConnection(authenticate=False)
    try:
        bad_auth = unauthenticated.encode("AUTH", AUTH_TOKEN + "-wrong")
        unauthenticated.socket.sendall(
            bad_auth * 5
            + unauthenticated.encode("AUTH", AUTH_TOKEN)
            + unauthenticated.encode("DEL", crud_key(table, 5))
        )
        for _ in range(5):
            try:
                unauthenticated.read_response()
                raise AssertionError("invalid AUTH did not return an error")
            except RespError as error:
                assert "WRONGPASS" in str(error)
        try:
            unauthenticated.read_response()
            raise AssertionError("command after AUTH failure limit was processed")
        except (EOFError, ConnectionResetError):
            pass
    finally:
        unauthenticated.close()
    assert sql(f"SELECT count(*) FROM public.{table} WHERE id = 5") == "1"


def test_transactional_commit_and_rollback(table: str) -> None:
    row_id = allocate_test_row_ids(table)[0]
    initial_value = "transactional-initial"
    sql(
        f"INSERT INTO public.{sql_identifier(table)} (id, value) "
        f"VALUES ({row_id}, '{initial_value}')"
    )
    client = RespConnection()
    key = crud_key(table, row_id)
    commit_writer: subprocess.Popen[str] | None = None
    rollback_writer: subprocess.Popen[str] | None = None
    try:
        initial = row_bytes(row_id, initial_value)
        committed = row_bytes(row_id, "committed")
        assert_eventual_value(client, key, initial)

        before_commit_writer = json.loads(client.command("STAT"))
        commit_writer = start_idle_writer(
            table,
            "committed",
            application_name=f"pglc_pipeline_commit_{os.getpid()}",
            row_id=row_id,
        )
        # The row trigger has collected the key in the writer's transaction,
        # but the new tuple is not visible yet.  The old committed value
        # remains the only valid response until PRE_COMMIT publishes the
        # invalidation fence.
        during_commit_writer = json.loads(client.command("STAT"))
        assert (
            during_commit_writer["invalidations"]
            == before_commit_writer["invalidations"]
        )
        assert mget_one(client, key) == initial
        after_open_commit_get = json.loads(client.command("STAT"))
        assert (
            after_open_commit_get["cache_hits"]
            - during_commit_writer["cache_hits"]
            == 1
        )
        assert (
            after_open_commit_get["cache_misses"]
            == during_commit_writer["cache_misses"]
        )
        assert (
            after_open_commit_get["database_reads"]
            == during_commit_writer["database_reads"]
        )
        finish_writer(commit_writer, commit=True)
        after_commit = json.loads(client.command("STAT"))
        assert after_commit["invalidations"] - before_commit_writer["invalidations"] == 1
        assert mget_one(client, key) == committed
        after_commit_refill = json.loads(client.command("STAT"))
        assert (
            after_commit_refill["cache_misses"]
            - after_commit["cache_misses"]
            == 1
        )
        assert (
            after_commit_refill["database_reads"]
            - after_commit["database_reads"]
            == 1
        )
        assert after_commit_refill["cache_hits"] == after_commit["cache_hits"]
        before_commit_hit = after_commit_refill
        assert mget_one(client, key) == committed
        after_commit_hit = json.loads(client.command("STAT"))
        assert after_commit_hit["cache_hits"] - before_commit_hit["cache_hits"] == 1
        assert after_commit_hit["cache_misses"] == before_commit_hit["cache_misses"]
        assert (
            after_commit_hit["database_reads"]
            == before_commit_hit["database_reads"]
        )

        before_rollback_writer = after_commit_hit
        rollback_writer = start_idle_writer(
            table,
            "rolled-back",
            application_name=f"pglc_pipeline_rollback_{os.getpid()}",
            row_id=row_id,
        )
        during_rollback_writer = json.loads(client.command("STAT"))
        assert (
            during_rollback_writer["invalidations"]
            == before_rollback_writer["invalidations"]
        )
        assert mget_one(client, key) == committed
        after_open_rollback_get = json.loads(client.command("STAT"))
        assert (
            after_open_rollback_get["cache_hits"]
            - during_rollback_writer["cache_hits"]
            == 1
        )
        assert (
            after_open_rollback_get["cache_misses"]
            == during_rollback_writer["cache_misses"]
        )
        assert (
            after_open_rollback_get["database_reads"]
            == during_rollback_writer["database_reads"]
        )
        finish_writer(rollback_writer, commit=False)
        before_rollback_hit = json.loads(client.command("STAT"))
        assert (
            before_rollback_hit["invalidations"]
            == before_rollback_writer["invalidations"]
        )
        assert mget_one(client, key) == committed
        after_rollback_hit = json.loads(client.command("STAT"))
        assert (
            after_rollback_hit["cache_hits"]
            - before_rollback_hit["cache_hits"]
            == 1
        )
        assert (
            after_rollback_hit["cache_misses"]
            == before_rollback_hit["cache_misses"]
        )
        assert (
            after_rollback_hit["database_reads"]
            == before_rollback_hit["database_reads"]
        )
    finally:
        terminate_writer(commit_writer)
        terminate_writer(rollback_writer)
        client.close()


# The late-fill race is covered by the concurrent stress test (to be added).
def test_uncommitted_write_is_not_served_before_commit(table: str) -> None:
    row_id = allocate_test_row_ids(table)[0]
    before_commit = "uncommitted-before"
    sql(
        f"INSERT INTO public.{sql_identifier(table)} (id, value) "
        f"VALUES ({row_id}, '{before_commit}')"
    )
    client = RespConnection()
    key = crud_key(table, row_id)
    writer: subprocess.Popen[str] | None = None
    try:
        # Start from a cache miss so the read performed during the open write
        # transaction has to fill from PostgreSQL's committed snapshot.
        assert isinstance(
            client.command("INVALIDATE", f"CRUD:{PGDATABASE}.public.{table}"), int
        )
        writer = start_idle_writer(
            table,
            "committed-after-write",
            application_name=f"pglc_pipeline_uncommitted_write_{os.getpid()}",
            row_id=row_id,
        )
        assert_eventual_value(client, key, row_bytes(row_id, before_commit))
        finish_writer(writer, commit=True)
        writer = None
        assert_eventual_value(
            client, key, row_bytes(row_id, "committed-after-write")
        )
    finally:
        terminate_writer(writer)
        client.close()


def run_fill_paused_before_store(
    table: str,
    barrier_table: str,
    key: str,
    during_pause: Callable[[], None],
    *,
    pause_point: str = "before_store",
) -> tuple[RespConnection, object, dict[str, object], dict[str, object]]:
    client = RespConnection(socket_timeout=45)
    locker: subprocess.Popen[str] | None = None
    thread: threading.Thread | None = None
    results: dict[str, object] = {}
    succeeded = False
    try:
        set_test_pause(pause_point, barrier_table)
        before = read_cache_stats()
        locker = start_table_locker(
            barrier_table,
            application_name=f"pglc_fill_barrier_{os.getpid()}",
        )
        thread = threading.Thread(
            target=run_mget_thread,
            args=(client, [key], results, "fill"),
            name="pglc-paused-fill",
        )
        thread.start()
        wait_for_blocked_worker_pid(barrier_table, timeout=10)
        during_pause()
        finish_writer(locker, commit=True)
        locker = None
        thread.join(timeout=10)
        assert not thread.is_alive(), "paused fill did not finish"
        after = read_cache_stats()
        succeeded = True
        return client, results["fill"], before, after
    finally:
        try:
            if locker is not None:
                finish_writer(locker, commit=True)
        finally:
            try:
                if thread is not None and thread.is_alive():
                    thread.join(timeout=10)
            finally:
                try:
                    set_test_pause(None)
                finally:
                    if not succeeded:
                        client.close()


def publish_test_fence(fence_kind: str, namespace: str) -> None:
    if fence_kind == "relation":
        sql(f"SELECT local_cache.invalidate({sql_literal(namespace)})")
    elif fence_kind == "global":
        sql("SELECT public.pglc_test_collect_global()")
    else:
        raise AssertionError(warm_hit_diagnostics(fence_kind))


def test_warm_hit_snapshot_and_copy_fences(
    table: str,
    namespace: str,
    barrier_table: str,
    second_barrier_table: str,
) -> None:
    row_id = allocate_test_row_ids(table)[0]
    value = "warm-hit-fence"
    sql(
        f"INSERT INTO public.{sql_identifier(table)} (id, value) "
        f"VALUES ({row_id}, '{value}')"
    )
    key = crud_key(table, row_id)
    expected = row_bytes(row_id, value)
    try:
        for fence_kind, pause_point, fence_state in (
            ("relation", "after_lookup_unlock", "completed"),
            ("relation", "before_lookup_lock", "outstanding"),
            ("global", "after_lookup_unlock", "outstanding"),
            ("global", "after_lookup_unlock", "completed"),
        ):
            iteration = (fence_kind, pause_point, fence_state)
            context_token = _WARM_HIT_ITERATION.set(iteration)
            client: RespConnection | None = None
            reader_locker: subprocess.Popen[str] | None = None
            thread: threading.Thread | None = None
            fence_locker: subprocess.Popen[str] | None = None
            fence_writer: subprocess.Popen[str] | None = None
            results: dict[str, object] = {}
            try:
                # A new socket per case prevents a failed or timed-out MGET
                # from shifting the next case onto a stale RESP reply.
                client = RespConnection(socket_timeout=45)
                warm_value = mget_one(client, key)
                assert warm_value == expected, warm_hit_diagnostics(
                    {"expected": expected, "actual": warm_value}
                )
                before = read_cache_stats()
                reader_locker = start_table_locker(
                    barrier_table,
                    application_name=f"pglc_warm_read_barrier_{os.getpid()}",
                )
                set_test_pause(pause_point, barrier_table)
                thread = threading.Thread(
                    target=run_mget_thread,
                    args=(client, [key], results, "warm"),
                    name="pglc-paused-warm-hit",
                )
                thread.start()
                wait_for_blocked_worker_pid(barrier_table, timeout=10)

                if fence_state == "outstanding":
                    set_test_pause(f"after_{fence_kind}_begin", second_barrier_table)
                    fence_locker = start_table_locker(
                        second_barrier_table,
                        application_name=f"pglc_warm_fence_barrier_{os.getpid()}",
                    )
                    fence_writer = start_relation_or_global_fence_writer(
                        fence_kind,
                        namespace,
                        application_name=f"pglc_warm_fence_{os.getpid()}",
                    )
                    wait_for_blocked_relation_pid(
                        second_barrier_table,
                        application_name=f"pglc_warm_fence_{os.getpid()}",
                        psql_process=fence_writer,
                        timeout=10,
                    )
                    set_test_pause(None)
                else:
                    set_test_pause(None)
                    publish_test_fence(fence_kind, namespace)

                finish_writer(reader_locker, commit=True)
                reader_locker = None
                thread.join(timeout=10)
                assert not thread.is_alive(), warm_hit_diagnostics(
                    {"thread_alive": thread.is_alive(), "results": results}
                )
                after = read_cache_stats()
                assert results.get("warm") == [expected], warm_hit_diagnostics(
                    {"expected": [expected], "actual": results}
                )
                assert after["cache_hits"] == before["cache_hits"], (
                    warm_hit_diagnostics(
                        {"before": before, "after": after, "counter": "cache_hits"}
                    )
                )
                assert after["database_reads"] >= before["database_reads"] + 1, (
                    warm_hit_diagnostics(
                        {
                            "before": before,
                            "after": after,
                            "counter": "database_reads",
                        }
                    )
                )
                if fence_state == "outstanding":
                    if fence_kind == "relation":
                        assert after["dirty_relations"] > 0, warm_hit_diagnostics(
                            {"actual": after["dirty_relations"], "stats": after}
                        )
                    else:
                        assert after["global_dirty_writers"] > 0, warm_hit_diagnostics(
                            {
                                "actual": after["global_dirty_writers"],
                                "stats": after,
                            }
                        )
                    assert fence_locker is not None and fence_writer is not None, (
                        warm_hit_diagnostics(
                            {
                                "fence_locker": fence_locker,
                                "fence_writer": fence_writer,
                            }
                        )
                    )
                    finish_writer(fence_locker, commit=True)
                    fence_locker = None
                    finish_fence_writer(fence_writer)
                    fence_writer = None
            finally:
                try:
                    set_test_pause(None)
                finally:
                    try:
                        if reader_locker is not None:
                            finish_writer(reader_locker, commit=True)
                    finally:
                        try:
                            if fence_locker is not None:
                                finish_writer(fence_locker, commit=True)
                        finally:
                            try:
                                if fence_writer is not None:
                                    finish_fence_writer(fence_writer)
                            finally:
                                try:
                                    if thread is not None and thread.is_alive():
                                        thread.join(timeout=10)
                                        assert not thread.is_alive(), (
                                            warm_hit_diagnostics(
                                                {
                                                    "thread_alive": thread.is_alive(),
                                                    "results": results,
                                                }
                                            )
                                        )
                                finally:
                                    try:
                                        if client is not None:
                                            client.close()
                                    finally:
                                        _WARM_HIT_ITERATION.reset(context_token)
    finally:
        set_test_pause(None)


def test_relation_global_claim_store_fences(
    table: str,
    namespace: str,
    barrier_table: str,
    second_barrier_table: str,
) -> None:
    cases = (
        ("relation", "outstanding", "after_claim_unlock"),
        ("relation", "completed", "before_store_lock"),
        ("global", "outstanding", "after_store_unlock"),
        ("global", "completed", "before_store_lock"),
    )
    row_ids = allocate_test_row_ids(table, len(cases))
    client = RespConnection(socket_timeout=45)
    try:
        for offset, (fence_kind, fence_state, pause_point) in enumerate(cases):
            row_id = row_ids[offset]
            value = f"claim-store-{fence_kind}-{fence_state}"
            key = crud_key(table, row_id)
            sql(
                f"INSERT INTO public.{sql_identifier(table)} (id, value) "
                f"VALUES ({row_id}, '{value}')"
            )
            before = read_cache_stats()
            reader_locker = start_table_locker(
                barrier_table,
                application_name=f"pglc_claim_store_reader_{os.getpid()}",
            )
            thread: threading.Thread | None = None
            fence_locker: subprocess.Popen[str] | None = None
            fence_writer: subprocess.Popen[str] | None = None
            results: dict[str, object] = {}
            try:
                set_test_pause(pause_point, barrier_table)
                thread = threading.Thread(
                    target=run_mget_thread,
                    args=(client, [key], results, "fill"),
                    name="pglc-paused-fenced-fill",
                )
                thread.start()
                wait_for_blocked_worker_pid(barrier_table, timeout=10)

                if fence_state == "outstanding":
                    set_test_pause(f"after_{fence_kind}_begin", second_barrier_table)
                    fence_locker = start_table_locker(
                        second_barrier_table,
                        application_name=f"pglc_claim_store_fence_barrier_{os.getpid()}",
                    )
                    fence_writer = start_relation_or_global_fence_writer(
                        fence_kind,
                        namespace,
                        application_name=f"pglc_claim_store_fence_{os.getpid()}",
                    )
                    wait_for_blocked_relation_pid(
                        second_barrier_table,
                        application_name=f"pglc_claim_store_fence_{os.getpid()}",
                        psql_process=fence_writer,
                        timeout=10,
                    )
                    set_test_pause(None)
                else:
                    set_test_pause(None)
                    publish_test_fence(fence_kind, namespace)

                finish_writer(reader_locker, commit=True)
                reader_locker = None
                thread.join(timeout=10)
                assert not thread.is_alive(), "paused fenced fill did not finish"
                after = read_cache_stats()
                assert results["fill"] == [row_bytes(row_id, value)], results
                assert after["cache_hits"] == before["cache_hits"], (
                    fence_kind,
                    fence_state,
                    pause_point,
                    before,
                    after,
                )
                assert after["database_reads"] >= before["database_reads"] + 1, (
                    fence_kind,
                    fence_state,
                    pause_point,
                    before,
                    after,
                )
                if fence_state == "outstanding":
                    if fence_kind == "relation":
                        assert after["dirty_relations"] > 0, after
                    else:
                        assert after["global_dirty_writers"] > 0, after
                if fence_locker is not None:
                    finish_writer(fence_locker, commit=True)
                    fence_locker = None
                if fence_writer is not None:
                    finish_fence_writer(fence_writer)
                    fence_writer = None

                before_refill = read_cache_stats()
                assert mget_one(client, key) == row_bytes(row_id, value)
                after_refill = read_cache_stats()
                assert after_refill["database_reads"] == (
                    before_refill["database_reads"] + 1
                ), (fence_kind, fence_state, before_refill, after_refill)
                assert mget_one(client, key) == row_bytes(row_id, value)
                after_hit = read_cache_stats()
                assert after_hit["cache_hits"] == after_refill["cache_hits"] + 1
                assert after_hit["database_reads"] == after_refill["database_reads"]
            finally:
                set_test_pause(None)
                if reader_locker is not None:
                    finish_writer(reader_locker, commit=True)
                if fence_locker is not None:
                    finish_writer(fence_locker, commit=True)
                if fence_writer is not None:
                    finish_fence_writer(fence_writer)
                if thread is not None and thread.is_alive():
                    thread.join(timeout=10)
                    assert not thread.is_alive(), "fenced-fill thread did not stop"
    finally:
        set_test_pause(None)
        client.close()


def test_reload_claim_keeps_truncated_payload_invalid(
    table: str, barrier_table: str
) -> None:
    clients = distinct_worker_connections(2, socket_timeout=45)
    key = crud_key(table, 1)
    old_value = row_bytes(1, "before-truncate")
    singleflight_wait_ms = guc_number("pg_local_cache.singleflight_wait_ms")
    follower_margin_seconds = 1.5
    locker: subprocess.Popen[str] | None = None
    thread: threading.Thread | None = None
    follower_thread: threading.Thread | None = None
    results: dict[str, object] = {}
    try:
        assert mget_one(clients[1], key) == old_value
        sql(f"TRUNCATE TABLE public.{sql_identifier(table)}")
        set_test_pause("before_store", barrier_table)
        locker = start_table_locker(
            barrier_table,
            application_name=f"pglc_truncate_claim_barrier_{os.getpid()}",
        )
        thread = threading.Thread(
            target=run_mget_thread,
            args=(clients[0], [key], results, "owner"),
            name="pglc-truncate-reload-owner",
        )
        thread.start()
        owner_pid = wait_for_blocked_worker_pid(barrier_table, timeout=10)

        before_follower = read_cache_stats()
        follower_thread = threading.Thread(
            target=run_mget_thread,
            args=(clients[1], [key], results, "follower"),
            name="pglc-truncate-reload-follower",
        )
        follower_started = time.monotonic()
        follower_thread.start()
        follower_timeout = (
            singleflight_wait_ms / 1000 + follower_margin_seconds
        )
        follower_thread.join(timeout=follower_timeout)
        follower_elapsed = time.monotonic() - follower_started
        assert not follower_thread.is_alive(), (
            "follower exceeded singleflight wait plus margin "
            f"({follower_elapsed:.3f}s > {follower_timeout:.3f}s)"
        )
        assert follower_elapsed <= follower_timeout
        assert results.get("follower") == [None], results
        after_follower = read_cache_stats()
        assert after_follower["cache_hits"] == before_follower["cache_hits"]
        assert after_follower["singleflight_waiters"] >= (
            before_follower["singleflight_waiters"] + 1
        )
        assert after_follower["database_reads"] >= (
            before_follower["database_reads"] + 1
        )

        assert sql(f"SELECT pg_cancel_backend({owner_pid})") == "t"
        finish_writer(locker, commit=True)
        locker = None
        thread.join(timeout=10)
        assert not thread.is_alive(), "canceled reload owner did not finish"
        assert isinstance(results.get("owner"), RespError), results
        set_test_pause(None)

        before_retry = read_cache_stats()
        assert mget_one(clients[1], key) is None
        after_retry = read_cache_stats()
        assert after_retry["cache_hits"] == before_retry["cache_hits"]
        assert after_retry["database_reads"] >= before_retry["database_reads"] + 1
        assert after_retry["loading_entries"] == 0
    finally:
        set_test_pause(None)
        if locker is not None:
            finish_writer(locker, commit=True)
        if thread is not None and thread.is_alive():
            thread.join(timeout=10)
            assert not thread.is_alive(), "truncate reload owner did not stop"
        if follower_thread is not None and follower_thread.is_alive():
            follower_thread.join(timeout=10)
            assert not follower_thread.is_alive(), "truncate reload follower did not stop"
        for client in clients:
            client.close()


def test_marker_exhaustion_falls_back_safely(
    table: str,
    namespace: str,
    barrier_table: str,
    second_barrier_table: str,
) -> None:
    row_id, first_marker_id, second_marker_id = allocate_test_row_ids(table, 3)
    value = "marker-exhaustion"
    sql(
        f"INSERT INTO public.{sql_identifier(table)} (id, value) "
        f"VALUES ({row_id}, '{value}')"
    )
    key = crud_key(table, row_id)
    expected = row_bytes(row_id, value)
    warm_client = RespConnection(socket_timeout=45)
    first_locker: subprocess.Popen[str] | None = None
    second_locker: subprocess.Popen[str] | None = None
    first_writer: subprocess.Popen[str] | None = None
    second_writer: subprocess.Popen[str] | None = None
    try:
        assert mget_one(warm_client, key) == expected
        before = read_cache_stats()
        assert before["dirty_marker_entries"] == 0, before
        set_test_dirty_marker_limit(1)

        set_test_pause("after_publish", barrier_table)
        first_locker = start_table_locker(
            barrier_table,
            application_name=f"pglc_marker_barrier_first_{os.getpid()}",
        )
        first_writer = start_publishing_key_writer(
            table,
            namespace,
            canonical_int8_key(first_marker_id),
            application_name=f"pglc_marker_first_{os.getpid()}",
        )
        wait_for_blocked_relation_pid(
            barrier_table,
            application_name=f"pglc_marker_first_{os.getpid()}",
            psql_process=first_writer,
            timeout=10,
        )

        set_test_pause("after_publish", second_barrier_table)
        second_locker = start_table_locker(
            second_barrier_table,
            application_name=f"pglc_marker_barrier_second_{os.getpid()}",
        )
        second_writer = start_publishing_key_writer(
            table,
            namespace,
            canonical_int8_key(second_marker_id),
            application_name=f"pglc_marker_second_{os.getpid()}",
        )
        wait_for_blocked_relation_pid(
            second_barrier_table,
            application_name=f"pglc_marker_second_{os.getpid()}",
            psql_process=second_writer,
            timeout=10,
        )

        set_test_pause(None)
        active = read_cache_stats()
        assert active["dirty_marker_entries"] >= before["dirty_marker_entries"] + 1, active
        assert active["dirty_marker_fallbacks_total"] >= (
            before["dirty_marker_fallbacks_total"] + 1
        ), (before, active)
        assert active["dirty_relations"] > 0, active

        before_bypass = read_cache_stats()
        assert mget_one(warm_client, key) == expected
        after_bypass = read_cache_stats()
        assert after_bypass["database_reads"] >= before_bypass["database_reads"] + 1, (
            before_bypass,
            after_bypass,
        )

        finish_writer(first_locker, commit=True)
        first_locker = None
        finish_publishing_key_writer(first_writer)
        first_writer = None
        finish_writer(second_locker, commit=True)
        second_locker = None
        finish_publishing_key_writer(second_writer)
        second_writer = None
        set_test_dirty_marker_limit(None)
        finished = read_cache_stats()
        assert finished["dirty_marker_entries"] == before["dirty_marker_entries"], finished
        assert finished["dirty_relations"] == 0, finished
        assert finished["global_dirty_writers"] == 0, finished
    finally:
        set_test_pause(None)
        set_test_dirty_marker_limit(None)
        for locker in (first_locker, second_locker):
            if locker is not None:
                finish_writer(locker, commit=True)
        for writer in (first_writer, second_writer):
            if writer is not None:
                finish_publishing_key_writer(writer)
        warm_client.close()


def test_dirty_key_dedup_and_relation_fallback(
    table: str,
    namespace: str,
    barrier_table: str,
) -> None:
    row_id, overflow_id = allocate_test_row_ids(table, 2)
    sql(
        f"INSERT INTO public.{sql_identifier(table)} (id, value) "
        f"VALUES ({row_id}, 'dirty-dedup-0'), ({overflow_id}, 'overflow-0')"
    )
    key = crud_key(table, row_id)
    client = RespConnection(socket_timeout=45)
    locker: subprocess.Popen[str] | None = None
    writer: subprocess.Popen[str] | None = None
    set_test_max_dirty_keys(1)
    try:
        before = read_cache_stats()
        updates = "; ".join(
            f"UPDATE public.{sql_identifier(table)} SET value = 'dirty-dedup-{index}' "
            f"WHERE id = {row_id}"
            for index in range(20)
        )
        sql(f"BEGIN; {updates}; COMMIT")
        after_repeated = read_cache_stats()
        assert after_repeated["dirty_key_limit_fallbacks"] == (
            before["dirty_key_limit_fallbacks"]
        ), (before, after_repeated)

        expected = row_bytes(row_id, "dirty-dedup-19")
        assert mget_one(client, key) == expected
        before_overflow = read_cache_stats()
        set_test_pause("after_publish", barrier_table)
        locker = start_table_locker(
            barrier_table,
            application_name=f"pglc_dirty_key_barrier_{os.getpid()}",
        )
        writer = start_publishing_keys_writer(
            table,
            namespace,
            [key, crud_key(table, overflow_id)],
            application_name=f"pglc_dirty_key_overflow_{os.getpid()}",
        )
        wait_for_blocked_relation_pid(
            barrier_table,
            application_name=f"pglc_dirty_key_overflow_{os.getpid()}",
            psql_process=writer,
            timeout=10,
        )

        set_test_pause(None)
        active = read_cache_stats()
        assert active["dirty_key_limit_fallbacks"] >= (
            before_overflow["dirty_key_limit_fallbacks"] + 1
        ), (before_overflow, active)
        assert active["dirty_relations"] > 0, active

        before_bypass = read_cache_stats()
        assert mget_one(client, key) == expected
        after_bypass = read_cache_stats()
        assert after_bypass["database_reads"] >= (
            before_bypass["database_reads"] + 1
        ), (before_bypass, after_bypass)

        finish_writer(locker, commit=True)
        locker = None
        finish_publishing_key_writer(writer)
        writer = None
        finished = read_cache_stats()
        assert finished["dirty_relations"] == 0, finished
        assert finished["global_dirty_writers"] == 0, finished
    finally:
        set_test_pause(None)
        set_test_max_dirty_keys(None)
        if locker is not None:
            finish_writer(locker, commit=True)
        if writer is not None:
            finish_publishing_key_writer(writer)
        client.close()


def test_unrelated_key_fill_survives_keyed_write(
    table: str, barrier_table: str
) -> None:
    row_id, other_row_id = allocate_test_row_ids(table, 2)
    original = "unrelated-fill"
    sql(
        f"INSERT INTO public.{sql_identifier(table)} (id, value) "
        f"VALUES ({row_id}, '{original}'), ({other_row_id}, 'other-key-before')"
    )

    def update_other_key() -> None:
        sql(
            f"UPDATE public.{sql_identifier(table)} SET value = 'other-key-write' "
            f"WHERE id = {other_row_id}"
        )

    client, response, before, after = run_fill_paused_before_store(
        table, barrier_table, crud_key(table, row_id), update_other_key
    )
    try:
        assert response == [row_bytes(row_id, original)], response
        assert after["database_reads"] >= before["database_reads"] + 1
        before_hit = read_cache_stats()
        assert mget_one(client, crud_key(table, row_id)) == row_bytes(row_id, original)
        after_hit = read_cache_stats()
        assert after_hit["cache_hits"] >= before_hit["cache_hits"] + 1
        assert after_hit["database_reads"] == before_hit["database_reads"]
    finally:
        client.close()


def test_same_key_relation_and_global_fences(
    table: str, namespace: str, barrier_table: str
) -> None:
    # Each global fence advances the sequence and epoch on different steps.
    # Keep them divergent while exercising fills and lease transitions below.
    for _ in range(3):
        sql("SELECT public.pglc_test_collect_global()")

    warm_row_id = allocate_test_row_ids(table)[0]
    warm_value = "global-fences-warm"
    sql(
        f"INSERT INTO public.{sql_identifier(table)} (id, value) "
        f"VALUES ({warm_row_id}, '{warm_value}')"
    )
    warm_client = RespConnection(socket_timeout=45)
    try:
        warm_key = crud_key(table, warm_row_id)
        before_fill = read_cache_stats()
        assert mget_one(warm_client, warm_key) == row_bytes(warm_row_id, warm_value)
        after_fill = read_cache_stats()
        assert after_fill["database_reads"] >= before_fill["database_reads"] + 1
        assert mget_one(warm_client, warm_key) == row_bytes(warm_row_id, warm_value)
        after_hit = read_cache_stats()
        assert after_hit["database_reads"] == after_fill["database_reads"]
        assert after_hit["cache_hits"] >= after_fill["cache_hits"] + 1
    finally:
        warm_client.close()

    cases: list[
        tuple[str, Callable[[int], Callable[[], None]] | None, str, str, int]
    ] = [
        (
            "same-key",
            lambda row_id: lambda: sql(
                f"UPDATE public.{sql_identifier(table)} "
                f"SET value = 'same-key-new' WHERE id = {row_id}"
            ),
            "same-key-new",
            "before_store",
            1,
        ),
        (
            "relation",
            lambda _row_id: lambda: sql(
                f"SELECT local_cache.invalidate({sql_literal(namespace)})"
            ),
            "relation-fence",
            "before_store",
            1,
        ),
        (
            "global-before-claim",
            None,
            "global-fence",
            "before_claim_lock",
            0,
        ),
        (
            "global-after-claim",
            None,
            "global-fence",
            "after_claim_unlock",
            1,
        ),
        (
            "global-after-store",
            None,
            "global-fence",
            "after_store_unlock",
            1,
        ),
    ]
    row_ids = allocate_test_row_ids(table, len(cases))
    for offset, (
        name,
        mutation_for,
        updated_value,
        pause_point,
        expected_extra_read,
    ) in enumerate(cases):
        row_id = row_ids[offset]
        old_value = f"{name}-old"
        sql(
            f"INSERT INTO public.{sql_identifier(table)} (id, value) "
            f"VALUES ({row_id}, '{old_value}')"
        )
        if mutation_for is None:
            mutation = lambda: sql("SELECT public.pglc_test_collect_global()")
        else:
            mutation = mutation_for(row_id)

        client, response, before_fence, after_fence = run_fill_paused_before_store(
            table,
            barrier_table,
            crud_key(table, row_id),
            mutation,
            pause_point=pause_point,
        )
        snapshots = {
            "before_fence": before_fence,
            "after_fence": after_fence,
        }

        def failure_context() -> dict[str, object]:
            fields = (
                "global_dirty_writers",
                "dirty_marker_entries",
                "dirty_marker_fallbacks_total",
                "dirty_key_limit_fallbacks",
                "relation_state_admission_rejections",
                "cache_bypass",
                "refresh_reservations_outstanding",
            )
            return {
                "case": name,
                **{
                    label: {
                        "database_reads": stats.get("database_reads"),
                        "cache_hits": stats.get("cache_hits"),
                        **{field: stats.get(field) for field in fields},
                        **{
                            field: value
                            for field, value in stats.items()
                            if field.startswith("refresh_skips_")
                        },
                    }
                    for label, stats in snapshots.items()
                },
            }

        try:
            assert response == [row_bytes(row_id, old_value)], failure_context()
            before_refill = read_cache_stats()
            snapshots["before_refill"] = before_refill
            expected_value = updated_value if name == "same-key" else old_value
            refill_result = mget_one(client, crud_key(table, row_id))
            after_refill = read_cache_stats()
            snapshots["after_refill"] = after_refill
            assert refill_result == row_bytes(
                row_id, expected_value
            ), failure_context()
            if expected_extra_read:
                assert after_refill["database_reads"] >= (
                    before_refill["database_reads"] + expected_extra_read
                ), failure_context()
            else:
                assert after_refill["database_reads"] == (
                    before_refill["database_reads"]
                ), failure_context()
            hit_result = mget_one(client, crud_key(table, row_id))
            after_hit = read_cache_stats()
            snapshots["after_hit"] = after_hit
            assert hit_result == row_bytes(
                row_id, expected_value
            ), failure_context()
            assert after_hit["database_reads"] == after_refill["database_reads"], (
                failure_context()
            )
            assert after_hit["cache_hits"] >= after_refill["cache_hits"] + 1, (
                failure_context()
            )
        finally:
            client.close()


def test_namespace_invalidation_preserves_other_scope(
    table: str,
    namespace: str,
    scoped_table: str,
    barrier_table: str,
) -> None:
    cached_key = crud_key(scoped_table, 1)
    fill_id = allocate_test_row_ids(scoped_table)[0]
    fill_key = crud_key(scoped_table, fill_id)
    sql(
        f"INSERT INTO public.{sql_identifier(scoped_table)} (id, value) "
        f"VALUES ({fill_id}, 'namespace-fill')"
    )
    client = RespConnection(socket_timeout=45)
    try:
        assert mget_one(client, cached_key) == row_bytes(1, "scope-only")
        assert isinstance(mget_one(client, crud_key(table, 1)), bytes)
        invalidated = client.command(
            "INVALIDATE", f"CRUD:{PGDATABASE}.public.{table}"
        )
        assert isinstance(invalidated, int) and invalidated >= 1, invalidated

        def invalidate_other_namespace() -> None:
            sql(f"SELECT local_cache.invalidate({sql_literal(namespace)})")

        fill_client, response, _before, after_fill = run_fill_paused_before_store(
            scoped_table, barrier_table, fill_key, invalidate_other_namespace
        )
        fill_client.close()
        assert response == [row_bytes(fill_id, "namespace-fill")], response
        before_refill = read_cache_stats()
        assert before_refill["database_reads"] == after_fill["database_reads"]
        assert mget_one(client, fill_key) == row_bytes(fill_id, "namespace-fill")
        after_fill_hit = read_cache_stats()
        assert after_fill_hit["database_reads"] == before_refill["database_reads"]

        assert mget_one(client, cached_key) == row_bytes(1, "scope-only")
        after_cached_hit = read_cache_stats()
        assert after_cached_hit["database_reads"] == after_fill_hit["database_reads"]
        assert after_cached_hit["cache_hits"] >= after_fill_hit["cache_hits"] + 1
    finally:
        client.close()


def test_overlapping_publishers_on_one_key(
    table: str,
    namespace: str,
    barrier_table: str,
    second_barrier_table: str,
) -> None:
    row_id = allocate_test_row_ids(table)[0]
    value = "overlapping-publishers"
    sql(
        f"INSERT INTO public.{sql_identifier(table)} (id, value) "
        f"VALUES ({row_id}, '{value}')"
    )
    key = crud_key(table, row_id)
    client = RespConnection()
    assert mget_one(client, key) == row_bytes(row_id, value)
    # Canonical int8 key: decimal byte length, colon, value, semicolon.
    canonical_key = canonical_int8_key(row_id)
    relation = f"public.{sql_identifier(table)}"
    original_incarnation = int(
        sql(
            "SELECT public.pglc_test_relation_incarnation("
            f"'{relation}'::regclass, {sql_literal(namespace)})"
        )
    )
    first: subprocess.Popen[str] | None = None
    second: subprocess.Popen[str] | None = None
    first_locker: subprocess.Popen[str] | None = None
    second_locker: subprocess.Popen[str] | None = None
    try:
        set_test_pause("after_publish", barrier_table)
        first_locker = start_table_locker(
            barrier_table,
            application_name=f"pglc_publish_barrier_first_{os.getpid()}",
        )
        first_name = f"pglc_publish_first_{os.getpid()}"
        first = start_updating_key_writer(
            table, row_id, application_name=first_name
        )
        wait_for_blocked_relation_pid(
            barrier_table,
            application_name=first_name,
            psql_process=first,
            timeout=10,
        )

        set_test_pause("after_publish", second_barrier_table)
        second_locker = start_table_locker(
            second_barrier_table,
            application_name=f"pglc_publish_barrier_second_{os.getpid()}",
        )
        second_name = f"pglc_publish_second_{os.getpid()}"
        # A second UPDATE of this row would wait on the first transaction's row lock.
        second = start_publishing_key_writer(
            table, namespace, canonical_key, application_name=second_name
        )
        wait_for_blocked_relation_pid(
            second_barrier_table,
            application_name=second_name,
            psql_process=second,
            timeout=10,
        )
        # Both publishers have reached their barriers. _forget publishes from
        # this test backend too, so it must not inherit the global pause.
        set_test_pause(None)
        assert int(
            sql(
                "SELECT public.pglc_test_relation_identity_pins("
                f"'{relation}'::regclass, {sql_literal(namespace)})"
            )
        ) == 2

        finish_writer(first_locker, commit=True)
        first_locker = None
        finish_publishing_key_writer(first)
        first = None
        assert second.poll() is None, "second publisher escaped its own barrier"
        assert int(
            sql(
                "SELECT public.pglc_test_relation_identity_pins("
                f"'{relation}'::regclass, {sql_literal(namespace)})"
            )
        ) == 1

        before_while_pinned = read_cache_stats()
        assert mget_one(client, key) == row_bytes(row_id, value)
        after_first_read = read_cache_stats()
        assert after_first_read["database_reads"] >= (
            before_while_pinned["database_reads"] + 1
        )
        assert mget_one(client, key) == row_bytes(row_id, value)
        after_second_read = read_cache_stats()
        assert after_second_read["database_reads"] >= (
            after_first_read["database_reads"] + 1
        ), "remaining publisher fence did not block cache publication"

        # Exercise global publication fallback and _forget in the same commit.
        sql(
            "BEGIN; SELECT public.pglc_test_collect_global(); "
            f"SELECT local_cache._forget({sql_literal(namespace)}, "
            f"'{relation}'::regclass::oid); COMMIT"
        )
        assert int(
            sql(
                "SELECT public.pglc_test_relation_identity_pins("
                f"'{relation}'::regclass, {sql_literal(namespace)})"
            )
        ) == 1
        assert read_cache_stats()["pending_forget"] >= 1

        finish_writer(second_locker, commit=True)
        second_locker = None
        finish_publishing_key_writer(second)
        second = None
        assert int(
            sql(
                "SELECT public.pglc_test_relation_identity_pins("
                f"'{relation}'::regclass, {sql_literal(namespace)})"
            )
        ) == 0
        assert read_cache_stats()["pending_forget"] == 0

        sql(f"SELECT local_cache.detach_table('{relation}'::regclass)")
        sql(
            f"SELECT local_cache.attach_table('{relation}'::regclass, true, "
            f"{sql_literal(namespace)})"
        )
        new_incarnation = int(
            sql(
                "SELECT public.pglc_test_relation_incarnation("
                f"'{relation}'::regclass, {sql_literal(namespace)})"
            )
        )
        assert new_incarnation != original_incarnation
        before_remap_read = read_cache_stats()
        assert wait_for_mapping(client, key) == row_bytes(row_id, value)
        after_remap_read = read_cache_stats()
        assert after_remap_read["database_reads"] >= (
            before_remap_read["database_reads"] + 1
        ), "remap served the old cached value"
        assert mget_one(client, key) == row_bytes(row_id, value)
        assert (
            read_cache_stats()["database_reads"]
            == after_remap_read["database_reads"]
        )
    finally:
        for locker in (first_locker, second_locker):
            if locker is not None:
                finish_writer(locker, commit=True)
        for process in (first, second):
            if process is not None:
                terminate_writer(process)
        set_test_pause(None)
        client.close()


def test_abort_after_dirty_publication(
    table: str, namespace: str, barrier_table: str
) -> None:
    row_id = allocate_test_row_ids(table)[0]
    value = "abort-after-publication"
    sql(
        f"INSERT INTO public.{sql_identifier(table)} (id, value) "
        f"VALUES ({row_id}, '{value}')"
    )
    client = RespConnection()
    key = crud_key(table, row_id)
    try:
        assert mget_one(client, key) == row_bytes(row_id, value)
        before = read_cache_stats()
        set_test_pause("after_publish_abort", barrier_table)
        command = (
            "BEGIN; "
            f"UPDATE public.{sql_identifier(table)} SET value = value "
            f"WHERE id = {row_id}; "
            "COMMIT"
        )
        result = run_psql(
            psql_base_args() + ["-c", command],
            statement=command,
            timeout=20,
        )
        assert result.returncode != 0, result.stdout + result.stderr
        assert "test abort after dirty publication" in result.stderr
        set_test_pause(None)
        after_abort = read_cache_stats()
        assert after_abort["invalidations"] > before["invalidations"]
        assert after_abort["dirty_entries"] == 0
        assert after_abort["global_dirty_writers"] == 0
        before_refill = read_cache_stats()
        assert mget_one(client, key) == row_bytes(row_id, value)
        after_refill = read_cache_stats()
        assert after_refill["database_reads"] >= before_refill["database_reads"] + 1
        assert mget_one(client, key) == row_bytes(row_id, value)
        assert read_cache_stats()["database_reads"] == after_refill["database_reads"]
    finally:
        set_test_pause(None)
        client.close()


def test_partial_reservation_abort_releases_identity(
    table: str, namespace: str
) -> None:
    relation = f"public.{sql_identifier(table)}"
    old_incarnation = int(
        sql(
            "SELECT public.pglc_test_relation_incarnation("
            f"'{relation}'::regclass, {sql_literal(namespace)})"
        )
    )
    key_one = canonical_int8_key(1)
    key_two = canonical_int8_key(2)
    command = (
        "BEGIN; SELECT public.pglc_test_abort_after_reservation(); "
        f"{test_collect_key_sql(table, namespace, key_one)}; "
        f"{test_collect_key_sql(table, namespace, key_two)}; COMMIT"
    )
    result = run_psql(
        psql_base_args() + ["-c", command],
        statement=command,
        timeout=20,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "test abort after dirty reservation" in result.stderr
    assert int(
        sql(
            "SELECT public.pglc_test_relation_identity_pins("
            f"'{relation}'::regclass, {sql_literal(namespace)})"
        )
    ) == 0
    assert read_cache_stats()["dirty_entries"] == 0

    sql(f"SELECT local_cache.detach_table('{relation}'::regclass)")
    sql(
        f"SELECT local_cache.attach_table('{relation}'::regclass, true, "
        f"{sql_literal(namespace)})"
    )
    new_incarnation = int(
        sql(
            "SELECT public.pglc_test_relation_incarnation("
            f"'{relation}'::regclass, {sql_literal(namespace)})"
        )
    )
    assert new_incarnation != old_incarnation


def test_relation_incarnation_forget_recreate(
    table: str,
    namespace: str,
    barrier_table: str,
) -> None:
    row_id, warmed_id = allocate_test_row_ids(table, 2)
    old_fill_value = "old-incarnation-fill"
    relation = f"public.{sql_identifier(table)}"
    sql(
        f"INSERT INTO {relation} (id, value) VALUES ({row_id}, '{old_fill_value}')"
    )
    relation_oid = int(sql(f"SELECT '{relation}'::regclass::oid"))
    old_incarnation = int(
        sql(
            "SELECT public.pglc_test_relation_incarnation("
            f"'{relation}'::regclass, {sql_literal(namespace)})"
        )
    )
    key = crud_key(table, row_id)
    recreated: list[int] = []

    def recreate_during_fill() -> None:
        recreated.append(
            int(
                sql(
                    "SELECT public.pglc_test_recreate_relation_state("
                    f"'{relation}'::regclass, {sql_literal(namespace)})"
                )
            )
        )

    client, response, _before, _after = run_fill_paused_before_store(
        table, barrier_table, key, recreate_during_fill
    )
    try:
        assert response == [row_bytes(row_id, old_fill_value)], response
        assert len(recreated) == 1
        assert recreated[0] != old_incarnation
        assert int(sql(f"SELECT '{relation}'::regclass::oid")) == relation_oid

        before_refill = read_cache_stats()
        assert mget_one(client, key) == row_bytes(row_id, old_fill_value)
        after_refill = read_cache_stats()
        assert after_refill["database_reads"] >= before_refill["database_reads"] + 1
        assert mget_one(client, key) == row_bytes(row_id, old_fill_value)
        assert read_cache_stats()["database_reads"] == after_refill["database_reads"]
    finally:
        client.close()

    warmed_key = crud_key(table, warmed_id)
    warmed_value = "old-warm-entry"
    sql(
        f"INSERT INTO {relation} (id, value) VALUES ({warmed_id}, '{warmed_value}')"
    )
    seed_client = RespConnection()
    try:
        assert mget_one(seed_client, warmed_key) == row_bytes(
            warmed_id, warmed_value
        )
    finally:
        seed_client.close()
    old_warm_incarnation = int(
        sql(
            "SELECT public.pglc_test_relation_incarnation("
            f"'{relation}'::regclass, {sql_literal(namespace)})"
        )
    )
    new_warm_incarnation = int(
        sql(
            "SELECT public.pglc_test_recreate_relation_state("
            f"'{relation}'::regclass, {sql_literal(namespace)})"
        )
    )
    assert new_warm_incarnation != old_warm_incarnation
    warm_client = RespConnection()
    try:
        before_reject = read_cache_stats()
        assert mget_one(warm_client, warmed_key) == row_bytes(
            warmed_id, warmed_value
        )
        after_reject = read_cache_stats()
        assert after_reject["database_reads"] >= before_reject["database_reads"] + 1
        assert after_reject["cache_hits"] == before_reject["cache_hits"]
        assert mget_one(warm_client, warmed_key) == row_bytes(
            warmed_id, warmed_value
        )
        assert read_cache_stats()["database_reads"] == after_reject["database_reads"]
    finally:
        warm_client.close()


def test_key_fill_hit_ratio_under_update_load(table: str) -> None:
    allocated_ids = allocate_test_row_ids(table, 16)
    read_ids = allocated_ids[:8]
    write_ids = allocated_ids[8:]
    sql(
        f"INSERT INTO public.{sql_identifier(table)} (id, value) VALUES "
        + ", ".join(
            f"({row_id}, 'fixed-{row_id}')" for row_id in read_ids
        )
        + ", "
        + ", ".join(
            f"({row_id}, 'writer-{row_id}')" for row_id in write_ids
        )
    )
    keys = [crud_key(table, row_id) for row_id in read_ids]
    expected = [row_bytes(row_id, f"fixed-{row_id}") for row_id in read_ids]
    client = RespConnection(socket_timeout=45)
    stop = threading.Event()
    writer_errors: list[str] = []
    writer_updates = [0]

    def update_other_keys() -> None:
        environment = os.environ.copy()
        environment["PGAPPNAME"] = f"pglc_load_writer_{os.getpid()}"
        process = subprocess.Popen(
            psql_base_args(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
        )
        assert process.stdin is not None
        try:
            sequence = 0
            while not stop.is_set() and process.poll() is None:
                row_id = write_ids[sequence % len(write_ids)]
                process.stdin.write(
                    f"UPDATE public.{sql_identifier(table)} "
                    f"SET value = 'write-{sequence}' WHERE id = {row_id};\n"
                )
                process.stdin.flush()
                writer_updates[0] += 1
                sequence += 1
        except BaseException as error:
            writer_errors.append(str(error))
        finally:
            process.stdin.close()
            process.stdin = None
            output = process.communicate(timeout=15)[0]
            if process.returncode != 0:
                writer_errors.append(output)

    before = read_cache_stats()
    writer = threading.Thread(target=update_other_keys, name="pglc-update-load")
    try:
        writer.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            assert client.command("MGET", *keys) == expected
        stop.set()
        writer.join(timeout=20)
        assert not writer.is_alive(), "background UPDATE writer did not stop"
        assert not writer_errors, writer_errors
        assert writer_updates[0] > 0
        after_load = read_cache_stats()
        hits = int(after_load["cache_hits"]) - int(before["cache_hits"])
        misses = int(after_load["cache_misses"]) - int(before["cache_misses"])
        assert hits + misses > 0
        assert hits / (hits + misses) > 0.9, (hits, misses)
        before_final_hit = read_cache_stats()
        assert client.command("MGET", *keys) == expected
        after_final_hit = read_cache_stats()
        assert after_final_hit["cache_hits"] - before_final_hit["cache_hits"] >= len(keys)
        assert after_final_hit["database_reads"] == before_final_hit["database_reads"]
    finally:
        stop.set()
        if writer.is_alive():
            writer.join(timeout=20)
        client.close()


def test_preprepare_still_rejected(table: str) -> None:
    command = (
        f"BEGIN; UPDATE public.{sql_identifier(table)} "
        "SET value = value WHERE id = 1; PREPARE TRANSACTION 'pglc_test';"
    )
    result = run_psql(
        psql_base_args() + ["-c", command], statement=command, timeout=20
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "PREPARE TRANSACTION is not supported" in output, output


def wait_for_cache_enabled(expected: bool) -> None:
    deadline = time.monotonic() + 10
    expected_text = "true" if expected else "false"
    while True:
        if sql("SELECT local_cache.health() ->> 'cache_enabled'") == expected_text:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"cache_enabled did not become {expected_text}")
        time.sleep(0.05)


def test_enabled_kill_switch(table: str) -> None:
    client = RespConnection()
    key = crud_key(table, 1)
    surviving_key = crud_key(table, 2)
    inserted_key = crud_key(table, 6)
    cache_off = False
    try:
        assert sql("SELECT local_cache.health() ->> 'cache_enabled'") == "true"
        ready_before = sql("SELECT local_cache.health() ->> 'ready'")
        current_value = sql(f"SELECT value FROM public.{table} WHERE id = 1")
        expected = row_bytes(1, current_value)
        assert mget_one(client, key) == expected
        surviving_expected = row_bytes(2, "x" * BACKPRESSURE_VALUE_BYTES)
        assert mget_one(client, surviving_key) == surviving_expected

        sql_commands(
            "ALTER SYSTEM SET pg_local_cache.enabled = off",
            "SELECT pg_reload_conf()",
        )
        cache_off = True
        wait_for_cache_enabled(False)
        assert sql("SELECT local_cache.health() ->> 'ready'") == ready_before

        before_disabled_reads = json.loads(client.command("STAT"))
        for _ in range(8):
            assert mget_one(client, key) == expected
        missing_key = crud_key(table, 99999999)
        for _ in range(3):
            assert mget_one(client, missing_key) is None
        assert client.command("MGET", missing_key, missing_key) == [None, None]
        after_disabled_reads = json.loads(client.command("STAT"))
        assert after_disabled_reads["cache_hits"] == before_disabled_reads["cache_hits"]
        assert after_disabled_reads["cache_misses"] == before_disabled_reads["cache_misses"]
        assert after_disabled_reads["negative_hits"] == before_disabled_reads["negative_hits"]
        assert (
            after_disabled_reads["singleflight_leaders"]
            == before_disabled_reads["singleflight_leaders"]
        )
        assert (
            after_disabled_reads["singleflight_waiters"]
            == before_disabled_reads["singleflight_waiters"]
        )
        assert (
            after_disabled_reads["database_reads"]
            - before_disabled_reads["database_reads"]
            == 12
        )

        rewritten_value = row_bytes(1, "updated-while-cache-disabled")
        assert client.command("SET", key, rewritten_value) == "OK"
        assert mget_one(client, key) == rewritten_value
        inserted_value = row_bytes(6, "written-while-cache-disabled").decode()
        assert client.command("SET", inserted_key, inserted_value) == "OK"
        expected_inserted = row_bytes(6, "written-while-cache-disabled")
        assert mget_one(client, inserted_key) == expected_inserted
        before_enable = json.loads(client.command("STAT"))

        sql_commands(
            "ALTER SYSTEM SET pg_local_cache.enabled = on",
            "SELECT pg_reload_conf()",
        )
        wait_for_cache_enabled(True)
        after_enable = json.loads(client.command("STAT"))
        assert after_enable["invalidations"] > before_enable["invalidations"]
        assert after_enable["cache_hits"] == before_enable["cache_hits"]
        assert client.command("MGET", missing_key, missing_key) == [None, None]
        after_bypass_refill = json.loads(client.command("STAT"))
        assert after_bypass_refill["singleflight_leaders"] == (
            after_enable["singleflight_leaders"] + 1
        )
        assert after_bypass_refill["database_reads"] == (
            after_enable["database_reads"] + 1
        )
        assert mget_one(client, key) == rewritten_value
        assert mget_one(client, inserted_key) == expected_inserted
        assert mget_one(client, surviving_key) == surviving_expected
        after_reenabled_reads = json.loads(client.command("STAT"))
        assert after_reenabled_reads["cache_hits"] == after_enable["cache_hits"]
        assert mget_one(client, surviving_key) == surviving_expected
        assert (
            json.loads(client.command("STAT"))["cache_hits"]
            == after_reenabled_reads["cache_hits"] + 1
        )
    finally:
        if cache_off:
            sql_commands(
                "ALTER SYSTEM RESET pg_local_cache.enabled",
                "SELECT pg_reload_conf()",
            )
            wait_for_cache_enabled(True)
        client.close()


PAUSE_HOOK_TEST_NAMES = (
    "test_partition_routing_and_opposite_order_writers",
    "test_unrelated_key_fill_survives_keyed_write",
    "test_same_key_relation_and_global_fences",
    "test_warm_hit_snapshot_and_copy_fences",
    "test_relation_global_claim_store_fences",
    "test_marker_exhaustion_falls_back_safely",
    "test_dirty_key_dedup_and_relation_fallback",
    "test_namespace_invalidation_preserves_other_scope",
    "test_overlapping_publishers_on_one_key",
    "test_abort_after_dirty_publication",
    "test_relation_incarnation_forget_recreate",
    "test_reload_claim_keeps_truncated_payload_invalid",
    "test_refresh_two_writer_orders",
    "test_refresh_fallbacks_and_slots",
)


def run_pause_hook_test(name: str, callback: Callable[[], None]) -> None:
    global _TEST_PAUSE_STARTED_AT, _TEST_PAUSE_POINTS, _TEST_PAUSE_BEFORE_STATS
    _TEST_PAUSE_RECORDS.clear()
    _TEST_PAUSE_STARTED_AT = None
    _TEST_PAUSE_POINTS = []
    _TEST_PAUSE_BEFORE_STATS = None
    try:
        callback()
    except AssertionError as error:
        try:
            current_stats: object = read_cache_stats()
        except Exception as stats_error:
            current_stats = f"stats unavailable: {stats_error!r}"
        diagnostics = {
            "test": name,
            "pause_intervals": list(_TEST_PAUSE_RECORDS),
            "current_stats": current_stats,
        }
        raise AssertionError(
            f"{error}\nPause diagnostics: "
            f"{json.dumps(diagnostics, sort_keys=True)}"
        ) from error


def run_pause_hook_tests(
    table: str,
    namespace: str,
    scoped_table: str,
    truncate_table: str,
    incarnation_table: str,
    incarnation_namespace: str,
    refresh_table: str,
    refresh_namespace: str,
    barrier_table: str,
    second_barrier_table: str,
) -> None:
    cases: tuple[tuple[str, Callable[[], None]], ...] = (
        (
            "test_partition_routing_and_opposite_order_writers",
            lambda: test_partition_routing_and_opposite_order_writers(
                table, namespace, barrier_table, second_barrier_table
            ),
        ),
        (
            "test_unrelated_key_fill_survives_keyed_write",
            lambda: test_unrelated_key_fill_survives_keyed_write(
                table, barrier_table
            ),
        ),
        (
            "test_same_key_relation_and_global_fences",
            lambda: test_same_key_relation_and_global_fences(
                table, namespace, barrier_table
            ),
        ),
        (
            "test_warm_hit_snapshot_and_copy_fences",
            lambda: test_warm_hit_snapshot_and_copy_fences(
                table, namespace, barrier_table, second_barrier_table
            ),
        ),
        (
            "test_relation_global_claim_store_fences",
            lambda: test_relation_global_claim_store_fences(
                table, namespace, barrier_table, second_barrier_table
            ),
        ),
        (
            "test_marker_exhaustion_falls_back_safely",
            lambda: test_marker_exhaustion_falls_back_safely(
                table, namespace, barrier_table, second_barrier_table
            ),
        ),
        (
            "test_dirty_key_dedup_and_relation_fallback",
            lambda: test_dirty_key_dedup_and_relation_fallback(
                table, namespace, barrier_table
            ),
        ),
        (
            "test_namespace_invalidation_preserves_other_scope",
            lambda: test_namespace_invalidation_preserves_other_scope(
                table, namespace, scoped_table, barrier_table
            ),
        ),
        (
            "test_overlapping_publishers_on_one_key",
            lambda: test_overlapping_publishers_on_one_key(
                table, namespace, barrier_table, second_barrier_table
            ),
        ),
        (
            "test_abort_after_dirty_publication",
            lambda: test_abort_after_dirty_publication(
                table, namespace, barrier_table
            ),
        ),
        (
            "test_relation_incarnation_forget_recreate",
            lambda: test_relation_incarnation_forget_recreate(
                incarnation_table, incarnation_namespace, barrier_table
            ),
        ),
        (
            "test_reload_claim_keeps_truncated_payload_invalid",
            lambda: test_reload_claim_keeps_truncated_payload_invalid(
                truncate_table, barrier_table
            ),
        ),
        (
            "test_refresh_two_writer_orders",
            lambda: test_refresh_two_writer_orders(
                refresh_table, refresh_namespace, barrier_table,
                second_barrier_table,
            ),
        ),
        (
            "test_refresh_fallbacks_and_slots",
            lambda: test_refresh_fallbacks_and_slots(
                refresh_table, refresh_namespace, barrier_table
            ),
        ),
    )
    for name, callback in cases:
        run_pause_hook_test(name, callback)

def refresh_row(
    row_id: int, value: str, *, flag: bool = False, small: int = 1, **extra: object
) -> dict[str, object]:
    return {
        "id": row_id,
        "flag": flag,
        "small": small,
        "value": value,
        "extra": None,
        **extra,
    }


def insert_refresh_rows(table: str, rows: list[tuple[int, bool, int, str]]) -> None:
    values = ", ".join(
        f"({row_id}, {'true' if flag else 'false'}, {small}, {sql_literal(value)})"
        for row_id, flag, small, value in rows
    )
    sql(
        f"INSERT INTO public.{sql_identifier(table)} (id, flag, small, value) "
        f"VALUES {values}"
    )


def refresh_lookup(
    client: RespConnection,
    key: str,
    expected: dict[str, object] | None,
    *,
    expected_cache_path: str | None = None,
) -> None:
    before = json.loads(client.command("STAT"))
    response = mget_one(client, key)
    if expected is None:
        assert response is None, (key, response)
    else:
        assert isinstance(response, bytes), (key, response)
        actual = json.loads(response)
        assert actual == expected, (key, actual, expected)
    after = json.loads(client.command("STAT"))
    if expected_cache_path == "hit":
        assert after["cache_hits"] - before["cache_hits"] == 1, (before, after)
        assert after["cache_misses"] == before["cache_misses"], (before, after)
        assert after["database_reads"] == before["database_reads"], (before, after)
    elif expected_cache_path == "miss":
        assert after["cache_misses"] - before["cache_misses"] == 1, (before, after)
        assert after["database_reads"] - before["database_reads"] == 1, (before, after)
    elif expected_cache_path == "negative-hit":
        assert after["negative_hits"] - before["negative_hits"] == 1, (before, after)
        assert after["database_reads"] == before["database_reads"], (before, after)


def set_refresh_write_mode(table: str, mode: str = "refresh") -> dict[str, object]:
    result = json.loads(
        sql(
            "SELECT local_cache.set_write_mode("
            f"'public.{sql_identifier(table)}'::regclass, {sql_literal(mode)})::text"
        )
    )
    assert result["write_mode_requested"] == mode, result
    return result


def wait_for_application_pid(application_name: str, *, timeout: float = 10) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = sql(
            "SELECT pid FROM pg_catalog.pg_stat_activity "
            f"WHERE application_name = {sql_literal(application_name)} "
            "AND state = 'idle in transaction' LIMIT 1"
        )
        if pid:
            return int(pid)
        time.sleep(0.01)
    raise AssertionError(f"writer did not become idle in transaction: {application_name}")


def start_writer_commit(process: subprocess.Popen[str]) -> None:
    write_psql_input(process, "COMMIT;\n")
    close_psql_input(process)


def finish_started_writer(process: subprocess.Popen[str]) -> str:
    output = _finish_tracked_psql(process)
    assert process.returncode == 0, warm_hit_diagnostics(
        {"returncode": process.returncode, "output": output}
    )
    return output


def test_refresh_basic_semantics(table: str, namespace: str) -> None:
    mode = set_refresh_write_mode(table)
    assert mode["write_mode_effective"] == "refresh", mode
    client = RespConnection()
    (
        row_id,
        miss_id,
        rollback_id,
        nested_id,
        recursive_id,
        deferred_id,
        repeated_id,
        subabort_second_id,
        insert_delete_id,
    ) = allocate_test_row_ids(table, 9)
    recursive_function = f"pglc_refresh_recursive_{os.getpid()}"
    deferred_function = f"pglc_refresh_deferred_{os.getpid()}"
    recursive_trigger = f"a_refresh_recursive_t_{os.getpid()}"
    deferred_trigger = f"pglc_refresh_deferred_t_{os.getpid()}"
    try:
        insert_refresh_rows(
            table,
            [
                (row_id, False, 1, "before-hit"),
                (miss_id, False, 2, "before-miss"),
                (rollback_id, False, 3, "rollback-base"),
                (nested_id, False, 4, "nested-base"),
                (recursive_id, False, 5, "recursive-base"),
                (deferred_id, False, 6, "deferred-base"),
                (repeated_id, False, 7, "repeated-base"),
                (subabort_second_id, False, 8, "second-key-base"),
            ],
        )
        # Start the write-allocation check with exactly the row we warm below
        # resident; refresh-mode INSERT may itself install captured rows.
        sql(f"SELECT local_cache.invalidate({sql_literal(namespace)})")
        hit_key = crud_key(table, row_id)
        miss_key = crud_key(table, miss_id)
        assert isinstance(mget_one(client, hit_key), bytes)
        sql(
            f"UPDATE public.{sql_identifier(table)} "
            f"SET flag = true, small = 9, value = 'after-hit' WHERE id = {row_id}"
        )
        refresh_lookup(
            client,
            hit_key,
            refresh_row(row_id, "after-hit", flag=True, small=9),
            expected_cache_path="hit",
        )

        # Refresh write-allocates a cold key from its captured committed tuple.
        sql(
            f"UPDATE public.{sql_identifier(table)} SET value = 'after-miss' "
            f"WHERE id = {miss_id}"
        )
        refresh_lookup(
            client,
            miss_key,
            refresh_row(miss_id, "after-miss", small=2),
            expected_cache_path="hit",
        )

        insert_delete_key = crud_key(table, insert_delete_id)
        sql(f"SELECT local_cache.invalidate({sql_literal(namespace)})")
        insert_refresh_rows(
            table, [(insert_delete_id, False, 13, "insert-refresh")]
        )
        refresh_lookup(
            client,
            insert_delete_key,
            refresh_row(insert_delete_id, "insert-refresh", small=13),
            expected_cache_path="hit",
        )
        sql(
            f"DELETE FROM public.{sql_identifier(table)} "
            f"WHERE id = {insert_delete_id}"
        )
        refresh_lookup(
            client,
            insert_delete_key,
            None,
            expected_cache_path="negative-hit",
        )

        rollback_key = crud_key(table, rollback_id)
        assert isinstance(mget_one(client, rollback_key), bytes)
        rollback_writer = start_idle_writer(
            table,
            "rolled-back-refresh",
            application_name=f"pglc_refresh_abort_{os.getpid()}",
            row_id=rollback_id,
        )
        finish_writer(rollback_writer, commit=False)
        refresh_lookup(
            client,
            rollback_key,
            refresh_row(rollback_id, "rollback-base", small=3),
            expected_cache_path="hit",
        )

        duplicate_key = crud_key(table, rollback_id)
        sql(
            "BEGIN; SELECT local_cache._test_duplicate_refresh_finish(); "
            f"UPDATE public.{sql_identifier(table)} SET value = 'duplicate-finish' "
            f"WHERE id = {rollback_id}; COMMIT"
        )
        refresh_lookup(
            client,
            duplicate_key,
            refresh_row(rollback_id, "duplicate-finish", small=3),
            expected_cache_path="hit",
        )

        # Rolled-back subtransactions discard their capture; RELEASE preserves
        # the surviving child capture through top-level commit.
        nested_key = crud_key(table, nested_id)
        subabort_second_key = crud_key(table, subabort_second_id)
        assert isinstance(mget_one(client, nested_key), bytes)
        assert isinstance(mget_one(client, subabort_second_key), bytes)
        sql(
            f"BEGIN; SAVEPOINT refresh_abort; "
            f"UPDATE public.{sql_identifier(table)} SET value = 'subabort' "
            f"WHERE id = {nested_id}; ROLLBACK TO refresh_abort; "
            "RELEASE refresh_abort; "
            f"UPDATE public.{sql_identifier(table)} "
            f"SET value = 'second-key-final' WHERE id = {subabort_second_id}; COMMIT"
        )
        refresh_lookup(
            client,
            nested_key,
            refresh_row(nested_id, "nested-base", small=4),
            expected_cache_path="miss",
        )
        refresh_lookup(
            client,
            subabort_second_key,
            refresh_row(subabort_second_id, "second-key-final", small=8),
            expected_cache_path="miss",
        )
        sql(
            f"BEGIN; SAVEPOINT refresh_release; "
            f"UPDATE public.{sql_identifier(table)} SET value = 'released-final' "
            f"WHERE id = {nested_id}; RELEASE refresh_release; COMMIT"
        )
        refresh_lookup(
            client,
            nested_key,
            refresh_row(nested_id, "released-final", small=4),
            expected_cache_path="hit",
        )

        recursive_key = crud_key(table, recursive_id)
        assert isinstance(mget_one(client, recursive_key), bytes)
        sql(
            f"CREATE FUNCTION public.{sql_identifier(recursive_function)}() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            f"IF NEW.id = {recursive_id} AND NEW.value = 'recursive-outer' THEN "
            f"UPDATE public.{sql_identifier(table)} SET value = 'recursive-inner' "
            f"WHERE id = {recursive_id}; END IF; RETURN NEW; END $$; "
            f"CREATE TRIGGER {sql_identifier(recursive_trigger)} AFTER UPDATE OF value "
            f"ON public.{sql_identifier(table)} FOR EACH ROW EXECUTE FUNCTION "
            f"public.{sql_identifier(recursive_function)}()"
        )
        sql(
            f"UPDATE public.{sql_identifier(table)} SET value = 'recursive-outer' "
            f"WHERE id = {recursive_id}"
        )
        refresh_lookup(
            client,
            recursive_key,
            refresh_row(recursive_id, "recursive-inner", small=5),
            expected_cache_path="miss",
        )

        deferred_key = crud_key(table, deferred_id)
        assert isinstance(mget_one(client, deferred_key), bytes)
        sql(
            f"CREATE FUNCTION public.{sql_identifier(deferred_function)}() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            f"IF NEW.id = {deferred_id} AND NEW.value = 'deferred-requested' THEN "
            f"UPDATE public.{sql_identifier(table)} SET value = 'deferred-final' "
            f"WHERE id = {deferred_id}; END IF; RETURN NULL; END $$; "
            f"CREATE CONSTRAINT TRIGGER {sql_identifier(deferred_trigger)} "
            f"AFTER UPDATE ON public.{sql_identifier(table)} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "
            f"public.{sql_identifier(deferred_function)}()"
        )
        sql(
            f"UPDATE public.{sql_identifier(table)} SET value = 'deferred-requested' "
            f"WHERE id = {deferred_id}"
        )
        refresh_lookup(
            client,
            deferred_key,
            refresh_row(deferred_id, "deferred-final", small=7),
            expected_cache_path="miss",
        )

        repeated_key = crud_key(table, repeated_id)
        assert isinstance(mget_one(client, repeated_key), bytes)
        sql(
            f"BEGIN; UPDATE public.{sql_identifier(table)} SET value = 'repeat-v1' "
            f"WHERE id = {repeated_id}; UPDATE public.{sql_identifier(table)} "
            f"SET value = 'repeat-v2' WHERE id = {repeated_id}; COMMIT"
        )
        refresh_lookup(
            client,
            repeated_key,
            refresh_row(repeated_id, "repeat-v2", small=7),
            expected_cache_path="miss",
        )

        # Cross hash-recreate threshold with retained payloads. Under ASan,
        # reset must free payloads before removing/destroying their HTAB entries.
        wide_ids = allocate_test_row_ids(table, 257)
        before_wide = read_cache_stats()
        insert_refresh_rows(
            table,
            [
                (wide_id, False, 13, f"wide-refresh-{index}")
                for index, wide_id in enumerate(wide_ids)
            ],
        )
        after_wide = read_cache_stats()
        assert after_wide["refresh_installs_total"] >= (
            before_wide["refresh_installs_total"] + len(wide_ids)
        ), (before_wide, after_wide)
        assert after_wide["refresh_reservations_outstanding"] == 0, after_wide
        for offset in range(0, len(wide_ids), 32):
            batch_ids = wide_ids[offset : offset + 32]
            responses = client.command(
                "MGET", *(crud_key(table, wide_id) for wide_id in batch_ids)
            )
            assert len(responses) == len(batch_ids)
            for wide_id, response in zip(batch_ids, responses, strict=True):
                assert isinstance(response, bytes)
                assert json.loads(response)["id"] == wide_id
                assert json.loads(response)["value"] == (
                    f"wide-refresh-{wide_ids.index(wide_id)}"
                )

        # Small TOAST succeeds; payload and aggregate raw-row bounds invalidate.
        (
            toast_id,
            payload_oversize_id,
            raw_below_id,
            raw_above_id,
            aggregate_id,
        ) = allocate_test_row_ids(table, 5)
        small_toast = "".join(chr(33 + (index * 37) % 90) for index in range(7000))
        payload_oversize = "".join(
            chr(33 + (index * 37) % 90) for index in range(9000)
        )
        # id/flag/small plus the text varlena header consume 15 bytes, so
        # these rows land one byte below and one byte above the 64 KiB raw
        # whole-row boundary before any JSON rendering.
        raw_below = "l" * 65_520
        raw_above = "r" * 65_522
        insert_refresh_rows(
            table,
            [
                (toast_id, False, 8, "toast-base"),
                (payload_oversize_id, False, 9, "payload-base"),
                (raw_below_id, False, 10, "raw-below-base"),
                (raw_above_id, False, 11, "raw-above-base"),
                (aggregate_id, False, 12, "aggregate-base"),
            ],
        )
        toast_key = crud_key(table, toast_id)
        payload_oversize_key = crud_key(table, payload_oversize_id)
        raw_below_key = crud_key(table, raw_below_id)
        raw_above_key = crud_key(table, raw_above_id)
        aggregate_key = crud_key(table, aggregate_id)
        assert isinstance(mget_one(client, toast_key), bytes)
        assert isinstance(mget_one(client, payload_oversize_key), bytes)
        assert isinstance(mget_one(client, raw_below_key), bytes)
        assert isinstance(mget_one(client, raw_above_key), bytes)
        assert isinstance(mget_one(client, aggregate_key), bytes)
        sql(
            f"UPDATE public.{sql_identifier(table)} SET value = {sql_literal(small_toast)} "
            f"WHERE id = {toast_id}"
        )
        sql(
            f"UPDATE public.{sql_identifier(table)} SET value = "
            f"{sql_literal(payload_oversize)} WHERE id = {payload_oversize_id}"
        )
        sql(
            f"UPDATE public.{sql_identifier(table)} SET value = "
            f"{sql_literal(raw_below)} WHERE id = {raw_below_id}"
        )
        sql(
            f"UPDATE public.{sql_identifier(table)} SET value = "
            f"{sql_literal(raw_above)} WHERE id = {raw_above_id}"
        )
        sql(
            f"UPDATE public.{sql_identifier(table)} SET value = repeat('a', 40000), "
            f"extra = repeat('b', 30000) WHERE id = {aggregate_id}"
        )
        refresh_lookup(
            client,
            toast_key,
            refresh_row(toast_id, small_toast, small=8),
            expected_cache_path="hit",
        )
        refresh_lookup(
            client,
            payload_oversize_key,
            refresh_row(payload_oversize_id, payload_oversize, small=9),
            expected_cache_path="miss",
        )
        refresh_lookup(
            client,
            raw_below_key,
            refresh_row(raw_below_id, raw_below, small=10),
            expected_cache_path="miss",
        )
        refresh_lookup(
            client,
            raw_above_key,
            refresh_row(raw_above_id, raw_above, small=11),
            expected_cache_path="miss",
        )
        refresh_lookup(
            client,
            aggregate_key,
            refresh_row(
                aggregate_id,
                "a" * 40_000,
                small=12,
                extra="b" * 30_000,
            ),
            expected_cache_path="miss",
        )

        # Many individually refreshable rows exhaust the transaction's 4 MiB
        # capture budget. Later rows stay invalidated, and retained bytes drain.
        capture_ids = allocate_test_row_ids(table, 600)
        insert_refresh_rows(
            table,
            [
                (row_id, False, 12, f"capture-base-{index}")
                for index, row_id in enumerate(capture_ids)
            ],
        )
        capture_value = "".join(
            chr(33 + (index * 37) % 90) for index in range(7000)
        )
        before_capture_limit = read_cache_stats()
        sql(
            f"UPDATE public.{sql_identifier(table)} SET value = "
            f"{sql_literal(capture_value)} WHERE id BETWEEN "
            f"{capture_ids[0]} AND {capture_ids[-1]}"
        )
        after_capture_limit = read_cache_stats()
        assert (
            after_capture_limit["refresh_skips_capture_limit_total"]
            > before_capture_limit["refresh_skips_capture_limit_total"]
        ), (before_capture_limit, after_capture_limit)
        assert after_capture_limit["refresh_capture_bytes_current"] == 0, (
            after_capture_limit
        )
        assert after_capture_limit["refresh_capture_bytes_highwater"] <= 4 * 1024 * 1024, (
            after_capture_limit
        )
        capture_keys = [crud_key(table, row_id) for row_id in capture_ids]
        for offset in range(0, len(capture_keys), 32):
            batch_keys = capture_keys[offset:offset + 32]
            batch_values = client.command("MGET", *batch_keys)
            assert len(batch_values) == len(batch_keys)
            for response in batch_values:
                assert isinstance(response, bytes)
                assert json.loads(response)["value"] == capture_value

        stats = read_cache_stats()
        assert int(stats["refresh_capture_bytes_current"]) == 0, stats
        assert int(stats["refresh_capture_bytes_highwater"]) > 0, stats
    finally:
        client.close()
        sql(
            f"DROP TRIGGER IF EXISTS {sql_identifier(recursive_trigger)} "
            f"ON public.{sql_identifier(table)}; "
            f"DROP TRIGGER IF EXISTS {sql_identifier(deferred_trigger)} "
            f"ON public.{sql_identifier(table)}; "
            f"DROP FUNCTION IF EXISTS public.{sql_identifier(recursive_function)}(); "
            f"DROP FUNCTION IF EXISTS public.{sql_identifier(deferred_function)}()"
        )


def test_refresh_two_writer_orders(
    table: str,
    namespace: str,
    barrier_table: str,
    second_barrier_table: str,
) -> None:
    """Choose both callback orders for two publications of one cache key."""
    relation = f"public.{sql_identifier(table)}"
    for iteration, (order, warm_initial) in enumerate(
        ((("A", "B"), True), (("B", "A"), False)), start=1
    ):
        row_id = allocate_test_row_ids(table)[0]
        insert_refresh_rows(
            table,
            [(row_id, False, 20 + iteration, f"order-{iteration}-a0")],
        )
        sql(f"SELECT local_cache.invalidate({sql_literal(namespace)})")
        client = RespConnection(socket_timeout=45)
        key = crud_key(table, row_id)
        locker_a: subprocess.Popen[str] | None = None
        locker_b: subprocess.Popen[str] | None = None
        writer_a: subprocess.Popen[str] | None = None
        writer_b: subprocess.Popen[str] | None = None
        try:
            if warm_initial:
                assert isinstance(mget_one(client, key), bytes)
            before_finishes = read_cache_stats()

            locker_a = start_table_locker(
                barrier_table,
                application_name=f"pglc_refresh_barrier_a_{os.getpid()}_{iteration}",
            )
            locker_b = start_table_locker(
                second_barrier_table,
                application_name=f"pglc_refresh_barrier_b_{os.getpid()}_{iteration}",
            )
            name_a = f"pglc_refresh_writer_a_{os.getpid()}_{iteration}"
            writer_a = start_idle_writer(
                table,
                f"order-{iteration}-a1",
                application_name=name_a,
                row_id=row_id,
            )
            pid_a = wait_for_application_pid(name_a)
            set_test_pause(
                "before_refresh_finish", barrier_table, pause_pid=pid_a
            )
            start_writer_commit(writer_a)
            wait_for_blocked_relation_pid(
                barrier_table,
                application_name=name_a,
                psql_process=writer_a,
                timeout=10,
            )
            refresh_lookup(
                client,
                key,
                refresh_row(row_id, f"order-{iteration}-a1", small=20 + iteration),
                expected_cache_path="miss",
            )

            name_b = f"pglc_refresh_writer_b_{os.getpid()}_{iteration}"
            writer_b = start_idle_writer(
                table,
                f"order-{iteration}-b1",
                application_name=name_b,
                row_id=row_id,
            )
            pid_b = wait_for_application_pid(name_b)
            set_test_pause(
                "before_refresh_finish", second_barrier_table, pause_pid=pid_b
            )
            start_writer_commit(writer_b)
            wait_for_blocked_relation_pid(
                second_barrier_table,
                application_name=name_b,
                psql_process=writer_b,
                timeout=10,
            )
            refresh_lookup(
                client,
                key,
                refresh_row(row_id, f"order-{iteration}-b1", small=20 + iteration),
                expected_cache_path="miss",
            )

            # Both sessions have published the same key. The active R for each
            # writer keeps the key unservable until both callbacks finish.
            set_test_pause(None)
            writers = {"A": writer_a, "B": writer_b}
            lockers = {"A": locker_a, "B": locker_b}
            for label in order:
                locker = lockers[label]
                assert locker is not None
                finish_writer(locker, commit=True)
                lockers[label] = None
                writer = writers[label]
                assert writer is not None
                finish_started_writer(writer)
                writers[label] = None
                if label == order[0]:
                    refresh_lookup(
                        client,
                        key,
                        refresh_row(
                            row_id, f"order-{iteration}-b1", small=20 + iteration
                        ),
                        expected_cache_path="miss",
                    )
            locker_a = locker_b = writer_a = writer_b = None
            after_finishes = read_cache_stats()
            expected_installs = 1 if order == ("A", "B") else 0
            assert after_finishes["refresh_installs_total"] == (
                before_finishes["refresh_installs_total"] + expected_installs
            ), (before_finishes, after_finishes, order)
            assert after_finishes["refresh_reservations_outstanding"] == 0, (
                after_finishes
            )
            assert int(
                sql(
                    "SELECT public.pglc_test_relation_identity_pins("
                    f"'{relation}'::regclass, {sql_literal(namespace)})"
                )
            ) == 0
            refresh_lookup(
                client,
                key,
                refresh_row(row_id, f"order-{iteration}-b1", small=20 + iteration),
                expected_cache_path="hit" if expected_installs else "miss",
            )
        finally:
            set_test_pause(None)
            for process in (locker_a, locker_b):
                if process is not None:
                    finish_writer(process, commit=True)
            for process in (writer_a, writer_b):
                terminate_writer(process)
            client.close()

    # A later same-key transaction that aborts never publishes a newer S.
    row_id = allocate_test_row_ids(table)[0]
    insert_refresh_rows(table, [(row_id, False, 39, "abort-order-base")])
    sql(f"SELECT local_cache.invalidate({sql_literal(namespace)})")
    client = RespConnection(socket_timeout=45)
    key = crud_key(table, row_id)
    locker: subprocess.Popen[str] | None = start_table_locker(
        barrier_table,
        application_name=f"pglc_refresh_abort_barrier_{os.getpid()}",
    )
    writer_a: subprocess.Popen[str] | None = None
    writer_b: subprocess.Popen[str] | None = None
    try:
        assert isinstance(mget_one(client, key), bytes)
        writer_a = start_idle_writer(
            table,
            "abort-order-a",
            application_name=f"pglc_refresh_abort_a_{os.getpid()}",
            row_id=row_id,
        )
        pid_a = wait_for_application_pid(f"pglc_refresh_abort_a_{os.getpid()}")
        set_test_pause("before_refresh_finish", barrier_table, pause_pid=pid_a)
        start_writer_commit(writer_a)
        wait_for_blocked_relation_pid(
            barrier_table,
            application_name=f"pglc_refresh_abort_a_{os.getpid()}",
            psql_process=writer_a,
            timeout=10,
        )
        before_aborted_successor = read_cache_stats()
        set_test_pause("after_publish_abort", barrier_table)
        successor_command = (
            "BEGIN; SELECT local_cache._test_collect_key("
            f"'{relation}'::regclass::oid, {sql_literal(namespace)}, "
            f"{sql_literal(canonical_int8_key(row_id))}); COMMIT"
        )
        successor_result = run_psql(
            psql_base_args() + ["-c", successor_command],
            statement=successor_command,
            timeout=20,
        )
        assert successor_result.returncode != 0, successor_result.stdout
        assert "test abort after dirty publication" in successor_result.stderr
        refresh_lookup(
            client,
            key,
            refresh_row(row_id, "abort-order-a", small=39),
            expected_cache_path="miss",
        )
        set_test_pause(None)
        finish_writer(locker, commit=True)
        locker = None
        finish_started_writer(writer_a)
        writer_a = None
        after_aborted_successor = read_cache_stats()
        assert after_aborted_successor["refresh_skips_stale_publication_total"] >= (
            before_aborted_successor["refresh_skips_stale_publication_total"] + 1
        ), (before_aborted_successor, after_aborted_successor)
        refresh_lookup(
            client,
            key,
            refresh_row(row_id, "abort-order-a", small=39),
            expected_cache_path="miss",
        )
        drained = read_cache_stats()
        assert drained["refresh_reservations_outstanding"] == 0, drained
        assert int(
            sql(
                "SELECT public.pglc_test_relation_identity_pins("
                f"'{relation}'::regclass, {sql_literal(namespace)})"
            )
        ) == 0
    finally:
        set_test_pause(None)
        if locker is not None:
            finish_writer(locker, commit=True)
        terminate_writer(writer_a)
        terminate_writer(writer_b)
        client.close()


def test_refresh_fallbacks_and_slots(
    table: str,
    namespace: str,
    barrier_table: str,
) -> None:
    relation = f"public.{sql_identifier(table)}"
    client = RespConnection(socket_timeout=45)
    try:
        test_abort_after_dirty_publication(table, namespace, barrier_table)

        # Capture allocation failure and admission exhaustion both degrade to
        # invalidation. Verify first post-commit observation is a database read.
        capture_id, admission_id, crc_id, boundary_id, removed_id = (
            allocate_test_row_ids(table, 5)
        )
        insert_refresh_rows(
            table,
            [
                (capture_id, False, 41, "capture-base"),
                (admission_id, False, 42, "admission-base"),
                (crc_id, False, 43, "crc-base"),
                (boundary_id, False, 44, "boundary-base"),
                (removed_id, False, 45, "removed-base"),
            ],
        )
        ids = (capture_id, admission_id, crc_id, boundary_id, removed_id)
        for row_id in ids:
            assert isinstance(mget_one(client, crud_key(table, row_id)), bytes)

        sql(
            "BEGIN; SELECT local_cache._test_refresh_capture_fail(); "
            f"UPDATE {relation} SET value = 'capture-failed' WHERE id = {capture_id}; "
            "COMMIT"
        )
        refresh_lookup(
            client,
            crud_key(table, capture_id),
            refresh_row(capture_id, "capture-failed", small=41),
            expected_cache_path="miss",
        )

        admission_guc = "pg_local_cache.test_refresh_admission_fail"
        previous_admission_fail = guc_number(admission_guc)
        sql(
            f"BEGIN; SET LOCAL {admission_guc} = 1; "
            f"UPDATE {relation} SET value = 'admission-failed' "
            f"WHERE id = {admission_id}; COMMIT"
        )
        assert guc_number(admission_guc) == previous_admission_fail
        refresh_lookup(
            client,
            crud_key(table, admission_id),
            refresh_row(admission_id, "admission-failed", small=42),
            expected_cache_path="miss",
        )

        for row_id, label, hook in (
            (crc_id, "crc-corrupt", "_test_corrupt_refresh_crc"),
            (boundary_id, "xid-boundary", "_test_refresh_xid_boundary"),
        ):
            before_boundary = read_cache_stats() if row_id == boundary_id else None
            sql(
                f"BEGIN; UPDATE {relation} SET value = {sql_literal(label)} "
                f"WHERE id = {row_id}; SELECT local_cache.{hook}("
                f"'{relation}'::regclass::oid, {sql_literal(namespace)}, "
                f"{sql_literal(canonical_int8_key(row_id))}); COMMIT"
            )
            refresh_lookup(
                client,
                crud_key(table, row_id),
                refresh_row(row_id, label, small=43 if row_id == crc_id else 44),
                expected_cache_path="miss",
            )
            if row_id == boundary_id:
                after_boundary = read_cache_stats()
                assert (
                    after_boundary["refresh_skips_full_xid_boundary_total"]
                    >= before_boundary["refresh_skips_full_xid_boundary_total"] + 1
                ), (before_boundary, after_boundary)

        # A no-XID transaction after a real writer may invalidate the key but
        # must never reuse the previous writer's commit horizon as a candidate.
        no_xid_id = allocate_test_row_ids(table)[0]
        insert_refresh_rows(table, [(no_xid_id, False, 46, "no-xid-base")])
        no_xid_key = crud_key(table, no_xid_id)
        assert isinstance(mget_one(client, no_xid_key), bytes)
        no_xid_session = subprocess.Popen(
            psql_base_args(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "PGAPPNAME": f"pglc_refresh_no_xid_{os.getpid()}"},
        )
        _PSQL_PROCESS_OUTPUT[no_xid_session.pid] = bytearray()
        write_psql_input(
            no_xid_session,
            "SELECT 'writer=' || pg_backend_pid();\n"
            f"UPDATE {relation} SET value = 'no-xid-writer-final' "
            f"WHERE id = {no_xid_id};\nSELECT 'writer_committed';\n",
        )
        no_xid_prefix = wait_for_psql_output(
            no_xid_session, "writer_committed"
        )
        writer_pid = next(
            line.removeprefix("writer=")
            for line in no_xid_prefix.splitlines()
            if line.startswith("writer=")
        )
        refresh_lookup(
            client,
            no_xid_key,
            refresh_row(no_xid_id, "no-xid-writer-final", small=46),
            expected_cache_path="hit",
        )
        write_psql_input(
            no_xid_session,
            "BEGIN;\n"
            "SELECT local_cache._test_collect_key("
            f"'{relation}'::regclass::oid, {sql_literal(namespace)}, "
            f"{sql_literal(canonical_int8_key(no_xid_id))});\n"
            "SELECT 'no_xid=' || pg_backend_pid() || ':' || "
            "CASE WHEN txid_current_if_assigned() IS NULL "
            "THEN 'absent' ELSE 'assigned' END;\n"
            "COMMIT;\n",
        )
        close_psql_input(no_xid_session)
        no_xid_output = _finish_tracked_psql(no_xid_session)
        assert no_xid_session.returncode == 0, no_xid_output
        no_xid_row = next(
            line.removeprefix("no_xid=")
            for line in no_xid_output.splitlines()
            if line.startswith("no_xid=")
        )
        assert no_xid_row == f"{writer_pid}:absent", no_xid_output
        refresh_lookup(
            client,
            no_xid_key,
            refresh_row(no_xid_id, "no-xid-writer-final", small=46),
            expected_cache_path="miss",
        )

        # Marker overflow broadens publication; neither uncached candidate may
        # be installed from an incomplete per-key reservation set.
        partial_one, partial_two = allocate_test_row_ids(table, 2)
        insert_refresh_rows(
            table,
            [
                (partial_one, False, 47, "partial-one-base"),
                (partial_two, False, 48, "partial-two-base"),
            ],
        )
        # Keep the two marker reservations cold so partial publication cannot
        # accidentally pass because INSERT had already write-allocated them.
        sql(f"SELECT local_cache.invalidate({sql_literal(namespace)})")
        before_partial = read_cache_stats()
        set_test_dirty_marker_limit(1)
        try:
            sql(
                f"BEGIN; UPDATE {relation} SET value = 'partial-one-final' "
                f"WHERE id = {partial_one}; UPDATE {relation} "
                f"SET value = 'partial-two-final' WHERE id = {partial_two}; COMMIT"
            )
        finally:
            set_test_dirty_marker_limit(None)
        after_partial = read_cache_stats()
        assert (
            after_partial["refresh_installs_total"]
            == before_partial["refresh_installs_total"]
        ), (before_partial, after_partial)
        assert (
            after_partial["refresh_skips_total"]
            >= before_partial["refresh_skips_total"] + 2
        ), (before_partial, after_partial)
        refresh_lookup(
            client,
            crud_key(table, partial_one),
            refresh_row(partial_one, "partial-one-final", small=47),
            expected_cache_path="miss",
        )
        refresh_lookup(
            client,
            crud_key(table, partial_two),
            refresh_row(partial_two, "partial-two-final", small=48),
            expected_cache_path="miss",
        )

        # Invalidate after candidate copy, before final fence validation. Cover
        # both overwrite of a resident entry and marker-to-entry conversion.
        fence_id = allocate_test_row_ids(table)[0]
        insert_refresh_rows(table, [(fence_id, False, 49, "fence-base")])
        fence_key = crud_key(table, fence_id)
        assert isinstance(mget_one(client, fence_key), bytes)
        set_refresh_write_mode(table, "invalidate")
        marker_id = allocate_test_row_ids(table)[0]
        try:
            insert_refresh_rows(
                table, [(marker_id, False, 50, "marker-fence-base")]
            )
            marker_mode = set_refresh_write_mode(table, "refresh")
            assert marker_mode["write_mode_effective"] == "refresh", marker_mode
        finally:
            set_refresh_write_mode(table, "refresh")

        for row_id, label, small, case_name in (
            (fence_id, "fence-final", 49, "resident"),
            (marker_id, "marker-fence-final", 50, "marker"),
        ):
            writer_name = f"pglc_refresh_postcopy_{case_name}_{os.getpid()}"
            fence_writer: subprocess.Popen[str] | None = start_idle_writer(
                table, label, application_name=writer_name, row_id=row_id
            )
            locker: subprocess.Popen[str] | None = start_table_locker(
                barrier_table,
                application_name=f"pglc_refresh_postcopy_barrier_{case_name}_{os.getpid()}",
            )
            try:
                fence_pid = wait_for_application_pid(writer_name)
                before_postcopy_fence = read_cache_stats()
                set_test_pause(
                    "before_refresh_postcopy_validation",
                    barrier_table,
                    pause_pid=fence_pid,
                )
                start_writer_commit(fence_writer)
                wait_for_blocked_relation_pid(
                    barrier_table,
                    application_name=writer_name,
                    psql_process=fence_writer,
                    timeout=10,
                )
                set_test_pause(None)
                sql(f"SELECT local_cache.invalidate({sql_literal(namespace)})")
                finish_writer(locker, commit=True)
                locker = None
                finish_started_writer(fence_writer)
                fence_writer = None
            finally:
                set_test_pause(None)
                if locker is not None:
                    finish_writer(locker, commit=True)
                terminate_writer(fence_writer)
            after_postcopy_fence = read_cache_stats()
            assert after_postcopy_fence["refresh_skips_fence_mismatch_total"] >= (
                before_postcopy_fence["refresh_skips_fence_mismatch_total"] + 1
            ), (case_name, before_postcopy_fence, after_postcopy_fence)
            assert after_postcopy_fence["refresh_reservations_outstanding"] == 0, (
                case_name,
                after_postcopy_fence,
            )
            assert int(
                sql(
                    "SELECT public.pglc_test_relation_identity_pins("
                    f"'{relation}'::regclass, {sql_literal(namespace)})"
                )
            ) == 0
            refresh_lookup(
                client,
                crud_key(table, row_id),
                refresh_row(row_id, label, small=small),
                expected_cache_path="miss",
            )

        # Ordinary admission pressure may evict unrelated entries but must
        # preserve the dirty reservation until post-commit installation.
        eviction_count = int(read_cache_stats()["cache_capacity"]) + 64
        pressure_ids = allocate_test_row_ids(table, eviction_count + 1)
        eviction_id = pressure_ids[0]
        pressure_ids = pressure_ids[1:]
        insert_refresh_rows(
            table,
            [
                (row_id, False, 55, f"pressure-{index}")
                for index, row_id in enumerate(pressure_ids)
            ]
            + [(eviction_id, False, 54, "eviction-base")],
        )
        eviction_key = crud_key(table, eviction_id)
        assert isinstance(mget_one(client, eviction_key), bytes)
        eviction_locker: subprocess.Popen[str] | None = start_table_locker(
            barrier_table,
            application_name=f"pglc_refresh_eviction_barrier_{os.getpid()}",
        )
        eviction_writer: subprocess.Popen[str] | None = start_idle_writer(
            table,
            "eviction-final",
            application_name=f"pglc_refresh_eviction_{os.getpid()}",
            row_id=eviction_id,
        )
        try:
            eviction_pid = wait_for_application_pid(
                f"pglc_refresh_eviction_{os.getpid()}"
            )
            set_test_pause(
                "before_refresh_install_lock", barrier_table,
                pause_pid=eviction_pid,
            )
            start_writer_commit(eviction_writer)
            wait_for_blocked_relation_pid(
                barrier_table,
                application_name=f"pglc_refresh_eviction_{os.getpid()}",
                psql_process=eviction_writer,
                timeout=10,
            )
            set_test_pause(None)
            before_pressure = read_cache_stats()
            pressure_keys = [crud_key(table, row_id) for row_id in pressure_ids]
            pressure_values = client.command("MGET", *pressure_keys)
            assert len(pressure_values) == len(pressure_ids)
            assert all(isinstance(value, bytes) for value in pressure_values)
            after_pressure = read_cache_stats()
            assert after_pressure["evictions"] > before_pressure["evictions"], (
                before_pressure,
                after_pressure,
            )
            finish_writer(eviction_locker, commit=True)
            eviction_locker = None
            finish_started_writer(eviction_writer)
            eviction_writer = None
        finally:
            set_test_pause(None)
            if eviction_locker is not None:
                finish_writer(eviction_locker, commit=True)
            terminate_writer(eviction_writer)
        refresh_lookup(
            client,
            eviction_key,
            refresh_row(eviction_id, "eviction-final", small=54),
            expected_cache_path="hit",
        )

        # Removing the reserved entry while refresh waits at install must not
        # permit a stale token/slot to be reused for the captured row.
        remove_locker: subprocess.Popen[str] | None = None
        remove_writer: subprocess.Popen[str] | None = None
        remove_id = removed_id
        remove_key = crud_key(table, remove_id)
        remove_locker = start_table_locker(
            barrier_table,
            application_name=f"pglc_refresh_remove_barrier_{os.getpid()}",
        )
        remove_writer = start_idle_writer(
            table,
            "reserved-removed-final",
            application_name=f"pglc_refresh_remove_{os.getpid()}",
            row_id=remove_id,
        )
        remove_pid = wait_for_application_pid(f"pglc_refresh_remove_{os.getpid()}")
        database_oid = int(
            sql(
                "SELECT oid FROM pg_catalog.pg_database "
                "WHERE datname = current_database()"
            )
        )
        removed_partition = int(
            sql(
                "SELECT public.pglc_test_partition("
                f"{database_oid}, {sql_literal(namespace)}, "
                f"{sql_literal(canonical_int8_key(remove_id))})"
            )
        )
        reuse_candidates = allocate_test_row_ids(table, 1024)
        reuse_id = int(
            sql(
                "SELECT candidate FROM generate_series("
                f"{reuse_candidates[0]}, {reuse_candidates[-1]}) AS candidates(candidate) "
                "WHERE public.pglc_test_partition("
                f"{database_oid}, {sql_literal(namespace)}, "
                "length(candidate::text)::text || ':' || candidate::text || ';'"
                f") = {removed_partition} LIMIT 1"
            )
        )
        insert_refresh_rows(table, [(reuse_id, True, 50, "replacement-slot")])
        reuse_key = crud_key(table, reuse_id)
        try:
            set_test_pause(
                "before_refresh_install_lock", barrier_table, pause_pid=remove_pid
            )
            start_writer_commit(remove_writer)
            wait_for_blocked_relation_pid(
                barrier_table,
                application_name=f"pglc_refresh_remove_{os.getpid()}",
                psql_process=remove_writer,
                timeout=10,
            )
            set_test_pause(None)
            sql(
                "SELECT local_cache._test_remove_reserved_entry("
                f"'{relation}'::regclass::oid, {sql_literal(namespace)}, "
                f"{sql_literal(canonical_int8_key(remove_id))})"
            )
            # Same-partition miss reuses the just-freed cache slot while the
            # old refresh candidate is still pending installation.
            refresh_lookup(
                client,
                reuse_key,
                refresh_row(reuse_id, "replacement-slot", flag=True, small=50),
                expected_cache_path="miss",
            )
            refresh_lookup(
                client,
                reuse_key,
                refresh_row(reuse_id, "replacement-slot", flag=True, small=50),
                expected_cache_path="hit",
            )
            finish_writer(remove_locker, commit=True)
            remove_locker = None
            finish_started_writer(remove_writer)
            remove_writer = None
        finally:
            set_test_pause(None)
            if remove_locker is not None:
                finish_writer(remove_locker, commit=True)
            terminate_writer(remove_writer)
        refresh_lookup(
            client,
            remove_key,
            refresh_row(remove_id, "reserved-removed-final", small=45),
            expected_cache_path="miss",
        )
        refresh_lookup(
            client,
            reuse_key,
            refresh_row(reuse_id, "replacement-slot", flag=True, small=50),
            expected_cache_path="miss",
        )
        final_stats = read_cache_stats()
        assert final_stats["cache_bypass"] == 1, final_stats
        assert final_stats["refresh_reservations_outstanding"] == 0, final_stats
        assert int(
            sql(
                "SELECT public.pglc_test_relation_identity_pins("
                f"'{relation}'::regclass, {sql_literal(namespace)})"
            )
        ) == 0
    finally:
        set_test_pause(None)
        set_test_dirty_marker_limit(None)
        client.close()


def test_refresh_identity_and_ddl(table: str, namespace: str) -> None:
    client = RespConnection()
    child_table = f"pglc_refresh_child_{os.getpid()}"
    (
        source_id,
        moved_id,
        truncate_id,
        truncate_cascade_id,
        reuse_source,
        reuse_moved,
    ) = allocate_test_row_ids(table, 6)
    try:
        insert_refresh_rows(
            table,
            [
                (source_id, False, 51, "pk-source"),
                (truncate_id, False, 52, "truncate-source"),
                (truncate_cascade_id, False, 53, "truncate-cascade-source"),
                (reuse_source, False, 54, "reuse-source"),
            ],
        )
        old_key = crud_key(table, source_id)
        moved_key = crud_key(table, moved_id)
        assert isinstance(mget_one(client, old_key), bytes)
        sql(
            f"UPDATE public.{sql_identifier(table)} SET id = {moved_id}, "
            f"value = 'pk-moved' WHERE id = {source_id}"
        )
        refresh_lookup(client, old_key, None, expected_cache_path="negative-hit")
        refresh_lookup(
            client,
            moved_key,
            refresh_row(moved_id, "pk-moved", small=51),
            expected_cache_path="hit",
        )
        insert_refresh_rows(table, [(source_id, True, 53, "pk-reused")])
        refresh_lookup(
            client,
            old_key,
            refresh_row(source_id, "pk-reused", flag=True, small=53),
        )

        # Reusing both keys during a key move poisons both candidates.
        reuse_source_key = crud_key(table, reuse_source)
        reuse_moved_key = crud_key(table, reuse_moved)
        assert isinstance(mget_one(client, reuse_source_key), bytes)
        sql(
            f"BEGIN; UPDATE public.{sql_identifier(table)} SET id = {reuse_moved}, "
            f"value = 'reuse-moved' WHERE id = {reuse_source}; "
            f"UPDATE public.{sql_identifier(table)} SET id = {reuse_source}, "
            f"value = 'reuse-final' WHERE id = {reuse_moved}; COMMIT"
        )
        refresh_lookup(
            client,
            reuse_source_key,
            refresh_row(reuse_source, "reuse-final", small=54),
            expected_cache_path="miss",
        )
        refresh_lookup(
            client,
            reuse_moved_key,
            None,
            expected_cache_path="miss",
        )

        # Detach/forget after a captured update, then remap the relation.
        assert isinstance(mget_one(client, moved_key), bytes)
        sql(
            f"BEGIN; UPDATE public.{sql_identifier(table)} SET value = 'remapped' "
            f"WHERE id = {moved_id}; "
            f"SELECT local_cache.detach_table('{table}'::regclass); COMMIT; "
            f"SELECT local_cache.attach_table('{table}'::regclass, true, "
            f"{sql_literal(namespace)}); "
            f"SELECT local_cache.set_write_mode('{table}'::regclass, 'refresh')"
        )
        refresh_lookup(
            client,
            moved_key,
            refresh_row(moved_id, "remapped", small=51),
            expected_cache_path="miss",
        )

        # A configuration change and restoration cannot resurrect a candidate.
        assert isinstance(mget_one(client, moved_key), bytes)
        sql(
            f"BEGIN; UPDATE public.{sql_identifier(table)} "
            f"SET value = 'config-final' WHERE id = {moved_id}; "
            f"SELECT local_cache.set_write_mode('{table}'::regclass, 'invalidate'); "
            f"SELECT local_cache.set_write_mode('{table}'::regclass, 'refresh'); "
            "COMMIT"
        )
        refresh_lookup(
            client,
            moved_key,
            refresh_row(moved_id, "config-final", small=51),
            expected_cache_path="miss",
        )

        # Unsupported descriptor mode reports invalidate; restore it and
        # confirm a supported write can refresh again.
        set_refresh_write_mode(table, "invalidate")
        sql(
            f"ALTER TABLE public.{sql_identifier(table)} ADD COLUMN payload jsonb"
        )
        unsupported = set_refresh_write_mode(table, "refresh")
        assert unsupported["write_mode_effective"] == "invalidate", unsupported
        payload_key = crud_key(table, moved_id)
        response = mget_one(client, payload_key)
        assert isinstance(response, bytes)
        payload_row = json.loads(response)
        assert payload_row["value"] == "config-final" and payload_row["payload"] is None
        sql(f"ALTER TABLE public.{sql_identifier(table)} DROP COLUMN payload")
        restored = set_refresh_write_mode(table, "refresh")
        assert restored["write_mode_effective"] == "refresh", restored
        sql(
            f"UPDATE public.{sql_identifier(table)} SET value = 'descriptor-restored' "
            f"WHERE id = {moved_id}"
        )
        refresh_lookup(
            client,
            payload_key,
            refresh_row(moved_id, "descriptor-restored", small=51),
            expected_cache_path="hit",
        )

        truncate_key = crud_key(table, truncate_id)
        assert isinstance(mget_one(client, truncate_key), bytes)
        sql(
            f"BEGIN; UPDATE public.{sql_identifier(table)} "
            f"SET value = 'truncate-updated' WHERE id = {truncate_id}; "
            f"TRUNCATE public.{sql_identifier(table)}; COMMIT"
        )
        refresh_lookup(
            client,
            truncate_key,
            None,
            expected_cache_path="miss",
        )

        insert_refresh_rows(
            table,
            [(truncate_cascade_id, False, 53, "truncate-cascade-source")],
        )
        truncate_cascade_key = crud_key(table, truncate_cascade_id)
        assert isinstance(mget_one(client, truncate_cascade_key), bytes)
        sql(
            f"CREATE TABLE public.{sql_identifier(child_table)} ("
            f"id bigint PRIMARY KEY, parent_id bigint NOT NULL REFERENCES "
            f"public.{sql_identifier(table)}(id)); "
            f"INSERT INTO public.{sql_identifier(child_table)} "
            f"VALUES (1, {truncate_cascade_id})"
        )
        sql(
            f"BEGIN; UPDATE public.{sql_identifier(table)} "
            f"SET value = 'truncate-cascade-updated' "
            f"WHERE id = {truncate_cascade_id}; "
            f"TRUNCATE public.{sql_identifier(table)} CASCADE; COMMIT"
        )
        refresh_lookup(
            client,
            truncate_cascade_key,
            None,
            expected_cache_path="miss",
        )
    finally:
        client.close()
        sql(
            f"DROP TABLE IF EXISTS public.{sql_identifier(child_table)} CASCADE"
        )


def test_refresh_no_xid_parallel_and_prepare(
    table: str, namespace: str
) -> None:
    client = RespConnection()
    row_id = allocate_test_row_ids(table)[0]
    relation = f"public.{sql_identifier(table)}"
    key = crud_key(table, row_id)
    try:
        insert_refresh_rows(table, [(row_id, False, 61, "parallel-base")])
        assert isinstance(mget_one(client, key), bytes)
        parallel_guc = (
            "debug_parallel_query"
            if sql(
                "SELECT current_setting('debug_parallel_query', true) "
                "IS NOT NULL"
            )
            == "t"
            else "force_parallel_mode"
        )
        command = (
            f"BEGIN; SET LOCAL {parallel_guc} = on; "
            "SET LOCAL max_parallel_workers_per_gather = 4; "
            "SET LOCAL parallel_setup_cost = 0; SET LOCAL parallel_tuple_cost = 0; "
            "SET LOCAL min_parallel_table_scan_size = 0; "
            "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) "
            "SELECT count(*) FROM generate_series(1, 100000) AS g(i) "
            "WHERE local_cache._test_collect_key("
            f"'{relation}'::regclass::oid, {sql_literal(namespace)}, "
            f"{sql_literal(canonical_int8_key(row_id))}) IS NULL; COMMIT"
        )
        plan = sql(command)
        assert "Gather" in plan or "Parallel" in plan, plan
        refresh_lookup(
            client,
            key,
            refresh_row(row_id, "parallel-base", small=61),
            expected_cache_path="hit",
        )

        # Refresh remains incompatible with prepared transactions because the
        # deferred finish/install contract requires a normal top-level commit.
        prepare_command = (
            f"BEGIN; UPDATE {relation} SET value = 'prepared-refresh' "
            f"WHERE id = {row_id}; PREPARE TRANSACTION "
            f"{sql_literal(f'pglc_refresh_{os.getpid()}')};"
        )
        result = run_psql(
            psql_base_args() + ["-c", prepare_command],
            statement=prepare_command,
            timeout=20,
        )
        output = result.stdout + result.stderr
        assert result.returncode != 0, output
        assert "PREPARE TRANSACTION is not supported" in output, output
        refresh_lookup(
            client,
            key,
            refresh_row(row_id, "parallel-base", small=61),
            expected_cache_path="hit",
        )
    finally:
        client.close()


def main() -> None:
    suffix = str(os.getpid())
    table = f"p{suffix}"
    composite_table = f"c{suffix}"
    scoped_table = f"s{suffix}"
    incarnation_table = f"i{suffix}"
    truncate_table = f"t{suffix}"
    refresh_table = f"r{suffix}"
    barrier_table = f"b{suffix}"
    second_barrier_table = f"q{suffix}"
    mapping_namespace = f"pipeline{suffix}"
    composite_namespace = f"pipelinec{suffix}"
    scoped_namespace = f"pipelines{suffix}"
    incarnation_namespace = f"pipelinei{suffix}"
    refresh_namespace = f"pipeliner{suffix}"
    granted_roles = list(dict.fromkeys(filter(None, (WORKER_ROLE, WRITER_ROLE))))
    grant = "".join(
        f"GRANT USAGE ON SCHEMA public TO {sql_identifier(role)};"
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        f"public.{table}, public.{composite_table}, public.{scoped_table}, "
        f"public.{incarnation_table}, "
        f"public.{truncate_table}, public.{refresh_table} "
        f"TO {sql_identifier(role)};"
        f"GRANT UPDATE ON TABLE public.{barrier_table}, "
        f"public.{second_barrier_table} TO {sql_identifier(role)};"
        for role in granted_roles
    )
    sql("CREATE EXTENSION IF NOT EXISTS pg_local_cache")
    reset_test_gucs_if_available()
    sql(
        f"CREATE TABLE public.{table} "
        "(id bigint PRIMARY KEY, value text NOT NULL);"
        f"INSERT INTO public.{table} VALUES "
        f"(1, 'initial'), (2, repeat('x', {BACKPRESSURE_VALUE_BYTES})), "
        "(3, 'delete-once'), "
        "(4, 'malformed-tail'), (5, 'auth-tail');"
        f"CREATE TABLE public.{composite_table} ("
        "tenant text, id bigint, value text NOT NULL, "
        "PRIMARY KEY (tenant, id));"
        f"INSERT INTO public.{composite_table} VALUES "
        "('tenant-a', 1, 'composite');"
        f"CREATE TABLE public.{scoped_table} "
        "(id bigint PRIMARY KEY, value text NOT NULL);"
        f"INSERT INTO public.{scoped_table} VALUES (1, 'scope-only');"
        f"CREATE TABLE public.{incarnation_table} "
        "(id bigint PRIMARY KEY, value text NOT NULL);"
        f"INSERT INTO public.{incarnation_table} VALUES (1, 'incarnation-base');"
        f"CREATE TABLE public.{truncate_table} "
        "(id bigint PRIMARY KEY, value text NOT NULL);"
        f"INSERT INTO public.{truncate_table} VALUES (1, 'before-truncate');"
        f"CREATE TABLE public.{refresh_table} ("
        "id bigint PRIMARY KEY, flag boolean NOT NULL, small smallint NOT NULL, "
        "value text NOT NULL, extra text);"
        f"CREATE TABLE public.{barrier_table} (id integer PRIMARY KEY);"
        f"CREATE TABLE public.{second_barrier_table} (id integer PRIMARY KEY);"
        f"INSERT INTO public.{barrier_table} VALUES (1);"
        f"INSERT INTO public.{second_barrier_table} VALUES (1);"
        f"{grant}"
        f"SELECT local_cache.attach_table("
        f"'public.{table}'::regclass, true, '{mapping_namespace}');"
        f"SELECT local_cache.attach_table("
        f"'public.{composite_table}'::regclass, true, "
        f"'{composite_namespace}');"
        f"SELECT local_cache.attach_table("
        f"'public.{scoped_table}'::regclass, true, '{scoped_namespace}');"
        f"SELECT local_cache.attach_table("
        f"'public.{incarnation_table}'::regclass, true, '{incarnation_namespace}');"
        f"SELECT local_cache.attach_table("
        f"'public.{truncate_table}'::regclass, true, 'pipelinet{suffix}')"
        f"; SELECT local_cache.attach_table("
        f"'public.{refresh_table}'::regclass, true, '{refresh_namespace}')"
    )
    hooks_available = install_test_hook_functions(
        incarnation_table, incarnation_namespace
    )
    try:
        if hooks_available:
            test_key_scanner_differential()
        bootstrap = RespConnection()
        try:
            assert wait_for_mapping(
                bootstrap, crud_key(table, 1)
            ) == row_bytes(1, "initial")
            assert wait_for_mapping(
                bootstrap, crud_key(table, 2)
            ) == row_bytes(2, "x" * BACKPRESSURE_VALUE_BYTES)
        finally:
            bootstrap.close()

        if os.environ.get("PGLC_PAUSE_HOOKS_ONLY") == "1":
            if not hooks_available:
                raise RuntimeError(
                    "pause-hook-only phase requires PGLC_TEST_HOOKS functions"
                )
            run_pause_hook_tests(
                table,
                mapping_namespace,
                scoped_table,
                incarnation_table,
                incarnation_namespace,
                barrier_table,
                second_barrier_table,
            )
            print("pipeline pause-hook fence/race cases passed")
        elif os.environ.get("PGLC_MULTI_WORKER_ONLY") == "1":
            test_mget_does_not_hold_claim_while_waiting(table, scoped_table)
            test_mget_cancelled_owner_releases_claim(table)
            if hooks_available:
                run_pause_hook_test(
                    "test_reload_claim_keeps_truncated_payload_invalid",
                    lambda: test_reload_claim_keeps_truncated_payload_invalid(
                        truncate_table, barrier_table
                    ),
                )
            else:
                print(
                    "SKIP hook-dependent case "
                    "(PGLC_TEST_HOOKS functions absent): "
                    "test_reload_claim_keeps_truncated_payload_invalid"
                )
            print("pipeline multi-worker integration passed")
        elif os.environ.get("PGLC_REFRESH_ONLY") == "1":
            if not hooks_available:
                raise AssertionError("W7 refresh integration requires PGLC_TEST_HOOKS")
            test_refresh_basic_semantics(refresh_table, refresh_namespace)
            test_refresh_two_writer_orders(
                refresh_table,
                refresh_namespace,
                barrier_table,
                second_barrier_table,
            )
            test_refresh_no_xid_parallel_and_prepare(
                refresh_table, refresh_namespace
            )
            test_refresh_identity_and_ddl(refresh_table, refresh_namespace)
            test_refresh_fallbacks_and_slots(
                refresh_table, refresh_namespace, barrier_table
            )
            print("pipeline refresh integration passed: callbacks, subtransactions, "
                  "fallbacks, capacity, descriptor/remap and identity fences")
        else:
            print(
                "SKIP multi-worker pipeline cases (requires four workers): "
                "test_mget_does_not_hold_claim_while_waiting, "
                "test_mget_cancelled_owner_releases_claim, "
                "test_reload_claim_keeps_truncated_payload_invalid"
            )
            test_fragmented_suffix_and_order(table)
            test_command_error_does_not_poison_batch(table)
            test_warm_pipeline_has_no_sql_reads(table)
            if hooks_available:
                test_fast_mget_hit_has_no_worker_palloc(table)
            test_hot_counter_shards_aggregate()
            test_mget(table, composite_table, scoped_table)
            test_relation_locked_mget_defers(table, scoped_table)
            test_deferred_queue_full_returns_busy(table)
            test_disconnect_clears_deferred_mget(table)
            # Existing stale-read stress test covers the snapshot/store race statistically.
            test_mget_statement_timeout_cleanup(table)
            test_mget_mapping_reload_cleanup(table)
            test_ddl_lock_does_not_stall_other_relations(table, scoped_table)
            test_pipeline_budget_is_a_fairness_yield()
            test_interleaved_client_pipelines_preserve_order()
            if not TLS_CA_FILE:
                test_deferred_pipeline_backpressure_and_half_close(table)
                test_half_close_drains_final_pipeline(table)
            test_backpressure_preserves_every_response(table)
            test_close_after_flush(table)
            test_transactional_commit_and_rollback(table)
            test_uncommitted_write_is_not_served_before_commit(table)
            test_enabled_kill_switch(table)
            test_key_fill_hit_ratio_under_update_load(table)
            test_preprepare_still_rejected(table)
            if hooks_available:
                if (
                    os.environ.get("PGLC_PAUSE_HOOKS_ONLY") != "1"
                    or os.environ.get("PGLC_SKIP_PAUSE_HOOK_TESTS") == "1"
                ):
                    print(
                        "SKIP pause-hook tests "
                        "(dedicated long-timeout pause phase only): "
                        + ", ".join(PAUSE_HOOK_TEST_NAMES)
                    )
                else:
                    run_pause_hook_tests(
                        table,
                        mapping_namespace,
                        scoped_table,
                        incarnation_table,
                        incarnation_namespace,
                        barrier_table,
                        second_barrier_table,
                    )
                test_partial_reservation_abort_releases_identity(
                    table, mapping_namespace
                )
            else:
                print(
                    "SKIP hook-dependent cases (PGLC_TEST_HOOKS functions absent): "
                    "test_partition_routing_and_opposite_order_writers, "
                    "test_unrelated_key_fill_survives_keyed_write, "
                    "test_same_key_relation_and_global_fences, "
                    "test_warm_hit_snapshot_and_copy_fences, "
                    "test_relation_global_claim_store_fences, "
                    "test_marker_exhaustion_falls_back_safely, "
                    "test_dirty_key_dedup_and_relation_fallback, "
                    "test_namespace_invalidation_preserves_other_scope, "
                    "test_overlapping_publishers_on_one_key, "
                    "test_abort_after_dirty_publication, "
                    "test_partial_reservation_abort_releases_identity, "
                    "test_relation_incarnation_forget_recreate"
                )
            half_close_coverage = "half-close drain, " if not TLS_CA_FILE else ""
            print(
                "pipeline integration passed: fragmentation/order, warm-hit stats, "
                "error recovery, fairness resume, "
                + half_close_coverage
                + "backpressure, phased MGET, close-after-flush, "
                "sharded stat totals, "
                "partition routing and opposite-order writers, "
                "transaction dirty-key deduplication and relation fallback, "
                "commit/rollback fence, database/table key scope, "
                "uncommitted-write visibility, relation-incarnation and stale-fill fences, "
                "completed-TRUNCATE reload fencing, "
                "overlapping/aborted publishers, UPDATE-load hit ratio, "
                "SIGHUP cache kill switch, "
                "non-superuser writer"
            )
    finally:
        reset_test_gucs_if_available()
        drop_test_hook_functions()
        sql(
            f"SELECT local_cache.detach_table('public.{table}'::regclass);"
            f"SELECT local_cache.detach_table("
            f"'public.{composite_table}'::regclass);"
            f"SELECT local_cache.detach_table("
            f"'public.{scoped_table}'::regclass);"
            f"SELECT local_cache.detach_table(to_regclass("
            f"'public.{incarnation_table}')) "
            f"WHERE to_regclass('public.{incarnation_table}') IS NOT NULL;"
            f"SELECT local_cache.detach_table(to_regclass("
            f"'public.{truncate_table}')) "
            f"WHERE to_regclass('public.{truncate_table}') IS NOT NULL;"
            f"SELECT local_cache.detach_table(to_regclass("
            f"'public.{refresh_table}')) "
            f"WHERE to_regclass('public.{refresh_table}') IS NOT NULL;"
            f"DROP TABLE IF EXISTS public.{table};"
            f"DROP TABLE IF EXISTS public.{composite_table};"
            f"DROP TABLE IF EXISTS public.{scoped_table};"
            f"DROP TABLE IF EXISTS public.{incarnation_table};"
            f"DROP TABLE IF EXISTS public.{truncate_table};"
            f"DROP TABLE IF EXISTS public.{refresh_table};"
            f"DROP TABLE IF EXISTS public.{barrier_table};"
            f"DROP TABLE IF EXISTS public.{second_barrier_table}"
        )


if __name__ == "__main__":
    main()
