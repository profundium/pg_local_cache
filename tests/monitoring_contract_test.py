#!/usr/bin/env python3
"""Source contracts for monitoring and bounded memory use.

The runtime/Docker suites prove behaviour with a live PostgreSQL instance.
These checks pin the less visible safety invariants which can otherwise be
lost during hot-path refactors: startup must reject an unsafe memory plan,
RESP connection reservations must be global and leak-free, and monitoring
must expose typed, least-privilege metrics.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONTROL = (ROOT / "pg_local_cache.control").read_text(encoding="utf-8")
CURRENT_VERSION_MATCH = re.search(
    r"^default_version = '([^']+)'$", CONTROL, flags=re.MULTILINE
)
if CURRENT_VERSION_MATCH is None:
    raise RuntimeError("could not determine pg_local_cache version")
CURRENT_VERSION = CURRENT_VERSION_MATCH.group(1)
CORE = (ROOT / "src" / "pg_local_cache.c").read_text(encoding="utf-8")
WORKER = (ROOT / "src" / "pg_local_cache_worker.c").read_text(
    encoding="utf-8"
)
HEADER = (ROOT / "src" / "pg_local_cache.h").read_text(encoding="utf-8")
INSTALL_SQL = (ROOT / "sql" / f"pg_local_cache--{CURRENT_VERSION}.sql").read_text(
    encoding="utf-8"
)
ENTRYPOINT = (ROOT / "docker" / "entrypoint.sh").read_text(
    encoding="utf-8"
)
COMPOSE = (ROOT / "compose.yaml").read_text(encoding="utf-8")
def c_function(source: str, name: str) -> str:
    """Return a PostgreSQL-style C function definition, including its body."""

    marker = re.search(rf"(?m)^{re.escape(name)}\s*\(", source)
    if marker is None:
        raise AssertionError(f"C function {name}() is missing")
    opening = source.find("{", marker.start())
    if opening < 0:
        raise AssertionError(f"C function {name}() has no body")
    depth = 0
    in_string = False
    in_character = False
    in_line_comment = False
    in_block_comment = False
    escaped = False
    for position in range(opening, len(source)):
        character = source[position]
        following = source[position + 1] if position + 1 < len(source) else ""
        if in_line_comment:
            if character == "\n":
                in_line_comment = False
            continue
        if in_block_comment:
            if character == "*" and following == "/":
                in_block_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if in_character:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "'":
                in_character = False
            continue
        if character == "/" and following == "/":
            in_line_comment = True
            continue
        if character == "/" and following == "*":
            in_block_comment = True
            continue
        if character == '"':
            in_string = True
        elif character == "'":
            in_character = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[marker.start() : position + 1]
    raise AssertionError(f"C function {name}() has an unterminated body")


def c_functions(source: str) -> dict[str, str]:
    """Collect function definitions without depending on return-type layout."""

    functions: dict[str, str] = {}
    for match in re.finditer(r"(?m)^([A-Za-z_][A-Za-z0-9_]*)\s*\(", source):
        name = match.group(1)
        if name in functions:
            continue
        try:
            functions[name] = c_function(source, name)
        except AssertionError:
            continue
    return functions


def function_containing(source: str, *needles: str) -> tuple[str, str]:
    for name, body in c_functions(source).items():
        if all(needle in body for needle in needles):
            return name, body
    joined = ", ".join(repr(needle) for needle in needles)
    raise AssertionError(f"no C function contains all of: {joined}")


def guc_block(name: str) -> str:
    marker = f'"pg_local_cache.{name}"'
    position = CORE.find(marker)
    if position < 0:
        raise AssertionError(f"GUC pg_local_cache.{name} is missing")
    start = CORE.rfind("DefineCustomIntVariable(", 0, position)
    if start < 0:
        raise AssertionError(f"pg_local_cache.{name} is not an integer GUC")
    candidates = [
        next_position
        for token in ("DefineCustomIntVariable(", "DefineCustomStringVariable(",
                      "DefineCustomBoolVariable(", "MarkGUCPrefixReserved(")
        if (next_position := CORE.find(token, position + len(marker))) >= 0
    ]
    end = min(candidates) if candidates else len(CORE)
    return CORE[start:end]


def assert_has_container_memory_limit(
    testcase: unittest.TestCase, compose: str, filename: str
) -> None:
    direct_limit = re.search(r"(?m)^\s+mem_limit:\s*[^\s#]+", compose)
    deploy_limit = re.search(
        r"resources:\s*\n\s+limits:\s*\n(?:\s+[^\n]+\n)*?\s+memory:\s*[^\s#]+",
        compose,
    )
    testcase.assertTrue(
        direct_limit or deploy_limit,
        f"{filename} must impose a container memory limit",
    )


class MemoryBudgetSourceTests(unittest.TestCase):
    def test_postmaster_gucs_cover_total_and_per_worker_limits(self) -> None:
        for name in (
            "memory_budget_mb",
            "max_clients",
            "max_clients_per_worker",
        ):
            with self.subTest(guc=name):
                self.assertRegex(HEADER, rf"extern int\s+pglc_{name}\s*;")
                block = guc_block(name)
                self.assertIn(f"&pglc_{name}", block)
                self.assertIn("PGC_POSTMASTER", block)

    def test_startup_estimate_includes_shmem_and_all_worker_client_arrays(self) -> None:
        worker_candidates = [
            (name, body)
            for name, body in c_functions(WORKER).items()
            if "sizeof(PgLocalCacheClient)" in body
            and "pglc_max_clients_per_worker" in body
        ]
        self.assertTrue(worker_candidates, "worker client-memory estimate is missing")
        per_worker_name, per_worker_estimate = next(
            (
                candidate
                for candidate in worker_candidates
                if "memory" in candidate[0] or "estimate" in candidate[0]
            ),
            worker_candidates[0],
        )
        aggregate_candidates = [
            (name, body)
            for name, body in c_functions(WORKER).items()
            if name == per_worker_name or f"{per_worker_name}(" in body
        ]
        aggregate_name, aggregate_estimate = next(
            (
                candidate
                for candidate in aggregate_candidates
                if f"{candidate[0]}()" in CORE
            ),
            aggregate_candidates[-1],
        )
        estimate_name, estimate = function_containing(
            CORE, "pglc_shared_memory_bytes()", f"{aggregate_name}("
        )
        self.assertTrue(estimate_name)
        self.assertTrue(per_worker_name)
        self.assertIn(
            "pglc_worker_count",
            estimate + per_worker_estimate + aggregate_estimate,
        )
        self.assertRegex(
            per_worker_estimate,
            r"(?:PGLC_MAX_CLIENTS_PER_WORKER|pglc_max_clients_per_worker)",
        )

    def test_unsafe_memory_plan_fails_inside_the_postmaster(self) -> None:
        _, validation = function_containing(CORE, "pglc_memory_budget_mb", "FATAL")
        self.assertRegex(validation, r"ereport\s*\(\s*FATAL")
        self.assertRegex(validation, r"(?:estimate|estimated|memory)_?[A-Za-z0-9_]*")
        self.assertIn("memory_budget", validation)

    def test_transaction_dirty_hash_has_a_hard_bound_and_observable_fallback(self) -> None:
        dirty_guc = guc_block("max_dirty_keys")
        self.assertRegex(
            dirty_guc,
            r"&pglc_max_dirty_keys\s*,\s*4096\s*,\s*128\s*,\s*16384\s*,",
        )
        self.assertRegex(
            HEADER,
            r"pg_atomic_uint64\s+dirty_(?:key_)?(?:limit_)?fallbacks\s*;",
        )
        collect_key = c_function(CORE, "pglc_collect_key")
        limit_at = collect_key.index("hash_get_num_entries(dirty)")
        fallback = collect_key[limit_at:]
        self.assertIn("pglc_max_dirty_keys", fallback)
        self.assertRegex(fallback, r"dirty_(?:key_)?(?:limit_)?fallbacks")
        self.assertIn("pg_atomic_fetch_add_u64", fallback)
        self.assertIn("pglc_collect_relation", fallback)

    def test_shared_hash_capacities_are_runtime_limits_not_sizing_hints(self) -> None:
        cache_entry = c_function(CORE, "get_cache_entry")
        cache_limit = cache_entry.index(
            "hash_get_num_entries(pglc_cache_hash)"
        )
        cache_insert = cache_entry.index("HASH_ENTER_NULL", cache_limit)
        self.assertLess(cache_limit, cache_insert)
        self.assertIn("pglc_cache_entries", cache_entry[cache_limit:cache_insert])
        self.assertIn("evict_one_cache_entry", cache_entry[cache_limit:cache_insert])

        relation_state = c_function(CORE, "get_relation_state")
        relation_limit = relation_state.index(
            "hash_get_num_entries(pglc_relation_hash)"
        )
        relation_insert = relation_state.index("HASH_ENTER_NULL", relation_limit)
        self.assertLess(relation_limit, relation_insert)
        self.assertIn(
            "pglc_relation_states",
            relation_state[relation_limit:relation_insert],
        )
        self.assertIn(
            "relation_state_admission_rejections",
            relation_state[relation_limit:],
        )

    def test_eviction_is_bounded_rotating_and_prioritizes_stale(self) -> None:
        eviction = c_function(CORE, "evict_one_cache_entry")
        bounded_sample = eviction.index("PGLC_EVICTION_SAMPLE")
        stale_check = eviction.index("!cache_entry_is_current_locked")
        live_lru = eviction.index("last_access = pg_atomic_read_u64")
        self.assertLess(stale_check, live_lru)
        self.assertIn("scanned++", eviction[:bounded_sample])
        self.assertIn("eviction_bucket_cursor", eviction)
        self.assertIn("sequence.curBucket = start_bucket", eviction)
        self.assertEqual(eviction.count("hash_seq_init"), 1)
        remove = eviction.index("HASH_REMOVE")
        counter = eviction.index("&pglc_shared->evictions")
        self.assertIn("== NULL", eviction[remove:counter])


class RespConnectionBudgetSourceTests(unittest.TestCase):
    def test_global_client_reservation_uses_a_compare_exchange_loop(self) -> None:
        _, reserve = function_containing(
            CORE, "active_clients", "pg_atomic_compare_exchange_u64"
        )
        self.assertIn("pglc_max_clients", reserve)
        self.assertRegex(
            reserve, r"client_limit_rejections?|rejected_connections"
        )

    def test_worker_allocates_only_the_configured_number_of_slots(self) -> None:
        server = c_function(WORKER, "run_server")
        allocation = re.search(
            r"sizeof\s*\(\s*PgLocalCacheClient\s*\)\s*\*\s*"
            r"(?P<slot>[A-Za-z_][A-Za-z0-9_]*)",
            server,
        )
        self.assertIsNotNone(
            allocation,
            "run_server() must dynamically size its client array",
        )
        slot_name = allocation.group("slot")
        self.assertRegex(
            server,
            rf"\b{re.escape(slot_name)}\s*=\s*"
            rf"(?:pglc_[A-Za-z0-9_]*client[A-Za-z0-9_]*\s*\(\s*\)|"
            rf"pglc_max_clients_per_worker|Min\s*\()",
        )
        self.assertGreaterEqual(server.count(slot_name), 5)
        self.assertNotRegex(
            server,
            r"sizeof\s*\(\s*PgLocalCacheClient\s*\)\s*\*\s*"
            r"PGLC_MAX_CLIENTS_PER_WORKER",
        )

    def test_reservation_precedes_assignment_and_failure_paths_release_it(self) -> None:
        server = c_function(WORKER, "run_server")
        reserve_at = server.find("pglc_try_reserve_client")
        assignment = re.search(
            r"clients\s*\[\s*slot\s*\]\.fd\s*=\s*client_fd", server
        )
        self.assertGreaterEqual(reserve_at, 0)
        self.assertIsNotNone(assignment)
        self.assertLess(reserve_at, assignment.start())
        reserved_but_unassigned = server[reserve_at : assignment.start()]
        self.assertGreaterEqual(
            reserved_but_unassigned.count("pglc_release_client"),
            2,
            "fcntl and setsockopt failures must both return the reservation",
        )

    def test_normal_close_and_worker_exit_cannot_leak_global_reservations(self) -> None:
        close_client = c_function(WORKER, "close_client")
        self.assertIn("pglc_release_client", close_client)

        hook = re.search(
            r"(?:before_shmem_exit|on_shmem_exit)\s*\(\s*"
            r"(?P<callback>[A-Za-z_][A-Za-z0-9_]*)",
            WORKER,
        )
        self.assertIsNotNone(hook, "the worker must register an exit reconciler")
        callback = c_function(WORKER, hook.group("callback"))
        self.assertRegex(
            callback,
            r"(?:pglc_release_client|pglc_reconcile_[A-Za-z0-9_]*client|"
            r"pg_atomic_fetch_sub_u64)",
        )
        worker_main = c_function(WORKER, "pg_local_cache_worker_main")
        hook_at = worker_main.find(hook.group(0))
        server_at = worker_main.find("run_server(listener)")
        self.assertGreaterEqual(hook_at, 0)
        self.assertGreaterEqual(server_at, 0)
        self.assertLess(hook_at, server_at)


class MonitoringInterfaceSourceTests(unittest.TestCase):
    def test_sql_mget_test_restores_application_acl(self) -> None:
        integration = (
            ROOT / "tests" / "sql_mget_integration.py"
        ).read_text(encoding="utf-8")
        cleanup = integration[integration.index("    finally:\n") :]
        for statement in (
            "local_cache.mget(regclass, anyarray)",
            "REVOKE USAGE ON SCHEMA local_cache",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, cleanup)

    def test_mapping_reload_uses_a_resettable_context_and_two_hard_limits(self) -> None:
        reload_mappings = c_function(WORKER, "reload_mappings")
        self.assertGreaterEqual(
            reload_mappings.count("MemoryContextReset(mapping_context)"), 2
        )
        self.assertIn(
            "configured_mapping_count > PGLC_MAX_MAPPINGS", reload_mappings
        )
        self.assertIn("SPI_processed > PGLC_MAX_MAPPINGS", reload_mappings)
        allocation_at = reload_mappings.index("MemoryContextAllocZero(mapping_context")
        configured_limit_at = reload_mappings.index(
            "configured_mapping_count > PGLC_MAX_MAPPINGS"
        )
        result_limit_at = reload_mappings.index(
            "SPI_processed > PGLC_MAX_MAPPINGS"
        )
        self.assertLess(configured_limit_at, allocation_at)
        self.assertLess(result_limit_at, allocation_at)

    def test_stats_expose_capacity_memory_and_fallback_signals(self) -> None:
        stats = c_function(CORE, "pglc_stats_json")
        required = (
            "memory_budget_bytes",
            "estimated_memory_bytes",
            "shared_memory_bytes",
            "worker_memory_bytes",
            "max_clients",
            "client_limit_rejections",
            "dirty_key_limit_fallbacks",
        )
        for field in required:
            with self.subTest(field=field):
                self.assertIn(f'\\"{field}\\"', stats)
        self.assertTrue(
            any(
                f'\\"{field}\\"' in stats
                for field in ("client_capacity", "client_slots", "total_client_slots")
            ),
            "stats must publish the physically allocated RESP slot capacity",
        )

    def test_sql_metrics_are_typed_stable_and_not_public(self) -> None:
        metrics = re.search(
            r"CREATE\s+FUNCTION\s+metrics\s*\(\s*\)"
            r"(?P<body>.*?)(?=\n(?:CREATE|REVOKE|GRANT)\b|\Z)",
            INSTALL_SQL,
            flags=re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(metrics, "local_cache.metrics() is missing")
        definition = metrics.group(0)
        self.assertRegex(definition, r"(?i)RETURNS\s+TABLE\s*\(")
        self.assertRegex(definition, r"(?i)\b(?:bigint|double\s+precision|numeric)\b")
        self.assertRegex(definition, r"(?i)\bSTABLE\b")
        self.assertRegex(
            INSTALL_SQL,
            r"(?i)REVOKE\s+ALL\s+ON\s+FUNCTION\s+metrics\s*\(\s*\)"
            r"\s+FROM\s+PUBLIC\s*;",
        )
        self.assertIn("workers_with_incomplete_mappings", definition)
        self.assertIn(
            "mapping_reload_incomplete_retries_total", definition
        )
        health = re.search(
            r"CREATE\s+FUNCTION\s+health\s*\(\s*\).*?\n\$function\$;",
            INSTALL_SQL,
            flags=re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(health)
        self.assertIn("workers_with_incomplete_mappings", health.group(0))
        self.assertIn("= 0", health.group(0))

    def test_entrypoint_validates_and_writes_every_memory_guard(self) -> None:
        variables = (
            ("PG_LOCAL_CACHE_MEMORY_BUDGET_MB", "memory_budget_mb"),
            ("PG_LOCAL_CACHE_MAX_CLIENTS", "max_clients"),
            ("PG_LOCAL_CACHE_MAX_CLIENTS_PER_WORKER", "max_clients_per_worker"),
        )
        for environment_name, guc_name in variables:
            with self.subTest(environment=environment_name):
                self.assertIn(f"${{{environment_name}:-", ENTRYPOINT)
                self.assertRegex(
                    ENTRYPOINT,
                    rf'require_integer_between\s+(?:\\\n\s*)?'
                    rf'"{environment_name}"\s+"\${guc_name}"\s+\d+\s+\d+',
                )
                self.assertIn(
                    f'printf "pg_local_cache.{guc_name} = %s\\n" '
                    f'"${guc_name}"',
                    ENTRYPOINT,
                )
        self.assertRegex(
            ENTRYPOINT,
            r'require_integer_between\s+(?:\\\n\s*)?'
            r'"PG_LOCAL_CACHE_MAX_DIRTY_KEYS"\s+"\$max_dirty_keys"'
            r"\s+128\s+16384",
        )

    def test_compose_sets_extension_and_container_budgets(self) -> None:
        for variable in (
            "PG_LOCAL_CACHE_MEMORY_BUDGET_MB",
            "PG_LOCAL_CACHE_MAX_CLIENTS",
            "PG_LOCAL_CACHE_MAX_CLIENTS_PER_WORKER",
        ):
            self.assertRegex(COMPOSE, rf"(?m)^\s+{variable}:\s*[^\s#]+")
        assert_has_container_memory_limit(self, COMPOSE, "compose.yaml")

if __name__ == "__main__":
    unittest.main()
