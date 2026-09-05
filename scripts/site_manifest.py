#!/usr/bin/env python3
"""Record a built site's bytes and check the exact artifact over HTTP."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urljoin
from urllib.request import Request, urlopen


def manifest(root, commit, base):
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Expected a full source commit SHA')
    files = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'Symlink in Pages artifact: {path}')
        if path.is_file() and path.name != 'site-manifest.json':
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if 'index.html' not in files or 'sitemap.xml' not in files:
        raise ValueError('Not a built documentation site')
    return {'source_commit': commit, 'base_url': base.rstrip('/') + '/', 'files': files}


def fetch(url):
    with urlopen(Request(url, headers={'Cache-Control': 'no-cache'}), timeout=20) as response:
        if response.status != 200:
            raise ValueError(f'{url}: HTTP {response.status}')
        return response.read()


def verify(root, base, attempts):
    expected = json.loads((root / 'site-manifest.json').read_text())
    if manifest(root, expected['source_commit'], expected['base_url']) != expected:
        raise ValueError('Local artifact differs from its manifest')
    for attempt in range(attempts):
        try:
            actual = json.loads(fetch(urljoin(base, 'site-manifest.json')))
            if actual != expected:
                raise ValueError('Published manifest is from a different build')
            for path, checksum in expected['files'].items():
                data = fetch(urljoin(base, path))
                if hashlib.sha256(data).hexdigest() != checksum:
                    raise ValueError(f'Published bytes differ: {path}')
            print(f"PASS: {len(expected['files'])} HTTP resources match {expected['source_commit']}")
            return
        except Exception:
            if attempt + 1 == attempts:
                raise
            time.sleep(10)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['write', 'verify'])
    parser.add_argument('directory', type=Path)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--commit')
    parser.add_argument('--attempts', type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.attempts <= 12:
        parser.error('--attempts must be between 1 and 12')
    if args.operation == 'write':
        if not args.commit:
            parser.error('write requires --commit')
        data = manifest(args.directory, args.commit, args.base_url)
        (args.directory / 'site-manifest.json').write_text(json.dumps(data, indent=2) + '\n')
    else:
        verify(args.directory, args.base_url.rstrip('/') + '/', args.attempts)
