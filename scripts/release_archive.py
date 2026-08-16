#!/usr/bin/env python3
"""Build and inspect deterministic pg_local_cache release archives."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
import tempfile


class ArchiveError(RuntimeError):
    pass


def _open_archive(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        os.close(descriptor)
        raise ArchiveError("archive must be a regular file with exactly one link")
    return descriptor


def _digest(descriptor: int) -> str:
    result = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1024 * 1024):
        result.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return result.hexdigest()


def _members(root: Path) -> list[Path]:
    return [root, *sorted(root.rglob("*"), key=lambda path: path.as_posix())]


def build(stage: Path, root_name: str, output: Path, epoch: int) -> None:
    root = stage / root_name
    if not root.is_dir() or PurePosixPath(root_name).name != root_name:
        raise ArchiveError("stage must contain the requested single release root")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output.parent, 0o700)
    if output.exists() or output.is_symlink():
        raise ArchiveError("refusing to replace an existing archive")

    temporary = tempfile.NamedTemporaryFile(
        prefix=f".{output.name}.", dir=output.parent, delete=False
    )
    temporary_path = Path(temporary.name)
    try:
        with temporary, gzip.GzipFile(
            filename="", mode="wb", fileobj=temporary, mtime=epoch, compresslevel=9
        ) as compressed, tarfile.open(
            fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
        ) as archive:
            for path in _members(root):
                relative = path.relative_to(stage).as_posix()
                info = archive.gettarinfo(path, arcname=relative)
                if not (info.isdir() or info.isfile()):
                    raise ArchiveError(f"unsupported staged member: {relative}")
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = epoch
                if info.isfile():
                    with path.open("rb") as source:
                        archive.addfile(info, source)
                else:
                    archive.addfile(info)
            compressed.flush()
        descriptor = os.open(temporary_path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(temporary_path, 0o444)
        os.replace(temporary_path, output)
        directory_fd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()

    descriptor = _open_archive(output)
    try:
        metadata = os.fstat(descriptor)
        digest = _digest(descriptor)
    finally:
        os.close(descriptor)
    print(f"archive={output.resolve()}")
    print(f"identity={metadata.st_dev}:{metadata.st_ino}:{metadata.st_size}")
    print(f"sha256={digest}")


def _validated_members(archive: tarfile.TarFile, expected_root: str) -> list[tarfile.TarInfo]:
    result: list[tarfile.TarInfo] = []
    seen: set[str] = set()
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or path.parts[0] != expected_root
            or member.name != path.as_posix()
            or member.name in seen
        ):
            raise ArchiveError(f"unsafe archive member: {member.name}")
        if not (member.isdir() or member.isfile()):
            raise ArchiveError(f"unsupported archive member type: {member.name}")
        seen.add(member.name)
        result.append(member)
    if not result or expected_root not in seen:
        raise ArchiveError("archive root is missing")
    member_types = {member.name: member.isdir() for member in result}
    for member in result:
        parents = PurePosixPath(member.name).parents
        for parent in parents:
            parent_name = parent.as_posix()
            if parent_name == ".":
                continue
            if parent_name in member_types and not member_types[parent_name]:
                raise ArchiveError(f"archive parent is not a directory: {parent_name}")
    return result


def inspect_archive(
    archive_path: Path, expected_root: str, extract_dir: Path, identity_out: Path | None
) -> None:
    descriptor = _open_archive(archive_path)
    try:
        metadata = os.fstat(descriptor)
        digest = _digest(descriptor)
        with os.fdopen(os.dup(descriptor), "rb") as source, tarfile.open(
            fileobj=source, mode="r:gz"
        ) as archive:
            members = _validated_members(archive, expected_root)
            if extract_dir.exists() or extract_dir.is_symlink():
                raise ArchiveError("extract destination must not exist")
            extract_dir.mkdir(parents=True, mode=0o700)
            directories: list[tuple[Path, tarfile.TarInfo]] = []
            for member in members:
                target = extract_dir.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    target.mkdir(mode=member.mode & 0o777, exist_ok=True)
                    directories.append((target, member))
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source_file = archive.extractfile(member)
                if source_file is None:
                    raise ArchiveError(f"could not read archive member: {member.name}")
                target_fd = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    member.mode & 0o777,
                )
                with os.fdopen(target_fd, "wb") as target_file:
                    while chunk := source_file.read(1024 * 1024):
                        target_file.write(chunk)
                os.chmod(target, member.mode & 0o777)
                os.utime(target, (member.mtime, member.mtime), follow_symlinks=False)
            for target, member in reversed(directories):
                os.chmod(target, member.mode & 0o777)
                os.utime(target, (member.mtime, member.mtime), follow_symlinks=False)
            print(f"archive={archive_path.resolve()}")
            print(f"identity={metadata.st_dev}:{metadata.st_ino}:{metadata.st_size}")
            print(f"sha256={digest}")
            for member in members:
                kind = "dir" if member.isdir() else "file"
                print(f"member={kind}:{member.mode & 0o777:04o}:{member.name}")
            extracted_root = (extract_dir / expected_root).resolve()
            print(f"extracted_root={extracted_root}")
            print(f"workdir={extracted_root}")
        if identity_out is not None:
            identity_out.write_text(
                json.dumps(
                    {
                        "device": metadata.st_dev,
                        "inode": metadata.st_ino,
                        "size": metadata.st_size,
                        "sha256": digest,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="ascii",
            )
    finally:
        os.close(descriptor)


def verify_identity(archive_path: Path, identity_path: Path) -> None:
    expected = json.loads(identity_path.read_text(encoding="ascii"))
    descriptor = _open_archive(archive_path)
    try:
        metadata = os.fstat(descriptor)
        actual = {
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "size": metadata.st_size,
            "sha256": _digest(descriptor),
        }
    finally:
        os.close(descriptor)
    if actual != expected:
        raise ArchiveError("archive identity changed after inspection")
    print(f"identity_verified={actual['sha256']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build")
    build_parser.add_argument("--stage", type=Path, required=True)
    build_parser.add_argument("--root", required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    build_parser.add_argument("--epoch", type=int, required=True)
    inspect_parser = commands.add_parser("inspect")
    inspect_parser.add_argument("--archive", type=Path, required=True)
    inspect_parser.add_argument("--root", required=True)
    inspect_parser.add_argument("--extract-dir", type=Path, required=True)
    inspect_parser.add_argument("--identity-out", type=Path)
    verify_parser = commands.add_parser("verify-identity")
    verify_parser.add_argument("--archive", type=Path, required=True)
    verify_parser.add_argument("--identity", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        if arguments.command == "build":
            build(arguments.stage, arguments.root, arguments.output, arguments.epoch)
        elif arguments.command == "inspect":
            inspect_archive(
                arguments.archive,
                arguments.root,
                arguments.extract_dir,
                arguments.identity_out,
            )
        else:
            verify_identity(arguments.archive, arguments.identity)
    except (ArchiveError, OSError, tarfile.TarError, json.JSONDecodeError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
