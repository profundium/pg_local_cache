#!/usr/bin/env python3
"""Run the quickstart's SQL and Node commands verbatim against its disposable DB."""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    text = (ROOT / 'docs/QUICKSTART.md').read_text()
    # Startup and teardown are owned by Actions, so cleanup also runs on failure.
    # These are the commands a reader runs after `compose up --wait`.
    sections = ['Read as an application role', 'Check commit and rollback', 'Compare with ordinary SQL']
    for heading in sections:
        section = text.split(f'\n## {heading}\n', 1)[1].split('\n## ', 1)[0]
        commands = re.findall(r'```bash\n(.*?)\n```', section, re.S)
        if not commands:
            raise ValueError(f'No shell commands under {heading}')
        for command in commands:
            subprocess.run(['bash', '-euo', 'pipefail', '-c', command], cwd=ROOT, check=True, timeout=300)
        print(f'PASS documented commands: {heading}', flush=True)


if __name__ == '__main__':
    main()
