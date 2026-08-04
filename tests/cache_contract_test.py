#!/usr/bin/env python3
"""Source contracts for cache fill ownership and transaction fences.

These checks complement Docker integration tests.  They intentionally pin the
small pieces that are easy to lose during hot-path refactors: a timed-out
follower must not publish, invalidation must revoke an obsolete loader, and an
evicted/recreated key must not reuse a former entry generation.
"""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CORE = (ROOT / "src" / "pg_local_cache.c").read_text(encoding="utf-8")
WORKER = (ROOT / "src" / "pg_local_cache_worker.c").read_text(
    encoding="utf-8"
)
HEADER = (ROOT / "src" / "pg_local_cache.h").read_text(encoding="utf-8")
SQL_FASTPATH = (ROOT / "src" / "pg_local_cache_sql.c").read_text(
    encoding="utf-8"
)
INSTALL_SQL = (ROOT / "sql" / "pg_local_cache--1.1.0.sql").read_text(
    encoding="utf-8"
)
ENTRYPOINT = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
HEALTHCHECK = (ROOT / "docker" / "healthcheck.sh").read_text(encoding="utf-8")
SQL_ONLY_COMPOSE = (ROOT / "compose.sql-only.yaml").read_text(encoding="utf-8")
COMPOSE = (ROOT / "compose.yaml").read_text(encoding="utf-8")


def c_function(source: str, name: str) -> str:
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


