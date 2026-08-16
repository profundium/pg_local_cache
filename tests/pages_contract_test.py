#!/usr/bin/env python3
"""Documentation contracts for the mget-only product surface."""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
DOCS = (
    ROOT / "README.md",
    ROOT / "docs" / "INSTALL_EXISTING.md",
    ROOT / "docs" / "TECHNICAL.md",
    ROOT / "docs" / "BENCHMARKS.md",
    ROOT / "benchmarks" / "SCENARIOS.md",
)
TEXT = "\n".join(path.read_text(encoding="utf-8") for path in DOCS)
HOMEPAGE = (ROOT / "index.html").read_text(encoding="utf-8")


class DocumentationContracts(unittest.TestCase):
    def test_product_surface_is_mget_only(self) -> None:
        for retired in (
            "local_cache.get(",
            "pg_local_cache.sql_cache",
            "Custom Scan",
            "transparent SQL",
            "planner hook",
        ):
            self.assertNotIn(retired, TEXT)
            self.assertNotIn(retired, HOMEPAGE)
        self.assertIn("local_cache.mget", TEXT)
        self.assertIn("Ordinary", TEXT)
        self.assertIn("is untouched", TEXT)

    def test_quick_install_is_one_short_command(self) -> None:
        command = (
            "curl -fsSL "
            "https://github.com/profundium/pg_local_cache/releases/latest/"
            "download/install-latest.sh | bash -s -- app"
        )
        readme = DOCS[0].read_text(encoding="utf-8")
        install = DOCS[1].read_text(encoding="utf-8")
        self.assertIn(command, readme)
        self.assertIn(command, install)
        self.assertIn(command, HOMEPAGE)
        self.assertIn("SHA256SUMS", install)

    def test_mget_contract_is_documented(self) -> None:
        for phrase in (
            "1,024 keys",
            "order",
            "duplicates",
            "Composite keys",
            "SECURITY INVOKER",
            "source table",
        ):
            self.assertIn(phrase, TEXT)

    def test_benchmark_docs_have_no_retired_sql_lane(self) -> None:
        benchmark_text = (ROOT / "docs" / "BENCHMARKS.md").read_text()
        source = (ROOT / "benchmarks" / "whole_row.py").read_text()
        self.assertIn("mget", benchmark_text)
        self.assertNotIn("ordinary_sql", source)
        self.assertNotIn("PGLC_BENCH_ROW_SQL_", source)
        self.assertIn("previous transparent-SQL evidence was removed", benchmark_text)

    def test_local_markdown_links_resolve(self) -> None:
        pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
        for source in DOCS:
            text = source.read_text(encoding="utf-8")
            for target in pattern.findall(text):
                if target.startswith(("http://", "https://", "#")):
                    continue
                path_text = target.split("#", 1)[0]
                if not path_text:
                    continue
                with self.subTest(source=source.name, target=target):
                    self.assertTrue((source.parent / path_text).resolve().exists())


if __name__ == "__main__":
    unittest.main()
