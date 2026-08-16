#!/usr/bin/env python3
"""Contracts for the public documentation site."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALL_COMMAND = (
    "curl -fsSL https://github.com/profundium/pg_local_cache/releases/latest/"
    "download/install-latest.sh | bash -s -- app"
)


class PagesContracts(unittest.TestCase):
    def test_pages_deploys_the_current_public_docs(self) -> None:
        workflow = (ROOT / ".github/workflows/pages.yml").read_text()
        self.assertIn("actions/jekyll-build-pages", workflow)
        self.assertIn("actions/deploy-pages", workflow)
        for path in (
            "index.html",
            "docs/INSTALL_EXISTING.md",
            "docs/TECHNICAL.md",
            "PGXN.md",
        ):
            self.assertTrue((ROOT / path).is_file())

    def test_homepage_exposes_installer_and_mget(self) -> None:
        homepage = (ROOT / "index.html").read_text()
        self.assertIn(INSTALL_COMMAND, homepage)
        self.assertIn("local_cache.mget", homepage)
        self.assertNotIn("BENCHMARKS", homepage)
        self.assertNotIn("MONITORING", homepage)

    def test_local_markdown_links_resolve(self) -> None:
        failures: list[str] = []
        for document in (ROOT / "README.md", ROOT / "PGXN.md", *(ROOT / "docs").glob("*.md")):
            for target in re.findall(r"\[[^]]*\]\(([^)]+)\)", document.read_text()):
                if target.startswith(("http://", "https://", "#", "mailto:")):
                    continue
                path = target.split("#", 1)[0]
                if path and not (document.parent / path).resolve().exists():
                    failures.append(f"{document.relative_to(ROOT)} -> {target}")
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
