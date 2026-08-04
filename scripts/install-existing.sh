#!/usr/bin/env bash
set -Eeuo pipefail

readonly program_name="$(basename -- "$0")"
readonly script_directory="$(
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
    pwd -P
)"

if [[ -f "${script_directory}/pg_local_cache.control" ||
      -f "${script_directory}/share/extension/pg_local_cache.control" ||
      -f "${script_directory}/RELEASE-METADATA" ]]; then
    readonly release_root="$script_directory"
else
    readonly release_root="$(cd -- "${script_directory}/.." && pwd -P)"
fi

command_name=""
database="${PGDATABASE:-postgres}"
worker_role="local_cache_worker"
postgres_os_user="postgres"
mode="sql-only"
pg_config_bin="${PG_CONFIG:-pg_config}"
psql_bin="${PSQL:-psql}"
restart_method="none"
systemd_unit=""
readiness_timeout=180
restart_goal_seconds=30
state_root="/var/lib/pg_local_cache/install-state"
dry_run=false
force=false

bind_address="127.0.0.1"
resp_port=6380
workers=4
cache_entries=16384
relation_states=1024
max_clients=256
max_clients_per_worker=64
memory_budget_mb=384
token_file=""

temporary_directory=""
state_directory=""
data_directory=""
auto_conf=""
preload_before=""
preload_after=""
max_workers_before=""
max_workers_after=""
extension_was_preloaded=false
extension_was_configured=false
active_preload=""
current_resp_workers=0
configured_resp_workers=0
binary_release_version=""

usage() {
    cat <<'EOF'
Install pg_local_cache into an existing local PostgreSQL 14-18 cluster.

Usage:
  install-existing.sh preflight [options]
  install-existing.sh install   [options]
  install-existing.sh verify    [options]

Safe defaults:
  * SQL-only mode (no RESP listener or token)
  * no PostgreSQL restart unless explicitly requested
  * existing shared_preload_libraries is preserved

Connection and target:
  --database NAME               database served by pg_local_cache
  --worker-role NAME            dedicated PostgreSQL role
  --postgres-os-user NAME       operating-system postmaster owner
  --pg-config PATH              pg_config for target PostgreSQL 14-18
  --psql PATH                   psql client

Mode and sizing:
  --mode sql-only|resp
  --bind-address 127.0.0.1|0.0.0.0
  --port N
  --workers N
  --cache-entries N
  --relation-states N
  --max-clients N
  --max-clients-per-worker N
  --memory-budget-mb N
  --token-file ABSOLUTE_PATH    required for RESP mode; owner must be postgres

Restart (install defaults to none):
  --restart-method none|systemd|pg_ctl
  --systemd-unit UNIT           required for systemd restart
  --readiness-timeout SECONDS   wait for readiness; default 180
  --restart-goal-seconds N      report whether the target was met; default 30

Safety:
  --state-root DIRECTORY        online backup/state location
  --dry-run                     validate and print plan; mutate nothing
  --force                       replace different existing extension files
  -h, --help

Examples:
  sudo ./install-existing.sh preflight --database app --mode sql-only
  sudo ./install-existing.sh install --database app --mode sql-only
  sudo ./install-existing.sh install --database app --mode sql-only \
    --restart-method systemd --systemd-unit postgresql@18-main

The first installation cannot be activated with pg_reload_conf(): PostgreSQL
must start pg_local_cache through shared_preload_libraries. For Patroni,
Kubernetes operators and managed PostgreSQL, follow docs/INSTALL_EXISTING.md
instead of asking this standalone installer to own the restart.
EOF
}

log() {
    printf '%s: %s\n' "$program_name" "$*"
}

warn() {
    printf '%s: warning: %s\n' "$program_name" "$*" >&2
}

fail() {
    printf '%s: error: %s\n' "$program_name" "$*" >&2
    exit 1
}

cleanup() {
    if [[ -n "$temporary_directory" && -d "$temporary_directory" ]]; then
        rm -rf -- "$temporary_directory"
    fi
}
trap cleanup EXIT

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

require_integer_between() {
    local name="$1"
    local value="$2"
    local minimum="$3"
    local maximum="$4"
    [[ "$value" =~ ^[0-9]+$ ]] || fail "$name must be an integer"
    ((value >= minimum && value <= maximum)) \
        || fail "$name must be between $minimum and $maximum"
}

require_identifier() {
    local name="$1"
    local value="$2"
    [[ "$value" =~ ^[A-Za-z_][A-Za-z0-9_\$]{0,62}$ ]] \
        || fail "$name must be an unquoted PostgreSQL identifier"
}

