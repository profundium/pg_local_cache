// Both paths return one object (or null) per requested key.
export async function getRows(client, keys, cached = true) {
  if (!Array.isArray(keys) || keys.length > 1024) {
    throw new RangeError('Expected an array of at most 1024 keys');
  }
  if (keys.some(key => key !== null && !Number.isSafeInteger(key))) {
    throw new TypeError('This example accepts safe integer keys or null');
  }
  if (!keys.length) return [];

  if (cached) {
    const result = await client.query({
      name: 'demo-mget',
      text: "SELECT local_cache.mget('public.items'::regclass, $1::bigint[]) AS rows",
      values: [keys],
    });
    return result.rows[0].rows.map(row => row === null ? null : JSON.parse(row));
  }

  const result = await client.query({
    name: 'demo-any',
    text: 'SELECT id::text AS key, row_to_json(i)::text AS row FROM public.items AS i WHERE id = ANY($1::bigint[])',
    values: [keys],
  });
  const rows = new Map(result.rows.map(row => [row.key, row.row]));
  return keys.map(key => {
    const row = rows.get(String(key));
    return row === undefined ? null : JSON.parse(row);
  });
}

export function demoConnection(admin = false) {
  const port = Number(process.env.PGLC_DEMO_PORT || 55432);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new RangeError('PGLC_DEMO_PORT must be a TCP port');
  }
  return {
    host: '127.0.0.1', port, database: 'pglc_demo',
    user: admin ? 'postgres' : 'demo', password: 'demo-only',
    connectionTimeoutMillis: 5000, statement_timeout: 10000,
  };
}
