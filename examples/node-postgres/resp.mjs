import assert from 'node:assert/strict';
import { createClient } from '@redis/client';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { validateKeys } from './queries.mjs';

export function respClient(port = process.env.PGLC_DEMO_RESP_PORT || 56379) {
  const client = createClient({
    url: `redis://127.0.0.1:${port}`,
    password: 'DemoRespToken_0123456789abcdef0123456789',
    RESP: 2,
    disableClientInfo: true,
    socket: { connectTimeout: 5000, reconnectStrategy: false },
    commandOptions: { timeout: 10000 },
  });
  client.on('error', error => console.error(error.message));
  return client;
}

export async function getRespRows(client, keys) {
  validateKeys(keys);
  const wireKeys = keys.filter(key => key !== null).map(id =>
    `CRUD:pglc_demo.public.items:${JSON.stringify({ id })}`);
  const values = wireKeys.length ? await client.mGet(wireKeys) : [];
  assert.equal(values.length, wireKeys.length);
  let position = 0;
  return keys.map(key => {
    const value = key === null ? null : values[position++];
    return value === null ? null : JSON.parse(value);
  });
}

async function main() {
  const client = respClient();
  try {
    await client.connect();
    const rows = await getRespRows(client, [42, 7, 42, null, 999999]);
    assert.deepEqual(rows.map(row => row?.id ?? null), [42, 7, 42, null, null]);
    console.log(JSON.stringify(rows, null, 2));
  } finally {
    if (client.isOpen) await client.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => { console.error(error); process.exitCode = 1; });
}
