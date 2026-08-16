#!/usr/bin/env python3
"""Unit contracts for the standalone SQL-only benchmark harness."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CONTROL = (ROOT / "pg_local_cache.control").read_text(encoding="utf-8")
CURRENT_VERSION_MATCH = re.search(
    r"^default_version = '([^']+)'$", CONTROL, flags=re.MULTILINE
)
if CURRENT_VERSION_MATCH is None:
    raise RuntimeError("could not determine pg_local_cache version")
CURRENT_VERSION = CURRENT_VERSION_MATCH.group(1)

SPEC = importlib.util.spec_from_file_location(
    "pg_local_cache_sql_only_benchmark",
    ROOT / "benchmarks" / "sql_only.py",
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load benchmarks/sql_only.py")
sql_only = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sql_only
SPEC.loader.exec_module(sql_only)


def config(**overrides: object) -> object:
    values: dict[str, object] = {
        "host": "postgres.example",
        "port": 5432,
        "database": "app",
        "admin_user": "postgres",
        "admin_password": "admin-password",
        "stock_host": "stock-postgres.example",
        "stock_port": 5432,
        "stock_database": "app",
        "stock_admin_user": "postgres",
        "stock_admin_password": "admin-password",
        "duration": 1.0,
        "warmup_seconds": 0.0,
        "latency_duration": 1.0,
        "latency_sample_rate": 0.1,
        "latency_min_samples": 100,
        "latency_max_p99_ms": None,
        "repetitions": 1,
        "concurrency": 2,
        "jobs": 2,
        "pipeline": 2,
        "keys": 8,
        "payload_bytes": 64,
        "prepared_min_ops": 10_000.0,
        "extended_min_ops": 10_000.0,
        "min_cached_to_direct_ratio": None,
        "min_cached_to_stock_ratio": None,
        "scaling_snapshot_enabled": False,
        "scaling_duration": 3.0,
        "scaling_warmup_seconds": 1.0,
        "scaling_latency_duration": 3.0,
        "scaling_latency_sample_rate": 0.1,
        "scaling_latency_min_samples": 500,
        "scaling_repetitions": 1,
        "output_directory": Path("/tmp/sql-only-results"),
        "run_id": "abc12345",
        "app_password": "ordinary-role-password",
        "keep_objects": False,
    }
    values.update(overrides)
    return sql_only.Config(**values)


PGBENCH_OUTPUT = """\
transaction type: sql-only.sql
number of transactions actually processed: 125
number of failed transactions: 0 (0.000%)
latency average = 1.250 ms
tps = 80.000000 (without initial connection time)
"""


def counters(
    *, hits: int = 0, misses: int = 0, fills: int = 0, bypasses: int = 0
) -> dict[str, int]:
    return {
        "sql_cache_hits": hits,
        "sql_cache_misses": misses,
        "sql_cache_fills": fills,
        "sql_cache_bypasses": bypasses,
    }


def timed_run(
    protocol: str,
    *,
    benchmark_mode: str,
    operations: int = 20_000,
    rate: float = 20_000.0,
    operations_per_batch: int = 2,
) -> dict[str, object]:
    cache_enabled = {
        "stock": None,
        "direct": False,
        "cached": True,
    }[benchmark_mode]
    result: dict[str, object] = {
        "successful_batches": operations // operations_per_batch,
        "successful_operations": operations,
        "failed_batches": 0,
        "batch_transactions_per_second": rate / 2,
        "operations_per_second": rate,
        "batch_latency_average_ms": 1.0,
        "operations_per_batch": operations_per_batch,
        "query_protocol": protocol,
        "benchmark_mode": benchmark_mode,
        "server_target": "stock" if benchmark_mode == "stock" else "mapped",
        "cache_enabled": cache_enabled,
        "random_seed": 1,
        "repetition": 1,
    }
    if benchmark_mode == "stock":
        result["cache_counter_accounting"] = "not applicable: stock server"
    else:
        result.update(
            {
                "sql_cache_hits_during_measurement": (
                    operations if benchmark_mode == "cached" else 0
                ),
                "sql_cache_misses_during_measurement": 0,
                "sql_cache_fills_during_measurement": 0,
                "sql_cache_bypasses_during_measurement": 0,
            }
        )
    return result


def latency_result(protocol: str, benchmark_mode: str) -> dict[str, object]:
    run = timed_run(
        protocol,
        benchmark_mode=benchmark_mode,
        operations=10_000,
        rate=10_000.0,
        operations_per_batch=1,
    )
    distribution = {
        "unit": "ms",
        "sample_count": 1_000,
        "mean_ms": 1.0,
        "minimum_ms": 1.0,
        "p50_ms": 1.0,
        "p95_ms": 1.0,
        "p99_ms": 1.0,
        "maximum_ms": 1.0,
        "measurement": "end-to-end single-SELECT transaction",
        "percentile_method": "linear interpolation at (n-1)*q",
        "duration_seconds": 1.0,
        "sampling_rate": 0.1,
        "protocol": protocol,
        "benchmark_mode": benchmark_mode,
        "pipeline_depth": 1,
    }
    return {
        "runs": [
            {
                "run": run,
                "distribution": dict(distribution),
                "repetition": 1,
                "measurement_order": 1,
                "order_within_repetition": 1,
                "raw_sample_offset": 0,
                "raw_sample_count": 1_000,
            }
        ],
        "raw_samples_ms": [1.0] * 1_000,
        "distribution": distribution,
        "gate": {
            "minimum_samples_per_run": 100,
            "minimum_aggregate_samples": 100,
            "maximum_p99_ms": None,
            "status": "MEASURED",
        },
    }


def lane(protocol: str, *, rate: float = 20_000.0) -> dict[str, object]:
    stock_run = timed_run(protocol, benchmark_mode="stock", rate=rate / 2)
    direct_run = timed_run(protocol, benchmark_mode="direct", rate=rate / 2)
    cached_run = timed_run(protocol, benchmark_mode="cached", rate=rate)
    stock = sql_only.aggregate_mode([stock_run])
    direct = sql_only.aggregate_mode([direct_run])
    cached = sql_only.aggregate_mode([cached_run])
    stock["latency"] = latency_result(protocol, "stock")
    direct["latency"] = latency_result(protocol, "direct")
    cached["latency"] = latency_result(protocol, "cached")
    return {
        "status": "MEASURED",
        "query_protocol": protocol,
        "protocol_semantics": "test",
        "stock_mode": stock,
        "direct_mode": direct,
        "cached_mode": cached,
        "cached_to_stock_throughput_ratio": 2.0,
        "direct_to_stock_throughput_ratio": 1.0,
        "cached_to_direct_throughput_ratio": 2.0,
        "relative_throughput_gate": sql_only.relative_throughput_gate(
            cached_to_direct=2.0,
            cached_to_stock=2.0,
            minimum_cached_to_direct=None,
            minimum_cached_to_stock=None,
        ),
        "throughput_gate": {
            "scope": f"{protocol} cached-mode median only",
            "minimum_cached_operations_per_second": 10_000.0,
            "measured_cached_operations_per_second": rate,
            "status": "PASS",
        },
        "latency_gate": {
            "scope": f"{protocol}, all three modes, single SELECT per transaction",
            "repetitions_per_mode": 1,
            "minimum_samples_per_run": 100,
            "minimum_aggregate_samples_per_mode": 100,
            "maximum_p99_ms": None,
            "status": "MEASURED",
        },
    }


def valid_report() -> dict[str, object]:
    cfg = config()
    return {
        "schema_version": 3,
        "generated_at_utc": "2026-08-02T00:00:00+00:00",
        "server": {
            "pg_local_cache_port": 0,
            "extension_version": CURRENT_VERSION,
            "server_version_num": 160014,
        },
        "stock_server": {
            "server_version_num": 160014,
            "extension_installed": False,
            "pg_local_cache_preloaded": False,
            "settings_match_mapped_server": True,
        },
        "ordinary_application_role": {
            "name": cfg.app_user,
            "superuser": False,
            "local_cache_schema_usage": True,
        },
        "stock_application_role": {
            "name": cfg.app_user,
            "superuser": False,
            "local_cache_schema_usage": False,
        },
        "read_equivalence_proof": {
            "query": cfg.lookup_query,
            "cached_plan": "Result",
            "direct_plan": "Index Scan using rows_pkey on rows",
            "stock_plan": "Index Scan using rows_pkey on rows",
            "stock_mapped_and_cached_rows_equal": True,
            "direct_and_cached_rows_equal": True,
        },
        "cold_miss_fill_hit_proof": {
            "status": "PASS",
            "sql_cache_hits_during_measurement": 1,
            "sql_cache_misses_during_measurement": 1,
            "sql_cache_fills_during_measurement": 1,
            "sql_cache_bypasses_during_measurement": 0,
        },
        "complete_keyspace_warm": {
            "status": "PASS",
            "keys_filled": cfg.keys,
            "sql_cache_hits_during_measurement": 0,
            "sql_cache_misses_during_measurement": cfg.keys,
            "sql_cache_fills_during_measurement": cfg.keys,
            "sql_cache_bypasses_during_measurement": 0,
        },
        "sentinel_row_integrity_check": {
            "status": "PASS",
            "source_row_count": cfg.keys,
            "source_min_id": 1,
            "source_max_id": cfg.keys,
            "source_distinct_ids": cfg.keys,
            "sentinel_keys": [1, 4, 8],
            "sentinel_rows": 3,
            "stock_mapped_and_cached_rows_equal": True,
            "direct_and_cached_rows_equal": True,
            "sentinel_rows_sha256": "a" * 64,
            "direct_counter_deltas": {
                "sql_cache_hits_during_measurement": 0,
                "sql_cache_misses_during_measurement": 0,
                "sql_cache_fills_during_measurement": 0,
                "sql_cache_bypasses_during_measurement": 0,
            },
            "cached_counter_deltas": {
                "sql_cache_hits_during_measurement": 3,
                "sql_cache_misses_during_measurement": 0,
                "sql_cache_fills_during_measurement": 0,
                "sql_cache_bypasses_during_measurement": 0,
            },
        },
        "workload": {
            "duration_seconds": cfg.duration,
            "effective_duration_seconds": 1,
            "warmup_seconds": cfg.warmup_seconds,
            "effective_warmup_seconds": 0,
            "latency_duration_seconds": cfg.latency_duration,
            "effective_latency_duration_seconds": 1,
            "latency_sample_rate": cfg.latency_sample_rate,
            "latency_min_samples": cfg.latency_min_samples,
            "latency_max_p99_ms": cfg.latency_max_p99_ms,
            "repetitions": cfg.repetitions,
            "concurrency": cfg.concurrency,
            "jobs": cfg.jobs,
            "pipeline": cfg.pipeline,
            "keys": cfg.keys,
            "payload_bytes": cfg.payload_bytes,
            "prepared_min_ops": cfg.prepared_min_ops,
            "extended_min_ops": cfg.extended_min_ops,
            "min_cached_to_direct_ratio": cfg.min_cached_to_direct_ratio,
            "min_cached_to_stock_ratio": cfg.min_cached_to_stock_ratio,
            "scaling_snapshot_enabled": False,
        },
        "protocols": {
            "prepared": lane("prepared"),
            "extended": lane("extended"),
        },
        "scaling_snapshot": {
            "status": "DISABLED",
            "performance_gating": False,
        },
        "gate": {
            "status": "PASS",
            "message": "all checks passed",
            "failures": [],
        },
    }


def enable_scaling_snapshot(report: dict[str, object]) -> None:
    workload = report["workload"]
    workload.update(
        {
            "concurrency": 16,
            "jobs": 2,
            "pipeline": 32,
            "scaling_snapshot_enabled": True,
        }
    )
    for protocol in sql_only.PROTOCOLS:
        lane_result = report["protocols"][protocol]
        for benchmark_mode in sql_only.BENCHMARK_MODES:
            for run in lane_result[f"{benchmark_mode}_mode"]["runs"]:
                run["operations_per_batch"] = 32
                run["successful_batches"] = (
                    run["successful_operations"] // 32
                )
                run["batch_transactions_per_second"] = (
                    run["operations_per_second"] / 32
                )
    secondary_protocols = copy.deepcopy(report["protocols"])
    for protocol in sql_only.PROTOCOLS:
        lane_result = secondary_protocols[protocol]
        lane_result["throughput_gate"].update(
            {
                "minimum_cached_operations_per_second": None,
                "status": "MEASURED",
            }
        )
        for benchmark_mode in sql_only.BENCHMARK_MODES:
            for run in lane_result[f"{benchmark_mode}_mode"]["runs"]:
                run["operations_per_batch"] = 8
                run["successful_batches"] = (
                    run["successful_operations"] // 8
                )
                run["batch_transactions_per_second"] = (
                    run["operations_per_second"] / 8
                )
    primary_workload = {
        "concurrency": 16,
        "jobs": 2,
        "throughput_pipeline": 32,
        "latency_pipeline": 1,
        "duration_seconds": workload["duration_seconds"],
        "warmup_seconds": workload["warmup_seconds"],
        "latency_duration_seconds": workload["latency_duration_seconds"],
        "latency_sample_rate": workload["latency_sample_rate"],
        "latency_min_samples": workload["latency_min_samples"],
        "repetitions": workload["repetitions"],
        "keys": workload["keys"],
        "payload_bytes": workload["payload_bytes"],
    }
    secondary_workload = {
        "concurrency": 4,
        "jobs": 2,
        "throughput_pipeline": 8,
        "latency_pipeline": 1,
        "duration_seconds": 3.0,
        "warmup_seconds": 1.0,
        "latency_duration_seconds": 3.0,
        "latency_sample_rate": 0.1,
        "latency_min_samples": 100,
        "repetitions": 1,
        "keys": workload["keys"],
        "payload_bytes": workload["payload_bytes"],
    }
    report["scaling_snapshot"] = {
        "status": "MEASURED",
        "performance_gating": False,
        "measurement_order": ["c16_p32", "c4_p8"],
        "profiles": {
            "c4_p8": {
                "source": "embedded",
                "workload": secondary_workload,
                "protocols": secondary_protocols,
            },
            "c16_p32": {
                "source": "primary_strict_profile",
                "source_path": "$.protocols",
                "workload": primary_workload,
            },
        },
    }


class ConfigurationTests(unittest.TestCase):
    def test_defaults_need_no_resp_token_and_have_independent_10k_gates(self) -> None:
        environment = {
            "PGHOST": "db.internal",
            "PGPORT": "6432",
            "PGDATABASE": "app",
            "PGUSER": "owner",
            "PGPASSWORD": "secret",
            "PGLC_SQL_ONLY_BENCH_RUN_ID": "run12345",
        }
        with mock.patch.dict(sql_only.os.environ, environment, clear=True):
            parsed = sql_only.Config.from_environment()
        self.assertEqual(parsed.host, "db.internal")
        self.assertEqual(parsed.port, 6432)
        self.assertEqual(parsed.prepared_min_ops, 10_000.0)
        self.assertEqual(parsed.extended_min_ops, 10_000.0)
        self.assertIsNone(parsed.latency_max_p99_ms)
        self.assertEqual(parsed.min_cached_to_direct_ratio, 1.5)
        self.assertEqual(parsed.min_cached_to_stock_ratio, 1.5)
        self.assertEqual((parsed.keys, parsed.payload_bytes), (4096, 3000))
        self.assertFalse(hasattr(parsed, "auth_token"))

    def test_protocol_gates_are_configured_independently(self) -> None:
        environment = {
            "PGLC_SQL_ONLY_BENCH_RUN_ID": "run12345",
            "PGLC_SQL_ONLY_BENCH_PREPARED_MIN_OPS": "25000",
            "PGLC_SQL_ONLY_BENCH_EXTENDED_MIN_OPS": "12000",
            "PGLC_SQL_ONLY_BENCH_LATENCY_MAX_P99_MS": "5.5",
            "PGLC_SQL_ONLY_BENCH_MIN_CACHED_TO_DIRECT_RATIO": "0.8",
            "PGLC_SQL_ONLY_BENCH_MIN_CACHED_TO_STOCK_RATIO": "0.75",
        }
        with mock.patch.dict(sql_only.os.environ, environment, clear=True):
            parsed = sql_only.Config.from_environment()
        self.assertEqual(parsed.prepared_min_ops, 25_000.0)
        self.assertEqual(parsed.extended_min_ops, 12_000.0)
        self.assertEqual(parsed.latency_max_p99_ms, 5.5)
        self.assertEqual(parsed.min_cached_to_direct_ratio, 0.8)
        self.assertEqual(parsed.min_cached_to_stock_ratio, 0.75)

    def test_scaling_snapshot_has_fixed_non_gating_profile(self) -> None:
        environment = {
            "PGLC_SQL_ONLY_BENCH_RUN_ID": "run12345",
            "PGLC_SQL_ONLY_BENCH_CONCURRENCY": "16",
            "PGLC_SQL_ONLY_BENCH_PIPELINE": "32",
            "PGLC_SQL_ONLY_BENCH_SCALING_SNAPSHOT": "true",
            "PGLC_SQL_ONLY_BENCH_SCALING_DURATION": "3",
            "PGLC_SQL_ONLY_BENCH_SCALING_LATENCY_DURATION": "3",
            "PGLC_SQL_ONLY_BENCH_SCALING_LATENCY_MIN_SAMPLES": "500",
        }
        with mock.patch.dict(sql_only.os.environ, environment, clear=True):
            parsed = sql_only.Config.from_environment()
        secondary = sql_only.scaling_profile_config(parsed)
        self.assertEqual((secondary.concurrency, secondary.pipeline), (4, 8))
        self.assertEqual(secondary.jobs, min(parsed.jobs, 4))
        self.assertEqual(secondary.repetitions, 1)
        self.assertIsNone(secondary.latency_max_p99_ms)
        self.assertIsNone(secondary.min_cached_to_direct_ratio)
        self.assertIsNone(secondary.min_cached_to_stock_ratio)
        self.assertEqual(secondary.database, parsed.database)
        self.assertEqual(secondary.run_id, parsed.run_id)

    def test_scaling_snapshot_does_not_replace_another_primary_profile(self) -> None:
        with self.assertRaisesRegex(ValueError, "strict primary profile c16/k32"):
            config(
                scaling_snapshot_enabled=True,
                concurrency=4,
                pipeline=8,
            ).validate()

    def test_pgbench_jobs_use_available_client_cpus_by_default(self) -> None:
        environment = {
            "PGLC_SQL_ONLY_BENCH_RUN_ID": "run12345",
            "PGLC_SQL_ONLY_BENCH_CONCURRENCY": "16",
            "PGLC_SQL_ONLY_BENCH_JOBS": "",
        }
        with mock.patch.dict(
            sql_only.os.environ, environment, clear=True
        ), mock.patch.object(sql_only.os, "cpu_count", return_value=12):
            parsed = sql_only.Config.from_environment()
        self.assertEqual(parsed.jobs, 12)

        environment["PGLC_SQL_ONLY_BENCH_JOBS"] = "3"
        with mock.patch.dict(
            sql_only.os.environ, environment, clear=True
        ), mock.patch.object(sql_only.os, "cpu_count", return_value=12):
            parsed = sql_only.Config.from_environment()
        self.assertEqual(parsed.jobs, 3)

    def test_run_id_and_bounds_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "RUN_ID"):
            config(run_id="unsafe-id").validate()
        with self.assertRaisesRegex(ValueError, "key count"):
            config(keys=1, concurrency=2).validate()
        with self.assertRaisesRegex(ValueError, "separate"):
            config(stock_host="postgres.example").validate()
        with mock.patch.dict(
            sql_only.os.environ,
            {"PGLC_SQL_ONLY_BENCH_KEEP_OBJECTS": "sometimes"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "boolean"):
                sql_only.Config.from_environment()

    def test_generated_names_and_query_use_sql_mget(self) -> None:
        cfg = config()
        self.assertEqual(cfg.schema, "pglc_sql_bench_abc12345")
        self.assertEqual(cfg.namespace, "sqlbench_abc12345")
        self.assertIn("local_cache.mget", cfg.lookup_query)
        self.assertIn("(:key)::bigint", cfg.lookup_query)
        self.assertIn("row_to_json", cfg.direct_lookup_query)

    def test_connection_arguments_use_hostname_env_and_real_app_role(self) -> None:
        cfg = config()
        arguments = sql_only.psql_arguments(cfg, application=True)
        self.assertEqual(arguments[arguments.index("-h") + 1], cfg.host)
        self.assertEqual(arguments[arguments.index("-p") + 1], "5432")
        self.assertEqual(arguments[arguments.index("-U") + 1], cfg.app_user)
        environment = sql_only.connection_environment(cfg, application=True)
        self.assertEqual(environment["PGPASSWORD"], cfg.app_password)
        stock_arguments = sql_only.psql_arguments(
            cfg, application=False, target="stock"
        )
        self.assertEqual(
            stock_arguments[stock_arguments.index("-h") + 1], cfg.stock_host
        )


class ParserAndWorkloadTests(unittest.TestCase):
    def test_pgbench_parser_converts_batches_to_select_operations(self) -> None:
        parsed = sql_only.parse_pgbench_output(PGBENCH_OUTPUT, 4)
        self.assertEqual(parsed["successful_batches"], 125)
        self.assertEqual(parsed["successful_operations"], 500)
        self.assertEqual(parsed["operations_per_second"], 320.0)
        self.assertEqual(parsed["failed_batches"], 0)

    def test_pgbench_parser_tracks_failures_and_rejects_partial_output(self) -> None:
        output = PGBENCH_OUTPUT.replace(
            "number of transactions actually processed: 125\n"
            "number of failed transactions: 0 (0.000%)",
            "number of transactions actually processed: 123\n"
            "number of failed transactions: 2 (1.600%)",
        )
        parsed = sql_only.parse_pgbench_output(output, 2)
        self.assertEqual(parsed["successful_operations"], 246)
        self.assertEqual(parsed["failed_batches"], 2)
        with self.assertRaisesRegex(ValueError, "could not parse"):
            sql_only.parse_pgbench_output("tps = 1", 1)

    def test_lookup_script_batches_keys_through_sql_mget(self) -> None:
        cfg = config(pipeline=3, keys=100)
        script = sql_only.lookup_script(cfg)
        self.assertEqual(script.count("local_cache.mget"), 1)
        self.assertIn(":key_0", script)
        self.assertIn(":key_2", script)
        self.assertNotIn("\\startpipeline", script)
        self.assertNotIn("\\endpipeline", script)

        latency_script = sql_only.latency_lookup_script(cfg)
        self.assertEqual(latency_script.count("local_cache.mget"), 1)
        self.assertNotIn("\\startpipeline", latency_script)

    def test_scaling_snapshot_reuses_primary_evidence_and_measures_only_c4(self) -> None:
        cfg = config(
            concurrency=16,
            pipeline=32,
            keys=32,
            scaling_snapshot_enabled=True,
        )

        def measured(
            profile: object,
            throughput_paths: dict[str, Path],
            latency_paths: dict[str, Path],
            protocol: str,
            minimum_ops: object,
        ) -> dict[str, object]:
            self.assertEqual((profile.concurrency, profile.pipeline), (4, 8))
            self.assertIn("local_cache.mget", throughput_paths["cached"].read_text())
            self.assertIn("array_agg", throughput_paths["stock"].read_text())
            self.assertIn("local_cache.mget", latency_paths["cached"].read_text())
            self.assertIsNone(minimum_ops)
            return {"query_protocol": protocol}

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            sql_only, "measure_protocol", side_effect=measured
        ) as measure:
            snapshot = sql_only.measure_scaling_snapshot(cfg, Path(directory))

        self.assertEqual(measure.call_count, 2)
        self.assertFalse(snapshot["performance_gating"])
        self.assertEqual(
            set(snapshot["profiles"]["c4_p8"]["protocols"]),
            {"prepared", "extended"},
        )
        primary = snapshot["profiles"]["c16_p32"]
        self.assertEqual(primary["source_path"], "$.protocols")
        self.assertNotIn("protocols", primary)

    def test_latency_parser_reports_interpolated_operation_percentiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "latency.1"
            second = Path(directory) / "latency.1.1"
            first.write_text(
                "0 1 1000 0 1 1\n0 2 2000 0 1 2\n",
                encoding="utf-8",
            )
            second.write_text(
                "1 1 3000 0 1 3\n1 2 4000 0 1 4\n",
                encoding="utf-8",
            )
            result = sql_only.parse_latency_logs([second, first])
        self.assertEqual(result["sample_count"], 4)
        self.assertEqual(result["mean_ms"], 2.5)
        self.assertEqual(result["p50_ms"], 2.5)
        self.assertAlmostEqual(result["p95_ms"], 3.85)
        self.assertAlmostEqual(result["p99_ms"], 3.97)

    def test_latency_repeats_rotates_and_aggregates_raw_samples(self) -> None:
        cfg = config(repetitions=3, latency_min_samples=2)

        def run_once(*args: object, **kwargs: object) -> dict[str, object]:
            return timed_run(
                str(kwargs["protocol"]),
                benchmark_mode=str(kwargs["benchmark_mode"]),
                operations=1_000,
                rate=1_000.0,
                operations_per_batch=1,
            )

        with tempfile.NamedTemporaryFile() as script, mock.patch.object(
            sql_only, "run_with_counter_accounting", side_effect=run_once
        ), mock.patch.object(
            sql_only,
            "read_latency_samples",
            side_effect=[[float(index), float(index) + 0.5] for index in range(1, 10)],
        ):
            result = sql_only.measure_latency_protocol(
                cfg,
                {mode: Path(script.name) for mode in sql_only.BENCHMARK_MODES},
                "prepared",
            )

        self.assertEqual(
            [item["measurement_order"] for item in result["stock"]["runs"]],
            [1, 5, 9],
        )
        self.assertEqual(
            [item["measurement_order"] for item in result["direct"]["runs"]],
            [2, 6, 7],
        )
        self.assertEqual(
            [item["measurement_order"] for item in result["cached"]["runs"]],
            [3, 4, 8],
        )
        for mode in sql_only.BENCHMARK_MODES:
            self.assertEqual(len(result[mode]["runs"]), 3)
            self.assertEqual(len(result[mode]["raw_samples_ms"]), 6)
            self.assertEqual(result[mode]["distribution"]["sample_count"], 6)
            self.assertEqual(result[mode]["gate"]["status"], "MEASURED")

    def test_client_resources_record_cgroup_limits_and_cpu_model(self) -> None:
        values = {
            "/proc/cpuinfo": "processor: 0\nmodel name: Test CPU 9000\n",
            "/sys/fs/cgroup/cpu.max": "200000 100000",
            "/sys/fs/cgroup/memory.max": "1073741824",
        }
        with mock.patch.object(
            sql_only,
            "read_text_if_present",
            side_effect=lambda path: values.get(str(path)),
        ), mock.patch.object(sql_only.os, "cpu_count", return_value=8):
            resources = sql_only.discover_client_resources()
        self.assertEqual(resources["logical_cpu_count"], 8)
        self.assertEqual(resources["cpu_model"], "Test CPU 9000")
        self.assertEqual(resources["cgroup_v2"]["cpu_quota_cores"], 2.0)
        self.assertEqual(
            resources["cgroup_v2"]["memory_limit_bytes"], 1_073_741_824
        )

    def test_counter_delta_is_exact_and_monotonic(self) -> None:
        delta = sql_only.counter_delta(
            counters(hits=10, misses=20, fills=5, bypasses=2),
            counters(hits=14, misses=21, fills=6, bypasses=2),
        )
        self.assertEqual(
            delta,
            {
                "sql_cache_hits_during_measurement": 4,
                "sql_cache_misses_during_measurement": 1,
                "sql_cache_fills_during_measurement": 1,
                "sql_cache_bypasses_during_measurement": 0,
            },
        )
        with self.assertRaisesRegex(ValueError, "moved backwards"):
            sql_only.counter_delta(counters(hits=2), counters(hits=1))

    def test_summary_uses_median_and_keeps_variance_visible(self) -> None:
        runs = [
            {
                "operations_per_second": rate,
                "batch_latency_average_ms": latency,
            }
            for rate, latency in ((10.0, 3.0), (30.0, 1.0), (20.0, 2.0))
        ]
        summary = sql_only.summarize_runs(runs)
        self.assertEqual(summary["median_operations_per_second"], 20.0)
        self.assertEqual(summary["minimum_operations_per_second"], 10.0)
        self.assertEqual(summary["maximum_operations_per_second"], 30.0)
        self.assertGreater(summary["coefficient_of_variation_percent"], 0)

    def test_runner_selects_prepared_or_unnamed_extended_protocol(self) -> None:
        cfg = config()
        with tempfile.NamedTemporaryFile() as stream, mock.patch.object(
            sql_only, "run_checked", return_value=PGBENCH_OUTPUT
        ) as run:
            prepared = sql_only.run_pgbench_once(
                cfg,
                Path(stream.name),
                protocol="prepared",
                benchmark_mode="cached",
                duration=1.25,
                seed=42,
            )
        arguments = run.call_args.args[0]
        environment = run.call_args.kwargs["environment"]
        self.assertEqual(arguments[arguments.index("-M") + 1], "prepared")
        self.assertEqual(arguments[arguments.index("-T") + 1], "2")
        self.assertEqual(arguments[arguments.index("-h") + 1], cfg.host)
        self.assertNotIn("pg_local_cache.sql_cache", environment["PGOPTIONS"])
        self.assertEqual(prepared["query_protocol"], "prepared")
        self.assertEqual(prepared["requested_duration_seconds"], 1.25)
        self.assertEqual(prepared["effective_duration_seconds"], 2)

        with tempfile.NamedTemporaryFile() as stream, mock.patch.object(
            sql_only, "run_checked", return_value=PGBENCH_OUTPUT
        ) as run:
            sql_only.run_pgbench_once(
                cfg,
                Path(stream.name),
                protocol="extended",
                benchmark_mode="direct",
                duration=1,
                seed=43,
            )
        arguments = run.call_args.args[0]
        environment = run.call_args.kwargs["environment"]
        self.assertEqual(arguments[arguments.index("-M") + 1], "extended")
        self.assertNotIn("pg_local_cache.sql_cache", environment["PGOPTIONS"])

        with tempfile.NamedTemporaryFile() as stream, mock.patch.object(
            sql_only, "run_checked", return_value=PGBENCH_OUTPUT
        ) as run:
            stock = sql_only.run_pgbench_once(
                cfg,
                Path(stream.name),
                protocol="prepared",
                benchmark_mode="stock",
                duration=1,
                seed=44,
            )
        arguments = run.call_args.args[0]
        environment = run.call_args.kwargs["environment"]
        self.assertEqual(arguments[arguments.index("-h") + 1], cfg.stock_host)
        self.assertNotIn("pg_local_cache", environment["PGOPTIONS"])
        self.assertIsNone(stock["cache_enabled"])


class DatabaseContractTests(unittest.TestCase):
    def test_discovery_requires_expected_major_extension_and_port_zero(self) -> None:
        cfg = config()
        discovery = f"160014|{CURRENT_VERSION}|0|app|16384|postgres|f"
        with mock.patch.object(
            sql_only, "psql", return_value=discovery
        ), mock.patch.object(sql_only, "read_stats", return_value=counters()):
            result = sql_only.discover_server(cfg)
        self.assertEqual(result["pg_local_cache_port"], 0)
        self.assertEqual(result["cache_capacity"], 16_384)

        for bad, message in (
            (f"160014|{CURRENT_VERSION}|6380|app|16384|postgres|f", "port=0"),
            (f"150014|{CURRENT_VERSION}|0|app|16384|postgres|f", "PostgreSQL 16"),
            ("160014||0|app|16384|postgres|f", "CREATE EXTENSION"),
        ):
            with self.subTest(bad=bad), mock.patch.object(
                sql_only, "psql", return_value=bad
            ):
                with self.assertRaisesRegex(RuntimeError, message):
                    sql_only.discover_server(cfg)

    def test_stock_server_has_no_extension_and_matches_query_settings(self) -> None:
        cfg = config()
        settings = {
            name: f"value-{index}"
            for index, name in enumerate(
                sql_only.COMPARABLE_SERVER_SETTINGS, start=1
            )
        }
        settings_output = "|".join(
            settings[name] for name in sql_only.COMPARABLE_SERVER_SETTINGS
        )
        with mock.patch.object(
            sql_only,
            "psql",
            side_effect=[
                "160014|f||postgres|f",
                settings_output,
            ],
        ):
            result = sql_only.discover_stock_server(
                cfg,
                mapped_server={"server_version_num": 160014},
                mapped_settings=settings,
            )
        self.assertFalse(result["extension_installed"])
        self.assertFalse(result["pg_local_cache_preloaded"])
        self.assertTrue(result["settings_match_mapped_server"])

        with mock.patch.object(
            sql_only, "psql", return_value="160014|t||postgres|f"
        ):
            with self.assertRaisesRegex(RuntimeError, "must not have"):
                sql_only.discover_stock_server(
                    cfg,
                    mapped_server={"server_version_num": 160014},
                    mapped_settings=settings,
                )

        different = dict(settings)
        different["work_mem"] = "different"
        different_output = "|".join(
            different[name] for name in sql_only.COMPARABLE_SERVER_SETTINGS
        )
        with mock.patch.object(
            sql_only,
            "psql",
            side_effect=["160014|f||postgres|f", different_output],
        ):
            with self.assertRaisesRegex(RuntimeError, "settings differ"):
                sql_only.discover_stock_server(
                    cfg,
                    mapped_server={"server_version_num": 160014},
                    mapped_settings=settings,
                )

    def test_setup_creates_whole_row_table_and_calls_attach_table(self) -> None:
        cfg = config(keys=9, payload_bytes=77)
        mapping = {
            "whole_row": True,
            "namespace": cfg.namespace,
            "primary_key_columns": ["id"],
        }
        with mock.patch.object(
            sql_only, "psql", return_value=json.dumps(mapping)
        ) as psql:
            returned = sql_only.setup_objects(cfg)
        query = psql.call_args.args[1]
        self.assertIn("PRIMARY KEY (id)", query)
        self.assertIn("generate_series(1, 9)", query)
        self.assertIn("string_agg(pg_catalog.md5", query)
        self.assertIn('GRANT CONNECT ON DATABASE "app"', query)
        self.assertIn("local_cache.attach_table", query)
        self.assertNotIn("attach_value", query)
        self.assertTrue(psql.call_args.kwargs["script"])
        self.assertEqual(returned, mapping)

        with mock.patch.object(sql_only, "psql", return_value="") as psql:
            sql_only.setup_stock_objects(cfg)
        stock_query = psql.call_args.args[1]
        self.assertIn("PRIMARY KEY (id)", stock_query)
        self.assertNotIn("local_cache", stock_query)
        self.assertEqual(psql.call_args.kwargs["target"], "stock")

    def test_setup_redacts_disposable_role_password_from_errors(self) -> None:
        cfg = config(app_password="never-print-this-secret")
        with mock.patch.object(
            sql_only,
            "psql",
            side_effect=RuntimeError(
                "CONTEXT: CREATE ROLE PASSWORD 'never-print-this-secret'"
            ),
        ):
            with self.assertRaises(RuntimeError) as raised:
                sql_only.setup_objects(cfg)
        self.assertNotIn(cfg.app_password, str(raised.exception))
        self.assertIn("<redacted>", str(raised.exception))

    def test_application_role_must_be_actual_isolated_login(self) -> None:
        cfg = config()
        identity = f"{cfg.app_user}|f|t|f|f|f|f|f|t"
        with mock.patch.object(sql_only, "psql", return_value=identity) as psql:
            role = sql_only.validate_application_role(cfg)
        self.assertTrue(psql.call_args.kwargs["application"])
        self.assertFalse(role["superuser"])
        self.assertTrue(role["local_cache_schema_usage"])

        with mock.patch.object(
            sql_only,
            "psql",
            return_value=f"{cfg.app_user}|t|t|f|f|f|f|f|f",
        ):
            with self.assertRaisesRegex(RuntimeError, "NOSUPERUSER"):
                sql_only.validate_application_role(cfg)

    def test_read_proof_compares_identical_rows(self) -> None:
        cfg = config()
        replies = [
            "Index Scan using rows_pkey on rows",
            '{"tenant_id":7,"id":1}',
            "Result",
            '{"tenant_id":7,"id":1}',
            "Index Scan using rows_pkey on rows",
            '{"tenant_id":7,"id":1}',
        ]
        with mock.patch.object(sql_only, "psql", side_effect=replies):
            proof = sql_only.explain_and_sample(cfg)
        self.assertTrue(proof["direct_and_cached_rows_equal"])
        self.assertTrue(proof["stock_mapped_and_cached_rows_equal"])
        self.assertIn("local_cache.mget", proof["query"])

    def test_cold_probe_requires_exact_one_miss_fill_then_hit(self) -> None:
        cfg = config()
        snapshots = [
            counters(hits=10, misses=20, fills=20),
            counters(hits=11, misses=21, fills=21),
        ]
        with mock.patch.object(
            sql_only, "invalidate_namespace", return_value=5
        ), mock.patch.object(
            sql_only, "read_stats", side_effect=snapshots
        ), mock.patch.object(
            sql_only, "psql", return_value="7|1|x\n7|1|x"
        ) as psql:
            proof = sql_only.cold_miss_fill_hit_proof(cfg)
        self.assertEqual(proof["status"], "PASS")
        self.assertEqual(proof["sql_cache_hits_during_measurement"], 1)
        script = psql.call_args.args[1]
        self.assertIn("PREPARE pglc_cold", script)
        self.assertEqual(script.count("EXECUTE pglc_cold(1)"), 2)

    def test_cold_probe_rejects_counter_contamination(self) -> None:
        cfg = config()
        snapshots = [counters(), counters(hits=2, misses=1, fills=1)]
        with mock.patch.object(
            sql_only, "invalidate_namespace", return_value=0
        ), mock.patch.object(
            sql_only, "read_stats", side_effect=snapshots
        ), mock.patch.object(
            sql_only, "psql", return_value="row\nrow"
        ):
            with self.assertRaisesRegex(RuntimeError, "accounting mismatch"):
                sql_only.cold_miss_fill_hit_proof(cfg)

    def test_complete_warm_pass_fills_every_key_exactly_once(self) -> None:
        cfg = config(keys=3)
        snapshots = [counters(), counters(misses=3, fills=3)]
        with mock.patch.object(
            sql_only, "invalidate_namespace", return_value=1
        ), mock.patch.object(
            sql_only, "read_stats", side_effect=snapshots
        ), mock.patch.object(sql_only, "psql", return_value="") as psql:
            proof = sql_only.warm_all_keys(cfg)
        script = psql.call_args.args[1]
        self.assertIn("EXECUTE pglc_warm(1)", script)
        self.assertIn("EXECUTE pglc_warm(2)", script)
        self.assertIn("EXECUTE pglc_warm(3)", script)
        self.assertEqual(proof["keys_filled"], 3)
        self.assertTrue(psql.call_args.kwargs["discard_rows"])

    def test_sentinel_integrity_checks_bounds_rows_and_exact_hits(self) -> None:
        cfg = config(keys=8)
        rows = "\n".join(
            json.dumps({"tenant_id": 7, "id": key}) for key in (1, 4, 8)
        )
        replies = ["8|1|8|8|t", "8|1|8|8|t", rows, rows, rows]
        snapshots = [
            counters(hits=10),
            counters(hits=10),
            counters(hits=10),
            counters(hits=13),
        ]
        with mock.patch.object(
            sql_only, "psql", side_effect=replies
        ) as psql, mock.patch.object(
            sql_only, "read_stats", side_effect=snapshots
        ):
            proof = sql_only.sentinel_row_integrity_check(cfg)
        self.assertEqual(proof["source_row_count"], 8)
        self.assertEqual(proof["sentinel_keys"], [1, 4, 8])
        self.assertTrue(proof["direct_and_cached_rows_equal"])
        self.assertEqual(
            proof["cached_counter_deltas"][
                "sql_cache_hits_during_measurement"
            ],
            3,
        )
        self.assertTrue(proof["stock_mapped_and_cached_rows_equal"])
        direct_script = psql.call_args_list[2].args[1]
        cached_script = psql.call_args_list[3].args[1]
        self.assertNotIn("pg_local_cache.sql_cache", direct_script)
        self.assertNotIn("pg_local_cache.sql_cache", cached_script)
        self.assertIn("EXECUTE pglc_integrity(4)", cached_script)

    def test_sentinel_integrity_rejects_wrong_rows(self) -> None:
        cfg = config(keys=8)
        with mock.patch.object(
            sql_only,
            "psql",
            side_effect=[
                "8|1|8|8|t",
                "8|1|8|8|t",
                "7|1|first\n7|8|last",
            ],
        ), mock.patch.object(
            sql_only,
            "read_stats",
            side_effect=[counters(), counters()],
        ):
            with self.assertRaisesRegex(RuntimeError, "expected 3 rows"):
                sql_only.sentinel_row_integrity_check(cfg)


class ValidationAndReportTests(unittest.TestCase):
    def test_valid_report_has_independent_gates_and_exact_counters(self) -> None:
        report = valid_report()
        self.assertEqual(sql_only.validate_report(report), [])

    def test_scaling_snapshot_is_non_gating_but_fail_closed(self) -> None:
        report = valid_report()
        enable_scaling_snapshot(report)
        self.assertEqual(sql_only.validate_report(report), [])
        primary = report["scaling_snapshot"]["profiles"]["c16_p32"]
        self.assertNotIn("protocols", primary)

        cached_run = report["scaling_snapshot"]["profiles"]["c4_p8"][
            "protocols"
        ]["prepared"]["cached_mode"]["runs"][0]
        cached_run["sql_cache_hits_during_measurement"] -= 1
        failures = sql_only.validate_report(report)
        self.assertTrue(
            any("c4/k8" in item and "non-exact hit" in item for item in failures)
        )

    def test_scaling_snapshot_rejects_wrong_pipeline_and_primary_copy(self) -> None:
        report = valid_report()
        enable_scaling_snapshot(report)
        direct_run = report["scaling_snapshot"]["profiles"]["c4_p8"][
            "protocols"
        ]["extended"]["direct_mode"]["runs"][0]
        direct_run["operations_per_batch"] = 32
        report["protocols"]["prepared"]["stock_mode"]["runs"][0][
            "operations_per_batch"
        ] = 8
        report["scaling_snapshot"]["profiles"]["c16_p32"]["protocols"] = (
            copy.deepcopy(report["protocols"])
        )
        failures = sql_only.validate_report(report)
        self.assertTrue(any("8 keys per MGET" in item for item in failures))
        self.assertTrue(any("32 keys per MGET" in item for item in failures))
        self.assertTrue(any("must not be duplicated" in item for item in failures))

    def test_cached_hit_accounting_must_equal_successful_selects(self) -> None:
        report = valid_report()
        cached_run = report["protocols"]["prepared"]["cached_mode"]["runs"][0]
        cached_run["sql_cache_hits_during_measurement"] -= 1
        failures = sql_only.validate_report(report)
        self.assertTrue(any("non-exact hit accounting" in item for item in failures))

    def test_direct_mode_must_not_touch_any_sql_cache_counter(self) -> None:
        report = valid_report()
        direct_run = report["protocols"]["extended"]["direct_mode"]["runs"][0]
        direct_run["sql_cache_bypasses_during_measurement"] = 1
        failures = sql_only.validate_report(report)
        self.assertTrue(any("touched sql_cache_bypasses" in item for item in failures))

    def test_each_protocol_fails_its_own_10k_gate(self) -> None:
        report = valid_report()
        report["protocols"]["extended"] = lane("extended", rate=9_999.0)
        failures = sql_only.validate_report(report)
        self.assertTrue(any("extended cached median" in item for item in failures))
        self.assertFalse(any("prepared cached median" in item for item in failures))

    def test_latency_has_no_implicit_limit_and_explicit_limit_is_enforced(self) -> None:
        report = valid_report()
        latency = report["protocols"]["prepared"]["cached_mode"]["latency"]
        self.assertEqual(latency["gate"]["status"], "MEASURED")
        self.assertIsNone(latency["gate"]["maximum_p99_ms"])

        latency["gate"].update(
            {"maximum_p99_ms": 0.5, "status": "FAIL"}
        )
        report["protocols"]["prepared"]["latency_gate"].update(
            {"maximum_p99_ms": 0.5, "status": "FAIL"}
        )
        failures = sql_only.validate_report(report)
        self.assertTrue(
            any("prepared cached latency gate" in item for item in failures)
        )

    def test_latency_requires_minimum_sample_count(self) -> None:
        report = valid_report()
        latency = report["protocols"]["extended"]["stock_mode"]["latency"]
        latency["distribution"]["sample_count"] = 99
        latency["gate"]["status"] = "FAIL"
        report["protocols"]["extended"]["latency_gate"]["status"] = "FAIL"
        failures = sql_only.validate_report(report)
        self.assertTrue(any("extended stock latency gate" in item for item in failures))

    def test_latency_distributions_must_match_raw_samples(self) -> None:
        report = valid_report()
        latency = report["protocols"]["prepared"]["cached_mode"]["latency"]
        latency["raw_samples_ms"][0] = 2.0
        failures = sql_only.validate_report(report)
        self.assertTrue(
            any("aggregate latency distribution does not match" in item for item in failures)
        )
        self.assertTrue(
            any("latency run 1 distribution does not match" in item for item in failures)
        )

    def test_malformed_latency_limit_fails_without_an_exception(self) -> None:
        report = valid_report()
        latency = report["protocols"]["prepared"]["cached_mode"]["latency"]
        latency["gate"].update({"maximum_p99_ms": "invalid", "status": "FAIL"})
        report["protocols"]["prepared"]["latency_gate"].update(
            {"maximum_p99_ms": "invalid", "status": "FAIL"}
        )
        failures = sql_only.validate_report(report)
        self.assertTrue(any("prepared cached latency gate" in item for item in failures))

    def test_relative_gates_are_optional_and_fail_independently(self) -> None:
        report = valid_report()
        self.assertEqual(sql_only.validate_report(report), [])
        prepared = report["protocols"]["prepared"]
        prepared["relative_throughput_gate"] = sql_only.relative_throughput_gate(
            cached_to_direct=2.0,
            cached_to_stock=2.0,
            minimum_cached_to_direct=2.1,
            minimum_cached_to_stock=None,
        )
        failures = sql_only.validate_report(report)
        self.assertTrue(
            any("prepared cached_to_direct relative throughput" in item for item in failures)
        )

    def test_configured_relative_gates_match_workload_and_reject_bad_values(self) -> None:
        report = valid_report()
        report["workload"]["min_cached_to_direct_ratio"] = 0.8
        for protocol in sql_only.PROTOCOLS:
            report["protocols"][protocol]["relative_throughput_gate"] = (
                sql_only.relative_throughput_gate(
                    cached_to_direct=2.0,
                    cached_to_stock=2.0,
                    minimum_cached_to_direct=0.8,
                    minimum_cached_to_stock=None,
                )
            )
        self.assertEqual(sql_only.validate_report(report), [])

        report["protocols"]["prepared"]["relative_throughput_gate"][
            "cached_to_direct"
        ]["minimum_ratio"] = "invalid"
        failures = sql_only.validate_report(report)
        self.assertTrue(
            any("prepared cached_to_direct relative throughput" in item for item in failures)
        )

    def test_cold_proof_is_exact_not_at_least(self) -> None:
        report = valid_report()
        report["cold_miss_fill_hit_proof"]["sql_cache_hits_during_measurement"] = 2
        failures = sql_only.validate_report(report)
        self.assertTrue(any("exactly 1" in item for item in failures))

    def test_warm_proof_requires_exact_miss_and_fill_for_every_key(self) -> None:
        report = valid_report()
        report["complete_keyspace_warm"][
            "sql_cache_fills_during_measurement"
        ] -= 1
        failures = sql_only.validate_report(report)
        self.assertTrue(any("warm proof" in item for item in failures))

    def test_sentinel_row_integrity_check_is_required_and_exact(self) -> None:
        report = valid_report()
        report["sentinel_row_integrity_check"]["cached_counter_deltas"][
            "sql_cache_hits_during_measurement"
        ] = 2
        failures = sql_only.validate_report(report)
        self.assertTrue(any("cached sentinel counters" in item for item in failures))

        report = valid_report()
        del report["sentinel_row_integrity_check"]
        failures = sql_only.validate_report(report)
        self.assertTrue(any("integrity check is missing" in item for item in failures))

    def test_markdown_leads_with_high_signal_sql_throughput(self) -> None:
        markdown = sql_only.render_markdown(valid_report())
        self.assertIn("# pg_local_cache SQL-only benchmark", markdown)
        self.assertIn("## Throughput and end-to-end latency", markdown)
        self.assertIn(
            "| prepared | Stock PostgreSQL (no extension) | c2/k2 | "
            "10 000 ops/s | c2/k1 |",
            markdown,
        )
        self.assertIn(
            "| prepared | pg_local_cache, cache on | c2/k2 | "
            "20 000 ops/s | c2/k1 |",
            markdown,
        )
        self.assertIn("p50", markdown)
        self.assertIn("p95", markdown)
        self.assertIn("p99", markdown)
        self.assertIn("no p99 limit configured", markdown)
        self.assertIn("| extended |", markdown)
        self.assertIn("cold read -> fill -> warm read", markdown)
        self.assertIn("stock/mapped/cache check", markdown)
        self.assertIn("first, middle, and last rows", markdown)
        self.assertNotIn(
            "byte-identical stock PostgreSQL primary-key batch lookup",
            markdown,
        )
        self.assertIn("pg_local_cache.port=0", markdown)
        self.assertIn("independent >=10k", markdown)

    def test_scaling_markdown_labels_throughput_and_p1_latency(self) -> None:
        report = valid_report()
        enable_scaling_snapshot(report)
        secondary = report["scaling_snapshot"]["profiles"]["c4_p8"][
            "protocols"
        ]["prepared"]["cached_mode"]
        secondary["summary"]["median_operations_per_second"] = 34_567.0
        markdown = sql_only.render_markdown(report)
        self.assertIn("## c4/k8 and c16/k32 scaling snapshot", markdown)
        self.assertIn("Throughput profile", markdown)
        self.assertIn("Latency profile", markdown)
        self.assertIn(
            "| prepared | pg_local_cache, cache on | c4/k8 | "
            "34 567 ops/s | c4/k1 |",
            markdown,
        )
        self.assertIn(
            "| prepared | pg_local_cache, cache on | c16/k32 |",
            markdown,
        )
        self.assertIn("c4/k1 and c16/k1", markdown)

    def test_report_writer_publishes_json_and_markdown_atomically(self) -> None:
        report = valid_report()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            sql_only.write_report(report, output)
            parsed = json.loads((output / "sql-only.json").read_text())
            markdown = (output / "sql-only.md").read_text()
            self.assertEqual(parsed["gate"]["status"], "PASS")
            self.assertIn("Throughput and end-to-end latency", markdown)
            self.assertFalse((output / ".sql-only.json.tmp").exists())
            self.assertFalse((output / ".sql-only.md.tmp").exists())

    def test_failure_writer_never_leaves_stale_success_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "sql-only.json").write_text("stale")
            (output / "sql-only.md").write_text("stale")
            try:
                raise RuntimeError("benchmark exploded")
            except RuntimeError as error:
                sql_only.write_failure_report(error, output)
            self.assertFalse((output / "sql-only.json").exists())
            self.assertFalse((output / "sql-only.md").exists())
            failure = json.loads((output / "sql-only-failure.json").read_text())
            self.assertEqual(failure["status"], "FAIL")
            self.assertEqual(failure["error_type"], "RuntimeError")

    def test_harness_has_no_resp_client_or_token_dependency(self) -> None:
        source = (ROOT / "benchmarks" / "sql_only.py").read_text()
        self.assertNotIn("RespConnection", source)
        self.assertNotIn("AUTH_TOKEN", source)
        self.assertNotIn("PGLC_BENCH_AUTH_TOKEN", source)


if __name__ == "__main__":
    unittest.main()
