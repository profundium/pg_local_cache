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
    sql_commands,
    sql_identifier,
    sql_literal,
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


def set_free_head_fault_hook(name: str, enabled: bool) -> bool:
    assert name in {
        "pg_local_cache.test_occupied_entry_free_head",
        "pg_local_cache.test_occupied_marker_free_head",
    }
    available = sql(
        f"SELECT current_setting({sql_literal(name)}, true) IS NOT NULL"
    )
    if available != "t":
        return False
    if enabled:
        sql_commands(
            f"ALTER SYSTEM SET {name} = 1",
            "SELECT pg_reload_conf()",
            "SELECT pg_sleep(0.1)",
        )
    else:
        sql_commands(
            f"ALTER SYSTEM RESET {name}",
            "SELECT pg_reload_conf()",
            "SELECT pg_sleep(0.1)",
        )
    return True


def main() -> None:
    table = f"capacity_{os.getpid()}"
    namespace = f"capacity{os.getpid()}"
    created = False
    attached = False
    entry_fault_hook = False
    marker_fault_hook = False
    next_id = 1
    inserted_id_ranges: list[range] = []

    def allocate_insert_ids(count: int) -> range:
        nonlocal next_id
        assert count > 0, count
        ids = range(next_id, next_id + count)
        assert all(
            ids.stop <= allocated.start or allocated.stop <= ids.start
            for allocated in inserted_id_ranges
        ), (ids, inserted_id_ranges)
        inserted_id_ranges.append(ids)
        next_id = ids.stop
        assert all(
            previous.stop <= current.start
            for previous, current in zip(
                inserted_id_ranges, inserted_id_ranges[1:]
            )
        ), inserted_id_ranges
        return ids

    seed_ids = allocate_insert_ids(ROW_COUNT)
    fault_count = 257
    entry_fault_ids = allocate_insert_ids(fault_count)
    marker_fault_ids = allocate_insert_ids(fault_count)
    storm_ids = allocate_insert_ids(2_048)
    pressure_ids = allocate_insert_ids(PRESSURE_ROWS)
    entries = int(sql("SELECT current_setting('pg_local_cache.cache_entries')::integer"))
    assert entries >= ROW_COUNT, entries
    budget_mb = int(
        sql(
            "SELECT setting FROM pg_catalog.pg_settings "
            "WHERE name = 'pg_local_cache.memory_budget_mb'"
        )
    )
    assert budget_mb <= 512, budget_mb
    defaults_active = sql(
        "SELECT count(*) = 5 FROM pg_catalog.pg_settings "
        "WHERE name IN ("
        "'pg_local_cache.cache_entries', "
        "'pg_local_cache.dirty_marker_entries', "
        "'pg_local_cache.dirty_marker_memory_mb', "
        "'pg_local_cache.lock_partitions', "
        "'pg_local_cache.memory_budget_mb') AND source = 'default'"
    ) == "t"
    if defaults_active:
        startup_stats = read_cache_stats()
        assert int(startup_stats["cache_memory_capacity_bytes"]) >= (
            budget_mb * 1024 * 1024 // 2
        ), startup_stats
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
            f"SELECT id, '{VALUE}' FROM generate_series("
            f"{seed_ids.start}, {seed_ids.stop - 1}) AS rows(id);"
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

            sql(
                f"INSERT INTO public.{sql_identifier(table)} (id, value) "
                f"SELECT id, '{VALUE}' FROM generate_series("
                f"{entry_fault_ids.start}, {entry_fault_ids.stop - 1}) AS rows(id)"
            )
            entry_fault_hook = set_free_head_fault_hook(
                "pg_local_cache.test_occupied_entry_free_head", True
            )
            if entry_fault_hook:
                before_entry_fault = read_cache_stats()
                assert client.command(
                    "MGET", *(crud_key(table, row_id) for row_id in entry_fault_ids)
                ) == [row_bytes(row_id, VALUE) for row_id in entry_fault_ids]
                after_entry_fault = read_cache_stats()
                assert after_entry_fault["cache_admission_rejections"] > (
                    before_entry_fault["cache_admission_rejections"]
                ), (before_entry_fault, after_entry_fault)
                set_free_head_fault_hook(
                    "pg_local_cache.test_occupied_entry_free_head", False
                )
                entry_fault_hook = False

            startup_stats = read_cache_stats()
            marker_test_fits = int(startup_stats["dirty_marker_capacity"]) >= (
                2 * int(startup_stats["lock_partitions"])
            )
            if marker_test_fits:
                marker_fault_hook = set_free_head_fault_hook(
                    "pg_local_cache.test_occupied_marker_free_head", True
                )
            if marker_fault_hook:
                before_marker_fault = read_cache_stats()
                sql(
                    "BEGIN; "
                    f"INSERT INTO public.{sql_identifier(table)} (id, value) "
                    f"SELECT id, '{VALUE}' FROM generate_series("
                    f"{marker_fault_ids.start}, "
                    f"{marker_fault_ids.stop - 1}) AS rows(id); "
                    "COMMIT"
                )
                after_marker_fault = read_cache_stats()
                assert after_marker_fault["dirty_marker_fallbacks_total"] > (
                    before_marker_fault["dirty_marker_fallbacks_total"]
                ), (before_marker_fault, after_marker_fault)
                marker_fault_probe_ids = range(
                    marker_fault_ids.start, marker_fault_ids.start + 2
                )
                assert client.command(
                    "MGET",
                    *(crud_key(table, row_id) for row_id in marker_fault_probe_ids),
                ) == [row_bytes(row_id, VALUE) for row_id in marker_fault_probe_ids]
                set_free_head_fault_hook(
                    "pg_local_cache.test_occupied_marker_free_head", False
                )
                marker_fault_hook = False

            warm_ids = list(range(1, WARM_SET_SIZE + 1))
            assert read_range(client, table, 1, WARM_SET_SIZE + 1) == WARM_SET_SIZE
            before_second_warm_pass = read_cache_stats()
            assert read_range(client, table, 1, WARM_SET_SIZE + 1) == WARM_SET_SIZE
            after_second_pass = read_cache_stats()
            assert after_second_pass["cache_hits"] == (
                before_second_warm_pass["cache_hits"] + WARM_SET_SIZE
            ), (before_second_warm_pass, after_second_pass)
            assert after_second_pass["database_reads"] == (
                before_second_warm_pass["database_reads"]
            ), (before_second_warm_pass, after_second_pass)
            before_storm = after_second_pass
            sql(
                f"INSERT INTO public.{sql_identifier(table)} (id, value) "
                f"SELECT id, repeat('w', 120) FROM generate_series("
                f"{storm_ids.start}, {storm_ids.stop - 1}) AS rows(id)"
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

            sql(
                f"INSERT INTO public.{sql_identifier(table)} (id, value) "
                f"SELECT id, repeat('p', 120) "
                f"FROM generate_series({pressure_ids.start}, {pressure_ids.stop - 1}) AS rows(id)"
            )
            before_pressure = read_cache_stats()
            assert read_range(
                client, table, pressure_ids.start, pressure_ids.stop, "p" * 120
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
        if entry_fault_hook:
            set_free_head_fault_hook(
                "pg_local_cache.test_occupied_entry_free_head", False
            )
        if marker_fault_hook:
            set_free_head_fault_hook(
                "pg_local_cache.test_occupied_marker_free_head", False
            )
        if attached:
            sql(
                f"SELECT local_cache.detach_table('public.{sql_identifier(table)}'::regclass)"
            )
        if created:
            sql(f"DROP TABLE public.{sql_identifier(table)}")


if __name__ == "__main__":
    main()
