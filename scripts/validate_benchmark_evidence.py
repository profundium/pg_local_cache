#!/usr/bin/env python3
"""Validate benchmark artifacts before they are attached to a release."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import statistics
import sys
from typing import Any, Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from benchmarks import sql_only as sql_only_benchmark


WHOLE_ROW_SCHEMA_VERSION = 4
SQL_ONLY_SCHEMA_VERSION = 3
MINIMUM_OPERATIONS_PER_SECOND = 10_000
MINIMUM_RELATIVE_THROUGHPUT = 1.50
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
IMAGE_IDENTITY_PATTERN = re.compile(r"(?:[^\s]+@)?sha256:[0-9a-f]{64}")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def require_mapping(value: Any, context: str) -> Mapping[str, Any]:
    require(isinstance(value, dict), f"{context}: missing object")
    return value


def require_runs(value: Any, context: str) -> list[Mapping[str, Any]]:
    require(
        isinstance(value, list) and bool(value),
        f"{context}: missing measured runs",
    )
    runs: list[Mapping[str, Any]] = []
    for index, run in enumerate(value, start=1):
        runs.append(require_mapping(run, f"{context} run {index}"))
    return runs


def require_minimum(value: Any, minimum: float, context: str) -> float:
    valid_number = (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )
    require(
        valid_number and float(value) >= minimum,
        f"{context}: minimum {value!r} is below {minimum}",
    )
    return float(value)


def require_positive_integer(value: Any, context: str) -> int:
    require(
        isinstance(value, int) and not isinstance(value, bool) and value >= 1,
        f"{context}: expected a positive integer, got {value!r}",
    )
    return value


def require_known_string(value: Any, context: str) -> None:
    require(
        isinstance(value, str)
        and bool(value.strip())
        and value.strip().lower() not in {"unknown", "unavailable"},
        f"{context}: value is missing or unknown",
    )


def require_cpu_identity(
    environment: Mapping[str, Any], client: Mapping[str, Any], context: str
) -> None:
    cpu_model = client.get("cpu_model")
    if isinstance(cpu_model, str) and cpu_model.strip().lower() not in {
        "",
        "unknown",
        "unavailable",
    }:
        return
    require_known_string(environment.get("machine"), f"{context} architecture")


def require_image_identity(value: Any, context: str) -> None:
    require(
        isinstance(value, str)
        and IMAGE_IDENTITY_PATTERN.fullmatch(value.strip()) is not None,
        f"{context}: expected a resolved sha256 image identity",
    )


def validate_throughput_result(
    value: Any,
    context: str,
    *,
    expected_runs: int,
) -> float:
    result = require_mapping(value, context)
    summary = require_mapping(result.get("summary"), f"{context} summary")
    runs = require_runs(result.get("runs"), context)
    require(
        len(runs) == expected_runs,
        f"{context}: expected {expected_runs} runs, got {len(runs)}",
    )
    rates: list[float] = []
    for index, run in enumerate(runs, start=1):
        rate = require_minimum(
            run.get("operations_per_second"),
            0,
            f"{context} run {index} throughput",
        )
        rates.append(rate)
        for failure_field in ("errors", "failed_batches"):
            if failure_field in run:
                require(
                    run.get(failure_field) == 0,
                    f"{context} run {index}: {failure_field} is not zero",
                )
    reported_median = require_minimum(
        summary.get("median_operations_per_second"),
        0,
        f"{context} median throughput",
    )
    expected_median = float(statistics.median(rates))
    require(
        math.isclose(reported_median, expected_median, rel_tol=1e-12, abs_tol=1e-9),
        f"{context}: reported median does not match measured runs",
    )
    return reported_median


def read_report(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{path}: cannot read benchmark JSON: {error}") from error
    return require_mapping(value, str(path))


def validate_common(
    path: Path,
    report: Mapping[str, Any],
    *,
    expected_revision: str,
    schema_version: int,
) -> Mapping[str, Any]:
    require(
        report.get("schema_version") == schema_version,
        f"{path}: expected schema_version={schema_version}",
    )
    environment = require_mapping(report.get("environment"), f"{path}: environment")
    revision = environment.get("source_revision")
    require(
        revision == expected_revision,
        f"{path}: source revision {revision!r} != {expected_revision!r}",
    )
    harness = environment.get("harness_sha256")
    require(
        isinstance(harness, str) and SHA256_PATTERN.fullmatch(harness) is not None,
        f"{path}: harness checksum is missing or invalid",
    )
    gate = require_mapping(report.get("gate"), f"{path}: gate")
    require(gate.get("status") == "PASS", f"{path}: benchmark gate is not PASS")
    return environment


def validate_whole_row(path: Path, expected_revision: str) -> None:
    report = read_report(path)
    environment = validate_common(
        path,
        report,
        expected_revision=expected_revision,
        schema_version=WHOLE_ROW_SCHEMA_VERSION,
    )

    runtime = require_mapping(
        environment.get("container_runtime"), f"{path}: container runtime"
    )
    require_known_string(runtime.get("docker_version"), f"{path}: Docker version")
    require_known_string(runtime.get("compose_version"), f"{path}: Compose version")

    images = require_mapping(environment.get("images"), f"{path}: images")
    for name in (
        "postgres",
        "valkey",
        "redis",
        "pg_local_cache",
        "benchmark_client",
    ):
        image = require_mapping(images.get(name), f"{path}: image {name}")
        require_known_string(image.get("reference"), f"{path}: image {name} reference")
        require_image_identity(image.get("identity"), f"{path}: image {name} identity")

    client = require_mapping(
        environment.get("benchmark_client"), f"{path}: benchmark client"
    )
    require_minimum(
        client.get("logical_cpu_count"), 1, f"{path}: benchmark client CPUs"
    )
    require_cpu_identity(environment, client, f"{path}: benchmark CPU")
    cgroup = require_mapping(client.get("cgroup_v2"), f"{path}: benchmark cgroup")
    require_known_string(cgroup.get("cpu.max"), f"{path}: benchmark cpu.max")
    require_known_string(cgroup.get("memory.max"), f"{path}: benchmark memory.max")
    client_cpu_quota = require_minimum(
        cgroup.get("cpu_quota_cores"), 0.1, f"{path}: benchmark CPU quota"
    )
    require_minimum(
        cgroup.get("memory_limit_bytes"), 1, f"{path}: benchmark memory limit"
    )

    workload = require_mapping(report.get("workload"), f"{path}: workload")
    repetitions = require_positive_integer(
        workload.get("repetitions"), f"{path}: workload repetitions"
    )
    configured_client_cpus = require_minimum(
        workload.get("client_cpus"), 0.1, f"{path}: workload client_cpus"
    )
    require(
        math.isclose(
            client_cpu_quota,
            configured_client_cpus,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ),
        f"{path}: benchmark client CPU quota does not match workload",
    )
    require_minimum(
        workload.get("server_cpus_per_target"),
        0.1,
        f"{path}: workload server_cpus_per_target",
    )
    for field in ("pg_local_cache_workers", "pgbench_jobs"):
        require_positive_integer(workload.get(field), f"{path}: workload {field}")
    require_known_string(workload.get("client_memory"), f"{path}: client memory limit")
    require_known_string(
        workload.get("server_memory_per_target"), f"{path}: server memory limit"
    )

    resp = require_mapping(report.get("resp_full_row"), f"{path}: resp_full_row")
    resp_gate = require_mapping(resp.get("gate"), f"{path}: RESP gate")
    require(resp_gate.get("status") == "PASS", f"{path}: RESP gate is not PASS")
    require_minimum(
        resp_gate.get("minimum_pg_local_cache_ops_per_second"),
        MINIMUM_OPERATIONS_PER_SECOND,
        f"{path}: RESP gate",
    )
    targets = require_mapping(resp.get("targets"), f"{path}: RESP targets")
    for name in ("pg_local_cache", "valkey", "redis"):
        median = validate_throughput_result(
            targets.get(name),
            f"{path}: RESP target {name}",
            expected_runs=repetitions,
        )
        if name == "pg_local_cache":
            require_minimum(
                median,
                MINIMUM_OPERATIONS_PER_SECOND,
                f"{path}: RESP measured throughput",
            )

    width_gate = require_mapping(report.get("width_gate"), f"{path}: width gate")
    require(width_gate.get("status") == "PASS", f"{path}: width gate is not PASS")
    width_lanes = require_mapping(
        report.get("resp_payload_width_sweep"), f"{path}: payload-width lanes"
    )
    require(bool(width_lanes), f"{path}: payload-width lanes are empty")
    for width, result in width_lanes.items():
        validate_throughput_result(
            result,
            f"{path}: payload-width lane {width}",
            expected_runs=repetitions,
        )
    validate_markdown(path)


def validate_sql_only(path: Path, expected_revision: str) -> None:
    report = read_report(path)
    environment = validate_common(
        path,
        report,
        expected_revision=expected_revision,
        schema_version=SQL_ONLY_SCHEMA_VERSION,
    )
    generator_failures = sql_only_benchmark.validate_report(report)
    require(
        not generator_failures,
        f"{path}: SQL-only generator contract failed: "
        + "; ".join(generator_failures),
    )
    client = require_mapping(
        environment.get("benchmark_client"), f"{path}: benchmark client"
    )
    require_minimum(
        client.get("logical_cpu_count"), 1, f"{path}: benchmark client CPUs"
    )
    require_cpu_identity(environment, client, f"{path}: benchmark CPU")
    cgroup = require_mapping(client.get("cgroup_v2"), f"{path}: benchmark cgroup")
    require_known_string(cgroup.get("cpu.max"), f"{path}: benchmark cpu.max")
    require_known_string(cgroup.get("memory.max"), f"{path}: benchmark memory.max")
    require_minimum(
        cgroup.get("memory_limit_bytes"), 1, f"{path}: benchmark memory limit"
    )

    workload = require_mapping(report.get("workload"), f"{path}: workload")
    repetitions = require_positive_integer(
        workload.get("repetitions"), f"{path}: workload repetitions"
    )
    protocols = require_mapping(report.get("protocols"), f"{path}: protocols")
    for protocol in ("prepared", "extended"):
        lane = require_mapping(protocols.get(protocol), f"{path}: {protocol} protocol")
        require(
            lane.get("query_protocol") == protocol,
            f"{path}: {protocol} protocol label is invalid",
        )

        throughput_gate = require_mapping(
            lane.get("throughput_gate"), f"{path}: {protocol} throughput gate"
        )
        require(
            throughput_gate.get("status") == "PASS",
            f"{path}: {protocol} throughput gate is not PASS",
        )
        require_minimum(
            throughput_gate.get("minimum_cached_operations_per_second"),
            MINIMUM_OPERATIONS_PER_SECOND,
            f"{path}: {protocol} throughput gate",
        )
        require_minimum(
            throughput_gate.get("measured_cached_operations_per_second"),
            MINIMUM_OPERATIONS_PER_SECOND,
            f"{path}: {protocol} measured cached throughput",
        )

        relative_gate = require_mapping(
            lane.get("relative_throughput_gate"),
            f"{path}: {protocol} relative gate",
        )
        require(
            relative_gate.get("status") == "PASS",
            f"{path}: {protocol} relative gate is not PASS",
        )
        for comparison in ("cached_to_direct", "cached_to_stock"):
            comparison_gate = require_mapping(
                relative_gate.get(comparison),
                f"{path}: {protocol}/{comparison} gate",
            )
            require(
                comparison_gate.get("status") == "PASS",
                f"{path}: {protocol}/{comparison} is not PASS",
            )
            require_minimum(
                comparison_gate.get("minimum_ratio"),
                MINIMUM_RELATIVE_THROUGHPUT,
                f"{path}: {protocol}/{comparison} gate",
            )
            require_minimum(
                comparison_gate.get("measured_ratio"),
                comparison_gate.get("minimum_ratio"),
                f"{path}: {protocol}/{comparison} measured ratio",
            )

        latency_gate = require_mapping(
            lane.get("latency_gate"), f"{path}: {protocol} latency gate"
        )
        require(
            latency_gate.get("status") in {"PASS", "MEASURED"},
            f"{path}: {protocol} latency gate failed",
        )
        require_minimum(
            latency_gate.get("minimum_samples_per_run"),
            1,
            f"{path}: {protocol} latency gate",
        )
        require_minimum(
            latency_gate.get("minimum_aggregate_samples_per_mode"),
            latency_gate.get("minimum_samples_per_run"),
            f"{path}: {protocol} aggregate latency gate",
        )

        for mode in ("stock", "direct", "cached"):
            result = require_mapping(
                lane.get(f"{mode}_mode"), f"{path}: {protocol}/{mode} mode"
            )
            validate_throughput_result(
                result,
                f"{path}: {protocol}/{mode}",
                expected_runs=repetitions,
            )
            latency = require_mapping(
                result.get("latency"), f"{path}: {protocol}/{mode} latency"
            )
            latency_runs = latency.get("runs")
            require_runs(latency_runs, f"{path}: {protocol}/{mode} latency")
            for run_number, run in enumerate(latency_runs, start=1):
                run_object = require_mapping(
                    run, f"{path}: {protocol}/{mode} latency run {run_number}"
                )
                run_distribution = require_mapping(
                    run_object.get("distribution"),
                    f"{path}: {protocol}/{mode} latency run {run_number} distribution",
                )
                require_minimum(
                    run_distribution.get("sample_count"),
                    latency_gate.get("minimum_samples_per_run"),
                    f"{path}: {protocol}/{mode} latency run {run_number}",
                )
            distribution = require_mapping(
                latency.get("distribution"),
                f"{path}: {protocol}/{mode} latency distribution",
            )
            require_minimum(
                distribution.get("sample_count"),
                latency_gate.get("minimum_aggregate_samples_per_mode"),
                f"{path}: {protocol}/{mode} latency samples",
            )
            require(
                isinstance(latency.get("raw_samples_ms"), list),
                f"{path}: {protocol}/{mode} raw latency samples are missing",
            )
            require(
                len(latency["raw_samples_ms"]) == distribution.get("sample_count"),
                f"{path}: {protocol}/{mode} raw latency sample count is inconsistent",
            )

    if workload.get("scaling_snapshot_enabled") is not True:
        validate_markdown(path)
        return

    snapshot = require_mapping(
        report.get("scaling_snapshot"), f"{path}: scaling snapshot"
    )
    require(
        snapshot.get("status") == "MEASURED",
        f"{path}: scaling snapshot was not measured",
    )
    require(
        snapshot.get("performance_gating") is False,
        f"{path}: scaling snapshot must be performance non-gating",
    )
    profiles = require_mapping(
        snapshot.get("profiles"), f"{path}: scaling profiles"
    )
    primary = require_mapping(
        profiles.get("c16_p32"), f"{path}: c16/k32 profile"
    )
    require(
        primary.get("source") == "primary_strict_profile"
        and primary.get("source_path") == "$.protocols"
        and "protocols" not in primary,
        f"{path}: c16/k32 must reference primary evidence without copying it",
    )
    primary_workload = require_mapping(
        primary.get("workload"), f"{path}: c16/k32 workload"
    )
    require(
        primary_workload.get("concurrency") == 16
        and primary_workload.get("throughput_pipeline") == 32
        and primary_workload.get("latency_pipeline") == 1,
        f"{path}: c16/k32 workload is invalid",
    )

    secondary = require_mapping(
        profiles.get("c4_p8"), f"{path}: c4/k8 profile"
    )
    secondary_workload = require_mapping(
        secondary.get("workload"), f"{path}: c4/k8 workload"
    )
    require(
        secondary_workload.get("concurrency") == 4
        and secondary_workload.get("throughput_pipeline") == 8
        and secondary_workload.get("latency_pipeline") == 1,
        f"{path}: c4/k8 workload is invalid",
    )
    secondary_repetitions = require_positive_integer(
        secondary_workload.get("repetitions"),
        f"{path}: c4/k8 repetitions",
    )
    secondary_protocols = require_mapping(
        secondary.get("protocols"), f"{path}: c4/k8 protocols"
    )
    for protocol in ("prepared", "extended"):
        lane = require_mapping(
            secondary_protocols.get(protocol),
            f"{path}: c4/k8 {protocol}",
        )
        throughput_gate = require_mapping(
            lane.get("throughput_gate"),
            f"{path}: c4/k8 {protocol} throughput status",
        )
        require(
            throughput_gate.get("status") == "MEASURED"
            and throughput_gate.get("minimum_cached_operations_per_second")
            is None,
            f"{path}: c4/k8 {protocol} must not have a performance gate",
        )
        for mode in ("stock", "direct", "cached"):
            result = require_mapping(
                lane.get(f"{mode}_mode"),
                f"{path}: c4/k8 {protocol}/{mode}",
            )
            validate_throughput_result(
                result,
                f"{path}: c4/k8 {protocol}/{mode}",
                expected_runs=secondary_repetitions,
            )
            latency = require_mapping(
                result.get("latency"),
                f"{path}: c4/k8 {protocol}/{mode} latency",
            )
            distribution = require_mapping(
                latency.get("distribution"),
                f"{path}: c4/k8 {protocol}/{mode} latency distribution",
            )
            raw_samples = latency.get("raw_samples_ms")
            require(
                isinstance(raw_samples, list)
                and len(raw_samples) == distribution.get("sample_count"),
                f"{path}: c4/k8 {protocol}/{mode} raw latency samples are inconsistent",
            )
    validate_markdown(path)


def validate_markdown(json_path: Path) -> None:
    markdown_path = json_path.with_suffix(".md")
    require(
        markdown_path.is_file() and markdown_path.stat().st_size > 0,
        f"{markdown_path}: missing benchmark Markdown",
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True, help="expected source commit SHA")
    parser.add_argument(
        "--whole", type=Path, help="optional whole-row RESP JSON report"
    )
    parser.add_argument(
        "--sql-only", required=True, type=Path, help="SQL-only JSON report"
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    if arguments.whole is not None:
        validate_whole_row(arguments.whole, arguments.revision)
    validate_sql_only(arguments.sql_only, arguments.revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
