#!/usr/bin/env python3
"""Verify compact-entry capacity and marker-only write publication."""

from __future__ import annotations

import os

from pipeline_integration import (
    RespConnection,
    WORKER_ROLE,
    crud_key,
    read_cache_stats,
    row_bytes,
    sql,
    sql_identifier,
    wait_for_mapping,
)


ROW_COUNT = 200_000
VALUE = "v" * 120
BATCH_SIZE = 256
WARM_SET_SIZE = 1_024
PRESSURE_ROWS = 70_000


def read_range(
    client: RespConnection,
    table: str,
    first: int,
    end_exclusive: int,
    value: str = VALUE,
) -> int:
    rows = 0
    for start in range(first, end_exclusive, BATCH_SIZE):
        end = min(start + BATCH_SIZE, end_exclusive)
        keys = [crud_key(table, row_id) for row_id in range(start, end)]
        values = client.command("MGET", *keys)
        assert isinstance(values, list) and len(values) == len(keys)
        assert values == [row_bytes(row_id, value) for row_id in range(start, end)], (
            start,
            end,
        )
        rows += len(values)
    return rows


def read_all(client: RespConnection, table: str) -> int:
    return read_range(client, table, 1, ROW_COUNT + 1)


def main() -> None:
    table = f"capacity_{os.getpid()}"
    namespace = f"capacity{os.getpid()}"
    created = False
    attached = False
    entries = int(sql("SELECT current_setting('pg_local_cache.cache_entries')::integer"))
    assert entries >= ROW_COUNT, entries
    budget_mb = int(
        sql(
            "SELECT setting FROM pg_catalog.pg_settings "
            "WHERE name = 'pg_local_cache.memory_budget_mb'"
        )
    )
    assert budget_mb <= 512, budget_mb
    worker_grant = ""
    if WORKER_ROLE:
        worker_grant = (
            f"GRANT USAGE ON SCHEMA public TO {sql_identifier(WORKER_ROLE)};"
            f"GRANT SELECT, INSERT ON public.{sql_identifier(table)} "
            f"TO {sql_identifier(WORKER_ROLE)};"
        )

    try:
        sql(
            f"CREATE TABLE public.{sql_identifier(table)} "
            "(id bigint PRIMARY KEY, value text NOT NULL);"
            f"INSERT INTO public.{sql_identifier(table)} "
            f"SELECT id, '{VALUE}' FROM generate_series(1, {ROW_COUNT}) AS rows(id);"
            f"{worker_grant}"
        )
        created = True
        sql(
            "SELECT local_cache.attach_table(" 
            f"'public.{sql_identifier(table)}'::regclass, true, "
            f"'{namespace}')"
        )
        attached = True

        client = RespConnection(socket_timeout=45)
        try:
            first_key = crud_key(table, 1)
            assert wait_for_mapping(client, first_key) == row_bytes(1, VALUE)
            assert read_all(client, table) == ROW_COUNT

            before_second_pass = read_cache_stats()
            assert read_all(client, table) == ROW_COUNT
            after_second_pass = read_cache_stats()
            assert after_second_pass["cache_hits"] == (
                before_second_pass["cache_hits"] + ROW_COUNT
            ), (before_second_pass, after_second_pass)
            assert after_second_pass["database_reads"] == before_second_pass["database_reads"], (
                before_second_pass,
                after_second_pass,
            )
            assert after_second_pass["entries"] >= ROW_COUNT, after_second_pass

            warm_ids = list(range(1, WARM_SET_SIZE + 1))
            before_storm = read_cache_stats()
            sql(
                f"INSERT INTO public.{sql_identifier(table)} (id, value) "
                f"SELECT {ROW_COUNT} + id, repeat('w', 120) "
                "FROM generate_series(1, 2048) AS rows(id)"
            )
            for start in range(0, len(warm_ids), BATCH_SIZE):
                ids = warm_ids[start : start + BATCH_SIZE]
                values = client.command(
                    "MGET", *(crud_key(table, row_id) for row_id in ids)
                )
                assert values == [row_bytes(row_id, VALUE) for row_id in ids]
            after_storm = read_cache_stats()
            assert after_storm["cache_hits"] == (
                before_storm["cache_hits"] + WARM_SET_SIZE
            ), (before_storm, after_storm)
            assert after_storm["database_reads"] == before_storm["database_reads"], (
                before_storm,
                after_storm,
            )
            assert after_storm["entries"] == after_second_pass["entries"], (
                after_second_pass,
                after_storm,
            )

            pressure_start = ROW_COUNT + 2_049
            pressure_end = pressure_start + PRESSURE_ROWS
            sql(
                f"INSERT INTO public.{sql_identifier(table)} (id, value) "
                f"SELECT id, repeat('p', 120) "
                f"FROM generate_series({pressure_start}, {pressure_end - 1}) AS rows(id)"
            )
            before_pressure = read_cache_stats()
            assert read_range(
                client, table, pressure_start, pressure_end, "p" * 120
            ) == PRESSURE_ROWS
            after_pressure = read_cache_stats()
            assert after_pressure["database_reads"] > before_pressure["database_reads"], (
                before_pressure,
                after_pressure,
            )
            assert after_pressure["entries"] <= after_pressure["cache_capacity"], (
                after_pressure,
            )
        finally:
            client.close()
        print(
            "capacity integration passed: 200k entries, second-pass hits, "
            "marker-only insert storm, and source-safe admission pressure"
        )
    finally:
        if attached:
            sql(
                f"SELECT local_cache.detach_table('public.{sql_identifier(table)}'::regclass)"
            )
        if created:
            sql(f"DROP TABLE public.{sql_identifier(table)}")


if __name__ == "__main__":
    main()
