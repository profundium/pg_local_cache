import assert from 'node:assert/strict';
import test from 'node:test';
import { getRows } from './queries.mjs';

const a = '{"id":1,"value":"one"}';
const b = '{"id":2,"value":"two"}';
const expected = [JSON.parse(b), JSON.parse(a), JSON.parse(b), null, null];

test('mget preserves positions and decodes each row', async () => {
  const client = { query: async query => {
    assert.deepEqual(query.values, [[2, 1, 2, null, 9]]);
    assert.match(query.text, /\$1::bigint\[\]/);
    return { rows: [{ rows: [b, a, b, null, null] }] };
  } };
  assert.deepEqual(await getRows(client, [2, 1, 2, null, 9]), expected);
});

test('ANY baseline restores order, duplicates and nulls', async () => {
  const client = { query: async () => ({ rows: [{ key: '1', row: a }, { key: '2', row: b }] }) };
  assert.deepEqual(await getRows(client, [2, 1, 2, null, 9], false), expected);
});

test('invalid keys do not reach the database', async () => {
  const client = { query: () => { throw new Error('unexpected query'); } };
  assert.deepEqual(await getRows(client, []), []);
  for (const keys of [[1.5], ['1'], [undefined], [Number.MAX_SAFE_INTEGER + 1]]) {
    await assert.rejects(getRows(client, keys), TypeError);
  }
  await assert.rejects(getRows(client, Array(1025).fill(1)), RangeError);
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
