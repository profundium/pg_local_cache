#!/usr/bin/env python3
"""Contracts for the existing-cluster installer and release pipeline."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-existing.sh"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"
BENCHMARK = ROOT / ".github" / "workflows" / "benchmark.yml"


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
    package_architecture: str = "amd64",
    active_preload: str = "pg_stat_statements",
    configured_preload: str | None = None,
    active_max_workers: int = 8,
    configured_max_workers: int | None = None,
    active_port: int = 0,
    active_workers: int = 4,
    configured_port: int | str | None = None,
    configured_workers: int | str | None = None,
) -> dict[str, Path | str]:
    """Create a binary-release layout and a controllable fake local cluster."""

    if not sys.platform.startswith("linux"):
        raise unittest.SkipTest("installer runtime fixtures require GNU/Linux")

    package_postgres_major = (
        postgres_major
        if package_postgres_major is None
        else package_postgres_major
    )
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
            while (($#)); do
              case "$1" in
                --command|-c) query="$2"; shift 2 ;;
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
            elif [[ "$query" == *'f.error IS NOT NULL'* || "$query" == *'pg_file_settings WHERE error IS NOT NULL'* ]]; then
              printf '0\\n'
            elif [[ "$query" == *'pg_file_settings'* && "$query" == *'shared_preload_libraries'* ]]; then
              printf '%s\\n' "$FAKE_CONFIGURED_PRELOAD"
            elif [[ "$query" == *'pg_file_settings'* && "$query" == *'max_worker_processes'* ]]; then
              printf '%s\\n' "$FAKE_CONFIGURED_MAX_WORKERS"
            elif [[ "$query" == *'pg_file_settings'* && "$query" == *'pg_local_cache.port'* ]]; then
              printf '%s\\n' "$FAKE_CONFIGURED_PORT"
            elif [[ "$query" == *'pg_file_settings'* && "$query" == *'pg_local_cache.workers'* ]]; then
              printf '%s\\n' "$FAKE_CONFIGURED_WORKERS"
            elif [[ "$query" == 'SHOW shared_preload_libraries' ]]; then
              printf '%s\\n' "$FAKE_ACTIVE_PRELOAD"
            elif [[ "$query" == 'SHOW max_worker_processes' ]]; then
              printf '%s\\n' "$FAKE_ACTIVE_MAX_WORKERS"
            elif [[ "$query" == *"current_setting('pg_local_cache.port'"* && "$query" == *"current_setting('pg_local_cache.workers'"* ]]; then
              printf '%s|%s\\n' "$FAKE_ACTIVE_PORT" "$FAKE_ACTIVE_WORKERS"
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
              # Empty output means the dedicated role does not exist yet.
              :
            elif [[ "$query" == *'CREATE ROLE'* || "$query" == *'GRANT CONNECT'* ]]; then
              :
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
            "FAKE_SERVER_VERSION_NUM": f"{postgres_major}0014",
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
            ({"package_architecture": "arm64"}, "binary architecture arm64"),
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


class ReleaseContracts(unittest.TestCase):
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
        self.assertGreaterEqual(source.count("gzip -n -9"), 3)
        self.assertIn('--mtime="@${epoch}"', source)

    def test_missing_release_tags_are_not_treated_as_existing_shas(self) -> None:
        source = RELEASE.read_text(encoding="utf-8")
        self.assertNotIn("--jq .sha 2>/dev/null || true", source)
        self.assertIn('stable_sha=""', source)
        self.assertIn('if resolved_stable_sha="$(\n', source)
        self.assertIn('stable_sha="$resolved_stable_sha"', source)
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
        self.assertIn("docs/MONITORING.md", workflow)
        self.assertIn("AS extension", dockerfile)
        self.assertIn("FROM extension AS runtime", dockerfile)
        self.assertIn('make PG_CONFIG="$pg_config" with_llvm=no clean', dockerfile)

    def test_release_requires_both_exact_commit_benchmark_artifacts(self) -> None:
        source = RELEASE.read_text(encoding="utf-8")
        start = source.index(
            "- name: Preserve benchmark evidence from successful CI"
        )
        end = source.index("- name: Create and verify checksums", start)
        evidence_step = source[start:end]
        self.assertIn("comparison-smoke", evidence_step)
        self.assertIn("sql-only-benchmark-smoke", evidence_step)
        self.assertIn("scripts/validate_benchmark_evidence.py", evidence_step)
        self.assertIn('--revision "$RELEASE_SHA"', evidence_step)
        self.assertIn(
            "--whole ci-evidence/comparison/whole-row.json", evidence_step
        )
        self.assertIn(
            "--sql-only ci-evidence/sql-only/sql-only.json", evidence_step
        )
        self.assertNotIn("comparison.json", evidence_step)
        self.assertNotIn("if ! gh run download", evidence_step)
        self.assertNotIn("artifact was not available", evidence_step)
        self.assertNotIn("if: env.TRIGGER_RUN_ID != ''", evidence_step)
        self.assertNotIn("<<'PY'", evidence_step)

    def test_whole_row_workflows_use_only_active_regression_gates(self) -> None:
        ci = CI.read_text(encoding="utf-8")
        benchmark = BENCHMARK.read_text(encoding="utf-8")
        combined = ci + benchmark
        for obsolete in (
            "PGLC_BENCH_REQUIRE_SINGLE_FLIGHT",
            "PGLC_BENCH_SQL_DIRECT_SETUP",
            "PGLC_BENCH_SQL_FAST_PATH_SETUP",
            "PGLC_BENCH_SQL_MIN_OPS",
            "PGLC_BENCH_MIN_OPS",
        ):
            self.assertNotIn(obsolete, combined)
        self.assertIn('PGLC_BENCH_SINGLEFLIGHT_WAIT_MS: "250"', ci)
        self.assertIn('PGLC_BENCH_ROW_RESP_MIN_OPS: "10000"', ci)
        self.assertIn('PGLC_BENCH_ROW_SQL_MIN_OPS: "10000"', ci)
        self.assertIn('PGLC_BENCH_ROW_WIDTH_MIN_OPS: "0"', ci)


class DocumentationContracts(unittest.TestCase):
    def test_local_markdown_links_resolve_after_readme_split(self) -> None:
        documents = [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]
        failures: list[str] = []
        for document in documents:
            source = document.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^]]*\]\(([^)]+)\)", source):
                if target.startswith(("http://", "https://", "#", "mailto:")):
                    continue
                pages_target = re.fullmatch(
                    r"\{\{ '(/docs/[^']+\.html)' \| relative_url \}\}",
                    target,
                )
                if pages_target:
                    source_path = pages_target.group(1).removeprefix("/")
                    source_path = source_path.removesuffix(".html") + ".md"
                    if not (ROOT / source_path).exists():
                        failures.append(
                            f"{document.relative_to(ROOT)} -> {target}"
                        )
                    continue
                path = target.split("#", 1)[0]
                if path and not (document.parent / path).resolve().exists():
                    failures.append(f"{document.relative_to(ROOT)} -> {target}")
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
