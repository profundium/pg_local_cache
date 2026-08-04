#!/usr/bin/env python3
"""Source contracts for the transparent SQL executor fast path."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "src" / "pg_local_cache_sql.c").read_text(encoding="utf-8")
CORE = (ROOT / "src" / "pg_local_cache.c").read_text(encoding="utf-8")
INSTALL_SQL = (ROOT / "sql" / "pg_local_cache--1.1.0.sql").read_text(
    encoding="utf-8"
)


def source_function(source: str, name: str) -> str:
    marker = f"\n{name}("
    start = source.find(marker)
    if start < 0:
        raise AssertionError(f"C function {name}() is missing")
    opening = source.find("{", start)
    if opening < 0:
        raise AssertionError(f"C function {name}() has no body")
    depth = 0
    for position in range(opening, len(source)):
        character = source[position]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[start : position + 1]
    raise AssertionError(f"C function {name}() has an unterminated body")


def c_function(name: str) -> str:
    return source_function(SOURCE, name)


def sql_function(name: str) -> str:
    marker = f"CREATE FUNCTION {name}()"
    start = INSTALL_SQL.find(marker)
    if start < 0:
        raise AssertionError(f"SQL function {name}() is missing")
    end = INSTALL_SQL.find("$function$;", start)
    if end < 0:
        raise AssertionError(f"SQL function {name}() has an unterminated body")
    return INSTALL_SQL[start : end + len("$function$;")]


class SqlExecutorFastPathContracts(unittest.TestCase):
    def test_rls_tables_are_never_cached(self) -> None:
        for function in (
            c_function("pglc_sql_relation_base_meta"),
            c_function("pglc_sql_validate_runtime"),
        ):
            self.assertIn("relation->rd_rel->relrowsecurity", function)
            self.assertIn("relation->rd_rel->relforcerowsecurity", function)

    def test_fallback_executor_is_initialized_only_when_needed(self) -> None:
        begin = c_function("pglc_sql_begin")
        self.assertIn("state->child_plan =", begin)
        self.assertIn("state->child = NULL", begin)
        self.assertIn("state->css.custom_ps = NIL", begin)
        self.assertNotIn("ExecInitNode", begin)

        initialize = c_function("pglc_sql_init_child")
        self.assertIn("if (state->child == NULL)", initialize)
        self.assertIn("state->css.ss.ps.state->es_query_cxt", initialize)
        self.assertIn("ExecInitNode(state->child_plan", initialize)
        self.assertIn("state->css.custom_ps = list_make1(state->child)", initialize)

        fallback = c_function("pglc_sql_run_child")
        self.assertLess(
            fallback.index("pglc_sql_init_child(state)"),
            fallback.index("ExecProcNode(child)"),
        )

    def test_explain_rescan_and_end_handle_a_lazy_child(self) -> None:
        explain = c_function("pglc_sql_explain")
        self.assertIn("if (state->child == NULL)", explain)
        self.assertIn("pglc_sql_init_child(state)", explain)
        self.assertLess(
            explain.index("pglc_sql_init_child(state)"),
            explain.index('ExplainPropertyText("Cache Namespace"'),
        )

        rescan = c_function("pglc_sql_rescan")
        self.assertIn("if (state->child != NULL)", rescan)
        self.assertIn("ExecReScan(state->child)", rescan)
        end = c_function("pglc_sql_end")
        self.assertIn("if (state->child != NULL)", end)
        self.assertIn("ExecEndNode(state->child)", end)

    def test_latest_visibility_slot_is_allocated_only_for_a_fill(self) -> None:
        begin = c_function("pglc_sql_begin")
        self.assertIn("state->latest_slot = NULL", begin)
        self.assertNotIn("table_slot_create", begin)

        initialize = c_function("pglc_sql_init_latest_slot")
        self.assertIn("if (state->latest_slot == NULL)", initialize)
        self.assertIn("state->css.ss.ps.state->es_query_cxt", initialize)
        self.assertIn("table_slot_create", initialize)
        store = c_function("pglc_sql_maybe_store")
        self.assertLess(
            store.index("if (load_id == 0"),
            store.index("pglc_sql_init_latest_slot(state)"),
        )

    def test_hit_payload_is_copied_once_into_an_aligned_query_buffer(self) -> None:
        create = c_function("pglc_sql_create_scan_state")
        self.assertIn(
            "state_size = MAXALIGN(sizeof(PgLocalCacheSqlScanState))", create
        )
        self.assertIn("palloc(add_size(state_size, PGLC_VALUE_MAX))", create)
        self.assertIn(
            "state->cache_buffer = ((char *) state) + state_size", create
        )
        self.assertNotIn("MemSet(state->cache_buffer", create)

        access = c_function("pglc_sql_access")
        self.assertNotIn("cached[PGLC_VALUE_MAX", access)
        self.assertIn(
            "pglc_cache_lookup_quiet(&state->mapping, canonical_key,",
            access,
        )
        self.assertIn("state->cache_buffer, PGLC_VALUE_MAX", access)
        self.assertIn("pglc_row_payload_decode_in_place(", access)
        self.assertNotIn("pglc_row_payload_decode(\n", access)

    def test_backend_row_cache_is_bounded_exact_and_commit_fenced(self) -> None:
        for bound in (
            "#define PGLC_SQL_ROW_CACHE_SETS 4096",
            "#define PGLC_SQL_ROW_CACHE_WAYS 4",
            "#define PGLC_SQL_ROW_CACHE_DATA_BYTES (16 * 1024 * 1024)",
        ):
            self.assertIn(bound, SOURCE)

        lookup = c_function("pglc_sql_row_cache_lookup")
        for exact_guard in (
            "entry->data_epoch != data_epoch",
            "entry->config_generation != mapping->config_generation",
            "entry->database_oid != MyDatabaseId",
            "entry->relation_oid != mapping->relation_oid",
            "strcmp(entry->nspace, mapping->nspace) != 0",
            "memcmp(entry->key, canonical_key, canonical_key_len) != 0",
        ):
            self.assertIn(exact_guard, lookup)

        store = c_function("pglc_sql_row_cache_store")
        self.assertIn("pglc_sql_row_cache_data_used + storage_len", store)
        self.assertIn("token->data_epoch != pglc_data_epoch()", store)
        self.assertLess(
            store.index("pglc_row_payload_decode_in_place("),
            store.index("victim->valid = true"),
        )

        access = c_function("pglc_sql_access")
        local_at = access.index("pglc_sql_row_cache_lookup(")
        shared_at = access.index("pglc_cache_lookup_quiet(")
        self.assertLess(local_at, shared_at)
        local_path = access[local_at:shared_at]
        self.assertIn("pglc_sql_source_visibility(", local_path)
        self.assertIn("row_cache_entry->source_observed_full_xid", local_path)
        self.assertNotIn("0xfffff", SOURCE)

        mget = c_function("pglc_sql_mget_common")
        self.assertEqual(mget.count("ReadNextFullTransactionId()"), 1)
        self.assertIn("pglc_sql_source_visibility_at(", mget)
        canonical_get = c_function("pglc_sql_get_canonical")
        self.assertIn("pglc_sql_source_visibility(", canonical_get)

        startup = source_function(CORE, "pglc_shmem_startup")
        self.assertIn("pg_atomic_init_u64(&pglc_shared->data_epoch, 1)", startup)
        token = source_function(CORE, "cache_lookup_locked")
        self.assertIn("token->data_epoch =", token)
        advance = source_function(CORE, "advance_global_version_locked")
        self.assertIn("pg_atomic_fetch_add_u64(&pglc_shared->data_epoch, 1)", advance)
        publish = source_function(CORE, "pglc_publish_dirty")
        self.assertIn("advance_global_version_locked()", publish)

    def test_sql_get_cold_fill_requires_latest_snapshot_proof(self) -> None:
        latest = c_function("pglc_sql_get_latest_matches")
        self.assertIn("RegisterSnapshot(GetLatestSnapshot())", latest)
        self.assertIn("SPI_execute_snapshot(", latest)
        self.assertIn("SPI_processed == 0", latest)
        self.assertIn("TransactionIdEquals(", latest)

        source = c_function("pglc_sql_get_source")
        self.assertGreaterEqual(source.count("pglc_sql_get_latest_matches("), 2)
        canonical = c_function("pglc_sql_get_canonical")
        self.assertIn("if (payload_cacheable)", canonical)
        self.assertNotIn("if (!found || payload_cacheable)", canonical)

    def test_runtime_common_path_uses_an_exact_validation_version(self) -> None:
        invalidate = c_function("pglc_sql_relcache_invalidation")
        self.assertGreaterEqual(
            invalidate.count("relation_validation_token = 0"), 2
        )
        remember = c_function("pglc_sql_remember_relation_meta")
        self.assertIn("pglc_sql_next_relation_validation_token()", remember)

        planner = c_function("pglc_sql_set_rel_pathlist")
        self.assertIn("pglc_sql_relation_validation_token(", planner)
        self.assertIn("pglc_sql_int8_const(relation_validation_token)", planner)

        runtime = c_function("pglc_sql_validate_runtime")
        fast_at = runtime.index("state->relation_validation_token != 0")
        descriptor_at = runtime.index("descriptor = RelationGetDescr(relation)")
        cached_at = runtime.index("pglc_sql_cached_relation_meta")
        provenance_at = runtime.index("pglc_sql_relation_meta")
        self.assertLess(fast_at, descriptor_at)
        self.assertLess(descriptor_at, cached_at)
        self.assertLess(cached_at, provenance_at)
        for guard in (
            "entry->relation_oid == state->mapping.relation_oid",
            "entry->config_generation == current_generation",
            "entry->mapping_known",
            "entry->mapping_found",
            "entry->relation_validated",
            "entry->relation_validation_token ==",
        ):
            self.assertIn(guard, runtime[fast_at:descriptor_at])
        self.assertGreaterEqual(
            runtime.count("state->relation_validation_token = validation_token"),
            3,
        )

    def test_inheritance_normalizer_reuses_exact_relation_validation(self) -> None:
        normalizer = c_function("pglc_sql_normalize_query_inheritance")
        generation_at = normalizer.index(
            "current_generation = pglc_config_generation()"
        )
        token_at = normalizer.index("pglc_sql_relation_validation_token(")
        open_at = normalizer.index("try_table_open")
        self.assertLess(generation_at, token_at)
        self.assertLess(token_at, open_at)

        token_fast_path = normalizer[token_at:open_at]
        self.assertIn("current_generation", token_fast_path)
        self.assertIn("rte->inh = false", token_fast_path)
        self.assertIn("continue", token_fast_path)

        fallback = normalizer[open_at:]
        for guard in (
            "RELKIND_RELATION",
            "RELPERSISTENCE_PERMANENT",
            "relation->rd_rel->relhassubclass",
            "pglc_sql_relation_has_children(relation)",
            "pglc_sql_source_relation_allowed(relation)",
            "pglc_sql_read_mapping(rte->relid, &meta)",
        ):
            self.assertIn(guard, fallback)
        self.assertIn("table_close(relation, NoLock)", fallback)

    def test_relation_validation_caches_primary_index_metadata(self) -> None:
        validation = c_function("pglc_sql_relation_base_meta")
        for guard in (
            "RelationGetPrimaryKeyIndex(relation)",
            "index->indisprimary",
            "index->indisvalid",
            "index->indisready",
            "index->indimmediate",
            "TYPECACHE_BTREE_OPFAMILY",
        ):
            self.assertIn(guard, validation)

        index_path = c_function("pglc_sql_primary_index_path")
        self.assertIn("index_info->indexoid != meta->primary_index_oid", index_path)
        self.assertIn("meta->key_btree_opfamilies[key_index]", index_path)
        self.assertNotIn("SearchSysCache1", index_path)
        self.assertNotIn("lookup_type_cache", index_path)
        self.assertNotIn("pglc_sql_index_is_primary", SOURCE)

    def test_catalog_provenance_changes_bump_the_shared_generation(self) -> None:
        ddl = sql_function("_ddl_invalidate")
        for catalog in (
            "pg_catalog.pg_proc",
            "pg_catalog.pg_extension",
            "pg_catalog.pg_trigger",
        ):
            self.assertIn(catalog, ddl)
        self.assertIn("JOIN local_cache.mapping AS m", ddl)
        self.assertIn("t.tgrelid = m.relation", ddl)
        self.assertIn("PERFORM local_cache._reload()", ddl)

        reload_function = source_function(CORE, "pg_local_cache_reload")
        self.assertIn("pglc_collect_global(true)", reload_function)
        collect_global = source_function(CORE, "pglc_collect_global")
        self.assertIn("collect_dirty(PGLC_DIRTY_GLOBAL", collect_global)
        self.assertIn("local_bump_config = true", collect_global)
        dirty = source_function(CORE, "pglc_current_transaction_is_dirty")
        self.assertIn("local_dirty_hash != NULL", dirty)
        finish = source_function(CORE, "pglc_finish_dirty")
        self.assertIn("&pglc_shared->config_generation", finish)

        runtime = c_function("pglc_sql_validate_runtime")
        self.assertIn(
            "state->mapping.config_generation == current_generation", runtime
        )
        can_use = c_function("pglc_sql_can_use_cache")
        self.assertIn(
            "state->mapping.config_generation == pglc_config_generation()",
            can_use,
        )

    def test_executor_uses_helpers_available_in_postgresql_14(self) -> None:
        self.assertNotIn("list_copy_head", SOURCE)
        self.assertNotIn("DatumGetItemPointer", SOURCE)
        source_guard = c_function("pglc_sql_source_relation_allowed")
        self.assertIn(
            "getExtensionOfObject(NamespaceRelationId, namespace_oid)",
            source_guard,
        )
        store = c_function("pglc_sql_maybe_store")
        self.assertIn("ItemPointerCopy", store)

    def test_postgresql_18_uses_split_explain_and_primary_key_apis(self) -> None:
        self.assertIn('#include "commands/explain_format.h"', SOURCE)
        self.assertIn("#if PG_VERSION_NUM >= 180000", SOURCE)
        self.assertIn("RelationGetPrimaryKeyIndex(relation, false)", SOURCE)


if __name__ == "__main__":
    unittest.main()
