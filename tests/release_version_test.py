#!/usr/bin/env python3
"""Contracts for semantic version preparation and release handoff."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

SPEC = importlib.util.spec_from_file_location(
    "pg_local_cache_auto_version",
    SCRIPTS / "auto_version.py",
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load scripts/auto_version.py")
auto_version = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = auto_version
SPEC.loader.exec_module(auto_version)

BASE_VERSION = auto_version.read_control_version(ROOT)
MINOR_VERSION = BASE_VERSION.bump("minor")
PATCH_VERSION = BASE_VERSION.bump("patch")


def run(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        list(arguments),
        cwd=root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


def copy_fixture(destination: Path) -> Path:
    root = destination / "repository"
    root.mkdir()
    for directory in (
        ".github",
        "assets",
        "docker",
        "scripts",
        "sql",
        "src",
        "tests",
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)

    for relative in (
        "META.json",
        "Makefile",
        "README.md",
        "LICENSE",
        "compose.yaml",
        "pg_local_cache.control",
        "src/pg_local_cache.h",
        "src/pg_local_cache_worker.c",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    for source in (ROOT / "sql").glob("pg_local_cache--*.sql"):
        if source.name == "pg_local_cache--unreleased.sql":
            continue
        shutil.copy2(source, root / "sql" / source.name)

    run(root, "git", "init", "--initial-branch=master")
    run(root, "git", "config", "user.name", "Release Test")
    run(root, "git", "config", "user.email", "release-test@example.invalid")
    run(root, "git", "add", "-A")
    run(root, "git", "commit", "-m", f"chore: establish {BASE_VERSION} baseline")
    run(root, "git", "tag", f"v{BASE_VERSION}")
    return root


def commit_file(root: Path, relative: str, content: str, message: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    run(root, "git", "add", relative)
    run(root, "git", "commit", "-m", message)


class VersionClassificationTests(unittest.TestCase):
    def test_conventional_commits_choose_highest_semantic_bump(self) -> None:
        self.assertEqual(auto_version.classify_message("docs: clarify setup"), "none")
        self.assertEqual(auto_version.classify_message("fix: close race"), "patch")
        self.assertEqual(auto_version.classify_message("feat(sql): add IN"), "minor")
        self.assertEqual(
            auto_version.classify_message(
                "feat!: replace the public API\n\nBREAKING CHANGE: old API removed"
            ),
            "major",
        )
        self.assertEqual(
            auto_version.classify_message(
                "docs: mark incompatible release\n\nBREAKING CHANGE: public API changed"
            ),
            "major",
        )
        self.assertEqual(auto_version.classify_message("Improve validation"), "patch")
        self.assertEqual(auto_version.classify_message("Merge pull request #42"), "none")
        self.assertEqual(
            auto_version.classify_messages(
                ("docs: explain", "fix: one", "feat: batch reads")
            ),
            "minor",
        )


class VersionWorkflowTests(unittest.TestCase):
    def test_feature_bump_materializes_all_release_files_and_hands_off(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            root = copy_fixture(Path(raw_directory))
            commit_file(
                root,
                "README.md",
                (root / "README.md").read_text() + "\nFeature marker.\n",
                "feat: add transparent batch reads",
            )

            plan = auto_version.plan_version(root)
            self.assertEqual(plan.action, "bump")
            self.assertEqual(plan.next_version, MINOR_VERSION)
            self.assertEqual(plan.bump, "minor")
            auto_version.apply_version(root, plan)

            self.assertIn(
                f"default_version = '{MINOR_VERSION}'",
                (root / "pg_local_cache.control").read_text(),
            )
            metadata = json.loads((root / "META.json").read_text())
            self.assertEqual(metadata["version"], str(MINOR_VERSION))
            self.assertEqual(
                metadata["provides"]["pg_local_cache"]["file"],
                f"sql/pg_local_cache--{MINOR_VERSION}.sql",
            )
            header = (root / "src" / "pg_local_cache.h").read_text()
            self.assertIn(
                f'#define PGLC_VERSION "{MINOR_VERSION}"', header
            )
            self.assertIn(
                f'#define PGLC_VERSION_LENGTH "{len(str(MINOR_VERSION))}"', header
            )
            self.assertIn(
                f"image: pg_local_cache:{MINOR_VERSION}",
                (root / "compose.yaml").read_text(),
            )
            self.assertTrue(
                (root / f"sql/pg_local_cache--{MINOR_VERSION}.sql").is_file()
            )
            upgrade = (
                root
                / f"sql/pg_local_cache--{BASE_VERSION}--{MINOR_VERSION}.sql"
            )
            self.assertIn("SQL objects are unchanged", upgrade.read_text())
            makefile = (root / "Makefile").read_text()
            self.assertIn(f"sql/pg_local_cache--{MINOR_VERSION}.sql", makefile)
            self.assertIn(
                f"sql/pg_local_cache--{BASE_VERSION}--{MINOR_VERSION}.sql",
                makefile,
            )
            auto_version.validate_repository(root)

            run(root, "git", "add", "-A")
            run(root, "git", "commit", "-m", f"chore(release): v{MINOR_VERSION}")
            handoff = auto_version.plan_version(root)
            self.assertEqual(handoff.action, "release")
            self.assertTrue(handoff.release_ready)
            self.assertFalse(handoff.changed)
            self.assertEqual(handoff.next_version, MINOR_VERSION)

            run(root, "git", "tag", f"v{MINOR_VERSION}")
            complete = auto_version.plan_version(root)
            self.assertEqual(complete.action, "none")
            self.assertFalse(complete.release_ready)

    def test_docs_only_commit_does_not_create_a_release_unless_forced(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            root = copy_fixture(Path(raw_directory))
            commit_file(
                root,
                "README.md",
                (root / "README.md").read_text() + "\nDocs marker.\n",
                "docs: improve PGXN instructions",
            )
            automatic = auto_version.plan_version(root)
            self.assertEqual(automatic.action, "none")
            self.assertEqual(automatic.bump, "none")

            forced = auto_version.plan_version(root, "patch")
            self.assertEqual(forced.action, "bump")
            self.assertEqual(forced.next_version, PATCH_VERSION)
            auto_version.apply_version(root, forced)
            run(root, "git", "add", "-A")
            run(root, "git", "commit", "-m", f"chore(release): v{PATCH_VERSION}")
            handoff = auto_version.plan_version(root)
            self.assertEqual(handoff.action, "release")
            self.assertEqual(handoff.current_version, PATCH_VERSION)

    def test_sql_changes_require_an_incremental_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            root = copy_fixture(Path(raw_directory))
            install = root / f"sql/pg_local_cache--{BASE_VERSION}.sql"
            baseline = install.read_text()
            install.write_text(baseline + "\nCREATE FUNCTION local_cache.fixture() RETURNS int LANGUAGE sql AS 'SELECT 1';\n")
            run(root, "git", "add", str(install.relative_to(root)))
            run(root, "git", "commit", "-m", "feat(sql): add fixture function")

            plan = auto_version.plan_version(root)
            with self.assertRaisesRegex(
                auto_version.VersionError,
                "incremental migration",
            ):
                auto_version.apply_version(root, plan)

            fragment = root / "sql/pg_local_cache--unreleased.sql"
            fragment.write_text(
                "CREATE FUNCTION local_cache.fixture() RETURNS int "
                "LANGUAGE sql AS 'SELECT 1';\n",
                encoding="utf-8",
            )
            run(root, "git", "add", str(fragment.relative_to(root)))
            run(root, "git", "commit", "-m", "build: add SQL upgrade fragment")

            plan = auto_version.plan_version(root)
            auto_version.apply_version(root, plan)
            self.assertEqual(
                (root / f"sql/pg_local_cache--{BASE_VERSION}.sql").read_text(),
                baseline,
            )
            self.assertIn(
                "local_cache.fixture",
                (root / f"sql/pg_local_cache--{MINOR_VERSION}.sql").read_text(),
            )
            upgrade = (
                root
                / f"sql/pg_local_cache--{BASE_VERSION}--{MINOR_VERSION}.sql"
            ).read_text()
            self.assertIn(
                f"ALTER EXTENSION pg_local_cache UPDATE TO '{MINOR_VERSION}'",
                upgrade,
            )
            self.assertIn("local_cache.fixture", upgrade)
            self.assertFalse(fragment.exists())

    def test_released_sql_history_cannot_be_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            root = copy_fixture(Path(raw_directory))
            old_upgrade = root / "sql/pg_local_cache--1.0.0--1.1.0.sql"
            old_upgrade.write_text(old_upgrade.read_text() + "\n-- rewritten\n")
            run(root, "git", "add", str(old_upgrade.relative_to(root)))
            run(root, "git", "commit", "-m", "fix: rewrite old migration")
            plan = auto_version.plan_version(root)
            with self.assertRaisesRegex(
                auto_version.VersionError,
                "released/versioned SQL files changed",
            ):
                auto_version.apply_version(root, plan)

    def test_released_sql_history_cannot_be_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            root = copy_fixture(Path(raw_directory))
            old_upgrade = root / "sql/pg_local_cache--1.0.0--1.1.0.sql"
            old_upgrade.unlink()
            run(root, "git", "add", str(old_upgrade.relative_to(root)))
            run(root, "git", "commit", "-m", "fix: delete old migration")
            plan = auto_version.plan_version(root)
            with self.assertRaisesRegex(
                auto_version.VersionError,
                "released/versioned SQL files changed",
            ):
                auto_version.apply_version(root, plan)


class VersionWorkflowSourceTests(unittest.TestCase):
    def test_version_workflow_commits_once_and_dispatches_ci(self) -> None:
        source = (
            ROOT / ".github" / "workflows" / "auto-version.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('branches: ["master"]', source)
        self.assertIn("workflow_dispatch:", source)
        self.assertIn("contents: write", source)
        self.assertIn("actions: write", source)
        self.assertIn('python3 scripts/auto_version.py \\', source)
        self.assertIn('--bump "$VERSION_BUMP"', source)
        self.assertIn('git commit \\', source)
        self.assertIn('-m "chore(release): v${RELEASE_VERSION}"', source)
        self.assertIn('-m "[skip version]"', source)
        self.assertIn('release_ref="release/v${RELEASE_VERSION}"', source)
        self.assertIn(
            'git push --atomic origin HEAD:master "HEAD:refs/heads/${release_ref}"',
            source,
        )
        self.assertIn(
            'gh workflow run ci.yml --ref "$release_ref" --field release=true',
            source,
        )
        self.assertNotIn(
            "gh workflow run ci.yml --ref master --field release=true", source
        )

        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("github.event_name == 'workflow_dispatch' && inputs.release", ci)
        self.assertIn("GH_REPO: ${{ github.repository }}", ci)
        self.assertIn("gh workflow run release.yml", ci)
        self.assertIn('--raw-field "ref=${GITHUB_SHA}"', ci)
        self.assertIn('--raw-field "ci_run_id=${GITHUB_RUN_ID}"', ci)
        self.assertIn("--field publish_pgxn=true", ci)

    def test_release_workflow_skips_unprepared_and_superseded_commits(self) -> None:
        source = (
            ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("release_ready: ${{ steps.meta.outputs.release_ready }}", source)
        self.assertIn("python3 scripts/auto_version.py --json", source)
        self.assertIn('if [[ "$action" != "release" ]]', source)
        self.assertIn("master advanced", source)
        self.assertIn("release_ready=false", source)
        self.assertIn("release_ready=true", source)
        self.assertIn(
            "if: needs.metadata.outputs.release_ready == 'true'", source
        )
        self.assertIn("|| inputs.ref", source)
        self.assertIn("|| inputs.ci_run_id", source)
        self.assertIn("gh workflow run pgxn.yml", source)
        self.assertIn("--field publish=true", source)


if __name__ == "__main__":
    unittest.main()
