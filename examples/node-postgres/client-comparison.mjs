// Application clients: every timed request decodes JSON and restores key positions.
import assert from 'node:assert/strict';
import { spawn, execFile } from 'node:child_process';
import { createInterface } from 'node:readline';
import { readFile } from 'node:fs/promises';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';
import pg from 'pg';
import { integer, latency } from './benchmark.mjs';
import { demoConnection, getRows, MGET_SQL, MGET_TEXT_SQL, ANY_SQL } from './queries.mjs';
import { startResources } from './server-resources.mjs';
import { respClient, getRespRows } from './resp.mjs';

const exec = promisify(execFile);
const workerFile = fileURLToPath(import.meta.url);

async function nodeWorker() {
  const lines = createInterface({ input: process.stdin })[Symbol.asyncIterator]();
  const config = JSON.parse((await lines.next()).value);
  assert.ok(Number.isInteger(config.clients) && config.clients >= 1 && config.clients <= 256);
  assert.ok([1, 16, 64].includes(config.batch));
  assert.ok(Number.isInteger(config.seconds) && config.seconds >= 1 && config.seconds <= 120);
  assert.ok(['mget', 'postgres-any', 'resp-mget'].includes(config.mode));
  assert.equal(config.driver, 'node-json');
  const resp = config.mode === 'resp-mget';
  const connection = { ...demoConnection(), port: config.port, application_name: 'pglc-node-benchmark' };
  const clients = Array.from({ length: config.clients }, () => resp ? respClient(config.resp_port) : new pg.Client(connection));
  const read = (client, keys) => resp ? getRespRows(client, keys) : getRows(client, keys, config.mode === 'mget');
  try {
    await Promise.all(clients.map(client => client.connect()));
    const check = new pg.Client(connection);
    try {
      await check.connect();
      for (const keys of [[42, 7, 42, null, 999999], [], [null, null]]) {
        assert.deepEqual(await read(clients[0], keys), await getRows(check, keys, false));
      }
    } finally { await check.end(); }
    const keys = Array.from({ length: config.batch }, (_, i) => i + 1);
    await Promise.all(clients.map(client => read(client, keys)));
    console.log(JSON.stringify({ ready: true, result_formats: resp ? 'RESP2 bulk JSON strings' : 'text', node: process.version }));
    assert.equal((await lines.next()).value, 'go');
    const times = [];
    let failure;
    const cpuBefore = process.cpuUsage(), loopBefore = performance.eventLoopUtilization();
    const start = performance.now(), end = start + config.seconds * 1000;
    await Promise.all(clients.map(async client => {
      while (!failure && performance.now() < end) {
        const before = performance.now();
        try { await read(client, keys); times.push(performance.now() - before); }
        catch (error) { failure = error; }
      }
    }));
    const seconds = (performance.now() - start) / 1000, cpu = process.cpuUsage(cpuBefore);
    const eventLoop = performance.eventLoopUtilization(loopBefore).utilization;
    if (failure) throw failure;
    console.log(JSON.stringify({ result: { requests: times.length, seconds, requests_s: times.length / seconds,
      latency: latency(times), client_cpu_cores: (cpu.user + cpu.system) / 1e6 / seconds,
      event_loop_utilization: eventLoop, threads: 1 } }));
    assert.equal((await lines.next()).value, 'done');
  } finally { await Promise.allSettled(clients.map(client => resp ? (client.isOpen && client.close()) : client.end())); }
}

