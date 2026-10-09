from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WorkerMappingContractTest(unittest.TestCase):
    def test_fresh_install_trigger_arguments_match_worker_filter(self) -> None:
        worker = (ROOT / "src/pg_local_cache_worker.c").read_text()
        install = (ROOT / "sql/pg_local_cache--3.2.0.sql").read_text()

        self.assertIn("2 + pg_catalog.cardinality(m.key_columns)", worker)
        self.assertIn("m.effective_write_mode", worker)
        self.assertNotIn("local_cache._effective_write_mode(", worker)
        self.assertIn("source_mapping.write_mode = 'refresh'", worker)
        self.assertIn("mwa.attgenerated = 'v'", worker)
        for pg_type in ("bool", "int2", "int4", "int8", "text", "varchar"):
            self.assertIn(f"'pg_catalog.{pg_type}'::pg_catalog.regtype", worker)
        self.assertIn("quote_literal(v_effective_write_mode)", install)
        self.assertIn("2 + pg_catalog.cardinality(p_key_columns)", install)
        self.assertNotIn("grant_worker_write_mode", install)
        self.assertIn(
            "REVOKE ALL ON FUNCTION _effective_write_mode(regclass, text) FROM PUBLIC",
            install,
        )

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
        self.assertNotIn("grant_worker_write_mode", upgrade)
        self.assertIn(
            "REVOKE ALL ON FUNCTION local_cache._effective_write_mode(regclass, text)",
            upgrade,
        )
        self.assertIn("SELECT local_cache.reconcile_all()", upgrade)


if __name__ == "__main__":
    unittest.main()
