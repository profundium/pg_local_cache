#!/usr/bin/env python3
"""Black-box whole-row RESP coverage."""

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
APP_ROLE = os.environ.get("PG_LOCAL_CACHE_TEST_APP_ROLE", "")
APP_PASSWORD = os.environ.get("PG_LOCAL_CACHE_TEST_APP_PASSWORD", "")
APP_HOST = os.environ.get("PG_LOCAL_CACHE_TEST_APP_HOST", "127.0.0.1")

SAFE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,62}\Z")
KVIK_STAT_NAMES = {
    "store_size",
    "store_memory",
    "client_connect",
    "client_disconnect",
    "client_requests",
    "client_request_errors",
    "client_gets",
    "client_sets",
    "client_dels",
    "cache_hit",
    "cache_hit_in_main",
    "cache_miss",
    "cache_neg_write_count",
    "cache_evict",
    "cache_invalidate_entry",
    "cache_invalidate_table",
    "pass_to_main",
    "sql_meta",
    "sql_gets",
    "sql_sets",
    "sql_dels",
    "sql_result_reuses",
}

if not SAFE_IDENTIFIER.fullmatch(APP_ROLE):
    raise ValueError(
        "PG_LOCAL_CACHE_TEST_APP_ROLE is required and must be a safe SQL identifier"
    )
if not APP_PASSWORD:
    raise ValueError("PG_LOCAL_CACHE_TEST_APP_PASSWORD is required")
if not AUTH_TOKEN:
    raise ValueError("PG_LOCAL_CACHE_AUTH_TOKEN is required")


class RespError(RuntimeError):
    """An error returned by the embedded RESP endpoint."""


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
            value if isinstance(value, bytes) else str(value).encode()
            for value in arguments
        ]
        request = [f"*{len(encoded)}\r\n".encode()]
        for value in encoded:
            request.extend((f"${len(value)}\r\n".encode(), value, b"\r\n"))
        self.socket.sendall(b"".join(request))
        return self._read_response()

    def _read_response(self) -> object:
        prefix = self.stream.read(1)
        if not prefix:
            raise EOFError("RESP connection closed")
        line = self.stream.readline() if prefix in (b"+", b"-", b":") else None
        if line is not None:
            if not line.endswith(b"\r\n"):
                raise ValueError("invalid RESP line")
            text = line[:-2].decode("utf-8", "replace")
            if prefix == b"-":
                raise RespError(text)
            return int(text) if prefix == b":" else text
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
        if prefix == b"*":
            length_line = self.stream.readline()
            if not length_line.endswith(b"\r\n"):
                raise ValueError("invalid RESP array length")
            length = int(length_line[:-2])
            if length == -1:
                return None
            return [self._read_response() for _ in range(length)]
        raise ValueError(f"unsupported RESP prefix {prefix!r}")


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def psql_arguments(*, application: bool, script: bool) -> list[str]:
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
        # These later options override admin defaults embedded by docker_smoke.
        arguments.extend(("-h", APP_HOST, "-U", APP_ROLE))
    if script:
        arguments.extend(("-f", "-"))
    return arguments


def run_psql(
    statement: str, *, application: bool, script: bool = False
) -> subprocess.CompletedProcess[str]:
    arguments = psql_arguments(application=application, script=script)
    if not script:
        arguments.extend(("-c", statement))
    environment = os.environ.copy()
    if application:
        environment["PGPASSWORD"] = APP_PASSWORD
    return subprocess.run(
        arguments,
        input=statement if script else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        timeout=45,
    )


def checked_psql(statement: str, *, application: bool, script: bool = False) -> str:
    result = run_psql(statement, application=application, script=script)
    diagnostics = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part.strip()
    )
    assert result.returncode == 0, diagnostics
    return result.stdout.strip()


def admin_sql(statement: str) -> str:
    return checked_psql(statement, application=False)


def app_sql(statement: str) -> str:
    return checked_psql(statement, application=True)


def app_script(statement: str) -> str:
    return checked_psql(statement, application=True, script=True)


def stat(client: RespClient) -> dict[str, int]:
    result = client.command("STAT")
    assert isinstance(result, str), result
    value = json.loads(result)
    assert isinstance(value, dict), value
    return {str(name): int(counter) for name, counter in value.items()}


def crud_key(table: str, tenant_id: int, row_id: int) -> str:
    # Reversed JSON member order proves keys are canonicalized in PK order.
    return (
        f"CRUD:{PGDATABASE}.public.{table}:"
        + json.dumps(
            {"id": row_id, "tenant_id": tenant_id},
            separators=(",", ":"),
        )
    )