detect_linux_architecture() {
    local machine
    machine="$(uname -m)"
    case "$machine" in
        x86_64 | amd64) printf 'amd64\n' ;;
        *) printf '%s\n' "$machine" ;;
    esac
}

detect_linux_libc() {
    local reported
    if command -v getconf >/dev/null 2>&1 &&
       reported="$(getconf GNU_LIBC_VERSION 2>/dev/null)" &&
       [[ "$reported" == glibc\ * ]]; then
        printf 'glibc\n'
        return
    fi
    reported="$(ldd --version 2>&1 || true)"
    if [[ "${reported,,}" == *musl* ]]; then
        printf 'musl\n'
        return
    fi
    fail "could not identify target libc as glibc or musl"
}

validate_binary_release() {
    local target_major="$1"
    local metadata="${release_root}/RELEASE-METADATA"
    local packaged_lib="${release_root}/lib/pg_local_cache.so"
    local key value seen='|'
    local format='' version='' postgres_major='' os='' libc=''
    local architecture='' commit='' build_image=''

    if [[ ! -f "$metadata" ]]; then
        [[ ! -f "$packaged_lib" ]] \
            || fail "binary release is missing RELEASE-METADATA"
        return
    fi
    [[ -f "$packaged_lib" ]] \
        || fail "RELEASE-METADATA exists but lib/pg_local_cache.so is missing"

    while IFS='=' read -r key value || [[ -n "$key$value" ]]; do
        [[ -n "$key" && "$key" != \#* && "$key" != *[[:space:]]* ]] \
            || fail "RELEASE-METADATA contains a malformed key"
        [[ "$seen" != *"|${key}|"* ]] \
            || fail "RELEASE-METADATA repeats key: $key"
        seen+="${key}|"
        case "$key" in
            format) format="$value" ;;
            version) version="$value" ;;
            postgres_major) postgres_major="$value" ;;
            os) os="$value" ;;
            libc) libc="$value" ;;
            architecture) architecture="$value" ;;
            commit) commit="$value" ;;
            build_image) build_image="$value" ;;
            *) fail "RELEASE-METADATA contains unknown key: $key" ;;
        esac
    done < "$metadata"

    [[ "$format" == "1" ]] || fail "binary release metadata format must be 1"
    [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
        || fail "binary release metadata has an invalid version"
    [[ "$postgres_major" == "$target_major" ]] \
        || fail "binary targets PostgreSQL $postgres_major, but pg_config targets $target_major"
    [[ "$os" == "linux" && "$(uname -s)" == "Linux" ]] \
        || fail "binary targets $os, but this installer supports it only on Linux"
    [[ "$architecture" == "$(detect_linux_architecture)" ]] \
        || fail "binary architecture $architecture does not match $(detect_linux_architecture)"
    [[ "$libc" == "$(detect_linux_libc)" ]] \
        || fail "binary libc $libc does not match $(detect_linux_libc)"
    [[ "$commit" =~ ^[0-9a-f]{40}$ ]] \
        || fail "binary release metadata has an invalid commit"
    [[ -n "$build_image" ]] \
        || fail "binary release metadata is missing build_image"
    binary_release_version="$version"
}

run_as_postgres() {
    if ((EUID == 0)) && [[ "$postgres_os_user" != "root" ]]; then
        runuser --preserve-environment -u "$postgres_os_user" -- "$@"
    else
        "$@"
    fi
}

psql_base() {
    run_as_postgres "$psql_bin" \
        --no-psqlrc \
        --set ON_ERROR_STOP=1 \
        --dbname "$database" \
        "$@"
}

psql_scalar() {
    psql_base --quiet --tuples-only --no-align --command "$1"
}

configuration_error_count() {
    psql_scalar "SELECT count(*) FROM pg_catalog.pg_file_settings AS f LEFT JOIN pg_catalog.pg_settings AS s ON s.name = f.name WHERE f.error IS NOT NULL AND NOT pg_catalog.coalesce(s.context = 'postmaster' AND s.pending_restart, false)"
}

preload_contains_extension() {
    [[ ",${1//[[:space:]\"]/}," == *,pg_local_cache,* ]]
}

