#!/usr/bin/env python3
"""Check RESP worker memory stays bounded during random cache misses."""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from pipeline_integration import (
    RespConnection,
    WORKER_ROLE,
    crud_key,
    sql,
    sql_identifier,
)


CLIENTS = 8
KEYS_PER_MGET = 16
MIN_RUN_SECONDS = 20
MAX_RUN_SECONDS = 60
MIN_CACHE_MISSES = 100_000
MAX_WORKER_GROWTH_BYTES = 32 * 1024 * 1024


def worker_pids() -> list[int]:
    output = sql(
        "SELECT pid FROM pg_catalog.pg_stat_activity "
        "WHERE backend_type = 'pg_local_cache RESP worker' ORDER BY pid"
    )
    return [int(line) for line in output.splitlines() if line]


def wait_for_workers(expected_count: int) -> list[int]:
    deadline = time.monotonic() + 10
    while True:
        pids = worker_pids()
        if len(pids) == expected_count:
            return pids
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"found {len(pids)} of {expected_count} RESP worker pids"
            )
        time.sleep(0.05)


def rss_anon_bytes(pid: int) -> int:
    with open(f"/proc/{pid}/status", encoding="ascii") as status:
        match = re.search(r"^RssAnon:\s+(\d+)\s+kB$", status.read(), re.MULTILINE)
    if match is None:
        raise AssertionError(f"RssAnon missing for RESP worker {pid}")
    return int(match.group(1)) * 1024


def read_stats(client: RespConnection) -> dict[str, int]:
    stats = json.loads(client.command("STAT"))
    return stats


def issue_random_mgets(
    table: str,
    row_count: int,
    ready: threading.Barrier,
    start: threading.Event,
    stop: threading.Event,
    client_index: int,
) -> None:
    client = RespConnection()
    try:
        ready.wait(timeout=15)
        if not start.wait(timeout=15):
            raise AssertionError("memory test load did not start")
        rng = random.Random(os.getpid() + client_index)
        while not stop.is_set():
            keys = [
                crud_key(table, rng.randrange(1, row_count + 1))
                for _ in range(KEYS_PER_MGET)
            ]
            response = client.command("MGET", *keys)
            assert isinstance(response, list) and len(response) == KEYS_PER_MGET
    finally:
        client.close()


def main() -> None:
    table = f"memory_{os.getpid()}"
    created = False
    attached = False
    sql("CREATE EXTENSION IF NOT EXISTS pg_local_cache")
    cache_entries = int(sql("SELECT current_setting('pg_local_cache.cache_entries')::integer"))
    worker_count = int(sql("SELECT current_setting('pg_local_cache.workers')::integer"))
    row_count = 4 * cache_entries
    role_grants = ""
    if WORKER_ROLE:
        role = sql_identifier(WORKER_ROLE)
        role_grants = (
            f"GRANT USAGE ON SCHEMA public TO {role};"
            f"GRANT SELECT ON TABLE public.{table} TO {role};"
        )

    try:
        sql(
            f"CREATE TABLE public.{table} (id bigint PRIMARY KEY, value text NOT NULL);"
            f"INSERT INTO public.{table} SELECT id, 'value-' || id::text "
            f"FROM generate_series(1, {row_count}) AS rows(id);"
            f"{role_grants}"
        )
        created = True
        sql(f"SELECT local_cache.attach_table('public.{table}'::regclass)")
        attached = True

        # First MGET also waits for each worker's mapping reload to complete.
        bootstrap = RespConnection()
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    value = bootstrap.command("MGET", crud_key(table, 1))
                    assert isinstance(value, list) and len(value) == 1
                    break
                except Exception as error:
                    if "unknown KVik table mapping" not in str(error):
                        raise
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
        finally:
            bootstrap.close()

        pids = wait_for_workers(worker_count)
        time.sleep(3)
        if worker_pids() != pids:
            raise AssertionError("RESP worker set changed during warmup")

        ready = threading.Barrier(CLIENTS + 1)
        start = threading.Event()
        stop = threading.Event()
        monitor = RespConnection()
        with ThreadPoolExecutor(max_workers=CLIENTS) as executor:
            futures = [
                executor.submit(
                    issue_random_mgets,
                    table,
                    row_count,
                    ready,
                    start,
                    stop,
                    index,
                )
                for index in range(CLIENTS)
            ]
            try:
                ready.wait(timeout=15)
                before_rss = {pid: rss_anon_bytes(pid) for pid in pids}
                before_misses = read_stats(monitor)["cache_misses"]
                started = time.monotonic()
                start.set()

                while True:
                    time.sleep(0.5)
                    for future in futures:
                        if future.done():
                            future.result()
                    stats = read_stats(monitor)
                    elapsed = time.monotonic() - started
                    misses = stats["cache_misses"] - before_misses
                    if elapsed >= MIN_RUN_SECONDS and misses >= MIN_CACHE_MISSES:
                        break
                    if elapsed >= MAX_RUN_SECONDS:
                        stop.set()
                        for future in futures:
                            future.result(timeout=10)
                        raise AssertionError(
                            f"only {misses} cache misses after {elapsed:.1f}s; "
                            f"need at least {MIN_CACHE_MISSES}"
                        )

                stop.set()
                for future in futures:
                    future.result(timeout=10)
                after_misses = read_stats(monitor)["cache_misses"]
                assert after_misses - before_misses > MIN_CACHE_MISSES

                # Observed leak is ~2 KiB/miss: 100k misses imply ~195 MiB total,
                # or ~49 MiB/worker across four evenly loaded workers.
                after_rss = {pid: rss_anon_bytes(pid) for pid in pids}
                growth = {
                    pid: after_rss[pid] - before_rss[pid]
                    for pid in pids
                }
                excessive = {
                    pid: size
                    for pid, size in growth.items()
                    if size >= MAX_WORKER_GROWTH_BYTES
                }
                assert not excessive, (
                    f"RESP worker RssAnon grew by >=32 MiB: {excessive}; "
                    f"all growth: {growth}; misses: {after_misses - before_misses}"
                )
                print(
                    "memory integration passed: "
                    f"{after_misses - before_misses} misses, worker growth {growth}"
                )
            finally:
                stop.set()
                monitor.close()

    finally:
        if created:
            detach = (
                f"SELECT local_cache.detach_table('public.{table}'::regclass);"
                if attached
                else ""
            )
            sql(f"{detach}DROP TABLE IF EXISTS public.{table}")


if __name__ == "__main__":
    main()
