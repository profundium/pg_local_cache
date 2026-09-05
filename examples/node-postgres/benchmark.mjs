import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { cpus, platform, arch } from 'node:os';
import { performance } from 'node:perf_hooks';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { demoConnection, getRows } from './queries.mjs';

function integer(name, fallback, max) {
  const value = Number(process.env[name] || fallback);
  if (!Number.isInteger(value) || value < 1 || value > max) {
    throw new RangeError(`${name} must be an integer between 1 and ${max}`);
  }
  return value;
}

export function latency(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const percentile = p => sorted[Math.ceil(sorted.length * p) - 1];
  return { samples: sorted.length, p50_ms: percentile(0.50), p95_ms: percentile(0.95), p99_ms: percentile(0.99) };
}

export function keysFor(request, batch, cold = false) {
  return Array.from({ length: batch }, (_, i) => cold ? request * batch + i + 1 : (request * batch + i) % 128 + 1);
}

function gitRevision() {
  try { return execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim(); }
  catch { return null; }
}

async function counters(admin) {
  return (await admin.query('SELECT local_cache.stats() AS stats')).rows[0].stats;
}

async function run(clients, admin, mode, workload, batch, requests) {
  const cached = mode === 'mget';
  const cold = workload === 'cold-fill';
  const writeOnly = workload.startsWith('writes-');
  const count = cold ? 4096 / batch : requests;
  const table = workload === 'writes-unattached' ? 'public.direct_items' : 'public.items';

  // Reset only the disposable demo tables; this is outside the timed region.
  await admin.query('TRUNCATE public.items, public.direct_items');
  await admin.query("INSERT INTO public.items (id, value) SELECT id, repeat(md5(id::text), 4) FROM generate_series(1, 4096) AS id");
  await admin.query('INSERT INTO public.direct_items SELECT * FROM public.items');
  await admin.query('ANALYZE public.items');
  await admin.query('ANALYZE public.direct_items');
  await admin.query('SELECT sum(octet_length(value)) FROM public.items');

  // Warm the source pages and each connection's prepared statements outside timing.
  for (const client of clients) {
    await getRows(client, Array.from({ length: 128 }, (_, i) => i + 1), false);
    await getRows(client, [1], cached);
  }
  if (cold) {
    await admin.query("SELECT local_cache.invalidate('public.items')");
  } else if (cached) {
    await getRows(clients[0], Array.from({ length: 128 }, (_, i) => i + 1));
  }
  const before = await counters(admin);
  const reads = [], writes = [];
  let next = 0, readKeys = 0, failure;
  const started = performance.now();
  await Promise.all(clients.map(async client => {
    while (!failure) {
      const request = next++;
      if (request >= count) break;
      const write = writeOnly || (workload === 'mixed-5pct' && request % 20 === 19);
      const start = performance.now();
      try {
        if (write) {
          await client.query({
            name: `update-${table}`,
            text: `UPDATE ${table} SET revision = revision + 1 WHERE id = $1`,
            values: [(request * 17) % 128 + 1],
          });
          writes.push(performance.now() - start);
        } else {
          await getRows(client, keysFor(request, batch, cold), cached);
          reads.push(performance.now() - start);
          readKeys += batch;
        }
      } catch (error) { failure = error; }
    }
  }));
  const seconds = (performance.now() - started) / 1000;
  if (failure) throw failure;
  const after = await counters(admin);
  const stats = Object.fromEntries(['sql_cache_hits', 'sql_cache_misses', 'sql_cache_bypasses', 'sql_cache_fills']
    .map(key => [key, Number(after[key]) - Number(before[key])]));
  assert.ok(Object.values(stats).every(Number.isFinite), 'missing SQL cache counters');
  assert.equal(reads.length + writes.length, count);
  if (cached && workload === 'warm') assert.ok(stats.sql_cache_hits > 0, 'warm run did not hit the cache');
  if (cached && cold) assert.ok(stats.sql_cache_misses > 0, 'cold run did not miss the cache');
  return {
    mode: writeOnly ? 'UPDATE' : mode, workload, batch, seconds,
    requests: count, requests_s: count / seconds,
    read_requests: reads.length, write_requests: writes.length,
    requested_read_keys: readKeys, requested_read_keys_s: readKeys / seconds,
    read_latency: latency(reads), write_latency: latency(writes), counters: stats,
  };
}

