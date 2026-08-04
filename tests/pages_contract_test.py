#!/usr/bin/env python3
"""Static contracts for the GitHub Pages documentation source."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import hashlib
import json
import re
import struct
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
RELEASES_LATEST = "https://github.com/profundium/pg_local_cache/releases/latest"
PUBLISHED_DOCUMENTS = (
    ROOT / "docs" / "INSTALL_EXISTING.md",
    ROOT / "docs" / "BENCHMARKS.md",
    ROOT / "docs" / "MONITORING.md",
    ROOT / "docs" / "TECHNICAL.md",
)
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


class _IndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.h1_count = 0
        self.image_sources: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.add(attributes["id"] or "")
        if tag == "h1":
            self.h1_count += 1
        if tag == "img" and attributes.get("src"):
            self.image_sources.append(attributes["src"] or "")


def front_matter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise AssertionError(f"{path}: missing YAML front matter")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise AssertionError(f"{path}: unterminated YAML front matter") from error
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise AssertionError(f"{path}: unsupported front-matter line {line!r}")
        values[key.strip()] = value.strip()
    return values


class PagesSourceContracts(unittest.TestCase):
    def test_homepage_is_semantic_static_html(self) -> None:
        source = (ROOT / "index.html").read_text(encoding="utf-8")
        parser = _IndexParser()
        parser.feed(source)
        self.assertEqual(parser.h1_count, 1)
        self.assertIn("main-content", parser.ids)
        self.assertIn("quick-start", parser.ids)
        self.assertIn("sql-api", parser.ids)
        self.assertEqual(parser.image_sources, [])
        self.assertNotIn("lorem ipsum", source.lower())
        hero_start = source.index('<pre id="hero-sql">')
        hero_end = source.index("</pre>", hero_start)
        hero = source[hero_start:hero_end]
        self.assertIn(
            "SELECT local_cache.get('public.items'::regclass, $1::bigint);",
            hero,
        )
        self.assertIn("SELECT local_cache.mget(", hero)
        self.assertIn("$1::bigint[]", hero)
        self.assertNotIn("SELECT * FROM public.items", hero)
        key_array = "ARRAY[" + ", ".join(
            f":key_{index}" for index in range(32)
        ) + "]::bigint[]"
        stock_batch_command = (
            "SELECT pg_catalog.array_agg("
            "pg_catalog.row_to_json(pglc_source)::text ORDER BY "
            "pglc_input.ordinality) FROM pg_catalog.unnest("
            f"{key_array}) WITH ORDINALITY AS pglc_input(id, ordinality) "
            'LEFT JOIN "pglc_sql_bench_e407c3350a"."rows" AS '
            "pglc_source USING (id);"
        )
        self.assertNotIn(stock_batch_command, source)
        self.assertNotIn(
            "SELECT local_cache.mget("
            "'pglc_sql_bench_e407c3350a.rows'::regclass, "
            f"{key_array});",
            source,
        )
        self.assertIn("64,954", source)
        self.assertIn("66,156", source)
        self.assertIn("10.30x", source)
        self.assertIn("10.39x", source)
        self.assertIn("raw evidence ZIP", source)
        self.assertIn("Current SQL MGET benchmark", source)
        self.assertIn("30803546805", source)
        self.assertNotIn("Reference SQL KV snapshot", source)
        self.assertNotIn("ee221410", source)
        self.assertNotIn("30796269395", source)
        self.assertNotIn("111,103", source)
        self.assertNotIn("104,956", source)

    def test_benchmark_pages_publish_only_current_api_results(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        homepage = (ROOT / "index.html").read_text(encoding="utf-8")
        benchmarks = (ROOT / "docs" / "BENCHMARKS.md").read_text(
            encoding="utf-8"
        )
        compact_benchmarks = re.sub(r"\s+", "", benchmarks)
        readme_benchmarks = readme.split("## Benchmarks", 1)[1].split(
            "## Monitoring", 1
        )[0]
        commands = (
            "SELECT * FROM public.pg_local_cache_whole_row_comparison "
            "WHERE tenant_id = 7 AND id = :key;",
            "SELECT metadata, payload, enabled, amount, note, id, tenant_id "
            "FROM public.pg_local_cache_whole_row_comparison WHERE "
            "tenant_id = 7 AND id = :key;",
            "SELECT payload, metadata, id, tenant_id FROM public."
            "pg_local_cache_whole_row_comparison WHERE id = :key AND "
            "tenant_id = 7;",
        )
        key_array = "ARRAY[" + ", ".join(
            f":key_{index}" for index in range(32)
        ) + "]::bigint[]"
        mget_command = (
            "SELECT local_cache.mget("
            "'pglc_sql_bench_e407c3350a.rows'::regclass, "
            f"{key_array});"
        )
        stock_batch_command = (
            "SELECT pg_catalog.array_agg("
            "pg_catalog.row_to_json(pglc_source)::text ORDER BY "
            "pglc_input.ordinality) FROM pg_catalog.unnest("
            f"{key_array}) WITH ORDINALITY AS pglc_input(id, ordinality) "
            'LEFT JOIN "pglc_sql_bench_e407c3350a"."rows" AS '
            "pglc_source USING (id);"
        )
        scalar_get_command = (
            "SELECT local_cache.get("
            "'pglc_sql_bench_e407c3350a.rows'::regclass, (:key)::bigint);"
        )
        scalar_stock_command = (
            "SELECT pg_catalog.row_to_json(pglc_source)::text FROM "
            '"pglc_sql_bench_e407c3350a"."rows" AS pglc_source '
            "WHERE id = :key;"
        )
        for command in commands:
            self.assertIn(re.sub(r"\s+", "", command), compact_benchmarks)
        for command in (
            mget_command,
            stock_batch_command,
            scalar_get_command,
            scalar_stock_command,
        ):
            self.assertIn(re.sub(r"\s+", "", command), compact_benchmarks)
        throughput_commands = benchmarks.split("### SQL GET/MGET", 1)[1].split(
            "Latency was measured", 1
        )[0]
        self.assertNotIn("| Lane | Exact throughput command |", throughput_commands)
        self.assertEqual(throughput_commands.count("```sql"), 2)
        for source in (readme, homepage):
            self.assertNotIn(mget_command, source)
            self.assertNotIn(stock_batch_command, source)
        self.assertIn("./install.sh install", readme)

        for source in (readme_benchmarks, benchmarks):
            self.assertIn("stock PostgreSQL", source)
            self.assertIn("30803546805", source)
            self.assertIn("fe2d23c", source)
            self.assertIn("local_cache.mget", source)
            self.assertIn("64,954", source)
            self.assertIn("66,156", source)
            self.assertNotIn("Reference SQL", source)
            self.assertNotIn("ee221410", source)
            self.assertNotIn("30796269395", source)
            self.assertNotIn("13.90x", source)
            self.assertNotIn("111,103", source)
            self.assertNotIn("104,956", source)
        self.assertIn(
            'GET CRUD:benchmark.public.pg_local_cache_whole_row_comparison:'
            '{"id":1,"tenant_id":7}',
            benchmarks,
        )

    def test_public_pages_use_browser_release_downloads(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        homepage = (ROOT / "index.html").read_text(encoding="utf-8")
        install = (ROOT / "docs" / "INSTALL_EXISTING.md").read_text(
            encoding="utf-8"
        )
        public_sources = (readme, homepage) + tuple(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "docs").glob("*.md")
        )
        for source in public_sources:
            self.assertNotRegex(source, r"(?m)^\s*(?:[$#]\s*)?gh(?:\s|$)")
        for source in (readme, homepage, install):
            self.assertIn(RELEASES_LATEST, source)
        for marker in (
            "SHA256SUMS",
            "pg_local_cache-pg${PG_MAJOR}-linux-${LIBC}-amd64.tar.gz",
            "pg_local_cache-source.tar.gz",
        ):
            self.assertIn(marker, install)
        for source in (readme, homepage, install):
            self.assertIn("/releases/latest/download", source)

    def test_pages_publish_supported_postgresql_and_linux_matrix(self) -> None:
        sources = [
            (ROOT / "index.html").read_text(encoding="utf-8"),
            (ROOT / "README.md").read_text(encoding="utf-8"),
            (ROOT / "docs" / "INSTALL_EXISTING.md").read_text(
                encoding="utf-8"
            ),
            (ROOT / "_layouts" / "default.html").read_text(
                encoding="utf-8"
            ),
        ]
        combined = "\n".join(sources)
        self.assertIn("PostgreSQL 14–18", combined)
        for major in range(14, 19):
            self.assertIn(f'"PostgreSQL {major}"', sources[-1])
        self.assertIn('"operatingSystem": "Linux amd64 (glibc or musl)"', combined)
        self.assertIn("PostgreSQL 14–18", (ROOT / "_config.yml").read_text())

    def test_published_assets_keep_only_current_benchmark_and_social_files(self) -> None:
        self.assertTrue((ROOT / "assets" / "benchmark-evidence" / "fe2d23c").is_dir())
        self.assertFalse((ROOT / "assets" / "benchmark-evidence" / "ee221410").exists())
        self.assertTrue((ROOT / "assets" / "social-card.png").is_file())
        self.assertFalse((ROOT / "assets" / "social-card.svg").exists())
        self.assertTrue((ROOT / "assets" / "favicon.svg").is_file())

    def test_current_benchmark_evidence_is_complete_and_pinned(self) -> None:
        evidence = ROOT / "assets" / "benchmark-evidence" / "fe2d23c"
        comparison = evidence / "comparison-smoke.zip"
        sql_only = evidence / "sql-only-benchmark-smoke.zip"
        self.assertEqual(
            hashlib.sha256(comparison.read_bytes()).hexdigest(),
            "fc624e7ebed11b10c8470d11e7d2a91855813e04f9fb809e62e4f0852f7c8a76",
        )
        self.assertEqual(
            hashlib.sha256(sql_only.read_bytes()).hexdigest(),
            "22be445d210138be086da186bdbe4c7fb1e3543b4a26b3f98b90c8099e929d02",
        )
        with zipfile.ZipFile(comparison) as archive:
            self.assertEqual(
                set(archive.namelist()), {"whole-row.json", "whole-row.md"}
            )
            report = json.loads(archive.read("whole-row.json"))
        with zipfile.ZipFile(sql_only) as archive:
            self.assertEqual(
                set(archive.namelist()), {"sql-only.json", "sql-only.md"}
            )
            sql_only_report = json.loads(archive.read("sql-only.json"))
        self.assertEqual(
            report["environment"]["source_revision"],
            "fe2d23c87ddc7e523ada2951376ebcb7d8570fb1",
        )
        self.assertEqual(report["gate"]["status"], "PASS")
        self.assertEqual(
            sql_only_report["environment"]["source_revision"],
            "fe2d23c87ddc7e523ada2951376ebcb7d8570fb1",
        )
        self.assertEqual(sql_only_report["gate"]["status"], "PASS")
        self.assertAlmostEqual(
            sql_only_report["protocols"]["prepared"]["cached_mode"]
            ["summary"]["median_operations_per_second"],
            64954.164064,
        )
        self.assertAlmostEqual(
            sql_only_report["protocols"]["extended"]["cached_mode"]
            ["summary"]["median_operations_per_second"],
            66155.7912,
        )
        self.assertEqual(
            {lane["query"] for lane in report["ordinary_sql"].values()},
            {
                "SELECT * FROM public.pg_local_cache_whole_row_comparison "
                "WHERE tenant_id = 7 AND id = :key;",
                "SELECT metadata, payload, enabled, amount, note, id, "
                "tenant_id FROM public.pg_local_cache_whole_row_comparison "
                "WHERE tenant_id = 7 AND id = :key;",
                "SELECT payload, metadata, id, tenant_id FROM public."
                "pg_local_cache_whole_row_comparison WHERE id = :key AND "
                "tenant_id = 7;",
            },
        )

    def test_public_docs_use_the_current_repository_and_default_branch(self) -> None:
        sources = [
            (ROOT / "README.md").read_text(encoding="utf-8"),
            (ROOT / "_config.yml").read_text(encoding="utf-8"),
            *(path.read_text(encoding="utf-8") for path in PUBLISHED_DOCUMENTS),
        ]
        combined = "\n".join(sources)
        self.assertNotIn("aicopilot-fr/pg_local_cache", combined)
        self.assertNotIn("/pg_local_cache/blob/main/", combined)
        self.assertIn("profundium/pg_local_cache", combined)

    def test_docs_prefer_ordinary_sql_for_native_tuples(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        technical = (ROOT / "docs" / "TECHNICAL.md").read_text(
            encoding="utf-8"
        )
        install = (ROOT / "docs" / "INSTALL_EXISTING.md").read_text(
            encoding="utf-8"
        )
        for source in (readme, technical, install):
            self.assertIn("SELECT * FROM public.items WHERE id =", source)
            self.assertIn("SELECT value", source)
            self.assertNotIn("NULL::public.items", source)
        self.assertIn("No result-type witness", technical)

    def test_every_published_document_has_metadata(self) -> None:
        documents = {
            "INSTALL_EXISTING.md": "/docs/INSTALL_EXISTING.html",
            "BENCHMARKS.md": "/docs/BENCHMARKS.html",
            "MONITORING.md": "/docs/MONITORING.html",
            "TECHNICAL.md": "/docs/TECHNICAL.html",
        }
        for name, permalink in documents.items():
            with self.subTest(name=name):
                metadata = front_matter(ROOT / "docs" / name)
                self.assertEqual(metadata.get("layout"), "doc")
                self.assertEqual(metadata.get("permalink"), permalink)
                self.assertTrue(metadata.get("title"))
                self.assertGreater(len(metadata.get("description", "")), 50)

    def test_published_documents_do_not_link_to_unbuilt_markdown(self) -> None:
        github_source_prefix = (
            "https://github.com/profundium/pg_local_cache/blob/master/"
        )
        for path in PUBLISHED_DOCUMENTS:
            source = path.read_text(encoding="utf-8")
            for match in MARKDOWN_LINK.finditer(source):
                target = match.group(1).strip()
                target_path = target.split("#", 1)[0]
                if not target_path.lower().endswith(".md"):
                    continue
                with self.subTest(document=path.name, target=target):
                    self.assertTrue(
                        target.startswith(github_source_prefix),
                        f"{path}: relative .md link is not built by Pages: {target}",
                    )

    def test_homepage_terminal_uses_one_explicit_transaction(self) -> None:
        source = (ROOT / "index.html").read_text(encoding="utf-8")
        begin = source.index('app=&gt; <span class="sql">BEGIN;</span>')
        update = source.index(
            'app=*&gt; <span class="sql">UPDATE</span>', begin
        )
        commit = source.index(
            'app=*&gt; <span class="sql">COMMIT;</span>', update
        )
        self.assertLess(begin, update)
        self.assertLess(update, commit)
        self.assertIn("SET value = 'updated' WHERE id = 42;", source)
        self.assertNotIn("SET price", source)
        self.assertNotIn("Buffers: shared hit=0 read=0", source)
        self.assertIn("fenced before commit became visible", source)
        self.assertIn("#security-boundary", source)
        self.assertNotIn(
            "'/docs/TECHNICAL.html' | relative_url }}#security\"", source
        )

    def test_default_layout_has_social_metadata(self) -> None:
        layout = (ROOT / "_layouts" / "default.html").read_text(
            encoding="utf-8"
        )
        for marker in (
            'rel="canonical"',
            'property="og:title"',
            'property="og:description"',
            'property="og:image"',
            'content="1200"',
            'content="630"',
            'name="twitter:card" content="summary_large_image"',
            'name="twitter:image"',
            '"@type": "SoftwareSourceCode"',
            'href="#main-content"',
        ):
            self.assertIn(marker, layout)

        social_card = ROOT / "assets" / "social-card.png"
        self.assertTrue(social_card.is_file())
        self.assertGreater(social_card.stat().st_size, 10_000)
        header = social_card.read_bytes()[:24]
        self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", header[16:24]), (1200, 630))

        structured = re.search(
            r'<script type="application/ld\+json">\s*(.*?)\s*</script>',
            layout,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(structured)
        payload = (structured.group(1) if structured else "").replace(
            "{{ site.description | jsonify }}", '"description"'
        )
        self.assertEqual(json.loads(payload)["@type"], "SoftwareSourceCode")

    def test_pages_workflow_uses_pinned_official_actions(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(
            encoding="utf-8"
        )
        expected = {
            "actions/checkout": "11bd71901bbe5b1630ceea73d27597364c9af683",
            "actions/configure-pages": "983d7736d9b0ae728b81ab479565c72886d7745b",
            "actions/jekyll-build-pages": "44a6e6beabd48582f863aeeb6cb2151cc1716697",
            "actions/upload-pages-artifact": "56afc609e74202658d3ffba0e8f6dda462b719fa",
            "actions/deploy-pages": "d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e",
        }
        for action, sha in expected.items():
            self.assertIn(f"uses: {action}@{sha}", workflow)
        self.assertNotRegex(workflow, r"uses:\s+[^\n]+@(v\d+|main)\s*$")
        self.assertIn("pages: write", workflow)
        self.assertIn("id-token: write", workflow)

    def test_pull_requests_build_pages_without_deploying(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("  pull_request:\n", workflow)
        self.assertIn("if: github.event_name == 'pull_request'", workflow)
        self.assertIn("Build Jekyll site without deployment", workflow)
        self.assertIn("if: github.event_name != 'pull_request'", workflow)
        self.assertIn("group: pg_local_cache-pages-${{ github.ref }}", workflow)
        validation = workflow[
            workflow.index("  validate:") : workflow.index("  deploy:")
        ]
        self.assertIn("actions/jekyll-build-pages@", validation)
        self.assertNotIn("actions/configure-pages@", validation)
        self.assertNotIn("actions/upload-pages-artifact@", validation)
        self.assertNotIn("actions/deploy-pages@", validation)
        self.assertEqual(workflow.count("actions/deploy-pages@"), 1)

    def test_crawler_files_use_the_configured_origin(self) -> None:
        config = (ROOT / "_config.yml").read_text(encoding="utf-8")
        robots = (ROOT / "robots.txt").read_text(encoding="utf-8")
        sitemap = (ROOT / "sitemap.xml").read_text(encoding="utf-8")
        self.assertIn("url: https://profundium.github.io", config)
        self.assertIn("repository: profundium/pg_local_cache", config)
        self.assertIn("baseurl: /pg_local_cache", config)
        self.assertIn("Sitemap:", robots)
        for page in (
            "/docs/INSTALL_EXISTING.html",
            "/docs/BENCHMARKS.html",
            "/docs/MONITORING.html",
            "/docs/TECHNICAL.html",
        ):
            self.assertIn(page, sitemap)

    def test_styles_cover_accessibility_and_responsive_layouts(self) -> None:
        css = (ROOT / "assets" / "site.css").read_text(encoding="utf-8")
        self.assertIn(".skip-link:focus", css)
        self.assertIn("@media (max-width: 720px)", css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn("@media (prefers-color-scheme: dark)", css)

    def test_navigation_and_document_toc_are_progressive(self) -> None:
        default_layout = (ROOT / "_layouts" / "default.html").read_text(
            encoding="utf-8"
        )
        doc_layout = (ROOT / "_layouts" / "doc.html").read_text(
            encoding="utf-8"
        )
        css = (ROOT / "assets" / "site.css").read_text(encoding="utf-8")
        javascript = (ROOT / "assets" / "site.js").read_text(encoding="utf-8")
        self.assertIn('class="no-js"', default_layout)
        self.assertIn("classList.replace('no-js', 'js')", default_layout)
        self.assertIn(".js .site-nav", css)
        self.assertIn("data-doc-toc", doc_layout)
        self.assertIn('aria-current="page"', doc_layout)
        self.assertIn("data-doc-toc-list", javascript)
        self.assertIn("document.querySelectorAll('.doc-content table')", javascript)
        self.assertIn("Scrollable data table", javascript)
        self.assertIn("region.setAttribute('tabindex', '0')", javascript)


if __name__ == "__main__":
    unittest.main()