parse_arguments() {
    (($# > 0)) || {
        usage
        exit 2
    }
    command_name="$1"
    shift
    case "$command_name" in
        preflight | install | verify) ;;
        -h | --help)
            usage
            exit 0
            ;;
        *) fail "unknown command: $command_name" ;;
    esac

    while (($# > 0)); do
        case "$1" in
            --database) database="${2:?missing value for --database}"; shift 2 ;;
            --worker-role) worker_role="${2:?missing value for --worker-role}"; shift 2 ;;
            --postgres-os-user) postgres_os_user="${2:?missing value for --postgres-os-user}"; shift 2 ;;
            --pg-config) pg_config_bin="${2:?missing value for --pg-config}"; shift 2 ;;
            --psql) psql_bin="${2:?missing value for --psql}"; shift 2 ;;
            --mode) mode="${2:?missing value for --mode}"; shift 2 ;;
            --bind-address) bind_address="${2:?missing value for --bind-address}"; shift 2 ;;
            --port) resp_port="${2:?missing value for --port}"; shift 2 ;;
            --workers) workers="${2:?missing value for --workers}"; shift 2 ;;
            --cache-entries) cache_entries="${2:?missing value for --cache-entries}"; shift 2 ;;
            --relation-states) relation_states="${2:?missing value for --relation-states}"; shift 2 ;;
            --max-clients) max_clients="${2:?missing value for --max-clients}"; shift 2 ;;
            --max-clients-per-worker) max_clients_per_worker="${2:?missing value for --max-clients-per-worker}"; shift 2 ;;
            --memory-budget-mb) memory_budget_mb="${2:?missing value for --memory-budget-mb}"; shift 2 ;;
            --token-file) token_file="${2:?missing value for --token-file}"; shift 2 ;;
            --restart-method) restart_method="${2:?missing value for --restart-method}"; shift 2 ;;
            --systemd-unit) systemd_unit="${2:?missing value for --systemd-unit}"; shift 2 ;;
            --readiness-timeout) readiness_timeout="${2:?missing value for --readiness-timeout}"; shift 2 ;;
            --restart-goal-seconds) restart_goal_seconds="${2:?missing value for --restart-goal-seconds}"; shift 2 ;;
            --state-root) state_root="${2:?missing value for --state-root}"; shift 2 ;;
            --dry-run) dry_run=true; shift ;;
            --force) force=true; shift ;;
            -h | --help) usage; exit 0 ;;
            *) fail "unknown option: $1" ;;
        esac
    done
}

