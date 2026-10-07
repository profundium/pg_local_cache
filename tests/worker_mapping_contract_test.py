from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WorkerMappingContractTest(unittest.TestCase):
    def test_fresh_install_trigger_arguments_match_worker_filter(self) -> None:
        worker = (ROOT / "src/pg_local_cache_worker.c").read_text()
        install = (ROOT / "sql/pg_local_cache--3.2.0.sql").read_text()

        self.assertIn("2 + pg_catalog.cardinality(m.key_columns)", worker)
        self.assertIn("m.effective_write_mode", worker)
        self.assertIn("local_cache._effective_write_mode(", worker)
        self.assertIn("quote_literal(v_effective_write_mode)", install)
        self.assertIn("2 + pg_catalog.cardinality(p_key_columns)", install)
        self.assertIn(
            "GRANT EXECUTE ON FUNCTION local_cache._effective_write_mode(regclass, text) TO %I",
            install,
        )
        self.assertIn("FROM pg_catalog.pg_roles AS r", install)
        self.assertIn("WHERE r.rolname = v_worker_role", install)

    def test_upgrade_reconciles_legacy_triggers_to_current_arguments(self) -> None:
        upgrade = (
            ROOT / "sql/pg_local_cache--3.1.0--3.2.0.sql"
        ).read_text()

        self.assertIn("ADD COLUMN write_mode", upgrade)
        self.assertIn("local_cache._effective_write_mode(", upgrade)
        self.assertIn("convert_to(v_effective_write_mode, v_encoding)", upgrade)
        self.assertIn("2 + pg_catalog.cardinality(p_key_columns)", upgrade)
        self.assertIn("quote_literal(v_effective_write_mode)", upgrade)
        self.assertIn("DEFAULT 'invalidate'", upgrade)
        self.assertIn(
            "GRANT EXECUTE ON FUNCTION local_cache._effective_write_mode(regclass, text) TO %I",
            upgrade,
        )
        self.assertIn("FROM pg_catalog.pg_roles AS r", upgrade)
        self.assertIn("WHERE r.rolname = v_worker_role", upgrade)
        self.assertIn("SELECT local_cache.reconcile_all()", upgrade)


if __name__ == "__main__":
    unittest.main()
