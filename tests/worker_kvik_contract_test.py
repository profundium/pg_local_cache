import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKER = (ROOT / "src" / "pg_local_cache_worker.c").read_text()
RESP_LIMITS = (ROOT / "src" / "resp_limits.h").read_text()
WHOLE_ROW_INTEGRATION = (
    ROOT / "tests" / "whole_row_integration.py"
).read_text()
PIPELINE_INTEGRATION = (
    ROOT / "tests" / "pipeline_integration.py"
).read_text()


class AuthTokenContractTests(unittest.TestCase):
    def test_token_file_checks_embedded_nul_using_raw_byte_length(self) -> None:
        loader = c_function(WORKER, "load_auth_token")
        self.assertIn("length = fread(", loader)
        self.assertIn("memchr(buffer, '\\0', length)", loader)
        self.assertNotIn("length = strlen(buffer)", loader)


def c_function(source: str, name: str) -> str:
    match = re.search(rf"(?m)^{re.escape(name)}\s*\(", source)
    if not match:
        raise AssertionError(f"missing C function {name}")
    start = match.start()
    depth = 0
    opened = False
    for position in range(match.end(), len(source)):
        if source[position] == "{":
            depth += 1
            opened = True
        elif source[position] == "}":
            depth -= 1
            if opened and depth == 0:
                return source[start : position + 1]
    raise AssertionError(f"unterminated C function {name}")


class KvikWireContractTests(unittest.TestCase):
    def test_attach_json_is_not_polluted_by_postgresql_notices(self):
        self.assertIn("stderr=subprocess.PIPE", WHOLE_ROW_INTEGRATION)
        self.assertNotIn("stderr=subprocess.STDOUT", WHOLE_ROW_INTEGRATION)
        self.assertIn("(result.stdout, result.stderr)", WHOLE_ROW_INTEGRATION)

    def test_crud_keys_are_scoped_to_the_current_database_and_exact_table(self):
        resolver = c_function(WORKER, "resolve_wire_key")
        self.assertIn('"CRUD:%s.%s.%s:"', resolver)
        self.assertIn("pglc_database", resolver)
        self.assertNotIn("get_database_name", resolver)
        self.assertNotIn("SearchSysCache", resolver)
        self.assertIn("ERR KVik key targets a different database", resolver)
        self.assertIn("ERR unknown KVik table mapping", resolver)

    def test_pipeline_black_box_uses_only_active_crud_wire_keys(self):
        self.assertIn(
            'f"CRUD:{PGDATABASE}.public.{table}:"', PIPELINE_INTEGRATION
        )
        self.assertIn('{"id": str(row_id)}', PIPELINE_INTEGRATION)
        self.assertNotIn('f"{namespace}:', PIPELINE_INTEGRATION)
        self.assertNotIn("namespace: str", PIPELINE_INTEGRATION)

    def test_composite_json_keys_are_complete_and_canonical(self):
        canonical = c_function(WORKER, "canonicalize_key")
        self.assertIn("JB_ROOT_IS_OBJECT", canonical)
        self.assertIn("JB_ROOT_COUNT(key_object) != mapping->key_count", canonical)
        self.assertIn("getKeyJsonValueFromContainer", canonical)
        self.assertIn("pglc_canonical_key", canonical)
        self.assertIn("mapping->key_outputs", canonical)

    def test_invalidate_supports_all_four_kvik_scopes(self):
        dispatch = c_function(WORKER, "execute_command_inner")
        self.assertIn('strcmp(scope, "CRUD") == 0', dispatch)
        self.assertIn("pglc_cache_invalidate_all()", dispatch)
        self.assertIn("pglc_cache_invalidate_database(MyDatabaseId)", dispatch)
        self.assertIn("pglc_cache_invalidate_namespace", dispatch)
        self.assertIn("pglc_cache_invalidate_key", dispatch)
        self.assertIn("pglc_database", dispatch)
        self.assertNotIn("get_database_name", dispatch)

    def test_mget_is_authenticated_bounded_and_prevalidated(self):
        dispatch = c_function(WORKER, "execute_command_inner")
        mget_at = dispatch.index('pglc_resp_arg_equals(&args[0], "MGET")')
        self.assertLess(dispatch.index("if (!client->authenticated)"), mget_at)
        self.assertIn("PGLC_MGET_MAX_KEYS + 1", dispatch)
        self.assertNotIn('pglc_resp_arg_equals(&args[0], "GET")', dispatch)

        mget = c_function(WORKER, "command_mget")
        validate_at = mget.index("resolve_wire_key(")
        deadline_at = mget.index("TimestampTzPlusMilliseconds(")
        canonical_at = mget.index("canonicalize_key(")
        count_at = mget.index("client_mget_keys")
        read_at = mget.index("command_mget_one(")
        self.assertLess(deadline_at, validate_at)
        self.assertLess(validate_at, canonical_at)
        self.assertLess(canonical_at, count_at)
        self.assertLess(count_at, read_at)
        self.assertIn("PGLC_RESPONSE_MAX - element_length", mget)
        self.assertIn('element[0] == \'-\'', mget)
        self.assertNotIn("pglc_cache_lookup", mget)
        self.assertIn("TimestampTzPlusMilliseconds(", mget)
        self.assertIn("pglc_statement_timeout_ms", mget)
        self.assertIn("raw_keys[key_index], deadline", mget)
        self.assertIn("ERR MGET deadline exceeded", mget)

        one = c_function(WORKER, "command_mget_one")
        wait_loop = one[one.index("for (;;)") :]
        self.assertIn("TimestampDifferenceMilliseconds(", one)
        self.assertIn("begin_spi_transaction(statement_timeout_ms)", one)
        self.assertIn("ERR MGET deadline exceeded", one)
        self.assertLess(
            wait_loop.index("GetCurrentTimestamp() >= deadline"),
            wait_loop.index("pglc_cache_claim_load("),
        )
        self.assertLess(
            one.index("ERR MGET deadline exceeded"),
            one.index("begin_spi_transaction(statement_timeout_ms)"),
        )
        self.assertIn("#define PGLC_MGET_MAX_KEYS 1024", RESP_LIMITS)


