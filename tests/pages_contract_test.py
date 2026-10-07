#!/usr/bin/env python3
"""Contracts for public docs, built-site validation, and benchmark reporting."""
import copy
import importlib.util
import json
from pathlib import Path
import re
import statistics
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LANGUAGES = re.findall(r'^([a-z]{2}):$', (ROOT / 'site/_data/locales.yml').read_text(), re.M)


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / f'{name}.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


site = module('check_site')
report = module('benchmark_report')


def front_matter(path):
    text = path.read_text(encoding='utf-8')
    match = re.match(r'\A---\s*\n(.*?)\n---(?:\n|$)', text, re.S)
    if match is None:
        return {}
    fields = {}
    for key in ('lang', 'translation_key', 'title', 'description', 'permalink'):
        value = re.search(rf'^{key}:\s*(.*?)\s*$', match.group(1), re.M)
        if value:
            fields[key] = value.group(1).strip('\"\'')
    return fields


def locale_entries():
    result = {}
    language = None
    for line in (ROOT / 'site/_data/locales.yml').read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'([a-z]{2}):', line)
        if match:
            language = match.group(1)
            result[language] = {}
        elif language and line.startswith('  '):
            key, value = line.strip().split(':', 1)
            result[language][key] = value.strip().strip('\"\'')
    return result


def locale_dictionary_entries(path):
    result = {}
    parents = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = re.fullmatch(r'(\s*)([A-Za-z_][A-Za-z0-9_-]*):(?:\s*(.*))?', line)
        if match is None:
            continue
        indentation, key, value = len(match.group(1)), match.group(2), (match.group(3) or '').strip()
        while parents and parents[-1][0] >= indentation:
            parents.pop()
        key_path = tuple(parent_key for _, parent_key in parents) + (key,)
        result[key_path] = value or None
        if not value:
            parents.append((indentation, key))
    return result


def code_blocks(text):
    return re.findall(r'(?ms)^(`{3,})([^\n]*)\n(.*?)^\1[ \t]*$', text)


def has_unquoted_colon_space(value):
    quote = None
    index = 0
    while index < len(value):
        char = value[index]
        if quote == "\"":
            if char == "\\":
                index += 2
                continue
            if char == "\"":
                quote = None
        elif quote == "'":
            if char == "'":
                if index + 1 < len(value) and value[index + 1] == "'":
                    index += 2
                    continue
                quote = None
        else:
            if char == "#" and (index == 0 or value[index - 1].isspace()):
                break
            if value.startswith(": ", index):
                return True
            if char in ("\"", "'"):
                quote = char
        index += 1
    return False