validate_options() {
    require_identifier "--database" "$database"
    require_identifier "--worker-role" "$worker_role"
    require_identifier "--postgres-os-user" "$postgres_os_user"
    [[ ! "$worker_role" =~ ^[Pp][Gg]_ ]] \
        || fail "--worker-role must not use PostgreSQL-reserved prefix pg_"
    [[ "$mode" == "sql-only" || "$mode" == "resp" ]] \
        || fail "--mode must be sql-only or resp"
    [[ "$bind_address" == "127.0.0.1" || "$bind_address" == "0.0.0.0" ]] \
        || fail "--bind-address must be 127.0.0.1 or 0.0.0.0"
    [[ "$restart_method" == "none" || "$restart_method" == "systemd" || "$restart_method" == "pg_ctl" ]] \
        || fail "--restart-method must be none, systemd or pg_ctl"
    if [[ "$restart_method" == "systemd" ]]; then
        [[ -n "$systemd_unit" ]] \
            || fail "--systemd-unit is required for a systemd restart"
        [[ "$systemd_unit" =~ ^[A-Za-z0-9_.@:-]+$ ]] \
            || fail "--systemd-unit contains unsupported characters"
    fi
    [[ "$state_root" == /* ]] || fail "--state-root must be absolute"

    require_integer_between "--port" "$resp_port" 1 65535
    require_integer_between "--workers" "$workers" 1 32
    require_integer_between "--cache-entries" "$cache_entries" 128 65536
    require_integer_between "--relation-states" "$relation_states" 128 8192
    require_integer_between "--max-clients" "$max_clients" 1 4096
    require_integer_between "--max-clients-per-worker" "$max_clients_per_worker" 1 128
    require_integer_between "--memory-budget-mb" "$memory_budget_mb" 64 8192
    require_integer_between "--readiness-timeout" "$readiness_timeout" 30 3600
    require_integer_between "--restart-goal-seconds" "$restart_goal_seconds" 1 3600

    if [[ "$mode" == "resp" ]]; then
        ((max_clients <= workers * max_clients_per_worker)) \
            || fail "max clients must not exceed workers x clients per worker"
        [[ "$token_file" == /* ]] \
            || fail "--token-file must be absolute in RESP mode"
    fi
}

validate_token_file() {
    [[ "$mode" == "resp" ]] || return 0
    [[ -f "$token_file" && ! -L "$token_file" ]] \
        || fail "RESP token must be a regular, non-symlink file: $token_file"
    local owner mode_value token
    owner="$(stat -c '%U' "$token_file")"
    mode_value="$(stat -c '%a' "$token_file")"
    [[ "$owner" == "$postgres_os_user" ]] \
        || fail "RESP token owner must be $postgres_os_user (actual: $owner)"
    [[ "$mode_value" == "400" || "$mode_value" == "600" ]] \
        || fail "RESP token mode must be exactly 0400 or 0600 (actual: $mode_value)"
    run_as_postgres test -r "$token_file" \
        || fail "RESP token is not readable by $postgres_os_user"
    token="$(tr -d '\r\n' < "$token_file")"
    [[ "$token" =~ ^[A-Za-z0-9_-]{32,256}$ ]] \
        || fail "RESP token must contain 32-256 base64url characters"
}

preflight() {
    require_command "$pg_config_bin"
    require_command "$psql_bin"
    if ((EUID == 0)) && [[ "$postgres_os_user" != "root" ]]; then
        require_command runuser
    fi
    id "$postgres_os_user" >/dev/null 2>&1 \
        || fail "operating-system user does not exist: $postgres_os_user"

    local local_version local_major server_version server_major
    local is_superuser is_primary
    local local_pkglib local_share server_pkglib server_share config_errors
    local configured_preload configured_max_workers active_port active_workers
    local configured_port configured_workers reserved_resp_workers

    local_version="$($pg_config_bin --version)"
    [[ "$local_version" =~ ^PostgreSQL[[:space:]]+(14|15|16|17|18)\. ]] \
        || fail "pg_config must target PostgreSQL 14-18 (actual: $local_version)"
    local_major="${BASH_REMATCH[1]}"

    server_version="$(psql_scalar "SHOW server_version_num")"
    [[ "$server_version" =~ ^(14|15|16|17|18)[0-9]{4}$ ]] \
        || fail "server must be PostgreSQL 14-18 (server_version_num=$server_version)"
    server_major="${BASH_REMATCH[1]}"
    [[ "$server_major" == "$local_major" ]] \
        || fail "pg_config PostgreSQL $local_major does not match server PostgreSQL $server_major"
    validate_binary_release "$local_major"
    is_superuser="$(psql_scalar "SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = CURRENT_USER")"
    [[ "$is_superuser" == "t" ]] \
        || fail "installer connection must use a PostgreSQL superuser"
    is_primary="$(psql_scalar "SELECT NOT pg_catalog.pg_is_in_recovery()")"
    [[ "$is_primary" == "t" ]] \
        || fail "standalone installer refuses a recovery/standby server; use the HA guide"

    local_pkglib="$($pg_config_bin --pkglibdir)"
    local_share="$($pg_config_bin --sharedir)"
    server_pkglib="$(psql_scalar "SELECT setting FROM pg_catalog.pg_config WHERE name = 'PKGLIBDIR'")"
    server_share="$(psql_scalar "SELECT setting FROM pg_catalog.pg_config WHERE name = 'SHAREDIR'")"
    [[ "$local_pkglib" == "$server_pkglib" && "$local_share" == "$server_share" ]] \
        || fail "local pg_config paths do not match the connected server; run this on the database host"
    [[ -d "$local_pkglib" && -d "$local_share/extension" ]] \
        || fail "PostgreSQL extension directories do not exist locally"

    data_directory="$(psql_scalar "SHOW data_directory")"
    [[ "$data_directory" == /* && -d "$data_directory" ]] \
        || fail "server data_directory is not a local directory: $data_directory"
    auto_conf="${data_directory}/postgresql.auto.conf"
    [[ -f "$auto_conf" ]] \
        || fail "postgresql.auto.conf is missing: $auto_conf"

    config_errors="$(configuration_error_count)"
    [[ "$config_errors" == "0" ]] \
        || fail "pg_file_settings already reports $config_errors configuration error(s)"

    active_preload="$(psql_scalar "SHOW shared_preload_libraries")"
    configured_preload="$(psql_scalar "SELECT pg_catalog.coalesce((SELECT setting FROM pg_catalog.pg_file_settings WHERE name = 'shared_preload_libraries' ORDER BY seqno DESC LIMIT 1), pg_catalog.current_setting('shared_preload_libraries'))")"
    configured_max_workers="$(psql_scalar "SELECT pg_catalog.coalesce((SELECT setting FROM pg_catalog.pg_file_settings WHERE name = 'max_worker_processes' ORDER BY seqno DESC LIMIT 1), pg_catalog.current_setting('max_worker_processes'))")"
    [[ "$configured_max_workers" =~ ^[0-9]+$ ]] \
        || fail "effective max_worker_processes is not numeric: $configured_max_workers"
    preload_before="$configured_preload"
    max_workers_before="$configured_max_workers"
    extension_was_preloaded=false
    extension_was_configured=false
    current_resp_workers=0
    configured_resp_workers=0
    if preload_contains_extension "$active_preload"; then
        extension_was_preloaded=true
        active_port="$(psql_scalar "SHOW pg_local_cache.port")"
        active_workers="$(psql_scalar "SHOW pg_local_cache.workers")"
        [[ "$active_port" =~ ^[0-9]+$ && "$active_workers" =~ ^[0-9]+$ ]] \
            || fail "active pg_local_cache port/workers settings are invalid"
        if ((active_port != 0)); then
            current_resp_workers="$active_workers"
        fi
    fi
    if preload_contains_extension "$preload_before"; then
        extension_was_configured=true
        preload_after="$preload_before"
    elif [[ -n "$preload_before" ]]; then
        preload_after="${preload_before},pg_local_cache"
    else
        preload_after="pg_local_cache"
    fi

    if [[ "$extension_was_configured" == true ]]; then
        configured_port="$(psql_scalar "SELECT pg_catalog.coalesce((SELECT setting FROM pg_catalog.pg_file_settings WHERE name = 'pg_local_cache.port' ORDER BY seqno DESC LIMIT 1), '')")"
        configured_workers="$(psql_scalar "SELECT pg_catalog.coalesce((SELECT setting FROM pg_catalog.pg_file_settings WHERE name = 'pg_local_cache.workers' ORDER BY seqno DESC LIMIT 1), '')")"
        if [[ -z "$configured_port" && "$extension_was_preloaded" == true ]]; then
            configured_port="$active_port"
        fi
        if [[ -z "$configured_workers" && "$extension_was_preloaded" == true ]]; then
            configured_workers="$active_workers"
        fi
        if [[ -n "$configured_port" || -n "$configured_workers" ]]; then
            [[ "$configured_port" =~ ^[0-9]+$ && "$configured_workers" =~ ^[0-9]+$ ]] \
                || fail "staged pg_local_cache port/workers settings are incomplete or invalid"
            if ((configured_port != 0)); then
                configured_resp_workers="$configured_workers"
            fi
        elif [[ "$extension_was_preloaded" == false && "$mode" == "resp" ]]; then
            fail "pg_local_cache is staged but inactive without explicit port/workers; cannot reserve RESP worker slots safely"
        fi
    fi

    max_workers_after="$max_workers_before"
    if [[ "$mode" == "resp" ]]; then
        reserved_resp_workers="$current_resp_workers"
        if ((configured_resp_workers > reserved_resp_workers)); then
            reserved_resp_workers="$configured_resp_workers"
        fi
        if ((workers > reserved_resp_workers)); then
            max_workers_after=$((max_workers_before + workers - reserved_resp_workers))
        fi
    fi

    validate_token_file

    log "preflight PASS"
    log "PostgreSQL: $local_version; database: $database; data: $data_directory"
    log "mode: $mode; shared_preload_libraries: '${preload_before}' -> '${preload_after}'"
    if [[ "$max_workers_after" != "$max_workers_before" ]]; then
        log "max_worker_processes: $max_workers_before -> $max_workers_after"
    fi
    if [[ "$restart_method" == "none" ]]; then
        log "restart: staged only; operator restart remains required"
    else
        log "restart: $restart_method; readiness timeout ${readiness_timeout}s; goal ${restart_goal_seconds}s"
    fi
}

create_state_backup() {
    if [[ "$dry_run" == true ]]; then
        log "dry-run: would create an installation state backup under $state_root"
        return
    fi
    install -d -m 0700 "$state_root"
    state_directory="$(mktemp -d "${state_root}/install-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")"
    chmod 0700 "$state_directory"
    cp --preserve=mode,ownership,timestamps \
        "$auto_conf" "$state_directory/postgresql.auto.conf.before"
    {
        printf 'database\t%s\n' "$database"
        printf 'worker_role\t%s\n' "$worker_role"
        printf 'mode\t%s\n' "$mode"
        printf 'preload_before\t%s\n' "$preload_before"
        printf 'preload_written\t%s\n' "$preload_after"
        printf 'max_worker_processes_before\t%s\n' "$max_workers_before"
        printf 'max_worker_processes_written\t%s\n' "$max_workers_after"
        printf 'data_directory\t%s\n' "$data_directory"
        printf 'created_at_utc\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$state_directory/metadata.tsv"
    chmod 0600 "$state_directory/metadata.tsv"
    log "state backup: $state_directory"
}

locate_release_files() {
    local packaged_lib packaged_share stage
    packaged_lib="${release_root}/lib/pg_local_cache.so"
    packaged_share="${release_root}/share/extension"
    if [[ -f "$packaged_lib" && -f "$packaged_share/pg_local_cache.control" ]]; then
        printf '%s\n%s\n' "$packaged_lib" "$packaged_share"
        return
    fi

    [[ -f "$release_root/Makefile" && -d "$release_root/src" ]] \
        || fail "release contains neither compatible binary files nor buildable source"
    require_command make
    temporary_directory="$(mktemp -d -t pg_local_cache_install.XXXXXX)"
    stage="${temporary_directory}/stage"
    log "building extension against $pg_config_bin" >&2
    make -C "$release_root" -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf 1)" \
        PG_CONFIG="$pg_config_bin" >&2
    make -C "$release_root" PG_CONFIG="$pg_config_bin" DESTDIR="$stage" install >&2
    packaged_lib="${stage}$($pg_config_bin --pkglibdir)/pg_local_cache.so"
    packaged_share="${stage}$($pg_config_bin --sharedir)/extension"
    [[ -f "$packaged_lib" && -f "$packaged_share/pg_local_cache.control" ]] \
        || fail "PGXS build did not produce the expected extension files"
    printf '%s\n%s\n' "$packaged_lib" "$packaged_share"
}

backup_and_install_file() {
    local source="$1"
    local target="$2"
    local mode_value="$3"
    local label="$4"
    if [[ -f "$target" ]]; then
        if cmp --silent "$source" "$target"; then
            log "$label is already current: $target"
            return
        fi
        [[ "$force" == true ]] \
            || fail "$target differs; use --force only for an intentional upgrade"
        install -m "$mode_value" -p "$target" "$state_directory/${label}.before"
        printf 'replaced\n' > "$state_directory/${label}.state"
    else
        printf 'created\n' > "$state_directory/${label}.state"
    fi
    local temporary_target="${target}.pg_local_cache.$$"
    install -m "$mode_value" "$source" "$temporary_target"
    mv -f -- "$temporary_target" "$target"
    log "installed $target"
}

install_extension_files() {
    if [[ "$dry_run" == true ]]; then
        log "dry-run: would build/stage .so, control and SQL through target pg_config"
        return
    fi
    local paths source_lib source_share target_lib target_share version
    local source_sql sql_name sql_label sql_count=0
    mapfile -t paths < <(locate_release_files)
    source_lib="${paths[0]}"
    source_share="${paths[1]}"
    target_lib="$($pg_config_bin --pkglibdir)/pg_local_cache.so"
    target_share="$($pg_config_bin --sharedir)/extension"
    version="$(sed -n "s/^default_version = '\([^']*\)'$/\1/p" "$source_share/pg_local_cache.control")"
    [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
        || fail "control file has an invalid default_version"
    if [[ -n "$binary_release_version" ]]; then
        [[ "$version" == "$binary_release_version" ]] \
            || fail "binary metadata version $binary_release_version does not match control version $version"
    fi
    [[ -f "$source_share/pg_local_cache--${version}.sql" ]] \
        || fail "release SQL file is missing for version $version"

    if command -v ldd >/dev/null 2>&1; then
        if ldd "$source_lib" | grep -Fq 'not found'; then
            ldd "$source_lib" >&2 || true
            fail "extension binary has unresolved runtime libraries"
        fi
    fi
    backup_and_install_file "$source_lib" "$target_lib" 0755 extension_so
    backup_and_install_file "$source_share/pg_local_cache.control" \
        "$target_share/pg_local_cache.control" 0644 extension_control
    for source_sql in "$source_share"/pg_local_cache--*.sql; do
        [[ -f "$source_sql" ]] || continue
        sql_name="${source_sql##*/}"
        [[ "$sql_name" =~ ^pg_local_cache--[0-9]+\.[0-9]+\.[0-9]+(--[0-9]+\.[0-9]+\.[0-9]+)?\.sql$ ]] \
            || fail "release contains an invalid extension SQL filename: $sql_name"
        sql_label="extension_sql_${sql_name//[^A-Za-z0-9]/_}"
        backup_and_install_file "$source_sql" \
            "$target_share/$sql_name" 0644 "$sql_label"
        sql_count=$((sql_count + 1))
    done
    ((sql_count > 0)) || fail "release contains no extension SQL files"
}

ensure_worker_role() {
    if [[ "$dry_run" == true ]]; then
        log "dry-run: would validate/create PostgreSQL role $worker_role"
        return
    fi
    local role_state
    role_state="$(psql_base --quiet --tuples-only --no-align \
        --set worker_role="$worker_role" <<'SQL'
SELECT pg_catalog.concat_ws('|', rolcanlogin, rolsuper, rolinherit,
                            rolcreatedb, rolcreaterole, rolreplication,
                            rolbypassrls)
  FROM pg_catalog.pg_roles
 WHERE rolname = :'worker_role';
SQL
)"
    if [[ -n "$role_state" && "$role_state" != "true|false|false|false|false|false|false" && "$role_state" != "t|f|f|f|f|f|f" ]]; then
        fail "existing role $worker_role is not an isolated LOGIN/NOSUPERUSER/NOINHERIT role"
    fi
    psql_base --quiet \
        --set worker_role="$worker_role" \
        --set cache_database="$database" <<'SQL'
SELECT pg_catalog.format(
    'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD NULL',
    :'worker_role'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = :'worker_role'
)
\gexec
GRANT CONNECT ON DATABASE :"cache_database" TO :"worker_role";
SQL
}

