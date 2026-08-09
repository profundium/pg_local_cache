#!/usr/bin/env python3
"""Adversarial black-box tests for the RESP pipeline/event-loop hot path.

The regular integration suite concentrates on cache semantics.  This script
keeps a single connection busy in the ways that are easy to regress while
optimising the worker: an incomplete suffix after complete commands, output
backpressure, a fairness yield, and close-after-flush.  It also verifies that
the optimised warm-hit path retains the transactional invalidation fence.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time


PSQL = os.environ.get("PG_LOCAL_CACHE_PSQL", "psql")
PGHOST = os.environ.get("PGHOST", "127.0.0.1")
PGPORT = os.environ.get("PGPORT", "5432")
PGDATABASE = os.environ.get("PGDATABASE", "postgres")
RESP_HOST = os.environ.get("PG_LOCAL_CACHE_RESP_HOST", "127.0.0.1")
RESP_PORT = int(os.environ.get("PG_LOCAL_CACHE_RESP_PORT", "6380"))
AUTH_TOKEN = os.environ.get("PG_LOCAL_CACHE_AUTH_TOKEN", "")
WORKER_ROLE = os.environ.get("PG_LOCAL_CACHE_TEST_ROLE", "")
WRITER_ROLE = os.environ.get("PG_LOCAL_CACHE_TEST_WRITER_ROLE", "")
WRITER_PASSWORD = os.environ.get("PG_LOCAL_CACHE_TEST_WRITER_PASSWORD", "")
WRITER_HOST = os.environ.get("PG_LOCAL_CACHE_TEST_WRITER_HOST", "127.0.0.1")
BACKPRESSURE_VALUE_BYTES = 3_900
MAX_PIPELINE_INPUT_BYTES = 65_536
MAX_RESPONSE_BYTES = 65_536 + 1_024

if WORKER_ROLE and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,62}", WORKER_ROLE):
    raise ValueError("PG_LOCAL_CACHE_TEST_ROLE is not a safe SQL identifier")
if WRITER_ROLE and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,62}", WRITER_ROLE):
    raise ValueError("PG_LOCAL_CACHE_TEST_WRITER_ROLE is not a safe SQL identifier")
if bool(WRITER_ROLE) != bool(WRITER_PASSWORD):
    raise ValueError("writer role and password must be configured together")


class RespError(RuntimeError):
    """An error frame returned by the RESP server."""


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class RespConnection:
    def __init__(
        self,
        *,
        authenticate: bool = True,
        receive_buffer: int | None = None,
    ) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if receive_buffer is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
        self.socket.settimeout(5)
        self.socket.connect((RESP_HOST, RESP_PORT))
        self.buffer = bytearray()
        self.position = 0
        if authenticate and AUTH_TOKEN:
            assert self.command("AUTH", AUTH_TOKEN) == "OK"

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


def sql(query: str) -> str:
    return subprocess.check_output(
        psql_args(query), text=True, stderr=subprocess.STDOUT, timeout=30
    ).strip()


def sql_commands(*queries: str) -> str:
    args = psql_base_args()
    for query in queries:
        args.extend(("-c", query))
    return subprocess.check_output(
        args, text=True, stderr=subprocess.STDOUT, timeout=30
    ).strip()


def wait_for_mapping(client: RespConnection, key: str) -> bytes:
    deadline = time.monotonic() + 10
    while True:
        try:
            value = client.command("GET", key)
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
        value = client.command("GET", key)
        if value == expected:
            return
        if time.monotonic() >= deadline:
            raise AssertionError((value, expected))
        time.sleep(0.01)


def row_bytes(row_id: int, value: str) -> bytes:
    return json.dumps(
        {"id": row_id, "value": value}, separators=(",", ":")
    ).encode()


def crud_key(table: str, row_id: int | str) -> str:
    return (
        f"CRUD:{PGDATABASE}.public.{table}:"
        + json.dumps({"id": str(row_id)}, separators=(",", ":"))
    )


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


def start_idle_writer(
    table: str, value: str, *, application_name: str
) -> subprocess.Popen[str]:
    role = (
        f"SET ROLE {sql_identifier(WORKER_ROLE)};"
        if WORKER_ROLE and not WRITER_ROLE
        else ""
    )
    arguments = psql_base_args()
    environment = None
    if WRITER_ROLE:
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
    assert process.stdin is not None
    process.stdin.write(
        f"{role}BEGIN; "
        f"UPDATE public.{table} SET value = '{value}' WHERE id = 1;"
        f"SET application_name = '{application_name}';\n"
    )
    process.stdin.flush()
    deadline = time.monotonic() + 10
    writer_identity = (
        f"AND usename = '{WRITER_ROLE}' " if WRITER_ROLE else ""
    )
    while sql(
        "SELECT count(*) FROM pg_catalog.pg_stat_activity "
        f"WHERE application_name = '{application_name}' "
        f"{writer_identity}"
        "AND state = 'idle in transaction'"
    ) != "1":
        if process.poll() is not None:
            output = process.communicate()[0]
            raise AssertionError(f"writer exited before its fence probe: {output}")
        if time.monotonic() >= deadline:
            process.terminate()
            output = process.communicate(timeout=5)[0]
            raise AssertionError(f"writer did not become idle in transaction: {output}")
        time.sleep(0.02)
    return process


def finish_writer(process: subprocess.Popen[str], *, commit: bool) -> str:
    assert process.stdin is not None
    process.stdin.write("COMMIT;\n" if commit else "ROLLBACK;\n")
    process.stdin.close()
    process.stdin = None
    output = process.communicate(timeout=10)[0]
    assert process.returncode == 0, output
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
        get = client.encode("GET", crud_key(table, 1))
        echo = client.encode("ECHO", "after-fragment")
        split = len(get) - 3

        # Reading PONG proves the server parsed the complete prefix while the
        # next frame was still incomplete and retained in its input buffer.
        client.socket.sendall(ping + get[:split])
        assert client.read_response() == "PONG"
        for byte in get[split:] + echo:
            client.socket.sendall(bytes((byte,)))
        assert client.read_response() == row_bytes(1, "initial")
        assert client.read_response() == b"after-fragment"
        assert client.command("PING") == "PONG"
    finally:
        client.close()


def test_command_error_does_not_poison_batch(table: str) -> None:
    client = RespConnection()
    try:
        client.socket.sendall(
            client.encode("PING")
            + client.encode("GET", crud_key(table, "not-a-bigint"))
            + client.encode("GET", crud_key(table, 1))
            + client.encode("ECHO", "after-error")
        )
        assert client.read_response() == "PONG"
        try:
            client.read_response()
            raise AssertionError("invalid key did not return a PostgreSQL error")
        except RespError as error:
            assert "PostgreSQL" in str(error)
        assert client.read_response() == row_bytes(1, "initial")
        assert client.read_response() == b"after-error"
        assert client.command("PING") == "PONG"
    finally:
        client.close()


def test_warm_pipeline_has_no_sql_reads(table: str) -> None:
    client = RespConnection()
    try:
        key = crud_key(table, 1)
        expected = row_bytes(1, "initial")
        assert client.command("GET", key) == expected
        before = json.loads(client.command("STAT"))
        count = 128
        client.socket.sendall(client.encode("GET", key) * count)
        for _ in range(count):
            assert client.read_response() == expected
        after = json.loads(client.command("STAT"))
        assert after["cache_misses"] - before["cache_misses"] == 0
        assert after["database_reads"] - before["database_reads"] == 0
        assert after["cache_hits"] - before["cache_hits"] == count
    finally:
        client.close()


def test_mget(table: str, composite_table: str) -> None:
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
        missing = crud_key(table, 9_000_000_000)
        composite = composite_key(composite_table, "tenant-a", 1)
        malformed = f"CRUD:{PGDATABASE}.public.unknown:{{\"id\":\"1\"}}"

        before_invalid = json.loads(client.command("STAT"))
        try:
            client.command("MGET", key_one, malformed)
            raise AssertionError("partially valid MGET did not fail")
        except RespError as error:
            assert "unknown KVik table mapping" in str(error)
        after_invalid = json.loads(client.command("STAT"))
        for counter in ("client_gets", "cache_hits", "cache_misses", "database_reads"):
            assert after_invalid[counter] == before_invalid[counter], (
                counter,
                before_invalid,
                after_invalid,
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
        assert after["client_gets"] - before["client_gets"] == 5
        assert after["cache_hits"] - before["cache_hits"] == 3
        assert after["cache_misses"] - before["cache_misses"] == 2
        assert after["database_reads"] - before["database_reads"] == 2

        repeated = client.command("MGET", *mixed_arguments)
        assert repeated == mixed
        repeated_stats = json.loads(client.command("STAT"))
        assert repeated_stats["client_gets"] - after["client_gets"] == 5
        assert repeated_stats["cache_hits"] - after["cache_hits"] == 5
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
    finally:
        client.close()


def test_pipeline_budget_is_a_fairness_yield() -> None:
    limit = int(sql("SHOW pg_local_cache.max_pipeline_commands"))
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


def test_half_close_drains_final_pipeline(table: str) -> None:
    client = RespConnection()
    try:
        client.socket.sendall(
            client.encode("ECHO", "before-half-close")
            + client.encode("GET", crud_key(table, 1))
            + client.encode("ECHO", "after-half-close")
        )
        client.socket.shutdown(socket.SHUT_WR)
        assert client.read_response() == b"before-half-close"
        assert client.read_response() == row_bytes(1, "initial")
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


def test_backpressure_preserves_every_response(table: str) -> None:
    client = RespConnection(receive_buffer=4096)
    peer: RespConnection | None = None
    try:
        client.socket.settimeout(20)
        key = crud_key(table, 2)
        expected = row_bytes(2, "x" * BACKPRESSURE_VALUE_BYTES)
        assert client.command("GET", key) == expected
        peer = same_worker_peer(client)
        before = json.loads(peer.command("STAT"))
        encoded_get = client.encode("GET", key)
        tail = (
            client.encode("DEL", crud_key(table, 3))
            + client.encode("GET", crud_key(table, 3))
        )
        count = min(
            1024,
            (MAX_PIPELINE_INPUT_BYTES - len(tail) - 1) // len(encoded_get),
        )
        assert count >= 256
        batch = encoded_get * count + tail
        assert len(batch) < MAX_PIPELINE_INPUT_BYTES
        client.socket.sendall(batch)
        client.socket.shutdown(socket.SHUT_WR)

        # The response is much larger than the deliberately restricted receive
        # window.  Require an observed EAGAIN, then prove the same event loop
        # stays serviceable.
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
        assert client.read_response() is None
        assert sql(f"SELECT count(*) FROM public.{table} WHERE id = 3") == "0"
        after = json.loads(peer.command("STAT"))
        assert (
            after["output_backpressure_events"]
            > before["output_backpressure_events"]
        )
        assert after["cache_hits"] - before["cache_hits"] == count
        assert after["cache_misses"] - before["cache_misses"] == 1
        assert after["database_reads"] - before["database_reads"] == 1
        assert after["database_writes"] - before["database_writes"] == 1
        try:
            client.read_response()
            raise AssertionError("backpressured half-close remained open after flush")
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
    client = RespConnection()
    key = crud_key(table, 1)
    commit_writer: subprocess.Popen[str] | None = None
    rollback_writer: subprocess.Popen[str] | None = None
    try:
        initial = row_bytes(1, "initial")
        committed = row_bytes(1, "committed")
        assert_eventual_value(client, key, initial)

        before_commit_writer = json.loads(client.command("STAT"))
        commit_writer = start_idle_writer(
            table,
            "committed",
            application_name=f"pglc_pipeline_commit_{os.getpid()}",
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
        assert client.command("GET", key) == initial
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
        assert client.command("GET", key) == committed
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
        assert client.command("GET", key) == committed
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
        )
        during_rollback_writer = json.loads(client.command("STAT"))
        assert (
            during_rollback_writer["invalidations"]
            == before_rollback_writer["invalidations"]
        )
        assert client.command("GET", key) == committed
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
        assert client.command("GET", key) == committed
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


def main() -> None:
    suffix = str(os.getpid())
    table = f"p{suffix}"
    composite_table = f"c{suffix}"
    mapping_namespace = f"pipeline{suffix}"
    composite_namespace = f"pipelinec{suffix}"
    granted_roles = list(dict.fromkeys(filter(None, (WORKER_ROLE, WRITER_ROLE))))
    grant = "".join(
        f"GRANT USAGE ON SCHEMA public TO {sql_identifier(role)};"
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        f"public.{table}, public.{composite_table} "
        f"TO {sql_identifier(role)};"
        for role in granted_roles
    )
    sql("CREATE EXTENSION IF NOT EXISTS pg_local_cache")
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
        f"{grant}"
        f"SELECT local_cache.attach_table("
        f"'public.{table}'::regclass, true, '{mapping_namespace}');"
        f"SELECT local_cache.attach_table("
        f"'public.{composite_table}'::regclass, true, "
        f"'{composite_namespace}')"
    )
    try:
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

        test_fragmented_suffix_and_order(table)
        test_command_error_does_not_poison_batch(table)
        test_warm_pipeline_has_no_sql_reads(table)
        test_mget(table, composite_table)
        test_pipeline_budget_is_a_fairness_yield()
        test_half_close_drains_final_pipeline(table)
        test_backpressure_preserves_every_response(table)
        test_close_after_flush(table)
        test_transactional_commit_and_rollback(table)
        print(
            "pipeline integration passed: fragmentation/order, warm-hit stats, "
            "error recovery, fairness resume, half-close drain, backpressure, "
            "bounded MGET, close-after-flush, commit/rollback fence, "
            "non-superuser writer"
        )
    finally:
        sql(
            f"SELECT local_cache.detach_table('public.{table}'::regclass);"
            f"SELECT local_cache.detach_table("
            f"'public.{composite_table}'::regclass);"
            f"DROP TABLE IF EXISTS public.{table};"
            f"DROP TABLE IF EXISTS public.{composite_table}"
        )


if __name__ == "__main__":
    main()
