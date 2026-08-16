#!/usr/bin/env python3
"""Contracts for the existing-cluster installer and release pipeline."""

from __future__ import annotations

import hashlib
import io
import os
import platform
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-existing.sh"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
ARCHIVE_HELPER = ROOT / "scripts" / "release_archive.py"
FETCHER = ROOT / "scripts" / "fetch-release.sh"
CONTROL = (ROOT / "pg_local_cache.control").read_text(encoding="utf-8")
CURRENT_VERSION_MATCH = re.search(
    r"^default_version = '([0-9]+\.[0-9]+\.[0-9]+)'$", CONTROL, re.MULTILINE
)
if CURRENT_VERSION_MATCH is None:
    raise RuntimeError("could not read current extension version")
CURRENT_VERSION = CURRENT_VERSION_MATCH.group(1)


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_packaged_install(
    directory: Path,
    *,
    postgres_major: int = 16,
    package_postgres_major: int | None = None,
    package_libc: str = "glibc",
    package_os: str = "linux",
    package_architecture: str | None = None,
    active_preload: str = "pg_stat_statements",
    configured_preload: str | None = None,
    active_max_workers: int = 8,
    configured_max_workers: int | None = None,
    active_port: int = 0,
    active_workers: int = 4,
    configured_port: int | str | None = None,
    configured_workers: int | str | None = None,
    configured_bind_address: str = "127.0.0.1",
    configured_cache_entries: int = 16384,
    configured_relation_states: int = 1024,
    configured_max_clients: int = 256,
    configured_max_clients_per_worker: int = 64,
    configured_memory_budget_mb: int = 384,
    configured_token_file: str = "",
    installed_extension_version: str | None = None,
) -> dict[str, Path | str]:
    """Create a binary-release layout and a controllable fake local cluster."""

    if not sys.platform.startswith("linux"):
        raise unittest.SkipTest("installer runtime fixtures require GNU/Linux")

    package_postgres_major = (
        postgres_major
        if package_postgres_major is None
        else package_postgres_major
    )
    if package_architecture is None:
        machine = platform.machine()
        package_architecture = "amd64" if machine in {"x86_64", "amd64"} else machine
    package = directory / (
        f"pg_local_cache-pg{package_postgres_major}-linux-"
        f"{package_libc}-{package_architecture}"
    )
    package_lib = package / "lib"
    package_extension = package / "share/extension"
    target_lib = directory / "target/lib"
    target_share = directory / "target/share"
    target_extension = target_share / "extension"
    data = directory / "data"
    bin_directory = directory / "bin"
    state = directory / "state"
    role_state = directory / "role-state"
    acl_state = directory / "role-connect-acl"
    role_marker_state = directory / "role-marker"
    extension_version_state = directory / "extension-version"
    schema_grant_state = directory / "schema-grant"
    mapping_grant_state = directory / "mapping-grant"
    for item in (
        package_lib,
        package_extension,
        target_lib,
        target_extension,
        data,
        bin_directory,
    ):
        item.mkdir(parents=True, exist_ok=True)

    installer = package / "install.sh"
    shutil.copy2(INSTALLER, installer)
    installer.chmod(0o755)

    # A known platform executable keeps dependency preflight realistic while
    # the fixture remains independent of PostgreSQL development packages.
    source_library = package_lib / "pg_local_cache.so"
    true_binary = shutil.which("true")
    if true_binary is None:
        raise RuntimeError("the installer fixture requires a true executable")
    shutil.copyfile(true_binary, source_library)
    source_library.chmod(0o755)
    (package / "RELEASE-METADATA").write_text(
        "format=1\n"
        "version=1.1.0\n"
        f"postgres_major={package_postgres_major}\n"
        f"os={package_os}\n"
        f"libc={package_libc}\n"
        f"architecture={package_architecture}\n"
        f"commit={'0' * 40}\n"
        "build_image=fixture\n",
        encoding="utf-8",
    )
    (package / "BUILD-ID").write_text(f"{'0' * 40}\n", encoding="ascii")
    (package_extension / "pg_local_cache.control").write_text(
        "comment = 'fixture'\ndefault_version = '1.1.0'\n"
        "module_pathname = '$libdir/pg_local_cache'\n",
        encoding="utf-8",
    )
    (package_extension / "pg_local_cache--1.0.0.sql").write_text(
        "-- fixture SQL\n", encoding="utf-8"
    )
    (package_extension / "pg_local_cache--1.1.0.sql").write_text(
        "-- fixture SQL\n", encoding="utf-8"
    )
    (package_extension / "pg_local_cache--1.0.0--1.1.0.sql").write_text(
        "-- fixture upgrade SQL\n", encoding="utf-8"
    )

    auto_conf = data / "postgresql.auto.conf"
    auto_conf.write_text("# original auto.conf\n", encoding="utf-8")
    if installed_extension_version is not None:
        extension_version_state.write_text(installed_extension_version, encoding="ascii")

    pg_config = bin_directory / "pg_config"
    _write_executable(
        pg_config,
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            case "$1" in
              --version) printf '%s\\n' 'PostgreSQL {postgres_major}.14' ;;
              --pkglibdir) printf '%s\\n' {str(target_lib)!r} ;;
              --sharedir) printf '%s\\n' {str(target_share)!r} ;;
              --bindir) printf '%s\\n' {str(bin_directory)!r} ;;
              *) exit 2 ;;
            esac
            """
        ),
    )

    psql = bin_directory / "psql"
    _write_executable(
        psql,
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -Eeuo pipefail
            original_arguments=("$@")
            query=""
            setting_name=""
            target_version=""
            while (($#)); do
              case "$1" in
                --command|-c) query="$2"; shift 2 ;;
                --set)
                  [[ "$2" == setting_name=* ]] && setting_name="${2#setting_name=}"
                  [[ "$2" == target_version=* ]] && target_version="${2#target_version=}"
                  shift 2 ;;
                *) shift ;;
              esac
            done

            if [[ -z "$query" ]]; then
              query="$(cat)"
            fi

            if [[ "$query" == 'SHOW server_version_num' ]]; then
              printf '%s\\n' "$FAKE_SERVER_VERSION_NUM"
            elif [[ "$query" == *'rolsuper FROM'* ]]; then
              printf 't\\n'
            elif [[ "$query" == *'NOT pg_catalog.pg_is_in_recovery'* ]]; then
              printf 't\\n'
            elif [[ "$query" == *"name = 'PKGLIBDIR'"* ]]; then
              printf '%s\\n' "$FAKE_TARGET_LIB"
            elif [[ "$query" == *"name = 'SHAREDIR'"* ]]; then
              printf '%s\\n' "$FAKE_TARGET_SHARE"
            elif [[ "$query" == 'SHOW data_directory' ]]; then
              printf '%s\\n' "$FAKE_DATA_DIRECTORY"
            elif [[ "$query" == *'pg_control_system()'* ]]; then
              printf '7300000000000000000\\n'
            elif [[ "$query" == *'pg_database WHERE datname'* ]]; then
              printf '16384\\n'
            elif [[ "$query" == *'pg_postmaster_start_time()'* ]]; then
              printf '%s\\n' "${FAKE_POSTMASTER_START:-2026-08-15 00:00:00+00}"
            elif [[ "$query" == *"current_setting('pg_local_cache.binary_version')"* ]]; then
              printf '%s|%s\\n' "$FAKE_BINARY_VERSION" "$FAKE_BINARY_BUILD_ID"
            elif [[ "$query" == *'FROM (SELECT 1) AS seed'* && "$query" == *'pg_extension AS e'* ]]; then
              if [[ -f "$FAKE_EXTENSION_VERSION_STATE" ]]; then
                printf '%s|%s|%s|%s|%s\\n' "$FAKE_EXTENSION_OID" "$FAKE_EXTENSION_SCHEMA_OID" "$FAKE_EXTENSION_OWNER_OID" "$(cat "$FAKE_EXTENSION_VERSION_STATE")" "$FAKE_EXTENSION_OWNER_OID"
              else
                printf '||||10\\n'
              fi
            elif [[ "$query" == *'CASE WHEN r.oid IS NULL OR n.oid IS NULL'* ]]; then
              if [[ -f "$FAKE_EXTENSION_VERSION_STATE" ]]; then
                printf '16384|%s|%s|%s|%s|local_cache|%s|%s\\n' \
                  "$FAKE_EXTENSION_OID" "$FAKE_EXTENSION_SCHEMA_OID" "$FAKE_EXTENSION_OWNER_OID" \
                  "$(cat "$FAKE_EXTENSION_VERSION_STATE")" \
                  "$([[ -f "$FAKE_SCHEMA_GRANT_STATE" ]] && printf true || printf false)" \
                  "$([[ -f "$FAKE_MAPPING_GRANT_STATE" ]] && printf true || printf false)"
              else
                printf '16384|||||false|false\\n'
              fi
            elif [[ "$query" == *'pg_available_extension_versions'* ]]; then
              [[ "${FAKE_DEFAULT_AVAILABLE:-1}" == 1 ]] && printf '1\\n' || printf '0\\n'
            elif [[ "$query" == *'f.error IS NOT NULL'* || "$query" == *'pg_file_settings WHERE error IS NOT NULL'* ]]; then
              printf '0\\n'
            elif [[ "$query" == *'has_database_privilege'* && "$query" == *'aclexplode'* ]]; then
              if [[ -f "$FAKE_ROLE_STATE" ]]; then
                printf '42420|true|%s\\n' "$([[ -f "$FAKE_ACL_STATE" ]] && printf true || printf false)"
              else
                printf '|false|false\\n'
              fi
            elif [[ "$query" == *'aclexplode'* ]]; then
              if [[ -f "$FAKE_ROLE_STATE" ]]; then
                printf '42420|%s\\n' "$([[ -f "$FAKE_ACL_STATE" ]] && printf true || printf false)"
              else
                printf '|false\\n'
              fi
            elif [[ "$query" == *'pg_file_settings'* && "$query" == *'shared_preload_libraries'* ]]; then
              printf '%s\\n' "$FAKE_CONFIGURED_PRELOAD"
            elif [[ "$query" == *'pg_file_settings'* && "$query" == *'max_worker_processes'* ]]; then
              printf '%s\\n' "$FAKE_CONFIGURED_MAX_WORKERS"
            elif [[ "$query" == *'pg_file_settings'* && "$query" == *'pg_local_cache.port'* ]]; then
              printf '%s\\n' "$FAKE_CONFIGURED_PORT"
            elif [[ "$query" == *'pg_file_settings'* && "$query" == *'pg_local_cache.workers'* ]]; then
              printf '%s\\n' "$FAKE_CONFIGURED_WORKERS"
            elif [[ -n "$setting_name" && "$query" == *"current_setting(:'setting_name'"* ]]; then
              case "$setting_name" in
                pg_local_cache.port) printf '%s\\n' "$FAKE_CONFIGURED_PORT" ;;
                pg_local_cache.workers) printf '%s\\n' "$FAKE_CONFIGURED_WORKERS" ;;
                pg_local_cache.bind_address) printf '%s\\n' "$FAKE_CONFIGURED_BIND_ADDRESS" ;;
                pg_local_cache.cache_entries) printf '%s\\n' "$FAKE_CONFIGURED_CACHE_ENTRIES" ;;
                pg_local_cache.relation_states) printf '%s\\n' "$FAKE_CONFIGURED_RELATION_STATES" ;;
                pg_local_cache.max_clients) printf '%s\\n' "$FAKE_CONFIGURED_MAX_CLIENTS" ;;
                pg_local_cache.max_clients_per_worker) printf '%s\\n' "$FAKE_CONFIGURED_MAX_CLIENTS_PER_WORKER" ;;
                pg_local_cache.memory_budget_mb) printf '%s\\n' "$FAKE_CONFIGURED_MEMORY_BUDGET_MB" ;;
                pg_local_cache.auth_token_file) printf '%s\\n' "$FAKE_CONFIGURED_TOKEN_FILE" ;;
                *) exit 3 ;;
              esac
            elif [[ "$query" == 'SHOW shared_preload_libraries' ]]; then
              printf '%s\\n' "$FAKE_ACTIVE_PRELOAD"
            elif [[ "$query" == 'SHOW max_worker_processes' ]]; then
              printf '%s\\n' "$FAKE_ACTIVE_MAX_WORKERS"
            elif [[ "$query" == *"current_setting('pg_local_cache.port'"* && "$query" == *"current_setting('pg_local_cache.workers'"* ]]; then
              printf '%s|%s\\n' "$FAKE_ACTIVE_PORT" "$FAKE_ACTIVE_WORKERS"
            elif [[ "$query" == *"current_setting('pg_local_cache.port')"* && "$query" == *'pg_stat_activity'* ]]; then
              printf '%s|%s\\n' "$FAKE_ACTIVE_PORT" "$([[ "$FAKE_ACTIVE_PORT" == 0 ]] && printf 0 || printf "$FAKE_ACTIVE_WORKERS")"
            elif [[ "$query" == *"current_setting('pg_local_cache.port'"* || "$query" == 'SHOW pg_local_cache.port' ]]; then
              printf '%s\\n' "$FAKE_ACTIVE_PORT"
            elif [[ "$query" == *"current_setting('pg_local_cache.workers'"* || "$query" == 'SHOW pg_local_cache.workers' ]]; then
              printf '%s\\n' "$FAKE_ACTIVE_WORKERS"
            elif [[ "$query" == *'ALTER SYSTEM SET shared_preload_libraries'* ]]; then
              if [[ -n "${FAKE_CAPTURE_ARGUMENTS:-}" ]]; then
                printf '%s\\n' "${original_arguments[@]}" > "$FAKE_CAPTURE_ARGUMENTS"
              fi
              if [[ -n "${FAKE_CAPTURE_SQL:-}" ]]; then
                printf '%s\\n' "$query" > "$FAKE_CAPTURE_SQL"
              fi
              if [[ "${FAKE_FAIL_ALTER:-0}" == 1 ]]; then
                printf '%s\\n' '# partially rewritten by ALTER SYSTEM' > "$FAKE_AUTO_CONF"
                printf '%s\\n' 'fixture ALTER SYSTEM failure' >&2
                exit 42
              fi
            elif [[ "$query" == *'rolcanlogin'* ]]; then
              [[ -f "$FAKE_ROLE_STATE" ]] && printf 't|f|f|f|f|f|f\\n' || :
            elif [[ "$query" == *'CREATE ROLE'* || "$query" == *'GRANT CONNECT'* ]]; then
              : > "$FAKE_ROLE_STATE"
              : > "$FAKE_ACL_STATE"
              if [[ "$query" == *'COMMENT ON ROLE'* ]]; then
                for argument in "${original_arguments[@]}"; do
                  [[ "$argument" == role_marker=* ]] && printf '%s' "${argument#role_marker=}" > "$FAKE_ROLE_MARKER_STATE"
                done
              fi
            elif [[ "$query" == *'SELECT oid::text FROM pg_catalog.pg_roles'* ]]; then
              [[ -f "$FAKE_ROLE_STATE" ]] && printf '42420\\n' || :
            elif [[ "$query" == *'shobj_description'* ]]; then
              [[ -f "$FAKE_ROLE_MARKER_STATE" ]] && cat "$FAKE_ROLE_MARKER_STATE" && printf '\\n' || :
            elif [[ "$query" == *'COMMENT ON ROLE'* && "$query" == *'IS NULL'* ]]; then
              rm -f "$FAKE_ROLE_MARKER_STATE"
            elif [[ "$query" == *'REVOKE CONNECT'* ]]; then
              rm -f "$FAKE_ACL_STATE"
              if [[ "$query" == *'DROP ROLE'* ]]; then
                rm -f "$FAKE_ROLE_STATE" "$FAKE_ROLE_MARKER_STATE"
              fi
            elif [[ "$query" == *'DROP ROLE'* ]]; then
              rm -f "$FAKE_ROLE_STATE" "$FAKE_ACL_STATE" "$FAKE_ROLE_MARKER_STATE"
            elif [[ "$query" == *'BEGIN ISOLATION LEVEL SERIALIZABLE'* ]]; then
              [[ "${FAKE_FAIL_ACTIVATION:-0}" != 1 ]] || exit 42
              if [[ "$query" == *'pg_extension_update_paths'* && "${FAKE_UPDATE_PATH:-1}" != 1 ]]; then
                exit 3
              fi
              [[ -n "$target_version" ]] || target_version="$(cat "$FAKE_EXTENSION_VERSION_STATE")"
              printf '%s' "$target_version" > "$FAKE_EXTENSION_VERSION_STATE"
              : > "$FAKE_SCHEMA_GRANT_STATE"
              : > "$FAKE_MAPPING_GRANT_STATE"
              if [[ -n "${FAKE_CAPTURE_ACTIVATION_SQL:-}" ]]; then printf '%s\\n' "$query" > "$FAKE_CAPTURE_ACTIVATION_SQL"; fi
            elif [[ "$query" == *"local_cache.health() ->> 'ready'"* ]]; then
              printf 't\\n'
            elif [[ "$query" == *'pg_reload_conf()'* ]]; then
              printf 't\\n'
            else
              printf 'unexpected query: %s\\n' "$query" >&2
              exit 3
            fi
            """
        ),
    )

    configured_preload = (
        active_preload if configured_preload is None else configured_preload
    )
    configured_max_workers = (
        active_max_workers
        if configured_max_workers is None
        else configured_max_workers
    )
    configured_port = active_port if configured_port is None else configured_port
    configured_workers = (
        active_workers if configured_workers is None else configured_workers
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_directory}:/usr/bin:/bin",
            "FAKE_TARGET_LIB": str(target_lib),
            "FAKE_TARGET_SHARE": str(target_share),
            "FAKE_DATA_DIRECTORY": str(data),
            "FAKE_AUTO_CONF": str(auto_conf),
            "FAKE_ACTIVE_PRELOAD": active_preload,
            "FAKE_CONFIGURED_PRELOAD": configured_preload,
            "FAKE_ACTIVE_MAX_WORKERS": str(active_max_workers),
            "FAKE_CONFIGURED_MAX_WORKERS": str(configured_max_workers),
            "FAKE_ACTIVE_PORT": str(active_port),
            "FAKE_ACTIVE_WORKERS": str(active_workers),
            "FAKE_CONFIGURED_PORT": str(configured_port),
            "FAKE_CONFIGURED_WORKERS": str(configured_workers),
            "FAKE_CONFIGURED_BIND_ADDRESS": configured_bind_address,
            "FAKE_CONFIGURED_CACHE_ENTRIES": str(configured_cache_entries),
            "FAKE_CONFIGURED_RELATION_STATES": str(configured_relation_states),
            "FAKE_CONFIGURED_MAX_CLIENTS": str(configured_max_clients),
            "FAKE_CONFIGURED_MAX_CLIENTS_PER_WORKER": str(configured_max_clients_per_worker),
            "FAKE_CONFIGURED_MEMORY_BUDGET_MB": str(configured_memory_budget_mb),
            "FAKE_CONFIGURED_TOKEN_FILE": configured_token_file,
            "FAKE_BINARY_VERSION": "1.1.0",
            "FAKE_BINARY_BUILD_ID": "0" * 40,
            "FAKE_EXTENSION_OID": "73001",
            "FAKE_EXTENSION_SCHEMA_OID": "23001",
            "FAKE_EXTENSION_OWNER_OID": "10",
            "FAKE_SERVER_VERSION_NUM": f"{postgres_major}0014",
            "FAKE_ROLE_STATE": str(role_state),
            "FAKE_ACL_STATE": str(acl_state),
            "FAKE_ROLE_MARKER_STATE": str(role_marker_state),
            "FAKE_EXTENSION_VERSION_STATE": str(extension_version_state),
            "FAKE_SCHEMA_GRANT_STATE": str(schema_grant_state),
            "FAKE_MAPPING_GRANT_STATE": str(mapping_grant_state),
        }
    )
    return {
        "package": package,
        "installer": installer,
        "source_library": source_library,
        "target_lib": target_lib,
        "target_extension": target_extension,
        "data": data,
        "auto_conf": auto_conf,
        "state": state,
        "role_state": role_state,
        "acl_state": acl_state,
        "role_marker_state": role_marker_state,
        "extension_version_state": extension_version_state,
        "schema_grant_state": schema_grant_state,
        "mapping_grant_state": mapping_grant_state,
        "pg_config": pg_config,
        "psql": psql,
        "environment": environment,
    }


