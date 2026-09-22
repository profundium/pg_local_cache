#!/usr/bin/env python3
"""Exercise the built documentation under its deployed URL prefix."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import tempfile
from threading import Thread
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import sync_playwright, expect


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def exercise(browser, name, paths, base, canonical_base, out, width, height, dark=False, js=True, motion='reduce'):
    context = browser.new_context(viewport={'width': width, 'height': height},
                                  color_scheme='dark' if dark else 'light',
                                  java_script_enabled=js, reduced_motion=motion)
    if js and name.startswith('chromium'):
        context.grant_permissions(['clipboard-read', 'clipboard-write'])
    # Keep local/CI visits out of real analytics and avoid an external dependency.
    context.route('https://www.googletagmanager.com/gtag/js?*',
                  lambda route: route.fulfill(status=200, content_type='application/javascript', body=''))
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
        for path in paths:
            relative = path[:-10] if path.endswith('index.html') else path
            response = page.goto(urljoin(base, relative), wait_until='load')
            assert response.status == 200, f'{path}: {response.status}'
            lang = page.locator('html').get_attribute('lang')
            prefix = '' if lang == 'en' else lang + '/'
            local_relative = relative[len(prefix):]
            expect(page.locator('.language-picker a')).to_have_count(6)
            expect(page.locator(f'.language-picker a[hreflang="{lang}"]')).to_have_attribute('aria-current', 'page')
            expect(page.locator('h1')).to_have_count(1)
            expect(page.locator('#main-content')).to_be_visible()
            assert page.title().strip(), path
            assert page.locator('link[rel=canonical]').get_attribute('href') == urljoin(canonical_base, relative)
            assert page.locator('script[type="application/ld+json"]').count() == 1
            json.loads(page.locator('script[type="application/ld+json"]').text_content())
            expect(page.locator('head script[src="https://www.googletagmanager.com/gtag/js?id=G-MHQBYKWZ7W"]')).to_have_count(1)
            if js:
                assert page.evaluate('''window.dataLayer.filter(args =>
                    args[0] === 'config' && args[1] === 'G-MHQBYKWZ7W').length''') == 1, f'{path}: analytics configuration missing or duplicated'
            for diagram in page.locator('.diagram svg').all():
                expect(diagram).to_have_attribute('role', 'img')
                assert diagram.locator('title').text_content().strip()
                assert diagram.locator('desc').text_content().strip()
                for label in diagram.get_attribute('aria-labelledby').split():
                    assert page.locator('[id="' + label + '"]').count() == 1
                assert diagram.evaluate('''svg => {
                    const bounds = svg.getBoundingClientRect();
                    const scale = bounds.width / svg.viewBox.baseVal.width;
                    return [...svg.querySelectorAll('text')].every(text => {
                        const box = text.getBoundingClientRect();
                        return parseFloat(getComputedStyle(text).fontSize) * scale >= 14 &&
                            box.left >= bounds.left - 1 && box.right <= bounds.right + 1 &&
                            box.top >= bounds.top - 1 && box.bottom <= bounds.bottom + 1;
                    });
                }'''), f'{path}: diagram labels too small or outside canvas at {width}px'
            if motion == 'reduce':
                assert page.evaluate('document.getAnimations().length === 0'), f'{path}: reduced motion ignored'
            # A document may scroll code blocks, but never the whole page horizontally.
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), f'{path}: horizontal overflow at {width}px'
            expect(page.locator('.site-header')).to_have_css('position', 'sticky')
            if js:
                assert page.locator('html').get_attribute('class') == 'js'
            if not local_relative:
                page.screenshot(path=str(out / f'{name}-{lang}-home.png'), full_page=True, animations="disabled")
                if js:
                    first_action = page.locator('.hero-actions a').first
                    first_action.focus()
                    page.keyboard.press('Escape')
                    expect(first_action).to_be_focused()
                    toggle, nav = page.locator('.nav-toggle'), page.locator('#site-nav')
                    if width <= 720:
                        expect(nav).to_be_hidden()
                        toggle.click()
                        expect(nav).to_be_visible()
                        expect(toggle).to_have_attribute('aria-expanded', 'true')
                        page.keyboard.press('Escape')
                        expect(nav).to_be_hidden()
                        toggle.click()
                        nav.locator('a[href$="/docs/QUICKSTART.html"]').click()
                        expect(page).to_have_url(urljoin(base, prefix + 'docs/QUICKSTART.html'))
                        expect(page.locator('#site-nav')).to_be_hidden()
                        page.goto(urljoin(base, prefix), wait_until='load')
                    faq = page.locator('#main-content details').first
                    summary = faq.locator('summary')
                    summary.click()
                    expect(faq).to_have_attribute('open', '')
                    expect(faq.locator('p')).to_be_visible()
                    summary.click()
                    button = page.locator('[data-copy]').first
                    if name.startswith('chromium'):
                        expected = page.locator('#' + button.get_attribute('data-copy')).text_content().strip()
                        button.click()
                        expect(button).to_have_text(page.locator('body').get_attribute('data-copied'))
                        assert page.evaluate('navigator.clipboard.readText()') == expected
                    else:
                        # WebKit disallows programmatic clipboard-read grants; test the real write.
                        button.click()
                        expect(button).to_have_text(page.locator('body').get_attribute('data-copied'))
                else:
                    expect(page.locator('#site-nav')).to_be_visible()
                action = page.locator('.hero-actions a').first
                if js:
                    action.click()
                else:
                    # Exercise native keyboard navigation without JS/rAF stability polling.
                    expect(action).to_be_visible()
                    action.focus()
                    page.keyboard.press('Enter')
                expect(page).to_have_url(urljoin(base, prefix + 'docs/QUICKSTART.html'))
            elif local_relative == 'docs/QUICKSTART.html':
                if js:
                    menu = page.locator('.doc-menu')
                    if width <= 980:
                        expect(page.locator('.doc-nav')).to_be_hidden()
                        menu.locator('summary').click()
                    expect(page.locator('.doc-nav')).to_be_visible()
                    toc = page.locator('[data-doc-toc-list] a')
                    assert toc.count() >= 2
                    target = toc.first.get_attribute('href')
                    toc.first.click()
                    expect(page).to_have_url(urljoin(base, relative) + target)
                    expect(page.locator(target)).to_be_in_viewport()
                page.evaluate('window.scrollTo({top: 0, behavior: "instant"})')
                page.screenshot(path=str(out / f'{name}-{lang}-quickstart.png'), full_page=True, animations="disabled")
            if local_relative == 'blog/' or local_relative == 'blog/measure-postgresql-row-cache/':
                page.screenshot(path=str(out / f'{name}-{lang}-{"blog" if local_relative == "blog/" else "article"}.png'), full_page=True, animations="disabled")
                if js and local_relative != 'blog/':
                    button = page.locator('[data-copy]').first
                    if button.count():
                        button.click()
                        expect(button).to_have_text(page.locator('body').get_attribute('data-copied'))
            # Exercise native language switching on each locale's index, guide and article.
            if local_relative in ('', 'blog/', 'blog/measure-postgresql-row-cache/', 'docs/QUICKSTART.html'):
                page.goto(urljoin(base, relative), wait_until='load')
                picker = page.locator('.language-picker')
                picker.locator('summary').focus()
                page.keyboard.press('Enter')
                expect(picker).to_have_attribute('open', '')
                destination = 'ru' if lang == 'en' else 'en'
                link = picker.locator(f'a[hreflang="{destination}"]')
                href = link.get_attribute('href')
                link.focus()
                page.keyboard.press('Enter')
                expect(page).to_have_url(urljoin(base, href))
                expect(page.locator('html')).to_have_attribute('lang', destination)
                expected_relative = ('ru/' if destination == 'ru' else '') + local_relative
                assert page.url == urljoin(base, expected_relative)
                page.goto(urljoin(base, relative), wait_until='load')
            if page.locator('.diagram').count() and relative:
                page.locator('.diagram').screenshot(path=str(out / f'{name}-{lang}-{Path(path).stem}-diagram.png'), animations="disabled")
            assert not failures, '\n'.join(failures)
            records.append({'page': relative or '/', 'status': 'passed'})
        # Unknown routes must not return a successful index page.
        response = context.request.get(urljoin(base, '__missing_document__.html'))
        assert response.status == 404
    finally:
        context.tracing.stop(path=str(out / f'{name}-trace.zip'))
        context.close()
    return {'case': name, 'viewport': [width, height], 'javascript': js, 'motion': motion, 'pages': records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--base-url', default='https://profundium.github.io/pg_local_cache/')
    parser.add_argument('--output', type=Path, default=Path('qa-artifacts/browser'))
    args = parser.parse_args()
    root = args.directory.resolve()
    paths = sorted(path.relative_to(root).as_posix() for path in root.rglob('*.html'))
    assert 'index.html' in paths, 'Build the documentation site first'
    canonical_base = args.base_url.rstrip('/') + '/'
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    server = None
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        try:
            prefix = urlsplit(canonical_base).path
            mount = Path(tmp) / prefix.strip('/')
            shutil.copytree(root, mount, dirs_exist_ok=True)
            server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=tmp))
            Thread(target=server.serve_forever, daemon=True).start()
            base = f'http://127.0.0.1:{server.server_port}{prefix}'
            for engine in ['chromium', 'webkit']:
                browser = getattr(pw, engine).launch()
                try:
                    for label, width, height, dark, js, motion in [
                        ('desktop', 1440, 900, False, True, 'no-preference'),
                        ('mobile', 390, 844, False, True, 'reduce'),
                        ('narrow-dark', 320, 740, True, True, 'reduce'),
                        ('nojs', 390, 844, False, False, 'reduce'),
                    ]:
                        name = f'{engine}-{label}'
                        result = exercise(browser, name, paths, base, canonical_base, args.output, width, height, dark, js, motion)
                        results.append(result)
                        print(f'PASS {name}: {len(result["pages"])} pages and user interactions', flush=True)
                finally:
                    browser.close()
        finally:
            if server:
                server.shutdown()
            (args.output / 'results.json').write_text(json.dumps({
                'directory': str(root),
                'cases': results, 'complete': len(results) == 8,
            }, indent=2) + '\n')


if __name__ == '__main__':
    main()
