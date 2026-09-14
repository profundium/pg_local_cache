#!/usr/bin/env python3
"""Run the quickstart's SQL and Node commands verbatim against its disposable DB."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-benchmark', action='store_true', help='Run only the functional examples')
    args = parser.parse_args()
    text = (ROOT / 'docs/QUICKSTART.md').read_text()
    recorded = subprocess.check_output([
        'docker', 'compose', '-f', 'examples/compose.yaml', 'exec', '-T', 'postgres',
        'psql', '-X', '-At', '-v', 'ON_ERROR_STOP=1', '-U', 'postgres', '-d', 'pglc_demo',
        '-c', "SELECT json_build_object('version', current_setting('pg_local_cache.binary_version'), "
        "'build_id', current_setting('pg_local_cache.binary_build_id'), "
        "'extension_version', (SELECT extversion FROM pg_extension WHERE extname = 'pg_local_cache'))",
    ], cwd=ROOT, text=True, timeout=30)
    binary = json.loads(recorded)
    expected_version = re.search(r"^default_version\s*=\s*'([^']+)'", (ROOT / 'pg_local_cache.control').read_text(), re.M)[1]
    assert binary['version'] == binary['extension_version'] == expected_version, binary
    assert binary['build_id'] == os.environ.get('PGLC_EXTENSION_REF', 'local'), binary
    print(f'PASS checked-out extension: {binary}', flush=True)
    # Startup and teardown are owned by Actions, so cleanup also runs on failure.
    # These are the commands a reader runs after `compose up --wait`.
    sections = ['Read as an application role', 'Check commit and rollback']
    if not args.skip_benchmark:
        sections.append('Compare with ordinary SQL')
    for heading in sections:
        section = text.split(f'\n## {heading}\n', 1)[1].split('\n## ', 1)[0]
        commands = re.findall(r'```bash\n(.*?)\n```', section, re.S)
        if not commands:
            raise ValueError(f'No shell commands under {heading}')
        for command in commands:
            subprocess.run(['bash', '-euo', 'pipefail', '-c', command], cwd=ROOT, check=True, timeout=300)
        print(f'PASS documented commands: {heading}', flush=True)
    if not args.skip_benchmark:
        result = json.loads((ROOT / 'benchmark.json').read_text())
        assert result['environment']['extension_build_id'] == binary['build_id'], result['environment']
        assert result['extension_ref'] == (None if binary['build_id'] == 'local' else binary['build_id'])
        assert 'error' not in result and all('server' in row for row in result['results'])
        print('PASS recorded binary provenance and server resources', flush=True)


if __name__ == '__main__':
    main()
