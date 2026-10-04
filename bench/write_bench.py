#!/usr/bin/env python3
"""Two-node pg_local_cache write and mixed read/write benchmark."""
import itertools
import json
import os
import re
import selectors
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
SERVER_CPUS = os.environ.get("PGLC_SERVER_CPUS", "").strip() or None
DB_NPROC = None
ANY_SQL = "SELECT id::text AS key, row_to_json(i)::text AS row FROM public.{table} AS i WHERE id = ANY($1::bigint[])"
REPEATS = int(os.environ.get("REPEATS", "3"))
WRITE_SECONDS = int(os.environ.get("WRITE_SECONDS", os.environ.get("SECONDS_PER_RUN", "20")))
MIXED_SECONDS = int(os.environ.get("MIXED_SECONDS", "30"))
KEY_SPACE = int(os.environ.get("KEY_SPACE", "60000"))
INSERT_START = int(os.environ.get("INSERT_START", "100001"))
CHECK_IDS = list(range(900_000_001, 900_000_009))


def values(name, default, cast=str):
    return [cast(value.strip()) for value in os.environ.get(name, default).split(",") if value.strip()]


def ssh(host, command, stdin=None, timeout=900):
    return subprocess.run([SSH, host, command], input=stdin, text=True,
                          capture_output=True, timeout=timeout, check=True).stdout


def ensure_check_rows():
    sql_items = ("INSERT INTO public.items(id, value, revision) "
                 "SELECT id, repeat(chr(120), 8), 0 "
                 "FROM generate_series(900000001, 900000008) AS ids(id) "
                 "ON CONFLICT (id) DO NOTHING")
    sql_direct = sql_items.replace("public.items", "public.direct_items")
    command = ("su postgres -c \"psql -qX -v ON_ERROR_STOP=1 -d pglc_demo "
               f"-c '{sql_items}' -c '{sql_direct}'\"")
    ssh(DB, command, timeout=300)


def flush_valkey(token):
    command = (f"valkey-cli -a {shlex.quote(token)} --no-auth-warning "
               "-n 0 FLUSHDB")
    ssh(DB, command)


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


def client_config(token, mode, seconds, **extra):
    return {"clients": 64, "batch": 1, "seconds": seconds, "mode": mode,
            "any_sql": ANY_SQL.format(table=extra.pop("read_table", "items")),
            "port": 5432, "resp_port": 6380, "resp_token": token,
            "key_space": KEY_SPACE, "key_dist": "uniform",
            "check_ids": CHECK_IDS, **extra}


def parse_result(output, stderr=""):
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "result" in value:
            return value["result"]
    raise RuntimeError("bench client returned no result JSON: " + stderr[-500:])