write_configuration() {
    if [[ "$dry_run" == true ]]; then
        log "dry-run: would append pg_local_cache without replacing existing preload libraries"
        return
    fi
    local configured_port="$resp_port"
    local configured_token="$token_file"
    if [[ "$mode" == "sql-only" ]]; then
        configured_port=0
        configured_token=""
    fi
    if ! psql_base --quiet \
        --set preload="$preload_after" \
        --set max_workers="$max_workers_after" \
        --set cache_database="$database" \
        --set worker_role="$worker_role" \
        --set bind_address="$bind_address" \
        --set cache_port="$configured_port" \
        --set workers="$workers" \
        --set cache_entries="$cache_entries" \
        --set relation_states="$relation_states" \
        --set max_clients="$max_clients" \
        --set max_clients_per_worker="$max_clients_per_worker" \
        --set memory_budget_mb="$memory_budget_mb" \
        --set token_file="$configured_token" <<'SQL'
ALTER SYSTEM SET shared_preload_libraries = :'preload';
ALTER SYSTEM SET max_worker_processes = :'max_workers';
ALTER SYSTEM SET pg_local_cache.database = :'cache_database';
ALTER SYSTEM SET pg_local_cache.role = :'worker_role';
ALTER SYSTEM SET pg_local_cache.bind_address = :'bind_address';
ALTER SYSTEM SET pg_local_cache.port = :'cache_port';
ALTER SYSTEM SET pg_local_cache.workers = :'workers';
ALTER SYSTEM SET pg_local_cache.cache_entries = :'cache_entries';
ALTER SYSTEM SET pg_local_cache.relation_states = :'relation_states';
ALTER SYSTEM SET pg_local_cache.max_clients = :'max_clients';
ALTER SYSTEM SET pg_local_cache.max_clients_per_worker = :'max_clients_per_worker';
ALTER SYSTEM SET pg_local_cache.memory_budget_mb = :'memory_budget_mb';
ALTER SYSTEM SET pg_local_cache.auth_token_file = :'token_file';
ALTER SYSTEM RESET pg_local_cache.auth_token;
SELECT pg_catalog.pg_reload_conf();
SQL
    then
        warn "ALTER SYSTEM failed; restoring the exact postgresql.auto.conf backup"
        restore_auto_conf
        psql_scalar "SELECT pg_catalog.pg_reload_conf()" >/dev/null 2>&1 || true
        fail "configuration staging failed; previous auto.conf restored"
    fi
    local config_errors
    config_errors="$(configuration_error_count)"
    if [[ "$config_errors" != "0" ]]; then
        warn "new configuration has $config_errors error(s); restoring postgresql.auto.conf"
        restore_auto_conf
        psql_scalar "SELECT pg_catalog.pg_reload_conf()" >/dev/null
        fail "configuration validation failed; previous auto.conf restored"
    fi
    log "configuration staged and parsed successfully; pending restart is expected"
}