async function main() {
  const { default: pg } = await import('pg');
  const concurrency = integer('CLIENTS', 4, 64);
  const requests = integer('REQUESTS', 2000, 100000);
  const repeats = integer('REPEATS', 3, 20);
  const batches = (process.env.BATCHES || '1,16,64').split(',').map(Number);
  if (!batches.length || batches.some(batch => ![1, 16, 64].includes(batch)) || new Set(batches).size !== batches.length) {
    throw new RangeError('BATCHES must contain distinct values from 1,16,64');
  }
  const admin = new pg.Client(demoConnection(true));
  const clients = Array.from({ length: concurrency }, () => new pg.Client(demoConnection()));
  try {
    await Promise.all([admin.connect(), ...clients.map(client => client.connect())]);
    const setup = (await admin.query(`SELECT current_database() AS database,
      obj_description('public.items'::regclass) AS marker,
      obj_description('public.direct_items'::regclass) AS direct_marker,
      (SELECT extversion FROM pg_extension WHERE extname = 'pg_local_cache') AS extension_version,
      current_setting('server_version') AS postgres_version,
      current_setting('shared_buffers') AS shared_buffers,
      current_setting('pg_local_cache.cache_entries') AS cache_entries,
      current_setting('pg_local_cache.memory_budget_mb') AS memory_budget_mb`)).rows[0];
    assert.equal(setup.database, 'pglc_demo');
    assert.equal(setup.marker, 'pg_local_cache disposable demo');
    assert.equal(setup.direct_marker, 'pg_local_cache disposable demo');
    assert.match(setup.extension_version, /^2\.0\./);
    const rowCount = (await admin.query('SELECT count(*)::int AS n FROM public.items')).rows[0].n;
    assert.equal(rowCount, 4096, 'run against the unmodified demo dataset');
    const edgeKeys = [42, 7, 42, null, 999999];
    assert.deepEqual(await getRows(clients[0], edgeKeys), await getRows(clients[0], edgeKeys, false));
    // Scan all rows once: cold-fill means a cold row cache, not cold PostgreSQL pages.
    await admin.query('SELECT sum(octet_length(value)) FROM public.items');
    const health = (await admin.query('SELECT local_cache.health() AS health')).rows[0].health;
    const results = [];
    for (let repeat = 1; repeat <= repeats; repeat++) {
      const modes = repeat % 2 ? ['postgres-any', 'mget'] : ['mget', 'postgres-any'];
      for (const batch of batches) {
        for (const workload of ['warm', 'cold-fill', 'mixed-5pct']) {
          for (const mode of modes) {
            console.error(`repeat ${repeat}: ${workload}, batch ${batch}, ${mode}`);
            results.push({ repeat, ...await run(clients, admin, mode, workload, batch, requests) });
          }
        }
      }
      const writeCases = repeat % 2 ? ['writes-unattached', 'writes-attached'] : ['writes-attached', 'writes-unattached'];
      for (const workload of writeCases) {
        results.push({ repeat, ...await run(clients, admin, 'postgres-any', workload, 1, requests) });
      }
    }
    console.log(JSON.stringify({
      schema: 1, measured_at: new Date().toISOString(),
      extension_ref: '8569a937abb9ba1859ffb9c2a4dbc34f076fbe20',
      harness_ref: process.env.PGLC_HARNESS_REF || gitRevision(),
      environment: { ...setup, health, node: process.version, client_os: platform(), client_arch: arch(), cpu: cpus()[0]?.model, visible_cpus: cpus().length },
      workload: { concurrency, requests_per_sample: requests, repeats, batches, rows: 4096, hot_rows: 128, value_bytes: 128, protocol: 'prepared statements', transport: 'loopback TCP', closed_loop: true },
      results,
    }, null, 2));
  } finally {
    await Promise.allSettled([admin.end(), ...clients.map(client => client.end())]);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => { console.error(error); process.exitCode = 1; });
}