class CacheOwnershipSourceTests(unittest.TestCase):
    def test_admin_paths_lock_the_exact_relation_oid_until_transaction_end(self) -> None:
        relation_lock = c_function(CORE, "pg_local_cache_lock_relation")
        self.assertIn("PG_GETARG_OID(0)", relation_lock)
        self.assertIn(
            "LockRelationOid(relation_oid, ShareRowExclusiveLock)",
            relation_lock,
        )
        self.assertIn("get_rel_name(relation_oid) == NULL", relation_lock)
        self.assertIn(
            "UnlockRelationOid(relation_oid, ShareRowExclusiveLock)",
            relation_lock,
        )
        self.assertIn(
            "PG_FUNCTION_INFO_V1(pg_local_cache_lock_relation)", CORE
        )

    def test_sql_fast_path_requires_the_current_key_columns_catalog(self) -> None:
        mapping = c_function(SQL_FASTPATH, "pglc_sql_read_mapping_once")
        self.assertIn('get_attnum(mapping_oid, "key_columns")', mapping)
        self.assertNotIn('get_attnum(mapping_oid, "key_column")', mapping)
        self.assertNotIn("legacy_key_attno", mapping)
        self.assertIn("deconstruct_array(key_array, NAMEOID", mapping)
        self.assertIn("meta->key_columns[key_index]", mapping)
        self.assertNotIn("value_attno", mapping)

        for alias in (
            "key_column[NAMEDATALEN]",
            "key_type;",
            "key_ioparam;",
            "key_typmod;",
            "key_input;",
            "key_output;",
        ):
            self.assertNotIn(alias, HEADER)
        for alias in (
            "key_column",
            "key_type",
            "key_ioparam",
            "key_typmod",
            "key_input",
            "key_output",
        ):
            self.assertNotIn(f"mapping->{alias} =", WORKER)
            self.assertNotIn(f"mapping.{alias} =", SQL_FASTPATH)

    def test_every_new_cache_slot_gets_a_unique_generation(self) -> None:
        entry_lookup = c_function(CORE, "get_cache_entry")
        self.assertIn("entry->version = next_entry_generation();", entry_lookup)
        self.assertIn("pg_atomic_uint64 entry_generation;", HEADER)
        self.assertIn(
            "pg_atomic_init_u64(&pglc_shared->entry_generation, 0);", CORE
        )

    def test_cache_hash_ignores_fixed_size_padding_without_aliasing_keys(
        self,
    ) -> None:
        startup = c_function(CORE, "pglc_shmem_startup")
        self.assertIn("control.hash = pglc_cache_key_hash", startup)
        self.assertIn("control.match = pglc_cache_key_match", startup)
        self.assertIn("HASH_FUNCTION | HASH_COMPARE", startup)

        key_hash = c_function(CORE, "pglc_cache_key_hash")
        self.assertIn("hash_bytes_extended", key_hash)
        self.assertIn("strnlen(cache_key->nspace", key_hash)
        self.assertIn("strnlen(cache_key->key", key_hash)
        self.assertNotIn("sizeof(PgLocalCacheCacheKey)", key_hash)
        self.assertNotIn(
            "hash_bytes_extended((const unsigned char *) key", key_hash
        )

        key_match = c_function(CORE, "pglc_cache_key_match")
        for field in ("database_oid", "nspace", "key"):
            self.assertIn(field, key_match)

        make_key = c_function(CORE, "make_cache_key")
        self.assertIn("if (initialize_padding)", make_key)
        entry_lookup = c_function(CORE, "get_cache_entry")
        self.assertIn(
            "make_cache_key(&cache_key, database_oid, nspace, key, create)",
            entry_lookup,
        )

    def test_sql_meta_cache_slot_collisions_fail_closed(self) -> None:
        slot = c_function(SQL_FASTPATH, "pglc_sql_meta_cache_entry")
        self.assertIn("PGLC_SQL_META_CACHE_SLOTS - 1", slot)

        # OIDs with identical low slot bits deliberately collide.  Every read
        # must compare the full OID before trusting the direct-mapped slot.
        self.assertEqual(1 & (32 - 1), 33 & (32 - 1))
        for reader_name in (
            "pglc_sql_cached_mapping",
            "pglc_sql_cached_relation_meta",
        ):
            reader = c_function(SQL_FASTPATH, reader_name)
            oid_check = reader.index("entry->relation_oid !=")
            success = reader.index("return true")
            self.assertLess(oid_check, success)

        invalidation = c_function(
            SQL_FASTPATH, "pglc_sql_relcache_invalidation"
        )
        self.assertIn("entry->relation_oid == relation_oid", invalidation)
        remember = c_function(SQL_FASTPATH, "pglc_sql_remember_mapping")
        self.assertLess(
            remember.index("MemSet(entry, 0, sizeof(*entry))"),
            remember.index("entry->relation_oid = relation_oid"),
        )

    def test_sql_metadata_cache_is_generation_and_relcache_fenced(self) -> None:
        init = c_function(SQL_FASTPATH, "pglc_sql_init")
        self.assertIn("CacheRegisterRelcacheCallback", init)

        invalidation = c_function(
            SQL_FASTPATH, "pglc_sql_relcache_invalidation"
        )
        self.assertIn("!OidIsValid(relation_oid)", invalidation)
        self.assertGreaterEqual(
            invalidation.count("relation_validated = false"), 2
        )

        cached_mapping = c_function(
            SQL_FASTPATH, "pglc_sql_cached_mapping"
        )
        self.assertIn("entry->relation_oid != relation_oid", cached_mapping)
        self.assertIn(
            "entry->config_generation != generation", cached_mapping
        )
        read_mapping = c_function(SQL_FASTPATH, "pglc_sql_read_mapping")
        self.assertLess(
            read_mapping.index("pglc_sql_cached_mapping"),
            read_mapping.index("pglc_sql_read_mapping_once"),
        )
        self.assertLess(
            read_mapping.index("before == after"),
            read_mapping.index("pglc_sql_remember_mapping"),
        )

        planner = c_function(SQL_FASTPATH, "pglc_sql_set_rel_pathlist")
        cached_at = planner.index("pglc_sql_cached_relation_meta")
        full_at = planner.index("pglc_sql_relation_meta", cached_at)
        remember_at = planner.index("pglc_sql_remember_relation_meta", full_at)
        self.assertLess(cached_at, full_at)
        self.assertLess(full_at, remember_at)

        runtime = c_function(SQL_FASTPATH, "pglc_sql_validate_runtime")
        cached_at = runtime.index("pglc_sql_cached_relation_meta")
        full_at = runtime.index("pglc_sql_relation_meta", cached_at)
        self.assertLess(cached_at, full_at)
        self.assertIn("pglc_sql_meta_matches_state", runtime[cached_at:full_at])
        for guard in (
            "relkind != RELKIND_RELATION",
            "relpersistence != RELPERSISTENCE_PERMANENT",
            "relam != HEAP_TABLE_AM_OID",
            "relispartition",
            "relrowsecurity",
            "relforcerowsecurity",
        ):
            self.assertIn(guard, runtime[:cached_at])

    def test_sql_integer_keys_avoid_fmgr_allocation(self) -> None:
        codec = (ROOT / "src" / "key_codec.c").read_text(encoding="utf-8")
        typed = c_function(codec, "pglc_canonical_key_typed")
        self.assertIn("key_types[component] == INT2OID", typed)
        self.assertIn("key_types[component] == INT4OID", typed)
        self.assertIn("key_types[component] == INT8OID", typed)
        self.assertIn("pg_ltoa", typed)
        self.assertIn("pg_lltoa", typed)
        self.assertIn("free_rendered", typed)
        access = c_function(SQL_FASTPATH, "pglc_sql_access")
        self.assertIn("pglc_canonical_key_typed", access)
        self.assertIn("state->key_types", access)

        begin = c_function(SQL_FASTPATH, "pglc_sql_begin")
        output_setup = begin.index("getTypeOutputInfo(")
        integer_guard = begin.index(
            "state->key_types[key_index] != INT2OID"
        )
        self.assertLess(integer_guard, output_setup)
        self.assertIn(
            "state->key_types[key_index] != INT4OID",
            begin[integer_guard:output_setup],
        )
        self.assertIn(
            "state->key_types[key_index] != INT8OID",
            begin[integer_guard:output_setup],
        )

    def test_store_requires_the_active_loader_id_and_fences_late_fill(self) -> None:
        store = c_function(CORE, "pglc_cache_store")
        self.assertIn(
            "load_id != 0 && entry->loading && entry->load_id == load_id",
            store,
        )
        generation_at = store.index("entry->version = next_entry_generation();")
        publish_at = store.index("entry->valid = true;")
        self.assertLess(generation_at, publish_at)
        self.assertIn("entry->loading = false;", store)

        command_get = c_function(WORKER, "command_get")
        self.assertGreaterEqual(command_get.count("owns_load ? load_id : 0"), 2)

    def test_claim_rejects_loader_from_an_obsolete_transaction_fence(self) -> None:
        claim = c_function(CORE, "pglc_cache_claim_load")
        for field in (
            "load_global_version",
            "load_relation_version",
            "load_key_version",
        ):
            self.assertIn(field, HEADER)
            self.assertIn(field, claim)
        self.assertIn("entry->loading = false;", claim)
        self.assertIn("entry->load_id++;", claim)

    def test_release_cannot_clear_a_recreated_entry_loader(self) -> None:
        release = c_function(CORE, "pglc_cache_release_load")
        self.assertIn("const PgLocalCacheReadToken *claim_token", release)
        for fence in (
            "claim_token->cacheable",
            "claim_token->has_entry",
            "entry->key.database_oid == MyDatabaseId",
            "strcmp(entry->key.nspace, mapping->nspace) == 0",
            "strcmp(entry->key.key, canonical_key) == 0",
            "entry->relation_oid == mapping->relation_oid",
            "entry->version == claim_token->key_version",
            "entry->load_global_version == claim_token->global_version",
            "entry->load_relation_version == claim_token->relation_version",
            "entry->load_key_version == claim_token->key_version",
            "entry->load_id == load_id",
        ):
            self.assertIn(fence, release)
        for mutable_current_fence in (
            "pglc_shared->config_generation",
            "pglc_shared->global_version == claim_token->global_version",
            "relation_state->version == claim_token->relation_version",
        ):
            self.assertNotIn(mutable_current_fence, release)
        self.assertIn(
            "const PgLocalCacheReadToken *claim_token", HEADER
        )
        self.assertIn(
            "pglc_cache_release_load(mapping, canonical, &token, load_id)",
            WORKER,
        )

    def test_eviction_skips_live_loads_but_can_reclaim_expired_ones(self) -> None:
        eviction = c_function(CORE, "evict_one_cache_entry")
        active_check = eviction.index("cache_load_is_active_locked(entry, now)")
        counted = eviction.index("scanned++")
        sample_bound = eviction.index("scanned >= PGLC_EVICTION_SAMPLE")
        self.assertLess(counted, active_check)
        self.assertLess(active_check, sample_bound)
        expiry = c_function(CORE, "cache_load_is_active_locked")
        self.assertIn("TimestampDifferenceExceeds", expiry)
        self.assertIn("entry->version = next_entry_generation();", expiry)

    def test_cache_hits_reuse_the_current_admission_clock(self) -> None:
        lookup = c_function(CORE, "cache_lookup_locked")
        self.assertIn(
            "access_clock = pg_atomic_read_u64(&pglc_shared->clock);", lookup
        )
        self.assertNotIn(
            "pg_atomic_fetch_add_u64(&pglc_shared->clock", lookup
        )

        store = c_function(CORE, "pglc_cache_store")
        self.assertIn(
            "pg_atomic_fetch_add_u64(&pglc_shared->clock, 1) + 1", store
        )

    def test_waiter_metric_is_once_per_request_not_once_per_poll(self) -> None:
        claim = c_function(CORE, "pglc_cache_claim_load")
        self.assertNotIn("singleflight_waiters", claim)
        command_get = c_function(WORKER, "command_get")
        self.assertIn("waiter_counted", command_get)
        self.assertEqual(command_get.count("pglc_note_singleflight_waiter();"), 1)

    def test_follower_retries_after_owner_publishes_new_generation(self) -> None:
        claim = c_function(CORE, "pglc_cache_claim_load")
        current_at = claim.index("if (cache_entry_is_current_locked")
        version_retry_at = claim.index(
            "if (entry->version != token->key_version)", current_at
        )
        loader_cleanup_at = claim.index("if (entry->loading &&", current_at)
        self.assertNotIn(
            "entry->version != token->key_version", claim[:current_at]
        )
        self.assertLess(current_at, version_retry_at)
        self.assertLess(version_retry_at, loader_cleanup_at)
        self.assertIn(
            "result = PGLC_LOAD_RETRY;", claim[current_at:version_retry_at]
        )

    def test_sql_xmin_has_a_full_xid_age_fence(self) -> None:
        store = c_function(CORE, "pglc_cache_store")
        lock_at = store.index(
            "LWLockAcquire(pglc_shared->lock, LW_EXCLUSIVE);"
        )
        horizon_at = store.index("ReadNextFullTransactionId()")
        self.assertLess(horizon_at, lock_at)
        self.assertIn("source_observed_full_xid", HEADER)
        self.assertIn(
            "entry->source_observed_full_xid = observed_full_xid;", store
        )

        visible = c_function(SQL_FASTPATH, "pglc_sql_source_visibility")
        visible_at = c_function(SQL_FASTPATH, "pglc_sql_source_visibility_at")
        self.assertIn("ReadNextFullTransactionId()", visible)
        self.assertIn("current_full_xid < source_observed_full_xid", visible_at)
        self.assertIn("UINT64CONST(0x80000000)", visible_at)
        self.assertIn("PGLC_SOURCE_AGE_EXPIRED", visible_at)
        self.assertIn("PGLC_SOURCE_SNAPSHOT_REJECTED", visible_at)
        self.assertIn("XidInMVCCSnapshot(source_xmin, snapshot)", visible_at)

        retire = c_function(CORE, "pglc_cache_retire_positive")
        for fence in (
            "token->cacheable",
            "token->has_entry",
            "token->config_generation",
            "token->global_version",
            "token->relation_version",
            "token->key_version",
            "token->source_observed_full_xid",
            "global_dirty_writers == 0",
            "relation_state->dirty_writers == 0",
            "entry->dirty_writers == 0",
            "cache_entry_is_current_locked",
            "!entry->negative",
            "expected_xmin",
        ):
            self.assertIn(fence, retire)
        self.assertIn("entry->valid = false", retire)
        self.assertIn("entry->loading = false", retire)
        self.assertIn("entry->version = next_entry_generation()", retire)

        access = c_function(SQL_FASTPATH, "pglc_sql_access")
        self.assertIn("lookup_attempt < 2", access)
        age_at = access.index("PGLC_SOURCE_AGE_EXPIRED")
        retire_at = access.index("pglc_cache_retire_positive")
        second_lookup_at = access.index("pglc_cache_lookup_quiet")
        claim_at = access.index("pglc_cache_claim_load")
        self.assertLess(age_at, retire_at)
        self.assertLess(second_lookup_at, claim_at)

    def test_cross_type_integer_keys_are_coerced_not_reinterpreted(self) -> None:
        supported = c_function(SQL_FASTPATH, "pglc_sql_key_input_supported")
        self.assertIn("key_type == INT8OID", supported)
        self.assertIn("expression_type == INT4OID", supported)
        coercion = c_function(SQL_FASTPATH, "pglc_sql_coerce_key_expr")
        self.assertIn("coerce_to_target_type", coercion)
        self.assertIn("COERCION_IMPLICIT", coercion)
        matcher = c_function(SQL_FASTPATH, "pglc_sql_match_clauses")
        self.assertIn(
            "pglc_sql_coerce_key_expr(\n"
            "\t\t\tother, meta->key_types[key_index], "
            "meta->key_typmods[key_index])",
            matcher,
        )
        self.assertIn("restrict_infos[key_index] = rinfo", matcher)
        self.assertIn("ordered_exprs[key_index] = coerced_other", matcher)
        self.assertIn("restrict_infos[key_index] != NULL", matcher)
        self.assertNotIn("operator->opno != type_cache->eq_opr", matcher)

    def test_sql_cache_rejects_aliasing_custom_btree_families(self) -> None:
        relation_meta = c_function(
            SQL_FASTPATH, "pglc_sql_relation_base_meta"
        )
        index_path = c_function(SQL_FASTPATH, "pglc_sql_primary_index_path")
        self.assertIn("TYPECACHE_BTREE_OPFAMILY", relation_meta)
        self.assertIn(
            "index_info->opfamily[key_index] !=\n"
            "\t\t\t\t\tmeta->key_btree_opfamilies[key_index]",
            index_path,
        )
        self.assertIn("index_info->indexkeys[key_index]", index_path)
        self.assertIn("match_index_to_operand(left, key_index", index_path)
        self.assertIn("match_index_to_operand(right, key_index", index_path)
        self.assertIn("get_op_opfamily_strategy(index_operator", index_path)
        self.assertIn("meta->key_btree_opfamilies[key_index]", index_path)

    def test_sql_fast_path_requires_source_and_trigger_provenance(self) -> None:
        source = c_function(
            SQL_FASTPATH, "pglc_sql_source_relation_allowed"
        )
        self.assertIn('strncmp(namespace_name, "pg_", 3)', source)
        self.assertIn('strcmp(namespace_name, "information_schema")', source)
        self.assertIn('strcmp(namespace_name, "local_cache")', source)
        self.assertIn('get_extension_oid("pg_local_cache", true)', source)
        self.assertIn(
            "getExtensionOfObject(NamespaceRelationId, namespace_oid) == extension_oid",
            source,
        )
        self.assertIn(
            "getExtensionOfObject(RelationRelationId,", source
        )

        base_meta = c_function(SQL_FASTPATH, "pglc_sql_relation_base_meta")
        normalizer = c_function(
            SQL_FASTPATH, "pglc_sql_normalize_query_inheritance"
        )
        self.assertIn("check_catalog_provenance", base_meta)
        self.assertIn("pglc_sql_source_relation_allowed(relation)", base_meta)
        self.assertIn(
            "pglc_sql_source_relation_allowed(relation)", normalizer
        )

        ownership = c_function(
            SQL_FASTPATH, "pglc_sql_trigger_owned_by_extension"
        )
        self.assertIn(
            "getAutoExtensionsOfObject(TriggerRelationId, trigger_oid)",
            ownership,
        )
        self.assertIn("list_member_oid(extension_oids, extension_oid)", ownership)
        triggers = c_function(SQL_FASTPATH, "pglc_sql_triggers_valid")
        self.assertIn('get_extension_oid("pg_local_cache", true)', triggers)
        self.assertEqual(
            triggers.count("pglc_sql_trigger_owned_by_extension"), 3
        )

        runtime = c_function(SQL_FASTPATH, "pglc_sql_validate_runtime")
        generation_at = runtime.index(
            "current_generation = pglc_config_generation()"
        )
        validation_at = runtime.index("pglc_sql_relation_meta")
        self.assertLess(generation_at, validation_at)
        self.assertIn(
            "pglc_sql_relation_meta(relation, &planned_meta, true)",
            runtime,
        )
        self.assertIn(
            "pglc_sql_relation_meta(relation, &current_meta, true)", runtime
        )

    def test_sql_fast_path_uses_only_the_primary_index(self) -> None:
        relation_meta = c_function(
            SQL_FASTPATH, "pglc_sql_relation_base_meta"
        )
        self.assertIn("RelationGetPrimaryKeyIndex(relation)", relation_meta)
        self.assertIn("SearchSysCache1(INDEXRELID", relation_meta)
        self.assertIn("index->indisprimary", relation_meta)
        self.assertIn("ReleaseSysCache(index_tuple)", relation_meta)

        index_path = c_function(SQL_FASTPATH, "pglc_sql_primary_index_path")
        self.assertIn(
            "index_info->indexoid != meta->primary_index_oid", index_path
        )
        self.assertNotIn("SearchSysCache1", index_path)

    def test_worker_trigger_query_uses_real_pg16_catalog_columns(self) -> None:
        # tgisclone/tgnattr exist only in the relcache Trigger C struct; the
        # SQL catalog represents them as tgparentid and tgattr respectively.
        self.assertNotIn("rt.tgisclone", WORKER)
        self.assertNotIn("tt.tgisclone", WORKER)
        self.assertNotIn("rt.tgnattr", WORKER)
        self.assertNotIn("tt.tgnattr", WORKER)
        self.assertIn("rt.tgparentid = 0", WORKER)
        self.assertIn("cardinality(rt.tgattr) = 0", WORKER)

    def test_statement_guard_fences_nested_trigger_reads(self) -> None:
        guard = c_function(CORE, "pg_local_cache_statement_guard")
        self.assertIn("TRIGGER_FIRED_BEFORE", guard)
        self.assertIn("TRIGGER_FIRED_FOR_STATEMENT", guard)
        for event in (
            "TRIGGER_TYPE_INSERT",
            "TRIGGER_TYPE_UPDATE",
            "TRIGGER_TYPE_DELETE",
            "TRIGGER_TYPE_TRUNCATE",
        ):
            self.assertIn(event, guard)
        self.assertIn("trigger_data->tg_trigger->tgnargs != 0", guard)
        self.assertIn("get_local_dirty_hash()", guard)
        self.assertNotIn("pglc_collect_", guard)
        publish = c_function(CORE, "pglc_publish_dirty")
        empty_at = publish.index("hash_get_num_entries(local_dirty_hash) == 0")
        shared_lock_at = publish.index(
            "LWLockAcquire(pglc_shared->lock, LW_EXCLUSIVE)"
        )
        self.assertLess(empty_at, shared_lock_at)

        trigger_validation = c_function(SQL_FASTPATH, "pglc_sql_triggers_valid")
        self.assertIn("pg_local_cache_statement_guard", trigger_validation)
        self.assertIn('"_statement_guard"', trigger_validation)
        self.assertIn("TRIGGER_TYPE_BEFORE", trigger_validation)
        self.assertIn("return guard_found && row_found && truncate_found", trigger_validation)

        self.assertIn("gt.tgtype = 62 AND gt.tgnargs = 0", WORKER)
        self.assertIn("octet_length(gt.tgargs) = 0", WORKER)
        self.assertIn("gt.tgparentid = 0", WORKER)
        self.assertIn("gt.tgqual IS NULL", WORKER)
        self.assertIn("local_cache._statement_guard()", WORKER)
        self.assertIn(
            "BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE", INSTALL_SQL
        )
        self.assertIn(
            "ENABLE ALWAYS TRIGGER pg_local_cache_statement_guard", INSTALL_SQL
        )
        self.assertIn(
            "REVOKE ALL ON FUNCTION _statement_guard() FROM PUBLIC", INSTALL_SQL
        )

    def test_runtime_refreshes_only_an_identical_mapping_generation(self) -> None:
        runtime = c_function(SQL_FASTPATH, "pglc_sql_validate_runtime")
        self.assertIn("pglc_sql_read_mapping", runtime)
        self.assertIn("pglc_sql_relation_meta", runtime)
        generation_at = runtime.index(
            "state->mapping.config_generation == current_generation"
        )
        mapping_scan_at = runtime.index("pglc_sql_read_mapping")
        self.assertLess(generation_at, mapping_scan_at)
        for field in (
            "relation_oid",
            "nspace",
            "key_count",
            "row_type_oid",
            "row_natts",
            "row_fingerprint",
            "key_attnos",
            "key_types",
            "key_typmods",
            "key_columns",
        ):
            self.assertIn(f"current_meta.{field}", runtime)
        self.assertIn(
            "state->mapping.config_generation = current_meta.config_generation",
            runtime,
        )
        can_use = c_function(SQL_FASTPATH, "pglc_sql_can_use_cache")
        self.assertIn(
            "state->mapping.config_generation == pglc_config_generation()",
            can_use,
        )

    def test_mapping_reload_backoff_is_scoped_to_one_generation(self) -> None:
        retry = c_function(WORKER, "maybe_reload_mappings")
        self.assertIn("generation == worker_retry_generation", retry)
        self.assertIn("reload_mappings(generation)", retry)

        reload_mappings = c_function(WORKER, "reload_mappings")
        self.assertIn("uint64 target_generation", reload_mappings)
        self.assertGreaterEqual(
            reload_mappings.count(
                "worker_retry_generation = target_generation;"
            ),
            2,
        )
        self.assertIn("worker_retry_generation = 0;", reload_mappings)

    def test_mapping_reload_takes_the_final_plan_lock_without_an_upgrade(self) -> None:
        reload_mappings = c_function(WORKER, "reload_mappings")
        lock_at = reload_mappings.index(
            "mapping->writable ? RowExclusiveLock : AccessShareLock"
        )
        plan_at = reload_mappings.index("prepare_kept_plan(", lock_at)
        self.assertLess(lock_at, plan_at)

    def test_mapping_health_tracks_every_worker_generation(self) -> None:
        self.assertIn("#define PGLC_MAX_WORKERS 32", HEADER)
        self.assertIn(
            "worker_mapping_generations[PGLC_MAX_WORKERS]", HEADER
        )
        self.assertIn(
            "pg_atomic_init_u64(\n"
            "\t\t\t\t&pglc_shared->worker_mapping_generations[worker_index], 0)",
            CORE,
        )

        publish = c_function(WORKER, "set_worker_mapping_generation")
        self.assertIn("worker_mapping_generations[worker_slot]", publish)
        reload_mappings = c_function(WORKER, "reload_mappings")
        self.assertIn(
            "set_worker_mapping_generation(target_generation)",
            reload_mappings,
        )
        self.assertGreaterEqual(
            reload_mappings.count("set_worker_mapping_generation(0)"), 2
        )
        exit_cleanup = c_function(WORKER, "worker_before_exit")
        self.assertIn("set_worker_mapping_generation(0)", exit_cleanup)

        readiness = c_function(
            CORE, "pglc_workers_without_current_mappings"
        )
        self.assertIn("worker_mapping_generations[worker_index]", readiness)
        self.assertGreaterEqual(
            readiness.count("pglc_config_generation()"), 2
        )
        self.assertIn("return (uint64) pglc_worker_count", readiness)
        for reporter in (
            c_function(CORE, "pglc_stats_json"),
            c_function(CORE, "pglc_metrics_json"),
        ):
            self.assertIn(
                "pglc_workers_without_current_mappings()", reporter
            )

    def test_inheritance_checks_do_not_trust_sticky_relhassubclass(self) -> None:
        # PostgreSQL may retain relhassubclass after the last child is dropped.
        # Registration, worker reload, and both fast-path validation stages
        # must all consult the actual pg_inherits rows so recovery is automatic.
        self.assertNotIn("c.relhassubclass", INSTALL_SQL)
        self.assertGreaterEqual(INSTALL_SQL.count("inh.inhparent = p_relation"), 2)
        self.assertGreaterEqual(INSTALL_SQL.count("inh.inhrelid = p_relation"), 2)
        self.assertGreaterEqual(INSTALL_SQL.count("v_relispartition"), 6)
        self.assertIn("inh.inhparent = d.objid", INSTALL_SQL)
        self.assertIn("inh.inhrelid = m.relation", INSTALL_SQL)
        self.assertNotIn("c.relhassubclass", WORKER)
        self.assertIn("inh.inhparent = c.oid", WORKER)
        self.assertIn("inh.inhrelid = c.oid", WORKER)
        self.assertIn("NOT c.relispartition", WORKER)

        child_check = c_function(
            SQL_FASTPATH, "pglc_sql_relation_has_children"
        )
        self.assertIn("find_inheritance_children", child_check)
        self.assertLess(
            child_check.index("!relation->rd_rel->relhassubclass"),
            child_check.index("find_inheritance_children"),
        )
        parent_check = c_function(SQL_FASTPATH, "pglc_sql_relation_has_parent")
        self.assertIn("relation->rd_rel->relispartition", parent_check)
        self.assertIn("has_superclass", parent_check)
        relation_meta = c_function(SQL_FASTPATH, "pglc_sql_relation_meta")
        simple_query = c_function(SQL_FASTPATH, "pglc_sql_simple_query")
        runtime = c_function(SQL_FASTPATH, "pglc_sql_validate_runtime")
        self.assertIn("pglc_sql_relation_has_children", relation_meta)
        self.assertIn("pglc_sql_relation_has_parent", relation_meta)
        self.assertIn("rte->inh", simple_query)
        self.assertNotIn("pglc_sql_relation_has_children", simple_query)
        self.assertIn("pglc_sql_relation_meta", runtime)
        self.assertNotIn("relhassubclass", relation_meta)
        self.assertNotIn("has_subclass", simple_query)
        self.assertNotIn("relhassubclass", runtime)

        normalizer = c_function(
            SQL_FASTPATH, "pglc_sql_normalize_query_inheritance"
        )
        self.assertIn("AccessShareLock", normalizer)
        self.assertIn("pglc_sql_read_mapping", normalizer)
        self.assertIn("pglc_sql_relation_has_children", normalizer)
        self.assertIn("rte->inh = false", normalizer)
        self.assertLess(
            normalizer.index("try_table_open"),
            normalizer.index("relation->rd_rel->relhassubclass"),
        )
        self.assertLess(
            normalizer.index("relation->rd_rel->relhassubclass"),
            normalizer.index("pglc_sql_read_mapping"),
        )
        planner = c_function(SQL_FASTPATH, "pglc_sql_planner")
        self.assertIn("pglc_sql_normalize_query_inheritance", planner)
        self.assertIn("previous_planner_hook", planner)
        self.assertIn("worker_mappings_incomplete", WORKER)
        retry = c_function(WORKER, "maybe_reload_mappings")
        self.assertIn("!worker_mappings_incomplete", retry)
        self.assertIn("worker_next_mapping_retry", retry)
        self.assertNotIn("worker_next_mapping_retry = 0", retry)

        transition = c_function(WORKER, "set_worker_mappings_incomplete")
        self.assertIn("worker_mappings_incomplete = incomplete", transition)
        self.assertNotIn("pg_atomic_", transition)
        exit_reconciler = c_function(WORKER, "worker_before_exit")
        self.assertIn("set_worker_mappings_incomplete(false)", exit_reconciler)
        worker_main = c_function(WORKER, "pg_local_cache_worker_main")
        self.assertLess(
            worker_main.index("set_worker_mappings_incomplete(true)"),
            worker_main.index("pglc_note_worker_start()"),
        )
        reload_mappings = c_function(WORKER, "reload_mappings")
        self.assertLess(
            reload_mappings.index("set_worker_mappings_incomplete(true)"),
            reload_mappings.index("PG_TRY()"),
        )


