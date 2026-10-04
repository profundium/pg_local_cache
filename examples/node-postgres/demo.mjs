import assert from 'node:assert/strict';
import pg from 'pg';
import { demoConnection, getRows } from './queries.mjs';
import { getRespRows, respClient } from './resp.mjs';

const reader = respClient();
const writer = new pg.Client(demoConnection());
try {
  await Promise.all([reader.connect(), writer.connect()]);
  const identity = await writer.query('SELECT rolsuper FROM pg_roles WHERE rolname = current_user');
  assert.equal(identity.rows[0].rolsuper, false);
  const keys = [42, 7, 42, null, 999999];
  const expected = await getRespRows(reader, keys);

  // The RESP worker sees committed table state, outside the SQL transaction.
  await writer.query('BEGIN');
  await writer.query('UPDATE public.items SET revision = revision + 1 WHERE id = 42');
  const ownWrite = await getRows(writer, [42]);
  assert.equal(ownWrite[0].revision, expected[0].revision + 1);
  assert.deepEqual(await getRespRows(reader, keys), expected);
  await writer.query('ROLLBACK');
  assert.deepEqual(await getRespRows(reader, keys), expected);

  await writer.query('UPDATE public.items SET revision = revision + 1 WHERE id = 42');
  const committed = await getRespRows(reader, keys);
  assert.equal(committed[0].revision, expected[0].revision + 1);
  assert.deepEqual(committed, await getRespRows(reader, keys));
  console.log('PASS: authenticated RESP reads, order, duplicates, missing keys, SQL read-your-writes, rollback and committed invalidation');
} finally {
  await Promise.allSettled([
    reader.isOpen ? reader.close() : Promise.resolve(),
    writer.end(),
  ]);
}
