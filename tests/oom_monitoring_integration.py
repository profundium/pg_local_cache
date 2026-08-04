#!/usr/bin/env python3
"""Runtime contracts for monitoring and global RESP memory limits."""

from __future__ import annotations

import json
import os
import time

from pipeline_integration import RespConnection as RespClient
from pipeline_integration import sql, sql_commands, sql_identifier


APP_ROLE = os.environ.get("PG_LOCAL_CACHE_TEST_APP_ROLE", "")
MONITOR_ROLE = "local_cache_monitor_test"


def wait_until(description: str, predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_value: object = None
    while time.monotonic() < deadline:
        last_value = predicate()
        if last_value:
            return
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {description}; last={last_value!r}")


def metric_row() -> dict[str, int]:
    payload = sql(
        "SELECT pg_catalog.row_to_json(metrics)::text "
        "FROM local_cache.metrics() AS metrics"
    )
    parsed = json.loads(payload)
    assert all(isinstance(value, int) for value in parsed.values()), parsed
    return parsed


def close_all(clients: list[RespClient]) -> None:
    for client in clients:
        try:
            client.close()
        except OSError:
            pass
    clients.clear()


def create_monitor_role() -> None:
    quoted = sql_identifier(MONITOR_ROLE)
    sql(
        f"DROP ROLE IF EXISTS {quoted};"
        f"CREATE ROLE {quoted} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOINHERIT NOREPLICATION NOBYPASSRLS;"
        f"GRANT USAGE ON SCHEMA local_cache TO {quoted};"
        f"GRANT EXECUTE ON FUNCTION local_cache.stats() TO {quoted};"
        f"GRANT EXECUTE ON FUNCTION local_cache.metrics() TO {quoted};"
        f"GRANT EXECUTE ON FUNCTION local_cache.health() TO {quoted}"
    )


def drop_monitor_role() -> None:
    quoted = sql_identifier(MONITOR_ROLE)
    sql(f"DROP OWNED BY {quoted}; DROP ROLE {quoted}")


def wait_for_mapping_ready() -> None:
    wait_until(
        "workers to apply the transactional ACL reload",
        lambda: sql(
            "SELECT (local_cache.health() ->> 'ready')::boolean"
        )
        == "t",
    )


def assert_monitor_acl() -> None:
    assert APP_ROLE, "PG_LOCAL_CACHE_TEST_APP_ROLE is required"
    create_monitor_role()
    wait_for_mapping_ready()
    quoted = sql_identifier(MONITOR_ROLE)
    try:
        result = sql_commands(
            f"SET ROLE {quoted}",
            "SELECT (local_cache.health() ->> 'ready')::boolean, "
            "       (SELECT count(*) FROM local_cache.metrics()) = 1, "
            "       pg_catalog.jsonb_typeof(local_cache.stats()) = 'object', "
            "       pg_catalog.has_table_privilege("
            "           current_user, 'local_cache.mapping', 'SELECT')",
            "RESET ROLE",
        )
        assert result == "t|t|t|f", result
        app_acl = sql(
            "SELECT pg_catalog.has_schema_privilege("
            f"           {APP_ROLE!r}, 'local_cache', 'USAGE'), "
            "       pg_catalog.has_function_privilege("
            f"           {APP_ROLE!r}, 'local_cache.stats()', 'EXECUTE'), "
            "       pg_catalog.has_function_privilege("
            f"           {APP_ROLE!r}, 'local_cache.metrics()', 'EXECUTE'), "
            "       pg_catalog.has_function_privilege("
            f"           {APP_ROLE!r}, 'local_cache.health()', 'EXECUTE')"
        )
        assert app_acl == "f|f|f|f", app_acl
    finally:
        drop_monitor_role()
        wait_for_mapping_ready()


def assert_metrics_contract() -> dict[str, int]:
    metrics = metric_row()
    required = {
        "up",
        "cache_capacity",
        "entries",
        "active_clients",
        "peak_active_clients",
        "max_clients",
        "client_slots",
        "workers_configured",
        "workers_running",
        "shared_memory_bytes",
        "worker_memory_bytes",
        "estimated_memory_bytes",
        "memory_budget_bytes",
        "client_limit_rejections_total",
        "worker_starts_total",
        "dirty_key_limit_fallbacks_total",
        "mapping_reload_failures_total",
        "workers_with_incomplete_mappings",
        "mapping_reload_incomplete_retries_total",
    }
    assert required <= metrics.keys(), required - metrics.keys()
    assert metrics["up"] == 1, metrics
    assert metrics["entries"] <= metrics["cache_capacity"], metrics
    assert metrics["active_clients"] <= metrics["max_clients"], metrics
    assert metrics["max_clients"] <= metrics["client_slots"], metrics
    assert metrics["workers_running"] == metrics["workers_configured"], metrics
    assert metrics["estimated_memory_bytes"] <= metrics["memory_budget_bytes"], metrics
    health = json.loads(sql("SELECT local_cache.health()::text"))
    assert health["ready"] is True, health
    stats_text = sql("SELECT local_cache.stats()::text")
    auth_token = os.environ.get("PG_LOCAL_CACHE_AUTH_TOKEN", "")
    if auth_token:
        assert auth_token not in stats_text
    return metrics


def assert_global_client_limit() -> None:
    initial = metric_row()
    maximum = initial["max_clients"]
    assert 1 <= maximum <= 64, maximum
    clients: list[RespClient] = []
    try:
        deadline = time.monotonic() + 15
        while len(clients) < maximum and time.monotonic() < deadline:
            try:
                clients.append(RespClient())
            except (AssertionError, EOFError, OSError):
                time.sleep(0.02)
        assert len(clients) == maximum, (len(clients), maximum, metric_row())
        wait_until(
            "all global client reservations",
            lambda: metric_row()["active_clients"] == maximum,
        )

        rejected_before = metric_row()["client_limit_rejections_total"]
        for _ in range(8):
            try:
                extra = RespClient()
            except (AssertionError, EOFError, OSError):
                continue
            else:
                extra.close()
                raise AssertionError("connection exceeded global client limit")
        wait_until(
            "client-limit rejection counters",
            lambda: metric_row()["client_limit_rejections_total"]
            >= rejected_before + 8,
        )
        saturated = metric_row()
        assert saturated["peak_active_clients"] <= maximum, saturated
        assert json.loads(sql("SELECT local_cache.health()::text"))["ready"] is True

        for _ in range(2):
            clients.pop().close()
        wait_until(
            "released global reservations",
            lambda: metric_row()["active_clients"] == maximum - 2,
        )
        clients.extend((RespClient(), RespClient()))
        wait_until(
            "reused global reservations",
            lambda: metric_row()["active_clients"] == maximum,
        )

        starts_before = metric_row()["worker_starts_total"]
        configured_workers = metric_row()["workers_configured"]
        sql(
            "SELECT pg_catalog.pg_terminate_backend(pid) "
            "FROM pg_catalog.pg_stat_activity "
            "WHERE backend_type = 'pg_local_cache RESP worker'"
        )
        wait_until(
            "worker reservation reconciliation",
            lambda: metric_row()["active_clients"] == 0,
        )
        wait_until(
            "RESP worker restart",
            lambda: (
                metric_row()["workers_running"] == configured_workers
                and metric_row()["worker_starts_total"]
                >= starts_before + configured_workers
            ),
            timeout=30,
        )
        close_all(clients)
        probe = RespClient()
        assert probe.command("PING") == "PONG"
        probe.close()
    finally:
        close_all(clients)


def main() -> None:
    assert_monitor_acl()
    assert_metrics_contract()
    assert_global_client_limit()
    final = assert_metrics_contract()
    print(
        "oom/monitoring integration passed: typed least-privilege metrics, "
        f"{final['max_clients']} global clients, rejection and restart recovery"
    )


if __name__ == "__main__":
    main()
