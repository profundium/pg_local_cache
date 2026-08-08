#!/usr/bin/env python3
"""Black-box integration tests for the transparent SQL cache fast path.

The administrative connection is supplied by the Docker smoke-test psql
wrapper.  Every query whose result could be accelerated is executed through a
real LOGIN NOSUPERUSER role over a password-authenticated libpq connection;
using SET ROLE here would miss PostgreSQL's connection and ACL boundary.
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
POSTGRES_MAJOR = os.environ.get("POSTGRES_MAJOR", "16")

# Prefer purpose-specific names, but accept the existing Docker smoke-test
# writer variables so the test can be added to old runners without weakening
# the actual-login requirement.
APP_ROLE = os.environ.get(
    "PG_LOCAL_CACHE_TEST_APP_ROLE",
    os.environ.get("PG_LOCAL_CACHE_TEST_WRITER_ROLE", ""),
)
APP_PASSWORD = os.environ.get(
    "PG_LOCAL_CACHE_TEST_APP_PASSWORD",
    os.environ.get("PG_LOCAL_CACHE_TEST_WRITER_PASSWORD", ""),
)
APP_HOST = os.environ.get(
    "PG_LOCAL_CACHE_TEST_APP_HOST",
    os.environ.get("PG_LOCAL_CACHE_TEST_WRITER_HOST", "127.0.0.1"),
)

SQL_COUNTERS = (
    "sql_cache_hits",
    "sql_cache_misses",
    "sql_cache_fills",
    "sql_cache_bypasses",
)


if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,62}", APP_ROLE):
    raise ValueError("PG_LOCAL_CACHE_TEST_APP_ROLE is required and must be a safe SQL identifier")
if not APP_PASSWORD:
    raise ValueError("PG_LOCAL_CACHE_TEST_APP_PASSWORD is required")
if RESP_PORT != 0 and not AUTH_TOKEN:
    raise ValueError("PG_LOCAL_CACHE_AUTH_TOKEN is required")
if POSTGRES_MAJOR not in {"14", "15", "16", "17", "18"}:
    raise ValueError("POSTGRES_MAJOR must be one of 14, 15, 16, 17, or 18")


class RespError(RuntimeError):
    """An error response returned by the RESP endpoint."""


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def psql_base_args(*, application: bool) -> list[str]:
    arguments = [
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
    if application:
        # These later libpq options intentionally override the admin defaults
        # embedded in the Docker psql wrapper.
        arguments.extend(("-h", APP_HOST, "-U", APP_ROLE))
    return arguments


def run_psql(
    query: str,
    *,
    application: bool,
    script: bool = False,
) -> subprocess.CompletedProcess[str]:
    arguments = psql_base_args(application=application)
    if not script:
        arguments.extend(("-c", query))
    environment = os.environ.copy()
    if application:
        environment["PGPASSWORD"] = APP_PASSWORD
    return subprocess.run(
        arguments,
        input=query if script else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
        timeout=30,
    )


def checked_psql(query: str, *, application: bool, script: bool = False) -> str:
    result = run_psql(query, application=application, script=script)
    assert result.returncode == 0, result.stdout
    return result.stdout.strip()


def admin_sql(query: str) -> str:
    return checked_psql(query, application=False)


def app_sql(query: str) -> str:
    return checked_psql(query, application=True)


def app_script(query: str) -> str:
    # Feeding psql one statement per line keeps PREPARE alive in one backend
    # while preserving transaction boundaries between lines.
    return checked_psql(query, application=True, script=True)


def app_sql_fails(query: str, expected: str) -> None:
    result = run_psql(query, application=True)
    assert result.returncode != 0, result.stdout
    assert expected.lower() in result.stdout.lower(), result.stdout


def stats() -> dict[str, int]:
    value = json.loads(admin_sql("SELECT local_cache.stats()::text"))
    for counter in SQL_COUNTERS:
        assert isinstance(value.get(counter), int), (counter, value)
    return value


def assert_counter_delta(
    before: dict[str, int],
    after: dict[str, int],
    **expected: int,
) -> None:
    unknown = set(expected).difference(SQL_COUNTERS)
    assert not unknown, unknown
    actual = {
        counter: after[counter] - before[counter] for counter in SQL_COUNTERS
    }
    wanted = {counter: expected.get(counter, 0) for counter in SQL_COUNTERS}
    assert actual == wanted, {"actual": actual, "expected": wanted}


class RespClient:
    def __init__(self) -> None:
        self.socket = socket.create_connection((RESP_HOST, RESP_PORT), timeout=5)
        self.stream = self.socket.makefile("rb")
        assert self.command("AUTH", AUTH_TOKEN) == "OK"

    def close(self) -> None:
        self.stream.close()
        self.socket.close()

    def command(self, *arguments: object) -> object:
        encoded = [
            argument if isinstance(argument, bytes) else str(argument).encode()
            for argument in arguments
        ]
        request = [f"*{len(encoded)}\r\n".encode()]
        for argument in encoded:
            request.extend(
                (f"${len(argument)}\r\n".encode(), argument, b"\r\n")
            )
        self.socket.sendall(b"".join(request))
        return self._read_response()

    def _read_response(self) -> object:
        prefix = self.stream.read(1)
        if not prefix:
            raise EOFError("RESP connection closed")
        if prefix in (b"+", b"-", b":"):
            line = self.stream.readline()
            if not line.endswith(b"\r\n"):
                raise ValueError("invalid RESP line")
            value = line[:-2].decode()
            if prefix == b"-":
                raise RespError(value)
            return int(value) if prefix == b":" else value
        if prefix == b"$":
            length_line = self.stream.readline()
            if not length_line.endswith(b"\r\n"):
                raise ValueError("invalid RESP bulk length")
            length = int(length_line[:-2])
            if length == -1:
                return None
            value = self.stream.read(length)
            if len(value) != length or self.stream.read(2) != b"\r\n":
                raise ValueError("truncated RESP bulk string")
            return value.decode()
        raise ValueError(f"unsupported RESP prefix {prefix!r}")


def wait_for_negative_entry(client: RespClient, key: str) -> None:
    deadline = time.monotonic() + 10
    while True:
        try:
            assert client.command("GET", key) is None
            return
        except RespError as error:
            if "unknown pg_local_cache namespace" not in str(error):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def assert_custom_scan(plan: str) -> None:
    assert "Custom Scan (pg_local_cache_sql)" in plan, plan


def assert_no_custom_scan(plan: str) -> None:
    assert "pg_local_cache_sql" not in plan, plan


def wait_for_query(
    marker: str,
    *,
    expected_state: str,
    expected_wait_event_type: str | None = None,
    expected_wait_event: str | None = None,
) -> int:
    escaped_marker = marker.replace("'", "''")
    deadline = time.monotonic() + 10
    while True:
        observed = admin_sql(
            "SELECT pid::text || '|' || state || '|' || "
            "COALESCE(wait_event_type, '') || '|' || COALESCE(wait_event, '') "
            "FROM pg_catalog.pg_stat_activity "
            "WHERE pid <> pg_backend_pid() "
            f"AND pg_catalog.strpos(query, '{escaped_marker}') > 0 "
            "ORDER BY query_start DESC LIMIT 1"
        )
        if observed:
            pid, state, wait_event_type, wait_event = observed.split("|", 3)
            if (
                state == expected_state
                and (
                    expected_wait_event_type is None
                    or wait_event_type == expected_wait_event_type
                )
                and (
                    expected_wait_event is None
                    or wait_event == expected_wait_event
                )
            ):
                return int(pid)
        assert time.monotonic() < deadline, (
            marker,
            observed,
            expected_state,
            expected_wait_event_type,
            expected_wait_event,
        )
        time.sleep(0.02)


def assert_negative_cache_respects_old_snapshot(
    *,
    relation: str,
    barrier: str,
    source_id: int,
    mutation_sql: str,
    expected_json: str,
    suffix: str,
    case: str,
) -> None:
    blocker_marker = f"pglc_neg_block_{case}_{suffix}"
    reader_marker = f"pglc_neg_reader_{case}_{suffix}"
    blocker = subprocess.Popen(
        psql_base_args(application=False),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=os.environ.copy(),
    )
    reader: subprocess.Popen[str] | None = None
    blocker_pid: int | None = None
    try:
        assert blocker.stdin is not None
        blocker.stdin.write(
            "BEGIN;\n"
            f"UPDATE {barrier} SET value = value + 1 WHERE id = 1;\n"
            f"SELECT pg_sleep(30) /* {blocker_marker} */;\n"
            "COMMIT;\n"
        )
        blocker.stdin.close()
        blocker_pid = wait_for_query(
            blocker_marker,
            expected_state="active",
            expected_wait_event_type="Timeout",
            expected_wait_event="PgSleep",
        )

        reader = subprocess.Popen(
            psql_base_args(application=True)
            + [
                "-c",
                f"/* {reader_marker} */ "
                "WITH gate AS MATERIALIZED ("
                f"SELECT id FROM {barrier} WHERE id = 1 FOR UPDATE) "
                "SELECT local_cache.get("
                f"'{relation}'::regclass, {source_id}::bigint), "
                "array_to_json(local_cache.mget("
                f"'{relation}'::regclass, "
                f"ARRAY[{source_id}]::bigint[]))::text FROM gate",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "PGPASSWORD": APP_PASSWORD},
        )
        wait_for_query(
            reader_marker,
            expected_state="active",
            expected_wait_event_type="Lock",
        )

        admin_sql(mutation_sql)
        assert (
            app_sql(
                "SELECT local_cache.get("
                f"'{relation}'::regclass, {source_id}::bigint)"
            )
            == ""
        )
        before = stats()

        assert (
            admin_sql(
                "SELECT pg_catalog.pg_terminate_backend("
                f"{blocker_pid})"
            )
            == "t"
        )
        blocker_pid = None
        blocker.wait(timeout=10)
        blocker_output = blocker.stdout.read() if blocker.stdout is not None else ""
        assert reader is not None
        reader_output, _ = reader.communicate(timeout=10)
        assert reader.returncode == 0, reader_output
        columns = reader_output.strip().split("|", 1)
        assert columns[0] == expected_json, {
            "case": case,
            "reader": reader_output,
            "blocker": blocker_output,
        }
        assert json.loads(columns[1]) == [expected_json], {
            "case": case,
            "reader": reader_output,
        }
        assert_counter_delta(before, stats(), sql_cache_misses=2)
    finally:
        if blocker_pid is not None:
            try:
                admin_sql(
                    "SELECT pg_catalog.pg_terminate_backend("
                    f"{blocker_pid})"
                )
            except AssertionError:
                pass
        if blocker.poll() is None:
            blocker.terminate()
            try:
                blocker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                blocker.kill()
                blocker.wait(timeout=5)
        if reader is not None and reader.poll() is None:
            reader.terminate()
            try:
                reader.wait(timeout=5)
            except subprocess.TimeoutExpired:
                reader.kill()
                reader.wait(timeout=5)


def main() -> None:
    suffix = str(os.getpid())
    table = f"pglc_sql_fastpath_{suffix}"
    barrier = f"public.pglc_sql_barrier_{suffix}"
    namespace = f"sqlfast{suffix}"
    inheritance_schema = f"pglc_sql_inh_{suffix}"
    inheritance_namespace = f"sqlinh{suffix}"
    inheritance_parent = f"{inheritance_schema}.parent_rows"
    inheritance_child = f"{inheritance_schema}.child_rows"
    missing_id = 9_000_000_000 + os.getpid()
    relation = f"public.{table}"
    quoted_app_role = sql_identifier(APP_ROLE)
    client: RespClient | None = None

    server_version_num = int(admin_sql("SHOW server_version_num"))
    assert server_version_num // 10000 == int(POSTGRES_MAJOR), server_version_num

    identity = app_sql(
        "SELECT current_user, session_user, rolcanlogin, rolsuper "
        "FROM pg_catalog.pg_roles WHERE rolname = current_user"
    ).split("|")
    assert identity == [APP_ROLE, APP_ROLE, "t", "f"], identity

    try:
        admin_sql(
            f"CREATE TABLE {relation} ("
            "id bigint PRIMARY KEY, value text NOT NULL);"
            f"INSERT INTO {relation} VALUES (1, 'one'), (-1, 'minus-one');"
            f"CREATE TABLE {barrier} (id integer PRIMARY KEY, value integer);"
            f"INSERT INTO {barrier} VALUES (1, 0);"
            "SELECT local_cache.attach_table("
            f"'{relation}'::regclass, false, '{namespace}');"
            f"GRANT SELECT, UPDATE ON TABLE {relation} TO {quoted_app_role};"
            f"GRANT SELECT, UPDATE ON TABLE {barrier} TO {quoted_app_role};"
            f"GRANT USAGE ON SCHEMA local_cache TO {quoted_app_role};"
            "GRANT EXECUTE ON FUNCTION local_cache.get(regclass, text[]) "
            f"TO {quoted_app_role};"
            "GRANT EXECUTE ON FUNCTION local_cache.get(regclass, anyelement) "
            f"TO {quoted_app_role};"
            "GRANT EXECUTE ON FUNCTION local_cache.mget(regclass, anyarray) "
            f"TO {quoted_app_role}"
        )

        # A literal PK lookup is eligible even on a tiny table.  The original
        # unique IndexScan remains the child used for every safe fallback.
        assert_custom_scan(
            app_sql(
                f"EXPLAIN (COSTS OFF) SELECT value FROM {relation} WHERE id = 1"
            )
        )

        # The first ordinary SELECT safely self-fills from PostgreSQL; the
        # second one is returned from the shared positive cache.
        admin_sql(f"SELECT local_cache.invalidate('{namespace}')")
        before = stats()
        assert app_sql(f"SELECT value FROM {relation} WHERE id = 1") == "one"
        assert app_sql(f"SELECT value FROM {relation} WHERE id = 1") == "one"
        assert_counter_delta(
            before,
            stats(),
            sql_cache_hits=1,
            sql_cache_misses=1,
            sql_cache_fills=1,
        )
        expected_row_json = '{"id":1,"value":"one"}'

        # The fast scalar overload and batch API keep PostgreSQL array order,
        # duplicates, and NULLs without requiring a non-SQL client protocol.
        admin_sql(f"SELECT local_cache.invalidate('{namespace}')")
        before = stats()
        scalar_rows = app_script(
            "SET plan_cache_mode = force_generic_plan;\n"
            f"PREPARE pglc_scalar_{suffix}(regclass, bigint) AS "
            "SELECT local_cache.get($1, $2);\n"
            f"EXECUTE pglc_scalar_{suffix}('{relation}', 1);\n"
            f"EXECUTE pglc_scalar_{suffix}('{relation}', 1);\n"
            f"DEALLOCATE pglc_scalar_{suffix};\n"
        ).splitlines()
        assert scalar_rows == [expected_row_json, expected_row_json], scalar_rows
        assert_counter_delta(
            before,
            stats(),
            sql_cache_hits=1,
            sql_cache_misses=1,
            sql_cache_fills=1,
        )

        # Ordinary PostgreSQL SQL is the native tuple API.  The same prepared
        # statement can expand the whole row or project selected columns.
        admin_sql(f"SELECT local_cache.invalidate('{namespace}')")
        before = stats()
        tuple_rows = app_script(
            "SET plan_cache_mode = force_generic_plan;\n"
            f"PREPARE pglc_row_{suffix}(bigint) AS "
            f"SELECT * FROM {relation} WHERE id = $1;\n"
            f"EXECUTE pglc_row_{suffix}(1);\n"
            f"EXECUTE pglc_row_{suffix}(1);\n"
            f"DEALLOCATE pglc_row_{suffix};\n"
        ).splitlines()
        assert tuple_rows == ["1|one", "1|one"], tuple_rows
        assert_counter_delta(
            before,
            stats(),
            sql_cache_hits=1,
            sql_cache_misses=1,
            sql_cache_fills=1,
        )

        # Ordinary IN / = ANY(array) queries use the same transparent tuple
        # API.  A cold batch falls back atomically to PostgreSQL's scalar-array
        # index scan and fills every returned key; the next batch is all-cache.
        admin_sql(f"SELECT local_cache.invalidate('{namespace}')")
        array_plan = app_sql(
            f"EXPLAIN (COSTS OFF) SELECT value FROM {relation} "
            "WHERE id IN (1, -1, 1, NULL)"
        )
        assert_custom_scan(array_plan)
        assert "Lookup Mode: primary-key array" in array_plan, array_plan
        before = stats()
        literal_first = app_sql(
            f"SELECT value FROM {relation} WHERE id IN (1, -1, 1, NULL)"
        ).splitlines()
        literal_second = app_sql(
            f"SELECT value FROM {relation} WHERE id IN (1, -1, 1, NULL)"
        ).splitlines()
        assert sorted(literal_first) == ["minus-one", "one"], literal_first
        assert sorted(literal_second) == ["minus-one", "one"], literal_second
        assert_counter_delta(
            before,
            stats(),
            sql_cache_hits=2,
            sql_cache_misses=2,
            sql_cache_fills=2,
        )

        # A generic prepared plan keeps the array as PARAM_EXTERN.  Duplicate
        # and NULL elements retain PostgreSQL semantics: each table row appears
        # at most once and NULL never creates a result row.
        admin_sql(f"SELECT local_cache.invalidate('{namespace}')")
        before = stats()
        any_rows = app_script(
            "SET plan_cache_mode = force_generic_plan;\n"
            f"PREPARE pglc_any_{suffix}(bigint[]) AS "
            f"SELECT value FROM {relation} WHERE id = ANY($1);\n"
            f"EXECUTE pglc_any_{suffix}(ARRAY[1, -1, 1, NULL]::bigint[]);\n"
            f"EXECUTE pglc_any_{suffix}(ARRAY[1, -1, 1, NULL]::bigint[]);\n"
            f"DEALLOCATE pglc_any_{suffix};\n"
        ).splitlines()
        assert sorted(any_rows[:2]) == ["minus-one", "one"], any_rows
        assert sorted(any_rows[2:]) == ["minus-one", "one"], any_rows
        assert_counter_delta(
            before,
            stats(),
            sql_cache_hits=2,
            sql_cache_misses=2,
            sql_cache_fills=2,
        )

        # Positive-only caching is deliberately all-or-nothing for transparent
        # arrays.  One absent key makes the whole statement use PostgreSQL so a
        # partial cache result can never duplicate or omit a row.
        before = stats()
        mixed_first = app_sql(
            f"SELECT value FROM {relation} "
            f"WHERE id = ANY(ARRAY[1, {missing_id}]::bigint[])"
        ).splitlines()
        mixed_second = app_sql(
            f"SELECT value FROM {relation} "
            f"WHERE id = ANY(ARRAY[1, {missing_id}]::bigint[])"
        ).splitlines()
        assert mixed_first == ["one"], mixed_first
        assert mixed_second == ["one"], mixed_second
        assert_counter_delta(before, stats(), sql_cache_misses=2)

        # Empty and NULL arrays return no rows without touching cache counters.
        before = stats()
        assert (
            app_sql(
                f"SELECT value FROM {relation} "
                "WHERE id = ANY(ARRAY[]::bigint[])"
            )
            == ""
        )
        assert (
            app_sql(
                f"SELECT value FROM {relation} "
                "WHERE id = ANY(NULL::bigint[])"
            )
            == ""
        )
        assert_counter_delta(before, stats())

        # local_cache.mget() keeps JSON in the backend-local row cache.  An
        # ordinary tuple SELECT in the same backend must replace that local
        # representation from the shared payload instead of treating JSON as a
        # composite Datum.
        admin_sql(f"SELECT local_cache.invalidate('{namespace}')")
        before = stats()
        json_then_tuple = app_script(
            "SET plan_cache_mode = force_generic_plan;\n"
            "SELECT array_to_json(local_cache.mget("
            f"'{relation}'::regclass, ARRAY[1, -1]::bigint[]))::text;\n"
            f"SELECT value FROM {relation} "
            "WHERE id = ANY(ARRAY[1, -1]::bigint[]);\n"
        ).splitlines()
        assert json.loads(json_then_tuple[0]) == [
            expected_row_json,
            '{"id":-1,"value":"minus-one"}',
        ], json_then_tuple
        assert sorted(json_then_tuple[1:]) == ["minus-one", "one"], (
            json_then_tuple
        )
        assert_counter_delta(
            before,
            stats(),
            sql_cache_hits=2,
            sql_cache_misses=2,
            sql_cache_fills=2,
        )

        admin_sql(f"SELECT local_cache.invalidate('{namespace}')")
        before = stats()
        batch_rows = app_script(
            "SET plan_cache_mode = force_generic_plan;\n"
            f"PREPARE pglc_mget_{suffix}(regclass, bigint[]) AS "
            "SELECT array_to_json(local_cache.mget($1, $2))::text;\n"
            f"EXECUTE pglc_mget_{suffix}('{relation}', "
            f"ARRAY[1, -1, 1, NULL, {missing_id}]::bigint[]);\n"
            f"EXECUTE pglc_mget_{suffix}('{relation}', "
            f"ARRAY[1, -1, 1, NULL, {missing_id}]::bigint[]);\n"
            f"DEALLOCATE pglc_mget_{suffix};\n"
        ).splitlines()
        expected_batch = [
            expected_row_json,
            '{"id":-1,"value":"minus-one"}',
            expected_row_json,
            None,
            None,
        ]
        assert [json.loads(row) for row in batch_rows] == [
            expected_batch,
            expected_batch,
        ], batch_rows
        assert_counter_delta(
            before,
            stats(),
            sql_cache_hits=4,
            sql_cache_misses=4,
            sql_cache_fills=3,
        )

        # The canonical KV API stays inside ordinary PostgreSQL SQL.  Its
        # first execution reads the attached source and fills; the second
        # returns the byte-identical whole-row JSON directly from the cache.
        admin_sql(f"SELECT local_cache.invalidate('{namespace}')")
        before = stats()
        kv_rows = app_script(
            "SET plan_cache_mode = force_generic_plan;\n"
            f"PREPARE pglc_get_{suffix}(regclass, text[]) AS "
            "SELECT local_cache.get($1, $2);\n"
            f"EXECUTE pglc_get_{suffix}('{relation}', ARRAY['1']);\n"
            f"EXECUTE pglc_get_{suffix}('{relation}', ARRAY['1']);\n"
            f"DEALLOCATE pglc_get_{suffix};\n"
        ).splitlines()
        assert kv_rows == [expected_row_json, expected_row_json], kv_rows
        assert app_sql(
            f"SELECT row_to_json(pglc_source)::text FROM {relation} "
            "AS pglc_source WHERE id = 1"
        ) == expected_row_json
        assert_counter_delta(
            before,
            stats(),
            sql_cache_hits=1,
            sql_cache_misses=1,
            sql_cache_fills=1,
        )

        # bigint = int4 is PostgreSQL's normal parse for uncast integer
        # literals.  Both signs must use a real widening coercion before the
        # key output function; reinterpreting an int4 Datum as int8 is unsafe.
        assert_custom_scan(
            app_sql(
                f"EXPLAIN (COSTS OFF) SELECT value FROM {relation} WHERE id = -1"
            )
        )
        before = stats()
        assert app_sql(f"SELECT value FROM {relation} WHERE id = -1") == "minus-one"
        assert app_sql(f"SELECT value FROM {relation} WHERE id = -1") == "minus-one"
        assert_counter_delta(
            before,
            stats(),
            sql_cache_hits=1,
            sql_cache_misses=1,
            sql_cache_fills=1,
        )

        # External Params and the common ORM-style literal LIMIT 1 retain the
        # CustomScan and use the already warm cache entry.
        prepared_plan = app_script(
            "SET plan_cache_mode = force_generic_plan;\n"
            f"PREPARE pglc_param_{suffix}(bigint) AS "
            f"SELECT value FROM {relation} WHERE id = $1 LIMIT 1;\n"
            f"EXPLAIN (COSTS OFF) EXECUTE pglc_param_{suffix}(1);\n"
            f"DEALLOCATE pglc_param_{suffix};\n"
        )
        assert_custom_scan(prepared_plan)
        before = stats()
        assert app_script(
            "SET plan_cache_mode = force_generic_plan;\n"
            f"PREPARE pglc_param_{suffix}(bigint) AS "
            f"SELECT value FROM {relation} WHERE id = $1 LIMIT 1;\n"
            f"EXECUTE pglc_param_{suffix}(1);\n"
            f"DEALLOCATE pglc_param_{suffix};\n"
        ) == "one"
        assert_counter_delta(before, stats(), sql_cache_hits=1)

        # Common drivers explicitly bind a 32-bit parameter even when the PK
        # is bigint.  The btree integer opfamily supports the comparison and
        # the cache expression widens the Param losslessly.
        int4_plan = app_script(
            "SET plan_cache_mode = force_generic_plan;\n"
            f"PREPARE pglc_int4_{suffix}(integer) AS "
            f"SELECT value FROM {relation} WHERE id = $1;\n"
            f"EXPLAIN (COSTS OFF) EXECUTE pglc_int4_{suffix}(-1);\n"
            f"DEALLOCATE pglc_int4_{suffix};\n"
        )
        assert_custom_scan(int4_plan)
        before = stats()
        assert app_script(
            "SET plan_cache_mode = force_generic_plan;\n"
            f"PREPARE pglc_int4_{suffix}(integer) AS "
            f"SELECT value FROM {relation} WHERE id = $1;\n"
            f"EXECUTE pglc_int4_{suffix}(-1);\n"
            f"DEALLOCATE pglc_int4_{suffix};\n"
        ) == "minus-one"
        assert_counter_delta(before, stats(), sql_cache_hits=1)

        # The USERSET kill switch produces an ordinary PostgreSQL plan and
        # does not mislabel a planner-ineligible query as a runtime bypass.
        before = stats()
        guc_off_output = app_script(
            "SET pg_local_cache.sql_cache = off;\n"
            f"SELECT value FROM {relation} WHERE id = 1;\n"
            f"EXPLAIN (COSTS OFF) SELECT value FROM {relation} WHERE id = 1;\n"
        )
        guc_off_lines = guc_off_output.splitlines()
        assert guc_off_lines[0] == "one", guc_off_output
        assert_no_custom_scan("\n".join(guc_off_lines[1:]))
        assert_counter_delta(before, stats())

        # Reuse a plan prepared while the transaction is clean, then write.
        # Runtime dirty detection must fall back to the child IndexScan so the
        # transaction reads its own tuple.  ROLLBACK keeps the old cache valid.
        before = stats()
        rollback_values = app_script(
            "SET plan_cache_mode = force_generic_plan;\n"
            f"PREPARE pglc_rollback_{suffix}(bigint) AS "
            f"SELECT value FROM {relation} WHERE id = $1;\n"
            f"EXECUTE pglc_rollback_{suffix}(1);\n"
            "BEGIN;\n"
            f"UPDATE {relation} SET value = 'own-write' WHERE id = 1;\n"
            f"EXECUTE pglc_rollback_{suffix}(1);\n"
            "ROLLBACK;\n"
            f"DEALLOCATE pglc_rollback_{suffix};\n"
        ).splitlines()
        assert rollback_values == ["one", "own-write"], rollback_values
        assert_counter_delta(
            before, stats(), sql_cache_hits=1, sql_cache_bypasses=1
        )
        assert admin_sql(f"SELECT value FROM {relation} WHERE id = 1") == "one"
        assert app_sql(f"SELECT value FROM {relation} WHERE id = 1") == "one"

        # The same clean prepared SQL KV functions must bypass after a write
        # and read the transaction's own tuple.  ROLLBACK preserves old data.
        before = stats()
        kv_rollback = app_script(
            "SET plan_cache_mode = force_generic_plan;\n"
            f"PREPARE pglc_get_rollback_{suffix}(regclass, bigint) AS "
            "SELECT local_cache.get($1, $2);\n"
            f"PREPARE pglc_mget_rollback_{suffix}(regclass, bigint[]) AS "
            "SELECT array_to_json(local_cache.mget($1, $2))::text;\n"
            f"EXECUTE pglc_get_rollback_{suffix}('{relation}', 1);\n"
            "BEGIN;\n"
            f"UPDATE {relation} SET value = 'own-write' WHERE id = 1;\n"
            f"EXECUTE pglc_get_rollback_{suffix}('{relation}', 1);\n"
            f"EXECUTE pglc_mget_rollback_{suffix}('{relation}', "
            "ARRAY[1, -1]::bigint[]);\n"
            "ROLLBACK;\n"
            f"DEALLOCATE pglc_get_rollback_{suffix};\n"
            f"DEALLOCATE pglc_mget_rollback_{suffix};\n"
        ).splitlines()
        assert kv_rollback[:2] == [
            expected_row_json,
            '{"id":1,"value":"own-write"}',
        ], kv_rollback
        assert json.loads(kv_rollback[2]) == [
            '{"id":1,"value":"own-write"}',
            '{"id":-1,"value":"minus-one"}',
        ], kv_rollback
        assert_counter_delta(
            before, stats(), sql_cache_hits=1, sql_cache_bypasses=3
        )
        assert app_sql(
            f"SELECT local_cache.get('{relation}'::regclass, 1::bigint)"
        ) == expected_row_json

        # A committed writer publishes the invalidation before its new tuple
        # can become visible.  The next scalar GET therefore refills, then
        # both GET and MGET can hit the committed whole-row value.
        assert (
            app_sql(
                f"UPDATE {relation} SET value = 'committed' WHERE id = 1 "
                "RETURNING value"
            )
            == "committed"
        )
        before = stats()
        expected_committed_json = '{"id":1,"value":"committed"}'
        assert app_sql(
            f"SELECT local_cache.get('{relation}'::regclass, 1::bigint)"
        ) == expected_committed_json
        assert app_sql(
            f"SELECT local_cache.get('{relation}'::regclass, 1::bigint)"
        ) == expected_committed_json
        assert json.loads(
            app_sql(
                "SELECT array_to_json(local_cache.mget("
                f"'{relation}'::regclass, ARRAY[1]::bigint[]))::text"
            )
        ) == [expected_committed_json]
        assert_counter_delta(
            before,
            stats(),
            sql_cache_hits=2,
            sql_cache_misses=1,
            sql_cache_fills=1,
        )

        # A CustomScan planned in READ COMMITTED may be reused inside an older
        # snapshot transaction.  It must runtime-bypass instead of exposing a
        # value that is not justified by the REPEATABLE READ snapshot.
        before = stats()
        repeatable_values = app_script(
            "SET plan_cache_mode = force_generic_plan;\n"
            f"PREPARE pglc_rr_{suffix}(bigint) AS "
            f"SELECT value FROM {relation} WHERE id = $1;\n"
            f"PREPARE pglc_get_rr_{suffix}(regclass, bigint) AS "
            "SELECT local_cache.get($1, $2);\n"
            f"PREPARE pglc_mget_rr_{suffix}(regclass, bigint[]) AS "
            "SELECT array_to_json(local_cache.mget($1, $2))::text;\n"
            f"EXECUTE pglc_rr_{suffix}(1);\n"
            "BEGIN ISOLATION LEVEL REPEATABLE READ;\n"
            f"EXECUTE pglc_rr_{suffix}(1);\n"
            f"EXECUTE pglc_get_rr_{suffix}('{relation}', 1);\n"
            f"EXECUTE pglc_mget_rr_{suffix}('{relation}', ARRAY[1]::bigint[]);\n"
            "COMMIT;\n"
            f"DEALLOCATE pglc_rr_{suffix};\n"
            f"DEALLOCATE pglc_get_rr_{suffix};\n"
            f"DEALLOCATE pglc_mget_rr_{suffix};\n"
        ).splitlines()
        assert repeatable_values[:3] == [
            "committed",
            "committed",
            expected_committed_json,
        ], repeatable_values
        assert json.loads(repeatable_values[3]) == [expected_committed_json]
        assert_counter_delta(
            before, stats(), sql_cache_hits=1, sql_cache_bypasses=3
        )

        # A latest-snapshot negative entry cannot prove absence for a
        # statement whose READ COMMITTED snapshot was taken before a concurrent
        # DELETE or primary-key move.  SQL GET and MGET must execute the source
        # plan so PostgreSQL can return the older visible row version.
        negative_delete_id = missing_id + 1
        negative_move_id = missing_id + 2
        negative_moved_id = missing_id + 3
        admin_sql(
            f"INSERT INTO {relation} VALUES "
            f"({negative_delete_id}, 'delete-visible'), "
            f"({negative_move_id}, 'move-visible')"
        )
        assert_negative_cache_respects_old_snapshot(
            relation=relation,
            barrier=barrier,
            source_id=negative_delete_id,
            mutation_sql=(
                f"DELETE FROM {relation} WHERE id = {negative_delete_id}"
            ),
            expected_json=(
                f'{{"id":{negative_delete_id},"value":"delete-visible"}}'
            ),
            suffix=suffix,
            case="delete",
        )
        assert_negative_cache_respects_old_snapshot(
            relation=relation,
            barrier=barrier,
            source_id=negative_move_id,
            mutation_sql=(
                f"UPDATE {relation} SET id = {negative_moved_id} "
                f"WHERE id = {negative_move_id}"
            ),
            expected_json=(
                f'{{"id":{negative_move_id},"value":"move-visible"}}'
            ),
            suffix=suffix,
            case="pk_move",
        )

        if RESP_PORT != 0:
            # RESP negative entries are never authoritative for ordinary SQL.
            client = RespClient()
            wait_for_negative_entry(
                client,
                f'CRUD:{PGDATABASE}.public.{table}:{{"id":{missing_id}}}',
            )

        # With or without a RESP listener, a missing SQL row must execute
        # PostgreSQL's child plan and remain a cache miss.  In the RESP-enabled
        # profile the preceding probe also proves that a cached negative entry
        # is never authoritative for ordinary SQL.
        before = stats()
        assert (
            app_sql(f"SELECT value FROM {relation} WHERE id = {missing_id}")
            == ""
        )
        assert_counter_delta(before, stats(), sql_cache_misses=1)

        # A statement can take its READ COMMITTED snapshot, block, and then
        # execute GET after another transaction commits.  Return the old
        # snapshot result, but never publish it as a current negative entry.
        admin_sql(f"SELECT local_cache.invalidate('{namespace}')")
        marker = f"pglc_stale_fill_{suffix}"
        writer = subprocess.Popen(
            psql_base_args(application=False),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert writer.stdin is not None
        writer.stdin.write(
            "BEGIN;\n"
            f"UPDATE {barrier} SET value = value + 1 WHERE id = 1;\n"
            f"SELECT pg_sleep(1) /* {marker} */;\n"
            f"INSERT INTO {relation} VALUES ({missing_id}, 'concurrent');\n"
            "COMMIT;\n"
        )
        writer.stdin.close()
        deadline = time.monotonic() + 5
        while admin_sql(
            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_stat_activity "
            f"WHERE query LIKE '%{marker}%' AND wait_event = 'PgSleep')"
        ) != "t":
            assert time.monotonic() < deadline, "writer did not reach stale-fill gate"
            time.sleep(0.02)

        before = stats()
        reader = subprocess.Popen(
            psql_base_args(application=True)
            + [
                "-c",
                "WITH gate AS MATERIALIZED ("
                f"SELECT id FROM {barrier} WHERE id = 1 FOR UPDATE) "
                "SELECT local_cache.get("
                f"'{relation}'::regclass, {missing_id}::bigint) FROM gate",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "PGPASSWORD": APP_PASSWORD},
        )
        writer_output = writer.stdout.read() if writer.stdout is not None else ""
        assert writer.wait(timeout=10) == 0, writer_output
        reader_output, _ = reader.communicate(timeout=10)
        assert reader.returncode == 0, reader_output
        assert reader_output.strip() == "", reader_output

        concurrent_json = (
            f'{{"id":{missing_id},"value":"concurrent"}}'
        )
        assert app_sql(
            f"SELECT local_cache.get('{relation}'::regclass, {missing_id}::bigint)"
        ) == concurrent_json
        assert app_sql(
            f"SELECT local_cache.get('{relation}'::regclass, {missing_id}::bigint)"
        ) == concurrent_json
        assert_counter_delta(
            before,
            stats(),
            sql_cache_hits=1,
            sql_cache_misses=2,
            sql_cache_fills=1,
        )

        # Additional predicates retain stock PostgreSQL semantics and do not
        # affect SQL-cache counters.
        before = stats()
        unsupported = app_sql(
            f"SELECT value FROM {relation} "
            "WHERE id = 1 AND value = 'committed'"
        )
        assert unsupported == "committed", unsupported
        assert_counter_delta(before, stats())
        assert_no_custom_scan(
            app_sql(
                f"EXPLAIN (COSTS OFF) SELECT value FROM {relation} "
                "WHERE id = 1 AND value = 'committed'"
            )
        )

        # The transparent hit still passes PostgreSQL's normal relation ACL
        # checks.  Revoking SELECT must fail before the custom executor can
        # return an otherwise warm entry.
        admin_sql(f"REVOKE SELECT ON TABLE {relation} FROM {quoted_app_role}")
        before = stats()
        app_sql_fails(
            f"SELECT value FROM {relation} WHERE id = 1",
            "permission denied",
        )
        app_sql_fails(
            f"SELECT local_cache.get('{relation}'::regclass, 1::bigint)",
            "permission denied",
        )
        app_sql_fails(
            "SELECT local_cache.mget("
            f"'{relation}'::regclass, ARRAY[1]::bigint[])",
            "permission denied",
        )
        assert_counter_delta(before, stats())

        # A cached relation-validation token must be cleared when a parent
        # gains an inherited child.  The next ordinary SELECT in the same
        # backend must see both rows; after DROP, the fast path may recover.
        admin_sql(
            f"CREATE SCHEMA {inheritance_schema} AUTHORIZATION {quoted_app_role};"
            f"CREATE TABLE {inheritance_parent} ("
            "id bigint PRIMARY KEY, value text NOT NULL);"
            f"ALTER TABLE {inheritance_parent} OWNER TO {quoted_app_role};"
            f"INSERT INTO {inheritance_parent} VALUES (1, 'parent');"
            "SELECT local_cache.attach_table("
            f"'{inheritance_parent}'::regclass, false, "
            f"'{inheritance_namespace}')"
        )
        before = stats()
        inheritance_values = app_script(
            f"SELECT value FROM {inheritance_parent} WHERE id = 1;\n"
            f"CREATE TABLE {inheritance_child} () "
            f"INHERITS ({inheritance_parent});\n"
            f"INSERT INTO {inheritance_child} VALUES (1, 'child');\n"
            f"SELECT value FROM {inheritance_parent} WHERE id = 1;\n"
            f"DROP TABLE {inheritance_child};\n"
            f"SELECT value FROM {inheritance_parent} WHERE id = 1;\n"
        ).splitlines()
        assert inheritance_values[0] == "parent", inheritance_values
        assert sorted(inheritance_values[1:3]) == ["child", "parent"], (
            inheritance_values
        )
        assert inheritance_values[3] == "parent", inheritance_values
        assert_counter_delta(
            before,
            stats(),
            sql_cache_hits=0,
            sql_cache_misses=2,
            sql_cache_fills=2,
        )

        print(
            "ok: transparent SQL cold fill/hit, ordinary IN/ANY batches, "
            "Param and LIMIT 1, EXPLAIN, "
            "GUC fallback, transactional read-your-writes/rollback, "
            "JSON GET/MGET, commit and inheritance invalidation, "
            "old-snapshot and negative MVCC fallback, "
            "unsupported-shape fallback, and NOSUPERUSER "
            "ACL enforcement"
        )
    finally:
        if client is not None:
            client.close()
        # Restore every privilege granted by this test.  docker_smoke reuses
        # the application role in later least-privilege checks, so leaking
        # schema or function ACLs makes their result depend on test order.
        subprocess.run(
            psql_base_args(application=False),
            input=(
                "REVOKE EXECUTE ON FUNCTION "
                "local_cache.get(regclass, text[]) "
                f"FROM {quoted_app_role};\n"
                "REVOKE EXECUTE ON FUNCTION "
                "local_cache.get(regclass, anyelement) "
                f"FROM {quoted_app_role};\n"
                "REVOKE EXECUTE ON FUNCTION "
                "local_cache.mget(regclass, anyarray) "
                f"FROM {quoted_app_role};\n"
                "REVOKE USAGE ON SCHEMA local_cache "
                f"FROM {quoted_app_role};\n"
                "DO $cleanup$\n"
                "BEGIN\n"
                f"  IF pg_catalog.to_regclass('{relation}') IS NOT NULL THEN\n"
                f"    EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE {relation} "
                f"FROM {quoted_app_role}';\n"
                "  END IF;\n"
                "END\n"
                "$cleanup$;\n"
                f"DROP TABLE IF EXISTS {relation};\n"
                f"DROP TABLE IF EXISTS {barrier};\n"
                f"DROP SCHEMA IF EXISTS {inheritance_schema} CASCADE;\n"
            ),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )


if __name__ == "__main__":
    main()