restore_auto_conf() {
    local temporary_auto_conf="${auto_conf}.pg_local_cache_restore.$$"
    cp --preserve=mode,ownership,timestamps \
        "$state_directory/postgresql.auto.conf.before" "$temporary_auto_conf"
    mv -f -- "$temporary_auto_conf" "$auto_conf"
}

raw_restart() {
    case "$restart_method" in
        systemd)
            require_command systemctl
            systemctl restart "$systemd_unit"
            ;;
        pg_ctl)
            local pg_ctl_bin
            pg_ctl_bin="$($pg_config_bin --bindir)/pg_ctl"
            [[ -x "$pg_ctl_bin" ]] || fail "pg_ctl is not executable: $pg_ctl_bin"
            run_as_postgres "$pg_ctl_bin" -D "$data_directory" \
                -m fast -w -t "$readiness_timeout" restart
            ;;
        none) return 0 ;;
    esac
}

wait_until_ready() {
    local deadline=$((SECONDS + readiness_timeout))
    until psql_scalar "SELECT 1" >/dev/null 2>&1; do
        ((SECONDS < deadline)) || return 1
        sleep 1
    done
}

postmaster_is_running() {
    local pg_ctl_bin
    pg_ctl_bin="$($pg_config_bin --bindir)/pg_ctl"
    [[ -x "$pg_ctl_bin" ]] || return 1
    run_as_postgres "$pg_ctl_bin" -D "$data_directory" status \
        >/dev/null 2>&1
}

