import assert from 'node:assert/strict';
import test from 'node:test';
import { getRows } from './queries.mjs';
import { getRespRows } from './resp.mjs';

const a = '{"id":1,"value":"one"}';
const b = '{"id":2,"value":"two"}';
const expected = [JSON.parse(b), JSON.parse(a), JSON.parse(b), null, null];

test('mget preserves positions and decodes each row', async () => {
  const seen = [];
  const client = { query: async query => {
    assert.match(query.text, /\$1::bigint\[\]/);
    const keys = query.values[0];
    seen.push(keys);
    return { rows: [{ rows: keys.map(key => key === 1 ? a : key === 2 ? b : null) }] };
  } };
  assert.deepEqual(await getRows(client, []), []);
  assert.deepEqual(await getRows(client, [null, null]), [null, null]);
  assert.deepEqual(await getRows(client, [2, 1, 2, null, 9]), expected);
  assert.deepEqual(seen, [[null, null], [2, 1, 2, null, 9]]);
});

test('ANY baseline restores order, duplicates and nulls', async () => {
  const seen = [];
  const client = { query: async query => {
    assert.match(query.text, /id = ANY/);
    seen.push(query.values[0]);
    return { rows: [{ key: '1', row: a }, { key: '2', row: b }] };
  } };
  assert.deepEqual(await getRows(client, [], false), []);
  assert.deepEqual(await getRows(client, [null, null], false), [null, null]);
  assert.deepEqual(await getRows(client, [2, 1, 2, null, 9], false), expected);
  assert.deepEqual(seen, [[null, null], [2, 1, 2, null, 9]]);
});

test('RESP MGET restores order, duplicates and nulls', async () => {
  let calls = 0;
  const client = { mGet: async keys => {
    calls++;
    assert.deepEqual(keys, [
      'CRUD:pglc_demo.public.items:{"id":2}',
      'CRUD:pglc_demo.public.items:{"id":1}',
      'CRUD:pglc_demo.public.items:{"id":2}',
      'CRUD:pglc_demo.public.items:{"id":9}',
    ]);
    return [b, a, b, null];
  } };
  assert.deepEqual(await getRespRows(client, []), []);
  assert.deepEqual(await getRespRows(client, [null, null]), [null, null]);
  assert.deepEqual(await getRespRows(client, [2, 1, 2, null, 9]), expected);
  assert.equal(calls, 1);
});

test('invalid keys do not reach any database path', async () => {
  const sql = { query: () => { throw new Error('unexpected query'); } };
  const resp = { mGet: () => { throw new Error('unexpected MGET'); } };
  for (const keys of [[1.5], ['1'], [undefined], Array(1), [Number.MAX_SAFE_INTEGER + 1]]) {
    await assert.rejects(getRows(sql, keys), TypeError);
    await assert.rejects(getRows(sql, keys, false), TypeError);
    await assert.rejects(getRespRows(resp, keys), TypeError);
  }
  const tooMany = Array(1025).fill(1);
  await assert.rejects(getRows(sql, tooMany), RangeError);
  await assert.rejects(getRows(sql, tooMany, false), RangeError);
  await assert.rejects(getRespRows(resp, tooMany), RangeError);
});

const { latency, keysFor } = await import('./benchmark.mjs');
test('percentiles use nearest rank without mutating input', () => {
  const values = [4, 1, 2, 3];
  assert.deepEqual(latency(values), { samples: 4, p50_ms: 2, p95_ms: 4, p99_ms: 4 });
  assert.deepEqual(values, [4, 1, 2, 3]);
  assert.equal(latency([]), null);
});
test('cold requests visit each row once; warm requests fit in 128 rows', () => {
  for (const batch of [1, 16, 64]) {
    const cold = Array.from({ length: 4096 / batch }, (_, i) => keysFor(i, batch, true)).flat();
    assert.equal(new Set(cold).size, 4096);
    assert.equal(Math.min(...cold), 1);
    assert.equal(Math.max(...cold), 4096);
    assert.ok(keysFor(1000, batch).every(key => key >= 1 && key <= 128));
  }
});

const { parseResources } = await import('./server-resources.mjs');
test('server resources distinguish CPU quota, memory classes, block I/O and network traffic', () => {
  const snapshot = '\ncpu.stat\nusage_usec 2000000\nthrottled_usec 100000\n' +
    '\nmemory.current\n1000\nmemory.stat\nanon 100\nfile 800\nshmem 600\n' +
    '\nmemory.events\noom_kill 0\nio.stat\n8:0 rbytes=10 wbytes=20 rios=1 wios=2\n8:1 rbytes=30 wbytes=40 rios=3 wios=4\n' +
    '\ncpu.max\n200000 100000\ncpuset.cpus.effective\n0-3,6\nmemory.max\nmax\n' +
    '\nnetwork\n lo: 99 0 0 0 0 0 0 0 99 0 0 0 0 0 0 0\n eth0: 50 0 0 0 0 0 0 0 70 0 0 0 0 0 0 0\n';
  const result = parseResources(snapshot);
  assert.equal(result.cpu_seconds, 2);
  assert.equal(result.cpu_capacity, 2);
  assert.equal(result.throttled_seconds, 0.1);
  assert.equal(result.memory_bytes, 1000);
  assert.equal(result.shmem_bytes, 600);
  assert.equal(result.memory_limit_bytes, null);
  assert.equal(result.rbytes, 40);
  assert.equal(result.wbytes, 60);
  assert.equal(result.rx_bytes, 50);
  assert.equal(result.tx_bytes, 70);
  assert.equal(parseResources(snapshot.replace('200000 100000', 'max 100000')).cpu_capacity, 5);
  assert.throws(() => parseResources(snapshot.replace('usage_usec 2000000', '')));
});
