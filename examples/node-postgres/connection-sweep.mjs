// Read-only diagnostic on the disposable demo. pgbench receives but does not decode rows.
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { performance } from 'node:perf_hooks';
import { promisify } from 'node:util';
import pg from 'pg';
import { integer, latency } from './benchmark.mjs';
import { demoConnection, getRows, MGET_SQL, ANY_SQL } from './queries.mjs';

const exec = promisify(execFile);
const seconds = integer('DURATION_SECONDS', 5, 120);
const repeats = integer('REPEATS', 3, 20);
const connections = (process.env.CONNECTIONS || '1,4,16,32').split(',').map(Number);
const batches = (process.env.BATCHES || '1,64').split(',').map(Number);
assert.ok(connections.length && new Set(connections).size === connections.length && connections.every(n => Number.isInteger(n) && n >= 1 && n <= 256));
assert.ok(batches.length && new Set(batches).size === batches.length && batches.every(n => [1,16,64].includes(n)));
const connection = demoConnection();
const admin = new pg.Client(demoConnection(true));
const directory = await mkdtemp(join(tmpdir(), 'pglc-sweep-'));
const counters = async () => (await admin.query('SELECT local_cache.stats() AS s')).rows[0].s;
let container;
async function serverCPU() {
  const { stdout } = await exec('docker', ['exec', container.trim(), 'cat', '/sys/fs/cgroup/cpu.stat']);
  const usage = stdout.match(/^usage_usec (\d+)$/m);
  assert.ok(usage, 'cgroup v2 CPU accounting required');
  return Number(usage[1]) / 1e6;
}

async function nodeRun(clients, keys, cached) {
  const pool = Array.from({ length: clients }, () => new pg.Client(connection));
  try {
    await Promise.all(pool.map(client => client.connect()));
    // Prepare and warm every persistent connection before starting the clock.
    await Promise.all(pool.map(client => getRows(client, keys, cached)));
    const times = [];
    let failure;
    const cpuStart = process.cpuUsage();
    const loopStart = performance.eventLoopUtilization();
    const start = performance.now(), end = start + seconds * 1000;
    await Promise.all(pool.map(async client => {
      while (!failure && performance.now() < end) {
        const before = performance.now();
        try { await getRows(client, keys, cached); }
        catch (error) { failure = error; break; }
        times.push(performance.now() - before);
      }
    }));
    const elapsed = (performance.now() - start) / 1000;
    const cpu = process.cpuUsage(cpuStart);
    const loop = performance.eventLoopUtilization(loopStart).utilization;
    if (failure) throw failure;
    return { requests: times.length, seconds: elapsed, requests_s: times.length / elapsed,
      latency: latency(times), client_cpu_cores: (cpu.user + cpu.system) / 1e6 / elapsed,
      event_loop_utilization: loop, threads: 1 };
  } finally {
    await Promise.allSettled(pool.map(client => client.end()));
  }
}

async function pgbenchRun(clients, keys, cached) {
  const script = join(directory, 'read.sql');
  await writeFile(script, (cached ? MGET_SQL : ANY_SQL).replace('$1', ':keys') + ';\n');
  const threads = Math.min(clients, 8);
  const args = ['-h', connection.host, '-p', String(connection.port), '-U', connection.user,
    '-d', connection.database, '-n', '-M', 'prepared', '-c', String(clients), '-j', String(threads),
    '-D', `keys={${keys.join(',')}}`, '-f', script];
  const env = { ...process.env, PGPASSWORD: connection.password, PGSSLMODE: 'disable', LC_ALL: 'C' };
  const { stdout, stderr } = await exec('/usr/bin/time', ['-p', 'pgbench', ...args, '-T', String(seconds)], { env });
  const match = (text, pattern) => { const result = text.match(pattern); assert.ok(result, text); return Number(result[1]); };
  assert.equal(match(stdout, /number of failed transactions:\s+(\d+)/), 0);
  const requests = match(stdout, /number of transactions actually processed:\s+(\d+)/);
  const throughput = match(stdout, /^tps = ([\d.]+)/m);
  const elapsed = requests / throughput;
  const user = match(stderr, /^user\s+([\d.]+)/m), system = match(stderr, /^sys\s+([\d.]+)/m);
  return { requests, seconds: elapsed, requests_s: throughput,
    average_latency_ms: match(stdout, /latency average = ([\d.]+)/),
    initial_connection_ms: match(stdout, /initial connection time = ([\d.]+)/),
    client_cpu_cores: (user + system) / elapsed, threads, stdout, time: stderr };
}