class RepositoryContentChecks(unittest.TestCase):
    def test_front_matter_is_strictly_fenced_and_quotes_yaml_colon_space(self):
        failures = []
        for area in (ROOT / 'docs', ROOT / 'site'):
            for path in sorted(area.rglob('*')):
                if not path.is_file() or path.suffix not in ('.md', '.html', '.xml'):
                    continue
                lines = path.read_text(encoding='utf-8').splitlines()
                if not lines or lines[0].strip() != '---':
                    continue
                closing = next((i for i in range(1, len(lines))
                                if lines[i].strip() in ('---', '...')), None)
                if closing is None:
                    failures.append(f'{path.relative_to(ROOT)}: missing closing front-matter fence')
                    continue
                for number, line in enumerate(lines[1:closing], 2):
                    if not line.strip() or line.lstrip().startswith('#'):
                        continue
                    mapping = re.match(r'^[ \t]*(?:-\s+)?[A-Za-z_][A-Za-z0-9_-]*\s*:(.*)$', line)
                    if mapping and has_unquoted_colon_space(mapping.group(1)):
                        failures.append(f'{path.relative_to(ROOT)}:{number}: quote plain YAML values containing colon-space')
        self.assertEqual(failures, [])

        documents = [ROOT / 'README.md', *sorted((ROOT / 'docs').glob('*.md'))]
        documents.extend(sorted((ROOT / 'site').rglob('*.md')))
        failures = []
        for document in documents:
            for target in re.findall(r'\[[^]]*\]\(([^)]+)\)', document.read_text(encoding='utf-8')):
                target = target.strip().split(maxsplit=1)[0].strip('<>')
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                path = Path(unquote(parsed.path))
                candidate = (document.parent / path).resolve()
                english_site_docs = (ROOT / 'site/docs').resolve()
                if not candidate.exists():
                    try:
                        candidate = (ROOT / 'docs' / candidate.relative_to(english_site_docs)).resolve()
                    except ValueError:
                        pass
                if not candidate.exists() and candidate.suffix == '.html':
                    candidate = candidate.with_suffix('.md')
                if not candidate.exists():
                    try:
                        relative = candidate.relative_to(ROOT).as_posix()
                    except ValueError:
                        relative = ''
                    if relative.startswith('site/'):
                        relative = relative[len('site/'):]
                    if relative.endswith('.md'):
                        relative = relative[:-3] + '.html'
                    failures.append(f'{document.relative_to(ROOT)} -> {target}')
        self.assertEqual(failures, [])

    def test_complete_translations_preserve_code_blocks_and_section_anchors(self):
        failures = []
        for section, english_root in (('docs', ROOT / 'docs'), ('blog', ROOT / 'site/blog')):
            english_documents = {path.name: path for path in english_root.glob('*.md')}
            for language in LANGUAGES:
                if language == 'en':
                    continue
                translated = ROOT / 'site' / language / section
                translated_documents = {path.name: path for path in translated.glob('*.md')}
                self.assertEqual(set(translated_documents), set(english_documents), f'{language}/{section}')
                for name, english_path in english_documents.items():
                    translated_path = translated_documents[name]
                    english = english_path.read_text(encoding='utf-8')
                    localized = translated_path.read_text(encoding='utf-8')
                    if code_blocks(localized) != code_blocks(english):
                        failures.append(f'{language}/{section}/{name}: code blocks differ')
                    english_anchors = re.findall(r'\{#([A-Za-z0-9_-]+)\}', english)
                    localized_anchors = re.findall(r'\{#([A-Za-z0-9_-]+)\}', localized)
                    if localized_anchors != english_anchors:
                        failures.append(f'{language}/{section}/{name}: section anchors differ')
        self.assertEqual(failures, [])

    def test_locale_dictionaries_match_english_keys_and_have_values(self):
        dictionary_files = {
            path.stem: path for path in (ROOT / 'site/_data').glob('[a-z][a-z].yml')
        }
        self.assertEqual(set(dictionary_files), set(LANGUAGES))
        dictionaries = {
            language: locale_dictionary_entries(path)
            for language, path in dictionary_files.items()
        }
        english_keys = set(dictionaries['en'])
        failures = []
        for language, entries in dictionaries.items():
            keys = set(entries)
            missing = english_keys - keys
            extra = keys - english_keys
            if missing:
                failures.append(f'{language}: missing keys {sorted(missing)}')
            if extra:
                failures.append(f'{language}: extra keys {sorted(extra)}')
            empty = []
            for key_path, value in entries.items():
                if value is None:
                    has_children = any(
                        len(candidate) > len(key_path) and candidate[:len(key_path)] == key_path
                        for candidate in entries
                    )
                    if not has_children:
                        empty.append('.'.join(key_path))
                else:
                    normalized = value.strip()
                    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in '\"\'':
                        normalized = normalized[1:-1].strip()
                    if normalized.lower() in ('', 'null', '~', '{}', '[]'):
                        empty.append('.'.join(key_path))
            if empty:
                failures.append(f'{language}: empty values at {sorted(empty)}')
        self.assertEqual(failures, [])

    def test_navigation_and_locale_metadata_resolve_and_are_distinct(self):
        locales = locale_entries()
        self.assertEqual(set(locales), set(LANGUAGES))
        self.assertEqual(set(locales), {'en', 'ru', 'es', 'de', 'fr', 'zh'})
        for language, entry in locales.items():
            self.assertEqual(entry.get('prefix'), '' if language == 'en' else f'/{language}')
            self.assertTrue(entry.get('og_locale'))
        self.assertEqual(len({entry['prefix'] for entry in locales.values()}), len(locales))
        self.assertEqual(len({entry['og_locale'] for entry in locales.values()}), len(locales))
        self.assertTrue(all(entry.get('label') and entry.get('og_locale') for entry in locales.values()))

        navigation = (ROOT / 'site/_data/navigation.yml').read_text(encoding='utf-8')
        targets = re.findall(r'^  url:\s*(\S+)\s*$', navigation, re.M)
        self.assertTrue(targets)
        failures = []
        for target in targets:
            source = Path(urlsplit(target).path.lstrip('/'))
            if source.suffix == '.html':
                source = source.with_suffix('.md')
            for language, entry in locales.items():
                localized = (ROOT / 'docs' / source.name if language == 'en'
                             else ROOT / 'site' / language / 'docs' / source.name)
                if not localized.is_file():
                    failures.append(f'{language}: {target}')
        self.assertEqual(failures, [])

        pages = {'en': ROOT / 'site/index.html'}
        pages.update({language: ROOT / 'site' / language / 'index.html'
                      for language in locales if language != 'en'})
        home_metadata = {language: front_matter(path) for language, path in pages.items()}
        self.assertEqual(set(home_metadata), set(locales))
        self.assertTrue(all(home_metadata[lang].get('lang') == lang for lang in locales))
        for language, metadata in home_metadata.items():
            self.assertEqual(metadata.get('permalink'), f"{locales[language]['prefix']}/")
        for field in ('title', 'description', 'permalink'):
            values = [metadata.get(field) for metadata in home_metadata.values()]
            self.assertTrue(all(values), field)
            self.assertEqual(len(set(values)), len(locales), field)

        english_documents = {path.name: front_matter(path) for path in (ROOT / 'docs').glob('*.md')}
        for name, english in english_documents.items():
            localized_metadata = [
                front_matter(ROOT / 'site' / language / 'docs' / name)
                for language in locales if language != 'en'
            ]
            all_metadata = [english, *localized_metadata]
            self.assertTrue(all(item.get('translation_key') == english.get('translation_key')
                                for item in localized_metadata), name)
            for field in ('title', 'description', 'permalink'):
                values = [item.get(field) for item in all_metadata]
                self.assertTrue(all(values), f'{name}: {field}')
                self.assertEqual(len(set(values)), len(locales), f'{name}: {field}')

    def test_homepage_benchmark_hero_matches_3_1_0_jsonl_medians(self):
        rows = [json.loads(line) for line in
                (ROOT / 'docs/benchmarks/3.1.0/read-per-core.jsonl').read_text(encoding='utf-8').splitlines()]
        modes = {'pg_local_cache': 'pg_local_cache', 'valkey': 'valkey',
                 'postgres-any': 'prepared SQL'}
        medians = {}
        for mode, label in modes.items():
            samples = [row['requests_s'] for row in rows
                       if row.get('mode') == mode and row.get('clients') == 256
                       and row.get('batch') == 1 and row.get('key_space') == 0
                       and row.get('server_cpus') == '0-1']
            self.assertEqual(len(samples), 5, mode)
            medians[label] = statistics.median(samples)

        def rate_value(value):
            value = value.strip()
            if value.endswith('k'):
                decimals = len(value[:-1].partition('.')[2])
                return float(value[:-1]) * 1000, 500 / (10 ** decimals)
            return int(value.replace(',', '')), 0

        for homepage in [ROOT / 'site/index.html', *(ROOT / 'site' / lang / 'index.html'
                                                     for lang in LANGUAGES if lang != 'en')]:
            text = homepage.read_text(encoding='utf-8')
            figure = re.search(r'<figure class="hero-benchmark".*?</figure>', text, re.S)
            self.assertIsNotNone(figure, homepage)
            markup = figure.group(0)
            resp = re.search(r'<p class="benchmark-number">\s*<strong>([\d,.]+k?)</strong>', markup)
            baselines = re.search(r'<dl class="benchmark-baselines">(.*?)</dl>', markup, re.S)
            baseline_values = re.findall(r'<dd>\s*([\d,.]+k?)', baselines.group(1)) if baselines else []
            self.assertTrue(resp and len(baseline_values) == 2, homepage)
            displayed = [rate_value(resp.group(1)), *(rate_value(value) for value in baseline_values)]
            expected = [medians['pg_local_cache'], medians['valkey'], medians['prepared SQL']]
            for (value, tolerance), median in zip(displayed, expected):
                self.assertLessEqual(abs(value - median), tolerance, homepage)


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

    def test_redirect_artifacts_only_require_existing_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            redirects = []
            for index in range(12):
                path = root / f'redirect-{index}.html'
                path.write_text(
                    f'<meta http-equiv="refresh" content="0; url={site.BASE}">')
                redirects.append(path)
            self.assertEqual(site.check(root), [])
            redirects[-1].write_text(
                f'<meta http-equiv="refresh" content="0; url={site.BASE}missing.html">')
            errors = site.check(root)
            self.assertTrue(any('redirect target is missing' in error for error in errors))

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
