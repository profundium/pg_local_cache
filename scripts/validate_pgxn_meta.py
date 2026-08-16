#!/usr/bin/env python3
"""Validate pg_local_cache PGXN metadata and repository version contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import urlparse


META_SPEC_VERSION = "1.0.0"
META_SPEC_URL = "https://pgxn.org/meta/spec.txt"
KNOWN_LICENSES = {
    "agpl_3",
    "apache_1_1",
    "apache_2_0",
    "artistic_1",
    "artistic_2",
    "bsd",
    "freebsd",
    "gfdl_1_2",
    "gfdl_1_3",
    "gpl_1",
    "gpl_2",
    "gpl_3",
    "lgpl_2_1",
    "lgpl_3_0",
    "mit",
    "mozilla_1_0",
    "mozilla_1_1",
    "openssl",
    "perl_5",
    "postgresql",
    "qpl_1_0",
    "ssleay",
    "sun",
    "zlib",
    "open_source",
    "restricted",
    "unrestricted",
    "unknown",
}
TOP_LEVEL_KEYS = {
    "abstract",
    "description",
    "generated_by",
    "license",
    "maintainer",
    "meta-spec",
    "name",
    "no_index",
    "prereqs",
    "provides",
    "release_status",
    "resources",
    "tags",
    "version",
}
SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
TERM = re.compile(r"^[^\s/\\\x00-\x1f\x7f]{2,}$")
CONTROL_VERSION = re.compile(
    r"^default_version = '([0-9]+\.[0-9]+\.[0-9]+)'$",
    flags=re.MULTILINE,
)
PREREQ_VERSION = re.compile(
    r"^(?:(?:<=|>=|==|!=|<|>)\s*)?"
    r"(?:0|(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)$"
)


class MetadataError(ValueError):
    """Raised when the repository is not a valid PGXN distribution."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise MetadataError(message)


def _require_string(value: Any, field: str) -> str:
    _require(
        isinstance(value, str) and bool(value.strip()),
        f"{field} must be a non-empty string",
    )
    return value


def _require_semver(value: Any, field: str) -> str:
    version = _require_string(value, field)
    _require(
        SEMVER.fullmatch(version) is not None,
        f"{field} must be a semantic X.Y.Z version",
    )
    return version


def _require_uri(value: Any, field: str) -> str:
    uri = _require_string(value, field)
    parsed = urlparse(uri)
    _require(
        parsed.scheme in {"http", "https", "git"},
        f"{field} must use http, https, or git",
    )
    _require(bool(parsed.netloc), f"{field} must include a host")
    return uri


def _validate_version_range(value: Any, field: str) -> None:
    if value == 0:
        return
    text = _require_string(value, field)
    parts = [part.strip() for part in text.split(",")]
    _require(all(parts), f"{field} contains an empty range item")
    for part in parts:
        _require(
            PREREQ_VERSION.fullmatch(part) is not None,
            f"{field} contains invalid version range item {part!r}",
        )


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MetadataError(f"could not parse {path}: {error}") from error
    _require(isinstance(value, dict), f"{path.name} must contain a JSON object")
    return value


def _validate_license(value: Any) -> None:
    values: list[str]
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        _require(bool(value), "license list must not be empty")
        values = [_require_string(item, "license[]") for item in value]
    elif isinstance(value, dict):
        _require(bool(value), "license map must not be empty")
        for name, uri in value.items():
            _require_string(name, "license name")
            _require_uri(uri, f"license[{name!r}]")
        return
    else:
        raise MetadataError("license must be a string, list, or map")
    for license_name in values:
        _require(
            license_name in KNOWN_LICENSES,
            f"unknown PGXN license string {license_name!r}",
        )


def _validate_maintainers(value: Any) -> None:
    maintainers = [value] if isinstance(value, str) else value
    _require(
        isinstance(maintainers, list) and bool(maintainers),
        "maintainer must name at least one contact",
    )
    for index, maintainer in enumerate(maintainers):
        _require_string(maintainer, f"maintainer[{index}]")


def _validate_tags(value: Any) -> None:
    _require(isinstance(value, list), "tags must be a list")
    seen: set[str] = set()
    for index, tag in enumerate(value):
        text = _require_string(tag, f"tags[{index}]")
        _require(
            len(text) < 256,
            f"tags[{index}] must be shorter than 256 characters",
        )
        _require(
            "/" not in text and "\\" not in text,
            f"tags[{index}] must not contain a slash",
        )
        _require(
            not any(ord(character) < 32 or ord(character) == 127 for character in text),
            f"tags[{index}] contains a control character",
        )
        _require(text not in seen, f"duplicate tag {text!r}")
        seen.add(text)


