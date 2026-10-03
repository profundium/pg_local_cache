#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
python3 - "$@" <<'PY'
import json, re, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
root = Path.cwd()
semver = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")
def fail(message):
    raise SystemExit(message)
def upgrade_content(path):
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"(?m)^\s*--[^\n]*(?:\n|$)", " ", text)
    return text.strip()
def has_upgrade_statement(path):
    text = upgrade_content(path)
    text = re.sub(r"(?m)^\s*\\echo(?:\s|$)[^\n]*(?:\n|$)", " ", text)
    statement = re.compile(
        r"^(?:ALTER|CALL|COMMENT|CREATE|DELETE|DO|DROP|GRANT|INSERT|"
        r"REINDEX|REFRESH|RESET|REVOKE|ROLLBACK|SELECT|SET|TRUNCATE|"
        r"UPDATE|VACUUM|WITH)\b",
        re.I,
    )
    return any(statement.match(part.strip()) for part in text.split(";")[:-1])
def prepare_changelog_release(text, old, new):
    unreleased_heading = re.compile(r"(?m)^## \[Unreleased\][ \t]*\r?\n")
    match = unreleased_heading.search(text)
    if match is None:
        fail("CHANGELOG.md has no [Unreleased] section")
    next_heading = re.search(r"(?m)^## \[", text[match.end():])
    section_end = match.end() + next_heading.start() if next_heading else len(text)
    notes = text[match.end():section_end].strip()
    if not re.sub(r"(?m)^#{3,6}[ \t]+.*$", "", notes).strip():
        fail("CHANGELOG.md [Unreleased] section is empty")
    if re.search(rf"(?m)^## \[{re.escape(new)}\](?:\s|$)", text):
        fail(f"CHANGELOG.md already has a section for version {new}")
    date = datetime.now(timezone.utc).date().isoformat()
    updated = (text[:match.end()] + f"\n## [{new}] - {date}\n\n{notes}\n\n"
               + text[section_end:].lstrip("\r\n"))
    if re.search(r"(?m)^\[Unreleased\]:", updated):
        compare_link = re.compile(
            r"(?m)^\[Unreleased\]:[ \t]*(?P<prefix>\S*/compare/)"
            r"v(?P<base>\d+\.\d+\.\d+)\.\.\.HEAD"
            r"(?P<newline>\r?\n|$)"
        )
        link = compare_link.search(updated)
        if link is None:
            fail("could not update CHANGELOG.md compare links")
        if link.group("base") != old:
            fail("CHANGELOG.md Unreleased compare link does not match current version")
        if re.search(rf"(?m)^\[{re.escape(new)}\]:", updated):
            fail(f"CHANGELOG.md already has a compare link for version {new}")
        newline = link.group("newline") or "\n"
        replacement = (f"[Unreleased]: {link.group('prefix')}v{new}...HEAD"
                       f"{newline}[{new}]: {link.group('prefix')}"
                       f"v{old}...v{new}{newline}")
        updated = updated[:link.start()] + replacement + updated[link.end():]
    return updated
def current_version():
    control = (root / "pg_local_cache.control").read_text()
    match = re.search(r"^default_version = '([^']+)'$", control, re.M)
    if not match or not semver.fullmatch(match.group(1)):
        fail("invalid control default_version")
    version = match.group(1)
    changelog = (root / "debian/changelog").read_text(encoding="utf-8")
    changelog_match = re.match(r"^pg-local-cache \(([^)]+)\) [^;\n]+; urgency=", changelog)
    if changelog_match is None or changelog_match.group(1) != f"{version}-1":
        fail("Debian changelog version does not match control default_version")
    project_changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if not re.search(
        rf"(?m)^## \[{re.escape(version)}\](?: - \d{{4}}-\d{{2}}-\d{{2}})?[ \t]*$",
        project_changelog,
    ):
        fail("CHANGELOG.md has no section for control default_version")
    rpm_spec = (root / "rpm/pg_local_cache.spec").read_text(encoding="utf-8")
    rpm_versions = re.findall(r"(?m)^Version:\s*(\S+)\s*$", rpm_spec)
    if rpm_versions != [version]:
        fail("RPM spec version does not match control default_version")
    metadata = json.loads((root / "META.json").read_text())
    provided = metadata["provides"]["pg_local_cache"]
    header = (root / "src/pg_local_cache.h").read_text()
    version_match = re.search(r'^#define PGLC_VERSION "([^"]+)"$', header, re.M)
    length_match = re.search(r'^#define PGLC_VERSION_LENGTH "([^"]+)"$', header, re.M)
    install = root / f"sql/pg_local_cache--{version}.sql"
    if (metadata.get("version") != version or provided.get("version") != version
            or provided.get("file") != f"sql/pg_local_cache--{version}.sql"
            or version_match is None or version_match.group(1) != version
            or length_match is None or length_match.group(1) != str(len(version))
            or not install.is_file()):
        fail("version metadata, header, or install SQL disagree")
    return version