try {
  ({ stdout: container } = await exec('docker', ['compose', '-f', 'examples/compose.yaml', 'ps', '-q', 'postgres']));
  assert.ok(container.trim(), 'start the disposable demo first');
  await admin.connect();
  const environment = (await admin.query(`SELECT current_database() AS database,
    obj_description('public.items'::regclass) AS marker,
    (SELECT extversion FROM pg_extension WHERE extname = 'pg_local_cache') AS extension_version,
    current_setting('server_version') AS postgres_version,
    current_setting('max_connections')::int AS max_connections,
    current_setting('superuser_reserved_connections')::int AS superuser_reserved_connections,
    (SELECT count(*)::int FROM pg_stat_activity WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()) AS other_client_sessions,
    current_setting('shared_buffers') AS shared_buffers,
    current_setting('pg_local_cache.cache_entries') AS cache_entries`)).rows[0];
  assert.equal(environment.database, 'pglc_demo');
  assert.equal(environment.marker, 'pg_local_cache disposable demo');
  assert.match(environment.extension_version, /^2\.0\./);
  assert.equal(environment.other_client_sessions, 0, 'stop other clients of the disposable demo before measuring');
  assert.ok(Math.max(...connections) + 1 < environment.max_connections - environment.superuser_reserved_connections);
  assert.equal((await admin.query('SELECT count(*)::int AS n FROM public.items')).rows[0].n, 4096);
  const verifier = new pg.Client(connection);
  await verifier.connect();
  try {
    const keys = [42,7,42,null,999999];
    assert.deepEqual(await getRows(verifier, keys), await getRows(verifier, keys, false));
    await getRows(verifier, Array.from({ length: 128 }, (_, i) => i + 1), false);
    await getRows(verifier, Array.from({ length: 128 }, (_, i) => i + 1));
  } finally { await verifier.end(); }
  const results = [];
  for (let repeat = 1; repeat <= repeats; repeat++) {
    const drivers = repeat % 2 ? ['node', 'pgbench'] : ['pgbench', 'node'];
    const modes = repeat % 2 ? ['postgres-any', 'mget'] : ['mget', 'postgres-any'];
    const levels = repeat % 2 ? connections : [...connections].reverse();
    for (const batch of batches) for (const clients of levels) for (const driver of drivers) for (const mode of modes) {
      const keys = Array.from({ length: batch }, (_, i) => i + 1);
      const before = await counters(admin), cpuBefore = await serverCPU();
      const started = performance.now();
      const result = await (driver === 'node' ? nodeRun : pgbenchRun)(clients, keys, mode === 'mget');
      const cpuAfter = await serverCPU(), cpuWindow = (performance.now() - started) / 1000;
      const after = await counters(admin);
      const cache = Object.fromEntries(['sql_cache_hits','sql_cache_misses','sql_cache_fills','sql_cache_bypasses'].map(key => [key, Number(after[key]) - Number(before[key])]));
      assert.ok(Object.values(cache).every(n => Number.isFinite(n) && n >= 0));
      assert.ok(result.requests > 0 && Number.isFinite(result.requests_s) && result.requests_s > 0);
      assert.ok(cpuAfter >= cpuBefore);
      if (mode === 'mget') {
        assert.ok(cache.sql_cache_hits >= result.requests * batch);
        assert.equal(cache.sql_cache_misses + cache.sql_cache_fills + cache.sql_cache_bypasses, 0);
      }
      const row = { repeat, batch, clients, driver, mode, ...result,
        server_cpu_cores: (cpuAfter - cpuBefore) / cpuWindow, server_cpu_seconds: cpuAfter - cpuBefore,
        server_cpu_window_seconds: cpuWindow, counters: cache };
      results.push(row);
      console.error(`${repeat}: batch=${batch} clients=${clients} ${driver}/${mode}: ${result.requests_s.toFixed(0)} req/s, client=${result.client_cpu_cores.toFixed(2)} CPU cores, server=${row.server_cpu_cores.toFixed(2)} cores`);
    }
  }
  const { stdout: revision } = await exec('git', ['rev-parse', 'HEAD']);
  const { stdout: version } = await exec('pgbench', ['--version']);
  console.log(JSON.stringify({ schema: 1, measured_at: new Date().toISOString(), harness_ref: revision.trim(),
    environment: { ...environment, node: process.version, pgbench: version.trim(), driver_version: JSON.parse(await readFile(new URL('./node_modules/pg/package.json', import.meta.url))).version },
    workload: { seconds, repeats, connections, batches, fixed_keys: true, persistent_connections: true,
      protocol: 'prepared', transport: `127.0.0.1:${connection.port}`, admin_connections: 1,
      node_decodes_and_normalizes_rows: true, pgbench_discards_results: true,
      server_cpu_includes_connection_setup_and_counter_sampling: true }, results }, null, 2));
} finally {
  await admin.end();
  await rm(directory, { recursive: true, force: true });
}