def _validate_resources(value: Any) -> None:
    _require(isinstance(value, dict), "resources must be a map")
    allowed = {"homepage", "bugtracker", "repository"}
    for key in value:
        _require(
            key in allowed or key.lower().startswith("x_"),
            f"resources contains unknown key {key!r}",
        )
    if "homepage" in value:
        _require_uri(value["homepage"], "resources.homepage")
    bugtracker = value.get("bugtracker")
    if bugtracker is not None:
        _require(
            isinstance(bugtracker, dict),
            "resources.bugtracker must be a map",
        )
        _require(
            set(bugtracker) <= {"web", "mailto"},
            "resources.bugtracker contains an unknown key",
        )
        if "web" in bugtracker:
            _require_uri(bugtracker["web"], "resources.bugtracker.web")
        if "mailto" in bugtracker:
            _require_string(bugtracker["mailto"], "resources.bugtracker.mailto")
    repository = value.get("repository")
    if repository is not None:
        _require(
            isinstance(repository, dict),
            "resources.repository must be a map",
        )
        _require(
            set(repository) <= {"url", "web", "type"},
            "resources.repository contains an unknown key",
        )
        if "url" in repository:
            _require_uri(repository["url"], "resources.repository.url")
        if "web" in repository:
            _require_uri(repository["web"], "resources.repository.web")
        if "type" in repository:
            _require(
                repository["type"] == "git",
                "resources.repository.type must be git",
            )


def _validate_prereqs(value: Any) -> None:
    _require(isinstance(value, dict), "prereqs must be a map")
    phases = {"configure", "build", "test", "runtime", "develop"}
    relationships = {"requires", "recommends", "suggests", "conflicts"}
    for phase, phase_value in value.items():
        _require(phase in phases, f"prereqs contains unknown phase {phase!r}")
        _require(
            isinstance(phase_value, dict),
            f"prereqs.{phase} must be a map",
        )
        for relationship, requirements in phase_value.items():
            _require(
                relationship in relationships,
                f"prereqs.{phase} contains unknown relationship {relationship!r}",
            )
            _require(
                isinstance(requirements, dict),
                f"prereqs.{phase}.{relationship} must be a map",
            )
            for name, version_range in requirements.items():
                _require(
                    TERM.fullmatch(name) is not None,
                    f"invalid prerequisite name {name!r}",
                )
                _validate_version_range(
                    version_range,
                    f"prereqs.{phase}.{relationship}.{name}",
                )


def _validate_no_index(value: Any, root: Path) -> None:
    _require(isinstance(value, dict), "no_index must be a map")
    _require(
        set(value) <= {"file", "directory"},
        "no_index contains an unknown key",
    )
    for kind in ("file", "directory"):
        entries = value.get(kind, [])
        _require(isinstance(entries, list), f"no_index.{kind} must be a list")
        for index, entry in enumerate(entries):
            text = _require_string(entry, f"no_index.{kind}[{index}]")
            path = Path(text)
            _require(
                not path.is_absolute() and ".." not in path.parts,
                f"no_index.{kind}[{index}] must be relative",
            )
            _require(
                (root / path).exists(),
                f"no_index.{kind}[{index}] does not exist: {text}",
            )


def control_default_version(root: Path) -> str:
    control = (root / "pg_local_cache.control").read_text(encoding="utf-8")
    matches = CONTROL_VERSION.findall(control)
    _require(
        len(matches) == 1,
        "pg_local_cache.control must contain one strict default_version",
    )
    return matches[0]