def start_client(cfg):
    process = subprocess.Popen(
        [SSH, LOAD, f"PGLC_BENCH_HOST={shlex.quote(DB_ADDR)} /root/bench-client"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1)
    process.stdin.write(json.dumps(cfg, separators=(",", ":")) + "\n")
    process.stdin.flush()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + 45
    ready = None
    while time.monotonic() < deadline:
        if not selector.select(deadline - time.monotonic()):
            break
        ready_line = process.stdout.readline()
        if not ready_line:
            break
        try:
            ready = json.loads(ready_line)
        except json.JSONDecodeError:
            continue
        if isinstance(ready, dict) and ready.get("ready") is True:
            break
        process.kill()
        _, stderr = process.communicate()
        raise RuntimeError(f"bench client not ready: {ready!r} {stderr[-500:]}")
    selector.close()
    if not isinstance(ready, dict) or ready.get("ready") is not True:
        process.kill()
        _, stderr = process.communicate()
        raise TimeoutError(f"bench client ready timeout: {stderr[-500:]}")
    return process


def release_client(process):
    process.stdin.write("go\n\n")
    process.stdin.close()


def finish_client(process, timeout):
    try:
        output, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        output, stderr = process.communicate()
        raise TimeoutError(f"bench client timed out: {stderr[-500:]}")
    if process.returncode:
        raise RuntimeError(f"bench client exit {process.returncode}: {stderr[-1000:]}\n{output[-1000:]}")
    return parse_result(output, stderr)


def stop_client(process):
    if process is not None and process.poll() is None:
        process.kill()
        process.communicate()


def run_client(cfg, timeout=None):
    process = start_client(cfg)
    release_client(process)
    return finish_client(process, timeout or cfg["seconds"] + 90)


def db_sample_start():
    mp_cpus = ",".join(str(cpu) for cpu in sorted(cpu_ids(SERVER_CPUS))) if SERVER_CPUS else None
    mpstat = f"LC_ALL=C mpstat -P {mp_cpus} 1" if mp_cpus else "LC_ALL=C mpstat 1"
    ssh(DB, "pkill -x mpstat >/dev/null 2>&1 || true; rm -f /tmp/pglc-write-mp.log; "
            f"({mpstat} > /tmp/pglc-write-mp.log 2>&1 &)")


def db_sample_stop():
    out = ssh(DB, "pkill -x mpstat >/dev/null 2>&1 || true; sleep 0.2; "
                  "cat /tmp/pglc-write-mp.log")
    busy = parse_mpstat(out, SERVER_CPUS)
    steady = busy[2:-1] if len(busy) > 4 else busy
    median = statistics.median(steady) if steady else None
    server_core_count = len(cpu_ids(SERVER_CPUS)) if SERVER_CPUS else DB_NPROC
    return {"db_nproc": DB_NPROC, "server_cpus": SERVER_CPUS,
            "db_cpu_busy_pct": round(median, 2) if median is not None else None,
            "db_cpu_cores": round(median / 100 * server_core_count, 3)
            if median is not None else None}


def stats():
    out = ssh(DB, "su postgres -c \"psql -qXAt -d pglc_demo -c 'SELECT local_cache.stats()'\"")
    result = json.loads(out.strip())
    if not {"cache_hits", "cache_misses"}.issubset(result):
        raise RuntimeError("local_cache.stats() lacks cache_hits/cache_misses")
    return result


def cache_stack(stack):
    if stack == "pglc":
        return {"mode": "resp-mget", "read_table": "items", "resp_port": 6380}
    if stack == "valkey":
        return {"mode": "valkey-aside", "read_table": "direct_items", "resp_port": 6379}
    return {"mode": "postgres-any", "read_table": "direct_items", "resp_port": 6380}


def read_cfg(token, stack, seconds, key_dist):
    config = client_config(token, **cache_stack(stack), seconds=seconds,
                           key_dist=key_dist)
    return config


def writer_cfg(token, table, seconds, op, rate, sync="off", valkey_del=False):
    return client_config(token, "write", seconds, read_table=table,
                         op=op, table=table, rate=rate, synchronous_commit=sync,
                         insert_start=INSERT_START, valkey_del=valkey_del,
                         resp_port=6379)


def cleanup_insert(table):
    command = ("su postgres -c \"psql -qX -v ON_ERROR_STOP=1 -d pglc_demo "
               f"-c 'DELETE FROM public.{table} WHERE id > 100000 AND id < 900000000' "
               f"-c 'VACUUM public.{table}'\"")
    ssh(DB, command, timeout=300)


def overhead_case(token, output, repeat, op, clients, sync, stack, seconds):
    table = "direct_items" if stack in ("plain", "plain+valkey-del") else "items"
    config = writer_cfg(token, table, seconds, op, 0, sync,
                        valkey_del=(stack == "plain+valkey-del"))
    config["clients"] = clients
    config["key_space"] = 100_000
    started = time.time()
    process = start_client(config)
    db_sample_start()
    try:
        release_client(process)
        result = finish_client(process, seconds + 120)
    finally:
        stop_client(process)
        db_cpu = db_sample_stop()
    tps = result["tx_s"]
    row = {"phase": "write-overhead", "repeat": repeat, "op": op,
           "stack": stack, "table": table, "clients": clients,
           "synchronous_commit": sync, "seconds": seconds, "started": started,
           **result, **db_cpu,
           "requests_per_server_core": round(result["requests_s"] / len(cpu_ids(SERVER_CPUS)), 2)
           if SERVER_CPUS and result.get("requests_s") is not None else None,
           "db_cpu_us_per_tx": round(db_cpu["db_cpu_cores"] * 1e6 / tps, 2)
           if db_cpu["db_cpu_cores"] is not None and tps else None}
    output.write(json.dumps(row, separators=(",", ":")) + "\n")
    output.flush()
    print(f"write {op:6} {stack:15} c={clients:<2} sync={sync:3} "
          f"{tps:9.0f} tx/s p99={result['latency']['p99_ms']:.3f}ms "
          f"DB={row['db_cpu_cores']} cores us/tx={row['db_cpu_us_per_tx']}", flush=True)
    if op == "insert":
        cleanup_insert(table)


def mixed_case(token, output, repeat, stack, key_dist, writer_rate, op, seconds):
    table = "items" if stack == "pglc" else "direct_items"
    if stack == "valkey":
        flush_valkey(token)
    warm = read_cfg(token, stack, 10, key_dist)
    run_client(warm, 120)
    reader_cfg = read_cfg(token, stack, seconds, key_dist)
    writer = None
    writer_clients = 64
    writer_seconds = seconds + 6
    if writer_rate is not None:
        writer = start_client(writer_cfg(token, table, writer_seconds, op,
                                         writer_rate, "off", stack == "valkey"))
    reader = start_client(reader_cfg)
    before = stats() if stack == "pglc" else None
    db_sample_start()
    try:
        if writer is not None:
            release_client(writer)
            time.sleep(3)
        release_client(reader)
        read_result = finish_client(reader, seconds + 90)
        after = stats() if stack == "pglc" else None
        write_result = finish_client(writer, writer_seconds + 90) if writer is not None else None
    finally:
        stop_client(reader)
        stop_client(writer)
        db_cpu = db_sample_stop()
    if writer_rate is None:
        write_result = None
    if stack == "pglc":
        hits = after["cache_hits"] - before["cache_hits"]
        misses = after["cache_misses"] - before["cache_misses"]
        hit_ratio = hits / max(hits + misses, 1)
    elif stack == "valkey":
        hits = read_result["cache_hits"]
        misses = read_result["cache_misses"]
        hit_ratio = hits / max(hits + misses, 1)
    else:
        hits = misses = hit_ratio = None
    stale = None
    if stack in ("pglc", "valkey"):
        stale_cfg = client_config(token, "stale-check", 1,
                                  target=stack, read_table=table,
                                  resp_port=6380 if stack == "pglc" else 6379)
        stale = run_client(stale_cfg, 120)
    reader_cpu = read_result["client_cpu_cores"]
    writer_cpu = write_result["client_cpu_cores"] if write_result else None
    if write_result is None:
        client_cpu = reader_cpu
    else:
        measured_seconds = max(write_result["seconds"], read_result["seconds"] + 3)
        client_cpu = (reader_cpu * read_result["seconds"] +
                      writer_cpu * write_result["seconds"]) / measured_seconds
    row = {"phase": "mixed", "repeat": repeat, "stack": stack,
           "op": op, "key_dist": key_dist, "key_space": KEY_SPACE,
           "read_clients": 64, "read_batch": 1, "seconds": seconds,
           "writer_rate": "none" if writer_rate is None else ("unlimited" if writer_rate == 0 else writer_rate),
           "writer_clients": writer_clients if write_result else 0,
           "synchronous_commit": "off" if write_result else None,
           "read": read_result, "read_requests_s": read_result["requests_s"],
           "requests_per_server_core": round(read_result["requests_s"] / len(cpu_ids(SERVER_CPUS)), 2)
           if SERVER_CPUS else None,
           "read_p50_ms": read_result["latency"]["p50_ms"],
           "read_p99_ms": read_result["latency"]["p99_ms"],
           "cache_hits": hits, "cache_misses": misses, "hit_ratio": hit_ratio,
           "writer": write_result,
           "writer_tps": write_result["tx_s"] if write_result else 0,
           "writer_p99_ms": write_result["latency"]["p99_ms"] if write_result else None,
           "writer_errors": write_result["errors"] if write_result else 0,
           "stale_check": stale, "client_cpu_cores": client_cpu,
           "reader_client_cpu_cores": reader_cpu,
           "writer_client_cpu_cores": writer_cpu,
           **db_cpu}
    output.write(json.dumps(row, separators=(",", ":")) + "\n")
    output.flush()
    print(f"mixed {stack:6} {op:6} {key_dist:7} rate={row['writer_rate']:>9} "
          f"read={row['read_requests_s']:9.0f}/s p99={row['read_p99_ms']:.3f}ms "
          f"hit={row['hit_ratio']} write={row['writer_tps']:.0f}/s "
          f"write_p99={row['writer_p99_ms']}ms DB={row['db_cpu_cores']} "
          f"stale={stale['stale'] if stale else 'n/a'}", flush=True)
    if op == "insert":
        cleanup_insert(table)


def overhead_cases(token, output):
    ops = values("WRITE_OPS", "update,insert")
    clients = values("WRITE_CLIENTS", "8,32,64", int)
    syncs = values("SYNC_COMMITS", "on,off")
    stacks = values("WRITE_STACKS", "plain,pglc,plain+valkey-del")
    cases = list(itertools.product(ops, clients, syncs, stacks))
    for repeat in range(REPEATS):
        offset = (repeat * max(1, len(cases) // REPEATS)) % len(cases)
        for op, count, sync, stack in cases[offset:] + cases[:offset]:
            overhead_case(token, output, repeat, op, count, sync, stack, WRITE_SECONDS)


def mixed_rate():
    parsed = []
    for value in values("MIXED_RATES", "none,1000,10000,30000,unlimited"):
        lower = str(value).lower()
        if lower in ("none", "0"):
            parsed.append(None)
        elif lower == "unlimited":
            parsed.append(0)
        else:
            parsed.append(int(value))
    return parsed


def mixed_cases(token, output):
    stacks = values("MIXED_STACKS", "pglc,valkey,sql")
    distributions = values("MIXED_KEY_DISTS", "uniform,zipf")
    rates = mixed_rate()
    for repeat in range(REPEATS):
        for stack, key_dist, rate in itertools.product(stacks, distributions, rates):
            mixed_case(token, output, repeat, stack, key_dist, rate, "update", MIXED_SECONDS)
        for stack in stacks:
            mixed_case(token, output, repeat, stack, "uniform", 0, "insert", MIXED_SECONDS)


def main():
    global DB_NPROC
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} OUTPUT.jsonl")
    if SERVER_CPUS:
        cpu_ids(SERVER_CPUS)
    ensure_check_rows()
    DB_NPROC = host_nproc(DB)
    token = ssh(LOAD, f"cat {shlex.quote(TOKEN_FILE)}").strip()
    try:
        if SERVER_CPUS:
            set_server_cpus(SERVER_CPUS)
        with open(sys.argv[1], "a", encoding="utf-8") as output:
            if os.environ.get("RUN_WRITE_OVERHEAD", "1") != "0":
                overhead_cases(token, output)
            if os.environ.get("RUN_MIXED", "1") != "0":
                mixed_cases(token, output)
    finally:
        if SERVER_CPUS:
            reset_server_cpus()


if __name__ == "__main__":
    main()
