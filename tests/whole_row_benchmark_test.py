#!/usr/bin/env python3
"""Small contracts for the RESP comparison benchmark."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"
sys.path.insert(0, str(BENCHMARKS))
SPEC = importlib.util.spec_from_file_location(
    "pglc_whole_row", BENCHMARKS / "whole_row.py"
)
assert SPEC is not None and SPEC.loader is not None
WHOLE_ROW = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = WHOLE_ROW
SPEC.loader.exec_module(WHOLE_ROW)


class WholeRowBenchmarkTests(unittest.TestCase):
    def test_payload_sizes_are_unique_and_bounded(self) -> None:
        self.assertEqual(WHOLE_ROW.parse_payload_sizes("64, 512,64"), (64, 512))
        for invalid in ("", "0", "3001", "64,,512", "bad"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                WHOLE_ROW.parse_payload_sizes(invalid)

    def test_harness_has_no_transparent_sql_lane(self) -> None:
        source = (BENCHMARKS / "whole_row.py").read_text(encoding="utf-8")
        for retired in (
            "ordinary_sql",
            "pg_local_cache.sql_cache",
            "Custom Scan",
            "SQL_IN_TABLE",
            "PGLC_BENCH_ROW_SQL_",
        ):
            self.assertNotIn(retired, source)
        self.assertIn('"resp_full_row": resp', source)
        self.assertIn('"resp_payload_width_sweep": widths', source)

    def test_markdown_reports_resp_and_width_only(self) -> None:
        summary = {
            "median_operations_per_second": 1000,
            "median_p99_ms": 1.25,
        }
        run = {"errors": 0}
        report = {
            "generated_at_utc": "2026-01-01T00:00:00+00:00",
            "environment": {"source_revision": "abc"},
            "workload": {
                "duration_seconds": 5,
                "repetitions": 3,
                "concurrency": 4,
                "keys": 100,
            },
            "resp_full_row": {
                "targets": {
                    name: {"summary": summary, "runs": [run]}
                    for name in ("pg_local_cache", "valkey", "redis")
                }
            },
            "resp_payload_width_sweep": {
                "64": {
                    "payload_text_bytes": 64,
                    "response_bytes_min": 80,
                    "response_bytes_max": 90,
                    "summary": summary,
                }
            },
            "gate": {"status": "PASS", "message": "ok"},
        }
        markdown = WHOLE_ROW.render_markdown(report)
        self.assertIn("pg_local_cache RESP benchmark", markdown)
        self.assertIn("Response-width sweep", markdown)
        self.assertNotIn("Ordinary SQL", markdown)


if __name__ == "__main__":
    unittest.main()
