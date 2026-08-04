#!/usr/bin/env python3
"""Static contract for downloadable PostgreSQL compatibility artifacts."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReleaseMatrixContracts(unittest.TestCase):
    def test_release_builds_every_supported_linux_lane(self) -> None:
        source = (ROOT / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("postgres_major: [14, 15, 16, 17, 18]", source)
        self.assertIn("variant: bookworm", source)
        self.assertIn("libc: glibc", source)
        self.assertIn("variant: alpine3.23", source)
        self.assertIn("libc: musl", source)
        self.assertIn("-eq 10", source)

        ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("variant: [bookworm, alpine3.23]", ci)
        self.assertIn("PGLC_MATRIX_VARIANTS: ${{ matrix.variant }}", ci)

    def test_latest_assets_have_stable_exact_names_and_metadata(self) -> None:
        source = (ROOT / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'asset="pg_local_cache-pg${POSTGRES_MAJOR}-linux-${LIBC}-amd64"',
            source,
        )
        self.assertIn("dist/pg_local_cache-source.tar.gz", source)
        self.assertIn("format=1", source)
        for key in ("version", "postgres_major", "os", "libc", "architecture"):
            self.assertIn(f"printf '{key}=", source)


if __name__ == "__main__":
    unittest.main()
