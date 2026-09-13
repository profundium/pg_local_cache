#!/usr/bin/env python3
"""Black-box contract for the explicit local_cache.mget SQL API."""

from __future__ import annotations

import json
import os
import re
import subprocess


PSQL = os.environ.get("PG_LOCAL_CACHE_PSQL", "psql")
PGHOST = os.environ.get("PGHOST", "127.0.0.1")
PGPORT = os.environ.get("PGPORT", "5432")
PGDATABASE = os.environ.get("PGDATABASE", "postgres")
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

if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,62}", APP_ROLE):
    raise ValueError("PG_LOCAL_CACHE_TEST_APP_ROLE is required")
if not APP_PASSWORD:
    raise ValueError("PG_LOCAL_CACHE_TEST_APP_PASSWORD is required")


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def run_sql(
    query: str,
    *,
    application: bool,
    script: bool = False,
) -> subprocess.CompletedProcess[str]:
    arguments = [
        PSQL,
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-h",
        APP_HOST if application else PGHOST,
        "-p",
        PGPORT,
        "-d",
        PGDATABASE,
        "-Atq",
    ]
    if application:
        arguments.extend(("-U", APP_ROLE))
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


def sql(query: str, *, application: bool = False, script: bool = False) -> str:
    result = run_sql(query, application=application, script=script)
    assert result.returncode == 0, result.stdout
    return result.stdout.strip()


def app_sql(query: str, *, script: bool = False) -> str:
    return sql(query, application=True, script=script)


def app_sql_fails(query: str, expected: str) -> None:
    result = run_sql(query, application=True)
    assert result.returncode != 0, result.stdout
    assert expected.lower() in result.stdout.lower(), result.stdout


def stats() -> dict[str, int]:
    return json.loads(sql("SELECT local_cache.stats()::text"))


def assert_mget_backend_memory_bounded(relation: str) -> None:
    context_names = ("SPI Plan", "CachedPlanSource", "CachedPlanQuery")
    values = ", ".join(f"('{name}')" for name in context_names)

    def snapshot(label: str) -> str:
        return (
            "SELECT "
            f"'{label}|' || wanted.name || '|' || "
            "count(context.name)::text || '|' || "
            "coalesce(sum(context.total_bytes), 0)::text "
            f"FROM (VALUES {values}) AS wanted(name) "
            "LEFT JOIN pg_backend_memory_contexts AS context "
            "ON context.name = wanted.name "
            "GROUP BY wanted.name ORDER BY wanted.name;"
        )

    probe = [
        "BEGIN;",
        "PREPARE pglc_mget_memory (bigint[]) AS "
        f"SELECT local_cache.mget('{relation}'::regclass, $1);",
        "EXECUTE pglc_mget_memory(ARRAY[1]::bigint[]);",
        snapshot("before"),
        "\\set ON_ERROR_STOP off",
        "SAVEPOINT pglc_mget_memory_error;",
        "EXECUTE pglc_mget_memory(ARRAY[" + ",".join(["1"] * 1025) + "]::bigint[]);",
        "\\set ON_ERROR_STOP on",
        "ROLLBACK TO SAVEPOINT pglc_mget_memory_error;",
        *["EXECUTE pglc_mget_memory(ARRAY[1]::bigint[]);" for _ in range(200)],
        snapshot("after"),
        "DEALLOCATE pglc_mget_memory;",
        "ROLLBACK;",
    ]
    output = sql("\n".join(probe), script=True)
    assert "at most 1024 keys" in output.lower(), output

    observed: dict[str, dict[str, tuple[int, int]]] = {"before": {}, "after": {}}
    pattern = re.compile(
        r"^(before|after)\|(SPI Plan|CachedPlanSource|CachedPlanQuery)\|"
        r"(\d+)\|(\d+)$"
    )
    for line in output.splitlines():
        match = pattern.fullmatch(line)
        if match:
            phase, name, count, total_bytes = match.groups()
            observed[phase][name] = (int(count), int(total_bytes))
    for name in context_names:
        assert name in observed["before"], output
        assert name in observed["after"], output
        before_count, before_bytes = observed["before"][name]
        after_count, after_bytes = observed["after"][name]
        assert after_count <= before_count + 1, (name, observed)
        assert after_bytes <= before_bytes + 64 * 1024, (name, observed)