rollback_stopped_postmaster() {
    local reason="$1"
    warn "$reason; postmaster is stopped, restoring exact postgresql.auto.conf backup"
    restore_auto_conf
    raw_restart || fail "rollback restart also failed; inspect PostgreSQL logs"
    wait_until_ready \
        || fail "rollback configuration started but PostgreSQL did not become SQL-ready"
    fail "new configuration was rolled back after a failed restart"
}

restart_or_restore() {
    [[ "$restart_method" != "none" ]] || return 0
    if [[ "$dry_run" == true ]]; then
        log "dry-run: would perform one $restart_method restart"
        return
    fi
    local started elapsed
    started="$SECONDS"
    if ! raw_restart; then
        if postmaster_is_running; then
            fail "restart command failed but postmaster is still running or recovering; automatic rollback was not attempted (state backup: $state_directory)"
        fi
        rollback_stopped_postmaster "restart command failed"
    fi
    if ! wait_until_ready; then
        if postmaster_is_running; then
            fail "PostgreSQL is running but did not become SQL-ready within ${readiness_timeout}s; it may still be recovering, so automatic rollback was not attempted (state backup: $state_directory)"
        fi
        rollback_stopped_postmaster "SQL readiness timed out"
    fi
    elapsed=$((SECONDS - started))
    log "PostgreSQL became SQL-ready after ${elapsed}s"
    if ((elapsed > restart_goal_seconds)); then
        warn "restart exceeded the ${restart_goal_seconds}s operational goal"
    else
        log "restart met the ${restart_goal_seconds}s operational goal"
    fi
}

