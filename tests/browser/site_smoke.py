#!/usr/bin/env python3
"""Exercise the unpacked Pages artifact, or that same artifact after deployment."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from threading import Thread
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import sync_playwright, expect


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def exercise(browser, name, root, base, manifest, out, width, height, dark=False, js=True):
    context = browser.new_context(viewport={'width': width, 'height': height},
                                  color_scheme='dark' if dark else 'light',
                                  java_script_enabled=js, reduced_motion='reduce')
    context.tracing.start(screenshots=True, snapshots=True)
    page = context.new_page()
    page.set_default_timeout(10000)
    failures = []
    page.on('pageerror', lambda error: failures.append(str(error)))
    page.on('console', lambda message: failures.append(message.text) if message.type == 'error' else None)
    page.on('response', lambda response: failures.append(f'HTTP {response.status} {response.url}')
            if response.status >= 400 else None)
    page.on('requestfailed', lambda request: failures.append(f'{request.url}: {request.failure}'))
    records = []
    try:
        for path in manifest['files']:
            if not path.endswith('.html'):
                continue
            relative = path[:-10] if path.endswith('index.html') else path
            response = page.goto(urljoin(base, relative), wait_until='networkidle')
            assert response.status == 200, f'{path}: {response.status}'
            expect(page.locator('h1')).to_have_count(1)
            expect(page.locator('#main-content')).to_be_visible()
            assert page.title().strip(), path
            assert page.locator('link[rel=canonical]').get_attribute('href') == urljoin(manifest['base_url'], relative)
            assert page.locator('script[type="application/ld+json"]').count() == 1
            json.loads(page.locator('script[type="application/ld+json"]').text_content())
            # A document may scroll code blocks, but never the whole page horizontally.
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), f'{path}: horizontal overflow at {width}px'
            assert page.locator('nextjs-portal, vite-error-overlay').count() == 0
            expect(page.locator('.site-header')).to_have_css('position', 'sticky')
            if js:
                assert page.locator('html').get_attribute('class') == 'js'
            if not relative:
                page.screenshot(path=str(out / f'{name}-home.png'), full_page=True)
                if js:
                    toggle, nav = page.locator('.nav-toggle'), page.locator('#site-nav')
                    if width <= 720:
                        expect(nav).to_be_hidden()
                        toggle.click()
                        expect(nav).to_be_visible()
                        expect(toggle).to_have_attribute('aria-expanded', 'true')
                        page.keyboard.press('Escape')
                        expect(nav).to_be_hidden()
                        toggle.click()
                        nav.get_by_role('link', name='Try locally').click()
                        expect(page).to_have_url(urljoin(base, 'docs/QUICKSTART.html'))
                        expect(page.locator('#site-nav')).to_be_hidden()
                        page.goto(base, wait_until='networkidle')
                    summary = page.locator('details summary').first
                    summary.click()
                    expect(page.locator('details').first).to_have_attribute('open', '')
                    expect(page.locator('details').first.locator('p')).to_be_visible()
                    summary.click()
                    button = page.locator('[data-copy]').first
                    if name.startswith('chromium'):
                        context.grant_permissions(['clipboard-read', 'clipboard-write'])
                        expected = page.locator('#' + button.get_attribute('data-copy')).text_content().strip()
                        button.click()
                        expect(button).to_have_text('Copied')
                        assert page.evaluate('navigator.clipboard.readText()') == expected
                    else:
                        # WebKit disallows programmatic clipboard-read grants; test the real write.
                        button.click()
                        expect(button).to_have_text('Copied')
                else:
                    expect(page.locator('#site-nav')).to_be_visible()
                page.locator('.hero-actions').get_by_role('link', name='Try locally').click()
                expect(page).to_have_url(urljoin(base, 'docs/QUICKSTART.html'))
            elif path == 'docs/QUICKSTART.html':
                if js:
                    toc = page.locator('[data-doc-toc-list] a')
                    assert toc.count() >= 2
                    target = toc.first.get_attribute('href')
                    toc.first.click()
                    expect(page).to_have_url(urljoin(base, relative) + target)
                    expect(page.locator(target)).to_be_in_viewport()
                page.screenshot(path=str(out / f'{name}-quickstart.png'), full_page=True)
            assert not failures, '\n'.join(failures)
            records.append({'page': relative or '/', 'status': 'passed'})
        # Unknown routes must not return a successful index page.
        response = context.request.get(urljoin(base, '__missing_document__.html'))
        assert response.status == 404
    finally:
        context.tracing.stop(path=str(out / f'{name}-trace.zip'))
        context.close()
    return {'case': name, 'viewport': [width, height], 'javascript': js, 'pages': records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--url', help='Published site URL; otherwise serve the artifact locally')
    parser.add_argument('--output', type=Path, default=Path('qa-artifacts/browser'))
    args = parser.parse_args()
    root = args.directory.resolve()
    manifest = json.loads((root / 'site-manifest.json').read_text())
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    server = None
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        try:
            base = args.url
            if not base:
                prefix = urlsplit(manifest['base_url']).path
                mount = Path(tmp) / prefix.strip('/')
                shutil.copytree(root, mount, dirs_exist_ok=True)
                server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=tmp))
                Thread(target=server.serve_forever, daemon=True).start()
                base = f'http://127.0.0.1:{server.server_port}{prefix}'
            base = base.rstrip('/') + '/'
            subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2] / 'scripts/site_manifest.py'),
                            'verify', str(root), '--base-url', base], check=True, timeout=180)
            for engine in ['chromium', 'webkit']:
                browser = getattr(pw, engine).launch()
                try:
                    for label, width, height, dark, js in [
                        ('desktop', 1440, 900, False, True),
                        ('mobile', 390, 844, False, True),
                        ('narrow-dark', 320, 740, True, True),
                        ('nojs', 390, 844, False, False),
                    ]:
                        name = f'{engine}-{label}'
                        result = exercise(browser, name, root, base, manifest, args.output, width, height, dark, js)
                        results.append(result)
                        print(f'PASS {name}: {len(result["pages"])} pages and user interactions', flush=True)
                finally:
                    browser.close()
        finally:
            if server:
                server.shutdown()
            (args.output / 'results.json').write_text(json.dumps({
                'source_commit': manifest['source_commit'], 'url': args.url or 'local artifact',
                'cases': results, 'complete': len(results) == 8,
            }, indent=2) + '\n')


if __name__ == '__main__':
    main()