async function runClient(admin, config) {
  const clientContainer = process.env.PGLC_CLIENT_CONTAINER;
  const command = config.driver === 'go-pgx'
    ? [clientContainer ? '/bench-bin/pglc-go-pgx' : process.env.PGLC_PGX_BIN || '/tmp/pglc-go-pgx']
    : [clientContainer ? 'node' : process.execPath, clientContainer ? '/bench/client-comparison.mjs' : workerFile, '--worker'];
  const child = clientContainer
    ? spawn('docker', ['exec', '-i', '-e', `GOMAXPROCS=${process.env.GOMAXPROCS || 8}`, clientContainer, 'timeout', String(config.seconds + 50), ...command], { stdio: ['pipe', 'pipe', 'inherit'] })
    : spawn(command[0], command.slice(1), { stdio: ['pipe', 'pipe', 'inherit'] });
  const exited = new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('exit', (code, signal) => code === 0 ? resolve() : reject(new Error(`client failed: ${code ?? signal}`)));
  });
  // Attach a handler immediately, including failures before the first output line.
  exited.catch(() => {});
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  const message = async () => {
    const line = await Promise.race([lines.next(), exited.then(() => { throw new Error('client exited before result'); })]);
    assert.ok(!line.done, 'client output ended'); return JSON.parse(line.value);
  };
  const watchdog = setTimeout(() => child.kill(), (config.seconds + 60) * 1000);
  let finish;
  try {
    child.stdin.write(JSON.stringify({ ...config, port: clientContainer ? 5432 : demoConnection().port, mget_sql: MGET_TEXT_SQL, any_sql: ANY_SQL,
      resp_port: clientContainer ? 6380 : integer('PGLC_DEMO_RESP_PORT', 56379, 65535),
      resp_token: 'DemoRespToken_0123456789abcdef0123456789' }) + '\n');
    const ready = await message(); assert.equal(ready.ready, true);
    const before = (await admin.query('SELECT local_cache.stats() AS s')).rows[0].s;
    finish = await startResources(admin);
    child.stdin.write('go\n');
    const { result } = await message();
    assert.ok(result.requests > 0 && Number.isFinite(result.requests_s));
    const stop = finish; finish = null;
    const server = await stop(result.requests);
    const after = (await admin.query('SELECT local_cache.stats() AS s')).rows[0].s;
    const counters = Object.fromEntries(['sql_cache_hits', 'sql_cache_misses', 'sql_cache_fills', 'sql_cache_bypasses',
      'cache_hit', 'cache_miss', 'client_mget_keys', 'client_request_errors', 'client_limit_rejections', 'database_reads']
      .map(key => [key, Number(after[key]) - Number(before[key])]));
    for (const value of Object.values(counters)) assert.ok(Number.isFinite(value) && value >= 0);
    if (config.mode === 'mget') {
      assert.equal(counters.sql_cache_hits, result.requests * config.batch);
      assert.equal(counters.sql_cache_misses + counters.sql_cache_fills + counters.sql_cache_bypasses, 0);
    }
    if (config.mode === 'resp-mget') {
      assert.equal(counters.cache_hit, result.requests * config.batch);
      assert.equal(counters.client_mget_keys, result.requests * config.batch);
      assert.equal(counters.cache_miss + counters.database_reads + counters.client_request_errors + counters.client_limit_rejections, 0);
    }
    child.stdin.end('done\n');
    await exited;
    return { ...config, ...result, client_metadata: ready, counters, server };
  } finally {
    clearTimeout(watchdog);
    if (finish) await finish(0).catch(() => {});
    if (child.exitCode === null) child.kill();
  }
}

