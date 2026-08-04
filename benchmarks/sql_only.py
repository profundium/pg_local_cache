#!/usr/bin/env python3
"""Standalone benchmark for pg_local_cache's canonical SQL-only KV API.

The harness talks only to PostgreSQL.  It deliberately requires
``pg_local_cache.port = 0`` and never opens a RESP connection.  A disposable
whole-row table is attached with ``local_cache.attach_table`` and queried by
a real LOGIN NOSUPERUSER role through ``local_cache.get`` and
``local_cache.mget``.

Two protocol lanes are reported independently:

* pgbench ``prepared`` (server-side prepared statement reuse), and
* pgbench ``extended`` (unnamed Parse/Bind/Execute for every statement).

Each lane compares a separate stock PostgreSQL server, an ordered mapped-table
batch lookup, and the SQL KV API.  All modes return the same bytes for the same
schema, rows, role, key stream, client limits, and protocol.  A separate scalar
key pass records end-to-end latency percentiles; throughput reports resolved
key positions per second from equal-width batches.

Cache counters are sampled around every mapped-server run.  Unrelated
pg_local_cache SQL traffic therefore makes the benchmark fail closed instead
of silently inflating its result.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import secrets
import statistics
import subprocess
import sys
import tempfile
import traceback
from typing import Any, Mapping, Sequence


SQL_COUNTERS = (
    "sql_cache_hits",
    "sql_cache_misses",
    "sql_cache_fills",
    "sql_cache_bypasses",
)
PROTOCOLS = ("prepared", "extended")
BENCHMARK_MODES = ("stock", "direct", "cached")
CACHE_ENABLED_BY_MODE = {"stock": None, "direct": False, "cached": True}
CUSTOM_SCAN_NAME = "Custom Scan (pg_local_cache_sql)"
DEFAULT_MINIMUM_OPS = 10_000.0
SUPPORTED_POSTGRES_MAJORS = frozenset(range(14, 19))
SCALING_PRIMARY_CONCURRENCY = 16
SCALING_PRIMARY_PIPELINE = 32
SCALING_SECONDARY_CONCURRENCY = 4
SCALING_SECONDARY_PIPELINE = 8
SCALING_PRIMARY_PROFILE = "c16_p32"
SCALING_SECONDARY_PROFILE = "c4_p8"
TENANT_ID = 7
MAX_PAYLOAD_BYTES = 3_000
COMPARABLE_SERVER_SETTINGS = (
    "block_size",
    "shared_buffers",
    "work_mem",
    "effective_cache_size",
    "random_page_cost",
    "seq_page_cost",
    "cpu_tuple_cost",
    "cpu_index_tuple_cost",
    "jit",
    "max_parallel_workers_per_gather",
    "default_statistics_target",
)


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip() or str(default)
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be in [{minimum}, {maximum}]")
    return value


def env_float(
    name: str, default: float, minimum: float, maximum: float
) -> float:
    raw = os.environ.get(name, "").strip() or str(default)
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be numeric") from error
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise ValueError(f"{name} must be in [{minimum}, {maximum}]")
    return value


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be a boolean")


def env_optional_float(
    name: str, minimum: float, maximum: float
) -> float | None:
    if not os.environ.get(name, "").strip():
        return None
    return env_float(name, minimum, minimum, maximum)


def postgres_major_from_environment() -> int:
    raw = os.environ.get("POSTGRES_MAJOR", "16").strip()
    try:
        major = int(raw)
    except ValueError as error:
        raise ValueError("POSTGRES_MAJOR must be an integer") from error
    if major not in SUPPORTED_POSTGRES_MAJORS:
        raise ValueError("POSTGRES_MAJOR must be one of 14, 15, 16, 17, or 18")
    return major


def safe_connection_value(name: str, default: str) -> str:
    value = os.environ.get(name, default)
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{name} must be a non-empty single-line value")
    return value


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    database: str
    admin_user: str
    admin_password: str
    stock_host: str
    stock_port: int
    stock_database: str
    stock_admin_user: str
    stock_admin_password: str
    duration: float
    warmup_seconds: float
    latency_duration: float
    latency_sample_rate: float
    latency_min_samples: int
    latency_max_p99_ms: float | None
    repetitions: int
    concurrency: int
    jobs: int
    pipeline: int
    keys: int
    payload_bytes: int
    prepared_min_ops: float
    extended_min_ops: float
    min_cached_to_direct_ratio: float | None
    min_cached_to_stock_ratio: float | None
    scaling_snapshot_enabled: bool
    scaling_duration: float
    scaling_warmup_seconds: float
    scaling_latency_duration: float
    scaling_latency_sample_rate: float
    scaling_latency_min_samples: int
    scaling_repetitions: int
    output_directory: Path
    run_id: str
    app_password: str
    keep_objects: bool

    @classmethod
    def from_environment(cls) -> "Config":
        concurrency = env_int("PGLC_SQL_ONLY_BENCH_CONCURRENCY", 16, 1, 256)
        duration = env_float(
            "PGLC_SQL_ONLY_BENCH_DURATION", 30.0, 1.0, 3600.0
        )
        generated_run_id = secrets.token_hex(5)
        config = cls(
            host=safe_connection_value("PGHOST", "127.0.0.1"),
            port=env_int(
                "PGPORT",
                5432,
                1,
                65535,
            ),
            database=safe_connection_value("PGDATABASE", "postgres"),
            admin_user=safe_connection_value("PGUSER", "postgres"),
            admin_password=os.environ.get("PGPASSWORD", ""),
            stock_host=safe_connection_value(
                "PGLC_SQL_ONLY_BENCH_STOCK_HOST", "postgres_stock"
            ),
            stock_port=env_int(
                "PGLC_SQL_ONLY_BENCH_STOCK_PORT", 5432, 1, 65535
            ),
            stock_database=safe_connection_value(
                "PGLC_SQL_ONLY_BENCH_STOCK_DATABASE",
                safe_connection_value("PGDATABASE", "postgres"),
            ),
            stock_admin_user=safe_connection_value(
                "PGLC_SQL_ONLY_BENCH_STOCK_USER",
                safe_connection_value("PGUSER", "postgres"),
            ),
            stock_admin_password=os.environ.get(
                "PGLC_SQL_ONLY_BENCH_STOCK_PASSWORD",
                os.environ.get("PGPASSWORD", ""),
            ),
            duration=duration,
            warmup_seconds=env_float(
                "PGLC_SQL_ONLY_BENCH_WARMUP_SECONDS", 5.0, 0.0, 600.0
            ),
            latency_duration=env_float(
                "PGLC_SQL_ONLY_BENCH_LATENCY_DURATION",
                min(duration, 15.0),
                1.0,
                600.0,
            ),
            latency_sample_rate=env_float(
                "PGLC_SQL_ONLY_BENCH_LATENCY_SAMPLE_RATE",
                0.05,
                0.0001,
                1.0,
            ),
            latency_min_samples=env_int(
                "PGLC_SQL_ONLY_BENCH_LATENCY_MIN_SAMPLES",
                200,
                1,
                10_000_000,
            ),
            latency_max_p99_ms=env_optional_float(
                "PGLC_SQL_ONLY_BENCH_LATENCY_MAX_P99_MS",
                0.001,
                60_000.0,
            ),
            repetitions=env_int(
                "PGLC_SQL_ONLY_BENCH_REPETITIONS", 3, 1, 20
            ),
            concurrency=concurrency,
            jobs=env_int(
                "PGLC_SQL_ONLY_BENCH_JOBS",
                min(concurrency, max(1, os.cpu_count() or 1)),
                1,
                concurrency,
            ),
            pipeline=env_int("PGLC_SQL_ONLY_BENCH_PIPELINE", 32, 1, 256),
            keys=env_int("PGLC_SQL_ONLY_BENCH_KEYS", 4_096, 1, 65_536),
            payload_bytes=env_int(
                "PGLC_SQL_ONLY_BENCH_PAYLOAD_BYTES",
                3_000,
                1,
                MAX_PAYLOAD_BYTES,
            ),
            prepared_min_ops=env_float(
                "PGLC_SQL_ONLY_BENCH_PREPARED_MIN_OPS",
                DEFAULT_MINIMUM_OPS,
                0.0,
                1e12,
            ),
            extended_min_ops=env_float(
                "PGLC_SQL_ONLY_BENCH_EXTENDED_MIN_OPS",
                DEFAULT_MINIMUM_OPS,
                0.0,
                1e12,
            ),
            min_cached_to_direct_ratio=env_float(
                "PGLC_SQL_ONLY_BENCH_MIN_CACHED_TO_DIRECT_RATIO",
                1.5,
                0.0,
                1_000.0,
            ),
            min_cached_to_stock_ratio=env_float(
                "PGLC_SQL_ONLY_BENCH_MIN_CACHED_TO_STOCK_RATIO",
                1.5,
                0.0,
                1_000.0,
            ),
            scaling_snapshot_enabled=env_bool(
                "PGLC_SQL_ONLY_BENCH_SCALING_SNAPSHOT", False
            ),
            scaling_duration=env_float(
                "PGLC_SQL_ONLY_BENCH_SCALING_DURATION", 3.0, 1.0, 600.0
            ),
            scaling_warmup_seconds=env_float(
                "PGLC_SQL_ONLY_BENCH_SCALING_WARMUP_SECONDS",
                1.0,
                0.0,
                600.0,
            ),
            scaling_latency_duration=env_float(
                "PGLC_SQL_ONLY_BENCH_SCALING_LATENCY_DURATION",
                3.0,
                1.0,
                600.0,
            ),
            scaling_latency_sample_rate=env_float(
                "PGLC_SQL_ONLY_BENCH_SCALING_LATENCY_SAMPLE_RATE",
                0.10,
                0.0001,
                1.0,
            ),
            scaling_latency_min_samples=env_int(
                "PGLC_SQL_ONLY_BENCH_SCALING_LATENCY_MIN_SAMPLES",
                500,
                1,
                10_000_000,
            ),
            scaling_repetitions=env_int(
                "PGLC_SQL_ONLY_BENCH_SCALING_REPETITIONS", 1, 1, 3
            ),
            output_directory=Path(
                os.environ.get(
                    "PGLC_SQL_ONLY_BENCH_OUTPUT_DIR", "benchmark-results"
                )
            ),
            run_id=os.environ.get(
                "PGLC_SQL_ONLY_BENCH_RUN_ID", generated_run_id
            ).strip(),
            app_password=(
                os.environ.get("PGLC_SQL_ONLY_BENCH_APP_PASSWORD")
                or secrets.token_urlsafe(32)
            ),
            keep_objects=env_bool("PGLC_SQL_ONLY_BENCH_KEEP_OBJECTS", False),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not re.fullmatch(r"[a-z0-9]{8,16}", self.run_id):
            raise ValueError(
                "PGLC_SQL_ONLY_BENCH_RUN_ID must contain 8-16 lowercase ASCII "
                "letters or digits"
            )
        if self.jobs > self.concurrency:
            raise ValueError("pgbench jobs must not exceed concurrency")
        if self.keys < self.concurrency:
            raise ValueError("key count must be at least concurrency")
        if self.pipeline > 256:
            raise ValueError("pipeline exceeds the supported benchmark limit")
        if self.scaling_snapshot_enabled:
            if (
                self.concurrency != SCALING_PRIMARY_CONCURRENCY
                or self.pipeline != SCALING_PRIMARY_PIPELINE
            ):
                raise ValueError(
                    "the scaling snapshot requires the strict primary profile "
                    f"c{SCALING_PRIMARY_CONCURRENCY}/k{SCALING_PRIMARY_PIPELINE}"
                )
            if self.keys < SCALING_SECONDARY_CONCURRENCY:
                raise ValueError(
                    "the scaling snapshot key count must be at least "
                    f"{SCALING_SECONDARY_CONCURRENCY}"
                )
        if "\x00" in self.admin_password:
            raise ValueError("PGPASSWORD contains a NUL byte")
        if "\x00" in self.stock_admin_password:
            raise ValueError(
                "PGLC_SQL_ONLY_BENCH_STOCK_PASSWORD contains a NUL byte"
            )
        if not self.app_password or "\x00" in self.app_password:
            raise ValueError(
                "PGLC_SQL_ONLY_BENCH_APP_PASSWORD must not be empty or contain NUL"
            )
        if (
            self.stock_host,
            self.stock_port,
            self.stock_database,
        ) == (self.host, self.port, self.database):
            raise ValueError(
                "stock PostgreSQL must use a separate host, port, or database"
            )

    @property
    def schema(self) -> str:
        return f"pglc_sql_bench_{self.run_id}"

    @property
    def table(self) -> str:
        return "rows"

    @property
    def namespace(self) -> str:
        return f"sqlbench_{self.run_id}"

    @property
    def app_user(self) -> str:
        return f"pglc_sql_app_{self.run_id}"

    @property
    def qualified_table(self) -> str:
        return f"{sql_identifier(self.schema)}.{sql_identifier(self.table)}"

    @property
    def lookup_query(self) -> str:
        return (
            "SELECT local_cache.get("
            f"{sql_literal(self.schema + '.' + self.table)}::regclass, "
            "(:key)::bigint);"
        )

    @property
    def direct_lookup_query(self) -> str:
        return (
            "SELECT pg_catalog.row_to_json(pglc_source)::text "
            f"FROM {self.qualified_table} AS pglc_source "
            "WHERE id = :key;"
        )


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def sql_literal(value: str) -> str:
    if "\x00" in value:
        raise ValueError("SQL literal contains a NUL byte")
    return "'" + value.replace("'", "''") + "'"


def connection_environment(
    config: Config, *, application: bool, target: str = "mapped"
) -> dict[str, str]:
    if target not in ("mapped", "stock"):
        raise ValueError(f"unsupported PostgreSQL target {target!r}")
    environment = os.environ.copy()
    environment["PGCONNECT_TIMEOUT"] = "10"
    environment.pop("PGOPTIONS", None)
    password = (
        config.app_password
        if application
        else (
            config.stock_admin_password
            if target == "stock"
            else config.admin_password
        )
    )
    if password:
        environment["PGPASSWORD"] = password
    else:
        environment.pop("PGPASSWORD", None)
    return environment


def psql_arguments(
    config: Config, *, application: bool, target: str = "mapped"
) -> list[str]:
    if target not in ("mapped", "stock"):
        raise ValueError(f"unsupported PostgreSQL target {target!r}")
    stock = target == "stock"
    return [
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-h",
        config.stock_host if stock else config.host,
        "-p",
        str(config.stock_port if stock else config.port),
        "-U",
        (
            config.app_user
            if application
            else (config.stock_admin_user if stock else config.admin_user)
        ),
        "-d",
        config.stock_database if stock else config.database,
        "-Atq",
    ]


def run_checked(
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    input_text: str | None = None,
    timeout: float = 300.0,
) -> str:
    result = subprocess.run(
        list(arguments),
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=dict(environment) if environment is not None else None,
        timeout=timeout,
    )
    if result.returncode != 0:
        output = result.stdout.strip()
        raise RuntimeError(
            f"command failed with exit code {result.returncode}: "
            f"{arguments[0]}\n{output}"
        )
    return result.stdout.strip()


def psql(
    config: Config,
    query: str,
    *,
    application: bool = False,
    script: bool = False,
    discard_rows: bool = False,
    target: str = "mapped",
) -> str:
    arguments = psql_arguments(
        config, application=application, target=target
    )
    input_text: str | None = None
    if discard_rows:
        arguments.extend(("-o", os.devnull))
    if script:
        arguments.extend(("-f", "-"))
        input_text = query
    else:
        arguments.extend(("-c", query))
    return run_checked(
        arguments,
        environment=connection_environment(
            config, application=application, target=target
        ),
        input_text=input_text,
        timeout=300,
    )


def parse_pgbench_output(
    output: str, operations_per_batch: int
) -> dict[str, Any]:
    if operations_per_batch <= 0:
        raise ValueError("operations_per_batch must be positive")
    patterns = {
        "tps": (
            r"^tps = ([0-9]+(?:\.[0-9]+)?) "
            r"\(without initial connection time\)$"
        ),
        "latency": r"^latency average = ([0-9]+(?:\.[0-9]+)?) ms$",
        "transactions": (
            r"^number of transactions actually processed: ([0-9]+)"
        ),
        "failures": r"^number of failed transactions: ([0-9]+)",
    }
    matches = {
        name: re.search(pattern, output, re.MULTILINE)
        for name, pattern in patterns.items()
    }
    if not all(matches[name] for name in ("tps", "latency", "transactions")):
        raise ValueError(f"could not parse pgbench output:\n{output}")
    tps = float(matches["tps"].group(1))  # type: ignore[union-attr]
    transactions = int(
        matches["transactions"].group(1)  # type: ignore[union-attr]
    )
    failures = (
        int(matches["failures"].group(1))
        if matches["failures"] is not None
        else 0
    )
    return {
        "successful_batches": transactions,
        "successful_operations": transactions * operations_per_batch,
        "failed_batches": failures,
        "batch_transactions_per_second": tps,
        "operations_per_second": tps * operations_per_batch,
        "batch_latency_average_ms": float(
            matches["latency"].group(1)  # type: ignore[union-attr]
        ),
        "operations_per_batch": operations_per_batch,
    }


def percentile(sorted_values: Sequence[float], fraction: float) -> float:
    if not sorted_values:
        raise ValueError("percentile requires at least one value")
    if fraction < 0.0 or fraction > 1.0:
        raise ValueError("percentile fraction must be between zero and one")
    position = (len(sorted_values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(sorted_values[lower])
    weight = position - lower
    return (
        float(sorted_values[lower]) * (1.0 - weight)
        + float(sorted_values[upper]) * weight
    )


def read_latency_samples(paths: Sequence[Path]) -> list[float]:
    latency_ms: list[float] = []
    for path in sorted(paths):
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                fields = line.split()
                if not fields:
                    continue
                if len(fields) < 6:
                    raise ValueError(
                        f"malformed pgbench log {path}:{line_number}"
                    )
                try:
                    elapsed_us = int(fields[2])
                except ValueError as error:
                    raise ValueError(
                        f"invalid pgbench latency in {path}:{line_number}"
                    ) from error
                if elapsed_us < 0:
                    raise ValueError(
                        f"negative pgbench latency in {path}:{line_number}"
                    )
                latency_ms.append(elapsed_us / 1000.0)
    if not latency_ms:
        raise ValueError("pgbench latency sampling produced no samples")
    latency_ms.sort()
    return latency_ms


def summarize_latency_samples(latency_ms: Sequence[float]) -> dict[str, Any]:
    if not latency_ms:
        raise ValueError("latency summary requires at least one sample")
    sorted_latency_ms = sorted(float(value) for value in latency_ms)
    if any(
        not math.isfinite(value) or value < 0 for value in sorted_latency_ms
    ):
        raise ValueError("latency samples must be finite and non-negative")
    return {
        "unit": "ms",
        "sample_count": len(sorted_latency_ms),
        "mean_ms": statistics.fmean(sorted_latency_ms),
        "minimum_ms": sorted_latency_ms[0],
        "p50_ms": percentile(sorted_latency_ms, 0.50),
        "p95_ms": percentile(sorted_latency_ms, 0.95),
        "p99_ms": percentile(sorted_latency_ms, 0.99),
        "maximum_ms": sorted_latency_ms[-1],
        "measurement": "end-to-end single-SELECT transaction",
        "percentile_method": "linear interpolation at (n-1)*q",
    }


def parse_latency_logs(paths: Sequence[Path]) -> dict[str, Any]:
    return summarize_latency_samples(read_latency_samples(paths))


def read_text_if_present(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def discover_client_resources() -> dict[str, Any]:
    cpu_info = read_text_if_present(Path("/proc/cpuinfo")) or ""
    cpu_model = "unknown"
    for line in cpu_info.splitlines():
        field, separator, value = line.partition(":")
        if separator and field.strip() in ("model name", "Hardware", "Processor"):
            if value.strip():
                cpu_model = value.strip()
                break

    cpu_max = read_text_if_present(Path("/sys/fs/cgroup/cpu.max"))
    memory_max = read_text_if_present(Path("/sys/fs/cgroup/memory.max"))
    quota_cores: float | None = None
    if cpu_max:
        parts = cpu_max.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                quota = int(parts[0])
                period = int(parts[1])
                if quota > 0 and period > 0:
                    quota_cores = quota / period
            except ValueError:
                pass

    memory_limit_bytes: int | None = None
    if memory_max and memory_max != "max":
        try:
            parsed_memory = int(memory_max)
            if parsed_memory > 0:
                memory_limit_bytes = parsed_memory
        except ValueError:
            pass

    return {
        "logical_cpu_count": os.cpu_count(),
        "cpu_model": cpu_model,
        "cgroup_v2": {
            "cpu.max": cpu_max or "unavailable",
            "cpu_quota_cores": quota_cores,
            "memory.max": memory_max or "unavailable",
            "memory_limit_bytes": memory_limit_bytes,
        },
    }


def lookup_script(config: Config, *, cached: bool = True) -> str:
    lines: list[str] = []
    variables: list[str] = []
    for index in range(config.pipeline):
        variable = f"key_{index}"
        variables.append(variable)
        lines.append(f"\\set {variable} random(1, {config.keys})")
    array = "ARRAY[" + ", ".join(f":{name}" for name in variables) + "]::bigint[]"
    if cached:
        lines.append(
            "SELECT local_cache.mget("
            f"{sql_literal(config.schema + '.' + config.table)}::regclass, "
            f"{array});"
        )
    else:
        lines.append(
            "SELECT pg_catalog.array_agg("
            "pg_catalog.row_to_json(pglc_source)::text ORDER BY pglc_input.ordinality) "
            f"FROM pg_catalog.unnest({array}) WITH ORDINALITY "
            "AS pglc_input(id, ordinality) LEFT JOIN "
            f"{config.qualified_table} AS pglc_source USING (id);"
        )
    return "\n".join(lines) + "\n"


def latency_lookup_script(config: Config, query: str | None = None) -> str:
    return (
        f"\\set tenant {TENANT_ID}\n"
        f"\\set key random(1, {config.keys})\n"
        + (query or config.lookup_query)
        + "\n"
    )


def read_stats(config: Config) -> dict[str, int]:
    raw = psql(config, "SELECT local_cache.stats()::text")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"local_cache.stats() returned invalid JSON: {raw!r}") from error
    if not isinstance(parsed, dict):
        raise RuntimeError("local_cache.stats() did not return a JSON object")
    result: dict[str, int] = {}
    for counter in SQL_COUNTERS:
        value = parsed.get(counter)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RuntimeError(
                f"local_cache.stats() has invalid {counter}: {value!r}"
            )
        result[counter] = value
    return result


def counter_delta(
    before: Mapping[str, int], after: Mapping[str, int]
) -> dict[str, int]:
    result: dict[str, int] = {}
    for counter in SQL_COUNTERS:
        if counter not in before or counter not in after:
            raise ValueError(f"counter snapshots lack {counter}")
        delta = after[counter] - before[counter]
        if delta < 0:
            raise ValueError(f"counter {counter} moved backwards")
        result[f"{counter}_during_measurement"] = delta
    return result


def summarize_runs(runs: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    if not runs:
        raise ValueError("at least one run is required")
    rates = [float(run["operations_per_second"]) for run in runs]
    if any(not math.isfinite(rate) or rate < 0 for rate in rates):
        raise ValueError("run throughput must be finite and non-negative")
    median = statistics.median(rates)
    mean = statistics.fmean(rates)
    deviation = statistics.pstdev(rates) if len(rates) > 1 else 0.0
    return {
        "median_operations_per_second": median,
        "mean_operations_per_second": mean,
        "minimum_operations_per_second": min(rates),
        "maximum_operations_per_second": max(rates),
        "coefficient_of_variation_percent": (
            deviation / mean * 100.0 if mean else 0.0
        ),
        "median_batch_latency_average_ms": statistics.median(
            float(run["batch_latency_average_ms"]) for run in runs
        ),
    }


def aggregate_mode(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "runs": [dict(run) for run in runs],
        "summary": summarize_runs(runs),
    }
    for counter in SQL_COUNTERS:
        field = f"{counter}_during_measurement"
        if all(field in run for run in runs):
            result[field] = sum(int(run[field]) for run in runs)
    return result


def discover_server(config: Config) -> dict[str, Any]:
    output = psql(
        config,
        "SELECT current_setting('server_version_num'), "
        "COALESCE((SELECT extversion FROM pg_catalog.pg_extension "
        "WHERE extname = 'pg_local_cache'), ''), "
        "COALESCE(current_setting('pg_local_cache.port', true), ''), "
        "COALESCE(current_setting('pg_local_cache.database', true), ''), "
        "COALESCE(current_setting('pg_local_cache.cache_entries', true), ''), "
        "current_user, pg_catalog.pg_is_in_recovery()",
    )
    fields = output.split("|")
    if len(fields) != 7:
        raise RuntimeError(f"unexpected PostgreSQL discovery output: {output!r}")
    version_num, extension_version, cache_port, cache_database, capacity = fields[:5]
    expected_major = postgres_major_from_environment()
    if int(version_num) // 10_000 != expected_major:
        raise RuntimeError(
            "this pg_local_cache build requires PostgreSQL "
            f"{expected_major}, got {version_num}"
        )
    if not extension_version:
        raise RuntimeError("CREATE EXTENSION pg_local_cache must be run first")
    if cache_port != "0":
        raise RuntimeError(
            "SQL-only benchmark requires pg_local_cache.port=0 "
            f"(actual {cache_port or 'unset'})"
        )
    if cache_database != config.database:
        raise RuntimeError(
            "PGDATABASE must match pg_local_cache.database "
            f"({config.database!r} != {cache_database!r})"
        )
    try:
        cache_capacity = int(capacity)
    except ValueError as error:
        raise RuntimeError(
            f"invalid pg_local_cache.cache_entries value {capacity!r}"
        ) from error
    if config.keys > cache_capacity:
        raise RuntimeError(
            f"benchmark keyspace {config.keys} exceeds cache capacity "
            f"{cache_capacity}"
        )
    if fields[6] == "t":
        raise RuntimeError("SQL-only benchmark requires a writable primary")
    # Calling stats proves that shared_preload_libraries initialized the
    # shared state, rather than merely accepting placeholder GUCs.
    read_stats(config)
    return {
        "server_version_num": int(version_num),
        "extension_version": extension_version,
        "pg_local_cache_port": int(cache_port),
        "pg_local_cache_database": cache_database,
        "cache_capacity": cache_capacity,
        "admin_user": fields[5],
        "in_recovery": fields[6] == "t",
    }


def read_comparable_settings(
    config: Config, *, target: str
) -> dict[str, str]:
    expressions = ", ".join(
        f"current_setting({sql_literal(name)})"
        for name in COMPARABLE_SERVER_SETTINGS
    )
    output = psql(config, f"SELECT {expressions}", target=target)
    fields = output.split("|")
    if len(fields) != len(COMPARABLE_SERVER_SETTINGS):
        raise RuntimeError(
            f"unexpected {target} PostgreSQL settings output: {output!r}"
        )
    return dict(zip(COMPARABLE_SERVER_SETTINGS, fields, strict=True))


def discover_stock_server(
    config: Config,
    *,
    mapped_server: Mapping[str, Any],
    mapped_settings: Mapping[str, str],
) -> dict[str, Any]:
    output = psql(
        config,
        "SELECT current_setting('server_version_num'), "
        "EXISTS (SELECT 1 FROM pg_catalog.pg_extension "
        "WHERE extname = 'pg_local_cache'), "
        "current_setting('shared_preload_libraries'), current_user, "
        "pg_catalog.pg_is_in_recovery()",
        target="stock",
    )
    fields = output.split("|")
    if len(fields) != 5:
        raise RuntimeError(
            f"unexpected stock PostgreSQL discovery output: {output!r}"
        )
    version_num = int(fields[0])
    if version_num != mapped_server.get("server_version_num"):
        raise RuntimeError(
            "stock and mapped PostgreSQL must have the exact same server "
            f"version ({version_num} != "
            f"{mapped_server.get('server_version_num')})"
        )
    if fields[1] != "f":
        raise RuntimeError(
            "stock PostgreSQL must not have pg_local_cache installed"
        )
    preloaded = {
        entry.strip().strip('"')
        for entry in fields[2].split(",")
        if entry.strip()
    }
    if any(entry.rsplit("/", 1)[-1] == "pg_local_cache" for entry in preloaded):
        raise RuntimeError(
            "stock PostgreSQL must not preload pg_local_cache"
        )
    if fields[4] == "t":
        raise RuntimeError("stock benchmark requires a writable primary")
    stock_settings = read_comparable_settings(config, target="stock")
    differences = {
        name: {
            "mapped": mapped_settings.get(name),
            "stock": stock_settings.get(name),
        }
        for name in COMPARABLE_SERVER_SETTINGS
        if mapped_settings.get(name) != stock_settings.get(name)
    }
    if differences:
        raise RuntimeError(
            "stock and mapped PostgreSQL query settings differ: "
            + json.dumps(differences, sort_keys=True)
        )
    return {
        "server_version_num": version_num,
        "extension_installed": False,
        "pg_local_cache_preloaded": False,
        "shared_preload_libraries": fields[2],
        "admin_user": fields[3],
        "in_recovery": False,
        "comparison_settings": stock_settings,
        "settings_match_mapped_server": True,
    }


def preflight_names(config: Config, *, target: str = "mapped") -> None:
    predicates = [
        "EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = "
        f"{sql_literal(config.app_user)})",
        "EXISTS (SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = "
        f"{sql_literal(config.schema)})",
    ]
    if target == "mapped":
        predicates.append(
            "EXISTS (SELECT 1 FROM local_cache.mapping WHERE namespace = "
            f"{sql_literal(config.namespace)})"
        )
    output = psql(config, "SELECT " + ", ".join(predicates), target=target)
    expected = "|".join("f" for _ in predicates)
    if output != expected:
        raise RuntimeError(
            f"benchmark run ID collides on the {target} server; "
            "choose another PGLC_SQL_ONLY_BENCH_RUN_ID"
        )


def setup_sql(config: Config, *, database: str, attach: bool) -> str:
    role = sql_identifier(config.app_user)
    schema = sql_identifier(config.schema)
    table = config.qualified_table
    statements = (
        "BEGIN;"
        "DO $pglc$ BEGIN EXECUTE pg_catalog.format("
        "'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB "
        "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS', "
        f"{sql_literal(config.app_user)}, {sql_literal(config.app_password)}"
        "); END $pglc$;"
        f"GRANT CONNECT ON DATABASE {sql_identifier(database)} TO {role};"
        f"CREATE SCHEMA {schema};"
        f"CREATE TABLE {table} ("
        "tenant_id bigint NOT NULL, id bigint NOT NULL, payload text NOT NULL, "
        "amount numeric(18,2) NOT NULL, enabled boolean NOT NULL, "
        "metadata jsonb NOT NULL, note text, "
        "PRIMARY KEY (id));"
        f"INSERT INTO {table} "
        f"SELECT {TENANT_ID}, g, p.payload, "
        "(g % 10000)::numeric / 100, (g % 2 = 0), "
        "pg_catalog.jsonb_build_object('bucket', g % 16, "
        "'active', g % 2 = 0), "
        "CASE WHEN g % 3 = 0 THEN NULL ELSE 'note-' || g::text END "
        f"FROM pg_catalog.generate_series(1, {config.keys}) AS g "
        "CROSS JOIN LATERAL (SELECT pg_catalog.left("
        "pg_catalog.string_agg(pg_catalog.md5(g::text || ':' || chunk::text), "
        "'' ORDER BY chunk), "
        f"{config.payload_bytes}) AS payload FROM pg_catalog.generate_series("
        f"1, ({config.payload_bytes} + 31) / 32) AS chunk) AS p;"
        f"GRANT USAGE ON SCHEMA {schema} TO {role};"
        f"GRANT SELECT ON TABLE {table} TO {role};"
        f"ANALYZE {table};"
    )
    if attach:
        statements += (
            "SELECT local_cache.attach_table("
            f"{sql_literal(config.schema + '.' + config.table)}::regclass, false, "
            f"{sql_literal(config.namespace)})::text;"
            f"GRANT USAGE ON SCHEMA local_cache TO {role};"
            "GRANT EXECUTE ON FUNCTION local_cache.get(regclass, text[]) TO "
            f"{role};"
            "GRANT EXECUTE ON FUNCTION local_cache.get(regclass, anyelement) TO "
            f"{role};"
            "GRANT EXECUTE ON FUNCTION local_cache.mget(regclass, anyarray) TO "
            f"{role};"
        )
    return statements + "COMMIT;"


def setup_objects(config: Config) -> dict[str, Any]:
    # Feed the role password over stdin; never expose it in the psql process
    # command line where another local user could read it through /proc.
    try:
        output = psql(
            config,
            setup_sql(config, database=config.database, attach=True),
            script=True,
        )
    except RuntimeError as error:
        # PostgreSQL may include dynamic SQL in PL/pgSQL CONTEXT.  Ensure an
        # installation error cannot copy the disposable login secret into a
        # console log or failure artifact.
        raise RuntimeError(
            str(error).replace(config.app_password, "<redacted>")
        ) from None
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("attach_table did not return mapping metadata")
    try:
        mapping = json.loads(lines[-1])
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"attach_table returned invalid JSON: {lines[-1]!r}"
        ) from error
    if not isinstance(mapping, dict) or mapping.get("whole_row") is not True:
        raise RuntimeError(f"attach_table did not create a whole-row mapping: {mapping!r}")
    return mapping


def setup_stock_objects(config: Config) -> None:
    try:
        psql(
            config,
            setup_sql(
                config,
                database=config.stock_database,
                attach=False,
            ),
            script=True,
            target="stock",
        )
    except RuntimeError as error:
        raise RuntimeError(
            str(error).replace(config.app_password, "<redacted>")
        ) from None


def cleanup_objects(config: Config, *, target: str = "mapped") -> None:
    cleanup_sql = ""
    if target == "mapped":
        cleanup_sql += (
            "DO $pglc$ BEGIN "
            f"IF pg_catalog.to_regclass({sql_literal(config.schema + '.' + config.table)}) "
            "IS NOT NULL AND pg_catalog.to_regprocedure("
            "'local_cache.detach_table(regclass)') IS NOT NULL THEN "
            "EXECUTE pg_catalog.format('SELECT local_cache.detach_table(%L::regclass)', "
            f"{sql_literal(config.schema + '.' + config.table)}); "
            "END IF; END $pglc$;"
        )
    cleanup_sql += (
        f"DROP SCHEMA IF EXISTS {sql_identifier(config.schema)} CASCADE;"
        "DO $pglc$ BEGIN IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles "
        f"WHERE rolname = {sql_literal(config.app_user)}) THEN "
        "EXECUTE pg_catalog.format('DROP OWNED BY %I', "
        f"{sql_literal(config.app_user)});"
        "EXECUTE pg_catalog.format('DROP ROLE %I', "
        f"{sql_literal(config.app_user)});"
        "END IF; END $pglc$;"
    )
    psql(config, cleanup_sql, target=target)


def validate_application_role(
    config: Config, *, target: str = "mapped"
) -> dict[str, Any]:
    privilege_expression = (
        "pg_catalog.has_schema_privilege(current_user, 'local_cache', 'USAGE')"
        if target == "mapped"
        else "false"
    )
    output = psql(
        config,
        "SELECT current_user, rolsuper, rolcanlogin, rolinherit, "
        "rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, "
        f"{privilege_expression} "
        "FROM pg_catalog.pg_roles WHERE rolname = current_user",
        application=True,
        target=target,
    )
    fields = output.split("|")
    expected = [
        config.app_user,
        "f",
        "t",
        "f",
        "f",
        "f",
        "f",
        "f",
        "t" if target == "mapped" else "f",
    ]
    if fields != expected:
        raise RuntimeError(
            f"benchmark application role is not isolated NOSUPERUSER: {fields!r}"
        )
    return {
        "name": config.app_user,
        "server": target,
        "login": True,
        "superuser": False,
        "inherit": False,
        "createdb": False,
        "createrole": False,
        "replication": False,
        "bypass_rls": False,
        "local_cache_schema_usage": target == "mapped",
    }


def explain_and_sample(config: Config) -> dict[str, Any]:
    cached_query = config.lookup_query.replace(
        ":tenant", str(TENANT_ID)
    ).replace(":key", "1")
    direct_query = config.direct_lookup_query.replace(
        ":tenant", str(TENANT_ID)
    ).replace(":key", "1")
    direct_plan = psql(
        config,
        "SET pg_local_cache.sql_cache = off;"
        f"EXPLAIN (COSTS OFF) {direct_query}",
        application=True,
    )
    direct_value = psql(
        config,
        "SET pg_local_cache.sql_cache = off;" + direct_query,
        application=True,
    )
    cached_plan = psql(
        config,
        "SET pg_local_cache.sql_cache = on;"
        f"EXPLAIN (COSTS OFF) {cached_query}",
        application=True,
    )
    cached_value = psql(
        config,
        "SET pg_local_cache.sql_cache = on;" + cached_query,
        application=True,
    )
    stock_plan = psql(
        config,
        f"EXPLAIN (COSTS OFF) {direct_query}",
        application=True,
        target="stock",
    )
    stock_value = psql(
        config,
        direct_query,
        application=True,
        target="stock",
    )
    if CUSTOM_SCAN_NAME in direct_plan:
        raise RuntimeError("cache-off plan unexpectedly contains pg_local_cache CustomScan")
    if CUSTOM_SCAN_NAME in cached_plan:
        raise RuntimeError("SQL KV lookup unexpectedly contains pg_local_cache CustomScan")
    if CUSTOM_SCAN_NAME in stock_plan:
        raise RuntimeError("stock PostgreSQL plan unexpectedly contains CustomScan")
    if not direct_value or cached_value != direct_value or stock_value != direct_value:
        raise RuntimeError(
            "stock, cached, and direct SQL KV reads returned different rows"
        )
    return {
        "query": config.lookup_query,
        "stock_and_direct_query": config.direct_lookup_query,
        "validated_key": {"tenant_id": TENANT_ID, "id": 1},
        "direct_plan": direct_plan,
        "cached_plan": cached_plan,
        "stock_plan": stock_plan,
        "stock_mapped_and_cached_rows_equal": True,
        "direct_and_cached_rows_equal": True,
        "sample_output_bytes": len(cached_value.encode("utf-8")),
    }


def invalidate_namespace(config: Config) -> int:
    raw = psql(
        config,
        "SELECT local_cache.invalidate("
        f"{sql_literal(config.namespace)})",
    )
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"invalidate returned a non-integer: {raw!r}") from error
    if value < 0:
        raise RuntimeError(f"invalidate returned a negative count: {value}")
    return value


def validate_expected_deltas(
    actual: Mapping[str, int], expected: Mapping[str, int], context: str
) -> None:
    normalized_expected = {
        f"{counter}_during_measurement": int(expected.get(counter, 0))
        for counter in SQL_COUNTERS
    }
    if dict(actual) != normalized_expected:
        raise RuntimeError(
            f"{context} SQL-cache accounting mismatch: expected "
            f"{normalized_expected!r}, got {dict(actual)!r}"
        )


def cold_miss_fill_hit_proof(config: Config) -> dict[str, Any]:
    invalidated = invalidate_namespace(config)
    before = read_stats(config)
    statement = config.lookup_query.replace(":key", "$1")
    output = psql(
        config,
        "SET pg_local_cache.sql_cache = on;\n"
        "SET plan_cache_mode = force_generic_plan;\n"
        f"PREPARE pglc_cold(bigint) AS {statement}\n"
        "EXECUTE pglc_cold(1);\n"
        "EXECUTE pglc_cold(1);\n"
        "DEALLOCATE pglc_cold;\n",
        application=True,
        script=True,
    )
    rows = output.splitlines()
    if len(rows) != 2 or not rows[0] or rows[0] != rows[1]:
        raise RuntimeError(f"cold-fill probe returned unexpected rows: {rows!r}")
    deltas = counter_delta(before, read_stats(config))
    validate_expected_deltas(
        deltas,
        {"sql_cache_hits": 1, "sql_cache_misses": 1, "sql_cache_fills": 1},
        "cold miss/fill/hit probe",
    )
    return {
        "status": "PASS",
        "invalidated_entries": invalidated,
        "query": statement,
        "executions": 2,
        "validated_key": {"tenant_id": TENANT_ID, "id": 1},
        **deltas,
    }


def warm_all_keys(config: Config) -> dict[str, Any]:
    invalidated = invalidate_namespace(config)
    statement = config.lookup_query.replace(":key", "$1")
    lines = [
        "SET pg_local_cache.sql_cache = on;",
        "SET plan_cache_mode = force_generic_plan;",
        f"PREPARE pglc_warm(bigint) AS {statement}",
    ]
    lines.extend(
        f"EXECUTE pglc_warm({key});"
        for key in range(1, config.keys + 1)
    )
    lines.append("DEALLOCATE pglc_warm;")
    before = read_stats(config)
    psql(
        config,
        "\n".join(lines) + "\n",
        application=True,
        script=True,
        discard_rows=True,
    )
    deltas = counter_delta(before, read_stats(config))
    validate_expected_deltas(
        deltas,
        {
            "sql_cache_misses": config.keys,
            "sql_cache_fills": config.keys,
        },
        "complete keyspace warm pass",
    )
    return {
        "status": "PASS",
        "invalidated_entries": invalidated,
        "keys_filled": config.keys,
        **deltas,
    }


def sentinel_row_integrity_check(config: Config) -> dict[str, Any]:
    aggregate_query = (
        "SELECT count(*), min(id), max(id), count(DISTINCT id), "
        f"bool_and(tenant_id = {TENANT_ID}) FROM {config.qualified_table}"
    )
    aggregate = psql(
        config,
        "SET pg_local_cache.sql_cache = off;" + aggregate_query,
        application=True,
    )
    stock_aggregate = psql(
        config,
        aggregate_query,
        application=True,
        target="stock",
    )
    expected_aggregate = f"{config.keys}|1|{config.keys}|{config.keys}|t"
    if aggregate != expected_aggregate or stock_aggregate != expected_aggregate:
        raise RuntimeError(
            "source table row-count/key-range proof failed: expected "
            f"{expected_aggregate!r}, got mapped={aggregate!r}, "
            f"stock={stock_aggregate!r}"
        )

    sentinel_keys = sorted({1, (config.keys + 1) // 2, config.keys})
    def execute(cache_enabled: bool) -> tuple[list[str], dict[str, int]]:
        mode = "on" if cache_enabled else "off"
        query = config.lookup_query if cache_enabled else config.direct_lookup_query
        statement = query.replace(":key", "$1")
        lines = [
            f"SET pg_local_cache.sql_cache = {mode};",
            "SET plan_cache_mode = force_generic_plan;",
            f"PREPARE pglc_integrity(bigint) AS {statement}",
        ]
        lines.extend(
            f"EXECUTE pglc_integrity({key});"
            for key in sentinel_keys
        )
        lines.append("DEALLOCATE pglc_integrity;")
        before = read_stats(config)
        output = psql(
            config,
            "\n".join(lines) + "\n",
            application=True,
            script=True,
        )
        deltas = counter_delta(before, read_stats(config))
        validate_expected_deltas(
            deltas,
            {"sql_cache_hits": len(sentinel_keys)} if cache_enabled else {},
            f"{'cached' if cache_enabled else 'direct'} sentinel check",
        )
        rows = output.splitlines()
        if len(rows) != len(sentinel_keys):
            raise RuntimeError(
                f"sentinel check expected {len(sentinel_keys)} rows, got {len(rows)}"
            )
        returned_keys: list[int] = []
        for row in rows:
            try:
                parsed = json.loads(row)
            except json.JSONDecodeError as error:
                raise RuntimeError(
                    f"sentinel check returned malformed JSON: {row!r}"
                ) from error
            if not isinstance(parsed, dict) or parsed.get("tenant_id") != TENANT_ID:
                raise RuntimeError(f"sentinel check returned malformed row: {row!r}")
            try:
                returned_keys.append(int(parsed["id"]))
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError(
                    f"sentinel check returned a non-integer key: {row!r}"
                ) from error
        if returned_keys != sentinel_keys:
            raise RuntimeError(
                f"sentinel check returned keys {returned_keys!r}, "
                f"expected {sentinel_keys!r}"
            )
        return rows, deltas

    direct_rows, direct_deltas = execute(False)
    cached_rows, cached_deltas = execute(True)
    statement = config.direct_lookup_query.replace(":key", "$1")
    stock_lines = [
        "SET plan_cache_mode = force_generic_plan;",
        f"PREPARE pglc_integrity(bigint) AS {statement}",
    ]
    stock_lines.extend(
        f"EXECUTE pglc_integrity({key});"
        for key in sentinel_keys
    )
    stock_lines.append("DEALLOCATE pglc_integrity;")
    stock_rows = psql(
        config,
        "\n".join(stock_lines) + "\n",
        application=True,
        script=True,
        target="stock",
    ).splitlines()
    if cached_rows != direct_rows or stock_rows != direct_rows:
        raise RuntimeError(
            "stock, cached, and direct sentinel rows are not identical"
        )
    digest = hashlib.sha256(
        ("\n".join(direct_rows) + "\n").encode("utf-8")
    ).hexdigest()
    return {
        "status": "PASS",
        "source_row_count": config.keys,
        "source_min_id": 1,
        "source_max_id": config.keys,
        "source_distinct_ids": config.keys,
        "sentinel_keys": sentinel_keys,
        "sentinel_rows": len(direct_rows),
        "stock_mapped_and_cached_rows_equal": True,
        "direct_and_cached_rows_equal": True,
        "sentinel_rows_sha256": digest,
        "direct_counter_deltas": direct_deltas,
        "cached_counter_deltas": cached_deltas,
    }


def run_pgbench_once(
    config: Config,
    script_path: Path,
    *,
    protocol: str,
    benchmark_mode: str,
    duration: float,
    seed: int,
    operations_per_batch: int | None = None,
    log_prefix: Path | None = None,
    sampling_rate: float | None = None,
) -> dict[str, Any]:
    if protocol not in PROTOCOLS:
        raise ValueError(f"unsupported pgbench protocol {protocol!r}")
    if benchmark_mode not in BENCHMARK_MODES:
        raise ValueError(f"unsupported benchmark mode {benchmark_mode!r}")
    target = "stock" if benchmark_mode == "stock" else "mapped"
    environment = connection_environment(
        config, application=True, target=target
    )
    if benchmark_mode == "stock":
        environment["PGOPTIONS"] = "-c plan_cache_mode=force_generic_plan"
        host = config.stock_host
        port = config.stock_port
        database = config.stock_database
        cache_enabled: bool | None = None
    else:
        cache_enabled = benchmark_mode == "cached"
        mode = "on" if cache_enabled else "off"
        environment["PGOPTIONS"] = (
            f"-c pg_local_cache.sql_cache={mode} "
            "-c plan_cache_mode=force_generic_plan"
        )
        host = config.host
        port = config.port
        database = config.database
    effective_duration = max(1, math.ceil(duration))
    arguments = [
        "pgbench",
        "-h",
        host,
        "-p",
        str(port),
        "-U",
        config.app_user,
        "-d",
        database,
        "-n",
        "-M",
        protocol,
        "-c",
        str(config.concurrency),
        "-j",
        str(config.jobs),
        "-T",
        str(effective_duration),
        "--random-seed",
        str(seed),
        "-f",
        str(script_path),
    ]
    if log_prefix is not None:
        if sampling_rate is None:
            raise ValueError("latency logging requires a sampling rate")
        arguments.extend(
            (
                "--log",
                "--log-prefix",
                str(log_prefix),
                "--sampling-rate",
                format(sampling_rate, ".12g"),
            )
        )
    elif sampling_rate is not None:
        raise ValueError("sampling rate requires latency logging")
    output = run_checked(
        arguments,
        environment=environment,
        timeout=max(90.0, math.ceil(duration) + 60.0),
    )
    result = parse_pgbench_output(
        output,
        config.pipeline
        if operations_per_batch is None
        else operations_per_batch,
    )
    result.update(
        {
            "query_protocol": protocol,
            "benchmark_mode": benchmark_mode,
            "server_target": target,
            "cache_enabled": cache_enabled,
            "random_seed": seed,
            "requested_duration_seconds": duration,
            "effective_duration_seconds": effective_duration,
        }
    )
    return result


def run_with_counter_accounting(
    config: Config,
    script_path: Path,
    *,
    protocol: str,
    benchmark_mode: str,
    duration: float,
    seed: int,
    operations_per_batch: int | None = None,
    log_prefix: Path | None = None,
    sampling_rate: float | None = None,
) -> dict[str, Any]:
    before = read_stats(config) if benchmark_mode != "stock" else None
    run = run_pgbench_once(
        config,
        script_path,
        protocol=protocol,
        benchmark_mode=benchmark_mode,
        duration=duration,
        seed=seed,
        operations_per_batch=operations_per_batch,
        log_prefix=log_prefix,
        sampling_rate=sampling_rate,
    )
    if before is None:
        run["cache_counter_accounting"] = "not applicable: stock server"
    else:
        run.update(counter_delta(before, read_stats(config)))
    return run


def mode_order(repetition: int) -> tuple[str, str, str]:
    orders = (
        ("stock", "direct", "cached"),
        ("cached", "stock", "direct"),
        ("direct", "cached", "stock"),
    )
    return orders[repetition % len(orders)]


def measure_latency_protocol(
    config: Config, script_paths: Mapping[str, Path], protocol: str
) -> dict[str, Any]:
    per_mode_runs: dict[str, list[dict[str, Any]]] = {
        mode: [] for mode in BENCHMARK_MODES
    }
    per_mode_samples: dict[str, list[float]] = {
        mode: [] for mode in BENCHMARK_MODES
    }
    protocol_offset = PROTOCOLS.index(protocol)
    with tempfile.TemporaryDirectory(prefix="pglc_sql_latency_") as directory:
        log_directory = Path(directory)
        for repetition in range(config.repetitions):
            order = mode_order(protocol_offset + repetition)
            for order_index, benchmark_mode in enumerate(order, start=1):
                prefix = log_directory / (
                    f"{protocol}_{repetition + 1}_{benchmark_mode}"
                )
                seed = 81_000 + protocol_offset * 1_000 + repetition
                run = run_with_counter_accounting(
                    config,
                    script_paths[benchmark_mode],
                    protocol=protocol,
                    benchmark_mode=benchmark_mode,
                    duration=config.latency_duration,
                    seed=seed,
                    operations_per_batch=1,
                    log_prefix=prefix,
                    sampling_rate=config.latency_sample_rate,
                )
                run["repetition"] = repetition + 1
                paths = list(log_directory.glob(prefix.name + ".*"))
                samples = read_latency_samples(paths)
                sample_offset = len(per_mode_samples[benchmark_mode])
                per_mode_samples[benchmark_mode].extend(samples)
                distribution = summarize_latency_samples(samples)
                distribution.update(
                    {
                        "duration_seconds": config.latency_duration,
                        "sampling_rate": config.latency_sample_rate,
                        "protocol": protocol,
                        "benchmark_mode": benchmark_mode,
                        "pipeline_depth": 1,
                    }
                )
                per_mode_runs[benchmark_mode].append(
                    {
                        "run": run,
                        "distribution": distribution,
                        "repetition": repetition + 1,
                        "measurement_order": repetition * 3 + order_index,
                        "order_within_repetition": order_index,
                        "raw_sample_offset": sample_offset,
                        "raw_sample_count": len(samples),
                    }
                )

    results: dict[str, Any] = {}
    for benchmark_mode in BENCHMARK_MODES:
        raw_samples = per_mode_samples[benchmark_mode]
        runs = per_mode_runs[benchmark_mode]
        distribution = summarize_latency_samples(raw_samples)
        distribution.update(
            {
                "duration_seconds_per_run": config.latency_duration,
                "repetitions": config.repetitions,
                "sampling_rate": config.latency_sample_rate,
                "protocol": protocol,
                "benchmark_mode": benchmark_mode,
                "pipeline_depth": 1,
                "aggregation": "percentiles recomputed from all raw samples",
            }
        )
        enough_per_run = all(
            run["distribution"]["sample_count"] >= config.latency_min_samples
            for run in runs
        )
        enough_aggregate = distribution["sample_count"] >= (
            config.latency_min_samples * config.repetitions
        )
        within_limit = (
            config.latency_max_p99_ms is None
            or distribution["p99_ms"] <= config.latency_max_p99_ms
        )
        if not enough_per_run or not enough_aggregate or not within_limit:
            gate_status = "FAIL"
        elif config.latency_max_p99_ms is None:
            gate_status = "MEASURED"
        else:
            gate_status = "PASS"
        results[benchmark_mode] = {
            "runs": runs,
            "raw_samples_ms": raw_samples,
            "distribution": distribution,
            "gate": {
                "minimum_samples_per_run": config.latency_min_samples,
                "minimum_aggregate_samples": (
                    config.latency_min_samples * config.repetitions
                ),
                "maximum_p99_ms": config.latency_max_p99_ms,
                "status": gate_status,
            },
        }
    return results


def relative_throughput_gate(
    *,
    cached_to_direct: float,
    cached_to_stock: float,
    minimum_cached_to_direct: float | None,
    minimum_cached_to_stock: float | None,
) -> dict[str, Any]:
    checks = {
        "cached_to_direct": {
            "measured_ratio": cached_to_direct,
            "minimum_ratio": minimum_cached_to_direct,
        },
        "cached_to_stock": {
            "measured_ratio": cached_to_stock,
            "minimum_ratio": minimum_cached_to_stock,
        },
    }
    configured = False
    failed = False
    for check in checks.values():
        minimum = check["minimum_ratio"]
        measured = float(check["measured_ratio"])
        if minimum is None:
            check["status"] = "MEASURED"
            continue
        configured = True
        if math.isfinite(measured) and measured >= float(minimum):
            check["status"] = "PASS"
        else:
            check["status"] = "FAIL"
            failed = True
    return {
        **checks,
        "status": "FAIL" if failed else ("PASS" if configured else "MEASURED"),
    }


def measure_protocol(
    config: Config,
    script_paths: Mapping[str, Path],
    latency_script_paths: Mapping[str, Path],
    protocol: str,
    minimum_ops: float | None,
) -> dict[str, Any]:
    if protocol not in PROTOCOLS:
        raise ValueError(f"unsupported protocol {protocol!r}")
    if config.warmup_seconds > 0:
        for benchmark_mode in BENCHMARK_MODES:
            warmup = run_pgbench_once(
                config,
                script_paths[benchmark_mode],
                protocol=protocol,
                benchmark_mode=benchmark_mode,
                duration=config.warmup_seconds,
                seed=70_000,
            )
            if warmup["failed_batches"]:
                raise RuntimeError(f"{protocol} warmup had failed batches")

    runs: dict[str, list[dict[str, Any]]] = {
        mode: [] for mode in BENCHMARK_MODES
    }
    for repetition in range(config.repetitions):
        for benchmark_mode in mode_order(repetition):
            run = run_with_counter_accounting(
                config,
                script_paths[benchmark_mode],
                protocol=protocol,
                benchmark_mode=benchmark_mode,
                duration=config.duration,
                seed=71_000 + repetition,
            )
            run["repetition"] = repetition + 1
            runs[benchmark_mode].append(run)

    stock = aggregate_mode(runs["stock"])
    direct = aggregate_mode(runs["direct"])
    cached = aggregate_mode(runs["cached"])
    latency = measure_latency_protocol(config, latency_script_paths, protocol)
    stock["latency"] = latency["stock"]
    direct["latency"] = latency["direct"]
    cached["latency"] = latency["cached"]
    stock_median = stock["summary"]["median_operations_per_second"]
    direct_median = direct["summary"]["median_operations_per_second"]
    cached_median = cached["summary"]["median_operations_per_second"]
    cached_to_stock = cached_median / stock_median if stock_median else 0.0
    cached_to_direct = cached_median / direct_median if direct_median else 0.0
    latency_statuses = {
        latency[mode]["gate"]["status"] for mode in BENCHMARK_MODES
    }
    if "FAIL" in latency_statuses:
        latency_gate_status = "FAIL"
    elif config.latency_max_p99_ms is None:
        latency_gate_status = "MEASURED"
    else:
        latency_gate_status = "PASS"
    return {
        "status": "MEASURED",
        "query_protocol": protocol,
        "protocol_semantics": (
            "extended protocol with server-side prepared statement reuse"
            if protocol == "prepared"
            else "unnamed extended protocol; Parse/Bind/Execute per statement"
        ),
        "stock_mode": stock,
        "direct_mode": direct,
        "cached_mode": cached,
        "cached_to_stock_throughput_ratio": cached_to_stock,
        "direct_to_stock_throughput_ratio": (
            direct_median / stock_median if stock_median else 0.0
        ),
        "cached_to_direct_throughput_ratio": cached_to_direct,
        "relative_throughput_gate": relative_throughput_gate(
            cached_to_direct=cached_to_direct,
            cached_to_stock=cached_to_stock,
            minimum_cached_to_direct=config.min_cached_to_direct_ratio,
            minimum_cached_to_stock=config.min_cached_to_stock_ratio,
        ),
        "throughput_gate": {
            "scope": f"{protocol} cached-mode median only",
            "minimum_cached_operations_per_second": minimum_ops,
            "measured_cached_operations_per_second": cached_median,
            "status": (
                "MEASURED"
                if minimum_ops is None
                else (
                    "PASS"
                    if math.isfinite(cached_median)
                    and cached_median >= minimum_ops
                    else "FAIL"
                )
            ),
        },
        "latency_gate": {
            "scope": f"{protocol}, all three modes, single SELECT per transaction",
            "repetitions_per_mode": config.repetitions,
            "minimum_samples_per_run": config.latency_min_samples,
            "minimum_aggregate_samples_per_mode": (
                config.latency_min_samples * config.repetitions
            ),
            "maximum_p99_ms": config.latency_max_p99_ms,
            "status": latency_gate_status,
        },
    }


def profile_workload(config: Config) -> dict[str, Any]:
    return {
        "concurrency": config.concurrency,
        "jobs": config.jobs,
        "throughput_pipeline": config.pipeline,
        "latency_pipeline": 1,
        "duration_seconds": config.duration,
        "warmup_seconds": config.warmup_seconds,
        "latency_duration_seconds": config.latency_duration,
        "latency_sample_rate": config.latency_sample_rate,
        "latency_min_samples": config.latency_min_samples,
        "repetitions": config.repetitions,
        "keys": config.keys,
        "payload_bytes": config.payload_bytes,
    }


def scaling_profile_config(config: Config) -> Config:
    profile = replace(
        config,
        duration=config.scaling_duration,
        warmup_seconds=config.scaling_warmup_seconds,
        latency_duration=config.scaling_latency_duration,
        latency_sample_rate=config.scaling_latency_sample_rate,
        latency_min_samples=config.scaling_latency_min_samples,
        latency_max_p99_ms=None,
        repetitions=config.scaling_repetitions,
        concurrency=SCALING_SECONDARY_CONCURRENCY,
        jobs=min(config.jobs, SCALING_SECONDARY_CONCURRENCY),
        pipeline=SCALING_SECONDARY_PIPELINE,
        min_cached_to_direct_ratio=None,
        min_cached_to_stock_ratio=None,
        scaling_snapshot_enabled=False,
    )
    profile.validate()
    return profile


def measure_scaling_snapshot(
    config: Config, script_directory: Path
) -> dict[str, Any]:
    if not config.scaling_snapshot_enabled:
        return {"status": "DISABLED", "performance_gating": False}

    secondary = scaling_profile_config(config)
    throughput_paths: dict[str, Path] = {}
    latency_paths: dict[str, Path] = {}
    for mode in BENCHMARK_MODES:
        query = (
            secondary.lookup_query
            if mode == "cached"
            else secondary.direct_lookup_query
        )
        throughput_path = script_directory / f"scaling-c4-p8-{mode}-throughput.sql"
        latency_path = script_directory / f"scaling-c4-p8-{mode}-latency.sql"
        throughput_path.write_text(
            lookup_script(secondary, cached=mode == "cached"), encoding="utf-8"
        )
        latency_path.write_text(
            latency_lookup_script(secondary, query), encoding="utf-8"
        )
        throughput_paths[mode] = throughput_path
        latency_paths[mode] = latency_path
    protocols = {
        protocol: measure_protocol(
            secondary,
            throughput_paths,
            latency_paths,
            protocol,
            None,
        )
        for protocol in PROTOCOLS
    }
    return {
        "status": "MEASURED",
        "performance_gating": False,
        "measurement_order": [SCALING_PRIMARY_PROFILE, SCALING_SECONDARY_PROFILE],
        "profiles": {
            SCALING_SECONDARY_PROFILE: {
                "source": "embedded",
                "workload": profile_workload(secondary),
                "protocols": protocols,
            },
            SCALING_PRIMARY_PROFILE: {
                "source": "primary_strict_profile",
                "source_path": "$.protocols",
                "workload": profile_workload(config),
            },
        },
    }


def scaling_profile_protocols(
    report: Mapping[str, Any], profile_name: str
) -> Mapping[str, Any]:
    profile = report["scaling_snapshot"]["profiles"][profile_name]
    if profile.get("source") == "primary_strict_profile":
        return report["protocols"]
    return profile["protocols"]


def validate_cold_proof(proof: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    expected = {
        "sql_cache_hits_during_measurement": 1,
        "sql_cache_misses_during_measurement": 1,
        "sql_cache_fills_during_measurement": 1,
        "sql_cache_bypasses_during_measurement": 0,
    }
    if proof.get("status") != "PASS":
        failures.append("cold SQL miss/fill/hit proof did not pass")
    for field, wanted in expected.items():
        if proof.get(field) != wanted:
            failures.append(f"cold proof {field} is not exactly {wanted}")
    return failures


def validate_warm_proof(
    proof: Mapping[str, Any], expected_keys: object
) -> list[str]:
    failures: list[str] = []
    if not isinstance(expected_keys, int) or isinstance(expected_keys, bool):
        return ["warm proof expected key count is invalid"]
    expected = {
        "sql_cache_hits_during_measurement": 0,
        "sql_cache_misses_during_measurement": expected_keys,
        "sql_cache_fills_during_measurement": expected_keys,
        "sql_cache_bypasses_during_measurement": 0,
    }
    if proof.get("status") != "PASS":
        failures.append("complete keyspace warm proof did not pass")
    if proof.get("keys_filled") != expected_keys:
        failures.append("complete keyspace warm proof filled the wrong key count")
    for field, wanted in expected.items():
        if proof.get(field) != wanted:
            failures.append(f"warm proof {field} is not exactly {wanted}")
    return failures


def validate_timed_run(
    run: Mapping[str, Any],
    *,
    context: str,
    protocol: str,
    benchmark_mode: str,
    per_operation: bool = False,
) -> list[str]:
    failures: list[str] = []
    expected_cache = CACHE_ENABLED_BY_MODE[benchmark_mode]
    if run.get("query_protocol") != protocol:
        failures.append(f"{context} has the wrong protocol")
    if run.get("benchmark_mode") != benchmark_mode:
        failures.append(f"{context} has the wrong mode")
    if run.get("cache_enabled") is not expected_cache:
        failures.append(f"{context} has the wrong cache setting")
    if run.get("failed_batches") != 0:
        failures.append(f"{context} has failed batches")
    if per_operation and run.get("operations_per_batch") != 1:
        failures.append(f"{context} is not per operation")
    counter_fields = {
        f"{counter}_during_measurement" for counter in SQL_COUNTERS
    }
    if benchmark_mode == "stock":
        if run.get("server_target") != "stock" or counter_fields & run.keys():
            failures.append(f"{context} is not a clean stock-server run")
        return failures
    expected_hits = (
        run.get("successful_operations") if expected_cache else 0
    )
    if run.get("sql_cache_hits_during_measurement") != expected_hits:
        failures.append(f"{context} has non-exact hit accounting")
    for counter in ("sql_cache_misses", "sql_cache_fills", "sql_cache_bypasses"):
        if run.get(f"{counter}_during_measurement") != 0:
            failures.append(f"{context} touched {counter}")
    return failures


def validate_latency_distribution(
    distribution: object, *, context: str
) -> tuple[list[str], int | None, float | None]:
    if not isinstance(distribution, Mapping):
        return [f"{context} distribution is missing"], None, None
    failures: list[str] = []
    try:
        values = [
            float(distribution[name])
            for name in (
                "minimum_ms", "mean_ms", "p50_ms", "p95_ms",
                "p99_ms", "maximum_ms",
            )
        ]
        samples_value = distribution["sample_count"]
        if not isinstance(samples_value, int) or isinstance(samples_value, bool):
            raise TypeError("sample_count must be an integer")
        samples = samples_value
    except (KeyError, OverflowError, TypeError, ValueError):
        return [f"{context} values are invalid"], None, None
    minimum, mean, p50, p95, p99, maximum = values
    valid = (
        distribution.get("pipeline_depth") == 1
        and samples > 0
        and all(math.isfinite(value) and value >= 0 for value in values)
        and minimum <= p50 <= p95 <= p99 <= maximum
        and minimum <= mean <= maximum
    )
    if not valid:
        failures.append(f"{context} values are invalid")
    return failures, samples, p99


def latency_distribution_matches_samples(
    distribution: object, samples: Sequence[float]
) -> bool:
    if not isinstance(distribution, Mapping):
        return False
    try:
        expected = summarize_latency_samples(samples)
        if distribution.get("sample_count") != expected["sample_count"]:
            return False
        for field in (
            "minimum_ms",
            "mean_ms",
            "p50_ms",
            "p95_ms",
            "p99_ms",
            "maximum_ms",
        ):
            if not math.isclose(
                float(distribution[field]),
                float(expected[field]),
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                return False
    except (KeyError, OverflowError, TypeError, ValueError):
        return False
    return True


def is_finite_nonnegative_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(parsed) and parsed >= 0


def validate_latency(
    latency: object,
    *,
    context: str,
    protocol: str,
    benchmark_mode: str,
    expected_repetitions: int,
) -> list[str]:
    if not isinstance(latency, Mapping):
        return [f"{context} latency is missing"]
    failures, samples, p99 = validate_latency_distribution(
        latency.get("distribution"), context=f"{context} latency"
    )
    raw_samples = latency.get("raw_samples_ms")
    raw_samples_valid = not (
        not isinstance(raw_samples, list)
        or samples is None
        or len(raw_samples) != samples
        or any(not is_finite_nonnegative_number(value) for value in raw_samples)
    )
    if not raw_samples_valid:
        failures.append(f"{context} aggregate raw latency samples are invalid")
    elif not latency_distribution_matches_samples(
        latency.get("distribution"), raw_samples
    ):
        failures.append(
            f"{context} aggregate latency distribution does not match raw samples"
        )

    runs = latency.get("runs")
    per_run_samples: list[int] = []
    if not isinstance(runs, list) or len(runs) != expected_repetitions:
        failures.append(f"{context} latency repetitions are incomplete")
    else:
        expected_offset = 0
        for index, item in enumerate(runs, start=1):
            if not isinstance(item, Mapping):
                failures.append(f"{context} latency run {index} is invalid")
                continue
            run = item.get("run")
            if isinstance(run, Mapping):
                failures.extend(validate_timed_run(
                    run, context=f"{context} latency run {index}",
                    protocol=protocol, benchmark_mode=benchmark_mode,
                    per_operation=True,
                ))
                if run.get("repetition") != index:
                    failures.append(
                        f"{context} latency run {index} has the wrong repetition"
                    )
            else:
                failures.append(f"{context} latency run {index} is missing")
            run_failures, run_samples, _ = validate_latency_distribution(
                item.get("distribution"),
                context=f"{context} latency run {index}",
            )
            failures.extend(run_failures)
            if run_samples is not None:
                per_run_samples.append(run_samples)
            offset = item.get("raw_sample_offset")
            count = item.get("raw_sample_count")
            sample_index_valid = (
                item.get("repetition") == index
                and isinstance(offset, int)
                and not isinstance(offset, bool)
                and offset == expected_offset
                and isinstance(count, int)
                and not isinstance(count, bool)
                and count == run_samples
            )
            if not sample_index_valid:
                failures.append(
                    f"{context} latency run {index} sample index is invalid"
                )
            elif raw_samples_valid and not latency_distribution_matches_samples(
                item.get("distribution"), raw_samples[offset : offset + count]
            ):
                failures.append(
                    f"{context} latency run {index} distribution does not match "
                    "raw samples"
                )
            if run_samples is not None:
                expected_offset += run_samples
        if samples is not None and expected_offset != samples:
            failures.append(f"{context} latency run samples do not aggregate")

    gate = latency.get("gate")
    if not isinstance(gate, Mapping):
        failures.append(f"{context} latency gate is missing")
    else:
        limit = gate.get("maximum_p99_ms")
        minimum_per_run = gate.get("minimum_samples_per_run")
        minimum_aggregate = gate.get("minimum_aggregate_samples")
        limit_valid = limit is None
        parsed_limit: float | None = None
        if limit is not None and not isinstance(limit, bool):
            try:
                parsed_limit = float(limit)
                limit_valid = math.isfinite(parsed_limit) and parsed_limit >= 0
            except (OverflowError, TypeError, ValueError):
                limit_valid = False
        if (
            not isinstance(minimum_per_run, int)
            or isinstance(minimum_per_run, bool)
            or minimum_per_run < 1
            or not isinstance(minimum_aggregate, int)
            or isinstance(minimum_aggregate, bool)
            or minimum_aggregate != minimum_per_run * expected_repetitions
            or not limit_valid
            or samples is None
            or samples < minimum_aggregate
            or len(per_run_samples) != expected_repetitions
            or any(value < minimum_per_run for value in per_run_samples)
            or (
                parsed_limit is not None
                and (p99 is None or p99 > parsed_limit)
            )
        ):
            expected = "FAIL"
        else:
            expected = "MEASURED" if limit is None else "PASS"
        if gate.get("status") != expected or expected == "FAIL":
            failures.append(f"{context} latency gate failed")
    return failures


def validate_protocol_lane(
    lane: Mapping[str, Any], protocol: str, minimum_ops: float | None,
    expected_repetitions: int,
    minimum_cached_to_direct: float | None,
    minimum_cached_to_stock: float | None,
) -> list[str]:
    if lane.get("status") != "MEASURED":
        return [f"{protocol} lane was not measured"]
    failures = [] if lane.get("query_protocol") == protocol else [
        f"{protocol} lane has the wrong protocol label"
    ]
    mode_medians: dict[str, float] = {}
    for benchmark_mode in BENCHMARK_MODES:
        context = f"{protocol} {benchmark_mode}"
        mode = lane.get(f"{benchmark_mode}_mode")
        if not isinstance(mode, Mapping):
            failures.append(f"{context} mode is missing")
            continue
        runs = mode.get("runs")
        if not isinstance(runs, list) or not runs:
            failures.append(f"{context} mode has no runs")
            continue
        for index, run in enumerate(runs, start=1):
            if not isinstance(run, Mapping):
                failures.append(f"{context} run {index} is invalid")
                continue
            failures.extend(validate_timed_run(
                run, context=f"{context} run {index}", protocol=protocol,
                benchmark_mode=benchmark_mode,
            ))
        failures.extend(validate_latency(
            mode.get("latency"), context=context, protocol=protocol,
            benchmark_mode=benchmark_mode,
            expected_repetitions=expected_repetitions,
        ))
        try:
            median = float(mode["summary"]["median_operations_per_second"])
        except (KeyError, OverflowError, TypeError, ValueError):
            failures.append(f"{context} median is missing")
        else:
            if not math.isfinite(median) or median <= 0:
                failures.append(f"{context} median is invalid")
            else:
                mode_medians[benchmark_mode] = median
    cached_median = mode_medians.get("cached")
    if cached_median is not None and minimum_ops is not None:
        if cached_median < minimum_ops:
            failures.append(
                f"{protocol} cached median {cached_median:.0f} ops/s is below "
                f"the independent {minimum_ops:.0f} ops/s gate"
            )
    throughput_gate = lane.get("throughput_gate")
    if not isinstance(throughput_gate, Mapping):
        failures.append(f"{protocol} throughput gate is missing")
    else:
        try:
            measured = float(
                throughput_gate["measured_cached_operations_per_second"]
            )
        except (KeyError, OverflowError, TypeError, ValueError):
            measured = math.nan
        minimum_matches = throughput_gate.get(
            "minimum_cached_operations_per_second"
        ) is None
        if minimum_ops is not None:
            try:
                minimum_matches = math.isclose(
                    float(
                        throughput_gate[
                            "minimum_cached_operations_per_second"
                        ]
                    ),
                    minimum_ops,
                )
            except (KeyError, OverflowError, TypeError, ValueError):
                minimum_matches = False
        expected_gate_status = (
            "MEASURED"
            if minimum_ops is None
            else (
                "PASS"
                if cached_median is not None and cached_median >= minimum_ops
                else "FAIL"
            )
        )
        if (
            cached_median is None
            or not math.isfinite(measured)
            or not math.isclose(measured, cached_median)
            or not minimum_matches
            or throughput_gate.get("status") != expected_gate_status
        ):
            failures.append(f"{protocol} throughput gate is inconsistent")
    relative_gate = lane.get("relative_throughput_gate")
    if not isinstance(relative_gate, Mapping):
        failures.append(f"{protocol} relative throughput gate is missing")
    else:
        configured_checks = 0
        gate_failed = False
        for name, ratio_field, denominator_mode, configured_minimum in (
            (
                "cached_to_direct",
                "cached_to_direct_throughput_ratio",
                "direct",
                minimum_cached_to_direct,
            ),
            (
                "cached_to_stock",
                "cached_to_stock_throughput_ratio",
                "stock",
                minimum_cached_to_stock,
            ),
        ):
            check = relative_gate.get(name)
            if not isinstance(check, Mapping):
                failures.append(f"{protocol} {name} gate is missing")
                if configured_minimum is not None:
                    configured_checks += 1
                    gate_failed = True
                continue
            try:
                measured = float(check["measured_ratio"])
                reported = float(lane[ratio_field])
            except (KeyError, OverflowError, TypeError, ValueError):
                failures.append(f"{protocol} {name} ratio is invalid")
                if configured_minimum is not None:
                    configured_checks += 1
                    gate_failed = True
                continue
            ratio_valid = (
                math.isfinite(measured)
                and measured >= 0
                and math.isfinite(reported)
                and reported >= 0
            )
            expected_ratio: float | None = None
            denominator = mode_medians.get(denominator_mode)
            if cached_median is not None and denominator is not None:
                expected_ratio = cached_median / denominator
            minimum_matches = check.get("minimum_ratio") is None
            if configured_minimum is not None:
                configured_checks += 1
                try:
                    minimum_matches = math.isclose(
                        float(check["minimum_ratio"]), configured_minimum
                    )
                except (KeyError, OverflowError, TypeError, ValueError):
                    minimum_matches = False
            if configured_minimum is None:
                expected = "MEASURED"
            elif ratio_valid and measured >= configured_minimum:
                expected = "PASS"
            else:
                expected = "FAIL"
                gate_failed = True
            if (
                not ratio_valid
                or expected_ratio is None
                or not math.isclose(measured, reported)
                or not math.isclose(measured, expected_ratio)
                or not minimum_matches
                or check.get("status") != expected
            ):
                failures.append(f"{protocol} {name} relative throughput gate failed")
            elif expected == "FAIL":
                failures.append(f"{protocol} {name} relative throughput is below gate")
        expected_overall = (
            "FAIL"
            if gate_failed
            else ("PASS" if configured_checks else "MEASURED")
        )
        if relative_gate.get("status") != expected_overall:
            failures.append(f"{protocol} aggregate relative throughput gate failed")
    latency_gate = lane.get("latency_gate")
    if not isinstance(latency_gate, Mapping):
        failures.append(f"{protocol} aggregate latency gate is missing")
    else:
        expected_status = (
            "MEASURED"
            if latency_gate.get("maximum_p99_ms") is None
            else "PASS"
        )
        if latency_gate.get("status") != expected_status:
            failures.append(f"{protocol} aggregate latency gate failed")
    return failures


def validate_scaling_snapshot(report: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    workload = report.get("workload")
    enabled = (
        workload.get("scaling_snapshot_enabled", False)
        if isinstance(workload, Mapping)
        else False
    )
    snapshot = report.get("scaling_snapshot")
    if not enabled:
        if snapshot is None:
            return failures
        if not isinstance(snapshot, Mapping) or snapshot.get("status") != "DISABLED":
            failures.append("disabled scaling snapshot has invalid status")
        return failures
    if not isinstance(snapshot, Mapping):
        return ["scaling snapshot is missing"]
    if snapshot.get("status") != "MEASURED":
        failures.append("scaling snapshot was not measured")
    if snapshot.get("performance_gating") is not False:
        failures.append("scaling snapshot must remain performance non-gating")
    if snapshot.get("measurement_order") != [
        SCALING_PRIMARY_PROFILE,
        SCALING_SECONDARY_PROFILE,
    ]:
        failures.append("scaling snapshot measurement order is invalid")
    profiles = snapshot.get("profiles")
    if not isinstance(profiles, Mapping):
        return failures + ["scaling snapshot profiles are missing"]
    if set(profiles) != {SCALING_SECONDARY_PROFILE, SCALING_PRIMARY_PROFILE}:
        failures.append("scaling snapshot profile set is invalid")

    primary = profiles.get(SCALING_PRIMARY_PROFILE)
    if not isinstance(primary, Mapping):
        failures.append("c16/k32 scaling reference is missing")
    else:
        if primary.get("source") != "primary_strict_profile":
            failures.append("c16/k32 must reference the strict primary profile")
        if primary.get("source_path") != "$.protocols":
            failures.append("c16/k32 primary evidence path is invalid")
        if "protocols" in primary:
            failures.append("c16/k32 raw evidence must not be duplicated")
        primary_workload = primary.get("workload")
        expected_primary = {
            "concurrency": SCALING_PRIMARY_CONCURRENCY,
            "jobs": workload.get("jobs") if isinstance(workload, Mapping) else None,
            "throughput_pipeline": SCALING_PRIMARY_PIPELINE,
            "latency_pipeline": 1,
            "duration_seconds": (
                workload.get("duration_seconds")
                if isinstance(workload, Mapping)
                else None
            ),
            "warmup_seconds": (
                workload.get("warmup_seconds")
                if isinstance(workload, Mapping)
                else None
            ),
            "latency_duration_seconds": (
                workload.get("latency_duration_seconds")
                if isinstance(workload, Mapping)
                else None
            ),
            "latency_sample_rate": (
                workload.get("latency_sample_rate")
                if isinstance(workload, Mapping)
                else None
            ),
            "latency_min_samples": (
                workload.get("latency_min_samples")
                if isinstance(workload, Mapping)
                else None
            ),
            "repetitions": (
                workload.get("repetitions")
                if isinstance(workload, Mapping)
                else None
            ),
            "keys": workload.get("keys") if isinstance(workload, Mapping) else None,
            "payload_bytes": (
                workload.get("payload_bytes")
                if isinstance(workload, Mapping)
                else None
            ),
        }
        if primary_workload != expected_primary:
            failures.append("c16/k32 workload does not match the strict profile")
        primary_protocols = report.get("protocols")
        if isinstance(primary_protocols, Mapping):
            for protocol in PROTOCOLS:
                lane = primary_protocols.get(protocol)
                if not isinstance(lane, Mapping):
                    continue
                for benchmark_mode in BENCHMARK_MODES:
                    mode = lane.get(f"{benchmark_mode}_mode")
                    runs = mode.get("runs") if isinstance(mode, Mapping) else None
                    if not isinstance(runs, list):
                        continue
                    for index, run in enumerate(runs, start=1):
                        if (
                            not isinstance(run, Mapping)
                            or run.get("operations_per_batch")
                            != SCALING_PRIMARY_PIPELINE
                        ):
                            failures.append(
                                f"c16/k32 {protocol} {benchmark_mode} run "
                                f"{index} does not use 32 keys per MGET"
                            )

    secondary = profiles.get(SCALING_SECONDARY_PROFILE)
    if not isinstance(secondary, Mapping):
        return failures + ["c4/k8 scaling evidence is missing"]
    if secondary.get("source") != "embedded":
        failures.append("c4/k8 scaling evidence source is invalid")
    secondary_workload = secondary.get("workload")
    if not isinstance(secondary_workload, Mapping):
        return failures + ["c4/k8 scaling workload is missing"]
    try:
        primary_jobs = int(workload.get("jobs", 1))
    except (AttributeError, TypeError, ValueError):
        primary_jobs = 1
        failures.append("strict profile job count is invalid")
    expected_secondary_fields = {
        "concurrency": SCALING_SECONDARY_CONCURRENCY,
        "jobs": min(primary_jobs, SCALING_SECONDARY_CONCURRENCY),
        "throughput_pipeline": SCALING_SECONDARY_PIPELINE,
        "latency_pipeline": 1,
        "keys": workload.get("keys") if isinstance(workload, Mapping) else None,
        "payload_bytes": (
            workload.get("payload_bytes") if isinstance(workload, Mapping) else None
        ),
    }
    for field, expected in expected_secondary_fields.items():
        if secondary_workload.get(field) != expected:
            failures.append(f"c4/k8 scaling workload {field} is invalid")
    repetitions = secondary_workload.get("repetitions")
    if (
        not isinstance(repetitions, int)
        or isinstance(repetitions, bool)
        or repetitions < 1
    ):
        failures.append("c4/k8 scaling repetition count is invalid")
        repetitions = 0
    protocols = secondary.get("protocols")
    if not isinstance(protocols, Mapping):
        return failures + ["c4/k8 scaling protocols are missing"]
    for protocol in PROTOCOLS:
        lane = protocols.get(protocol)
        if not isinstance(lane, Mapping):
            failures.append(f"c4/k8 {protocol} result is missing")
            continue
        failures.extend(
            f"c4/k8 {failure}"
            for failure in validate_protocol_lane(
                lane,
                protocol,
                None,
                repetitions,
                None,
                None,
            )
        )
        for benchmark_mode in BENCHMARK_MODES:
            mode = lane.get(f"{benchmark_mode}_mode")
            runs = mode.get("runs") if isinstance(mode, Mapping) else None
            if not isinstance(runs, list):
                continue
            for index, run in enumerate(runs, start=1):
                if (
                    not isinstance(run, Mapping)
                    or run.get("operations_per_batch")
                    != SCALING_SECONDARY_PIPELINE
                ):
                    failures.append(
                        f"c4/k8 {protocol} {benchmark_mode} run {index} "
                        "does not use 8 keys per MGET"
                    )
    return failures


def validate_report(report: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    server = report.get("server", {})
    if not isinstance(server, Mapping) or server.get("pg_local_cache_port") != 0:
        failures.append("benchmark server is not in SQL-only port=0 mode")
    stock_server = report.get("stock_server", {})
    if not isinstance(stock_server, Mapping):
        failures.append("stock PostgreSQL server proof is missing")
    else:
        if stock_server.get("extension_installed") is not False:
            failures.append("stock PostgreSQL has pg_local_cache installed")
        if stock_server.get("pg_local_cache_preloaded") is not False:
            failures.append("stock PostgreSQL preloads pg_local_cache")
        if stock_server.get("settings_match_mapped_server") is not True:
            failures.append("stock and mapped PostgreSQL settings differ")
        if isinstance(server, Mapping) and (
            stock_server.get("server_version_num")
            != server.get("server_version_num")
        ):
            failures.append("stock and mapped PostgreSQL versions differ")
    role = report.get("ordinary_application_role", {})
    if not isinstance(role, Mapping) or role.get("superuser") is not False:
        failures.append("ordinary benchmark role is not a proven NOSUPERUSER")
    if isinstance(role, Mapping) and role.get("local_cache_schema_usage") is not True:
        failures.append("SQL KV benchmark role cannot access local_cache schema")
    stock_role = report.get("stock_application_role", {})
    if not isinstance(stock_role, Mapping) or stock_role.get("superuser") is not False:
        failures.append("stock benchmark role is not a proven NOSUPERUSER")
    proof = report.get("cold_miss_fill_hit_proof", {})
    if not isinstance(proof, Mapping):
        failures.append("cold SQL proof is missing")
    else:
        failures.extend(validate_cold_proof(proof))
    warm = report.get("complete_keyspace_warm", {})
    workload = report.get("workload", {})
    expected_keys = workload.get("keys") if isinstance(workload, Mapping) else None
    if not isinstance(warm, Mapping):
        failures.append("complete keyspace warm proof is missing")
    else:
        failures.extend(validate_warm_proof(warm, expected_keys))
    integrity = report.get("sentinel_row_integrity_check")
    if not isinstance(integrity, Mapping):
        failures.append("sentinel-row integrity check is missing")
    elif not isinstance(expected_keys, int) or isinstance(expected_keys, bool):
        failures.append("sentinel-row integrity expected key count is invalid")
    else:
        expected_sentinels = sorted({1, (expected_keys + 1) // 2, expected_keys})
        if integrity.get("status") != "PASS":
            failures.append("sentinel-row integrity check did not pass")
        for field, wanted in (
            ("source_row_count", expected_keys),
            ("source_min_id", 1),
            ("source_max_id", expected_keys),
            ("source_distinct_ids", expected_keys),
            ("sentinel_keys", expected_sentinels),
            ("sentinel_rows", len(expected_sentinels)),
            ("stock_mapped_and_cached_rows_equal", True),
            ("direct_and_cached_rows_equal", True),
        ):
            if integrity.get(field) != wanted:
                failures.append(
                    f"sentinel-row integrity {field} is not {wanted!r}"
                )
        direct_deltas = integrity.get("direct_counter_deltas")
        cached_deltas = integrity.get("cached_counter_deltas")
        zero_deltas = {
            f"{counter}_during_measurement": 0 for counter in SQL_COUNTERS
        }
        expected_cached_deltas = dict(zero_deltas)
        expected_cached_deltas["sql_cache_hits_during_measurement"] = len(
            expected_sentinels
        )
        if direct_deltas != zero_deltas:
            failures.append("direct sentinel counters are not all zero")
        if cached_deltas != expected_cached_deltas:
            failures.append("cached sentinel counters are not exact hits")
        digest = integrity.get("sentinel_rows_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            failures.append("sentinel-row digest is invalid")
    plan = report.get("ordinary_select_proof", {})
    if not isinstance(plan, Mapping):
        failures.append("ordinary SELECT plan proof is missing")
    else:
        if CUSTOM_SCAN_NAME in str(plan.get("cached_plan", "")):
            failures.append("SQL KV function unexpectedly used CustomScan")
        if CUSTOM_SCAN_NAME in str(plan.get("direct_plan", "")):
            failures.append("direct ordinary SELECT unexpectedly used CustomScan")
        if CUSTOM_SCAN_NAME in str(plan.get("stock_plan", "")):
            failures.append("stock ordinary SELECT unexpectedly used CustomScan")
        if plan.get("stock_mapped_and_cached_rows_equal") is not True:
            failures.append("stock, mapped, and cached ordinary SELECT rows differ")
        if plan.get("direct_and_cached_rows_equal") is not True:
            failures.append("cached and direct ordinary SELECT rows differ")
    protocols = report.get("protocols", {})
    if not isinstance(protocols, Mapping):
        return failures + ["protocol results are missing"]
    minimums: dict[str, float] = {}
    for protocol in PROTOCOLS:
        raw_minimum = (
            workload.get(f"{protocol}_min_ops", DEFAULT_MINIMUM_OPS)
            if isinstance(workload, Mapping)
            else None
        )
        if isinstance(raw_minimum, bool):
            parsed_minimum = math.inf
        else:
            try:
                parsed_minimum = float(raw_minimum)
            except (OverflowError, TypeError, ValueError):
                parsed_minimum = math.inf
        if not math.isfinite(parsed_minimum) or parsed_minimum < 0:
            failures.append(f"{protocol} throughput minimum is invalid")
            parsed_minimum = math.inf
        minimums[protocol] = parsed_minimum

    ratio_minimums: dict[str, float | None] = {}
    for name in ("cached_to_direct", "cached_to_stock"):
        field = f"min_{name}_ratio"
        raw_minimum = workload.get(field) if isinstance(workload, Mapping) else None
        if raw_minimum is None:
            ratio_minimums[name] = None
            continue
        if isinstance(raw_minimum, bool):
            parsed_minimum = math.inf
        else:
            try:
                parsed_minimum = float(raw_minimum)
            except (OverflowError, TypeError, ValueError):
                parsed_minimum = math.inf
        if not math.isfinite(parsed_minimum) or parsed_minimum < 0:
            failures.append(f"{name} throughput ratio minimum is invalid")
            parsed_minimum = math.inf
        ratio_minimums[name] = parsed_minimum
    expected_repetitions = (
        workload.get("repetitions") if isinstance(workload, Mapping) else None
    )
    if (
        not isinstance(expected_repetitions, int)
        or isinstance(expected_repetitions, bool)
        or expected_repetitions < 1
    ):
        failures.append("benchmark repetition count is invalid")
        expected_repetitions = 0
    for protocol in PROTOCOLS:
        lane = protocols.get(protocol)
        if not isinstance(lane, Mapping):
            failures.append(f"{protocol} result is missing")
            continue
        failures.extend(
            validate_protocol_lane(
                lane,
                protocol,
                minimums[protocol],
                expected_repetitions,
                ratio_minimums["cached_to_direct"],
                ratio_minimums["cached_to_stock"],
            )
        )
    failures.extend(validate_scaling_snapshot(report))
    return failures


def format_number(value: object, digits: int = 0) -> str:
    return f"{float(value):,.{digits}f}".replace(",", " ")


def render_scaling_markdown(
    report: Mapping[str, Any], mode_labels: Mapping[str, str]
) -> list[str]:
    snapshot = report.get("scaling_snapshot")
    if not isinstance(snapshot, Mapping) or snapshot.get("status") != "MEASURED":
        return []
    lines = [
        "",
        "## c4/k8 and c16/k32 scaling snapshot",
        "",
        "| Protocol | Mode | Throughput profile | Median throughput | "
        "Latency profile | Mean | p50 | p95 | p99 | Samples |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for protocol in PROTOCOLS:
        for benchmark_mode in BENCHMARK_MODES:
            for profile_name in (
                SCALING_SECONDARY_PROFILE,
                SCALING_PRIMARY_PROFILE,
            ):
                lane = scaling_profile_protocols(report, profile_name)[protocol]
                mode = lane[f"{benchmark_mode}_mode"]
                throughput = mode["summary"]["median_operations_per_second"]
                latency = mode["latency"]["distribution"]
                concurrency = snapshot["profiles"][profile_name]["workload"][
                    "concurrency"
                ]
                lines.append(
                    f"| {protocol} | {mode_labels[benchmark_mode]} | "
                    f"{profile_name.replace('_p', '/k')} | "
                    f"{format_number(throughput)} ops/s | "
                    f"c{concurrency}/k1 | "
                    f"{format_number(latency['mean_ms'], 3)} ms | "
                    f"{format_number(latency['p50_ms'], 3)} ms | "
                    f"{format_number(latency['p95_ms'], 3)} ms | "
                    f"{format_number(latency['p99_ms'], 3)} ms | "
                    f"{format_number(latency['sample_count'])} |"
                )
    secondary_workload = snapshot["profiles"][SCALING_SECONDARY_PROFILE][
        "workload"
    ]
    primary_workload = snapshot["profiles"][SCALING_PRIMARY_PROFILE][
        "workload"
    ]
    lines.extend(
        (
            "",
            "The MGET key width applies to throughput only. Latency uses "
            "one scalar key read per transaction: c4/k1 and c16/k1. The c4/k8 "
            f"snapshot uses {secondary_workload['repetitions']} repetition; "
            "its performance values are non-gating. c16/k32 references the "
            f"{primary_workload['repetitions']}-repetition strict profile "
            "above without copying its raw latency samples.",
        )
    )
    return lines


def render_markdown(report: Mapping[str, Any]) -> str:
    workload = report["workload"]
    mode_labels = {
        "stock": "Stock PostgreSQL (no extension)",
        "direct": "Mapped table, cache off",
        "cached": "pg_local_cache, cache on",
    }
    lines = [
        "# pg_local_cache SQL-only benchmark",
        "",
        f"Generated: `{report['generated_at_utc']}`",
        "",
        "SQL-only `local_cache.mget(regclass, anyarray)` compared with a stock "
        "PostgreSQL primary-key batch lookup designed to return the same ordered "
        "row JSON. The first, middle, and last rows are compared byte-for-byte. "
        "Both use the same LOGIN NOSUPERUSER, key stream, rows, and wire protocol. The mapped "
        "server has `pg_local_cache.port=0`; no RESP listener, client, or token "
        "is used.",
        "",
        "## Throughput and end-to-end latency",
        "",
        "| Protocol | Mode | Throughput profile | Median throughput | "
        "Latency profile | Mean | p50 | p95 | p99 | Samples |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for protocol in PROTOCOLS:
        lane = report["protocols"][protocol]
        for benchmark_mode in BENCHMARK_MODES:
            mode = lane[f"{benchmark_mode}_mode"]
            throughput = mode["summary"]["median_operations_per_second"]
            latency = mode["latency"]["distribution"]
            lines.append(
                f"| {protocol} | {mode_labels[benchmark_mode]} | "
                f"c{workload['concurrency']}/k{workload['pipeline']} | "
                f"{format_number(throughput)} ops/s | "
                f"c{workload['concurrency']}/k1 | "
                f"{format_number(latency['mean_ms'], 3)} ms | "
                f"{format_number(latency['p50_ms'], 3)} ms | "
                f"{format_number(latency['p95_ms'], 3)} ms | "
                f"{format_number(latency['p99_ms'], 3)} ms | "
                f"{format_number(latency['sample_count'])} |"
            )
    lines.extend(
        (
            "",
            "Latency is measured in separate, rotated repetitions with one "
            "scalar key read per transaction. This is a "
            "closed-loop saturation measurement, not an equal offered-rate "
            "test. Aggregate "
            "percentiles are recomputed from the retained raw samples. "
            "Throughput is batch TPS multiplied by the configured keys per MGET.",
        )
    )
    lines.extend(render_scaling_markdown(report, mode_labels))
    lines.extend(
        (
            "",
            "## Relative throughput and gates",
            "",
            "| Protocol | Cache/stock | Cache/mapped-off | "
            "Throughput gate | Relative gate | Latency gate |",
            "|---|---:|---:|---:|---:|---:|",
        )
    )
    for protocol in PROTOCOLS:
        lane = report["protocols"][protocol]
        latency_gate = lane["latency_gate"]
        if latency_gate["maximum_p99_ms"] is None:
            latency_gate_text = "**MEASURED** (no p99 limit configured)"
        else:
            latency_gate_text = (
                f"**{latency_gate['status']}** "
                f"(p99 <= {format_number(latency_gate['maximum_p99_ms'], 3)} ms)"
            )
        minimum_ops = lane["throughput_gate"][
            "minimum_cached_operations_per_second"
        ]
        relative_gate = lane["relative_throughput_gate"]
        relative_gate_text = f"**{relative_gate['status']}**"
        lines.append(
            f"| {protocol} | "
            f"{format_number(lane['cached_to_stock_throughput_ratio'], 2)}x | "
            f"{format_number(lane['cached_to_direct_throughput_ratio'], 2)}x | "
            f"**{lane['throughput_gate']['status']}** "
            f"(>= {format_number(minimum_ops)} "
            "ops/s) | "
            f"{relative_gate_text} | "
            f"{latency_gate_text} |"
        )
    cold = report["cold_miss_fill_hit_proof"]
    warm = report["complete_keyspace_warm"]
    integrity = report["sentinel_row_integrity_check"]
    cached_sentinel_hits = integrity["cached_counter_deltas"][
        "sql_cache_hits_during_measurement"
    ]
    lines.extend(
        (
            "",
            "## Correctness evidence",
            "",
            "| Proof | Hits | Misses | Fills | Bypasses |",
            "|---|---:|---:|---:|---:|",
            f"| cold read -> fill -> warm read | "
            f"{cold['sql_cache_hits_during_measurement']} | "
            f"{cold['sql_cache_misses_during_measurement']} | "
            f"{cold['sql_cache_fills_during_measurement']} | "
            f"{cold['sql_cache_bypasses_during_measurement']} |",
            f"| complete {warm['keys_filled']}-key warm pass | "
            f"{warm['sql_cache_hits_during_measurement']} | "
            f"{warm['sql_cache_misses_during_measurement']} | "
            f"{warm['sql_cache_fills_during_measurement']} | "
            f"{warm['sql_cache_bypasses_during_measurement']} |",
            f"| {integrity['sentinel_rows']}-row stock/mapped/cache check | "
            f"{cached_sentinel_hits} | "
            "0 | 0 | 0 |",
            "",
            "Every cached timed run requires `hits == successful key reads` and "
            "zero misses, fills, or bypasses. Every direct run requires all "
            "four SQL-cache counter deltas to be zero.",
            "",
            "## Workload",
            "",
            "| Parameter | Value |",
            "|---|---:|",
            f"| Requested/effective duration per measured run | "
            f"{workload['duration_seconds']} / "
            f"{workload['effective_duration_seconds']} s |",
            f"| Requested/effective warmup per mode | "
            f"{workload['warmup_seconds']} / "
            f"{workload['effective_warmup_seconds']} s |",
            f"| Requested/effective latency duration per mode | "
            f"{workload['latency_duration_seconds']} / "
            f"{workload['effective_latency_duration_seconds']} s |",
            f"| Latency sampling rate | {workload['latency_sample_rate']} |",
            f"| Minimum latency samples per run/mode | {workload['latency_min_samples']} |",
            f"| Repetitions per mode/protocol | {workload['repetitions']} |",
            f"| Concurrent connections | {workload['concurrency']} |",
            f"| pgbench jobs | {workload['jobs']} |",
            f"| Keys per SQL MGET | {workload['pipeline']} |",
            f"| Distinct KV rows | {workload['keys']} |",
            f"| Text payload bytes | {workload['payload_bytes']} |",
            "",
            f"Overall gate: **{report['gate']['status']}** — "
            f"{report['gate']['message']}",
            "",
            "Prepared and unnamed-extended results have independent >=10k "
            "gates and are never pooled. Cache/direct and cache/stock ratio "
            "gates are reported separately. A rotating three-mode order "
            "reduces run-order bias. All modes use identical schema, data, returned "
            "bytes, role, key stream, connections, jobs, duration, and protocol "
            "settings; SQL differs only because stock PostgreSQL has no `mget`.",
            "",
        )
    )
    return "\n".join(lines)


def write_report(report: Mapping[str, Any], output_directory: Path) -> None:
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "sql-only-failure.json").unlink(missing_ok=True)
    (output_directory / "sql-only-failure.md").unlink(missing_ok=True)
    json_tmp = output_directory / ".sql-only.json.tmp"
    markdown_tmp = output_directory / ".sql-only.md.tmp"
    json_tmp.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    markdown_tmp.write_text(render_markdown(report), encoding="utf-8")
    os.replace(json_tmp, output_directory / "sql-only.json")
    os.replace(markdown_tmp, output_directory / "sql-only.md")


def write_failure_report(error: BaseException, output_directory: Path) -> None:
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "sql-only.json").unlink(missing_ok=True)
    (output_directory / "sql-only.md").unlink(missing_ok=True)
    payload = {
        "schema_version": 3,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "FAIL",
        "error_type": type(error).__name__,
        "error": str(error),
        "traceback": traceback.format_exc(),
        "source_revision": os.environ.get(
            "PGLC_BENCH_SOURCE_REVISION", "unknown"
        ),
    }
    json_tmp = output_directory / ".sql-only-failure.json.tmp"
    json_tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(json_tmp, output_directory / "sql-only-failure.json")
    markdown_tmp = output_directory / ".sql-only-failure.md.tmp"
    markdown_tmp.write_text(
        "# SQL-only benchmark failed\n\n"
        f"- Error: `{type(error).__name__}: {error}`\n",
        encoding="utf-8",
    )
    os.replace(markdown_tmp, output_directory / "sql-only-failure.md")


def tool_version(command: str) -> str:
    try:
        return run_checked([command, "--version"], timeout=30)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        return f"unavailable: {error}"


def build_report(config: Config) -> dict[str, Any]:
    server = discover_server(config)
    mapped_settings = read_comparable_settings(config, target="mapped")
    server["comparison_settings"] = mapped_settings
    stock_server = discover_stock_server(
        config,
        mapped_server=server,
        mapped_settings=mapped_settings,
    )
    preflight_names(config, target="mapped")
    preflight_names(config, target="stock")
    cleanup_authorized = True
    primary_error: BaseException | None = None
    try:
        mapping = setup_objects(config)
        setup_stock_objects(config)
        role = validate_application_role(config, target="mapped")
        stock_role = validate_application_role(config, target="stock")
        select_proof = explain_and_sample(config)
        cold_proof = cold_miss_fill_hit_proof(config)
        warm_proof = warm_all_keys(config)
        integrity_check = sentinel_row_integrity_check(config)
        with tempfile.TemporaryDirectory(
            prefix="pglc_sql_only_scripts_"
        ) as directory:
            script_paths: dict[str, Path] = {}
            latency_script_paths: dict[str, Path] = {}
            for mode in BENCHMARK_MODES:
                query = (
                    config.lookup_query
                    if mode == "cached"
                    else config.direct_lookup_query
                )
                script_path = Path(directory) / f"{mode}-throughput.sql"
                latency_script_path = Path(directory) / f"{mode}-latency.sql"
                script_path.write_text(
                    lookup_script(config, cached=mode == "cached"),
                    encoding="utf-8",
                )
                latency_script_path.write_text(
                    latency_lookup_script(config, query), encoding="utf-8"
                )
                script_paths[mode] = script_path
                latency_script_paths[mode] = latency_script_path
            protocols = {
                "prepared": measure_protocol(
                    config,
                    script_paths,
                    latency_script_paths,
                    "prepared",
                    config.prepared_min_ops,
                ),
                "extended": measure_protocol(
                    config,
                    script_paths,
                    latency_script_paths,
                    "extended",
                    config.extended_min_ops,
                ),
            }
            scaling_snapshot = measure_scaling_snapshot(
                config, Path(directory)
            )
        report: dict[str, Any] = {
            "schema_version": 3,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "server": server,
            "stock_server": stock_server,
            "connection": {
                "host": config.host,
                "port": config.port,
                "database": config.database,
            },
            "stock_connection": {
                "host": config.stock_host,
                "port": config.stock_port,
                "database": config.stock_database,
            },
            "environment": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "psql": tool_version("psql"),
                "pgbench": tool_version("pgbench"),
                "benchmark_client": discover_client_resources(),
                "source_revision": os.environ.get(
                    "PGLC_BENCH_SOURCE_REVISION", "unknown"
                ),
                "harness_sha256": os.environ.get(
                    "PGLC_BENCH_SQL_ONLY_HARNESS_SHA256", "unknown"
                ),
            },
            "workload": {
                "duration_seconds": config.duration,
                "effective_duration_seconds": max(1, math.ceil(config.duration)),
                "warmup_seconds": config.warmup_seconds,
                "effective_warmup_seconds": (
                    max(1, math.ceil(config.warmup_seconds))
                    if config.warmup_seconds > 0
                    else 0
                ),
                "latency_duration_seconds": config.latency_duration,
                "effective_latency_duration_seconds": max(
                    1, math.ceil(config.latency_duration)
                ),
                "latency_sample_rate": config.latency_sample_rate,
                "latency_min_samples": config.latency_min_samples,
                "latency_max_p99_ms": config.latency_max_p99_ms,
                "repetitions": config.repetitions,
                "concurrency": config.concurrency,
                "jobs": config.jobs,
                "pipeline": config.pipeline,
                "keys": config.keys,
                "payload_bytes": config.payload_bytes,
                "prepared_min_ops": config.prepared_min_ops,
                "extended_min_ops": config.extended_min_ops,
                "min_cached_to_direct_ratio": (
                    config.min_cached_to_direct_ratio
                ),
                "min_cached_to_stock_ratio": config.min_cached_to_stock_ratio,
                "scaling_snapshot_enabled": config.scaling_snapshot_enabled,
                "query": config.lookup_query,
            },
            "methodology": {
                "transport": "PostgreSQL wire protocol only; RESP disabled",
                "table_shape": (
                    "whole row with bigint id primary key and fixed tenant_id "
                    "payload column"
                ),
                "application_access": "actual LOGIN NOSUPERUSER, SELECT only",
                "stock_mode": (
                    "separate PostgreSQL server with no pg_local_cache "
                    "extension or preload"
                ),
                "direct_mode": "SET pg_local_cache.sql_cache=off",
                "cached_mode": "SET pg_local_cache.sql_cache=on",
                "prepared_protocol": "pgbench -M prepared",
                "unnamed_extended_protocol": "pgbench -M extended",
                "counter_isolation": (
                    "global SQL counters must exactly equal harness operations; "
                    "concurrent cache traffic fails the run"
                ),
                "comparability": (
                    "exact server version and query-affecting settings; "
                    "identical schema, generated data, role, protocol, client "
                    "concurrency, jobs, duration, seed, keyspace, and intended "
                    "ordered row JSON; SQL differs because stock PostgreSQL "
                    "does not provide local_cache.mget"
                ),
                "latency": (
                    "closed-loop saturation latency from sampled pgbench "
                    "per-transaction logs in rotated, repeated pipeline-depth-1 "
                    "passes; aggregate percentiles are recomputed from retained "
                    "raw samples and are not an equal offered-rate comparison"
                ),
                "row_integrity": (
                    "source count/key range plus identical stock, mapped, and "
                    "cached rows at the first, middle, and last key"
                ),
                "scaling_snapshot": (
                    "non-gating c4/k8 measurement on the same servers after "
                    "the strict c16/k32 profile; correctness accounting remains "
                    "fail-closed and latency uses pipeline depth one"
                ),
            },
            "mapping": mapping,
            "ordinary_application_role": role,
            "stock_application_role": stock_role,
            "ordinary_select_proof": select_proof,
            "cold_miss_fill_hit_proof": cold_proof,
            "complete_keyspace_warm": warm_proof,
            "sentinel_row_integrity_check": integrity_check,
            "protocols": protocols,
            "scaling_snapshot": scaling_snapshot,
        }
        failures = validate_report(report)
        report["gate"] = {
            "status": "PASS" if not failures else "FAIL",
            "message": (
                "both SQL-only protocol gates and all exact accounting checks passed"
                if not failures
                else "; ".join(failures)
            ),
            "failures": failures,
        }
        return report
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if cleanup_authorized and not config.keep_objects:
            cleanup_errors: list[Exception] = []
            for target in ("mapped", "stock"):
                try:
                    cleanup_objects(config, target=target)
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                if primary_error is None:
                    raise cleanup_errors[0]
                for cleanup_error in cleanup_errors:
                    print(
                        f"warning: benchmark cleanup also failed: {cleanup_error}",
                        file=sys.stderr,
                        flush=True,
                    )


def main() -> int:
    config = Config.from_environment()
    report = build_report(config)
    write_report(report, config.output_directory)
    print(render_markdown(report), flush=True)
    return 0 if report["gate"]["status"] == "PASS" else 1


if __name__ == "__main__":
    output = Path(
        os.environ.get("PGLC_SQL_ONLY_BENCH_OUTPUT_DIR", "benchmark-results")
    )
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as error:
        print(f"SQL-only benchmark failed: {error}", file=sys.stderr, flush=True)
        try:
            write_failure_report(error, output)
        except Exception as report_error:
            print(
                f"could not write SQL-only failure artifact: {report_error}",
                file=sys.stderr,
                flush=True,
            )
        raise SystemExit(1)
