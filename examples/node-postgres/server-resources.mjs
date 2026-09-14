// Sample only the disposable PostgreSQL container. memory.current is not RSS.
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { performance } from 'node:perf_hooks';
import { fileURLToPath } from 'node:url';

const exec = promisify(execFile);
let container;
const files = ['cpu.stat', 'memory.current', 'memory.stat', 'memory.events', 'io.stat', 'cpu.max', 'cpuset.cpus.effective', 'memory.max'];
const command = files.map(file => `printf '\\n${file}\\n'; cat /sys/fs/cgroup/${file}`).join('; ') + "; printf '\\nnetwork\\n'; cat /proc/net/dev";
const pairs = text => Object.fromEntries(text.trim().split('\n').filter(Boolean).map(line => {
  const [key, value] = line.trim().split(/\s+/); return [key, Number(value)];
}));

export function parseResources(stdout) {
  const sections = stdout.split(/\n(cpu\.stat|memory\.current|memory\.stat|memory\.events|io\.stat|cpu\.max|cpuset\.cpus\.effective|memory\.max|network)\n/);
  const values = {};
  for (let i = 1; i < sections.length; i += 2) values[sections[i]] = sections[i + 1].trim();
  const cpu = pairs(values['cpu.stat']), memory = pairs(values['memory.stat']);
  const [quota, period] = values['cpu.max'].split(' ');
  const cpus = values['cpuset.cpus.effective'].split(',').reduce((n, part) => {
    const [a, b = a] = part.split('-').map(Number); return n + b - a + 1;
  }, 0);
  const io = { rbytes: 0, wbytes: 0, rios: 0, wios: 0 };
  for (const field of values['io.stat'].split(/\s+/)) {
    const [key, value] = field.split('='); if (key in io) io[key] += Number(value);
  }
  let rx = 0, tx = 0;
  for (const line of values.network.split('\n')) {
    const [name, data] = line.split(':');
    if (!data || name.trim() === 'lo') continue;
    const fields = data.trim().split(/\s+/).map(Number); rx += fields[0]; tx += fields[8];
  }
  const result = { cpu_seconds: cpu.usage_usec / 1e6, throttled_seconds: (cpu.throttled_usec || 0) / 1e6,
    cpu_capacity: Math.min(cpus, quota === 'max' ? Infinity : Number(quota) / Number(period)),
    memory_bytes: Number(values['memory.current']), anon_bytes: memory.anon, shmem_bytes: memory.shmem,
    file_bytes: memory.file, memory_limit_bytes: values['memory.max'] === 'max' ? null : Number(values['memory.max']),
    memory_events: pairs(values['memory.events']), ...io, rx_bytes: rx, tx_bytes: tx };
  for (const key of ['cpu_seconds', 'cpu_capacity', 'memory_bytes', 'anon_bytes', 'shmem_bytes', 'file_bytes']) {
    assert.ok(Number.isFinite(result[key]) && result[key] >= 0, `missing cgroup metric: ${key}`);
  }
  assert.ok(result.cpu_capacity > 0);
  return result;
}

export async function startResources(admin) {
  if (!container) {
    const { stdout } = await exec('docker', ['compose', '-f', fileURLToPath(new URL('../compose.yaml', import.meta.url)), 'ps', '-q', 'postgres']);
    container = stdout.trim(); assert.ok(container, 'start the disposable demo first');
  }
  const samples = [];
  const sample = async () => {
    const [{ stdout }, activity] = await Promise.all([
      exec('docker', ['exec', container, 'sh', '-c', command]),
      admin.query(`SELECT state, wait_event_type, count(*)::int AS count FROM pg_stat_activity
        WHERE datname = current_database() AND pid <> pg_backend_pid() AND backend_type = 'client backend'
        GROUP BY state, wait_event_type`),
    ]);
    samples.push({ at_ms: performance.now(), ...parseResources(stdout), activity: activity.rows });
  };
  await sample();
  let pending = Promise.resolve(), failure, busy = false;
  const timer = setInterval(() => {
    if (busy || failure) return;
    busy = true;
    pending = sample().catch(error => { failure = error; }).finally(() => { busy = false; });
  }, 500);
  return async requests => {
    clearInterval(timer);
    await pending;
    if (failure) throw failure;
    await sample();
    const first = samples[0], last = samples.at(-1), seconds = (last.at_ms - first.at_ms) / 1000;
    const cpu = last.cpu_seconds - first.cpu_seconds;
    const mean = key => samples.reduce((n, row) => n + row[key], 0) / samples.length;
    const peak = key => Math.max(...samples.map(row => row[key]));
    return { window_seconds: seconds, cpu_seconds: cpu, cpu_cores: cpu / seconds,
      cpu_capacity: first.cpu_capacity, cpu_percent_capacity: cpu / seconds / first.cpu_capacity * 100,
      cpu_us_per_request: cpu * 1e6 / requests,
      memory_mean_bytes: mean('memory_bytes'), memory_peak_bytes: peak('memory_bytes'),
      anon_peak_bytes: peak('anon_bytes'), shmem_peak_bytes: peak('shmem_bytes'),
      file_peak_bytes: peak('file_bytes'), memory_limit_bytes: first.memory_limit_bytes,
      io_read_bytes: last.rbytes - first.rbytes, io_write_bytes: last.wbytes - first.wbytes,
      network_rx_bytes: last.rx_bytes - first.rx_bytes, network_tx_bytes: last.tx_bytes - first.tx_bytes,
      throttled_seconds: last.throttled_seconds - first.throttled_seconds,
      memory_events: Object.fromEntries(Object.keys(first.memory_events).map(key => [key, last.memory_events[key] - first.memory_events[key]])),
      samples: samples.map(row => ({ ...row, at_ms: row.at_ms - first.at_ms })),
    };
  };
}