async function main() {
  const benchmarkClient = process.env.BENCHMARK_CLIENT || 'all';
  assert.ok(['all', 'node', 'go'].includes(benchmarkClient), 'BENCHMARK_CLIENT must be all, node or go');
  const seconds = integer('DURATION_SECONDS', 5, 120), repeats = integer('REPEATS', 3, 20);
  const connections = (process.env.CONNECTIONS || '4,64,256').split(',').map(Number);
  const batches = (process.env.BATCHES || '1,16,64').split(',').map(Number);
  assert.ok(connections.length && new Set(connections).size === connections.length && connections.every(n => Number.isInteger(n) && n >= 1 && n <= 256));
  assert.ok(batches.length && new Set(batches).size === batches.length && batches.every(n => [1, 16, 64].includes(n)));
  const admin = new pg.Client(demoConnection(true)), results = [];
  let environment, failure;
  try {
    await admin.connect();
    environment = (await admin.query(`SELECT current_database() AS database, obj_description('public.items'::regclass) AS marker,
      (SELECT extversion FROM pg_extension WHERE extname = 'pg_local_cache') AS extension_version,
      current_setting('pg_local_cache.binary_build_id') AS extension_build_id,
      current_setting('server_version') AS postgres_version, current_setting('max_connections')::int AS max_connections,
      current_setting('superuser_reserved_connections')::int AS reserved_connections,
      current_setting('shared_buffers') AS shared_buffers,
      local_cache.health() AS health,
      (SELECT count(*)::int FROM pg_stat_activity WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()) AS other_sessions`)).rows[0];
    assert.equal(environment.database, 'pglc_demo');
    assert.equal(environment.marker, 'pg_local_cache disposable demo');
    assert.match(environment.extension_version, /^2\.0\./);
    assert.equal(environment.other_sessions, 0);
    assert.ok(Math.max(...connections) + 1 < environment.max_connections - environment.reserved_connections);
    assert.ok(environment.health.ready && environment.health.resp_enabled, 'start the RESP demo overlay first');
    assert.ok(Math.max(...connections) < environment.health.max_clients, 'leave RESP connection headroom');
    assert.equal(environment.health.active_clients, 0, 'other RESP clients are connected');
    assert.equal((await admin.query('SELECT count(*)::int AS n FROM public.items')).rows[0].n, 4096);
    await admin.query('SELECT sum(octet_length(value)) FROM public.items');
    await getRows(admin, Array.from({ length: 128 }, (_, i) => i + 1));
    const drivers = benchmarkClient === 'all' ? ['node-json', 'go-pgx'] : [benchmarkClient === 'go' ? 'go-pgx' : 'node-json'];
    const variants = drivers.flatMap(driver => ['postgres-any', 'mget', 'resp-mget'].map(mode => [driver, mode]));
    for (let repeat = 1; repeat <= repeats; repeat++) {
      // Rotate the first client and reverse connection/batch order between repetitions.
      const first = (repeat - 1) % variants.length;
      const order = [...variants.slice(first), ...variants.slice(0, first)];
      const levels = repeat % 2 ? connections : [...connections].reverse();
      const sizes = repeat % 2 ? batches : [...batches].reverse();
      for (const batch of sizes) for (const clients of levels) for (const [driver, mode] of order) {
        const row = await runClient(admin, { repeat, batch, clients, driver, mode, seconds });
        results.push(row);
        console.error(`${repeat}: batch=${batch} clients=${clients} ${driver}/${mode}: ${row.requests_s.toFixed(0)} req/s; server=${row.server.cpu_cores.toFixed(2)} cores, memory peak=${(row.server.memory_peak_bytes / 2**20).toFixed(1)} MiB`);
      }
    }
  } catch (error) { failure = error; }
  finally { await admin.end(); }
  const { stdout: revision } = await exec('git', ['rev-parse', 'HEAD']);
  const { stdout: changes } = await exec('git', ['status', '--porcelain', '--', 'examples/benchmark.sh', 'examples/node-postgres', 'examples/go-pgx']);
  const pgVersion = JSON.parse(await readFile(new URL('./node_modules/pg/package.json', import.meta.url))).version;
  const redisVersion = JSON.parse(await readFile(new URL('./node_modules/@redis/client/package.json', import.meta.url))).version;
  console.log(JSON.stringify({ schema: 1, measured_at: new Date().toISOString(), harness_ref: revision.trim() + (changes ? '-dirty' : ''),
    extension_ref: environment?.extension_build_id === 'local' ? null : environment?.extension_build_id ?? null,
    environment: { ...environment, controller_node: process.version, pg: pgVersion, node_redis: redisVersion,
      client_placement: process.env.PGLC_CLIENT_CONTAINER ? 'Linux VM, same separate client container, server network namespace' : 'host through Docker published ports' },
    workload: { seconds, repeats, connections, batches, fixed_keys: true, persistent_connections: true,
      protocol: 'prepared SQL and RESP2 MGET, no pipelining', all_clients_decode_json_and_restore_positions: true, server_sample_interval_ms: 500 },
    queries: { mget_text: MGET_TEXT_SQL, mget_json: MGET_SQL, postgres_any: ANY_SQL,
      resp_mget: 'MGET CRUD:pglc_demo.public.items:{"id":1} ...' },
    results, ...(failure ? { error: { message: failure.message } } : {}) }, null, 2));
  if (failure) throw failure;
}

(process.argv[2] === '--worker' ? nodeWorker() : main()).catch(error => { console.error(error); process.exitCode = 1; });
