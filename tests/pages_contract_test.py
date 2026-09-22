#!/usr/bin/env python3
"""Contracts for public docs, built-site validation, and benchmark reporting."""
import copy
import importlib.util
import json
from pathlib import Path
import re
import runpy
import statistics
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LANGUAGES = re.findall(r'^([a-z]{2}):$', (ROOT / '_data/locales.yml').read_text(), re.M)


def documents():
    return [ROOT / 'index.html', *(ROOT / 'docs').glob('*.md'), *(ROOT / 'blog').glob('*.md')]


def metadata(text):
    return {key: value.strip('"\'') for key, value in
            re.findall(r'^([a-z_]+):\s*(.+)$', text.split('---', 2)[1], re.M)}


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / f'{name}.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


site = module('check_site')
report = module('benchmark_report')


class PagesContracts(unittest.TestCase):
    def test_executable_docs_keep_heading_extraction_with_stable_anchors(self):
        blocks = runpy.run_path(str(ROOT / 'tests/install_docs_smoke.py'))['blocks']
        for filename, heading, language in [
            ('INSTALL_EXISTING.md', 'Configure before restart', 'conf'),
            ('INSTALL_EXISTING.md', 'Initialize a source installation', 'sql'),
            ('QUICKSTART.md', 'Read as an application role', 'bash'),
            ('QUICKSTART.md', 'Check commit and rollback', 'bash'),
        ]:
            document = (ROOT / 'docs' / filename).read_text()
            self.assertEqual(blocks(document, heading, language),
                             blocks(re.sub(r' \{#[^}]+\}', '', document), heading, language))

    def test_complete_translations_preserve_code_and_section_anchors(self):
        originals = documents()
        self.assertEqual(len(originals), 19)
        self.assertEqual(len(list((ROOT / 'docs').glob('*.md'))), 14)
        self.assertEqual(len(list((ROOT / 'blog').glob('*.md'))), 4)
        dictionary_keys = re.findall(r'^(\s*[a-z_]+):', (ROOT / '_data/en.yml').read_text(), re.M)
        for language in LANGUAGES:
            dictionary = (ROOT / f'_data/{language}.yml').read_text()
            self.assertEqual(re.findall(r'^(\s*[a-z_]+):', dictionary, re.M), dictionary_keys, language)
            for source in originals:
                translated = source if language == 'en' else ROOT / language / source.relative_to(ROOT)
                with self.subTest(path=translated):
                    original, text = source.read_text(), translated.read_text()
                    a, b = metadata(original), metadata(text)
                    self.assertEqual(b['lang'], language)
                    self.assertEqual(b['translation_key'], a['translation_key'])
                    self.assertEqual(b['permalink'], ('' if language == 'en' else '/' + language) + a['permalink'])
                    self.assertEqual(b['layout'], a['layout'])
                    self.assertEqual(re.findall(r'\{#[^}]+\}', original), re.findall(r'\{#[^}]+\}', text))
                    self.assertEqual(re.findall(r'^```[^\n]*\n(.*?)^```', original, re.M | re.S),
                                     re.findall(r'^```[^\n]*\n(.*?)^```', text, re.M | re.S))
                    for field in ('date', 'last_modified_at', 'topic'):
                        self.assertEqual(b.get(field), a.get(field))
                    if language != 'en':
                        self.assertNotEqual(b['title'], a['title'])
                        self.assertNotEqual(b['description'], a['description'])
                        self.assertGreater(len(text), len(original) * .35)

    def test_navigation_resolves_and_metadata_is_distinct(self):
        paths, titles, descriptions = set(), set(), set()
        for document in documents():
            text = document.read_text()
            self.assertTrue(text.startswith('---\n'), document)
            frontmatter = text.split('---', 2)[1]
            fields = dict(re.findall(r'^([a-z_]+):\s*(.+)$', frontmatter, re.M))
            for key, seen in [('title', titles), ('description', descriptions), ('permalink', paths)]:
                value = fields[key].strip('"\'')
                self.assertNotIn(value, seen, document)
                seen.add(value)
        navigation = (ROOT / '_data/navigation.yml').read_text()
        urls = re.findall(r'^  url: (.+)$', navigation, re.M)
        for url in urls:
            self.assertIn(url, paths)
        self.assertEqual(len(urls), len(set(urls)))

    def test_homepage_starts_with_demo_and_benchmark(self):
        homepage = (ROOT / 'index.html').read_text()
        hero = homepage.split('<section id="benchmarks"')[0]
        self.assertIn('QUICKSTART.html', hero)
        self.assertIn('BENCHMARKS.html', hero)
        self.assertIn('local_cache.attach_table', hero)
        self.assertIn('local_cache.mget', hero)
        self.assertNotIn('curl -fsSL', hero)
        self.assertNotIn('1.3.0', homepage)

    def test_local_markdown_links_resolve(self):
        failures = []
        translated = [ROOT / lang / source.relative_to(ROOT) for lang in LANGUAGES if lang != 'en'
                      for source in documents() if source.suffix == '.md']
        for document in [ROOT / 'README.md', ROOT / 'CONTRIBUTING.md',
                         *(path for path in documents() if path.suffix == '.md'), *translated]:
            for label, target in re.findall(r'\[([^]]*)\]\(([^)]+)\)', document.read_text()):
                if target.startswith(('http://', 'https://', '#', 'mailto:')):
                    continue
                path = target.split('#', 1)[0]
                if path.endswith('.md') and '\n' in label:
                    failures.append(f'{document.relative_to(ROOT)}: Jekyll cannot rewrite a multiline link label')
                if path and not (document.parent / path).resolve().exists():
                    failures.append(f'{document.relative_to(ROOT)} -> {target}')
        self.assertEqual(failures, [])

    def test_hero_benchmark_matches_recorded_medians(self):
        data = json.loads((ROOT / 'assets/benchmarks/2026-09-15-m3-max-resp.json').read_text())
        rows = data['runs']['initial_optimized_vm']['results']
        medians = {
            mode: statistics.median(row['requests_s'] for row in rows
                                    if row['batch'] == 1 and row['clients'] == 256 and row['mode'] == mode)
            for mode in ('postgres-any', 'mget', 'resp-mget')
        }
        hero = (ROOT / 'index.html').read_text().split('</section>', 1)[0]
        for value in medians.values():
            self.assertIn(f'{value:,.0f}', hero)
        self.assertIn(f'{medians["resp-mget"] / medians["postgres-any"]:.2f}×', hero)
        self.assertIn('SQL mget was slower', hero)
        self.assertIn('benchmarks-go.html', hero)

    def test_sitemap_and_workflow_cover_the_built_pages(self):
        self.assertIn('site.pages', (ROOT / 'sitemap.xml').read_text())
        workflow = (ROOT / '.github/workflows/pages.yml').read_text()
        self.assertIn('python3 scripts/check_site.py _site', workflow)
        self.assertIn('needs: validate', workflow)
        self.assertIn('site_smoke.py _site', workflow)
        self.assertNotIn('docs/2.0-adoption', workflow)
        self.assertNotIn('site_manifest', workflow)
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
    def multilingual_fixture(self, root):
        self.fixture(root)
        template = (root / 'index.html').read_text()
        alternates = ''.join(f'<link rel="alternate" hreflang="{lang}" href="{site.BASE}{lang + "/" if lang != "en" else ""}">'
                             for lang in LANGUAGES)
        alternates += f'<link rel="alternate" hreflang="x-default" href="{site.BASE}">'
        links = ''.join(f'<a hreflang="{lang}" href="{site.BASE}{lang + "/" if lang != "en" else ""}">{lang}</a>' for lang in LANGUAGES)
        urls = []
        for lang in LANGUAGES:
            prefix = '' if lang == 'en' else lang + '/'
            url = site.BASE + prefix
            directory = root / prefix
            directory.mkdir(exist_ok=True)
            urls.append(url)
            text = template.replace('<html>', f'<html lang="{lang}">')
            text = text.replace('Demo', 'Demo ' + lang).replace('A demo', 'A demo ' + lang)
            text = text.replace('href="' + site.BASE + '"', 'href="' + url + '"')
            text = text.replace('content="' + site.BASE + '"', 'content="' + url + '"')
            text = text.replace('{"@type":"SoftwareSourceCode"}', json.dumps({
                '@type': 'BlogPosting', 'inLanguage': lang, 'datePublished': '2026-09-22',
                'dateModified': '2026-09-22', 'headline': 'Demo ' + lang,
                'author': {'@type': 'Organization', 'name': 'Demo'},
                'publisher': {'@type': 'Organization', 'name': 'Demo'},
            }))
            text = text.replace('</head>', alternates + f'<link rel="alternate" type="application/atom+xml" href="{url}feed.xml"></head>')
            text = text.replace('</main>', links + '</main>')
            (directory / 'index.html').write_text(text)
            (directory / 'feed.xml').write_text(f'''<feed xmlns="http://www.w3.org/2005/Atom" xml:lang="{lang}">
<id>{url}blog/</id><link rel="self" href="{url}feed.xml"/><title>Demo</title><updated>2026-09-22T00:00:00Z</updated>
<entry><id>{url}</id><link href="{url}"/><title>Demo {lang}</title><summary>Demo</summary>
<published>2026-09-22T00:00:00Z</published><updated>2026-09-22T00:00:00Z</updated></entry></feed>''')
        (root / 'sitemap.xml').write_text('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' +
                                        ''.join(f'<url><loc>{url}</loc></url>' for url in urls) + '</urlset>')

    def test_multilingual_artifact_and_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.multilingual_fixture(root)
            self.assertEqual(site.check(root, languages=LANGUAGES), [])
            mutations = [
                ('fr/index.html', '<a hreflang="es"', '<a', 'content link changes locale'),
                ('ru/index.html', 'hreflang="es"', 'hreflang="it"', 'hreflang'),
                ('fr/index.html', '<html lang="fr">', '<html lang="en">', 'language'),
                ('de/index.html', '"inLanguage": "de"', '"inLanguage": "en"', 'structured-data language'),
                ('es/feed.xml', f'<id>{site.BASE}es/</id>', f'<id>{site.BASE}fr/</id>', 'locale articles'),
                ('zh/index.html', '"datePublished": "2026-09-22"', '"datePublished": ""', 'datePublished'),
            ]
            for filename, before, after, error in mutations:
                path = root / filename
                original = path.read_text()
                self.assertIn(before, original)
                path.write_text(original.replace(before, after))
                self.assertTrue(any(error in item for item in site.check(root, languages=LANGUAGES)), filename)
                path.write_text(original)

    def fixture(self, root):
        (root / 'index.html').write_text('''<!doctype html><html><head><title>Demo</title>
<meta name="description" content="A demo"><meta name="robots" content="index,follow">
<meta property="og:title" content="Demo"><meta property="og:description" content="A demo">
<meta name="twitter:title" content="Demo"><meta name="twitter:description" content="A demo">
<meta property="og:image" content="https://profundium.github.io/pg_local_cache/card.png">
<meta name="twitter:image" content="https://profundium.github.io/pg_local_cache/card.png">
<meta property="og:url" content="https://profundium.github.io/pg_local_cache/">
<link rel="canonical" href="https://profundium.github.io/pg_local_cache/">
<script type="application/ld+json">{"@type":"SoftwareSourceCode"}</script>
</head><body><main id="main-content"><h1>Demo</h1><a href="#code">Code</a>
<pre id="code">SELECT 1</pre><button data-copy="code">Copy</button></main></body></html>''')
        (root / 'card.png').write_bytes(b'image fixture')
        (root / 'sitemap.xml').write_text('''<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://profundium.github.io/pg_local_cache/</loc></url></urlset>''')

    def test_rejects_noindex_and_wrong_canonical_host(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            path = root / 'index.html'
            path.write_text(path.read_text().replace('content="index,follow"', 'content="noindex,follow"'))
            self.assertTrue(any('indexing policy' in error for error in site.check(root)))
            self.fixture(root)
            path.write_text(path.read_text().replace(site.BASE, 'https://example.com/'))
            self.assertTrue(any('canonical' in error for error in site.check(root)))

    def test_missing_social_image_and_duplicate_canonical(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            (root / 'card.png').unlink()
            path = root / 'index.html'
            path.write_text(path.read_text().replace('</head>',
                '<link rel="canonical" href="https://profundium.github.io/pg_local_cache/"></head>'))
            errors = site.check(root)
            self.assertTrue(any('card.png' in error for error in errors))
            self.assertTrue(any('duplicate canonical' in error for error in errors))

    def test_valid_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            self.assertEqual(site.check(root), [])

    def test_svg_title_does_not_change_page_title(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            path = root / 'index.html'
            path.write_text(path.read_text().replace('</main>',
                '<svg role="img" aria-labelledby="diagram-title"><title id="diagram-title">Read path</title></svg></main>'))
            self.assertEqual(site.check(root), [])

    def test_sitemap_entry_cannot_replace_a_crawlable_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            home = root / 'index.html'
            orphan = home.read_text().replace('Demo', 'Guide').replace('A demo', 'A guide')
            orphan = orphan.replace('href="' + site.BASE + '"', 'href="' + site.BASE + 'guide.html"')
            orphan = orphan.replace('content="' + site.BASE + '"', 'content="' + site.BASE + 'guide.html"')
            (root / 'guide.html').write_text(orphan)
            sitemap = root / 'sitemap.xml'
            sitemap.write_text(sitemap.read_text().replace('</urlset>',
                '<url><loc>' + site.BASE + 'guide.html</loc></url></urlset>'))
            self.assertEqual(site.check(root), [site.BASE + 'guide.html: page is not reachable through links from the homepage'])
            home.write_text(home.read_text().replace('</main>', '<a href="guide.html">Guide</a></main>'))
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

    def test_rejects_partial_run_after_query_error(self):
        data = self.sample()
        for error in [{'message': 'canceling statement due to statement timeout', 'code': '57014'}, {}, None, '']:
            data['error'] = error
            with self.assertRaisesRegex(ValueError, 'benchmark did not complete'):
                report.summary(data)

    def test_server_resource_units(self):
        data = self.sample()
        data['results'][0]['server'] = {
            'cpu_cores': 2, 'cpu_percent_capacity': 25,
            'memory_peak_bytes': 128 * 2**20, 'io_read_bytes': 1024, 'io_write_bytes': 2048,
        }
        text = report.summary(data)
        self.assertIn('not process RSS', text)
        self.assertIn('| 2.000 | 25.000 | 128.000 | 1.000 / 2.000 |', text)

    def test_application_client_report_keeps_driver_and_connections(self):
        data = self.sample()
        row = data['results'][0]
        row.update(driver='go-pgx', clients=64, latency=row.pop('read_latency'))
        del row['workload'], row['requested_read_keys_s'], row['write_latency']
        text = report.summary(data)
        self.assertIn('go-pgx (64 connections)', text)
        self.assertIn('1,600.000', text)
        self.assertNotIn('workload', row)

    def test_unlabelled_build_does_not_invent_a_revision(self):
        data = self.sample()
        data['extension_ref'] = None
        self.assertIn('Extension ref: `unrecorded`', report.summary(data))


if __name__ == '__main__':
    unittest.main()
