#!/usr/bin/env python3
"""Two-machine read benchmark: prepared SQL vs pg_local_cache RESP vs Valkey.

Runs from a workstation over SSH. The load node runs the Go client against the
database node's private address; the database node is sampled with mpstat/sar.
Writes one JSON object per run to the output file.
"""
import itertools
import ipaddress
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
import time

SSH = os.environ.get("PGLC_SSH", "ssh")
DB = os.environ.get("PGLC_DB_HOST", "pglc-db")
LOAD = os.environ.get("PGLC_LOAD_HOST", "pglc-load")
DB_ADDR = os.environ.get("PGLC_DB_ADDR", "192.168.0.4")
TOKEN_FILE = os.environ.get("PGLC_TOKEN_FILE", "/root/pglc.token")
NET_IF = os.environ.get("PGLC_NET_IF")
SERVER_CPUS = os.environ.get("PGLC_SERVER_CPUS", "").strip() or None
ANY_SQL = ("SELECT id::text AS key, row_to_json(i)::text AS row "
           "FROM public.items AS i WHERE id = ANY($1::bigint[])")
MODE_CONFIG = {
    "postgres-any": {"mode": "postgres-any", "resp_port": 6380},
    "pg_local_cache": {"mode": "resp-mget", "resp_port": 6380},
    "valkey": {"mode": "resp-mget", "resp_port": 6379},
}
MODES = [value.strip() for value in os.environ.get(
    "MODES", ",".join(MODE_CONFIG)).split(",") if value.strip()]
SECONDS = int(os.environ.get("SECONDS_PER_RUN", "20"))
REPEATS = int(os.environ.get("REPEATS", "3"))
BATCHES = [int(x) for x in os.environ.get("BATCHES", "1,16,64").split(",")]
CLIENTS = [int(x) for x in os.environ.get("CLIENTS", "16,64,256").split(",")]
KEY_SPACES = [int(x) for x in os.environ.get("KEY_SPACES", "0,100000").split(",")]


def ssh(host, command, stdin=None, timeout=600):
    return subprocess.run([SSH, host, command], input=stdin, text=True,
                          capture_output=True, timeout=timeout, check=True).stdout


