#!/usr/bin/env python3
"""Check the disposable benchmark launchers locally; requires Docker, Node and Go."""
import json
import os
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
env = dict(os.environ, CONNECTIONS='4', CLIENTS='4', REQUESTS='100',
           BATCHES='1,16,64', REPEATS='1', DURATION_SECONDS='1')


def containers():
    return set(subprocess.check_output(
        ['docker', 'ps', '-a', '--filter', 'name=pglc-bench-', '--format', '{{.Names}}'],
        text=True).splitlines())


before = containers()
for mode in ('all', 'node-workload'):
    result = subprocess.run(['./examples/benchmark.sh', mode], cwd=root,
                            env=env, text=True, stdout=subprocess.PIPE, check=True)
    data = json.loads(result.stdout)
    assert 'error' not in data, data.get('error')
    assert data['results'], mode
    if mode == 'all':
        assert {(r['driver'], r['mode'], r['batch'], r['clients'], r['repeat']) for r in data['results']} == {
            (driver, variant, batch, 4, 1)
            for driver in ('node-json', 'go-pgx')
            for variant in ('postgres-any', 'mget', 'resp-mget')
            for batch in (1, 16, 64)
        }
    for row in data['results']:
        assert row['requests_s'] > 0
        assert row['server']['cpu_cores'] > 0
        assert row['server']['memory_peak_bytes'] > 0
    assert containers() == before, 'benchmark containers were not removed'
    print(f'{mode}: results, resources and cleanup OK')

result = subprocess.run(['./examples/benchmark.sh', 'node'], cwd=root,
                        env=dict(env, REPEATS='0'), stdout=subprocess.DEVNULL)
assert result.returncode != 0, 'invalid configuration must fail'
assert containers() == before, 'failure must also remove benchmark containers'
print('failure cleanup OK')
