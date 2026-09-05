#!/usr/bin/env python3
"""Contracts for public docs, built-site validation, and benchmark reporting."""
import copy
import importlib.util
from pathlib import Path
import re
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / f'{name}.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


site = module('check_site')
report = module('benchmark_report')
manifest = module('site_manifest')


class PagesContracts(unittest.TestCase):
    def test_navigation_resolves_and_metadata_is_distinct(self):
        paths, titles, descriptions = set(), set(), set()
        for document in [ROOT / 'index.html', *(ROOT / 'docs').glob('*.md')]:
            text = document.read_text()
            self.assertTrue(text.startswith('---\n'), document)
            frontmatter = text.split('---', 2)[1]
            fields = dict(re.findall(r'^([a-z_]+):\s*(.+)$', frontmatter, re.M))
            for key, seen in [('title', titles), ('description', descriptions), ('permalink', paths)]:
                value = fields[key].strip('"\'')
                self.assertNotIn(value, seen, document)
                seen.add(value)
        navigation = (ROOT / '_data/navigation.yml').read_text()
        for url in re.findall(r'^  url: (.+)$', navigation, re.M):
            self.assertIn(url, paths)
        self.assertEqual(len(paths), len(re.findall(r'^  url: (.+)$', navigation, re.M)) + 1)

    def test_homepage_starts_with_demo_and_benchmark(self):
        homepage = (ROOT / 'index.html').read_text()
        hero = homepage.split('<section id="benchmarks"')[0]
        self.assertIn('QUICKSTART.html', hero)
        self.assertIn('BENCHMARKS.html', hero)
        self.assertIn('unnest(local_cache.mget', hero)
        self.assertNotIn('curl -fsSL', hero)
        self.assertNotIn('1.3.0', homepage)

    def test_local_markdown_links_resolve(self):
        failures = []
        for document in [ROOT / 'README.md', ROOT / 'CONTRIBUTING.md', *(ROOT / 'docs').glob('*.md')]:
            for target in re.findall(r'\[[^]]*\]\(([^)]+)\)', document.read_text()):
                if target.startswith(('http://', 'https://', '#', 'mailto:')):
                    continue
                path = target.split('#', 1)[0]
                if path and not (document.parent / path).resolve().exists():
                    failures.append(f'{document.name} -> {target}')
        self.assertEqual(failures, [])

    def test_sitemap_and_workflow_cover_the_built_pages(self):
        self.assertIn('site.pages', (ROOT / 'sitemap.xml').read_text())
        workflow = (ROOT / '.github/workflows/pages.yml').read_text()
        self.assertIn('python3 scripts/check_site.py _site', workflow)
        self.assertIn('needs: validate', workflow)
        self.assertIn('site_manifest.py verify', workflow)
        self.assertIn('site_smoke.py _candidate', workflow)
        layout = (ROOT / '_layouts/default.html').read_text()
        self.assertIn('rel="canonical"', layout)
        self.assertIn('application/ld+json', layout)
        self.assertIn('google_site_verification', layout)

    def test_benchmark_defines_metrics_and_scope(self):
        document = (ROOT / 'docs/BENCHMARKS.md').read_text()
        self.assertIn('requests/s', document)
        self.assertIn('benchmark.json', document)
        self.assertNotIn('1.3.0', document)
        self.assertIn('closed-loop', document.lower())
        self.assertIn('coordinated omission', document)


class BuiltSiteChecks(unittest.TestCase):
    def fixture(self, root):
        (root / 'index.html').write_text('''<!doctype html><html><head><title>Demo</title>
<meta name="description" content="A demo"><meta name="robots" content="index,follow">
<meta property="og:url" content="https://profundium.github.io/pg_local_cache/">
<link rel="canonical" href="https://profundium.github.io/pg_local_cache/">
<script type="application/ld+json">{"@type":"SoftwareSourceCode"}</script>
</head><body><main id="main-content"><h1>Demo</h1><a href="#code">Code</a>
<pre id="code">SELECT 1</pre><button data-copy="code">Copy</button></main></body></html>''')
        (root / 'sitemap.xml').write_text('''<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://profundium.github.io/pg_local_cache/</loc></url></urlset>''')

    def test_preview_policy_and_canonical_host(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            base = 'https://aicopilot-fr.github.io/pg_local_cache/'
            path = root / 'index.html'
            path.write_text(path.read_text().replace(site.BASE, base).replace('content="index,follow"', 'content="noindex,follow"'))
            (root / 'sitemap.xml').write_text('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>')
            self.assertEqual(site.check(root, base, preview=True), [])
            self.assertTrue(site.check(root, base))
            self.assertTrue(site.check(root, site.BASE, preview=True))

    def test_manifest_records_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            before = manifest.manifest(root, 'a' * 40, site.BASE)
            self.assertIn('index.html', before['files'])
            (root / 'index.html').write_text('changed')
            self.assertNotEqual(before, manifest.manifest(root, 'a' * 40, site.BASE))
            (root / 'link').symlink_to(root / 'index.html')
            with self.assertRaises(ValueError):
                manifest.manifest(root, 'a' * 40, site.BASE)

    def test_valid_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            self.assertEqual(site.check(root), [])

    def test_broken_fragment_and_copy_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            path = root / 'index.html'
            path.write_text(path.read_text().replace('id="code"', 'id="different"'))
            errors = site.check(root)
            self.assertTrue(any('missing fragment' in error for error in errors))
            self.assertTrue(any('copy target' in error for error in errors))

    def test_bad_json_and_missing_page(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            path = root / 'index.html'
            path.write_text(path.read_text().replace('{"@type":"SoftwareSourceCode"}', '{bad}').replace('href="#code"', 'href="missing.html"'))
            errors = site.check(root)
            self.assertTrue(any('invalid JSON-LD' in error for error in errors))
            self.assertTrue(any('missing local target' in error for error in errors))


class BenchmarkReportChecks(unittest.TestCase):
    def sample(self):
        return {'schema': 1, 'measured_at': 'test fixture',
                'environment': {'postgres_version': '16', 'extension_version': '2.0.1'},
                'extension_ref': 'test', 'harness_ref': 'test', 'results': [
                    {'repeat': 1, 'workload': 'warm', 'mode': 'mget', 'batch': 16,
                     'requests_s': 100, 'requested_read_keys_s': 1600,
                     'read_latency': {'samples': 10, 'p50_ms': 1, 'p95_ms': 2, 'p99_ms': 3},
                     'write_latency': None}]}

    def test_keeps_repetitions_separate(self):
        data = self.sample()
        second = copy.deepcopy(data['results'][0])
        second['repeat'] = 2
        data['results'].append(second)
        text = report.summary(data)
        self.assertIn('| 1 | warm | mget', text)
        self.assertIn('| 2 | warm | mget', text)
        self.assertIn('1,600.000', text)

    def test_rejects_invalid_measurements(self):
        for value in [float('nan'), float('inf'), -1, True, '100']:
            with self.assertRaises(ValueError):
                report.number(value)
        with self.assertRaises(ValueError):
            report.summary({'schema': 1, 'results': []})


if __name__ == '__main__':
    unittest.main()