def _installer_arguments(
    fixture: dict[str, Path | str], command: str, *extra: str
) -> list[str]:
    os_user = pwd.getpwuid(os.getuid()).pw_name
    return [
        str(fixture["installer"]),
        command,
        "--database",
        "app",
        "--postgres-os-user",
        os_user,
        "--pg-config",
        str(fixture["pg_config"]),
        "--psql",
        str(fixture["psql"]),
        "--state-root",
        str(fixture["state"]),
        *extra,
    ]


def _run_installer(
    fixture: dict[str, Path | str], command: str, *extra: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _installer_arguments(fixture, command, *extra),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=fixture["environment"] if env is None else env,
    )


def _state_path(result: subprocess.CompletedProcess[str]) -> Path:
    match = re.search(r"state directory: (.+)", result.stdout + result.stderr)
    if match is None:
        raise AssertionError(result.stdout + result.stderr)
    return Path(match.group(1).strip())


def _restarted_environment(
    fixture: dict[str, Path | str], **overrides: str
) -> dict[str, str]:
    environment = dict(fixture["environment"])
    environment.update(
        {
            "FAKE_POSTMASTER_START": "2026-08-15 00:01:00+00",
            "FAKE_ACTIVE_PRELOAD": "pg_stat_statements,pg_local_cache",
            "FAKE_CONFIGURED_PRELOAD": "pg_stat_statements,pg_local_cache",
            **overrides,
        }
    )
    return environment