def cpu_ids(cpus):
    if not re.fullmatch(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*", cpus):
        raise ValueError(f"invalid PGLC_SERVER_CPUS: {cpus!r}")
    result = set()
    for part in cpus.split(","):
        bounds = [int(value) for value in part.split("-")]
        if len(bounds) == 1:
            ids = {bounds[0]}
        elif bounds[0] <= bounds[1]:
            ids = set(range(bounds[0], bounds[1] + 1))
        else:
            raise ValueError(f"invalid CPU range: {part!r}")
        if result.intersection(ids):
            raise ValueError(f"duplicate CPU in PGLC_SERVER_CPUS: {cpus!r}")
        result.update(ids)
    return result


def host_nproc(host):
    return int(ssh(host, "nproc --all").strip())


def db_interface():
    wanted = ipaddress.ip_address(DB_ADDR)
    for line in ssh(DB, "ip -o -4 addr").splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[2] == "inet":
            if ipaddress.ip_interface(fields[3]).ip == wanted:
                return fields[1].split("@", 1)[0]
    raise RuntimeError(f"no DB interface owns PGLC_DB_ADDR={DB_ADDR}")


def set_server_cpus(cpus):
    quoted = shlex.quote(cpus)
    ssh(DB, "systemctl set-property --runtime postgresql@16-main.service "
            f"AllowedCPUs={quoted} && systemctl set-property --runtime "
            f"valkey-server.service AllowedCPUs={quoted}")


def reset_server_cpus():
    ssh(DB, "systemctl set-property --runtime postgresql@16-main.service AllowedCPUs=; "
            "systemctl set-property --runtime valkey-server.service AllowedCPUs=")


def parse_mpstat(log, cpus):
    header = None
    groups = {}
    pinned = cpu_ids(cpus) if cpus else None
    for line in log.splitlines():
        fields = line.split()
        if "CPU" in fields and "%idle" in fields:
            header = fields
            cpu_index = fields.index("CPU")
            idle_index = fields.index("%idle")
            continue
        if not header or not fields or fields[0] == "Average:" or len(fields) <= idle_index:
            continue
        processor = fields[cpu_index]
        if pinned is None and processor != "all":
            continue
        if pinned is not None and (not processor.isdigit() or int(processor) not in pinned):
            continue
        try:
            busy = 100 - float(fields[idle_index])
        except ValueError:
            continue
        timestamp = " ".join(fields[:cpu_index])
        groups.setdefault(timestamp, []).append(busy)
    if pinned is None:
        return [values[0] for values in groups.values() if values]
    return [sum(values) / len(values) for values in groups.values()
            if len(values) == len(pinned)]


def parse_sar(log, net_if):
    header = None
    samples = []
    for line in log.splitlines():
        fields = line.split()
        if "IFACE" in fields and "rxkB/s" in fields and "txkB/s" in fields:
            header = fields
            iface_index = fields.index("IFACE")
            rx_offset = fields.index("rxkB/s") - iface_index
            tx_offset = fields.index("txkB/s") - iface_index
            continue
        if not header or not fields or fields[0] == "Average:" or net_if not in fields:
            continue
        iface_index = fields.index(net_if)
        try:
            samples.append((float(fields[iface_index + rx_offset]),
                            float(fields[iface_index + tx_offset])))
        except (IndexError, ValueError):
            continue
    return samples


def db_sample_start(cpus):
    mp_cpus = ",".join(str(cpu) for cpu in sorted(cpu_ids(cpus))) if cpus else None
    mpstat = f"LC_ALL=C mpstat -P {mp_cpus} 1" if mp_cpus else "LC_ALL=C mpstat 1"
    ssh(DB, "pkill -x mpstat >/dev/null 2>&1 || true; "
            "pkill -x sar >/dev/null 2>&1 || true; rm -f /tmp/mp.log /tmp/sar.log; "
            f"({mpstat} > /tmp/mp.log 2>&1 &) ; "
            "(LC_ALL=C sar -n DEV 1 > /tmp/sar.log 2>&1 &)")


def db_sample_stop(cpus, net_if):
    out = ssh(DB, "pkill -x mpstat >/dev/null 2>&1 || true; "
                  "pkill -x sar >/dev/null 2>&1 || true; sleep 0.2; "
                  "cat /tmp/mp.log; echo ---PGLC-SAR---; cat /tmp/sar.log")
    cpu_part, net_part = out.split("---PGLC-SAR---", 1)
    return parse_mpstat(cpu_part, cpus), parse_sar(net_part, net_if)


def steady(values, seconds):
    # Drop connection setup/warmup and teardown: keep the last `seconds` samples minus 2.
    tail = values[-(seconds + 1):-1] if len(values) > seconds + 1 else values
    return tail[2:] if len(tail) > 4 else tail


def run_case(token, mode_name, batch, clients, key_space, db_nproc,
             net_if, server_cpus):
    cfg = {"clients": clients, "batch": batch, "seconds": SECONDS, "any_sql": ANY_SQL,
           "port": 5432, "resp_token": token, "key_space": key_space,
           **MODE_CONFIG[mode_name]}
    db_sample_start(server_cpus)
    started = time.time()
    out = ssh(LOAD, f"PGLC_BENCH_HOST={shlex.quote(DB_ADDR)} /root/bench-client",
              stdin=json.dumps(cfg) + "\ngo\n\n", timeout=SECONDS * 6 + 120)
    busy, net = db_sample_stop(server_cpus, net_if)
    result = [json.loads(line) for line in out.splitlines() if line.startswith("{")][-1]["result"]
    busy_s = steady(busy, SECONDS)
    net_s = steady(net, SECONDS)
    busy_pct = statistics.median(busy_s) if busy_s else None
    server_core_count = len(cpu_ids(server_cpus)) if server_cpus else db_nproc
    return {
        "mode": mode_name, "batch": batch, "clients": clients, "key_space": key_space,
        "seconds": SECONDS, "started": started, **result,
        "db_nproc": db_nproc, "server_cpus": server_cpus,
        "server_cpu_busy_pct": round(busy_pct, 1) if busy_pct is not None else None,
        "server_cpu_cores": round(busy_pct / 100 * server_core_count, 2) if busy_pct is not None else None,
        "requests_per_server_core": round(result["requests_s"] / len(cpu_ids(server_cpus)), 2)
        if server_cpus else None,
        "server_rx_kBps": round(statistics.median(n[0] for n in net_s), 1) if net_s else None,
        "server_tx_kBps": round(statistics.median(n[1] for n in net_s), 1) if net_s else None,
    }


def main():
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} OUTPUT.jsonl")
    unknown = sorted(set(MODES) - set(MODE_CONFIG))
    if unknown:
        raise SystemExit(f"unknown MODES: {', '.join(unknown)}")
    if not MODES:
        raise SystemExit("MODES must contain at least one mode")
    if SERVER_CPUS:
        cpu_ids(SERVER_CPUS)
    db_nproc = host_nproc(DB)
    client_nproc = host_nproc(LOAD)
    net_if = NET_IF or db_interface()
    token = ssh(LOAD, f"cat {shlex.quote(TOKEN_FILE)}").strip()
    cases = list(itertools.product(KEY_SPACES, BATCHES, CLIENTS, MODES))
    done = 0
    try:
        if SERVER_CPUS:
            set_server_cpus(SERVER_CPUS)
        with open(sys.argv[1], "a", encoding="utf-8") as sink:
            for repeat in range(REPEATS):
                for key_space, batch, clients, mode_name in cases:
                    for attempt in range(3):
                        try:
                            row = run_case(token, mode_name, batch, clients, key_space,
                                           db_nproc, net_if, SERVER_CPUS)
                            break
                        except subprocess.CalledProcessError as error:
                            print(f"retry {mode_name} b={batch} c={clients}: {error.stderr[-300:]}",
                                  file=sys.stderr, flush=True)
                            time.sleep(5)
                    else:
                        raise SystemExit(f"failed: {mode_name} b={batch} c={clients} ks={key_space}")
                    row["repeat"] = repeat
                    row["client_nproc"] = client_nproc
                    sink.write(json.dumps(row) + "\n")
                    sink.flush()
                    done += 1
                    print(f"[{done}/{len(cases) * REPEATS}] {mode_name:14} ks={key_space:<6} b={batch:<2} "
                          f"c={clients:<3} {row['requests_s']:>10.0f} req/s p99={row['latency']['p99_ms']:.3f}ms "
                          f"client={row['client_cpu_cores']:.2f} server={row['server_cpu_cores']} "
                          f"tx={row['server_tx_kBps']}kB/s", flush=True)
    finally:
        if SERVER_CPUS:
            reset_server_cpus()


if __name__ == "__main__":
    main()