def validate_repository(root: Path) -> dict[str, Any]:
    root = root.resolve()
    metadata_path = root / "META.json"
    _require(metadata_path.is_file(), "META.json is missing")
    metadata = _load_json(metadata_path)

    for key in metadata:
        _require(
            key in TOP_LEVEL_KEYS or key.lower().startswith("x_"),
            f"META.json contains unknown key {key!r}",
        )
    for key in (
        "abstract",
        "license",
        "maintainer",
        "meta-spec",
        "name",
        "provides",
        "version",
    ):
        _require(key in metadata, f"META.json is missing required field {key!r}")

    name = _require_string(metadata["name"], "name")
    _require(
        TERM.fullmatch(name) is not None,
        "name is not a valid PGXN term",
    )
    _require(name == "pg_local_cache", "distribution name must be pg_local_cache")
    version = _require_semver(metadata["version"], "version")
    _require_string(metadata["abstract"], "abstract")
    if "description" in metadata:
        _require_string(metadata["description"], "description")
    if "generated_by" in metadata:
        _require_string(metadata["generated_by"], "generated_by")
    _validate_maintainers(metadata["maintainer"])
    _validate_license(metadata["license"])

    meta_spec = metadata["meta-spec"]
    _require(isinstance(meta_spec, dict), "meta-spec must be a map")
    _require(
        meta_spec.get("version") == META_SPEC_VERSION,
        f"meta-spec.version must be {META_SPEC_VERSION}",
    )
    _require(
        meta_spec.get("url") == META_SPEC_URL,
        f"meta-spec.url must be {META_SPEC_URL}",
    )

    if "release_status" in metadata:
        _require(
            metadata["release_status"] in {"stable", "testing", "unstable"},
            "release_status must be stable, testing, or unstable",
        )
    if "tags" in metadata:
        _validate_tags(metadata["tags"])
    if "resources" in metadata:
        _validate_resources(metadata["resources"])
    if "prereqs" in metadata:
        _validate_prereqs(metadata["prereqs"])
    if "no_index" in metadata:
        _validate_no_index(metadata["no_index"], root)

    provides = metadata["provides"]
    _require(
        isinstance(provides, dict) and bool(provides),
        "provides must be a non-empty map",
    )
    for extension_name, provided in provides.items():
        _require(
            TERM.fullmatch(extension_name) is not None,
            f"invalid extension name {extension_name!r}",
        )
        _require(
            isinstance(provided, dict),
            f"provides.{extension_name} must be a map",
        )
        _require(
            set(provided) <= {"abstract", "docfile", "file", "version"},
            f"provides.{extension_name} contains an unknown key",
        )
        for required in ("file", "version"):
            _require(
                required in provided,
                f"provides.{extension_name} is missing {required}",
            )
        extension_version = _require_semver(
            provided["version"],
            f"provides.{extension_name}.version",
        )
        source_file = Path(
            _require_string(
                provided["file"],
                f"provides.{extension_name}.file",
            )
        )
        _require(
            not source_file.is_absolute() and ".." not in source_file.parts,
            f"provides.{extension_name}.file must be relative",
        )
        _require(
            (root / source_file).is_file(),
            f"provides.{extension_name}.file does not exist",
        )
        if "docfile" in provided:
            docfile = Path(
                _require_string(
                    provided["docfile"],
                    f"provides.{extension_name}.docfile",
                )
            )
            _require(
                not docfile.is_absolute() and ".." not in docfile.parts,
                f"provides.{extension_name}.docfile must be relative",
            )
            _require(
                (root / docfile).is_file(),
                f"provides.{extension_name}.docfile does not exist",
            )
        if "abstract" in provided:
            _require_string(
                provided["abstract"],
                f"provides.{extension_name}.abstract",
            )
        if extension_name == "pg_local_cache":
            _require(
                extension_version == version,
                "distribution and pg_local_cache extension versions must match",
            )
            expected_file = Path(f"sql/pg_local_cache--{version}.sql")
            _require(
                source_file == expected_file,
                f"provides.pg_local_cache.file must be {expected_file}",
            )

    _require(
        set(provides) == {"pg_local_cache"},
        "distribution must provide exactly pg_local_cache",
    )
    control_version = control_default_version(root)
    _require(
        version == control_version,
        "META.json version must match pg_local_cache.control default_version",
    )
    header = (root / "src" / "pg_local_cache.h").read_text(
        encoding="utf-8"
    )
    _require(
        f'#define PGLC_VERSION "{version}"' in header,
        "META.json version must match the shared binary/RESP version",
    )
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    _require(
        "EXTENSION = pg_local_cache" in makefile,
        "Makefile must build pg_local_cache",
    )
    _require(
        f"sql/pg_local_cache--{version}.sql" in makefile,
        "Makefile DATA must include the current SQL file",
    )

    postgres_range = (
        metadata.get("prereqs", {})
        .get("runtime", {})
        .get("requires", {})
        .get("PostgreSQL")
    )
    _require(
        postgres_range == ">= 14.0.0, < 19.0.0",
        "PostgreSQL prerequisite must match the supported 14-18 matrix",
    )
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--print-version", action="store_true")
    arguments = parser.parse_args()
    try:
        metadata = validate_repository(arguments.root)
    except (MetadataError, OSError, UnicodeError) as error:
        print(f"PGXN metadata validation failed: {error}", file=sys.stderr)
        return 1
    if arguments.print_version:
        print(metadata["version"])
    else:
        print(
            f"META.json is PGXN-ready for pg_local_cache {metadata['version']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
