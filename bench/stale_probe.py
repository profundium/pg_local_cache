#!/usr/bin/env python3
"""Run a concurrent reader and unlimited writer, then count stale cache rows.

Usage: stale_probe.py pglc|valkey uniform|zipf SECONDS
"""
import json
import os
import shlex
import subprocess
import sys
import threading

SSH = os.environ.get("PGLC_SSH", "ssh")
DB = os.environ.get("PGLC_DB_HOST", "pglc-db")
LOAD = os.environ.get("PGLC_LOAD_HOST", "pglc-load")
DB_ADDR = os.environ.get("PGLC_DB_ADDR", "192.168.0.4")
TOKEN_FILE = os.environ.get("PGLC_TOKEN_FILE", "/root/pglc.token")
KEY_SPACE = 60_000
CHECK_IDS = list(range(900_000_001, 900_000_009))
ANY_SQL = ("SELECT id::text AS key, row_to_json(i)::text AS row "
           "FROM public.{table} AS i WHERE id = ANY($1::bigint[])")


def ssh(host, command, stdin=None, timeout=600):
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


def config(token, mode, seconds, read_table, resp_port, key_dist, **extra):
    return {"clients": 64, "batch": 1, "seconds": seconds, "mode": mode,
            "any_sql": ANY_SQL.format(table=read_table), "port": 5432,
            "resp_port": resp_port, "resp_token": token,
            "key_space": KEY_SPACE, "key_dist": key_dist,
            "check_ids": CHECK_IDS, **extra}


def run_client(cfg):
    command = f"PGLC_BENCH_HOST={shlex.quote(DB_ADDR)} /root/bench-client"
    output = ssh(LOAD, command,
                 stdin=json.dumps(cfg, separators=(",", ":")) + "\ngo\n\n",
                 timeout=cfg["seconds"] + 120)
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "result" in value:
            return value["result"]
    raise RuntimeError("bench client returned no result JSON")


def capture(cfg, result):
    try:
        result.append(run_client(cfg))
    except Exception as error:
        result.append({"error": str(error)})


def main():
    if len(sys.argv) != 4:
        raise ValueError(f"usage: {sys.argv[0]} pglc|valkey uniform|zipf SECONDS")
    stack, key_dist, seconds_text = sys.argv[1:]
    if stack not in ("pglc", "valkey"):
        raise ValueError("stack must be pglc or valkey")
    if key_dist not in ("uniform", "zipf"):
        raise ValueError("key_dist must be uniform or zipf")
    seconds = int(seconds_text)
    if not 1 <= seconds <= 120:
        raise ValueError("SECONDS must be between 1 and 120")

    ensure_check_rows()
    token = ssh(LOAD, f"cat {shlex.quote(TOKEN_FILE)}").strip()
    if stack == "valkey":
        flush_valkey(token)
        table, read_mode, resp_port = "direct_items", "valkey-aside", 6379
    else:
        table, read_mode, resp_port = "items", "resp-mget", 6380

    writer_result = []
    reader_result = []
    writer = threading.Thread(
        target=capture,
        args=(config(token, "write", seconds, table, 6379, key_dist, op="update_value",
                     table=table, rate=0, synchronous_commit="off",
                     insert_start=200001, valkey_del=(stack == "valkey")),
              writer_result))
    writer.start()
    capture(config(token, read_mode, seconds, table, resp_port, key_dist), reader_result)
    writer.join()

    stale_result = []
    capture(config(token, "stale-check", 1, table, resp_port, key_dist,
                   target=stack), stale_result)
    reader, writer, stale = reader_result[0], writer_result[0], stale_result[0]
    errors = [value["error"] for value in (reader, writer, stale) if "error" in value]
    result = {"stack": stack, "dist": key_dist, "seconds": seconds,
              "reads_s": round(reader.get("requests_s", 0)),
              "read_p99_ms": reader.get("latency", {}).get("p99_ms"),
              "hits": reader.get("cache_hits"), "misses": reader.get("cache_misses"),
              "writes_s": round(writer.get("tx_s", 0)),
              "stale": stale.get("stale"), "cached": stale.get("cached"),
              "examples": stale.get("examples"), "errors": errors}
    print(json.dumps(result, separators=(",", ":")))
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error": str(error)}, separators=(",", ":")))
        raise SystemExit(1)
