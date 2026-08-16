#!/usr/bin/env python3
"""Contracts for PGXN metadata, archives, publishing, and versioning."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load_script(name: str) -> object:
    specification = importlib.util.spec_from_file_location(
        name,
        SCRIPTS / f"{name}.py",
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"could not load scripts/{name}.py")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


validator = _load_script("validate_pgxn_meta")
builder = _load_script("build_pgxn_dist")
CURRENT_VERSION = validator.control_default_version(ROOT)
CURRENT_SQL = ROOT / "sql" / f"pg_local_cache--{CURRENT_VERSION}.sql"



class PgxnMetadataContracts(unittest.TestCase):
    def test_repository_metadata_is_valid_and_searchable(self) -> None:
        metadata = validator.validate_repository(ROOT)
        self.assertEqual(metadata["name"], "pg_local_cache")
        self.assertEqual(metadata["version"], CURRENT_VERSION)
        self.assertEqual(metadata["license"], "mit")
        self.assertEqual(metadata["release_status"], "stable")
        self.assertEqual(
            metadata["prereqs"]["runtime"]["requires"]["PostgreSQL"],
            ">= 14.0.0, < 19.0.0",
        )
        self.assertEqual(
            metadata["provides"]["pg_local_cache"]["file"],
            f"sql/pg_local_cache--{CURRENT_VERSION}.sql",
        )
        self.assertEqual(
            metadata["provides"]["pg_local_cache"]["docfile"],
            "README.md",
        )
        self.assertTrue(
            {
                "cache",
                "performance",
                "shared-memory",
                "primary-key",
                "transactions",
                "sql",
                "postgresql",
            }.issubset(metadata["tags"])
        )

    def test_version_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
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
            for name in ("README.md", "LICENSE"):
                shutil.copy2(ROOT / name, root / name)
            for path in (
                ROOT / "META.json",
                ROOT / "pg_local_cache.control",
                ROOT / "Makefile",
                CURRENT_SQL,
                ROOT / "src" / "pg_local_cache_worker.c",
            ):
                target = root / path.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            metadata = json.loads((root / "META.json").read_text())
            metadata["version"] = "99.0.0"
            (root / "META.json").write_text(
                json.dumps(metadata, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                validator.MetadataError,
                "provides.pg_local_cache.file|versions must match",
            ):
                validator.validate_repository(root)

    def test_pgxn_documentation_discloses_activation_and_credential_boundary(self) -> None:
        source = (ROOT / "PGXN.md").read_text(encoding="utf-8")
        for marker in (
            "shared_preload_libraries",
            "one controlled restart",
            "pgxn install",
            "configure the server",
            "PGXN_USERNAME",
            "PGXN_PASSWORD",
            "automatic semantic",
            "Never reuse an existing semantic version",
        ):
            self.assertIn(marker, source)


class PgxnArchiveContracts(unittest.TestCase):
    def test_make_dist_is_standalone(self) -> None:
        result = subprocess.run(
            ["make", "-n", "dist"],
            cwd=ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertIn("scripts/validate_pgxn_meta.py", result.stdout)
        self.assertIn("scripts/build_pgxn_dist.py", result.stdout)
        self.assertNotIn("--pgxs", result.stdout)

    def test_builder_creates_pgxn_shaped_archive(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            output = Path(raw_directory)
            archive = builder.build_distribution(ROOT, output, allow_dirty=True)
            self.assertEqual(
                archive.name,
                f"pg_local_cache-{CURRENT_VERSION}.zip",
            )
            with zipfile.ZipFile(archive) as package:
                names = set(package.namelist())
                prefix = f"pg_local_cache-{CURRENT_VERSION}/"
                for path in (
                    "BUILD-ID",
                    "META.json",
                    "Makefile",
                    "PGXN.md",
                    "README.md",
                    "pg_local_cache.control",
                    f"sql/pg_local_cache--{CURRENT_VERSION}.sql",
                    "src/pg_local_cache.c",
                ):
                    self.assertIn(prefix + path, names)
                expected_build_id = (
                    (ROOT / "BUILD-ID").read_text(encoding="ascii").strip()
                    if not (ROOT / ".git").exists()
                    else subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=ROOT,
                        check=True,
                        text=True,
                        stdout=subprocess.PIPE,
                    ).stdout.strip()
                )
                self.assertEqual(
                    package.read(prefix + "BUILD-ID").decode("ascii").strip(),
                    expected_build_id,
                )
                self.assertFalse(
                    any(
                        part in {".agent", ".git", ".tmp", "dist", "secrets"}
                        for name in names
                        for part in Path(name).parts
                    )
                )

    def test_source_archive_builds_pgxn_without_git(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            source = directory / "source"
            shutil.copytree(
                ROOT,
                source,
                ignore=shutil.ignore_patterns(
                    ".git",
                    ".supergoal",
                    ".codegraph",
                    "plans",
                    "dist",
                    "__pycache__",
                    "resp_test",
                    "resp_test_sanitized",
                    "*.o",
                    "*.so",
                    "*.bc",
                ),
            )
            build_id = (
                subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=ROOT,
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout.strip()
                if (ROOT / ".git").exists()
                else (ROOT / "BUILD-ID").read_text(encoding="ascii").strip()
            )
            (source / "BUILD-ID").write_text(build_id + "\n", encoding="ascii")
            archive = builder.build_distribution(source, directory / "output")
            with zipfile.ZipFile(archive) as package:
                self.assertEqual(
                    package.read(f"pg_local_cache-{CURRENT_VERSION}/BUILD-ID")
                    .decode("ascii")
                    .strip(),
                    build_id,
                )

    def test_source_archive_rejects_common_secret_files_without_git(self) -> None:
        for name in (
            ".env",
            ".env.production",
            ".envrc",
            ".netrc",
            ".authinfo",
            ".authinfo.gpg",
            "credentials.json",
            "id_rsa",
            "server.pem",
            ".docker/config.json",
            ".aws/credentials",
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                (root / "README.md").write_text("safe\n", encoding="utf-8")
                secret = root / name
                secret.parent.mkdir(parents=True, exist_ok=True)
                secret.write_text("private\n", encoding="utf-8")
                with self.assertRaisesRegex(
                    builder.MetadataError, "contains sensitive file"
                ):
                    builder._source_archive_files(root)

    def test_workflow_publishes_exact_stable_release_to_pgxn(self) -> None:
        source = (
            ROOT / ".github" / "workflows" / "pgxn.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("github.event_name == 'workflow_dispatch'", source)
        self.assertIn("inputs.publish", source)
        self.assertNotIn("workflow_run:", source)
        self.assertIn("make pgxn-check dist", source)
        self.assertIn('commit_tag="master-${sha:0:12}"', source)
        self.assertIn('stable_tag="v${version}"', source)
        self.assertIn('[[ "$stable_sha" == "$sha" ]]', source)
        self.assertIn("--json isDraft,isPrerelease", source)
        self.assertIn("[.isDraft, .isPrerelease] | any", source)
        self.assertIn("already exists with different bytes", source)
        self.assertIn("Qualify the exact stable GitHub release", source)
        self.assertIn("publish=false", source)
        self.assertIn("skipping PGXN", source)
        self.assertIn("PGXN_USERNAME", source)
        self.assertIn("PGXN_PASSWORD", source)
        self.assertIn("https://manager.pgxn.org/upload", source)
        self.assertIn("X-Requested-With: XMLHttpRequest", source)
        self.assertIn('--form "archive=@${asset};type=application/zip"', source)
        self.assertIn("api.pgxn.org/dist/pg_local_cache", source)
        self.assertIn('cmp --silent "$asset" "$mirrored"', source)
        self.assertIn("for attempt in $(seq 1 72)", source)
        self.assertNotIn("pgxn/pgxn-tools", source)


if __name__ == "__main__":
    unittest.main()
