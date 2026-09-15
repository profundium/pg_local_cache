import assert from 'node:assert/strict';
import { createClient } from '@redis/client';

const client = createClient({
  url: `redis://127.0.0.1:${process.env.PGLC_DEMO_RESP_PORT || 56379}`,
  password: 'DemoRespToken_0123456789abcdef0123456789',
  RESP: 2,
  disableClientInfo: true,
  socket: { connectTimeout: 5000, reconnectStrategy: false },
  commandOptions: { timeout: 5000 },
});
client.on('error', error => console.error(error.message));

try {
  await client.connect();
  const keys = [42, 7, 42, 999999].map(id =>
    `CRUD:pglc_demo.public.items:${JSON.stringify({ id })}`
  );
  const values = await client.mGet(keys);
  const rows = values.map(value => value === null ? null : JSON.parse(value));
  assert.deepEqual(rows.map(row => row?.id ?? null), [42, 7, 42, null]);
  console.log(JSON.stringify(rows, null, 2));
} finally {
  if (client.isOpen) await client.close();
}
