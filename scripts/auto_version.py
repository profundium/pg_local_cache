#!/usr/bin/env python3
"""Plan and materialize pg_local_cache semantic release versions.

The newest reachable ``vX.Y.Z`` tag is the release baseline. Commits after the
baseline are classified with Conventional Commits:

* a breaking marker (``type!`` or ``BREAKING CHANGE``) selects a major bump;
* ``feat`` selects a minor bump;
* ``fix``, ``perf``, ``refactor``, ``build``, ``revert`` and ``security``
  select a patch bump;
* ``docs``, ``test``, ``ci``, ``chore`` and ``style`` do not release by
  themselves;
* an otherwise unclassified non-merge commit selects a patch bump so changes
  are not silently left unpublished.

The script has three actions:

* ``bump``: update all version-bearing files and create versioned SQL files;
* ``release``: the version bump is already committed and can be published;
* ``none``: no releasable changes exist after the latest stable tag.

Released SQL files are immutable. A C-only release copies the current install
script to the new version and creates an explicit no-op upgrade script. If SQL
objects change, edit the current install script and add only the incremental
migration to ``sql/pg_local_cache--unreleased.sql``. During the version bump,
the old install script is restored from the stable tag, the edited script is
saved under the new version, and the incremental fragment becomes the upgrade
script.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable, Literal

from validate_pgxn_meta import MetadataError, validate_repository


SEMVER_PATTERN = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
SEMVER = re.compile(rf"^{SEMVER_PATTERN}$")
STABLE_TAG = re.compile(rf"^v(?P<version>{SEMVER_PATTERN})$")
CONTROL_VERSION = re.compile(
    rf"^default_version = '(?P<version>{SEMVER_PATTERN})'$",
    flags=re.MULTILINE,
)
CONVENTIONAL = re.compile(
    r"^(?P<type>[A-Za-z][A-Za-z0-9_-]*)(?:\([^\)\r\n]+\))?"
    r"(?P<breaking>!)?:\s+",
)
BREAKING_FOOTER = re.compile(r"(?m)^BREAKING(?:[ -]CHANGE):\s*\S")
RELEASE_SUBJECT = re.compile(
    rf"^chore\(release\): (?:prepare )?v(?P<version>{SEMVER_PATTERN})"
    r"(?: \[skip version\])?$"
)
INSTALL_SQL = re.compile(rf"^pg_local_cache--(?P<version>{SEMVER_PATTERN})\.sql$")
UPGRADE_SQL = re.compile(
    rf"^pg_local_cache--(?P<old>{SEMVER_PATTERN})--"
    rf"(?P<new>{SEMVER_PATTERN})\.sql$"
)
NO_RELEASE_TYPES = {"chore", "ci", "docs", "style", "test"}
PATCH_TYPES = {"build", "fix", "perf", "refactor", "revert", "security"}

Bump = Literal["none", "patch", "minor", "major"]
Action = Literal["none", "bump", "release"]
BUMP_ORDER: dict[Bump, int] = {
    "none": 0,
    "patch": 1,
    "minor": 2,
    "major": 3,
}


class VersionError(RuntimeError):
    """Raised when version state cannot be advanced safely."""


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> "Version":
        match = SEMVER.fullmatch(value.strip())
        if match is None:
            raise VersionError(f"invalid semantic version: {value!r}")
        return cls(*(int(item) for item in match.groups()))

    def bump(self, kind: Bump) -> "Version":
        if kind == "major":
            return Version(self.major + 1, 0, 0)
        if kind == "minor":
            return Version(self.major, self.minor + 1, 0)
        if kind == "patch":
            return Version(self.major, self.minor, self.patch + 1)
        return self

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class VersionPlan:
    base_tag: str | None
    base_version: Version | None
    current_version: Version
    next_version: Version
    bump: Bump
    action: Action
    commit_count: int
    reason: str

    @property
    def changed(self) -> bool:
        return self.action == "bump"

    @property
    def release_ready(self) -> bool:
        return self.action == "release"

    def as_dict(self) -> dict[str, object]:
        return {
            "base_tag": self.base_tag or "none",
            "base_version": str(self.base_version) if self.base_version else "none",
            "current_version": str(self.current_version),
            "next_version": str(self.next_version),
            "version": str(self.next_version),
            "bump": self.bump,
            "action": self.action,
            "changed": self.changed,
            "release_ready": self.release_ready,
            "commit_count": self.commit_count,
            "reason": self.reason,
        }


def _run_git(root: Path, *arguments: str, check: bool = True) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise VersionError("git is required for automatic versioning") from error
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or str(result.returncode)
        raise VersionError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout


def read_control_version(root: Path) -> Version:
    path = root / "pg_local_cache.control"
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as error:
        raise VersionError(f"could not read {path}: {error}") from error
    matches = list(CONTROL_VERSION.finditer(source))
    if len(matches) != 1:
        raise VersionError(
            "pg_local_cache.control must contain one strict default_version"
        )
    return Version.parse(matches[0].group("version"))


def latest_stable_tag(root: Path) -> tuple[str, Version] | None:
    raw = _run_git(
        root,
        "tag",
        "--merged",
        "HEAD",
        "--list",
        "v*.*.*",
        "--sort=-version:refname",
    )
    candidates: list[tuple[Version, str]] = []
    for line in raw.splitlines():
        tag = line.strip()
        match = STABLE_TAG.fullmatch(tag)
        if match is not None:
            candidates.append((Version.parse(match.group("version")), tag))
    if not candidates:
        return None
    version, tag = max(candidates)
    return tag, version


def _tag_sha(root: Path, tag: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-list", "-n", "1", tag],
        cwd=root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def commit_messages(root: Path, base_tag: str | None) -> list[str]:
    revision = f"{base_tag}..HEAD" if base_tag else "HEAD"
    raw = _run_git(root, "log", "--reverse", "--format=%B%x00", revision)
    return [message.strip() for message in raw.split("\x00") if message.strip()]


def classify_message(message: str) -> Bump:
    if not message.strip() or "[skip version]" in message.lower():
        return "none"
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    if not lines:
        return "none"
    if BREAKING_FOOTER.search(message):
        return "major"

    result: Bump = "none"
    matched = False
    for line in lines:
        match = CONVENTIONAL.match(line)
        if match is None:
            continue
        matched = True
        if match.group("breaking"):
            candidate: Bump = "major"
        else:
            kind = match.group("type").lower()
            if kind == "feat":
                candidate = "minor"
            elif kind in PATCH_TYPES:
                candidate = "patch"
            elif kind in NO_RELEASE_TYPES:
                candidate = "none"
            else:
                candidate = "patch"
        if BUMP_ORDER[candidate] > BUMP_ORDER[result]:
            result = candidate
    if matched:
        return result

    first = lines[0].lower()
    if first.startswith("merge ") or first.startswith("chore(release):"):
        return "none"
    return "patch"


def classify_messages(messages: Iterable[str]) -> Bump:
    result: Bump = "none"
    for message in messages:
        candidate = classify_message(message)
        if BUMP_ORDER[candidate] > BUMP_ORDER[result]:
            result = candidate
    return result


def _release_commit_version(messages: Iterable[str]) -> Version | None:
    found: Version | None = None
    for message in messages:
        subject = message.splitlines()[0].strip() if message.splitlines() else ""
        match = RELEASE_SUBJECT.fullmatch(subject)
        if match is not None:
            found = Version.parse(match.group("version"))
    return found


def _bump_between(old: Version, new: Version) -> Bump:
    if new == Version(old.major + 1, 0, 0):
        return "major"
    if new == Version(old.major, old.minor + 1, 0):
        return "minor"
    if new == Version(old.major, old.minor, old.patch + 1):
        return "patch"
    raise VersionError(
        f"release version {new} is not one semantic bump after {old}"
    )


def plan_version(root: Path, requested_bump: str = "auto") -> VersionPlan:
    root = root.resolve()
    current = read_control_version(root)
    baseline = latest_stable_tag(root)

    if baseline is None:
        return VersionPlan(
            base_tag=None,
            base_version=None,
            current_version=current,
            next_version=current,
            bump="none",
            action="release",
            commit_count=0,
            reason="no stable tag exists; current metadata defines the first release",
        )

    base_tag, base = baseline
    messages = commit_messages(root, base_tag)
    calculated = classify_messages(messages)
    if requested_bump == "auto":
        bump = calculated
    elif requested_bump in {"major", "minor", "patch"}:
        bump = requested_bump  # type: ignore[assignment]
    else:
        raise VersionError(f"unknown bump mode {requested_bump!r}")

    if current < base:
        raise VersionError(
            f"control version {current} is older than latest stable tag {base_tag}"
        )

    current_tag = f"v{current}"
    current_tag_sha = _tag_sha(root, current_tag)
    head_sha = _run_git(root, "rev-parse", "HEAD").strip()
    if current == base:
        if bump == "none":
            return VersionPlan(
                base_tag=base_tag,
                base_version=base,
                current_version=current,
                next_version=current,
                bump="none",
                action="none",
                commit_count=len(messages),
                reason="commits after the latest tag do not require a release",
            )
        next_version = base.bump(bump)
        return VersionPlan(
            base_tag=base_tag,
            base_version=base,
            current_version=current,
            next_version=next_version,
            bump=bump,
            action="bump",
            commit_count=len(messages),
            reason=f"{bump} release required after {base_tag}",
        )

    if current_tag_sha is not None and current_tag_sha != head_sha:
        raise VersionError(
            f"{current_tag} already points to {current_tag_sha}; refusing reuse"
        )

    # A prior workflow run has already committed the generated version. Rebuild
    # the expected value from the unreleased commits, or accept the explicit
    # release commit as the authoritative one-time handoff for a forced bump.
    release_commit = _release_commit_version(messages)
    expected = base.bump(calculated)
    if current == expected and calculated != "none":
        effective_bump = calculated
    elif release_commit == current:
        effective_bump = _bump_between(base, current)
    else:
        raise VersionError(
            "control version is ahead of the stable tag but does not match "
            f"the calculated release ({expected}) or a chore(release) commit"
        )

    if current_tag_sha == head_sha:
        return VersionPlan(
            base_tag=base_tag,
            base_version=base,
            current_version=current,
            next_version=current,
            bump=effective_bump,
            action="none",
            commit_count=len(messages),
            reason=f"{current_tag} already points to HEAD",
        )

    return VersionPlan(
        base_tag=base_tag,
        base_version=base,
        current_version=current,
        next_version=current,
        bump=effective_bump,
        action="release",
        commit_count=len(messages),
        reason=f"version {current} is prepared and has no stable tag",
    )


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise VersionError(f"expected one {label}, found {count}")
    return source.replace(old, new, 1)


def _sql_sort_key(path: Path) -> tuple[int, Version, Version, str]:
    install = INSTALL_SQL.fullmatch(path.name)
    if install is not None:
        version = Version.parse(install.group("version"))
        return (0, version, version, path.name)
    upgrade = UPGRADE_SQL.fullmatch(path.name)
    if upgrade is not None:
        return (
            1,
            Version.parse(upgrade.group("old")),
            Version.parse(upgrade.group("new")),
            path.name,
        )
    raise VersionError(f"unexpected extension SQL filename {path.name}")


def _render_makefile_data(sql_paths: list[Path]) -> str:
    relative = [f"sql/{path.name}" for path in sorted(sql_paths, key=_sql_sort_key)]
    if not relative:
        raise VersionError("no extension SQL files found")
    lines: list[str] = []
    for index, item in enumerate(relative):
        prefix = "DATA = " if index == 0 else "\t"
        suffix = " \\" if index + 1 < len(relative) else ""
        lines.append(f"{prefix}{item}{suffix}")
    return "\n".join(lines) + "\n"


def _update_makefile(root: Path) -> None:
    path = root / "Makefile"
    source = path.read_text(encoding="utf-8")
    match = re.search(
        r"^DATA = .*?(?=^PGFILEDESC\s*=)",
        source,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise VersionError("could not locate the Makefile DATA block")
    sql_paths = [
        item
        for item in (root / "sql").glob("pg_local_cache--*.sql")
        if INSTALL_SQL.fullmatch(item.name) or UPGRADE_SQL.fullmatch(item.name)
    ]
    replacement = _render_makefile_data(sql_paths)
    _write_text(path, source[: match.start()] + replacement + source[match.end() :])


def _update_worker_version(root: Path, old: Version, new: Version) -> None:
    path = root / "src" / "pg_local_cache.h"
    source = path.read_text(encoding="utf-8")
    old_text = str(old)
    new_text = str(new)
    source = _replace_once(
        source,
        f'#define PGLC_VERSION "{old_text}"',
        f'#define PGLC_VERSION "{new_text}"',
        "shared extension version",
    )
    source = _replace_once(
        source,
        f'#define PGLC_VERSION_LENGTH "{len(old_text)}"',
        f'#define PGLC_VERSION_LENGTH "{len(new_text)}"',
        "shared extension version length",
    )
    _write_text(path, source)


def _update_compose_images(root: Path, old: Version, new: Version) -> None:
    old_tag = f"image: pg_local_cache:{old}"
    new_tag = f"image: pg_local_cache:{new}"
    for name in ("compose.yaml",):
        path = root / name
        source = path.read_text(encoding="utf-8")
        source = _replace_once(source, old_tag, new_tag, f"{name} image tag")
        _write_text(path, source)


def _update_metadata(root: Path, new: Version) -> None:
    path = root / "META.json"
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise VersionError(f"could not read META.json: {error}") from error
    provided = metadata.get("provides", {}).get("pg_local_cache")
    if not isinstance(provided, dict):
        raise VersionError("META.json lacks provides.pg_local_cache")
    metadata["version"] = str(new)
    provided["version"] = str(new)
    provided["file"] = f"sql/pg_local_cache--{new}.sql"
    _write_text(path, json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")


def _changed_sql_paths(root: Path, base_tag: str) -> set[Path]:
    raw = _run_git(
        root,
        "diff",
        "--name-only",
        "--diff-filter=ACMRTD",
        f"{base_tag}..HEAD",
        "--",
        "sql",
    )
    return {Path(line.strip()) for line in raw.splitlines() if line.strip()}


def _prepare_sql_upgrade(root: Path, plan: VersionPlan) -> None:
    if plan.base_tag is None or plan.base_version is None:
        raise VersionError("automatic SQL upgrade generation needs a stable tag")

    old = plan.current_version
    new = plan.next_version
    sql_directory = root / "sql"
    old_path = sql_directory / f"pg_local_cache--{old}.sql"
    new_path = sql_directory / f"pg_local_cache--{new}.sql"
    upgrade_path = sql_directory / f"pg_local_cache--{old}--{new}.sql"
    fragment_path = sql_directory / "pg_local_cache--unreleased.sql"

    if new_path.exists() or upgrade_path.exists():
        raise VersionError(
            f"target SQL version files already exist for {new}; refusing overwrite"
        )
    if not old_path.is_file():
        raise VersionError(f"missing current install script {old_path}")

    changed_paths = _changed_sql_paths(root, plan.base_tag)
    allowed_changes = {old_path.relative_to(root), fragment_path.relative_to(root)}
    unexpected = sorted(changed_paths - allowed_changes)
    if unexpected:
        raise VersionError(
            "released/versioned SQL files changed after the stable tag: "
            + ", ".join(str(path) for path in unexpected)
        )

    current_install = old_path.read_text(encoding="utf-8")
    stable_install = _run_git(
        root,
        "show",
        f"{plan.base_tag}:{old_path.relative_to(root).as_posix()}",
    )
    fragment = fragment_path.read_text(encoding="utf-8") if fragment_path.exists() else ""
    sql_changed = current_install != stable_install

    if sql_changed and not fragment.strip():
        raise VersionError(
            "the current install SQL changed since the stable tag; add the "
            "incremental migration to sql/pg_local_cache--unreleased.sql"
        )
    if not sql_changed and fragment.strip():
        raise VersionError(
            "sql/pg_local_cache--unreleased.sql exists but the current install "
            "SQL is unchanged"
        )

    _write_text(new_path, current_install)
    _write_text(old_path, stable_install)
    guard = (
        f'\\echo Use "ALTER EXTENSION pg_local_cache UPDATE TO \'{new}\'" '
        "to load this file. \\quit\n\n"
    )
    if sql_changed:
        body = fragment.strip() + "\n"
    else:
        body = (
            f"-- Version {new} changes the shared library, packaging, or "
            "documentation only.\n"
            f"-- SQL objects are unchanged from {old}.\n"
        )
    _write_text(upgrade_path, guard + body)
    if fragment_path.exists():
        fragment_path.unlink()


def apply_version(root: Path, plan: VersionPlan) -> None:
    if not plan.changed:
        return
    root = root.resolve()
    if _run_git(root, "status", "--porcelain").strip():
        raise VersionError("refusing to version a dirty working tree")

    old = plan.current_version
    new = plan.next_version
    _prepare_sql_upgrade(root, plan)

    control_path = root / "pg_local_cache.control"
    control = control_path.read_text(encoding="utf-8")
    control = _replace_once(
        control,
        f"default_version = '{old}'",
        f"default_version = '{new}'",
        "control default_version",
    )
    _write_text(control_path, control)

    _update_metadata(root, new)
    _update_worker_version(root, old, new)
    _update_compose_images(root, old, new)
    _update_makefile(root)

    try:
        validate_repository(root)
    except (MetadataError, OSError, UnicodeError) as error:
        raise VersionError(f"generated release metadata is invalid: {error}") from error

    if not _run_git(root, "status", "--porcelain").strip():
        raise VersionError("automatic versioning produced no changes")


def _write_github_output(path: Path, plan: VersionPlan) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for key, value in plan.as_dict().items():
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            else:
                rendered = str(value)
            if "\n" in rendered or "\r" in rendered:
                raise VersionError(f"GitHub output {key} contains a newline")
            stream.write(f"{key}={rendered}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument(
        "--bump",
        choices=("auto", "major", "minor", "patch"),
        default="auto",
        help="override Conventional Commit classification for this release",
    )
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--print-next-version", action="store_true")
    parser.add_argument("--github-output", type=Path)
    arguments = parser.parse_args()

    try:
        plan = plan_version(arguments.root, arguments.bump)
        if arguments.write:
            apply_version(arguments.root, plan)
        output = arguments.github_output
        if output is None and os.environ.get("GITHUB_OUTPUT"):
            output = Path(os.environ["GITHUB_OUTPUT"])
        if output is not None:
            _write_github_output(output, plan)
    except (
        VersionError,
        MetadataError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as error:
        print(f"automatic versioning failed: {error}", file=sys.stderr)
        return 1

    if arguments.print_next_version:
        print(plan.next_version)
    elif arguments.json:
        print(json.dumps(plan.as_dict(), indent=2, sort_keys=True))
    else:
        print(
            f"version action={plan.action}: {plan.current_version} -> "
            f"{plan.next_version} ({plan.bump}; {plan.reason})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
