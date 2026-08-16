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

    def test_pgxn_artifact_has_one_fresh_sql_only_lifecycle(self) -> None:
        source = (ROOT / "tests/compatibility_matrix.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("python3 -m zipfile -e", source)
        self.assertIn("! command -v python3", source)
        self.assertIn('docker wait "$pgxn_build_container"', source)
        self.assertIn('--volume "$pgxn_release_root:/release"', source)
        self.assertNotIn('docker cp "$pgxn_build_container:', source)
        self.assertIn('--volume "$pgxn_release_root:/artifact:ro"', source)
        self.assertNotIn('docker cp "$pgxn_release_root/', source)
        self.assertIn("--mode sql-only", source)
        self.assertIn("/artifact/install.sh verify", source)
        self.assertIn("local_cache.stats() ->> 'sql_cache_hits'", source)
        self.assertIn("test_upgrade_preserves_an_existing_extension_owner", source)
        self.assertIn("^Ran 8 tests in ", source)
        self.assertIn('rm -f -- "$source_archive"', source)
        self.assertIn('$repository_directory/compose.demo.yaml', source)
        self.assertIn('$repository_directory/docker/initdb/020_demo.sql', source)

    def test_full_runtime_is_bounded_to_one_major_per_libc(self) -> None:
        source = (ROOT / "tests/compatibility_matrix.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('if [[ "$major" == 16 ]]; then', source)
        self.assertIn("artifact_preflight_smoke", source)
        self.assertIn(
            'fetch_log="$results_directory/pg16-${fetch_libc}-fetcher-tests.txt"',
            source,
        )
        self.assertGreaterEqual(
            source.count("PostgreSQL init process complete; ready for start up."),
            2,
        )


if __name__ == "__main__":
    unittest.main()