def main() -> None:
    suffix = str(os.getpid())
    relation = f"public.pglc_mget_{suffix}"
    composite = f"public.pglc_mget_composite_{suffix}"
    namespace = f"mget{suffix}"
    composite_namespace = f"mgetc{suffix}"
    role = quote_identifier(APP_ROLE)

    identity = app_sql(
        "SELECT current_user, session_user, rolcanlogin, rolsuper "
        "FROM pg_catalog.pg_roles WHERE rolname = current_user"
    ).split("|")
    assert identity == [APP_ROLE, APP_ROLE, "t", "f"], identity

    try:
        sql(
            f"CREATE TABLE {relation} (id bigint PRIMARY KEY, value text NOT NULL);"
            f"INSERT INTO {relation} VALUES (1, 'one'), (-1, 'minus-one');"
            f"CREATE TABLE {composite} (tenant text, id bigint, value text NOT NULL, "
            "PRIMARY KEY (tenant, id));"
            f"INSERT INTO {composite} VALUES "
            "('alpha', 1, 'one'), ('beta', 2, 'two');"
            f"SELECT local_cache.attach_table('{relation}'::regclass, false, "
            f"'{namespace}');"
            f"SELECT local_cache.attach_table('{composite}'::regclass, false, "
            f"'{composite_namespace}');"
            f"GRANT SELECT, UPDATE ON {relation} TO {role};"
            f"GRANT SELECT, UPDATE, DELETE ON {composite} TO {role};"
            f"GRANT USAGE ON SCHEMA local_cache TO {role};"
            "GRANT EXECUTE ON FUNCTION local_cache.mget(regclass, anyarray) "
            f"TO {role}"
        )

        assert sql(
            "SELECT count(*) FROM pg_catalog.pg_proc AS p "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'local_cache' AND p.proname = 'get'"
        ) == "0"

        expected = [
            '{"id":1,"value":"one"}',
            '{"id":-1,"value":"minus-one"}',
            '{"id":1,"value":"one"}',
            None,
            None,
        ]
        sql(f"SELECT local_cache.invalidate('{namespace}')")
        before = stats()
        rows = json.loads(
            app_sql(
                "SELECT array_to_json(local_cache.mget("
                f"'{relation}'::regclass, "
                "ARRAY[1, -1, 1, NULL, 999]::bigint[]))::text"
            )
        )
        assert rows == expected, rows
        warm = json.loads(
            app_sql(
                "SELECT array_to_json(local_cache.mget("
                f"'{relation}'::regclass, ARRAY[1, -1]::bigint[]))::text"
            )
        )
        assert warm == expected[:2], warm
        assert stats()["sql_cache_hits"] > before["sql_cache_hits"]
        assert_mget_backend_memory_bounded(relation)

        before = stats()
        plan = app_sql(
            f"EXPLAIN (COSTS OFF) SELECT value FROM {relation} WHERE id = 1"
        )
        assert "Custom Scan" not in plan, plan
        assert app_sql(f"SELECT value FROM {relation} WHERE id = 1") == "one"
        assert stats()["sql_cache_hits"] == before["sql_cache_hits"]

        dirty = app_sql(
            "BEGIN;\n"
            f"UPDATE {relation} SET value = 'dirty' WHERE id = 1;\n"
            "SELECT array_to_json(local_cache.mget("
            f"'{relation}'::regclass, ARRAY[1]::bigint[]))::text;\n"
            "ROLLBACK;\n",
            script=True,
        ).splitlines()
        assert json.loads(dirty[-1]) == ['{"id":1,"value":"dirty"}'], dirty

        app_sql(f"UPDATE {relation} SET value = 'committed' WHERE id = 1")
        committed = json.loads(
            app_sql(
                "SELECT array_to_json(local_cache.mget("
                f"'{relation}'::regclass, ARRAY[1]::bigint[]))::text"
            )
        )
        assert committed == ['{"id":1,"value":"committed"}'], committed

        composite_rows = json.loads(
            app_sql(
                "SELECT array_to_json(local_cache.mget("
                f"'{composite}'::regclass, "
                "ARRAY[['alpha', '1'], ['beta', '2'], "
                "['alpha', '1'], ['missing', '9']]::text[][]))::text"
            )
        )
        assert composite_rows == [
            '{"tenant":"alpha","id":1,"value":"one"}',
            '{"tenant":"beta","id":2,"value":"two"}',
            '{"tenant":"alpha","id":1,"value":"one"}',
            None,
        ], composite_rows

        app_sql_fails(
            "SELECT local_cache.mget("
            f"'{composite}'::regclass, ARRAY[['alpha']]::text[][])",
            "expected 2 primary-key values",
        )
        app_sql_fails(
            "SELECT local_cache.mget("
            f"'{relation}'::regclass, "
            "ARRAY(SELECT value::bigint FROM generate_series(1, 1025) AS value))",
            "at most 1024 keys",
        )

        sql(f"REVOKE SELECT ON {relation} FROM {role}")
        app_sql_fails(
            f"SELECT local_cache.mget('{relation}'::regclass, ARRAY[1]::bigint[])",
            "permission denied",
        )
        print(
            "ok: mget order, duplicates, NULLs, cache, fallback, ACL, "
            "composite keys, and bounded backend memory"
        )
    finally:
        subprocess.run(
            [
                PSQL,
                "-X",
                "-v",
                "ON_ERROR_STOP=0",
                "-h",
                PGHOST,
                "-p",
                PGPORT,
                "-d",
                PGDATABASE,
            ],
            input=(
                "REVOKE EXECUTE ON FUNCTION local_cache.mget(regclass, anyarray) "
                f"FROM {role};\n"
                f"REVOKE USAGE ON SCHEMA local_cache FROM {role};\n"
                f"DROP TABLE IF EXISTS {relation};\n"
                f"DROP TABLE IF EXISTS {composite};\n"
            ),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=os.environ.copy(),
            timeout=30,
        )


if __name__ == "__main__":
    main()