def single_key(table: str, row_id: int) -> str:
    return (
        f"CRUD:{PGDATABASE}.public.{table}:"
        + json.dumps({"id": row_id}, separators=(",", ":"))
    )


def wait_for_json(client: RespClient, key: str) -> dict[str, object]:
    deadline = time.monotonic() + 10
    while True:
        try:
            response = client.command("GET", key)
            assert isinstance(response, str), response
            parsed = json.loads(response)
            assert isinstance(parsed, dict), parsed
            return parsed
        except RespError as error:
            if "unknown" not in str(error).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def wait_for_absent(client: RespClient, key: str) -> None:
    deadline = time.monotonic() + 10
    while True:
        try:
            response = client.command("GET", key)
            assert response is None, response
            return
        except RespError as error:
            if "unknown" not in str(error).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def assert_resp_error(
    client: RespClient, expected: str, *arguments: object
) -> None:
    try:
        client.command(*arguments)
    except RespError as error:
        assert expected.lower() in str(error).lower(), str(error)
        return
    raise AssertionError(f"RESP command unexpectedly succeeded: {arguments!r}")


def assert_row_one(row: dict[str, object], payload: str = "row-one") -> None:
    assert row == {
        "tenant_id": 7,
        "id": 1,
        "payload": payload,
        "amount": 12.50,
        "enabled": True,
        "metadata": {"kind": "alpha", "nested": {"ok": True}},
        "note": None,
    }, row


def assert_invalidation_refills(
    client: RespClient, scope: str, key: str, expected_payload: str
) -> None:
    before = stat(client)
    removed = client.command("INVALIDATE", scope)
    assert isinstance(removed, int) and removed >= 0, removed
    row = wait_for_json(client, key)
    assert row["payload"] == expected_payload, row
    after = stat(client)
    assert after["database_reads"] - before["database_reads"] == 1, {
        "scope": scope,
        "before": before,
        "after": after,
    }