class SqlOnlyContainerSourceTests(unittest.TestCase):
    def test_port_zero_does_not_require_or_copy_a_resp_secret(self) -> None:
        self.assertIn(
            'require_integer_between "PG_LOCAL_CACHE_PORT" "$port" 0 65535',
            ENTRYPOINT,
        )
        self.assertIn("if (( port != 0 )); then", ENTRYPOINT)
        self.assertIn('runtime_token_config=""', ENTRYPOINT)
        self.assertIn(
            'pg_local_cache.auth_token_file = \'%s\'', ENTRYPOINT
        )

    def test_sql_only_healthcheck_skips_worker_and_resp_probes(self) -> None:
        self.assertIn("local_cache.health() ->> 'ready'", HEALTHCHECK)
        self.assertIn(
            "current_setting('pg_local_cache.port')::integer = 0",
            HEALTHCHECK,
        )
        resp_probe = HEALTHCHECK.index('exec 3<>"/dev/tcp/127.0.0.1/${port}"')
        early_exit = HEALTHCHECK.index("if (( port == 0 )); then")
        self.assertLess(early_exit, resp_probe)

    def test_sql_only_compose_has_no_resp_port_or_token_secret(self) -> None:
        self.assertIn('PG_LOCAL_CACHE_PORT: "0"', SQL_ONLY_COMPOSE)
        self.assertNotIn("pg_local_cache_auth_token", SQL_ONLY_COMPOSE)
        self.assertNotIn(":6380", SQL_ONLY_COMPOSE)
        self.assertIn("postgres_password", SQL_ONLY_COMPOSE)

    def test_compose_pins_pgdata_across_postgresql_14_through_18(self) -> None:
        for compose in (COMPOSE, SQL_ONLY_COMPOSE):
            self.assertIn("PGDATA: /var/lib/postgresql/data", compose)


if __name__ == "__main__":
    unittest.main()
