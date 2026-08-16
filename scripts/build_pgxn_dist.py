#!/usr/bin/env python3
"""Build and verify the source archive uploaded to PGXN."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

from validate_pgxn_meta import MetadataError, validate_repository


def _run(command: list[str], *, cwd: Path) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise MetadataError(f"required program is missing: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or error.stdout.strip() or f"exit {error.returncode}"
        raise MetadataError(f"{' '.join(command)} failed: {detail}") from error
    return result.stdout.strip()


def _verify_clean_head(root: Path, revision: str, allow_dirty: bool) -> str:
    if not (root / ".git").exists():
        build_id_path = root / "BUILD-ID"
        if revision != "HEAD" or not build_id_path.is_file():
            raise MetadataError("source archive requires its packaged BUILD-ID")
        resolved = build_id_path.read_text(encoding="ascii").strip()
        if re.fullmatch(r"[0-9a-f]{40}", resolved) is None:
            raise MetadataError("source archive BUILD-ID is invalid")
        return resolved
    resolved = _run(["git", "rev-parse", "--verify", f"{revision}^{{commit}}"], cwd=root)
    if revision == "HEAD" and not allow_dirty:
        status = _run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=root,
        )
        if status:
            raise MetadataError(
                "working tree is not clean; commit the release inputs or pass --allow-dirty"
            )
    return resolved


def _source_archive_files(root: Path) -> list[Path]:
    excluded_parts = {
        ".git",
        ".supergoal",
        ".codegraph",
        "__pycache__",
        "benchmark-results",
        "dist",
        "log",
        "results",
        "secrets",
        "tmp_check",
        "tmp_check_iso",
    }
    excluded_suffixes = {".bc", ".o", ".so"}
    sensitive_names = {
        ".authinfo",
        ".authinfo.gpg",
        ".netrc",
        ".envrc",
        ".npmrc",
        ".pgpass",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ed25519",
        "id_ecdsa",
        "id_rsa",
    }
    sensitive_parts = {".aws", ".azure", ".docker", ".gnupg", ".kube", ".ssh"}
    sensitive_suffixes = {".key", ".p12", ".pem", ".pfx"}
    files: list[Path] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if excluded_parts.intersection(relative.parts):
            continue
        if path.is_symlink():
            raise MetadataError(f"source archive contains symlink: {relative}")
        if not path.is_file() or path.name == "BUILD-ID":
            continue
        lowered = path.name.lower()
        if (
            sensitive_parts.intersection(relative.parts)
            or lowered == ".env"
            or lowered.startswith(".env.")
            or lowered in sensitive_names
            or path.suffix.lower() in sensitive_suffixes
            or re.search(
                r"(^|[._-])(credential|credentials|private[-_]?key|secret|secrets)($|[._-])",
                lowered,
            )
        ):
            raise MetadataError(f"source archive contains sensitive file: {relative}")
        if path.suffix in excluded_suffixes or path.name in {
            "resp_test",
            "resp_test_sanitized",
        }:
            continue
        files.append(path)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _build_without_git(root: Path, temporary: Path, prefix: str, epoch: int) -> None:
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for path in _source_archive_files(root):
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(prefix + relative, time.gmtime(epoch)[:6])
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | stat.S_IMODE(path.stat().st_mode)) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            package.writestr(info, path.read_bytes())


def _verify_archive(
    archive: Path,
    *,
    distribution: str,
    version: str,
    build_id: str,
    expected_meta: dict[str, object],
) -> None:
    prefix = f"{distribution}-{version}/"
    required = {
        f"{prefix}LICENSE",
        f"{prefix}BUILD-ID",
        f"{prefix}META.json",
        f"{prefix}Makefile",
        f"{prefix}README.md",
        f"{prefix}pg_local_cache.control",
        f"{prefix}sql/pg_local_cache--{version}.sql",
        f"{prefix}src/pg_local_cache.c",
        f"{prefix}src/pg_local_cache_sql.c",
        f"{prefix}src/pg_local_cache_worker.c",
    }
    forbidden_parts = {".agent", ".git", ".tmp", "benchmark-results", "dist", "secrets"}

    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        if not names:
            raise MetadataError("PGXN archive is empty")
        if len(names) != len(set(names)):
            raise MetadataError("PGXN archive contains duplicate paths")
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise MetadataError(f"PGXN archive contains unsafe path {name!r}")
            if not name.startswith(prefix):
                raise MetadataError(
                    f"PGXN archive path {name!r} is outside {prefix!r}"
                )
            if forbidden_parts.intersection(path.parts):
                raise MetadataError(f"PGXN archive contains private path {name!r}")
        missing = sorted(required.difference(names))
        if missing:
            raise MetadataError(
                "PGXN archive is missing required files: " + ", ".join(missing)
            )
        try:
            archived_meta = json.loads(package.read(f"{prefix}META.json"))
        except (KeyError, UnicodeError, json.JSONDecodeError) as error:
            raise MetadataError(f"could not read archived META.json: {error}") from error
        if archived_meta != expected_meta:
            raise MetadataError("archived META.json differs from the validated source")
        if package.read(f"{prefix}BUILD-ID").decode("ascii") != f"{build_id}\n":
            raise MetadataError("archived BUILD-ID differs from the resolved revision")


def build_distribution(
    root: Path,
    output_directory: Path,
    revision: str = "HEAD",
    *,
    allow_dirty: bool = False,
) -> Path:
    root = root.resolve()
    metadata = validate_repository(root)
    distribution = str(metadata["name"])
    version = str(metadata["version"])
    resolved = _verify_clean_head(root, revision, allow_dirty)

    output_directory = output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    destination = output_directory / f"{distribution}-{version}.zip"

    with tempfile.TemporaryDirectory(prefix="pg_local_cache_pgxn_") as raw:
        temporary = Path(raw) / destination.name
        if (root / ".git").exists() and not (
            revision == "HEAD" and allow_dirty
        ):
            _run(
                [
                    "git",
                    "archive",
                    "--format=zip",
                    f"--prefix={distribution}-{version}/",
                    f"--output={temporary}",
                    resolved,
                ],
                cwd=root,
            )
            epoch = int(_run(["git", "show", "-s", "--format=%ct", resolved], cwd=root))
        elif (root / ".git").exists():
            epoch = int(_run(["git", "show", "-s", "--format=%ct", resolved], cwd=root))
            _build_without_git(
                root, temporary, f"{distribution}-{version}/", epoch
            )
        else:
            epoch = int((root / "BUILD-ID").stat().st_mtime)
            _build_without_git(
                root, temporary, f"{distribution}-{version}/", epoch
            )
        build_info = zipfile.ZipInfo(
            f"{distribution}-{version}/BUILD-ID",
            time.gmtime(epoch)[:6],
        )
        build_info.create_system = 3
        build_info.external_attr = (stat.S_IFREG | 0o644) << 16
        build_info.compress_type = zipfile.ZIP_DEFLATED
        with zipfile.ZipFile(temporary, "a") as package:
            package.writestr(build_info, f"{resolved}\n")
        _verify_archive(
            temporary,
            distribution=distribution,
            version=version,
            build_id=resolved,
            expected_meta=metadata,
        )
        staged = destination.with_suffix(".zip.tmp")
        shutil.copyfile(temporary, staged)
        staged.replace(destination)

    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    print(f"{destination}  sha256:{digest}")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--allow-dirty", action="store_true")
    arguments = parser.parse_args()
    try:
        build_distribution(
            arguments.root,
            arguments.output_dir,
            arguments.revision,
            allow_dirty=arguments.allow_dirty,
        )
    except (MetadataError, OSError, UnicodeError, zipfile.BadZipFile) as error:
        print(f"PGXN distribution build failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
