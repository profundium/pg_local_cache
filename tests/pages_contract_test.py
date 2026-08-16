#!/usr/bin/env python3
"""Documentation contracts for the mget-only product surface."""

from __future__ import annotations

from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
