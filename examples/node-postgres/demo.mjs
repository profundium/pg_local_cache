import assert from 'node:assert/strict';
import pg from 'pg';
import { demoConnection, getRows } from './queries.mjs';

const admin = new pg.Client(demoConnection(true));
const reader = new pg.Client(demoConnection());
const writer = new pg.Client(demoConnection());
try {
  await Promise.all([admin.connect(), reader.connect(), writer.connect()]);
  const identity = await reader.query('SELECT rolsuper FROM pg_roles WHERE rolname = current_user');
  assert.equal(identity.rows[0].rolsuper, false);
  const keys = [42, 7, 42, null, 999999];
  const expected = await getRows(reader, keys, false);
  await getRows(reader, keys);
  const before = (await admin.query('SELECT local_cache.stats() AS stats')).rows[0].stats;
  assert.deepEqual(await getRows(reader, keys), expected);
  const after = (await admin.query('SELECT local_cache.stats() AS stats')).rows[0].stats;
  assert.ok(Number(after.sql_cache_hits) > Number(before.sql_cache_hits), 'second read must hit the cache');

  // A separate connection must not see an uncommitted update.
  await writer.query('BEGIN');
  await writer.query('UPDATE public.items SET revision = revision + 1 WHERE id = 42');
  assert.deepEqual(await getRows(reader, keys), expected);
  const ownWrite = await getRows(writer, [42]);
  assert.equal(ownWrite[0].revision, expected[0].revision + 1);
  await writer.query('ROLLBACK');
  assert.deepEqual(await getRows(reader, keys), expected);

  await writer.query('UPDATE public.items SET revision = revision + 1 WHERE id = 42');
  const committed = await getRows(reader, keys);
  assert.equal(committed[0].revision, expected[0].revision + 1);
  assert.deepEqual(committed, await getRows(reader, keys, false));
  console.log('PASS: non-superuser reads, warm hit, order, duplicates, missing keys, read-your-writes, rollback and commit');
} finally {
  await Promise.allSettled([admin.end(), reader.end(), writer.end()]);
}
