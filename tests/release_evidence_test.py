#!/usr/bin/env python3
"""Tests for release benchmark evidence validation."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from tests import sql_only_benchmark_test as sql_only_contract


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = ROOT / "scripts" / "validate_benchmark_evidence.py"
SPEC = importlib.util.spec_from_file_location("benchmark_evidence", VALIDATOR_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import {VALIDATOR_PATH}")
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)

REVISION = "1" * 40
HARNESS = "a" * 64


def measured_result() -> dict[str, object]:
    return {
        "summary": {"median_operations_per_second": 20_000},
        "runs": [{"operations_per_second": 20_000, "errors": 0}],
    }


def whole_row_report() -> dict[str, object]:
    images = {
        name: {
            "reference": f"example/{name}:1",
            "identity": f"sha256:{str(index) * 64}",
        }
        for index, name in enumerate(
            (
                "postgres",
                "valkey",
                "redis",
                "pg_local_cache",
                "benchmark_client",
            ),
            start=1,
        )
    }
    return {
        "schema_version": 4,
        "environment": {
            "source_revision": REVISION,
            "harness_sha256": HARNESS,
            "container_runtime": {
                "docker_version": "28.0.0",
                "compose_version": "2.35.0",
            },
            "images": images,
            "benchmark_client": {
                "logical_cpu_count": 4,
                "cpu_model": "Test CPU",
                "cgroup_v2": {
                    "cpu.max": "400000 100000",
                    "cpu_quota_cores": 4,
                    "memory.max": "1073741824",
                    "memory_limit_bytes": 1_073_741_824,
                },
            },
        },
        "workload": {
            "repetitions": 1,
            "client_cpus": 4,
            "server_cpus_per_target": 4,
            "pg_local_cache_workers": 4,
            "pgbench_jobs": 2,
            "client_memory": "1g",
            "server_memory_per_target": "2g",
        },
        "resp_full_row": {
            "gate": {
                "status": "PASS",
                "minimum_pg_local_cache_ops_per_second": 10_000,
            },
            "targets": {
                name: measured_result()
                for name in ("pg_local_cache", "valkey", "redis")
            },
        },
        "width_gate": {"status": "PASS"},
        "resp_payload_width_sweep": {"64": measured_result()},
        "gate": {"status": "PASS"},
    }


def sql_only_report() -> dict[str, object]:
    report = copy.deepcopy(sql_only_contract.valid_report())
    sql_only_contract.enable_scaling_snapshot(report)
    report["environment"] = {
        "source_revision": REVISION,
        "harness_sha256": HARNESS,
        "benchmark_client": {
            "logical_cpu_count": 4,
            "cpu_model": "Test CPU",
            "cgroup_v2": {
                "cpu.max": "max 100000",
                "cpu_quota_cores": None,
                "memory.max": "1073741824",
                "memory_limit_bytes": 1_073_741_824,
            },
        },
    }
    report["workload"]["min_cached_to_direct_ratio"] = 1.5
    report["workload"]["min_cached_to_stock_ratio"] = 1.5
    for protocol in ("prepared", "extended"):
        lane = report["protocols"][protocol]
        lane["relative_throughput_gate"] = (
            sql_only_contract.sql_only.relative_throughput_gate(
                cached_to_direct=lane["cached_to_direct_throughput_ratio"],
                cached_to_stock=lane["cached_to_stock_throughput_ratio"],
                minimum_cached_to_direct=1.5,
                minimum_cached_to_stock=1.5,
            )
        )
    return report


class ReleaseEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.whole_path = self.directory / "whole-row.json"
        self.sql_path = self.directory / "sql-only.json"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def write_report(path: Path, report: dict[str, object]) -> None:
        path.write_text(json.dumps(report), encoding="utf-8")
        path.with_suffix(".md").write_text("# Benchmark\n", encoding="utf-8")

    def test_accepts_complete_exact_revision_evidence(self) -> None:
        self.write_report(self.whole_path, whole_row_report())
        self.write_report(self.sql_path, sql_only_report())

        VALIDATOR.validate_whole_row(self.whole_path, REVISION)
        VALIDATOR.validate_sql_only(self.sql_path, REVISION)

    def test_rejects_evidence_from_another_revision(self) -> None:
        report = whole_row_report()
        report["environment"]["source_revision"] = "2" * 40
        self.write_report(self.whole_path, report)

        with self.assertRaisesRegex(ValueError, "source revision"):
            VALIDATOR.validate_whole_row(self.whole_path, REVISION)

    def test_rejects_unknown_image_identity(self) -> None:
        report = whole_row_report()
        report["environment"]["images"]["postgres"]["identity"] = " unknown "
        self.write_report(self.whole_path, report)

        with self.assertRaisesRegex(ValueError, "image postgres identity"):
            VALIDATOR.validate_whole_row(self.whole_path, REVISION)

    def test_rejects_summary_that_does_not_match_runs(self) -> None:
        report = whole_row_report()
        target = report["resp_full_row"]["targets"]["pg_local_cache"]
        target["summary"]["median_operations_per_second"] = 99_999
        self.write_report(self.whole_path, report)

        with self.assertRaisesRegex(ValueError, "median does not match"):
            VALIDATOR.validate_whole_row(self.whole_path, REVISION)

    def test_rejects_ineffective_client_cgroup_limit(self) -> None:
        report = whole_row_report()
        cgroup = report["environment"]["benchmark_client"]["cgroup_v2"]
        cgroup["cpu.max"] = "max 100000"
        cgroup["cpu_quota_cores"] = None
        self.write_report(self.whole_path, report)

        with self.assertRaisesRegex(ValueError, "CPU quota"):
            VALIDATOR.validate_whole_row(self.whole_path, REVISION)

    def test_rejects_relative_gate_below_release_minimum(self) -> None:
        report = sql_only_report()
        gate = report["protocols"]["prepared"]["relative_throughput_gate"]
        gate["cached_to_stock"]["minimum_ratio"] = 0.98
        self.write_report(self.sql_path, report)

        with self.assertRaisesRegex(ValueError, "cached_to_stock"):
            VALIDATOR.validate_sql_only(self.sql_path, REVISION)

    def test_rejects_missing_raw_latency_samples(self) -> None:
        report = copy.deepcopy(sql_only_report())
        del report["protocols"]["extended"]["cached_mode"]["latency"][
            "raw_samples_ms"
        ]
        self.write_report(self.sql_path, report)

        with self.assertRaisesRegex(ValueError, "raw latency samples"):
            VALIDATOR.validate_sql_only(self.sql_path, REVISION)

    def test_accepts_architecture_when_runtime_hides_cpu_model(self) -> None:
        report = sql_only_report()
        report["environment"]["benchmark_client"]["cpu_model"] = "unknown"
        report["environment"]["machine"] = "aarch64"
        self.write_report(self.sql_path, report)

        VALIDATOR.validate_sql_only(self.sql_path, REVISION)

    def test_accepts_release_without_non_gating_scaling_snapshot(self) -> None:
        report = sql_only_report()
        report["workload"]["scaling_snapshot_enabled"] = False
        report["scaling_snapshot"] = {
            "status": "DISABLED",
            "performance_gating": False,
        }
        self.write_report(self.sql_path, report)

        VALIDATOR.validate_sql_only(self.sql_path, REVISION)

    def test_cli_accepts_sql_only_release_evidence(self) -> None:
        report = sql_only_report()
        report["workload"]["scaling_snapshot_enabled"] = False
        report["scaling_snapshot"] = {
            "status": "DISABLED",
            "performance_gating": False,
        }
        self.write_report(self.sql_path, report)

        with mock.patch.object(
            sys,
            "argv",
            [
                str(VALIDATOR_PATH),
                "--revision",
                REVISION,
                "--sql-only",
                str(self.sql_path),
            ],
        ):
            self.assertEqual(VALIDATOR.main(), 0)

    def test_rejects_forged_pass_with_failed_generator_proof(self) -> None:
        report = sql_only_report()
        report["cold_miss_fill_hit_proof"]["status"] = "FAIL"
        self.write_report(self.sql_path, report)

        with self.assertRaisesRegex(ValueError, "generator contract failed"):
            VALIDATOR.validate_sql_only(self.sql_path, REVISION)


if __name__ == "__main__":
    unittest.main()