def main() -> None:
    suffix = str(os.getpid())
    table = f"pglc_whole_row_{suffix}"
    relation = f"public.{table}"
    namespace = f"whole{suffix}"
    identity_table = f"pglc_identity_row_{suffix}"
    identity_namespace = f"identity{suffix}"
    enum_type = f"pglc_row_state_{suffix}"
    enum_table = f"pglc_enum_row_{suffix}"
    enum_namespace = f"rowenum{suffix}"
    quoted_app_role = sql_identifier(APP_ROLE)
    key_one = crud_key(table, 7, 1)
    missing_key = crud_key(table, 7, 9_000_000_000 + os.getpid())
    moved_key = crud_key(table, 8, 11)
    client: RespClient | None = None

    attached = json.loads(
        admin_sql(
            f"CREATE TABLE {relation} ("
            "tenant_id bigint NOT NULL, id bigint NOT NULL, "
            "payload text NOT NULL, amount numeric(18,2), "
            "enabled boolean NOT NULL, metadata jsonb NOT NULL, note text, "
            "PRIMARY KEY (tenant_id, id));"
            f"INSERT INTO {relation} VALUES ("
            "7, 1, 'row-one', 12.50, true, "
            "'{\"kind\":\"alpha\",\"nested\":{\"ok\":true}}', NULL);"
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {relation} "
            f"TO {quoted_app_role};"
            f"SELECT local_cache.attach_table("
            f"'{relation}'::regclass, true, '{namespace}')"
        )
    )
    assert attached["whole_row"] is True, attached
    assert attached["primary_key_columns"] == ["tenant_id", "id"], attached
    assert attached["writable"] is True, attached
    assert attached["templates"]["key"].startswith(
        f"CRUD:{PGDATABASE}.public.{table}:"
    ), attached

    try:
        client = RespClient()

        # Full native row types, including NULL, numeric, bool, and jsonb.
        assert_row_one(wait_for_json(client, key_one))
        assert_row_one(wait_for_json(client, key_one))

        # A negative entry suppresses duplicate PostgreSQL reads for RESP.
        before_missing = stat(client)
        assert client.command("GET", missing_key) is None
        assert client.command("GET", missing_key) is None
        after_missing = stat(client)
        assert after_missing["database_reads"] - before_missing["database_reads"] == 1
        assert after_missing["negative_hits"] - before_missing["negative_hits"] >= 1
        # A committed insert invalidates the negative entry before the new row
        # is visible, so GET cannot remain falsely empty.
        missing_id = 9_000_000_000 + os.getpid()
        app_sql(
            f"INSERT INTO {relation} VALUES (7, {missing_id}, 'after-negative', "
            "1.00, true, '{\"kind\":\"negative-refresh\"}', NULL)"
        )
        assert wait_for_json(client, missing_key)["payload"] == "after-negative"
        app_sql(
            f"DELETE FROM {relation} WHERE tenant_id = 7 AND id = {missing_id}"
        )

        # SET accepts an omitted PK (the wire key is authoritative), accepts a
        # matching PK, and fails closed on mismatch or an unknown field.
        key_two = crud_key(table, 7, 2)
        omitted_pk = {
            "payload": "created-with-wire-pk",
            "amount": 3.25,
            "enabled": False,
            "metadata": {"kind": "created"},
            "note": None,
        }
        assert client.command("SET", key_two, json.dumps(omitted_pk)) == "OK"
        created = wait_for_json(client, key_two)
        assert created["tenant_id"] == 7 and created["id"] == 2, created
        assert created["payload"] == "created-with-wire-pk", created
        matching_pk = dict(omitted_pk, tenant_id=7, id=2, payload="matching-pk")
        assert client.command("SET", key_two, json.dumps(matching_pk)) == "OK"
        assert wait_for_json(client, key_two)["payload"] == "matching-pk"
        assert_resp_error(
            client,
            "does not match the wire key",
            "SET",
            key_two,
            json.dumps(dict(matching_pk, id=999)),
        )
        assert_resp_error(
            client,
            "unknown column",
            "SET",
            key_two,
            json.dumps(dict(matching_pk, nonexistent="x")),
        )
        assert_resp_error(client, "invalid input syntax", "SET", key_two, "{")
        assert client.command("DEL", key_two) == 1
        assert client.command("DEL", key_two) == 0
        assert client.command("GET", key_two) is None

        # Writable rows support identity PKs and stored generated non-key
        # columns.  The wire key supplies the explicit identity value while
        # PostgreSQL recomputes generated data on every upsert.
        identity_attached = json.loads(
            admin_sql(
                f"CREATE TABLE public.{identity_table} ("
                "id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
                "base integer NOT NULL, "
                "doubled integer GENERATED ALWAYS AS (base * 2) STORED);"
                f"SELECT local_cache.attach_table("
                f"'public.{identity_table}'::regclass, "
                f"true, '{identity_namespace}')"
            )
        )
        assert identity_attached["whole_row"] is True, identity_attached
        identity_key = single_key(identity_table, 42)
        wait_for_absent(client, identity_key)
        assert client.command(
            "SET", identity_key, json.dumps({"base": 7})
        ) == "OK"
        identity_row = wait_for_json(client, identity_key)
        assert identity_row == {"id": 42, "base": 7, "doubled": 14}, identity_row
        assert client.command(
            "SET",
            identity_key,
            json.dumps({"id": 42, "base": 9, "doubled": -1}),
        ) == "OK"
        identity_row = wait_for_json(client, identity_key)
        assert identity_row == {"id": 42, "base": 9, "doubled": 18}, identity_row
        assert client.command("DEL", identity_key) == 1

        # Cached JSON depends on type output semantics.  Renaming an enum value
        # must fence the old JSON even though the mapped table itself is not DDL'd.
        enum_attached = json.loads(
            admin_sql(
                f"CREATE TYPE public.{enum_type} AS ENUM ('old_label');"
                f"CREATE TABLE public.{enum_table} ("
                f"id bigint PRIMARY KEY, state public.{enum_type} NOT NULL);"
                f"INSERT INTO public.{enum_table} VALUES (1, 'old_label');"
                f"SELECT local_cache.attach_table("
                f"'public.{enum_table}'::regclass, false, '{enum_namespace}')"
            )
        )
        assert enum_attached["whole_row"] is True, enum_attached
        enum_key = single_key(enum_table, 1)
        assert wait_for_json(client, enum_key)["state"] == "old_label"
        admin_sql(
            f"ALTER TYPE public.{enum_type} "
            "RENAME VALUE 'old_label' TO 'new_label'"
        )
        assert wait_for_json(client, enum_key)["state"] == "new_label"

        # KVik's exact/table/database/global invalidation scopes all force a
        # fresh read while preserving the transactional source of truth.
        table_scope = f"CRUD:{PGDATABASE}.public.{table}"
        assert_invalidation_refills(client, key_one, key_one, "row-one")
        assert_invalidation_refills(client, table_scope, key_one, "row-one")
        assert_invalidation_refills(client, f"CRUD:{PGDATABASE}", key_one, "row-one")
        assert_invalidation_refills(client, "CRUD", key_one, "row-one")

        app_script(
            "BEGIN;\n"
            f"UPDATE {relation} SET payload = 'committed' "
            "WHERE tenant_id = 7 AND id = 1;\n"
            "COMMIT;\n"
        )
        assert wait_for_json(client, key_one)["payload"] == "committed"
        # PK moves invalidate both OLD and NEW canonical composite keys.  A
        # rollback does not leak either tentative key; a commit publishes both
        # invalidations before the moved tuple becomes visible.
        app_script(
            "BEGIN;\n"
            f"UPDATE {relation} SET tenant_id = 8, id = 11 "
            "WHERE tenant_id = 7 AND id = 1;\n"
            "ROLLBACK;\n"
        )
        assert wait_for_json(client, key_one)["payload"] == "committed"
        assert client.command("GET", moved_key) is None
        app_script(
            "BEGIN;\n"
            f"UPDATE {relation} SET tenant_id = 8, id = 11 "
            "WHERE tenant_id = 7 AND id = 1;\n"
            "COMMIT;\n"
        )
        assert client.command("GET", key_one) is None
        moved = wait_for_json(client, moved_key)
        assert moved["tenant_id"] == 8 and moved["id"] == 11, moved
        assert moved["payload"] == "committed", moved

        # Oversized cache entries remain correct RESP results and are not admitted.
        admin_sql(
            f"INSERT INTO {relation} VALUES (99, 99, repeat('z', 12000), "
            "99.99, false, '{\"wide\":true}', NULL)"
        )
        wide_key = crud_key(table, 99, 99)
        before_wide_resp = stat(client)
        wide_row = wait_for_json(client, wide_key)
        assert wide_row["payload"] == "z" * 12000, len(str(wide_row["payload"]))
        after_wide_resp = stat(client)
        assert (
            after_wide_resp["database_reads"]
            - before_wide_resp["database_reads"]
            == 1
        )

        # Escaped JSON refills from PostgreSQL and keeps metrics truthful.
        admin_sql(
            f"INSERT INTO {relation} VALUES (98, 98, repeat(chr(92), 4000), "
            "98.00, false, '{\"escaped\":true}', NULL)"
        )
        before_escaped = stat(client)
        escaped_row = wait_for_json(client, crud_key(table, 98, 98))
        assert escaped_row["payload"] == "\\" * 4000
        after_escaped = stat(client)
        assert after_escaped["cache_hits"] == before_escaped["cache_hits"]
        assert (
            after_escaped["cache_misses"]
            - before_escaped["cache_misses"]
            == 1
        )
        assert (
            after_escaped["database_reads"]
            - before_escaped["database_reads"]
            == 1
        )

        admin_sql(
            f"INSERT INTO {relation} VALUES (100, 100, repeat('q', 100000), "
            "100.00, false, '{\"too_wide\":true}', NULL)"
        )
        assert_resp_error(
            client,
            "row JSON exceeds the RESP limit",
            "GET",
            crud_key(table, 100, 100),
        )

        # STAT exposes the full documented KVik vocabulary alongside native
        # metrics; aliases that have exact native equivalents stay equal.
        final_stats = stat(client)
        assert KVIK_STAT_NAMES.issubset(final_stats), sorted(
            KVIK_STAT_NAMES.difference(final_stats)
        )
        assert final_stats["store_size"] == (
            final_stats["positive_entries"] + final_stats["negative_entries"]
        )
        assert final_stats["cache_hit"] == final_stats["cache_hits"]
        assert final_stats["cache_miss"] == final_stats["cache_misses"]
        assert final_stats["cache_evict"] == final_stats["evictions"]
        assert final_stats["sql_gets"] == final_stats["database_reads"]

        print("whole-row/KVik integration test passed")
    finally:
        if client is not None:
            client.close()
        admin_sql(
            f"DROP TABLE IF EXISTS {relation};"
            f"DROP TABLE IF EXISTS public.{identity_table};"
            f"DROP TABLE IF EXISTS public.{enum_table};"
            f"DROP TYPE IF EXISTS public.{enum_type}"
        )


if __name__ == "__main__":
    main()
