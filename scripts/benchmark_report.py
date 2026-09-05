#!/usr/bin/env python3
"""Render recorded demo samples without merging percentiles or selecting winners."""
import argparse
import json
import math
from pathlib import Path


def number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ValueError(f"invalid measurement: {value!r}")
    return f"{value:,.3f}"


def summary(data):
    if data.get("schema") != 1 or not data.get("results"):
        raise ValueError("expected schema 1 and at least one recorded sample")
    environment = data["environment"]
    lines = ["# pg_local_cache benchmark", "",
             f"Measured: {data['measured_at']}",
             f"PostgreSQL: {environment['postgres_version']}; extension: {environment['extension_version']}",
             f"Extension ref: `{data['extension_ref']}`; harness ref: `{data['harness_ref']}`", "",
             "Closed-loop client timings over loopback TCP. See the JSON for configuration and cache counters.", "",
             "| Repeat | Workload | Path | Batch | Requests/s | Requested read keys/s |",
             "|---:|---|---|---:|---:|---:|"]
    for row in data["results"]:
        lines.append(f"| {row['repeat']} | {row['workload']} | {row['mode']} | {row['batch']} | {number(row['requests_s'])} | {number(row['requested_read_keys_s'])} |")
    lines += ["", "## Latency (milliseconds)", "",
              "| Repeat | Workload | Path | Batch | Operation | Samples | p50 | p95 | p99 |",
              "|---:|---|---|---:|---|---:|---:|---:|---:|"]
    for row in data["results"]:
        for operation in ("read", "write"):
            measured = row[f"{operation}_latency"]
            if measured is not None:
                values = " | ".join(number(measured[key]) for key in ("p50_ms", "p95_ms", "p99_ms"))
                lines.append(f"| {row['repeat']} | {row['workload']} | {row['mode']} | {row['batch']} | {operation} | {measured['samples']} | {values} |")
    lines += ["", "Repetitions are separate samples. Small cold-fill samples do not give stable tail estimates.", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    args = parser.parse_args()
    try:
        print(summary(json.loads(args.result.read_text())), end="")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"cannot render benchmark: {error}\n")