class WholeRowWorkerContractTests(unittest.TestCase):
    def test_mget_stores_validated_tuple_payload_and_returns_row_json(self):
        one = c_function(WORKER, "command_mget_one")
        self.assertIn("pglc_row_payload_encode", one)
        self.assertIn("PGLC_ROW_PAYLOAD_FLAG_HAS_JSON", one)
        self.assertIn("cached_row_json", one)
        self.assertIn("source_row_json", one)
        self.assertIn("row JSON exceeds the RESP limit", one)
        self.assertIn("database_payload_cacheable", one)
        self.assertIn("pglc_cache_lookup_quiet", one)
        self.assertIn("note_resp_cache_lookup(true, false)", one)
        self.assertIn("note_resp_cache_lookup(false, false)", one)
        self.assertNotIn("pglc_cache_lookup(mapping, canonical", one)

        source = c_function(WORKER, "source_row_json")
        self.assertIn("slot_getallattrs(slot)", source)
        self.assertIn("toast_raw_datum_size", source)
        self.assertIn("raw_attribute_bytes", source)
        self.assertLess(source.index("toast_raw_datum_size"), source.index("F_ROW_TO_JSON_RECORD"))

    def test_set_keeps_wire_pk_authoritative_and_rejects_unknown_columns(self):
        validate = c_function(WORKER, "row_json_validate")
        setter = c_function(WORKER, "command_set")
        self.assertIn("get_attnum(mapping->relation_oid, column_name) <= 0", validate)
        self.assertIn("does not match the wire key", validate)
        self.assertIn("if (json_value == NULL)", validate)
        self.assertIn("JsonbPGetDatum(row)", setter)
        self.assertIn("row_json_validate", setter)

    def test_loader_builds_parameterized_whole_row_plans(self):
        reload = c_function(WORKER, "reload_mappings")
        self.assertIn("m.key_columns", reload)
        self.assertIn("source_mapping.key_columns", reload)
        self.assertNotIn("ARRAY[source_mapping.key_column]", reload)
        self.assertNotIn("get_attnum(mapping_relation_oid", reload)
        self.assertNotIn("mapping_relation_oid", reload)
        self.assertIn("jsonb_populate_record", reload)
        self.assertIn("ON CONFLICT (%s) %s", reload)
        self.assertIn("mapping->key_count + 1", reload)
        self.assertIn("mapping->row_desc", reload)
        self.assertIn("OVERRIDING SYSTEM VALUE", reload)
        self.assertIn("attribute->attgenerated != '\\0'", reload)

    def test_loader_preserves_generated_and_identity_flags_without_constraints(self):
        reload = c_function(WORKER, "reload_mappings")
        copy_at = reload.index("CreateTupleDescCopy(source_desc)")
        generated_at = reload.index(
            "copied_attribute->attgenerated = source_attribute->attgenerated"
        )
        identity_at = reload.index(
            "copied_attribute->attidentity = source_attribute->attidentity"
        )
        close_at = reload.index("table_close(relation, NoLock)")
        self.assertLess(copy_at, generated_at)
        self.assertLess(generated_at, identity_at)
        self.assertLess(identity_at, close_at)
        self.assertNotIn("CreateTupleDescCopyConstr", reload)

    def test_loader_revalidates_primary_key_semantics_after_ddl(self):
        reload = c_function(WORKER, "reload_mappings")
        self.assertIn("i.indisprimary", reload)
        self.assertIn("i.indexprs IS NULL", reload)
        self.assertIn("am.amname = 'btree'", reload)
        self.assertIn("NOT opc.opcdefault", reload)
        self.assertIn("pc.castmethod = 'b'", reload)

    def test_loader_rejects_ambiguous_kvik_wire_identifiers(self):
        reload = c_function(WORKER, "reload_mappings")
        self.assertIn("current_database() !~ '[.:]'", reload)
        self.assertIn("n.nspname !~ '[.:]'", reload)
        self.assertIn("c.relname !~ '[.:]'", reload)

    def test_loader_rejects_untrusted_trigger_and_source_provenance(self):
        reload = c_function(WORKER, "reload_mappings")
        self.assertGreaterEqual(reload.count("deptype = 'x'"), 3)
        self.assertIn("source_ext.extnamespace = n.oid", reload)
        self.assertIn("source_dep.deptype = 'e'", reload)
        self.assertIn("n.nspname !~ '^pg_'", reload)
        self.assertIn("n.nspname <> 'information_schema'", reload)
        self.assertIn(
            "c.relowner <> CURRENT_USER::pg_catalog.regrole", reload
        )
        for role_guard in (
            "worker_role.rolcanlogin",
            "NOT worker_role.rolsuper",
            "NOT worker_role.rolinherit",
            "NOT worker_role.rolcreatedb",
            "NOT worker_role.rolcreaterole",
            "NOT worker_role.rolreplication",
            "NOT worker_role.rolbypassrls",
        ):
            self.assertIn(role_guard, reload)

    def test_loader_revalidates_writable_and_read_only_mapping_shape(self):
        reload = c_function(WORKER, "reload_mappings")
        self.assertIn("m.writable OR (", reload)
        for privilege in ("INSERT", "UPDATE", "DELETE"):
            self.assertIn(
                f"NOT pg_catalog.has_table_privilege(c.oid, '{privilege}')",
                reload,
            )
        self.assertIn(
            "wa.attgenerated <> ''", reload
        )
        self.assertIn("wa.attname = ANY (m.key_columns)", reload)


if __name__ == "__main__":
    unittest.main()