activate_and_verify() {
    if [[ "$dry_run" == true ]]; then
        log "dry-run: would CREATE EXTENSION and verify health"
        return
    fi
    local active_preload health_ready state
    active_preload="$(psql_scalar "SHOW shared_preload_libraries")"
    preload_contains_extension "$active_preload" \
        || fail "pg_local_cache is configured but not active; restart PostgreSQL first"
    psql_base --quiet \
        --set worker_role="$worker_role" <<'SQL'
CREATE EXTENSION IF NOT EXISTS pg_local_cache;
GRANT USAGE ON SCHEMA local_cache TO :"worker_role";
GRANT SELECT ON TABLE local_cache.mapping TO :"worker_role";
SQL
    health_ready="$(psql_scalar "SELECT (local_cache.health() ->> 'ready')::boolean")"
    [[ "$health_ready" == "t" ]] \
        || fail "extension is installed but health().ready is false"
    state="$(psql_scalar "SELECT current_setting('pg_local_cache.port'), (SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE backend_type = 'pg_local_cache RESP worker')")"
    if [[ "$mode" == "sql-only" ]]; then
        [[ "$state" == "0|0" ]] \
            || fail "SQL-only verification expected port=0 and zero RESP workers, got $state"
    else
        [[ "$state" == "${resp_port}|${workers}" ]] \
            || fail "RESP verification expected ${workers} workers on port ${resp_port}, got $state"
    fi
    log "verification PASS: extension active, health ready, mode $mode"
}

install_command() {
    preflight
    create_state_backup
    install_extension_files
    ensure_worker_role
    write_configuration
    restart_or_restore
    if [[ "$restart_method" == "none" ]]; then
        log "online stage complete; no PostgreSQL restart was performed"
        log "restart the cluster once, then run: $program_name verify --database $database --mode $mode"
        return
    fi
    activate_and_verify
}

main() {
    parse_arguments "$@"
    validate_options
    case "$command_name" in
        preflight) preflight ;;
        install) install_command ;;
        verify)
            preflight
            activate_and_verify
            ;;
    esac
}

main "$@"