args = sys.argv[1:]
if args.count("--no-baseline") > 1:
    fail("usage: scripts/bump-version.sh [--no-baseline] X.Y.Z | --check")
no_baseline = "--no-baseline" in args
if no_baseline:
    args.remove("--no-baseline")
if args == ["--check"] and not no_baseline:
    print(f"version {current_version()} consistent")
elif len(args) == 1 and semver.fullmatch(args[0]):
    old, new = current_version(), args[0]
    old_parts, new_parts = tuple(map(int, old.split("."))), tuple(map(int, args[0].split(".")))
    valid_next = {(old_parts[0] + 1, 0, 0), (old_parts[0], old_parts[1] + 1, 0),
                  (old_parts[0], old_parts[1], old_parts[2] + 1)}
    if new_parts not in valid_next:
        fail("new version must be one major, minor, or patch bump")
    old_sql, new_sql = Path(f"sql/pg_local_cache--{old}.sql"), Path(f"sql/pg_local_cache--{new}.sql")
    upgrade = Path(f"sql/pg_local_cache--{old}--{new}.sql")
    if not old_sql.is_file() or new_sql.exists():
        fail("source install SQL missing or target SQL already exists")
    keep_upgrade = upgrade.exists() or upgrade.is_symlink()
    upgrade_has_statement = has_upgrade_statement(upgrade)
    tag = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/v{old}^{{commit}}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if tag.returncode != 0:
        if not no_baseline and not upgrade_has_statement:
            fail(f"tag v{old} is missing; provide {upgrade.as_posix()} with a SQL statement or pass --no-baseline")
    else:
        tagged_sql = subprocess.run(
            ["git", "show", f"v{old}:sql/pg_local_cache--{old}.sql"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
        if (tagged_sql.returncode != 0 or tagged_sql.stdout != old_sql.read_bytes()) and not upgrade_has_statement:
            fail(f"install SQL changed or baseline SQL is unavailable at v{old}; write {upgrade.as_posix()} with a SQL statement before bumping")
    control = (root / "pg_local_cache.control").read_text()
    metadata = json.loads((root / "META.json").read_text())
    header = (root / "src/pg_local_cache.h").read_text()
    rpm_spec_path = root / "rpm/pg_local_cache.spec"
    rpm_spec = rpm_spec_path.read_text(encoding="utf-8")
    rpm_spec, spec_updates = re.subn(
        r"(?m)^(Version:\s*)\S+(\s*)$",
        lambda match: f"{match.group(1)}{new}{match.group(2)}",
        rpm_spec,
        count=1,
    )
    if spec_updates != 1:
        fail("expected one RPM Version field")
    compose = (root / "compose.yaml").read_text()
    old_tag = f"image: pg_local_cache:{old}"
    if compose.count(old_tag) != 1:
        fail("expected one current compose image tag")
    changelog_path = root / "CHANGELOG.md"
    prepared_changelog = prepare_changelog_release(
        changelog_path.read_text(encoding="utf-8"), old, new
    )
    subprocess.run(["git", "mv", str(old_sql), str(new_sql)], check=True)
    guard = (f'\\echo Use "ALTER EXTENSION pg_local_cache UPDATE TO \'{new}\'" '
             "to load this file. \\quit\n\n")
    if not keep_upgrade:
        upgrade.write_text(guard + f"-- Version {new} changes the shared library, packaging, or documentation only.\n"
                           f"-- SQL objects are unchanged from {old}.\n")
    (root / "pg_local_cache.control").write_text(control.replace(
        f"default_version = '{old}'", f"default_version = '{new}'", 1))
    rpm_spec_path.write_text(rpm_spec, encoding="utf-8")
    metadata["version"] = metadata["provides"]["pg_local_cache"]["version"] = new
    metadata["provides"]["pg_local_cache"]["file"] = f"sql/pg_local_cache--{new}.sql"
    (root / "META.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")
    header = header.replace(f'#define PGLC_VERSION "{old}"', f'#define PGLC_VERSION "{new}"', 1)
    header = header.replace(f'#define PGLC_VERSION_LENGTH "{len(old)}"',
                            f'#define PGLC_VERSION_LENGTH "{len(new)}"', 1)
    (root / "src/pg_local_cache.h").write_text(header)
    (root / "compose.yaml").write_text(compose.replace(old_tag, f"image: pg_local_cache:{new}", 1))
    date = datetime.now().astimezone().strftime("%a, %d %b %Y %H:%M:%S %z")
    entry = (f"pg-local-cache ({new}-1) unstable; urgency=medium\n\n"
             "  * New upstream release.\n\n"
             f" -- maxbronnikov10 <bronnikovmr@gmail.com>  {date}\n\n")
    changelog_path = root / "debian/changelog"
    changelog_path.write_text(entry + changelog_path.read_text(encoding="utf-8"), encoding="utf-8")
    (root / "CHANGELOG.md").write_text(prepared_changelog, encoding="utf-8")
    current_version()
    print(f"prepared version {old} -> {new}")
else:
    fail("usage: scripts/bump-version.sh [--no-baseline] X.Y.Z | --check")
PY
