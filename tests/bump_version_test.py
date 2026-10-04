#!/usr/bin/env python3
"""Behavioral tests for release version bumps."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]


def command(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=check,
    )


def has_checkout_fixtures() -> bool:
    if not (ROOT / ".git").exists():
        return False
    try:
        control = (ROOT / "pg_local_cache.control").read_text(encoding="utf-8")
    except OSError:
        return False
    match = re.search(r"^default_version = '([^']+)'$", control, re.M)
    if match is None:
        return False
    fixtures = (
        "scripts/bump-version.sh",
        "pg_local_cache.control",
        "META.json",
        "src/pg_local_cache.h",
        "compose.yaml",
        "CHANGELOG.md",
        "debian/changelog",
        "rpm/pg_local_cache.spec",
        f"sql/pg_local_cache--{match.group(1)}.sql",
    )
    return all((ROOT / relative).is_file() for relative in fixtures)


@unittest.skipUnless(
    has_checkout_fixtures(),
    "version bump tests require a git checkout and checkout-only fixtures",
)
class BumpVersionChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        control = (ROOT / "pg_local_cache.control").read_text(encoding="utf-8")
        self.old = re.search(r"^default_version = '([^']+)'$", control, re.M).group(1)
        major, minor, patch = map(int, self.old.split("."))
        self.new = f"{major}.{minor}.{patch + 1}"
        self.install_sql = f"sql/pg_local_cache--{self.old}.sql"

        for relative in (
            "scripts/bump-version.sh",
            "pg_local_cache.control",
            "META.json",
            "src/pg_local_cache.h",
            "compose.yaml",
            "CHANGELOG.md",
            "debian/changelog",
            "rpm/pg_local_cache.spec",
            self.install_sql,
        ):
            destination = self.repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)

        # This checkout is already on a released version. Seed the isolated
        # fixture with an Unreleased section so the tests exercise the next bump.
        changelog_path = self.repo / "CHANGELOG.md"
        changelog = changelog_path.read_text(encoding="utf-8")
        release = re.search(
            rf"(?m)^## \[{re.escape(self.old)}\](?: - \d{{4}}-\d{{2}}-\d{{2}})?\n",
            changelog,
        )
        if release is None:
            raise AssertionError(f"missing current release section for {self.old}")
        next_release = re.search(r"(?m)^## \[", changelog[release.end():])
        release_end = release.end() + next_release.start() if next_release else len(changelog)
        notes = changelog[release.end():release_end].strip()
        if not notes:
            raise AssertionError("current release section has no notes")
        changelog_path.write_text(
            changelog[:release.start()] + "## [Unreleased]\n\n" + notes + "\n\n" +
            changelog[release.start():],
            encoding="utf-8",
        )

        command(["git", "init", "--quiet"], self.repo)
        command(["git", "config", "user.name", "Bump Version Test"], self.repo)
        command(["git", "config", "user.email", "bump-version-test@example.invalid"], self.repo)
        command(["git", "add", "."], self.repo)
        command(["git", "commit", "--quiet", "-m", "baseline"], self.repo)
        command(["git", "tag", f"v{self.old}"], self.repo)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def bump(self, *options: str) -> subprocess.CompletedProcess[str]:
        return command(["bash", "scripts/bump-version.sh", *options, self.new], self.repo, check=False)

    def test_noop_bump_creates_stub_and_keeps_install_sql(self) -> None:
        before = (self.repo / self.install_sql).read_bytes()

        result = self.bump()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.repo / f"sql/pg_local_cache--{self.new}.sql").read_bytes(), before)
        migration = self.repo / f"sql/pg_local_cache--{self.old}--{self.new}.sql"
        self.assertIn("SQL objects are unchanged", migration.read_text(encoding="utf-8"))
        changelog = (self.repo / "debian/changelog").read_text(encoding="utf-8")
        self.assertTrue(changelog.startswith(f"pg-local-cache ({self.new}-1) unstable;"))
        project_changelog = (self.repo / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(
            f"## [{self.new}] - {datetime.now(timezone.utc).date().isoformat()}",
            project_changelog,
        )
        self.assertRegex(
            project_changelog,
            rf"(?m)^## \[Unreleased\]\n\n## \[{re.escape(self.new)}\] - ",
        )
        self.assertIn(
            "### Added\n\n- Add the SIGHUP `pg_local_cache.enabled` operational kill switch.",
            project_changelog,
        )
        rpm_spec = (self.repo / "rpm/pg_local_cache.spec").read_text(encoding="utf-8")
        self.assertRegex(rpm_spec, rf"(?m)^Version:\s*{re.escape(self.new)}\s*$")

    def test_bump_updates_compare_links(self) -> None:
        path = self.repo / "CHANGELOG.md"
        changelog = path.read_text(encoding="utf-8")
        changelog += f"\n[Unreleased]: https://github.com/example/project/compare/v{self.old}...HEAD\n"
        path.write_text(changelog, encoding="utf-8")

        result = self.bump()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        updated = path.read_text(encoding="utf-8")
        self.assertIn(
            f"[Unreleased]: https://github.com/example/project/compare/v{self.new}...HEAD\n",
            updated,
        )
        self.assertIn(
            f"[{self.new}]: https://github.com/example/project/compare/v{self.old}...v{self.new}\n",
            updated,
        )

    def test_refuses_empty_unreleased_section_before_mutating(self) -> None:
        changelog_path = self.repo / "CHANGELOG.md"
        changelog_path.write_text(
            f"# Changelog\n\n## [Unreleased]\n\n## [{self.old}] - 2026-09-16\n\n"
            "### Fixed\n\n- Existing release.\n",
            encoding="utf-8",
        )

        result = self.bump()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CHANGELOG.md [Unreleased] section is empty", result.stderr)
        self.assertTrue((self.repo / self.install_sql).is_file())
        self.assertFalse((self.repo / f"sql/pg_local_cache--{self.new}.sql").exists())

    def test_refuses_changed_install_sql_without_migration(self) -> None:
        (self.repo / self.install_sql).write_text("-- developer changed schema\n", encoding="utf-8")

        result = self.bump()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("write sql/pg_local_cache--", result.stderr)
        self.assertIn("before bumping", result.stderr)
        self.assertFalse((self.repo / f"sql/pg_local_cache--{self.new}.sql").exists())

    def test_refuses_changed_install_sql_with_comment_only_migration(self) -> None:
        (self.repo / self.install_sql).write_text("-- developer changed schema\n", encoding="utf-8")
        migration = self.repo / f"sql/pg_local_cache--{self.old}--{self.new}.sql"
        migration.write_text("\\echo migration pending\n-- no SQL yet\n", encoding="utf-8")

        result = self.bump()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("with a SQL statement", result.stderr)
        self.assertFalse((self.repo / f"sql/pg_local_cache--{self.new}.sql").exists())

    def test_refuses_missing_tag_without_sql_upgrade(self) -> None:
        command(["git", "tag", "--delete", f"v{self.old}"], self.repo)

        result = self.bump()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("with a SQL statement", result.stderr)
        self.assertFalse((self.repo / f"sql/pg_local_cache--{self.new}.sql").exists())

    def test_refuses_missing_tag_with_comment_only_upgrade(self) -> None:
        command(["git", "tag", "--delete", f"v{self.old}"], self.repo)
        migration = self.repo / f"sql/pg_local_cache--{self.old}--{self.new}.sql"
        migration.write_text("\\echo migration pending\n-- no SQL yet\n/* still empty */\n", encoding="utf-8")

        result = self.bump()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("with a SQL statement", result.stderr)
        self.assertFalse((self.repo / f"sql/pg_local_cache--{self.new}.sql").exists())

    def test_missing_tag_accepts_sql_upgrade(self) -> None:
        command(["git", "tag", "--delete", f"v{self.old}"], self.repo)
        migration = self.repo / f"sql/pg_local_cache--{self.old}--{self.new}.sql"
        migration.write_text("-- planned schema migration\nALTER TABLE example ADD COLUMN added integer;\n", encoding="utf-8")
        before = migration.read_bytes()

        result = self.bump()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(migration.read_bytes(), before)

    def test_missing_tag_requires_explicit_no_baseline_override(self) -> None:
        command(["git", "tag", "--delete", f"v{self.old}"], self.repo)

        result = self.bump("--no-baseline")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.repo / f"sql/pg_local_cache--{self.new}.sql").is_file())

    def test_keeps_handwritten_migration_when_install_sql_changed(self) -> None:
        (self.repo / self.install_sql).write_text("-- developer changed schema\n", encoding="utf-8")
        migration = self.repo / f"sql/pg_local_cache--{self.old}--{self.new}.sql"
        migration.write_text("-- developer migration\nALTER TABLE example ADD COLUMN added integer;\n", encoding="utf-8")
        before = migration.read_bytes()

        result = self.bump()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(migration.read_bytes(), before)

    def test_check_fails_when_metadata_version_disagrees(self) -> None:
        metadata_path = self.repo / "META.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["version"] = self.new
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        result = command(["bash", "scripts/bump-version.sh", "--check"], self.repo, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("version metadata, header, or install SQL disagree", result.stderr)

    def test_check_fails_when_debian_changelog_version_disagrees(self) -> None:
        changelog_path = self.repo / "debian/changelog"
        changelog_path.write_text(
            changelog_path.read_text(encoding="utf-8").replace(
                f"({self.old}-1)", f"({self.new}-1)", 1
            ),
            encoding="utf-8",
        )

        result = command(["bash", "scripts/bump-version.sh", "--check"], self.repo, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Debian changelog version does not match", result.stderr)

    def test_check_fails_when_project_changelog_section_is_missing(self) -> None:
        path = self.repo / "CHANGELOG.md"
        path.write_text("# Changelog\n\n## [Unreleased]\n\n- Pending.\n", encoding="utf-8")

        result = command(["bash", "scripts/bump-version.sh", "--check"], self.repo, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CHANGELOG.md has no section for control default_version", result.stderr)

    def test_check_fails_when_rpm_spec_version_disagrees(self) -> None:
        spec_path = self.repo / "rpm/pg_local_cache.spec"
        spec_path.write_text(
            spec_path.read_text(encoding="utf-8").replace(
                f"Version:        {self.old}", f"Version:        {self.new}", 1
            ),
            encoding="utf-8",
        )

        result = command(["bash", "scripts/bump-version.sh", "--check"], self.repo, check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RPM spec version does not match", result.stderr)


if __name__ == "__main__":
    unittest.main()