class InstallerContracts(unittest.TestCase):
    def test_help_is_dependency_free_and_documents_safe_restart_default(self) -> None:
        result = subprocess.run(
            [str(INSTALLER), "--help"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin"},
        )
        self.assertIn("SQL-only mode", result.stdout)
        self.assertIn("no PostgreSQL restart", result.stdout)
        self.assertIn("--restart-method none|systemd|pg_ctl", result.stdout)
        self.assertNotIn("AUTH token value", result.stdout)

    def test_installer_has_valid_shell_and_no_eval(self) -> None:
        subprocess.run(["bash", "-n", str(INSTALLER)], check=True)
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertNotIn("eval ", source)
        self.assertNotIn("shared_preload_libraries = 'pg_local_cache'", source)
        self.assertIn("preload_after=\"${preload_before},pg_local_cache\"", source)
        self.assertIn("postgresql.auto.conf.before", source)
        self.assertIn("pg_file_settings", source)
        self.assertIn("restart exceeded", source)
        self.assertIn("run_as_postgres bash -c", source)
        self.assertIn("set -o noclobber", source)
        self.assertIn("release_os_user=\"$(stat -c '%U' \"$release_root\")\"", source)
        self.assertIn(
            "target_owner=\"$(stat -c '%U' \"${target_files[index]}\")\"", source
        )
        self.assertNotIn("id -nu", source)
        self.assertNotIn('cat > "$temporary"\n    chown', source)

    def test_sql_only_dry_run_is_read_only_and_preserves_preloads(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            bin_directory = directory / "bin"
            pkglib = directory / "pkglib"
            shared = directory / "share"
            extension = shared / "extension"
            data = directory / "data"
            state = directory / "state"
            for item in (bin_directory, pkglib, extension, data):
                item.mkdir(parents=True, exist_ok=True)
            auto_conf = data / "postgresql.auto.conf"
            auto_conf.write_text("# existing\n", encoding="utf-8")
            before = hashlib.sha256(auto_conf.read_bytes()).hexdigest()

            pg_config = bin_directory / "pg_config"
            pg_config.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    case "$1" in
                      --version) printf '%s\\n' 'PostgreSQL 16.14' ;;
                      --pkglibdir) printf '%s\\n' {str(pkglib)!r} ;;
                      --sharedir) printf '%s\\n' {str(shared)!r} ;;
                      --bindir) printf '%s\\n' {str(bin_directory)!r} ;;
                      *) exit 2 ;;
                    esac
                    """
                ),
                encoding="utf-8",
            )
            pg_config.chmod(pg_config.stat().st_mode | stat.S_IXUSR)

            psql = bin_directory / "psql"
            psql.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    query=""
                    while (($#)); do
                      if [[ "$1" == "--command" ]]; then query="$2"; shift 2; else shift; fi
                    done
                    case "$query" in
                      'SHOW server_version_num') printf '160014\\n' ;;
                      *'rolsuper FROM'*) printf 't\\n' ;;
                      *'NOT pg_catalog.pg_is_in_recovery'*) printf 't\\n' ;;
                      *"name = 'PKGLIBDIR'"*) printf '%s\\n' {str(pkglib)!r} ;;
                      *"name = 'SHAREDIR'"*) printf '%s\\n' {str(shared)!r} ;;
                      'SHOW data_directory') printf '%s\\n' {str(data)!r} ;;
                      *'pg_control_system()'*) printf '7300000000000000000\\n' ;;
                      *'pg_database WHERE datname'*) printf '16384\\n' ;;
                      *'pg_postmaster_start_time()'*) printf '2026-08-15 00:00:00+00\\n' ;;
                      *'pg_file_settings WHERE error IS NOT NULL'*|*'f.error IS NOT NULL'*) printf '0\\n' ;;
                      *'pg_file_settings'*'shared_preload_libraries'*) printf 'pg_stat_statements,auto_explain\\n' ;;
                      *'pg_file_settings'*'max_worker_processes'*) printf '8\\n' ;;
                      'SHOW shared_preload_libraries') printf 'pg_stat_statements,auto_explain\\n' ;;
                      'SHOW max_worker_processes') printf '8\\n' ;;
                      *) printf 'unexpected query: %s\\n' "$query" >&2; exit 3 ;;
                    esac
                    """
                ),
                encoding="utf-8",
            )
            psql.chmod(psql.stat().st_mode | stat.S_IXUSR)

            result = subprocess.run(
                [
                    str(INSTALLER),
                    "install",
                    "--database",
                    "app",
                    "--mode",
                    "sql-only",
                    "--postgres-os-user",
                    "root",
                    "--pg-config",
                    str(pg_config),
                    "--psql",
                    str(psql),
                    "--state-root",
                    str(state),
                    "--dry-run",
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"PATH": f"{bin_directory}:/usr/bin:/bin"},
            )
            self.assertIn(
                "'pg_stat_statements,auto_explain' -> "
                "'pg_stat_statements,auto_explain,pg_local_cache'",
                result.stdout,
            )
            self.assertIn("no PostgreSQL restart was performed", result.stdout)
            self.assertFalse(state.exists())
            self.assertEqual(
                hashlib.sha256(auto_conf.read_bytes()).hexdigest(), before
            )
            self.assertEqual(auto_conf.read_text(encoding="utf-8"), "# existing\n")

    def test_fresh_install_allows_only_its_unregistered_gucs(self) -> None:
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("configuration_error_count true", source)
        self.assertIn("s.name IS NULL", source)
        self.assertIn("'pg_local_cache.auth_token_file'", source)
        self.assertNotIn("f.name LIKE 'pg_local_cache.%'", source)
        self.assertIn(
            'REVOKE CONNECT ON DATABASE :"cache_database" FROM :"worker_role";',
            source,
        )
        self.assertIn('^([0-9]+)MB$', source)

    def test_binary_archive_root_is_installed_without_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            fixture = _fake_packaged_install(Path(raw_directory))
            result = subprocess.run(
                _installer_arguments(fixture, "install", "--mode", "sql-only"),
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=fixture["environment"],
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            installed_library = Path(fixture["target_lib"]) / "pg_local_cache.so"
            self.assertEqual(
                installed_library.read_bytes(),
                Path(fixture["source_library"]).read_bytes(),
            )
            self.assertTrue(
                (Path(fixture["target_extension"]) / "pg_local_cache.control").is_file()
            )
            self.assertTrue(
                (
                    Path(fixture["target_extension"])
                    / "pg_local_cache--1.1.0.sql"
                ).is_file()
            )
            self.assertTrue(
                (
                    Path(fixture["target_extension"])
                    / "pg_local_cache--1.0.0--1.1.0.sql"
                ).is_file()
            )

    def test_binary_mismatch_fails_before_any_install_write(self) -> None:
        if not sys.platform.startswith("linux"):
            raise unittest.SkipTest("installer runtime fixtures require GNU/Linux")
        ldd = subprocess.run(
            ["ldd", "--version"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        host_libc = "musl" if "musl" in (ldd.stdout + ldd.stderr).lower() else "glibc"
        wrong_libc = "glibc" if host_libc == "musl" else "musl"
        cases = (
            ({"package_postgres_major": 15}, "targets PostgreSQL 15"),
            ({"package_libc": wrong_libc}, "binary libc"),
            ({"package_os": "darwin"}, "binary targets darwin"),
            (
                {
                    "package_architecture": (
                        "amd64" if platform.machine() not in {"x86_64", "amd64"} else "arm64"
                    )
                },
                "binary architecture",
            ),
        )
        for options, message in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as raw:
                fixture = _fake_packaged_install(Path(raw), **options)
                result = subprocess.run(
                    _installer_arguments(fixture, "install", "--mode", "sql-only"),
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=fixture["environment"],
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stdout + result.stderr)
                self.assertEqual(list(Path(fixture["target_lib"]).iterdir()), [])
                self.assertEqual(
                    list(Path(fixture["target_extension"]).iterdir()), []
                )
                self.assertFalse(Path(fixture["state"]).exists())

    def test_resp_token_accepts_only_owner_readable_0400_or_0600(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            fixture = _fake_packaged_install(Path(raw_directory))
            token = Path(raw_directory) / "auth-token"
            token.write_text("a" * 48 + "\n", encoding="ascii")

            for mode in (0o400, 0o600):
                with self.subTest(mode=oct(mode)):
                    token.chmod(mode)
                    result = subprocess.run(
                        _installer_arguments(
                            fixture,
                            "preflight",
                            "--mode",
                            "resp",
                            "--token-file",
                            str(token),
                        ),
                        check=False,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=fixture["environment"],
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )

            for mode in (0o000, 0o200, 0o700):
                with self.subTest(mode=oct(mode)):
                    token.chmod(mode)
                    result = subprocess.run(
                        _installer_arguments(
                            fixture,
                            "preflight",
                            "--mode",
                            "resp",
                            "--token-file",
                            str(token),
                        ),
                        check=False,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=fixture["environment"],
                    )
                    self.assertNotEqual(result.returncode, 0)

    def test_resp_token_framing_matches_documented_subset_and_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            fixture = _fake_packaged_install(directory)
            token = directory / "auth-token"
            accepted = (b"A" * 32, b"b" * 256 + b"\n", b"C_" * 24 + b"\r\n")
            rejected = (
                b"a" * 31,
                b"a" * 257,
                b"a" * 48 + b"\r",
                b"a" * 48 + b"\n\n",
                b"a" * 24 + b"\n" + b"a" * 24,
                b"a" * 47 + b"!",
                b"a" * 47 + b"\x00",
            )
            for contents in accepted:
                with self.subTest(accepted=repr(contents[-4:])):
                    token.write_bytes(contents)
                    token.chmod(0o600)
                    result = _run_installer(
                        fixture,
                        "preflight",
                        "--mode",
                        "resp",
                        "--token-file",
                        str(token),
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn(contents.rstrip(b"\r\n").decode("ascii"), result.stdout + result.stderr)
            for contents in rejected:
                with self.subTest(rejected=repr(contents[-4:])):
                    token.write_bytes(contents)
                    token.chmod(0o600)
                    result = _run_installer(
                        fixture,
                        "preflight",
                        "--mode",
                        "resp",
                        "--token-file",
                        str(token),
                    )
                    self.assertNotEqual(result.returncode, 0)
                    printable = contents.replace(b"\x00", b"").decode("ascii", errors="ignore").strip()
                    if printable:
                        self.assertNotIn(printable, result.stdout + result.stderr)

    def test_package_and_destination_validation_precedes_state_or_target_writes(self) -> None:
        cases = (
            "source_symlink",
            "source_mode",
            "package_mode",
            "bad_sql",
            "missing_update",
            "target_symlink",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw_directory:
                fixture = _fake_packaged_install(Path(raw_directory))
                source_library = Path(fixture["source_library"])
                package = Path(fixture["package"])
                if case == "source_symlink":
                    source_library.unlink()
                    source_library.symlink_to("/bin/true")
                elif case == "source_mode":
                    source_library.chmod(0o777)
                elif case == "package_mode":
                    package.chmod(0o777)
                elif case == "bad_sql":
                    (package / "share/extension/pg_local_cache--bad.sql").write_text("-- bad\n")
                elif case == "missing_update":
                    (package / "share/extension/pg_local_cache--1.0.0--1.1.0.sql").unlink()
                else:
                    (Path(fixture["target_lib"]) / "pg_local_cache.so").symlink_to("/bin/true")
                result = _run_installer(fixture, "install", "--mode", "sql-only")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(Path(fixture["state"]).exists())
                self.assertFalse((Path(fixture["data"]) / ".pg_local_cache-install.lock").exists())
                self.assertEqual(
                    [path.name for path in Path(fixture["target_extension"]).iterdir()], []
                )

    def test_pre_restart_failures_restore_files_metadata_role_acl_and_config(self) -> None:
        for point in (
            "after_file_extension_so",
            "after_role_commit",
            "after_role",
            "after_configuration",
        ):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as raw_directory:
                fixture = _fake_packaged_install(Path(raw_directory))
                target_library = Path(fixture["target_lib"]) / "pg_local_cache.so"
                target_library.write_bytes(b"old-extension-bytes")
                target_library.chmod(0o640)
                auto_conf = Path(fixture["auto_conf"])
                before_config = auto_conf.read_bytes()
                environment = dict(fixture["environment"])
                environment["PGLC_INSTALL_FAIL_AT"] = point
                result = _run_installer(
                    fixture,
                    "install",
                    "--mode",
                    "sql-only",
                    "--force",
                    env=environment,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(target_library.read_bytes(), b"old-extension-bytes")
                self.assertEqual(stat.S_IMODE(target_library.stat().st_mode), 0o640)
                self.assertEqual(auto_conf.read_bytes(), before_config)
                self.assertFalse(Path(fixture["role_state"]).exists())
                self.assertFalse(Path(fixture["acl_state"]).exists())
                self.assertFalse((Path(fixture["data"]) / ".pg_local_cache-install.lock").exists())
                state_path = _state_path(result)
                self.assertIn("state\trecovered\n", (state_path / "state.tsv").read_text())

    def test_lock_is_single_writer_and_recover_is_state_bound(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            fixture = _fake_packaged_install(Path(raw_directory))
            first = _run_installer(fixture, "install", "--mode", "sql-only")
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            state_path = _state_path(first)
            lock = Path(fixture["data"]) / ".pg_local_cache-install.lock"
            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((lock / "owner.tsv").stat().st_mode), 0o600)
            second = _run_installer(fixture, "install", "--mode", "sql-only")
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("another install or pending recovery", second.stdout + second.stderr)
            recovered = _run_installer(
                fixture, "recover", "--state-directory", str(state_path)
            )
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assertFalse(lock.exists())
            self.assertFalse(Path(fixture["role_state"]).exists())
            self.assertEqual(list(Path(fixture["target_extension"]).iterdir()), [])

    def test_crash_after_lock_owner_allows_only_matching_ownerless_cleanup(self) -> None:
        for point in ("after_lock", "before_journal"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as raw_directory:
                fixture = _fake_packaged_install(Path(raw_directory))
                environment = dict(fixture["environment"])
                environment["PGLC_INSTALL_CRASH_AT"] = point
                crashed = _run_installer(
                    fixture, "install", "--mode", "sql-only", env=environment
                )
                self.assertNotEqual(crashed.returncode, 0)
                state_path = _state_path(crashed)
                self.assertEqual(state_path.exists(), point == "before_journal")
                lock = Path(fixture["data"]) / ".pg_local_cache-install.lock"
                self.assertTrue((lock / "owner.tsv").is_file())
                cleaned = _run_installer(
                    fixture, "recover", "--state-directory", str(state_path)
                )
                self.assertEqual(cleaned.returncode, 0, cleaned.stdout + cleaned.stderr)
                self.assertFalse(state_path.exists())
                self.assertFalse(lock.exists())

    def test_manual_restart_routes_unchanged_to_recover_and_changed_to_verify(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            fixture = _fake_packaged_install(Path(raw_directory))
            installed = _run_installer(fixture, "install", "--mode", "sql-only")
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            state_path = _state_path(installed)
            unchanged = _run_installer(
                fixture, "verify", "--state-directory", str(state_path)
            )
            self.assertNotEqual(unchanged.returncode, 0)
            self.assertIn("has not restarted", unchanged.stdout + unchanged.stderr)
            environment = dict(fixture["environment"])
            environment.update(
                {
                    "FAKE_POSTMASTER_START": "2026-08-15 00:01:00+00",
                    "FAKE_ACTIVE_PRELOAD": "pg_stat_statements,pg_local_cache",
                    "FAKE_CONFIGURED_PRELOAD": "pg_stat_statements,pg_local_cache",
                }
            )
            verified = _run_installer(
                fixture,
                "verify",
                "--state-directory",
                str(state_path),
                env=environment,
            )
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
            self.assertIn("state\tcomplete\n", (state_path / "state.tsv").read_text())
            self.assertFalse((Path(fixture["data"]) / ".pg_local_cache-install.lock").exists())

    def test_restart_request_is_irreversible_before_restart_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            fixture = _fake_packaged_install(Path(raw_directory))
            environment = dict(fixture["environment"])
            environment["PGLC_INSTALL_FAIL_AT"] = "after_restart_requested"
            result = _run_installer(
                fixture,
                "install",
                "--mode",
                "sql-only",
                "--restart-method",
                "systemd",
                "--systemd-unit",
                "fixture.service",
                env=environment,
            )
            self.assertNotEqual(result.returncode, 0)
            state_path = _state_path(result)
            self.assertIn("state\trestart_requested\n", (state_path / "state.tsv").read_text())
            self.assertTrue((Path(fixture["target_lib"]) / "pg_local_cache.so").is_file())
            recovery = _run_installer(
                fixture, "recover", "--state-directory", str(state_path)
            )
            self.assertNotEqual(recovery.returncode, 0)
            self.assertIn("past the automatic recovery boundary", recovery.stdout + recovery.stderr)

    def test_state_or_lock_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            fixture = _fake_packaged_install(Path(raw_directory))
            installed = _run_installer(fixture, "install", "--mode", "sql-only")
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            state_path = _state_path(installed)
            owner = Path(fixture["data"]) / ".pg_local_cache-install.lock/owner.tsv"
            owner.chmod(0o644)
            recovery = _run_installer(
                fixture, "recover", "--state-directory", str(state_path)
            )
            self.assertNotEqual(recovery.returncode, 0)
            self.assertIn("mode 0600", recovery.stdout + recovery.stderr)
            self.assertTrue(owner.exists())
            self.assertTrue((Path(fixture["target_lib"]) / "pg_local_cache.so").exists())

    def test_pending_file_settings_are_preserved_in_written_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            fixture = _fake_packaged_install(
                directory,
                active_preload="pg_stat_statements",
                configured_preload="pg_stat_statements,auto_explain",
                active_max_workers=8,
                configured_max_workers=12,
            )
            capture = directory / "alter-arguments"
            environment = dict(fixture["environment"])
            environment["FAKE_CAPTURE_ARGUMENTS"] = str(capture)
            result = subprocess.run(
                _installer_arguments(fixture, "install", "--mode", "sql-only"),
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            arguments = capture.read_text(encoding="utf-8")
            self.assertIn(
                "preload=pg_stat_statements,auto_explain,pg_local_cache", arguments
            )
            self.assertIn("max_workers=12", arguments)

    def test_sql_only_to_resp_adds_the_new_worker_slots(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            fixture = _fake_packaged_install(
                directory,
                active_preload="pg_stat_statements,pg_local_cache",
                configured_preload="pg_stat_statements,pg_local_cache",
                active_max_workers=8,
                configured_max_workers=8,
                active_port=0,
                active_workers=4,
            )
            token = directory / "auth-token"
            token.write_text("b" * 48 + "\n", encoding="ascii")
            token.chmod(0o400)
            capture = directory / "alter-arguments"
            environment = dict(fixture["environment"])
            environment["FAKE_CAPTURE_ARGUMENTS"] = str(capture)
            result = subprocess.run(
                _installer_arguments(
                    fixture,
                    "install",
                    "--mode",
                    "resp",
                    "--workers",
                    "6",
                    "--token-file",
                    str(token),
                ),
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("max_workers=14", capture.read_text(encoding="utf-8"))

    def test_partial_alter_system_failure_restores_exact_auto_conf(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            fixture = _fake_packaged_install(directory)
            auto_conf = Path(fixture["auto_conf"])
            original = (
                b"# preserve byte-for-byte\n"
                b"shared_preload_libraries = 'pg_stat_statements'\n"
            )
            auto_conf.write_bytes(original)
            environment = dict(fixture["environment"])
            environment["FAKE_FAIL_ALTER"] = "1"
            result = subprocess.run(
                _installer_arguments(fixture, "install", "--mode", "sql-only"),
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(auto_conf.read_bytes(), original)
            self.assertIn("restor", (result.stdout + result.stderr).lower())

    def test_recovery_without_post_mutation_digest_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            fixture = _fake_packaged_install(Path(raw_directory))
            staged = _run_installer(fixture, "install", "--mode", "sql-only")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            state_path = _state_path(staged)
            state_file = state_path / "state.tsv"
            state_file.write_text(
                re.sub(
                    r"^config_after_digest\t.*$",
                    "config_after_digest\t",
                    state_file.read_text(),
                    flags=re.MULTILINE,
                )
            )
            Path(fixture["auto_conf"]).write_text("# unknown mutation\n")
            recovery = _run_installer(
                fixture, "recover", "--state-directory", str(state_path)
            )
            self.assertNotEqual(recovery.returncode, 0)
            self.assertIn(
                "mutation outcome was not journaled",
                recovery.stdout + recovery.stderr,
            )
            self.assertEqual(
                Path(fixture["auto_conf"]).read_text(), "# unknown mutation\n"
            )

    def test_staged_sql_only_to_resp_reserves_slots_before_first_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            fixture = _fake_packaged_install(
                directory,
                active_preload="pg_stat_statements",
                configured_preload="pg_stat_statements,pg_local_cache",
                active_max_workers=8,
                configured_max_workers=8,
                configured_port=0,
                configured_workers=4,
            )
            token = directory / "auth-token"
            token.write_text("c" * 48 + "\n", encoding="ascii")
            token.chmod(0o400)
            capture = directory / "alter-arguments"
            environment = dict(fixture["environment"])
            environment["FAKE_CAPTURE_ARGUMENTS"] = str(capture)
            result = subprocess.run(
                _installer_arguments(
                    fixture,
                    "install",
                    "--mode",
                    "resp",
                    "--workers",
                    "6",
                    "--token-file",
                    str(token),
                ),
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("max_workers=14", capture.read_text(encoding="utf-8"))

    def test_fresh_equal_and_upgrade_activation_use_safe_version_sql(self) -> None:
        cases = (
            (None, "fresh", "CREATE EXTENSION pg_local_cache VERSION :'target_version'"),
            ("1.1.0", "already current", "GRANT USAGE ON SCHEMA local_cache"),
            ("1.0.0", "1.0.0 -> 1.1.0", "ALTER EXTENSION pg_local_cache UPDATE TO :'target_version'"),
        )
        for installed_version, expected_status, expected_sql in cases:
            with self.subTest(installed_version=installed_version), tempfile.TemporaryDirectory() as raw_directory:
                fixture = _fake_packaged_install(
                    Path(raw_directory), installed_extension_version=installed_version
                )
                staged = _run_installer(fixture, "install", "--mode", "sql-only")
                self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
                capture = Path(raw_directory) / "activation.sql"
                verified = _run_installer(
                    fixture,
                    "verify",
                    "--state-directory",
                    str(_state_path(staged)),
                    env=_restarted_environment(
                        fixture, FAKE_CAPTURE_ACTIVATION_SQL=str(capture)
                    ),
                )
                self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
                self.assertIn(expected_status, verified.stdout)
                self.assertEqual(
                    Path(fixture["extension_version_state"]).read_text(), "1.1.0"
                )
                if capture.exists():
                    sql = capture.read_text()
                    self.assertIn(expected_sql, sql)
                    if installed_version == "1.1.0":
                        self.assertNotIn("CREATE EXTENSION", sql)
                        self.assertNotIn("ALTER EXTENSION", sql)

    def test_upgrade_preserves_an_existing_extension_owner(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            fixture = _fake_packaged_install(
                Path(raw_directory), installed_extension_version="1.0.0"
            )
            environment = dict(fixture["environment"])
            environment["FAKE_EXTENSION_OWNER_OID"] = "42"
            staged = _run_installer(
                fixture, "install", "--mode", "sql-only", env=environment
            )
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            verified = _run_installer(
                fixture,
                "verify",
                "--state-directory",
                str(_state_path(staged)),
                env=_restarted_environment(
                    fixture, FAKE_EXTENSION_OWNER_OID="42"
                ),
            )
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

    def test_upgrade_without_path_or_with_migration_error_fails_closed(self) -> None:
        for overrides in ({"FAKE_UPDATE_PATH": "0"}, {"FAKE_FAIL_ACTIVATION": "1"}):
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as raw_directory:
                fixture = _fake_packaged_install(
                    Path(raw_directory), installed_extension_version="1.0.0"
                )
                staged = _run_installer(fixture, "install", "--mode", "sql-only")
                self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
                state_path = _state_path(staged)
                failed = _run_installer(
                    fixture,
                    "verify",
                    "--state-directory",
                    str(state_path),
                    env=_restarted_environment(fixture, **overrides),
                )
                self.assertNotEqual(failed.returncode, 0)
                self.assertEqual(
                    Path(fixture["extension_version_state"]).read_text(), "1.0.0"
                )
                self.assertIn(
                    "state\trestart_observed\n", (state_path / "state.tsv").read_text()
                )

    def test_omitted_gucs_are_preserved_with_one_scoped_override(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            fixture = _fake_packaged_install(
                Path(raw_directory),
                active_preload="pg_local_cache",
                configured_preload="pg_local_cache",
                active_port=0,
                configured_port=0,
                configured_bind_address="0.0.0.0",
                configured_workers=7,
                configured_cache_entries=32768,
                configured_relation_states=2048,
                configured_max_clients=777,
                configured_max_clients_per_worker=111,
                configured_memory_budget_mb=768,
                installed_extension_version="1.1.0",
            )
            capture = Path(raw_directory) / "arguments.txt"
            environment = dict(fixture["environment"])
            environment["FAKE_CAPTURE_ARGUMENTS"] = str(capture)
            result = _run_installer(
                fixture, "install", "--cache-entries", "4096", env=environment
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            arguments = capture.read_text()
            for expected in (
                "bind_address=0.0.0.0",
                "cache_port=0",
                "workers=7",
                "cache_entries=4096",
                "relation_states=2048",
                "max_clients=777",
                "max_clients_per_worker=111",
                "memory_budget_mb=768",
            ):
                self.assertIn(expected, arguments)

    def test_identity_and_each_installed_member_are_rechecked_before_ddl(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            fixture = _fake_packaged_install(Path(raw_directory))
            staged = _run_installer(fixture, "install", "--mode", "sql-only")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            state_path = _state_path(staged)
            package = Path(fixture["package"])
            pairs = (
                (Path(fixture["target_lib"]) / "pg_local_cache.so", package / "lib/pg_local_cache.so"),
                (Path(fixture["target_extension"]) / "pg_local_cache.control", package / "share/extension/pg_local_cache.control"),
                (Path(fixture["target_extension"]) / "pg_local_cache--1.1.0.sql", package / "share/extension/pg_local_cache--1.1.0.sql"),
                (Path(fixture["target_extension"]) / "pg_local_cache--1.0.0--1.1.0.sql", package / "share/extension/pg_local_cache--1.0.0--1.1.0.sql"),
            )
            for target, source in pairs:
                with self.subTest(target=target.name):
                    target.write_bytes(target.read_bytes() + b"tampered")
                    failed = _run_installer(
                        fixture,
                        "verify",
                        "--state-directory",
                        str(state_path),
                        env=_restarted_environment(fixture),
                    )
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertFalse(Path(fixture["extension_version_state"]).exists())
                    shutil.copyfile(source, target)
                    target.chmod(0o755 if target.suffix == ".so" else 0o644)

            wrong_build = _run_installer(
                fixture,
                "verify",
                "--state-directory",
                str(state_path),
                env=_restarted_environment(fixture, FAKE_BINARY_BUILD_ID="f" * 40),
            )
            self.assertNotEqual(wrong_build.returncode, 0)
            self.assertIn(
                "active binary identity", wrong_build.stdout + wrong_build.stderr
            )

    def test_activation_commit_reconciles_without_replaying_ddl(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            fixture = _fake_packaged_install(
                Path(raw_directory), installed_extension_version="1.0.0"
            )
            staged = _run_installer(fixture, "install", "--mode", "sql-only")
            self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
            state_path = _state_path(staged)
            crashed = _run_installer(
                fixture,
                "verify",
                "--state-directory",
                str(state_path),
                env=_restarted_environment(
                    fixture, PGLC_INSTALL_CRASH_AT="after_activation_sql_commit"
                ),
            )
            self.assertNotEqual(crashed.returncode, 0)
            self.assertEqual(
                Path(fixture["extension_version_state"]).read_text(), "1.1.0"
            )
            reconciled = _run_installer(
                fixture,
                "verify",
                "--state-directory",
                str(state_path),
                env=_restarted_environment(fixture, FAKE_FAIL_ACTIVATION="1"),
            )
            self.assertEqual(
                reconciled.returncode, 0, reconciled.stdout + reconciled.stderr
            )
            self.assertIn("state\tcomplete\n", (state_path / "state.tsv").read_text())

    def test_missing_default_and_catalog_recreation_fail_before_activation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            fixture = _fake_packaged_install(Path(raw_directory))
            unavailable = _run_installer(
                fixture,
                "install",
                "--mode",
                "sql-only",
                env={**fixture["environment"], "FAKE_DEFAULT_AVAILABLE": "0"},
            )
            self.assertNotEqual(unavailable.returncode, 0)
            self.assertEqual(list(Path(fixture["target_extension"]).iterdir()), [])

        with tempfile.TemporaryDirectory() as raw_directory:
            fixture = _fake_packaged_install(
                Path(raw_directory), installed_extension_version="1.0.0"
            )
            staged = _run_installer(fixture, "install", "--mode", "sql-only")
            state_path = _state_path(staged)
            recreated = _run_installer(
                fixture,
                "verify",
                "--state-directory",
                str(state_path),
                env=_restarted_environment(fixture, FAKE_EXTENSION_OID="73002"),
            )
            self.assertNotEqual(recreated.returncode, 0)
            self.assertEqual(
                Path(fixture["extension_version_state"]).read_text(), "1.0.0"
            )

    def test_verify_without_pending_state_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            fixture = _fake_packaged_install(
                Path(raw_directory),
                active_preload="pg_local_cache",
                configured_preload="pg_local_cache",
                active_port=0,
                configured_port=0,
                installed_extension_version="1.1.0",
            )
            Path(fixture["role_state"]).touch()
            Path(fixture["schema_grant_state"]).touch()
            Path(fixture["mapping_grant_state"]).touch()
            verified = _run_installer(fixture, "verify")
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
            self.assertIn("read-only", verified.stdout)
            self.assertFalse(Path(fixture["state"]).exists())


class FetchReleaseContracts(unittest.TestCase):
    def _tools(self, directory: Path) -> tuple[Path, Path]:
        bin_directory = directory / "bin"
        bin_directory.mkdir()
        trace = directory / "trace"
        trace.touch()
        real_tar = shutil.which("tar")
        if real_tar is None:
            raise unittest.SkipTest("system tar is required")
        _write_executable(
            bin_directory / "pg_config",
            "#!/bin/sh\nprintf 'PostgreSQL %s.0\\n' \"${FETCH_PG_MAJOR:-16}\"\n",
        )
        _write_executable(
            bin_directory / "uname",
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  -s) printf '%s\\n' \"${FETCH_OS:-Linux}\" ;;\n"
            "  -m) printf '%s\\n' \"${FETCH_ARCH:-x86_64}\" ;;\n"
            "esac\n",
        )
        _write_executable(
            bin_directory / "ldd",
            "#!/bin/sh\nprintf '%s\\n' \"${FETCH_LDD:-ldd (GNU libc) 2.36}\"\n",
        )
        _write_executable(
            bin_directory / "tar",
            textwrap.dedent(
                f"""\
                #!/bin/sh
                printf 'tar %s\\n' "$*" >> "$FETCH_TRACE"
                if [ "$1" = --version ]; then
                    printf '%s\\n' "${{FETCH_TAR_VERSION:-tar (GNU tar) 1.35}}"
                    exit 0
                fi
                if [ "$1" = --help ]; then
                    if [ "${{FETCH_TAR_IMPL:-gnu}}" = busybox ]; then
                        printf '%s\\n' 'BusyBox tar c|x|t [-ZzJjahmvokO] [-f TARFILE]'
                    else
                        printf '%s\\n' '--list --extract --gzip --file'
                    fi
                    exit 0
                fi
                exec {real_tar!s} "$@"
                """
            ),
        )
        python = sys.executable
        _write_executable(
            bin_directory / "sha256sum",
            textwrap.dedent(
                f"""\
                #!{python}
                import hashlib, sys
                for name in sys.argv[1:]:
                    print(hashlib.sha256(open(name, 'rb').read()).hexdigest(), ' ', name)
                """
            ),
        )
        _write_executable(
            bin_directory / "curl",
            textwrap.dedent(
                f"""\
                #!{python}
                import os, shutil, signal, sys, time
                trace = os.environ['FETCH_TRACE']
                with open(trace, 'a', encoding='utf-8') as output:
                    output.write('curl ' + ' '.join(sys.argv[1:]) + '\\n')
                if '--version' in sys.argv:
                    print('curl 8.0.0')
                    raise SystemExit(0)
                if os.environ.get('FETCH_INTERRUPT') == '1':
                    os.kill(os.getppid(), signal.SIGTERM)
                    time.sleep(0.2)
                    raise SystemExit(1)
                target = sys.argv[sys.argv.index('--output') + 1]
                url = sys.argv[-1]
                race = os.environ.get('FETCH_RACE_OUTPUT')
                if race and url.endswith('/SHA256SUMS'):
                    os.mkdir(race)
                    open(os.path.join(race, 'sentinel'), 'w').write('keep')
                source = os.environ[
                    'FETCH_MANIFEST' if url.endswith('/SHA256SUMS') else 'FETCH_ARCHIVE'
                ]
                shutil.copyfile(source, target)
                """
            ),
        )
        for forbidden in ("sudo", "psql", "install.sh"):
            _write_executable(
                bin_directory / forbidden,
                "#!/bin/sh\nprintf 'FORBIDDEN %s\\n' \"$0 $*\" >> \"$FETCH_TRACE\"\nexit 99\n",
            )
        return bin_directory, trace

    @staticmethod
    def _archive(
        path: Path,
        *,
        major: int = 16,
        libc: str = "glibc",
        extra: tuple[str, bytes | None, bytes] | None = None,
    ) -> str:
        root = f"pg_local_cache-1.3.0-pg{major}-linux-{libc}-amd64"
        build_id = "a" * 40
        members = {
            f"{root}/install.sh": (0o755, b"#!/bin/sh\nexit 77\n"),
            f"{root}/BUILD-ID": (0o644, f"{build_id}\n".encode()),
            f"{root}/RELEASE-METADATA": (
                0o644,
                (
                    "format=1\nversion=1.3.0\n"
                    f"postgres_major={major}\nos=linux\nlibc={libc}\n"
                    f"architecture=amd64\ncommit={build_id}\nbuild_image=fixture\n"
                ).encode(),
            ),
        }
        with tarfile.open(path, "w:gz") as package:
            root_member = tarfile.TarInfo(root)
            root_member.type = tarfile.DIRTYPE
            root_member.mode = 0o755
            package.addfile(root_member)
            for name, (mode, payload) in members.items():
                member = tarfile.TarInfo(name)
                member.mode = mode
                member.size = len(payload)
                package.addfile(member, io.BytesIO(payload))
            if extra is not None:
                name, member_type, payload = extra
                member = tarfile.TarInfo(name.replace("{root}", root))
                member.mode = 0o644
                member.type = member_type or tarfile.REGTYPE
                member.size = len(payload) if member.isreg() else 0
                member.linkname = "target" if member.islnk() or member.issym() else ""
                package.addfile(member, io.BytesIO(payload) if member.isreg() else None)
        return root

    def _run(
        self,
        directory: Path,
        arguments: list[str],
        **overrides: str,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        bin_directory, trace = self._tools(directory)
        environment = os.environ.copy()
        test_libc = os.environ.get("PGLC_FETCH_TEST_LIBC", "glibc")
        test_tar = os.environ.get("PGLC_FETCH_TEST_TAR_IMPL", "gnu")
        environment.update(
            {
                "PATH": f"{bin_directory}:{os.defpath}",
                "FETCH_TRACE": str(trace),
                "FETCH_LDD": "musl libc" if test_libc == "musl" else "ldd (GNU libc)",
                "FETCH_TAR_IMPL": test_tar,
                "FETCH_TAR_VERSION": (
                    "BusyBox v1.37.0" if test_tar == "busybox" else "tar (GNU tar) 1.35"
                ),
                **overrides,
            }
        )
        return (
            subprocess.run(
                ["bash", str(FETCHER), *arguments],
                cwd=directory,
                env=environment,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ),
            trace,
        )

    def test_dry_run_selects_all_ten_assets_without_network_or_writes(self) -> None:
        for major in range(14, 19):
            for libc, tar_impl in (("glibc", "gnu"), ("musl", "busybox")):
                with self.subTest(major=major, libc=libc), tempfile.TemporaryDirectory() as raw:
                    directory = Path(raw)
                    result, trace = self._run(
                        directory,
                        ["--release-tag", "v1.3.0", "--dry-run"],
                        FETCH_PG_MAJOR=str(major),
                        FETCH_LDD=("musl libc" if libc == "musl" else "ldd (GNU libc)"),
                        FETCH_TAR_IMPL=tar_impl,
                        FETCH_TAR_VERSION=(
                            "BusyBox v1.37.0" if tar_impl == "busybox" else "tar (GNU tar) 1.35"
                        ),
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(
                        f"asset=pg_local_cache-pg{major}-linux-{libc}-amd64",
                        result.stdout,
                    )
                    self.assertIn("dry_run=PASS (no network or writes)", result.stdout)
                    self.assertNotIn("releases/download", trace.read_text())
                    self.assertEqual(list(directory.glob(".pg_local_cache-fetch.*")), [])

    def test_verified_archive_is_extracted_but_never_executed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            libc = os.environ.get("PGLC_FETCH_TEST_LIBC", "glibc")
            archive = directory / "fixture.tar.gz"
            root = self._archive(archive, libc=libc)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            manifest = directory / "manifest"
            manifest.write_text(
                f"{digest}  pg_local_cache-pg16-linux-{libc}-amd64.tar.gz\n",
                encoding="ascii",
            )
            output = directory / "output"
            result, trace = self._run(
                directory,
                [
                    "--release-tag",
                    "v1.3.0",
                    "--output-directory",
                    str(output),
                ],
                FETCH_ARCHIVE=str(archive),
                FETCH_MANIFEST=str(manifest),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((output / "install.sh").is_file())
            self.assertFalse((output / root).exists())
            self.assertIn(f"archive_sha256={digest}", result.stdout)
            self.assertIn(f"extracted_path={output}", result.stdout)
            self.assertIn(f"next_command=sudo {output}/install.sh install", result.stdout)
            self.assertIn("install_executed=no", result.stdout)
            command_trace = trace.read_text()
            self.assertIn("/releases/download/v1.3.0/", command_trace)
            self.assertNotIn("FORBIDDEN", command_trace)

    def test_checksum_and_archive_attacks_leave_no_output(self) -> None:
        libc = os.environ.get("PGLC_FETCH_TEST_LIBC", "glibc")
        selected_asset = f"pg_local_cache-pg16-linux-{libc}-amd64.tar.gz"
        attacks = (
            ("traversal", ("{root}/../escape", tarfile.REGTYPE, b"x")),
            ("absolute", ("/absolute", tarfile.REGTYPE, b"x")),
            ("symlink", ("{root}/link", tarfile.SYMTYPE, b"")),
            ("hardlink", ("{root}/hard", tarfile.LNKTYPE, b"")),
            ("fifo", ("{root}/fifo", tarfile.FIFOTYPE, b"")),
            ("device", ("{root}/device", tarfile.CHRTYPE, b"")),
            ("extra-root", ("other-root/file", tarfile.REGTYPE, b"x")),
            ("checkout", ("{root}/.git/config", tarfile.REGTYPE, b"x")),
        )
        for name, extra in attacks:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                directory = Path(raw)
                archive = directory / "fixture.tar.gz"
                self._archive(archive, libc=libc, extra=extra)
                digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                manifest = directory / "manifest"
                manifest.write_text(
                    f"{digest}  {selected_asset}\n",
                    encoding="ascii",
                )
                output = directory / "output"
                result, _ = self._run(
                    directory,
                    ["--release-tag", "v1.3.0", "--output-directory", str(output)],
                    FETCH_ARCHIVE=str(archive),
                    FETCH_MANIFEST=str(manifest),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())
                self.assertEqual(list(directory.glob(".pg_local_cache-fetch.*")), [])

        for name, rows in (
            ("missing", "0" * 64 + "  another.tar.gz\n"),
            (
                "duplicate",
                ("0" * 64 + f"  {selected_asset}\n") * 2,
            ),
            ("bad", "0" * 64 + f"  {selected_asset}\n"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                directory = Path(raw)
                archive = directory / "fixture.tar.gz"
                self._archive(archive, libc=libc)
                manifest = directory / "manifest"
                manifest.write_text(rows, encoding="ascii")
                output = directory / "output"
                result, _ = self._run(
                    directory,
                    ["--release-tag", "v1.3.0", "--output-directory", str(output)],
                    FETCH_ARCHIVE=str(archive),
                    FETCH_MANIFEST=str(manifest),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())

    def test_unsupported_probes_and_existing_output_fail_before_download(self) -> None:
        cases = (
            ("pg", {"FETCH_PG_MAJOR": "13"}),
            ("os", {"FETCH_OS": "Darwin"}),
            ("arch", {"FETCH_ARCH": "aarch64"}),
            ("libc", {"FETCH_LDD": "unknown libc"}),
            ("tar", {"FETCH_TAR_VERSION": "bsdtar 3.7"}),
        )
        for name, environment in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                directory = Path(raw)
                result, trace = self._run(
                    directory,
                    ["--release-tag", "v1.3.0", "--dry-run"],
                    **environment,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("PGXN/source", result.stderr)
                self.assertNotIn("releases/download", trace.read_text())

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            bin_directory, trace = self._tools(directory)
            (bin_directory / "sha256sum").unlink()
            environment = os.environ.copy()
            environment.update({"PATH": str(bin_directory), "FETCH_TRACE": str(trace)})
            result = subprocess.run(
                ["/bin/bash", str(FETCHER), "--release-tag", "v1.3.0", "--dry-run"],
                cwd=directory,
                env=environment,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("sha256sum was not found", result.stderr)

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            output = directory / "existing"
            output.mkdir()
            result, trace = self._run(
                directory,
                ["--release-tag", "v1.3.0", "--output-directory", str(output)],
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("releases/download", trace.read_text())

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            archive = directory / "fixture.tar.gz"
            self._archive(archive)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            manifest = directory / "manifest"
            manifest.write_text(
                f"{digest}  pg_local_cache-pg16-linux-glibc-amd64.tar.gz\n",
                encoding="ascii",
            )
            output = directory / "raced"
            result, _ = self._run(
                directory,
                ["--release-tag", "v1.3.0", "--output-directory", str(output)],
                FETCH_ARCHIVE=str(archive),
                FETCH_MANIFEST=str(manifest),
                FETCH_RACE_OUTPUT=str(output),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((output / "sentinel").read_text(), "keep")

    def test_tag_boundary_and_interruption_cleanup(self) -> None:
        for tag in ("latest", "master", "v1.3", "v1.3.0-rc1", "https://evil.test/x"):
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as raw:
                directory = Path(raw)
                result = subprocess.run(
                    ["bash", str(FETCHER), "--release-tag", tag, "--dry-run"],
                    cwd=directory,
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.assertNotEqual(result.returncode, 0)

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            output = directory / "output"
            result, _ = self._run(
                directory,
                ["--release-tag", "v1.3.0", "--output-directory", str(output)],
                FETCH_INTERRUPT="1",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())
            self.assertEqual(list(directory.glob(".pg_local_cache-fetch.*")), [])


class ReleaseContracts(unittest.TestCase):
    def test_release_archive_build_inspect_and_identity_are_single_copy(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            stage = directory / "stage"
            root = stage / "release-root"
            root.mkdir(parents=True)
            installer = root / "install.sh"
            installer.write_text("#!/bin/sh\n", encoding="ascii")
            installer.chmod(0o755)
            output = directory / "private" / "release.tar.gz"
            built = subprocess.run(
                [
                    sys.executable,
                    str(ARCHIVE_HELPER),
                    "build",
                    "--stage",
                    str(stage),
                    "--root",
                    root.name,
                    "--output",
                    str(output),
                    "--epoch",
                    "1",
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o444)
            self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)
            extracted = directory / "extracted"
            identity = directory / "identity.json"
            inspected = subprocess.run(
                [
                    sys.executable,
                    str(ARCHIVE_HELPER),
                    "inspect",
                    "--archive",
                    str(output),
                    "--root",
                    root.name,
                    "--extract-dir",
                    str(extracted),
                    "--identity-out",
                    str(identity),
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
            self.assertIn("sha256=", built.stdout)
            self.assertIn("member=file:0755:release-root/install.sh", inspected.stdout)
            self.assertTrue((extracted / root.name / "install.sh").is_file())
            subprocess.run(
                [
                    sys.executable,
                    str(ARCHIVE_HELPER),
                    "verify-identity",
                    "--archive",
                    str(output),
                    "--identity",
                    str(identity),
                ],
                check=True,
            )

    def test_release_archive_rejects_unsafe_headers_before_extraction(self) -> None:
        cases = (
            ("traversal", "release-root/../escape", tarfile.REGTYPE, ""),
            ("absolute", "/release-root/file", tarfile.REGTYPE, ""),
            ("symlink", "release-root/link", tarfile.SYMTYPE, "target"),
            ("hardlink", "release-root/link", tarfile.LNKTYPE, "release-root/file"),
            ("fifo", "release-root/fifo", tarfile.FIFOTYPE, ""),
            ("device", "release-root/device", tarfile.CHRTYPE, ""),
            ("extra-root", "other-root/file", tarfile.REGTYPE, ""),
        )
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            for name, member_name, member_type, linkname in cases:
                with self.subTest(name=name):
                    archive_path = directory / f"{name}.tar.gz"
                    with tarfile.open(archive_path, "w:gz") as archive:
                        root = tarfile.TarInfo("release-root")
                        root.type = tarfile.DIRTYPE
                        archive.addfile(root)
                        member = tarfile.TarInfo(member_name)
                        member.type = member_type
                        member.linkname = linkname
                        archive.addfile(member, io.BytesIO(b"") if member.isreg() else None)
                    extract_dir = directory / f"extract-{name}"
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(ARCHIVE_HELPER),
                            "inspect",
                            "--archive",
                            str(archive_path),
                            "--root",
                            "release-root",
                            "--extract-dir",
                            str(extract_dir),
                        ],
                        check=False,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(extract_dir.exists())

            duplicate = directory / "duplicate.tar.gz"
            with tarfile.open(duplicate, "w:gz") as archive:
                root = tarfile.TarInfo("release-root")
                root.type = tarfile.DIRTYPE
                archive.addfile(root)
                for _ in range(2):
                    member = tarfile.TarInfo("release-root/file")
                    member.size = 1
                    archive.addfile(member, io.BytesIO(b"x"))
            duplicate_extract = directory / "extract-duplicate"
            result = subprocess.run(
                [
                    sys.executable,
                    str(ARCHIVE_HELPER),
                    "inspect",
                    "--archive",
                    str(duplicate),
                    "--root",
                    "release-root",
                    "--extract-dir",
                    str(duplicate_extract),
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(duplicate_extract.exists())

    def test_release_waits_for_successful_master_ci(self) -> None:
        source = RELEASE.read_text(encoding="utf-8")
        self.assertIn('workflows: ["CI"]', source)
        self.assertIn("workflow_run.conclusion == 'success'", source)
        self.assertIn("workflow_run.event == 'push'", source)
        self.assertIn("workflow_run.head_branch == 'master'", source)
        self.assertIn("head_repository.full_name == github.repository", source)

    def test_release_is_downloadable_versioned_and_immutable(self) -> None:
        source = RELEASE.read_text(encoding="utf-8")
        self.assertIn('commit_tag="master-${short_sha}"', source)
        self.assertIn('stable_tag="v${version}"', source)
        self.assertIn("Refusing to move immutable tag", source)
        self.assertIn("refusing overwrite", source)
        self.assertNotIn("--clobber", source)
        self.assertIn("SHA256SUMS", source)
        self.assertIn("retention-days: 90", source)
        self.assertGreaterEqual(source.count("scripts/release_archive.py build"), 2)
        self.assertGreaterEqual(source.count("scripts/release_archive.py inspect"), 2)
        self.assertGreaterEqual(
            source.count("scripts/release_archive.py verify-identity"), 2
        )
        checksums = source.index("- name: Create and verify checksums")
        helper = source.index("install -m 0755 scripts/fetch-release.sh")
        self.assertLess(helper, checksums)
        self.assertIn("dist/fetch-release.sh", source)
        self.assertIn("dist/install-latest.sh", source)
        bootstrap = (ROOT / "scripts" / "install-latest.sh").read_text()
        self.assertIn("releases/latest", bootstrap)
        self.assertIn("sha256sum --check --strict", bootstrap)
        self.assertIn('"$temporary_directory/package/install.sh" install', bootstrap)

    def test_archives_are_inspected_and_smoked_before_upload(self) -> None:
        source = RELEASE.read_text(encoding="utf-8")
        binary_start = source.index("- name: Assemble deterministic binary archive")
        binary_end = source.index("- name: Upload binary lane", binary_start)
        binary = source[binary_start:binary_end]
        self.assertLess(
            binary.index("scripts/release_archive.py build"),
            binary.index("scripts/release_archive.py inspect"),
        )
        self.assertLess(
            binary.index('bash "$extracted_root/install.sh" --help'),
            binary.index("scripts/release_archive.py verify-identity"),
        )
        self.assertIn('docker cp "$extracted_root/."', binary)
        self.assertIn('/artifact/install.sh install --dry-run', binary)
        self.assertNotIn("scripts/install-existing.sh --help", binary)

        package_start = source.index("- name: Validate source and create source archive")
        package_end = source.index(
            "- name: Create and verify checksums", package_start
        )
        package = source[package_start:package_end]
        self.assertIn("sha256sum --check", package)
        self.assertIn('cd "$extracted_root"', package)
        self.assertIn("make verify-static source-test pgxn-check", package)

    def test_release_requires_an_unpublished_prepared_version(self) -> None:
        source = RELEASE.read_text(encoding="utf-8")
        self.assertIn("python3 scripts/auto_version.py --json", source)
        self.assertIn('if [[ "$action" != "release" ]]', source)
        self.assertIn("release_ready=false", source)
        self.assertIn("release_ready=true", source)
        self.assertIn(
            "if: needs.metadata.outputs.release_ready == 'true'", source
        )
        self.assertIn("master advanced", source)
        self.assertIn("refusing version reuse", source)
        self.assertNotIn("--jq .sha 2>/dev/null || true", source)
        self.assertIn('local tag="$1" existing', source)
        self.assertIn('if existing="$(gh api', source)

    def test_release_creation_avoids_cross_api_tag_propagation_race(self) -> None:
        source = RELEASE.read_text(encoding="utf-8")
        publish_start = source.index(
            "- name: Publish immutable commit prerelease"
        )
        publish = source[publish_start:]
        self.assertNotIn("--verify-tag", publish)
        self.assertEqual(publish.count('--target "$RELEASE_SHA"'), 2)
        self.assertIn('create_ref "$COMMIT_TAG"', publish)
        self.assertIn('create_ref "$STABLE_TAG"', publish)
        self.assertIn('gh release create "$COMMIT_TAG"', publish)
        self.assertIn('gh release create "$STABLE_TAG"', publish)

    def test_binary_asset_scope_and_installer_are_explicit(self) -> None:
        workflow = RELEASE.read_text(encoding="utf-8")
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("postgres_major: [14, 15, 16, 17, 18]", workflow)
        self.assertIn("variant: bookworm", workflow)
        self.assertIn("variant: alpine3.23", workflow)
        self.assertIn("libc: glibc", workflow)
        self.assertIn("libc: musl", workflow)
        self.assertIn("architecture=amd64", workflow)
        self.assertIn("scripts/install-existing.sh", workflow)
        self.assertIn("docs/INSTALL_EXISTING.md", workflow)
        self.assertIn("AS extension", dockerfile)
        self.assertIn("FROM extension AS runtime", dockerfile)
        self.assertIn('PGLC_BUILD_ID="$PGLC_BUILD_ID" with_llvm=no clean', dockerfile)
        self.assertIn('--build-arg "PGLC_BUILD_ID=${RELEASE_SHA}"', workflow)
        self.assertIn('printf \'%s\\n\' "$RELEASE_SHA" > "$root/BUILD-ID"', workflow)

    def test_binary_identity_is_shared_and_not_user_configurable(self) -> None:
        header = (ROOT / "src/pg_local_cache.h").read_text()
        core = (ROOT / "src/pg_local_cache.c").read_text()
        worker = (ROOT / "src/pg_local_cache_worker.c").read_text()
        makefile = (ROOT / "Makefile").read_text()
        self.assertIn(f'#define PGLC_VERSION "{CURRENT_VERSION}"', header)
        self.assertNotIn(CURRENT_VERSION, worker)
        for name in ("pg_local_cache.binary_version", "pg_local_cache.binary_build_id"):
            start = core.index(name)
            self.assertIn("PGC_INTERNAL", core[start : start + 600])
            self.assertIn("GUC_DISALLOW_IN_FILE", core[start : start + 600])
        self.assertIn("PGLC_BUILD_ID_RESOLVED", makefile)
        self.assertIn("BUILD-ID", makefile)
        self.assertIn("git status --porcelain", makefile)
        release = RELEASE.read_text(encoding="utf-8")
        self.assertIn(
            'grep -Fqx "#define PGLC_VERSION \\"${version}\\"" src/pg_local_cache.h',
            release,
        )
        self.assertNotIn("pg_local_cache_version:${version}", release)

if __name__ == "__main__":
    unittest.main()
