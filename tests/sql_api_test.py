"""Source contracts for the whole-row table attachment API."""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SQL = (ROOT / "sql" / "pg_local_cache--1.1.0.sql").read_text()


def sql_function(name: str) -> str:
    match = re.search(
        rf"CREATE FUNCTION {re.escape(name)}\(.*?\n\$function\$;",
        SQL,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"missing SQL function {name}")
    return match.group(0)


class MappingSchemaTests(unittest.TestCase):
    def test_mapping_is_whole_row_only(self) -> None:
        mapping = SQL.partition("CREATE TABLE mapping (")[2].partition(");")[0]
        self.assertIn("relation regclass NOT NULL UNIQUE", mapping)
        self.assertIn("key_columns name[] NOT NULL", mapping)
        self.assertIn("writable boolean NOT NULL", mapping)
        self.assertNotIn("value_column", mapping)

    def test_primary_keys_are_bounded_and_ordered(self) -> None:
        mapping = SQL.partition("CREATE TABLE mapping (")[2].partition(");")[0]
        self.assertIn("cardinality(key_columns) BETWEEN 1 AND 16", mapping)
        primary_key = sql_function("_primary_key_columns")
        self.assertIn("ORDER BY key.key_position", primary_key)
        self.assertIn("i.indisprimary", primary_key)


class AttachmentSafetyTests(unittest.TestCase):
    def test_attach_result_identifies_the_whole_row_contract(self) -> None:
        result = sql_function("_mapping_result")
        self.assertIn("'whole_row', true", result)

    def test_attach_uses_the_primary_key_and_one_registration_path(self) -> None:
        attach = sql_function("attach_table")
        self.assertIn("local_cache._validate_attach_relation(p_relation)", attach)
        self.assertIn("local_cache._primary_key_columns(p_relation)", attach)
        self.assertIn("local_cache._register_mapping(", attach)
        self.assertIn("local_cache._mapping_result(", attach)
        self.assertNotIn("value_column", attach)

    def test_registration_locks_metadata_and_installs_three_owned_triggers(self) -> None:
        register = sql_function("_register_mapping")
        self.assertIn("LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE", register)
        for trigger in (
            "pg_local_cache_statement_guard",
            "pg_local_cache_row_invalidate",
            "pg_local_cache_truncate_invalidate",
        ):
            self.assertIn(trigger, register)
        self.assertGreaterEqual(
            register.count("DEPENDS ON EXTENSION pg_local_cache"), 3
        )
        self.assertIn("v_ready_trigger_count <> 3", register)

    def test_worker_role_is_least_privilege_and_exactly_granted(self) -> None:
        register = sql_function("_register_mapping")
        for guard in (
            "NOT r.rolsuper",
            "NOT r.rolinherit",
            "NOT r.rolcreatedb",
            "NOT r.rolcreaterole",
            "NOT r.rolreplication",
            "NOT r.rolbypassrls",
        ):
            self.assertIn(guard, register)
        self.assertIn("GRANT SELECT, INSERT, UPDATE, DELETE", register)
        self.assertIn("REVOKE INSERT, UPDATE, DELETE", register)

    def test_detach_removes_mapping_triggers_and_direct_acl(self) -> None:
        detach = sql_function("detach_table")
        self.assertIn("DELETE FROM local_cache.mapping", detach)
        self.assertIn("local_cache._forget", detach)
        self.assertIn("local_cache._drop_owned_triggers", detach)
        self.assertIn("REVOKE ALL PRIVILEGES ON TABLE", detach)

    def test_reconcile_reuses_the_guarded_registration_path(self) -> None:
        self.assertIn("local_cache._register_mapping(", sql_function("reconcile_table"))
        reconcile_all = sql_function("reconcile_all")
        self.assertIn("mappings changed concurrently", reconcile_all)
        self.assertIn("local_cache._register_mapping(", reconcile_all)


class ConsistencyTests(unittest.TestCase):
    def test_row_invalidation_is_published_by_the_c_trigger(self) -> None:
        register = sql_function("_register_mapping")
        self.assertIn("AFTER INSERT OR UPDATE OR DELETE", register)
        self.assertIn("FOR EACH ROW", register)
        self.assertIn("local_cache._row_invalidate", register)

    def test_ddl_and_drop_event_triggers_are_installed(self) -> None:
        self.assertIn("CREATE EVENT TRIGGER pg_local_cache_ddl_invalidate", SQL)
        self.assertIn("CREATE EVENT TRIGGER pg_local_cache_sql_drop_invalidate", SQL)
        self.assertIn("ON ddl_command_end", SQL)
        self.assertIn("ON sql_drop", SQL)

    def test_grant_and_revoke_force_transactional_mapping_reload(self) -> None:
        ddl = sql_function("_ddl_invalidate")
        self.assertIn("TG_TAG IN ('GRANT', 'REVOKE')", ddl)
        grant_guard = ddl.index("TG_TAG IN ('GRANT', 'REVOKE')")
        object_scan = ddl.index("pg_event_trigger_ddl_commands()")
        self.assertLess(grant_guard, object_scan)
        self.assertIn("PERFORM local_cache._reload()", ddl[grant_guard:object_scan])


class PrivilegeTests(unittest.TestCase):
    def test_sql_kv_reads_are_security_invoker_c_functions(self) -> None:
        for signature, symbol in (
            ("get(relation regclass, key_values text[])", "pg_local_cache_sql_get"),
            ("get(relation regclass, key_value anyelement)", "pg_local_cache_sql_get_scalar"),
            ("mget(relation regclass, key_values anyarray)", "pg_local_cache_sql_mget"),
        ):
            definition = SQL.partition(f"CREATE FUNCTION {signature}")[2].partition(";")[0]
            self.assertIn(symbol, definition)
            self.assertIn("LANGUAGE C STRICT VOLATILE PARALLEL UNSAFE", definition)
            self.assertIn("SECURITY INVOKER", definition)

    def test_sql_api_does_not_expose_a_polymorphic_row_witness(self) -> None:
        self.assertNotIn("row_type anyelement", SQL)
        self.assertNotIn("anycompatible", SQL)
        self.assertNotIn("pg_local_cache_sql_get_row", SQL)

    def test_admin_and_internal_functions_are_not_public(self) -> None:
        signatures = (
            "attach_table(regclass, boolean, text)",
            "detach_table(regclass)",
            "reconcile_table(regclass)",
            "reconcile_all()",
            "_register_mapping(text, regclass, name[], boolean)",
            "_row_invalidate()",
        )
        for signature in signatures:
            self.assertRegex(
                SQL,
                rf"REVOKE ALL ON FUNCTION {re.escape(signature)}\s+FROM PUBLIC",
            )

    def test_application_observability_functions_are_not_public_by_default(self) -> None:
        for signature in ("stats()", "metrics()", "health()", "invalidate(text)"):
            self.assertIn(
                f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC", SQL
            )


if __name__ == "__main__":
    unittest.main()
