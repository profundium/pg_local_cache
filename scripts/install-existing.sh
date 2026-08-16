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
mode_explicit=false
pg_config_bin="${PG_CONFIG:-pg_config}"
psql_bin="${PSQL:-psql}"
restart_method="none"
systemd_unit=""
readiness_timeout=180
restart_goal_seconds=30
state_root="/var/lib/pg_local_cache/install-state"
requested_state_directory=""
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
bind_address_explicit=false
resp_port_explicit=false
workers_explicit=false
cache_entries_explicit=false
relation_states_explicit=false
max_clients_explicit=false
max_clients_per_worker_explicit=false
memory_budget_mb_explicit=false
token_file_explicit=false

temporary_directory=""
build_directory=""
staged_file=""
release_os_user=""
release_uid=""
release_gid=""
release_input_root="$release_root"
staged_binary_lib=""
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
binary_release_build_id=""
packaged_version=""
packaged_build_id=""
extension_oid_before=""
extension_schema_oid_before=""
extension_owner_oid_before=""
extension_version_before=""
expected_extension_owner_oid=""
postgres_uid=""
postgres_gid=""
installer_uid="$(id -u)"
installer_gid="$(id -g)"
canonical_data_directory=""
cluster_system_identifier=""
database_oid=""
postmaster_start_time=""
initial_postmaster_start_time=""
postmaster_start_after=""
lock_directory=""
lock_nonce=""
lock_owned=false
current_state=""
package_digest=""
role_existed="false"
role_oid_before=""
role_had_direct_connect="false"
role_oid_after=""
role_created="false"
config_mutating="false"
saved_state=""
saved_nonce=""
saved_data_directory=""
saved_cluster_system_identifier=""
saved_database_oid=""
saved_postmaster_start=""
saved_postmaster_start_after=""
saved_package_digest=""
saved_packaged_version=""
saved_packaged_build_id=""
saved_extension_oid_before=""
saved_extension_schema_oid_before=""
saved_extension_owner_oid_before=""
saved_extension_version_before=""
saved_expected_extension_owner_oid=""
saved_lock_directory=""
saved_role_existed="false"
saved_role_oid_before=""
saved_role_had_direct_connect="false"
saved_role_oid_after=""
saved_role_created="false"
saved_config_mutating="false"
auto_conf_before_digest=""
auto_conf_before_mode=""
auto_conf_before_uid=""
auto_conf_before_gid=""
config_after_digest=""
saved_auto_conf_before_digest=""
saved_auto_conf_before_mode=""
saved_auto_conf_before_uid=""
saved_auto_conf_before_gid=""
saved_config_after_digest=""
saved_preload_before=""
saved_preload_written=""
saved_max_workers_before=""
saved_max_workers_written=""
saved_mode=""
saved_bind_address=""
saved_resp_port=""
saved_workers=""
saved_token_file=""
saved_cache_entries=""
saved_relation_states=""
saved_max_clients=""
saved_max_clients_per_worker=""
saved_memory_budget_mb=""
declare -a source_files=()
declare -a target_files=()
declare -a target_labels=()
declare -a target_modes=()
declare -a target_actions=()
declare -a target_before_digests=()
declare -a target_after_digests=()
declare -a target_before_modes=()
declare -a target_before_uids=()
declare -a target_before_gids=()

usage() {
    cat <<'EOF'
Install pg_local_cache into an existing local PostgreSQL 14-18 cluster.

Usage:
  install-existing.sh preflight [options]
  install-existing.sh install   [options]
  install-existing.sh verify    [--state-directory DIRECTORY] [options]
  install-existing.sh recover   --state-directory DIRECTORY [options]

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
  --state-directory DIRECTORY   exact state printed by install; verify/recover only
  --dry-run                     validate and print plan; mutate nothing
  --force                       replace different existing extension files
  -h, --help

Examples:
  sudo ./install-existing.sh preflight --database app --mode sql-only
  sudo ./install-existing.sh install --database app --mode sql-only
  sudo ./install-existing.sh install --database app --mode sql-only \
    --restart-method systemd --systemd-unit postgresql@18-main
  sudo ./install-existing.sh recover --state-directory /path/printed/by/install

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
    local exit_status=$?
    trap - EXIT ERR
    if ((exit_status != 0)) && [[ "$command_name" == "install" && "$lock_owned" == true ]]; then
        case "$current_state" in
            prepared | files_staged)
                requested_state_directory="$state_directory"
                if (load_requested_state && validate_state_binding && recover_pre_restart_state); then
                    warn "failed staging was restored from its pre-restart journal"
                else
                    warn "automatic pre-restart recovery failed; retained state: $state_directory"
                fi
                ;;
            '')
                release_matching_lock || warn "could not release incomplete install lock: $lock_directory"
                ;;
            *)
                warn "failure occurred after restart boundary $current_state; automatic rollback forbidden (state: $state_directory)"
                ;;
        esac
    fi
    if [[ -n "$temporary_directory" && -d "$temporary_directory" ]]; then
        rm -rf -- "$temporary_directory"
    fi
    if [[ -n "$build_directory" && -d "$build_directory" ]]; then
        rm -rf -- "$build_directory"
    fi
    exit "$exit_status"
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

require_path_text() {
    local name="$1"
    local value="$2"
    [[ -n "$value" && "$value" != *$'\n'* && "$value" != *$'\r'* && "$value" != *$'\t'* ]] \
        || fail "$name contains an unsupported control character"
}

canonical_directory() {
    local path="$1"
    [[ -d "$path" ]] || fail "directory does not exist: $path"
    (cd -- "$path" && pwd -P)
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

    stage_release_file "$metadata"
    metadata="$staged_file"
    stage_release_file "$packaged_lib"
    staged_binary_lib="$staged_file"

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
    binary_release_build_id="$commit"
}

resolve_package_build_id() {
    local build_id_file="${release_root}/BUILD-ID"
    if [[ -n "${PGLC_BUILD_ID:-}" ]]; then
        packaged_build_id="$PGLC_BUILD_ID"
    elif [[ -e "$build_id_file" || -L "$build_id_file" ]]; then
        stage_release_file "$build_id_file"
        build_id_file="$staged_file"
        packaged_build_id="$(<"$build_id_file")"
        [[ "$(stat -c '%s' "$build_id_file")" == "${#packaged_build_id}" || "$(stat -c '%s' "$build_id_file")" == "$(( ${#packaged_build_id} + 1 ))" ]] \
            || fail "BUILD-ID must contain one line"
    elif [[ -n "$binary_release_build_id" ]]; then
        packaged_build_id="$binary_release_build_id"
    elif [[ -d "$release_root/.git" && -z "$(run_as_release_owner git -C "$release_root" status --porcelain --untracked-files=all)" ]]; then
        packaged_build_id="$(run_as_release_owner git -C "$release_root" rev-parse --verify HEAD)"
    else
        fail "release needs PGLC_BUILD_ID, BUILD-ID, or an exact clean Git checkout"
    fi
    [[ "$packaged_build_id" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]] \
        || fail "build ID contains unsupported characters"
    if [[ -n "$binary_release_build_id" ]]; then
        [[ "$packaged_build_id" == "$binary_release_build_id" ]] \
            || fail "BUILD-ID does not match RELEASE-METADATA commit"
    fi
}

run_as_user() {
    local os_user="$1"
    shift
    if ((EUID == 0)) && [[ "$os_user" != "root" ]]; then
        if command -v runuser >/dev/null 2>&1; then
            runuser --preserve-environment -u "$os_user" -- "$@"
        elif [[ -x /usr/sbin/runuser ]]; then
            /usr/sbin/runuser --preserve-environment -u "$os_user" -- "$@"
        else
            su-exec "$os_user" "$@"
        fi
    else
        "$@"
    fi
}

run_as_postgres() {
    run_as_user "$postgres_os_user" "$@"
}

initialize_release_owner() {
    [[ -n "$release_os_user" ]] && return
    release_uid="$(stat -c '%u' "$release_root")"
    release_gid="$(stat -c '%g' "$release_root")"
    release_os_user="$(stat -c '%U' "$release_root")"
    [[ -n "$release_os_user" && "$release_os_user" != "UNKNOWN" ]] \
        || fail "release owner UID has no operating-system account: $release_uid"
    if ((EUID == 0)) && [[ "$release_os_user" != "root" ]]; then
        command -v runuser >/dev/null 2>&1 \
            || [[ -x /usr/sbin/runuser ]] \
            || require_command su-exec
    fi
}

validate_release_directory_chain() {
    local canonical="$1" current='' part mode_value owner_uid
    local -a parts
    require_path_text "release directory" "$canonical"
    IFS='/' read -r -a parts <<< "${canonical#/}"
    for part in "${parts[@]}"; do
        [[ -n "$part" ]] || continue
        current+="/$part"
        [[ -d "$current" && ! -L "$current" ]] \
            || fail "release directory chain is not stable: $current"
        mode_value="$(stat -c '%a' "$current")"
        owner_uid="$(stat -c '%u' "$current")"
        [[ "$owner_uid" == "0" || "$owner_uid" == "$release_uid" ]] \
            || fail "release directory chain has an untrusted owner: $current"
        if (( (8#$mode_value & 0022) != 0 )); then
            [[ "$owner_uid" == "0" ]] && (( (8#$mode_value & 01000) != 0 )) \
                || fail "release directory chain is group/world writable: $current"
        fi
    done
}

run_as_release_owner() {
    initialize_release_owner
    run_as_user "$release_os_user" "$@"
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
    local allow_unregistered_extension_settings="${1:-false}"
    local allowed=""
    if [[ "$allow_unregistered_extension_settings" == true ]]; then
        allowed=" AND NOT (s.name IS NULL AND f.name IN ('pg_local_cache.database', 'pg_local_cache.role', 'pg_local_cache.bind_address', 'pg_local_cache.port', 'pg_local_cache.workers', 'pg_local_cache.cache_entries', 'pg_local_cache.relation_states', 'pg_local_cache.max_clients', 'pg_local_cache.max_clients_per_worker', 'pg_local_cache.memory_budget_mb', 'pg_local_cache.auth_token_file'))"
    fi
    psql_scalar "SELECT count(*) FROM pg_catalog.pg_file_settings AS f LEFT JOIN pg_catalog.pg_settings AS s ON s.name = f.name WHERE f.error IS NOT NULL AND NOT coalesce(s.context = 'postmaster' AND s.pending_restart, false)${allowed}"
}

configured_setting() {
    psql_base --quiet --tuples-only --no-align --set setting_name="$1" <<'SQL'
SELECT coalesce(
           (SELECT setting
              FROM pg_catalog.pg_file_settings
             WHERE name = :'setting_name'
             ORDER BY seqno DESC
             LIMIT 1),
           pg_catalog.current_setting(:'setting_name', true),
           '');
SQL
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
        preflight | install | verify | recover) ;;
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
            --mode) mode="${2:?missing value for --mode}"; mode_explicit=true; shift 2 ;;
            --bind-address) bind_address="${2:?missing value for --bind-address}"; bind_address_explicit=true; shift 2 ;;
            --port) resp_port="${2:?missing value for --port}"; resp_port_explicit=true; shift 2 ;;
            --workers) workers="${2:?missing value for --workers}"; workers_explicit=true; shift 2 ;;
            --cache-entries) cache_entries="${2:?missing value for --cache-entries}"; cache_entries_explicit=true; shift 2 ;;
            --relation-states) relation_states="${2:?missing value for --relation-states}"; relation_states_explicit=true; shift 2 ;;
            --max-clients) max_clients="${2:?missing value for --max-clients}"; max_clients_explicit=true; shift 2 ;;
            --max-clients-per-worker) max_clients_per_worker="${2:?missing value for --max-clients-per-worker}"; max_clients_per_worker_explicit=true; shift 2 ;;
            --memory-budget-mb) memory_budget_mb="${2:?missing value for --memory-budget-mb}"; memory_budget_mb_explicit=true; shift 2 ;;
            --token-file) token_file="${2:?missing value for --token-file}"; token_file_explicit=true; shift 2 ;;
            --restart-method) restart_method="${2:?missing value for --restart-method}"; shift 2 ;;
            --systemd-unit) systemd_unit="${2:?missing value for --systemd-unit}"; shift 2 ;;
            --readiness-timeout) readiness_timeout="${2:?missing value for --readiness-timeout}"; shift 2 ;;
            --restart-goal-seconds) restart_goal_seconds="${2:?missing value for --restart-goal-seconds}"; shift 2 ;;
            --state-root) state_root="${2:?missing value for --state-root}"; shift 2 ;;
            --state-directory) requested_state_directory="${2:?missing value for --state-directory}"; shift 2 ;;
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
    if [[ "$command_name" == "recover" ]]; then
        [[ "$requested_state_directory" == /* ]] \
            || fail "--state-directory is required and must be absolute for $command_name"
    elif [[ "$command_name" == "verify" && -n "$requested_state_directory" ]]; then
        [[ "$requested_state_directory" == /* ]] \
            || fail "--state-directory must be absolute"
    elif [[ -n "$requested_state_directory" ]]; then
        fail "--state-directory is valid only for verify or recover"
    fi

    require_integer_between "--port" "$resp_port" 0 65535
    require_integer_between "--workers" "$workers" 1 32
    require_integer_between "--cache-entries" "$cache_entries" 128 65536
    require_integer_between "--relation-states" "$relation_states" 128 8192
    require_integer_between "--max-clients" "$max_clients" 1 4096
    require_integer_between "--max-clients-per-worker" "$max_clients_per_worker" 1 128
    require_integer_between "--memory-budget-mb" "$memory_budget_mb" 64 8192
    require_integer_between "--readiness-timeout" "$readiness_timeout" 30 3600
    require_integer_between "--restart-goal-seconds" "$restart_goal_seconds" 1 3600

    if [[ "$mode" == "resp" ]]; then
        ((resp_port > 0)) || fail "--port must be nonzero in RESP mode"
        ((max_clients <= workers * max_clients_per_worker)) \
            || fail "max clients must not exceed workers x clients per worker"
        [[ -z "$token_file" || "$token_file" == /* ]] \
            || fail "--token-file must be absolute in RESP mode"
    fi
}

validate_token_file() {
    [[ "$mode" == "resp" ]] || return 0
    [[ -f "$token_file" && ! -L "$token_file" ]] \
        || fail "RESP token must be a regular, non-symlink file: $token_file"
    local owner mode_value link_count raw token byte_count suffix_bytes=0 last_byte last_two
    owner="$(stat -c '%U' "$token_file")"
    mode_value="$(stat -c '%a' "$token_file")"
    link_count="$(stat -c '%h' "$token_file")"
    [[ "$owner" == "$postgres_os_user" ]] \
        || fail "RESP token owner must be $postgres_os_user (actual: $owner)"
    [[ "$mode_value" == "400" || "$mode_value" == "600" ]] \
        || fail "RESP token mode must be exactly 0400 or 0600 (actual: $mode_value)"
    [[ "$link_count" == "1" ]] \
        || fail "RESP token must have exactly one hard link"
    run_as_postgres test -r "$token_file" \
        || fail "RESP token is not readable by $postgres_os_user"
    byte_count="$(stat -c '%s' "$token_file")"
    LC_ALL=C raw="$(<"$token_file")"
    token="$raw"
    last_byte="$(tail -c 1 -- "$token_file" | od -An -tu1 | tr -d ' \n')"
    last_two="$(tail -c 2 -- "$token_file" | od -An -tx1 | tr -d ' \n')"
    if [[ "$raw" == *$'\r' && "$last_two" == "0d0a" ]]; then
        token="${raw%$'\r'}"
        suffix_bytes=2
    elif ((byte_count == ${#raw} + 1)) && [[ "$last_byte" == "10" ]]; then
        suffix_bytes=1
    fi
    ((byte_count == ${#token} + suffix_bytes)) \
        || fail "RESP token permits only one terminal LF or CRLF"
    (( ${#token} >= 32 && ${#token} <= 256 )) &&
        [[ "$token" =~ ^[A-Za-z0-9_-]+$ ]] \
        || fail "RESP token must contain 32-256 base64url characters"
}

preflight() {
    require_command "$pg_config_bin"
    require_command "$psql_bin"
    if [[ "$dry_run" != true ]]; then
        local utility
        for utility in awk cat chown chmod date install mkdir mktemp mv od readlink rm rmdir sha256sum sort stat tail; do
            require_command "$utility"
        done
    fi
    if ((EUID == 0)) && [[ "$postgres_os_user" != "root" ]]; then
        command -v runuser >/dev/null 2>&1 \
            || [[ -x /usr/sbin/runuser ]] \
            || require_command su-exec
    fi
    id "$postgres_os_user" >/dev/null 2>&1 \
        || fail "operating-system user does not exist: $postgres_os_user"

    local local_version local_major server_version server_major
    local is_superuser is_primary data_owner_uid
    local local_pkglib local_share server_pkglib server_share config_errors
    local configured_preload configured_max_workers active_port active_workers
    local configured_port configured_workers reserved_resp_workers
    local configured_bind configured_cache_entries configured_relation_states
    local configured_max_clients configured_max_clients_per_worker
    local configured_memory_budget_mb configured_token_file

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
    if [[ "$command_name" != "recover" ]]; then
        validate_binary_release "$local_major"
    fi
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
    require_path_text "server data_directory" "$data_directory"
    canonical_data_directory="$(canonical_directory "$data_directory")"
    postgres_uid="$(id -u "$postgres_os_user")"
    postgres_gid="$(id -g "$postgres_os_user")"
    if [[ "$dry_run" != true ]]; then
        data_owner_uid="$(stat -c '%u' "$canonical_data_directory")"
        [[ "$data_owner_uid" == "$postgres_uid" ]] \
            || fail "data_directory owner does not match $postgres_os_user"
    fi
    data_directory="$canonical_data_directory"
    lock_directory="${data_directory}/.pg_local_cache-install.lock"
    auto_conf="${data_directory}/postgresql.auto.conf"
    [[ -f "$auto_conf" ]] \
        || fail "postgresql.auto.conf is missing: $auto_conf"
    if [[ "$dry_run" != true ]]; then
        [[ ! -L "$auto_conf" && "$(stat -c '%h' "$auto_conf")" == "1" ]] \
            || fail "postgresql.auto.conf must be a regular, non-symlink, one-link file"
        [[ "$(stat -c '%u' "$auto_conf")" == "$postgres_uid" \
            && "$(stat -c '%g' "$auto_conf")" == "$postgres_gid" ]] \
            || fail "postgresql.auto.conf owner must be $postgres_os_user"
    fi

    cluster_system_identifier="$(psql_scalar "SELECT system_identifier::text FROM pg_catalog.pg_control_system()")"
    database_oid="$(psql_scalar "SELECT oid::text FROM pg_catalog.pg_database WHERE datname = pg_catalog.current_database()")"
    postmaster_start_time="$(psql_scalar "SELECT pg_catalog.pg_postmaster_start_time()::text")"
    [[ "$cluster_system_identifier" =~ ^[0-9]+$ ]] \
        || fail "could not read cluster system identifier"
    [[ "$database_oid" =~ ^[0-9]+$ ]] \
        || fail "could not read database OID"
    require_path_text "postmaster start time" "$postmaster_start_time"

    active_preload="$(psql_scalar "SHOW shared_preload_libraries")"
    configured_preload="$(psql_scalar "SELECT coalesce((SELECT setting FROM pg_catalog.pg_file_settings WHERE name = 'shared_preload_libraries' ORDER BY seqno DESC LIMIT 1), pg_catalog.current_setting('shared_preload_libraries'))")"
    if preload_contains_extension "$configured_preload" && ! preload_contains_extension "$active_preload"; then
        config_errors="$(configuration_error_count true)"
    else
        config_errors="$(configuration_error_count)"
    fi
    [[ "$config_errors" == "0" ]] \
        || fail "pg_file_settings already reports $config_errors configuration error(s)"

    configured_max_workers="$(psql_scalar "SELECT coalesce((SELECT setting FROM pg_catalog.pg_file_settings WHERE name = 'max_worker_processes' ORDER BY seqno DESC LIMIT 1), pg_catalog.current_setting('max_worker_processes'))")"
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
        configured_port="$(configured_setting pg_local_cache.port)"
        configured_workers="$(configured_setting pg_local_cache.workers)"
        configured_bind="$(configured_setting pg_local_cache.bind_address)"
        configured_cache_entries="$(configured_setting pg_local_cache.cache_entries)"
        configured_relation_states="$(configured_setting pg_local_cache.relation_states)"
        configured_max_clients="$(configured_setting pg_local_cache.max_clients)"
        configured_max_clients_per_worker="$(configured_setting pg_local_cache.max_clients_per_worker)"
        configured_memory_budget_mb="$(configured_setting pg_local_cache.memory_budget_mb)"
        configured_token_file="$(configured_setting pg_local_cache.auth_token_file)"
        if [[ "$configured_memory_budget_mb" =~ ^([0-9]+)MB$ ]]; then
            configured_memory_budget_mb="${BASH_REMATCH[1]}"
        fi
        [[ "$configured_port" =~ ^[0-9]+$ && "$configured_workers" =~ ^[0-9]+$ ]] \
            || fail "staged pg_local_cache port/workers settings are incomplete or invalid"
        [[ -n "$configured_bind" && "$configured_cache_entries" =~ ^[0-9]+$ \
            && "$configured_relation_states" =~ ^[0-9]+$ \
            && "$configured_max_clients" =~ ^[0-9]+$ \
            && "$configured_max_clients_per_worker" =~ ^[0-9]+$ \
            && "$configured_memory_budget_mb" =~ ^[0-9]+$ ]] \
            || fail "staged pg_local_cache settings are incomplete or invalid"
        [[ "$mode_explicit" == true ]] || {
            if ((configured_port == 0)); then mode=sql-only; else mode=resp; fi
        }
        [[ "$bind_address_explicit" == true ]] || bind_address="$configured_bind"
        [[ "$resp_port_explicit" == true || "$mode_explicit" == true ]] \
            || resp_port="$configured_port"
        [[ "$workers_explicit" == true ]] || workers="$configured_workers"
        [[ "$cache_entries_explicit" == true ]] || cache_entries="$configured_cache_entries"
        [[ "$relation_states_explicit" == true ]] || relation_states="$configured_relation_states"
        [[ "$max_clients_explicit" == true ]] || max_clients="$configured_max_clients"
        [[ "$max_clients_per_worker_explicit" == true ]] || max_clients_per_worker="$configured_max_clients_per_worker"
        [[ "$memory_budget_mb_explicit" == true ]] || memory_budget_mb="$configured_memory_budget_mb"
        [[ "$token_file_explicit" == true ]] || token_file="$configured_token_file"
        if ((configured_port != 0)); then configured_resp_workers="$configured_workers"; fi
    fi

    if [[ "$mode" == "sql-only" ]]; then
        resp_port=0
        if [[ "$extension_was_configured" == false || "$mode_explicit" == true ]]; then
            token_file=""
        fi
    fi
    validate_options

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

    if [[ "$command_name" != "recover" ]]; then
        [[ "$mode" != "resp" || "$token_file" == /* ]] \
            || fail "--token-file must be absolute in RESP mode"
        validate_token_file
    fi

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

prepared_source_lib=""
prepared_source_share=""

ensure_temporary_directory() {
    if [[ -z "$temporary_directory" ]]; then
        temporary_directory="$(mktemp -d -t pg_local_cache_install.XXXXXX)"
        chmod 0700 "$temporary_directory"
        mkdir -m 0700 "$temporary_directory/staged"
    fi
}

stage_file_as_user() {
    local os_user="$1" source="$2" destination
    ensure_temporary_directory
    destination="$(mktemp "$temporary_directory/staged/file.XXXXXX")"
    if ! (umask 077; run_as_user "$os_user" cat -- "$source" > "$destination"); then
        rm -f -- "$destination"
        fail "could not stage untrusted file safely: $source"
    fi
    [[ -f "$destination" && ! -L "$destination" && "$(stat -c '%h' "$destination")" == "1" ]] \
        || fail "staged file identity is invalid: $source"
    staged_file="$destination"
}

stage_release_file() {
    local source="$1" source_mode source_uid canonical_source canonical_root
    [[ -f "$source" && ! -L "$source" && "$(stat -c '%h' "$source")" == "1" ]] \
        || fail "release member must be a regular, non-symlink, one-link file: $source"
    source_mode="$(stat -c '%a' "$source")"
    (( (8#$source_mode & 0022) == 0 )) \
        || fail "release member must not be group/world writable: $source"
    canonical_root="$(canonical_directory "$release_input_root")"
    canonical_source="$(readlink -f -- "$source")"
    [[ "$canonical_source" == "$canonical_root/"* ]] \
        || fail "release member escapes its package directory: $source"
    initialize_release_owner
    validate_release_directory_chain "$(dirname -- "$canonical_source")"
    source_uid="$(stat -c '%u' "$canonical_source")"
    [[ "$source_uid" == "$release_uid" ]] \
        || fail "release member owner differs from the package owner: $source"
    stage_file_as_user "$release_os_user" "$canonical_source"
}

stage_postgres_file() {
    stage_file_as_user "$postgres_os_user" "$1"
}

postgres_file_digest() {
    run_as_postgres sha256sum -- "$1" | awk '{print $1}'
}

private_replace() {
    local target="$1" mode_value="$2"
    local temporary="${target}.tmp.$$"
    run_as_postgres bash -c '
        set -Eeuo pipefail
        set -o noclobber
        umask 077
        temporary="$1"
        target="$2"
        mode_value="$3"
        trap '\''rm -f -- "$temporary"'\'' EXIT
        exec 3> "$temporary"
        cat >&3
        exec 3>&-
        chmod "$mode_value" "$temporary"
        mv -f -- "$temporary" "$target"
        trap - EXIT
    ' _ "$temporary" "$target" "$mode_value"
}

private_write() {
    private_replace "$1" 0600
}

validate_private_directory() {
    local path="$1"
    [[ -d "$path" && ! -L "$path" ]] \
        || fail "trusted directory is missing or is a symlink: $path"
    [[ "$(stat -c '%u' "$path")" == "$postgres_uid" && "$(stat -c '%a' "$path")" == "700" ]] \
        || fail "trusted directory must be owned by $postgres_os_user with mode 0700: $path"
}

validate_private_file() {
    local path="$1"
    [[ -f "$path" && ! -L "$path" && "$(stat -c '%h' "$path")" == "1" ]] \
        || fail "trusted state file must be regular, non-symlink and one-link: $path"
    [[ "$(stat -c '%u' "$path")" == "$postgres_uid" && "$(stat -c '%a' "$path")" == "600" ]] \
        || fail "trusted state file must be owned by $postgres_os_user with mode 0600: $path"
}

create_private_directory() {
    local path="$1"
    if [[ -e "$path" || -L "$path" ]]; then
        validate_private_directory "$path"
    else
        install -d -o "$postgres_uid" -g "$postgres_gid" -m 0700 "$path"
    fi
}

process_start_marker() {
    local pid="$1"
    [[ "$pid" =~ ^[0-9]+$ && -r "/proc/${pid}/stat" ]] || return 1
    awk '{print $22}' "/proc/${pid}/stat"
}

failure_point() {
    local point="$1"
    if [[ "${PGLC_INSTALL_CRASH_AT:-}" == "$point" ]]; then
        kill -KILL "$$"
    fi
    [[ "${PGLC_INSTALL_FAIL_AT:-}" != "$point" ]] \
        || fail "injected failure at $point"
}

write_lock_owner() {
    local process_marker
    process_marker="$(process_start_marker "$$")" \
        || fail "could not read installer process start time"
    {
        printf 'pid\t%s\n' "$$"
        printf 'process_start\t%s\n' "$process_marker"
        printf 'cluster_system_identifier\t%s\n' "$cluster_system_identifier"
        printf 'nonce\t%s\n' "$lock_nonce"
        printf 'state_directory\t%s\n' "$state_directory"
    } | private_write "$lock_directory/owner.tsv"
}

acquire_install_lock() {
    if ! mkdir -m 0700 -- "$lock_directory" 2>/dev/null; then
        fail "another install or pending recovery owns $lock_directory; use its state-bound verify/recover path"
    fi
    chown "$postgres_uid:$postgres_gid" "$lock_directory"
    lock_owned=true
    write_lock_owner
    failure_point after_lock
}

write_state() {
    local next_state="$1"
    current_state="$next_state"
    {
        printf 'format\t1\n'
        printf 'state\t%s\n' "$current_state"
        printf 'nonce\t%s\n' "$lock_nonce"
        printf 'state_directory\t%s\n' "$state_directory"
        printf 'lock_directory\t%s\n' "$lock_directory"
        printf 'data_directory\t%s\n' "$data_directory"
        printf 'cluster_system_identifier\t%s\n' "$cluster_system_identifier"
        printf 'database\t%s\n' "$database"
        printf 'database_oid\t%s\n' "$database_oid"
        printf 'postmaster_start_before\t%s\n' "$initial_postmaster_start_time"
        printf 'postmaster_start_after\t%s\n' "$postmaster_start_after"
        printf 'package_digest\t%s\n' "$package_digest"
        printf 'packaged_version\t%s\n' "$packaged_version"
        printf 'packaged_build_id\t%s\n' "$packaged_build_id"
        printf 'extension_oid_before\t%s\n' "$extension_oid_before"
        printf 'extension_schema_oid_before\t%s\n' "$extension_schema_oid_before"
        printf 'extension_owner_oid_before\t%s\n' "$extension_owner_oid_before"
        printf 'extension_version_before\t%s\n' "$extension_version_before"
        printf 'expected_extension_owner_oid\t%s\n' "$expected_extension_owner_oid"
        printf 'database_role\t%s\n' "$worker_role"
        printf 'mode\t%s\n' "$mode"
        printf 'bind_address\t%s\n' "$bind_address"
        printf 'resp_port\t%s\n' "$resp_port"
        printf 'workers\t%s\n' "$workers"
        printf 'cache_entries\t%s\n' "$cache_entries"
        printf 'relation_states\t%s\n' "$relation_states"
        printf 'max_clients\t%s\n' "$max_clients"
        printf 'max_clients_per_worker\t%s\n' "$max_clients_per_worker"
        printf 'memory_budget_mb\t%s\n' "$memory_budget_mb"
        printf 'token_file\t%s\n' "$token_file"
        printf 'preload_before\t%s\n' "$preload_before"
        printf 'preload_written\t%s\n' "$preload_after"
        printf 'max_worker_processes_before\t%s\n' "$max_workers_before"
        printf 'max_worker_processes_written\t%s\n' "$max_workers_after"
        printf 'role_existed\t%s\n' "$role_existed"
        printf 'role_oid_before\t%s\n' "$role_oid_before"
        printf 'role_had_direct_connect\t%s\n' "$role_had_direct_connect"
        printf 'role_oid_after\t%s\n' "$role_oid_after"
        printf 'role_created\t%s\n' "$role_created"
        printf 'config_mutating\t%s\n' "$config_mutating"
        printf 'auto_conf_before_digest\t%s\n' "$auto_conf_before_digest"
        printf 'auto_conf_before_mode\t%s\n' "$auto_conf_before_mode"
        printf 'auto_conf_before_uid\t%s\n' "$auto_conf_before_uid"
        printf 'auto_conf_before_gid\t%s\n' "$auto_conf_before_gid"
        printf 'config_after_digest\t%s\n' "$config_after_digest"
        printf 'updated_at_utc\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } | private_write "$state_directory/state.tsv"
}

locate_release_files() {
    local packaged_lib packaged_share stage
    packaged_lib="${release_root}/lib/pg_local_cache.so"
    packaged_share="${release_root}/share/extension"
    if [[ -f "$packaged_lib" && -f "$packaged_share/pg_local_cache.control" ]]; then
        [[ -n "$staged_binary_lib" ]] || stage_release_file "$packaged_lib"
        prepared_source_lib="${staged_binary_lib:-$staged_file}"
        prepared_source_share="$packaged_share"
        release_input_root="$release_root"
        return
    fi

    [[ -f "$release_root/Makefile" && -d "$release_root/src" ]] \
        || fail "release contains neither compatible binary files nor buildable source"
    require_command make
    initialize_release_owner
    stage_release_file "$release_root/Makefile"
    build_directory="$(mktemp -d -t pg_local_cache_build.XXXXXX)"
    chown "$release_uid:$release_gid" "$build_directory"
    chmod 0700 "$build_directory"
    stage="${build_directory}/stage"
    log "building extension against $pg_config_bin" >&2
    run_as_release_owner make -C "$release_root" \
        PG_CONFIG="$pg_config_bin" PGLC_BUILD_ID="$packaged_build_id" >&2
    run_as_release_owner make -C "$release_root" PG_CONFIG="$pg_config_bin" \
        PGLC_BUILD_ID="$packaged_build_id" DESTDIR="$stage" install >&2
    packaged_lib="${stage}$($pg_config_bin --pkglibdir)/pg_local_cache.so"
    packaged_share="${stage}$($pg_config_bin --sharedir)/extension"
    [[ -f "$packaged_lib" && -f "$packaged_share/pg_local_cache.control" ]] \
        || fail "PGXS build did not produce the expected extension files"
    release_input_root="$stage"
    stage_release_file "$packaged_lib"
    prepared_source_lib="$staged_file"
    prepared_source_share="$packaged_share"
}

add_release_target() {
    local source="$1" target="$2" mode_value="$3" label="$4"
    local source_mode parent_mode canonical_source canonical_parent
    [[ -f "$source" && ! -L "$source" && "$(stat -c '%h' "$source")" == "1" ]] \
        || fail "release member must be a regular, non-symlink, one-link file: $source"
    source_mode="$(stat -c '%a' "$source")"
    (( (8#$source_mode & 0022) == 0 )) \
        || fail "release member must not be group/world writable: $source"
    canonical_source="$(readlink -f -- "$source")"
    canonical_parent="$(canonical_directory "$(dirname -- "$target")")"
    parent_mode="$(stat -c '%a' "$canonical_parent")"
    (( (8#$parent_mode & 0022) == 0 )) \
        || fail "install destination directory must not be group/world writable: $canonical_parent"
    if ((EUID == 0)); then
        [[ "$(stat -c '%u' "$canonical_parent")" == "$installer_uid" ]] \
            || fail "root install destination directory must be root-owned: $canonical_parent"
    fi
    target="$canonical_parent/$(basename -- "$target")"
    require_path_text "release source" "$canonical_source"
    require_path_text "install destination" "$target"
    source_files+=("$canonical_source")
    target_files+=("$target")
    target_modes+=("$mode_value")
    target_labels+=("$label")
}

prepare_release_files() {
    local target_lib target_share version source_sql staged_control sql_name sql_label
    local sql_count=0 update_count=0 manifest='' index
    source_files=(); target_files=(); target_modes=(); target_labels=()
    resolve_package_build_id
    locate_release_files
    target_lib="$(canonical_directory "$($pg_config_bin --pkglibdir)")/pg_local_cache.so"
    target_share="$(canonical_directory "$($pg_config_bin --sharedir)/extension")"
    stage_release_file "$prepared_source_share/pg_local_cache.control"
    staged_control="$staged_file"
    version="$(sed -n "s/^default_version = '\([^']*\)'$/\1/p" "$staged_control")"
    [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
        || fail "control file has an invalid default_version"
    packaged_version="$version"
    if [[ -n "$binary_release_version" ]]; then
        [[ "$version" == "$binary_release_version" ]] \
            || fail "binary metadata version $binary_release_version does not match control version $version"
    fi
    [[ -f "$prepared_source_share/pg_local_cache--${version}.sql" ]] \
        || fail "release SQL file is missing for version $version"

    if [[ "$(detect_linux_libc)" == glibc ]] && command -v ldd >/dev/null 2>&1; then
        if ldd "$prepared_source_lib" | grep -Fq 'not found'; then
            ldd "$prepared_source_lib" >&2 || true
            fail "extension binary has unresolved runtime libraries"
        fi
    fi
    add_release_target "$prepared_source_lib" "$target_lib" 0755 extension_so
    add_release_target "$staged_control" \
        "$target_share/pg_local_cache.control" 0644 extension_control
    shopt -s nullglob
    for source_sql in "$prepared_source_share"/pg_local_cache--*.sql; do
        sql_name="${source_sql##*/}"
        [[ "$sql_name" =~ ^pg_local_cache--[0-9]+\.[0-9]+\.[0-9]+(--[0-9]+\.[0-9]+\.[0-9]+)?\.sql$ ]] \
            || fail "release contains an invalid extension SQL filename: $sql_name"
        [[ "$sql_name" != *--*--* || "$sql_name" =~ --[0-9]+\.[0-9]+\.[0-9]+--[0-9]+\.[0-9]+\.[0-9]+\.sql$ ]] \
            || fail "release contains an invalid update SQL filename: $sql_name"
        sql_label="extension_sql_${sql_name//[^A-Za-z0-9]/_}"
        stage_release_file "$source_sql"
        add_release_target "$staged_file" "$target_share/$sql_name" 0644 "$sql_label"
        [[ "$sql_name" == *--*--* ]] && update_count=$((update_count + 1))
        sql_count=$((sql_count + 1))
    done
    shopt -u nullglob
    ((sql_count > 0)) || fail "release contains no extension SQL files"
    ((update_count > 0)) || fail "release contains no extension update SQL file"
    for index in "${!source_files[@]}"; do
        manifest+="${target_labels[index]} ${target_modes[index]} $(sha256sum "${source_files[index]}" | awk '{print $1}')\n"
    done
    manifest+="build_id 0644 $(printf '%s' "$packaged_build_id" | sha256sum | awk '{print $1}')\n"
    package_digest="$(printf '%b' "$manifest" | LC_ALL=C sort | sha256sum | awk '{print $1}')"
}

read_package_identity() {
    local control
    resolve_package_build_id
    release_input_root="$release_root"
    if [[ -f "$release_root/share/extension/pg_local_cache.control" ]]; then
        control="$release_root/share/extension/pg_local_cache.control"
    elif [[ -f "$release_root/pg_local_cache.control" ]]; then
        control="$release_root/pg_local_cache.control"
    else
        fail "release is missing pg_local_cache.control"
    fi
    stage_release_file "$control"
    control="$staged_file"
    packaged_version="$(sed -n "s/^default_version = '\([^']*\)'$/\1/p" "$control")"
    [[ "$packaged_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
        || fail "control file has an invalid default_version"
    [[ -z "$binary_release_version" || "$binary_release_version" == "$packaged_version" ]] \
        || fail "binary metadata version does not match control version"
}

inspect_targets() {
    local index source target mode_value action before_digest before_mode before_uid before_gid
    target_actions=(); target_before_digests=(); target_after_digests=()
    target_before_modes=(); target_before_uids=(); target_before_gids=()
    for index in "${!source_files[@]}"; do
        source="${source_files[index]}"; target="${target_files[index]}"; mode_value="${target_modes[index]}"
        [[ "$(canonical_directory "$(dirname -- "$target")")" == "$(dirname -- "$target")" ]] \
            || fail "install destination parent is no longer canonical: $target"
        before_digest='-'; before_mode='-'; before_uid='-'; before_gid='-'
        if [[ -e "$target" || -L "$target" ]]; then
            [[ -f "$target" && ! -L "$target" && "$(stat -c '%h' "$target")" == "1" ]] \
                || fail "install destination must be a regular, non-symlink, one-link file: $target"
            before_digest="$(sha256sum "$target" | awk '{print $1}')"
            before_mode="$(stat -c '%a' "$target")"
            before_uid="$(stat -c '%u' "$target")"
            before_gid="$(stat -c '%g' "$target")"
            if [[ "$before_digest" == "$(sha256sum "$source" | awk '{print $1}')" && "$before_mode" == "${mode_value#0}" ]]; then
                action=unchanged
            else
                [[ "$force" == true ]] \
                    || fail "$target differs; use --force only for an intentional upgrade"
                action=replaced
            fi
        else
            action=created
        fi
        target_actions+=("$action")
        target_before_digests+=("$before_digest")
        target_after_digests+=("$(sha256sum "$source" | awk '{print $1}')")
        target_before_modes+=("$before_mode")
        target_before_uids+=("$before_uid")
        target_before_gids+=("$before_gid")
    done
}

capture_database_baseline() {
    local baseline
    baseline="$(psql_base --quiet --tuples-only --no-align --set worker_role="$worker_role" <<'SQL'
SELECT coalesce(r.oid::text, ''),
       coalesce(pg_catalog.has_database_privilege(r.oid, d.oid, 'CONNECT')::text, 'false'),
       coalesce(EXISTS (
           SELECT 1
             FROM pg_catalog.aclexplode(coalesce(d.datacl, pg_catalog.acldefault('d', d.datdba))) AS a
            WHERE a.grantee = r.oid AND a.privilege_type = 'CONNECT'
       )::text, 'false')
  FROM pg_catalog.pg_database AS d
  LEFT JOIN pg_catalog.pg_roles AS r ON r.rolname = :'worker_role'
 WHERE d.datname = pg_catalog.current_database();
SQL
)"
    IFS='|' read -r role_oid_before _ role_had_direct_connect <<< "$baseline"
    if [[ -n "$role_oid_before" ]]; then role_existed=true; else role_existed=false; fi
    [[ "$role_had_direct_connect" == "t" || "$role_had_direct_connect" == "true" ]] \
        && role_had_direct_connect=true || role_had_direct_connect=false
}

capture_extension_baseline() {
    local baseline
    baseline="$(psql_base --quiet --tuples-only --no-align <<'SQL'
SELECT coalesce(e.oid::text, ''),
       coalesce(e.extnamespace::text, ''),
       coalesce(e.extowner::text, ''),
       coalesce(e.extversion, ''),
       coalesce(e.extowner::text,
                (SELECT oid::text FROM pg_catalog.pg_roles WHERE rolname = CURRENT_USER))
  FROM (SELECT 1) AS seed
  LEFT JOIN pg_catalog.pg_extension AS e ON e.extname = 'pg_local_cache';
SQL
)"
    IFS='|' read -r extension_oid_before extension_schema_oid_before \
        extension_owner_oid_before extension_version_before \
        expected_extension_owner_oid <<< "$baseline"
    [[ -z "$extension_oid_before" || "$extension_oid_before" =~ ^[0-9]+$ ]] \
        || fail "installed extension OID is invalid"
    [[ -z "$extension_schema_oid_before" || "$extension_schema_oid_before" =~ ^[0-9]+$ ]] \
        || fail "installed extension schema OID is invalid"
    [[ -z "$extension_owner_oid_before" || "$extension_owner_oid_before" =~ ^[0-9]+$ ]] \
        || fail "installed extension owner OID is invalid"
    [[ "$expected_extension_owner_oid" =~ ^[0-9]+$ ]] \
        || fail "could not record extension owner identity"
    require_path_text "installed extension version" "${extension_version_before:-absent}"
}

validate_packaged_default_available() {
    local available
    available="$(psql_base --quiet --tuples-only --no-align \
        --set target_version="$packaged_version" <<'SQL'
SELECT count(*)
  FROM pg_catalog.pg_available_extension_versions
 WHERE name = 'pg_local_cache' AND version = :'target_version';
SQL
)"
    [[ "$available" == "1" ]] \
        || fail "packaged default version $packaged_version is not available after staging"
}

create_state_journal() {
    local index backup target_owner
    create_private_directory "$state_root"
    state_root="$(canonical_directory "$state_root")"
    lock_nonce="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
    [[ "$lock_nonce" =~ ^[0-9a-f]{32}$ ]] || fail "could not generate install nonce"
    state_directory="${state_root%/}/install-$(date -u +%Y%m%dT%H%M%SZ)-${lock_nonce}"
    initial_postmaster_start_time="$postmaster_start_time"
    require_path_text "state directory" "$state_directory"
    log "state directory: $state_directory"
    acquire_install_lock
    create_private_directory "$state_directory"
    create_private_directory "$state_directory/backups"
    inspect_targets
    capture_database_baseline
    capture_extension_baseline
    auto_conf_before_mode="$(stat -c '%a' "$auto_conf")"
    auto_conf_before_uid="$(stat -c '%u' "$auto_conf")"
    auto_conf_before_gid="$(stat -c '%g' "$auto_conf")"
    stage_postgres_file "$auto_conf"
    auto_conf_before_digest="$(sha256sum "$staged_file" | awk '{print $1}')"
    [[ "$(postgres_file_digest "$auto_conf")" == "$auto_conf_before_digest" \
        && "$(stat -c '%a' "$auto_conf")" == "$auto_conf_before_mode" \
        && "$(stat -c '%u' "$auto_conf")" == "$auto_conf_before_uid" \
        && "$(stat -c '%g' "$auto_conf")" == "$auto_conf_before_gid" ]] \
        || fail "postgresql.auto.conf changed while its backup was staged"
    private_write "$state_directory/backups/postgresql.auto.conf.before" < "$staged_file"
    {
        for index in "${!target_files[@]}"; do
            backup='-'
            if [[ "${target_actions[index]}" == "replaced" ]]; then
                backup="$state_directory/backups/${target_labels[index]}.before"
                target_owner="$(stat -c '%U' "${target_files[index]}")"
                [[ -n "$target_owner" && "$target_owner" != "UNKNOWN" ]] \
                    || fail "install target owner UID has no operating-system account: ${target_before_uids[index]}"
                stage_file_as_user "$target_owner" "${target_files[index]}"
                [[ "$(sha256sum "$staged_file" | awk '{print $1}')" == "${target_before_digests[index]}" ]] \
                    || fail "install target changed while its backup was staged: ${target_files[index]}"
                private_write "$backup" < "$staged_file"
            fi
            printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                "${target_labels[index]}" "${target_actions[index]}" \
                "${target_files[index]}" "$backup" "${target_modes[index]}" \
                "${target_before_digests[index]}" "${target_after_digests[index]}" \
                "${target_before_modes[index]}" "${target_before_uids[index]}" \
                "${target_before_gids[index]}" "$installer_uid" "$installer_gid"
        done
    } | private_write "$state_directory/files.tsv"
    failure_point before_journal
    write_state prepared
    failure_point after_journal
}

install_extension_files() {
    local index source target action temporary_target
    if [[ "$dry_run" == true ]]; then
        log "dry-run: package and every destination validated; no files written"
        return
    fi
    for index in "${!source_files[@]}"; do
        source="${source_files[index]}"; target="${target_files[index]}"; action="${target_actions[index]}"
        if [[ "$action" == "unchanged" ]]; then
            log "${target_labels[index]} is already current: $target"
            continue
        fi
        temporary_target="${target}.pg_local_cache.${lock_nonce}"
        install -m "${target_modes[index]}" -o "$installer_uid" -g "$installer_gid" "$source" "$temporary_target"
        mv -f -- "$temporary_target" "$target"
        log "installed $target"
        failure_point "after_file_${target_labels[index]}"
    done
    failure_point after_files
    validate_packaged_default_available
}

state_value() {
    local file="$1" key="$2"
    run_as_postgres awk -F '\t' -v wanted="$key" '
        $1 == wanted {
            count++
            value = substr($0, index($0, "\t") + 1)
        }
        END {
            if (count != 1) exit 2
            print value
        }
    ' "$file"
}

load_requested_state() {
    state_directory="$requested_state_directory"
    require_path_text "state directory" "$state_directory"
    [[ -d "$state_directory" ]] || return 1
    [[ "$(canonical_directory "$state_directory")" == "$state_directory" ]] \
        || fail "state directory must be canonical"
    state_root="$(dirname -- "$state_directory")"
    validate_private_directory "$state_root"
    validate_private_directory "$state_directory"
    if [[ ! -e "$state_directory/state.tsv" && ! -L "$state_directory/state.tsv" ]]; then
        return 1
    fi
    validate_private_directory "$state_directory/backups"
    validate_private_file "$state_directory/state.tsv"
    validate_private_file "$state_directory/files.tsv"

    saved_state="$(state_value "$state_directory/state.tsv" state)" \
        || fail "state file has missing or duplicate state"
    saved_nonce="$(state_value "$state_directory/state.tsv" nonce)" \
        || fail "state file has missing or duplicate nonce"
    saved_data_directory="$(state_value "$state_directory/state.tsv" data_directory)" \
        || fail "state file has missing data_directory"
    saved_cluster_system_identifier="$(state_value "$state_directory/state.tsv" cluster_system_identifier)" \
        || fail "state file has missing cluster identity"
    saved_database_oid="$(state_value "$state_directory/state.tsv" database_oid)" \
        || fail "state file has missing database identity"
    saved_postmaster_start="$(state_value "$state_directory/state.tsv" postmaster_start_before)" \
        || fail "state file has missing postmaster identity"
    saved_postmaster_start_after="$(state_value "$state_directory/state.tsv" postmaster_start_after)"
    saved_package_digest="$(state_value "$state_directory/state.tsv" package_digest)" \
        || fail "state file has missing package digest"
    saved_packaged_version="$(state_value "$state_directory/state.tsv" packaged_version)"
    saved_packaged_build_id="$(state_value "$state_directory/state.tsv" packaged_build_id)"
    saved_extension_oid_before="$(state_value "$state_directory/state.tsv" extension_oid_before)"
    saved_extension_schema_oid_before="$(state_value "$state_directory/state.tsv" extension_schema_oid_before)"
    saved_extension_owner_oid_before="$(state_value "$state_directory/state.tsv" extension_owner_oid_before)"
    saved_extension_version_before="$(state_value "$state_directory/state.tsv" extension_version_before)"
    saved_expected_extension_owner_oid="$(state_value "$state_directory/state.tsv" expected_extension_owner_oid)"
    saved_lock_directory="$(state_value "$state_directory/state.tsv" lock_directory)" \
        || fail "state file has missing lock path"
    saved_role_existed="$(state_value "$state_directory/state.tsv" role_existed)"
    saved_role_oid_before="$(state_value "$state_directory/state.tsv" role_oid_before)"
    saved_role_had_direct_connect="$(state_value "$state_directory/state.tsv" role_had_direct_connect)"
    saved_role_oid_after="$(state_value "$state_directory/state.tsv" role_oid_after)"
    saved_role_created="$(state_value "$state_directory/state.tsv" role_created)"
    saved_config_mutating="$(state_value "$state_directory/state.tsv" config_mutating)"
    saved_auto_conf_before_digest="$(state_value "$state_directory/state.tsv" auto_conf_before_digest)"
    saved_auto_conf_before_mode="$(state_value "$state_directory/state.tsv" auto_conf_before_mode)"
    saved_auto_conf_before_uid="$(state_value "$state_directory/state.tsv" auto_conf_before_uid)"
    saved_auto_conf_before_gid="$(state_value "$state_directory/state.tsv" auto_conf_before_gid)"
    saved_config_after_digest="$(state_value "$state_directory/state.tsv" config_after_digest)"
    saved_preload_before="$(state_value "$state_directory/state.tsv" preload_before)"
    saved_preload_written="$(state_value "$state_directory/state.tsv" preload_written)"
    saved_max_workers_before="$(state_value "$state_directory/state.tsv" max_worker_processes_before)"
    saved_max_workers_written="$(state_value "$state_directory/state.tsv" max_worker_processes_written)"
    database="$(state_value "$state_directory/state.tsv" database)"
    worker_role="$(state_value "$state_directory/state.tsv" database_role)"
    saved_mode="$(state_value "$state_directory/state.tsv" mode)"
    saved_bind_address="$(state_value "$state_directory/state.tsv" bind_address)"
    saved_resp_port="$(state_value "$state_directory/state.tsv" resp_port)"
    saved_workers="$(state_value "$state_directory/state.tsv" workers)"
    saved_token_file="$(state_value "$state_directory/state.tsv" token_file)"
    saved_cache_entries="$(state_value "$state_directory/state.tsv" cache_entries)"
    saved_relation_states="$(state_value "$state_directory/state.tsv" relation_states)"
    saved_max_clients="$(state_value "$state_directory/state.tsv" max_clients)"
    saved_max_clients_per_worker="$(state_value "$state_directory/state.tsv" max_clients_per_worker)"
    saved_memory_budget_mb="$(state_value "$state_directory/state.tsv" memory_budget_mb)"
    [[ "$mode_explicit" != true || "$mode" == "$saved_mode" ]] || fail "--mode does not match pending state"
    [[ "$bind_address_explicit" != true || "$bind_address" == "$saved_bind_address" ]] || fail "--bind-address does not match pending state"
    [[ "$resp_port_explicit" != true || "$resp_port" == "$saved_resp_port" ]] || fail "--port does not match pending state"
    [[ "$workers_explicit" != true || "$workers" == "$saved_workers" ]] || fail "--workers does not match pending state"
    [[ "$cache_entries_explicit" != true || "$cache_entries" == "$saved_cache_entries" ]] || fail "--cache-entries does not match pending state"
    [[ "$relation_states_explicit" != true || "$relation_states" == "$saved_relation_states" ]] || fail "--relation-states does not match pending state"
    [[ "$max_clients_explicit" != true || "$max_clients" == "$saved_max_clients" ]] || fail "--max-clients does not match pending state"
    [[ "$max_clients_per_worker_explicit" != true || "$max_clients_per_worker" == "$saved_max_clients_per_worker" ]] || fail "--max-clients-per-worker does not match pending state"
    [[ "$memory_budget_mb_explicit" != true || "$memory_budget_mb" == "$saved_memory_budget_mb" ]] || fail "--memory-budget-mb does not match pending state"
    [[ "$token_file_explicit" != true || "$token_file" == "$saved_token_file" ]] || fail "--token-file does not match pending state"
    mode="$saved_mode"; bind_address="$saved_bind_address"; resp_port="$saved_resp_port"
    workers="$saved_workers"; token_file="$saved_token_file"
    cache_entries="$saved_cache_entries"; relation_states="$saved_relation_states"
    max_clients="$saved_max_clients"; max_clients_per_worker="$saved_max_clients_per_worker"
    memory_budget_mb="$saved_memory_budget_mb"
    mode_explicit=true; bind_address_explicit=true; resp_port_explicit=true
    workers_explicit=true; cache_entries_explicit=true; relation_states_explicit=true
    max_clients_explicit=true; max_clients_per_worker_explicit=true
    memory_budget_mb_explicit=true; token_file_explicit=true
    [[ "$saved_nonce" =~ ^[0-9a-f]{32}$ && "$saved_package_digest" =~ ^[0-9a-f]{64}$ ]] \
        || fail "state nonce or package digest is malformed"
    [[ "$saved_packaged_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && "$saved_packaged_build_id" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]] \
        || fail "state package version or build ID is malformed"
    [[ "$(state_value "$state_directory/state.tsv" state_directory)" == "$state_directory" ]] \
        || fail "state directory identity mismatch"
    case "$saved_state" in
        prepared | files_staged | restart_requested | restart_observed | activation_committed | complete | recovered) ;;
        *) fail "unknown installer state: $saved_state" ;;
    esac
    return 0
}

validate_lock_binding() {
    local owner_file="$lock_directory/owner.tsv" owner_pid owner_start live_start expected_nonce
    expected_nonce="${saved_nonce:-$lock_nonce}"
    validate_private_directory "$lock_directory"
    validate_private_file "$owner_file"
    [[ "$(state_value "$owner_file" nonce)" == "$expected_nonce" ]] \
        || fail "lock nonce does not match state"
    [[ "$(state_value "$owner_file" state_directory)" == "$state_directory" ]] \
        || fail "lock state path does not match state"
    [[ "$(state_value "$owner_file" cluster_system_identifier)" == "$cluster_system_identifier" ]] \
        || fail "lock cluster identity does not match live cluster"
    owner_pid="$(state_value "$owner_file" pid)"
    owner_start="$(state_value "$owner_file" process_start)"
    if live_start="$(process_start_marker "$owner_pid" 2>/dev/null)" && [[ "$live_start" == "$owner_start" && "$owner_pid" != "$$" ]]; then
        fail "install state is still owned by live process $owner_pid"
    fi
    lock_nonce="$expected_nonce"
    lock_owned=true
}

validate_state_binding() {
    [[ "$saved_data_directory" == "$data_directory" ]] \
        || fail "state data_directory does not match live cluster"
    [[ "$saved_cluster_system_identifier" == "$cluster_system_identifier" ]] \
        || fail "state system identifier does not match live cluster"
    [[ "$saved_database_oid" == "$database_oid" ]] \
        || fail "state database OID does not match live database"
    [[ "$saved_lock_directory" == "$lock_directory" ]] \
        || fail "state lock path does not match canonical PGDATA lock"
    validate_lock_binding
}

validate_journal_target() {
    local label="$1" target="$2"
    local expected_lib expected_share basename_value
    expected_lib="$(canonical_directory "$($pg_config_bin --pkglibdir)")/pg_local_cache.so"
    expected_share="$(canonical_directory "$($pg_config_bin --sharedir)/extension")"
    basename_value="$(basename -- "$target")"
    case "$label" in
        extension_so) [[ "$target" == "$expected_lib" ]] ;;
        extension_control) [[ "$target" == "$expected_share/pg_local_cache.control" ]] ;;
        extension_sql_*)
            [[ "$(dirname -- "$target")" == "$expected_share" && "$basename_value" =~ ^pg_local_cache--[0-9]+\.[0-9]+\.[0-9]+(--[0-9]+\.[0-9]+\.[0-9]+)?\.sql$ ]]
            ;;
        *) return 1 ;;
    esac || fail "journal target is outside the validated extension destinations: $target"
}

restore_extension_files() {
    local label action target backup installed_mode before_digest after_digest
    local before_mode before_uid before_gid after_uid after_gid current_digest current_mode current_uid current_gid temporary_target
    local manifest='' lines=0 journal_file trusted_backup
    stage_postgres_file "$state_directory/files.tsv"
    journal_file="$staged_file"
    while IFS=$'\t' read -r label action target backup installed_mode before_digest after_digest before_mode before_uid before_gid after_uid after_gid; do
        [[ -n "$label" && -n "$after_gid" ]] || fail "file journal contains a malformed row"
        validate_journal_target "$label" "$target"
        [[ "$action" == "unchanged" || "$action" == "created" || "$action" == "replaced" ]] \
            || fail "file journal contains an invalid action"
        [[ "$after_digest" =~ ^[0-9a-f]{64}$ && "$installed_mode" =~ ^0?[0-7]{3}$ ]] \
            || fail "file journal contains invalid installed metadata"
        manifest+="$label $installed_mode $after_digest\n"
        lines=$((lines + 1))
        temporary_target="${target}.pg_local_cache.${saved_nonce}"
        if [[ -e "$temporary_target" || -L "$temporary_target" ]]; then
            [[ -f "$temporary_target" && ! -L "$temporary_target" && "$(stat -c '%h' "$temporary_target")" == "1" && "$(sha256sum "$temporary_target" | awk '{print $1}')" == "$after_digest" && "$(stat -c '%a' "$temporary_target")" == "${installed_mode#0}" && "$(stat -c '%u' "$temporary_target")" == "$after_uid" && "$(stat -c '%g' "$temporary_target")" == "$after_gid" ]] \
                || fail "temporary install target drifted: $temporary_target"
            rm -f -- "$temporary_target"
        fi
        if [[ ! -e "$target" && ! -L "$target" ]]; then
            [[ "$action" == "created" ]] && continue
            fail "journaled install target is missing: $target"
        fi
        [[ -f "$target" && ! -L "$target" && "$(stat -c '%h' "$target")" == "1" ]] \
            || fail "journaled install target type/link count drifted: $target"
        current_digest="$(sha256sum "$target" | awk '{print $1}')"
        current_mode="$(stat -c '%a' "$target")"
        current_uid="$(stat -c '%u' "$target")"
        current_gid="$(stat -c '%g' "$target")"
        case "$action" in
            unchanged)
                [[ "$current_digest" == "$before_digest" && "$current_mode" == "$before_mode" && "$current_uid" == "$before_uid" && "$current_gid" == "$before_gid" ]] \
                    || fail "unchanged target drifted: $target"
                ;;
            created)
                [[ "$current_digest" == "$after_digest" && "$current_mode" == "${installed_mode#0}" && "$current_uid" == "$after_uid" && "$current_gid" == "$after_gid" ]] \
                    || fail "created target drifted; refusing removal: $target"
                rm -f -- "$target"
                ;;
            replaced)
                validate_private_file "$backup"
                stage_postgres_file "$backup"
                trusted_backup="$staged_file"
                [[ "$backup" == "$state_directory/backups/${label}.before" && "$(sha256sum "$trusted_backup" | awk '{print $1}')" == "$before_digest" ]] \
                    || fail "backup identity mismatch for $target"
                if [[ "$current_digest" == "$before_digest" && "$current_mode" == "$before_mode" && "$current_uid" == "$before_uid" && "$current_gid" == "$before_gid" ]]; then
                    continue
                fi
                [[ "$current_digest" == "$after_digest" && "$current_mode" == "${installed_mode#0}" && "$current_uid" == "$after_uid" && "$current_gid" == "$after_gid" ]] \
                    || fail "replaced target drifted; refusing restore: $target"
                temporary_target="${target}.pg_local_cache_restore.${saved_nonce}"
                install -m "$before_mode" -o "$before_uid" -g "$before_gid" "$trusted_backup" "$temporary_target"
                mv -f -- "$temporary_target" "$target"
                ;;
        esac
    done < "$journal_file"
    ((lines >= 3)) || fail "file journal is incomplete"
    manifest+="build_id 0644 $(printf '%s' "$saved_packaged_build_id" | sha256sum | awk '{print $1}')\n"
    [[ "$(printf '%b' "$manifest" | LC_ALL=C sort | sha256sum | awk '{print $1}')" == "$saved_package_digest" ]] \
        || fail "file journal package digest does not match state"
}

validate_installed_files() {
    local label action target backup installed_mode before_digest after_digest
    local before_mode before_uid before_gid after_uid after_gid expected_digest expected_mode expected_uid expected_gid
    local manifest='' lines=0 journal_file
    stage_postgres_file "$state_directory/files.tsv"
    journal_file="$staged_file"
    while IFS=$'\t' read -r label action target backup installed_mode before_digest after_digest before_mode before_uid before_gid after_uid after_gid; do
        [[ -n "$label" && -n "$after_gid" ]] || fail "file journal contains a malformed row"
        validate_journal_target "$label" "$target"
        manifest+="$label $installed_mode $after_digest\n"
        lines=$((lines + 1))
        if [[ "$action" == "unchanged" ]]; then
            expected_digest="$before_digest"; expected_mode="$before_mode"; expected_uid="$before_uid"; expected_gid="$before_gid"
        else
            expected_digest="$after_digest"; expected_mode="${installed_mode#0}"; expected_uid="$after_uid"; expected_gid="$after_gid"
        fi
        [[ -f "$target" && ! -L "$target" && "$(stat -c '%h' "$target")" == "1" \
            && "$(sha256sum "$target" | awk '{print $1}')" == "$expected_digest" \
            && "$(stat -c '%a' "$target")" == "$expected_mode" \
            && "$(stat -c '%u' "$target")" == "$expected_uid" \
            && "$(stat -c '%g' "$target")" == "$expected_gid" ]] \
            || fail "installed target identity drifted: $target"
        if [[ "$action" == "replaced" ]]; then
            validate_private_file "$backup"
            [[ "$backup" == "$state_directory/backups/${label}.before" && "$(postgres_file_digest "$backup")" == "$before_digest" ]] \
                || fail "backup identity mismatch for $target"
        fi
    done < "$journal_file"
    ((lines >= 3)) || fail "file journal is incomplete"
    manifest+="build_id 0644 $(printf '%s' "$saved_packaged_build_id" | sha256sum | awk '{print $1}')\n"
    [[ "$(printf '%b' "$manifest" | LC_ALL=C sort | sha256sum | awk '{print $1}')" == "$saved_package_digest" ]] \
        || fail "file journal package digest does not match state"
}

restore_configuration() {
    local backup="$state_directory/backups/postgresql.auto.conf.before"
    local current_digest current_mode current_uid current_gid trusted_backup
    validate_private_file "$backup"
    stage_postgres_file "$backup"
    trusted_backup="$staged_file"
    [[ "$(sha256sum "$trusted_backup" | awk '{print $1}')" == "$saved_auto_conf_before_digest" ]] \
        || fail "postgresql.auto.conf backup digest mismatch"
    [[ -f "$auto_conf" && ! -L "$auto_conf" && "$(stat -c '%h' "$auto_conf")" == "1" ]] \
        || fail "live postgresql.auto.conf type/link count drifted"
    current_digest="$(postgres_file_digest "$auto_conf")"
    current_mode="$(stat -c '%a' "$auto_conf")"
    current_uid="$(stat -c '%u' "$auto_conf")"
    current_gid="$(stat -c '%g' "$auto_conf")"
    if [[ "$current_digest" == "$saved_auto_conf_before_digest" ]]; then
        [[ "$current_mode" == "$saved_auto_conf_before_mode" && "$current_uid" == "$saved_auto_conf_before_uid" && "$current_gid" == "$saved_auto_conf_before_gid" ]] \
            || fail "postgresql.auto.conf metadata drifted"
        return
    fi
    [[ "$saved_config_mutating" == "true" ]] \
        || fail "postgresql.auto.conf changed outside the journaled config mutation"
    [[ -n "$saved_config_after_digest" ]] \
        || fail "configuration mutation outcome was not journaled; refusing automatic restore"
    [[ "$current_digest" == "$saved_config_after_digest" ]] \
        || fail "postgresql.auto.conf digest drifted after staging"
    [[ "$saved_auto_conf_before_uid" == "$postgres_uid" \
        && "$saved_auto_conf_before_gid" == "$postgres_gid" ]] \
        || fail "journaled postgresql.auto.conf owner does not match $postgres_os_user"
    private_replace "$auto_conf" "$saved_auto_conf_before_mode" < "$trusted_backup"
    psql_scalar "SELECT pg_catalog.pg_reload_conf()" >/dev/null
}

restore_database_baseline() {
    local current
    current="$(psql_base --quiet --tuples-only --no-align --set worker_role="$worker_role" <<'SQL'
SELECT coalesce(r.oid::text, ''),
       coalesce(EXISTS (
           SELECT 1 FROM pg_catalog.aclexplode(coalesce(d.datacl, pg_catalog.acldefault('d', d.datdba))) AS a
            WHERE a.grantee = r.oid AND a.privilege_type = 'CONNECT'
       )::text, 'false')
  FROM pg_catalog.pg_database AS d
  LEFT JOIN pg_catalog.pg_roles AS r ON r.rolname = :'worker_role'
 WHERE d.datname = pg_catalog.current_database();
SQL
)"
    local current_oid current_direct
    IFS='|' read -r current_oid current_direct <<< "$current"
    if [[ "$saved_role_existed" == "true" ]]; then
        [[ "$current_oid" == "$saved_role_oid_before" ]] \
            || fail "worker role identity drifted; refusing ACL restore"
        if [[ "$saved_role_had_direct_connect" == "false" && ( "$current_direct" == "t" || "$current_direct" == "true" ) ]]; then
            psql_base --quiet --set worker_role="$worker_role" --set cache_database="$database" <<'SQL'
REVOKE CONNECT ON DATABASE :"cache_database" FROM :"worker_role";
SQL
        fi
    elif [[ -n "$current_oid" ]]; then
        if [[ -n "$saved_role_oid_after" ]]; then
            [[ "$current_oid" == "$saved_role_oid_after" ]] \
                || fail "created worker role identity drifted; refusing removal"
        else
            [[ "$(psql_base --quiet --tuples-only --no-align --set worker_role="$worker_role" <<'SQL'
SELECT coalesce(pg_catalog.shobj_description(oid, 'pg_authid'), '')
  FROM pg_catalog.pg_roles
 WHERE rolname = :'worker_role';
SQL
)" == "pg_local_cache installer $saved_nonce" ]] \
                || fail "created worker role has no matching installer marker; refusing removal"
        fi
        psql_base --quiet --set worker_role="$worker_role" \
            --set cache_database="$database" <<'SQL'
REVOKE CONNECT ON DATABASE :"cache_database" FROM :"worker_role";
DROP ROLE :"worker_role";
SQL
    fi
}

release_matching_lock() {
    validate_lock_binding
    rm -f -- "$lock_directory/owner.tsv"
    rmdir -- "$lock_directory"
    lock_owned=false
}

recover_pre_restart_state() {
    [[ "$postmaster_start_time" == "$saved_postmaster_start" ]] \
        || fail "postmaster identity changed; automatic recovery is forbidden; use verify/manual recovery"
    [[ "$saved_state" == "prepared" || "$saved_state" == "files_staged" ]] \
        || fail "state $saved_state is past the automatic recovery boundary; use verify/manual recovery"
    restore_configuration
    restore_database_baseline
    restore_extension_files
    current_state=recovered
    role_existed="$saved_role_existed"; role_oid_before="$saved_role_oid_before"
    role_had_direct_connect="$saved_role_had_direct_connect"; role_oid_after="$saved_role_oid_after"
    role_created="$saved_role_created"; config_mutating="$saved_config_mutating"
    initial_postmaster_start_time="$saved_postmaster_start"; postmaster_start_after="$saved_postmaster_start_after"
    auto_conf_before_digest="$saved_auto_conf_before_digest"; auto_conf_before_mode="$saved_auto_conf_before_mode"
    auto_conf_before_uid="$saved_auto_conf_before_uid"; auto_conf_before_gid="$saved_auto_conf_before_gid"
    config_after_digest="$saved_config_after_digest"; package_digest="$saved_package_digest"
    write_state recovered
    release_matching_lock
    log "recovery PASS: exact pre-restart state restored; state retained at $state_directory"
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
    if [[ "$role_existed" == "false" ]]; then
        psql_base --quiet \
            --set worker_role="$worker_role" \
            --set cache_database="$database" \
            --set role_marker="pg_local_cache installer $lock_nonce" <<'SQL'
CREATE ROLE :"worker_role" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD NULL;
COMMENT ON ROLE :"worker_role" IS :'role_marker';
GRANT CONNECT ON DATABASE :"cache_database" TO :"worker_role";
SQL
    else
        psql_base --quiet \
            --set worker_role="$worker_role" \
            --set cache_database="$database" <<'SQL'
GRANT CONNECT ON DATABASE :"cache_database" TO :"worker_role";
SQL
    fi
    role_oid_after="$(psql_base --quiet --tuples-only --no-align --set worker_role="$worker_role" <<'SQL'
SELECT oid::text FROM pg_catalog.pg_roles WHERE rolname = :'worker_role';
SQL
)"
    [[ "$role_oid_after" =~ ^[0-9]+$ ]] || fail "could not record worker role identity"
    if [[ "$role_existed" == "false" ]]; then role_created=true; else role_created=false; fi
    failure_point after_role_commit
    write_state prepared
    if [[ "$role_created" == "true" ]]; then
        psql_base --quiet --set worker_role="$worker_role" <<'SQL'
COMMENT ON ROLE :"worker_role" IS NULL;
SQL
    fi
    failure_point after_role
}

write_configuration() {
    if [[ "$dry_run" == true ]]; then
        log "dry-run: would append pg_local_cache without replacing existing preload libraries"
        return
    fi
    local configured_port="$resp_port"
    local configured_token="$token_file"
    local register_placeholders=false
    local configuration_written=false
    if [[ "$mode" == "sql-only" ]]; then
        configured_port=0
    fi
    if [[ "$extension_was_preloaded" != true ]]; then
        register_placeholders=true
    fi
    config_mutating=true
    write_state prepared
    failure_point before_configuration
    if psql_base --quiet \
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
        --set token_file="$configured_token" \
        --set register_placeholders="$register_placeholders" <<'SQL'
\if :register_placeholders
SET pg_local_cache.database = '';
SET pg_local_cache.role = '';
SET pg_local_cache.bind_address = '';
SET pg_local_cache.port = '';
SET pg_local_cache.workers = '';
SET pg_local_cache.cache_entries = '';
SET pg_local_cache.relation_states = '';
SET pg_local_cache.max_clients = '';
SET pg_local_cache.max_clients_per_worker = '';
SET pg_local_cache.memory_budget_mb = '';
SET pg_local_cache.auth_token_file = '';
SET pg_local_cache.auth_token = '';
\endif
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
        configuration_written=true
    fi
    config_after_digest="$(postgres_file_digest "$auto_conf")"
    write_state prepared
    if [[ "$configuration_written" != true ]]; then
        fail "configuration staging failed; the pre-restart journal will restore prior state"
    fi
    local config_errors
    if [[ "$extension_was_preloaded" == true ]]; then
        config_errors="$(configuration_error_count)"
    else
        config_errors="$(configuration_error_count true)"
    fi
    if [[ "$config_errors" != "0" ]]; then
        fail "configuration validation failed with $config_errors error(s); the pre-restart journal will restore prior state"
    fi
    write_state files_staged
    failure_point after_configuration
    log "configuration staged and parsed successfully; pending restart is expected"
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

wait_until_extension_ready() {
    local deadline=$((SECONDS + readiness_timeout)) ready
    while ((SECONDS < deadline)); do
        ready="$(psql_scalar "SELECT (local_cache.health() ->> 'ready')::boolean" 2>/dev/null || true)"
        [[ "$ready" == "t" ]] && return 0
        sleep 1
    done
    return 1
}

restart_or_restore() {
    [[ "$restart_method" != "none" ]] || return 0
    if [[ "$dry_run" == true ]]; then
        log "dry-run: would perform one $restart_method restart"
        return
    fi
    local started elapsed
    started="$SECONDS"
    write_state restart_requested
    failure_point after_restart_requested
    if ! raw_restart; then
        fail "restart command failed; automatic rollback is forbidden after restart invocation (state: $state_directory)"
    fi
    if ! wait_until_ready; then
        fail "PostgreSQL did not become SQL-ready within ${readiness_timeout}s; automatic rollback is forbidden (state: $state_directory)"
    fi
    postmaster_start_after="$(psql_scalar "SELECT pg_catalog.pg_postmaster_start_time()::text")"
    [[ -n "$postmaster_start_after" && "$postmaster_start_after" != "$initial_postmaster_start_time" ]] \
        || fail "restart returned ready without a changed postmaster identity; use state-bound verify/manual recovery"
    write_state restart_observed
    failure_point after_restart_observed
    elapsed=$((SECONDS - started))
    log "PostgreSQL became SQL-ready after ${elapsed}s"
    if ((elapsed > restart_goal_seconds)); then
        warn "restart exceeded the ${restart_goal_seconds}s operational goal"
    else
        log "restart met the ${restart_goal_seconds}s operational goal"
    fi
}

catalog_database_oid=""
catalog_extension_oid=""
catalog_schema_oid=""
catalog_owner_oid=""
catalog_version=""
catalog_schema_name=""
catalog_schema_grant=""
catalog_mapping_grant=""

read_catalog_identity() {
    local identity
    identity="$(psql_base --quiet --tuples-only --no-align \
        --set worker_role="$worker_role" <<'SQL'
SELECT d.oid::text,
       coalesce(e.oid::text, ''),
       coalesce(e.extnamespace::text, ''),
       coalesce(e.extowner::text, ''),
       coalesce(e.extversion, ''),
       coalesce(n.nspname, ''),
       CASE WHEN r.oid IS NULL OR n.oid IS NULL THEN false ELSE EXISTS (
           SELECT 1
             FROM pg_catalog.aclexplode(coalesce(n.nspacl, pg_catalog.acldefault('n', n.nspowner))) AS a
            WHERE a.grantee = r.oid AND a.privilege_type = 'USAGE'
       ) END,
       CASE WHEN r.oid IS NULL OR c.oid IS NULL THEN false ELSE EXISTS (
           SELECT 1
             FROM pg_catalog.aclexplode(coalesce(c.relacl, pg_catalog.acldefault('r', c.relowner))) AS a
            WHERE a.grantee = r.oid AND a.privilege_type = 'SELECT'
       ) END
  FROM pg_catalog.pg_database AS d
  LEFT JOIN pg_catalog.pg_extension AS e ON e.extname = 'pg_local_cache'
  LEFT JOIN pg_catalog.pg_namespace AS n ON n.oid = e.extnamespace
  LEFT JOIN pg_catalog.pg_class AS c ON c.relnamespace = n.oid AND c.relname = 'mapping'
  LEFT JOIN pg_catalog.pg_roles AS r ON r.rolname = :'worker_role'
 WHERE d.datname = pg_catalog.current_database();
SQL
)"
    IFS='|' read -r catalog_database_oid catalog_extension_oid catalog_schema_oid \
        catalog_owner_oid catalog_version catalog_schema_name catalog_schema_grant \
        catalog_mapping_grant <<< "$identity"
}

catalog_matches_baseline() {
    [[ "$catalog_database_oid" == "$database_oid" \
        && "$catalog_extension_oid" == "$extension_oid_before" \
        && "$catalog_schema_oid" == "$extension_schema_oid_before" \
        && "$catalog_owner_oid" == "$extension_owner_oid_before" \
        && "$catalog_version" == "$extension_version_before" ]]
}

catalog_matches_target() {
    [[ "$catalog_database_oid" == "$database_oid" \
        && -n "$catalog_extension_oid" \
        && "$catalog_version" == "$packaged_version" \
        && "$catalog_schema_name" == "local_cache" \
        && "$catalog_owner_oid" == "$expected_extension_owner_oid" \
        && ( "$catalog_schema_grant" == "t" || "$catalog_schema_grant" == "true" ) \
        && ( "$catalog_mapping_grant" == "t" || "$catalog_mapping_grant" == "true" ) ]] \
        || return 1
    if [[ -n "$extension_oid_before" ]]; then
        [[ "$catalog_extension_oid" == "$extension_oid_before" \
            && "$catalog_schema_oid" == "$extension_schema_oid_before" \
            && "$catalog_owner_oid" == "$extension_owner_oid_before" ]] \
            || return 1
    fi
    return 0
}

run_activation_transaction() {
    local installed_label="${extension_version_before:-absent}"
    failure_point before_activation_sql
    if [[ -z "$extension_oid_before" ]]; then
        psql_base --quiet \
            --set database_oid="$database_oid" \
            --set target_version="$packaged_version" \
            --set worker_role="$worker_role" <<'SQL'
BEGIN ISOLATION LEVEL SERIALIZABLE;
SELECT (d.oid = :'database_oid'::oid AND NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_extension WHERE extname = 'pg_local_cache'
       ))::text AS pglc_identity_ok
  FROM pg_catalog.pg_database AS d
 WHERE d.datname = pg_catalog.current_database() \gset
\if :pglc_identity_ok
\else
  ROLLBACK;
  \quit 3
\endif
CREATE EXTENSION pg_local_cache VERSION :'target_version';
GRANT USAGE ON SCHEMA local_cache TO :"worker_role";
GRANT SELECT ON TABLE local_cache.mapping TO :"worker_role";
COMMIT;
SQL
    elif [[ "$extension_version_before" == "$packaged_version" ]]; then
        psql_base --quiet \
            --set database_oid="$database_oid" \
            --set extension_oid="$extension_oid_before" \
            --set schema_oid="$extension_schema_oid_before" \
            --set owner_oid="$extension_owner_oid_before" \
            --set installed_version="$extension_version_before" \
            --set worker_role="$worker_role" <<'SQL'
BEGIN ISOLATION LEVEL SERIALIZABLE;
SELECT (d.oid = :'database_oid'::oid AND e.oid = :'extension_oid'::oid
        AND e.extnamespace = :'schema_oid'::oid AND e.extowner = :'owner_oid'::oid
        AND e.extversion = :'installed_version')::text AS pglc_identity_ok
  FROM pg_catalog.pg_database AS d
  JOIN pg_catalog.pg_extension AS e ON e.extname = 'pg_local_cache'
 WHERE d.datname = pg_catalog.current_database() \gset
\if :pglc_identity_ok
\else
  ROLLBACK;
  \quit 3
\endif
GRANT USAGE ON SCHEMA local_cache TO :"worker_role";
GRANT SELECT ON TABLE local_cache.mapping TO :"worker_role";
COMMIT;
SQL
    else
        psql_base --quiet \
            --set database_oid="$database_oid" \
            --set extension_oid="$extension_oid_before" \
            --set schema_oid="$extension_schema_oid_before" \
            --set owner_oid="$extension_owner_oid_before" \
            --set installed_version="$extension_version_before" \
            --set target_version="$packaged_version" \
            --set worker_role="$worker_role" <<'SQL'
BEGIN ISOLATION LEVEL SERIALIZABLE;
SELECT (d.oid = :'database_oid'::oid AND e.oid = :'extension_oid'::oid
        AND e.extnamespace = :'schema_oid'::oid AND e.extowner = :'owner_oid'::oid
        AND e.extversion = :'installed_version'
        AND EXISTS (
            SELECT 1
              FROM pg_catalog.pg_extension_update_paths('pg_local_cache') AS p
             WHERE p.source = :'installed_version'
               AND p.target = :'target_version'
               AND p.path IS NOT NULL
        ))::text AS pglc_identity_ok
  FROM pg_catalog.pg_database AS d
  JOIN pg_catalog.pg_extension AS e ON e.extname = 'pg_local_cache'
 WHERE d.datname = pg_catalog.current_database() \gset
\if :pglc_identity_ok
\else
  ROLLBACK;
  \quit 3
\endif
ALTER EXTENSION pg_local_cache UPDATE TO :'target_version';
GRANT USAGE ON SCHEMA local_cache TO :"worker_role";
GRANT SELECT ON TABLE local_cache.mapping TO :"worker_role";
COMMIT;
SQL
    fi || fail "activation $installed_label -> $packaged_version failed; live files are retained; state: $state_directory"
    failure_point after_activation_sql_commit
}

activate_and_verify() {
    if [[ "$dry_run" == true ]]; then
        log "dry-run: would CREATE EXTENSION and verify health"
        return
    fi
    local active_preload health_ready state binary_identity lifecycle
    active_preload="$(psql_scalar "SHOW shared_preload_libraries")"
    preload_contains_extension "$active_preload" \
        || fail "pg_local_cache is configured but not active; restart PostgreSQL first"
    binary_identity="$(psql_scalar "SELECT pg_catalog.current_setting('pg_local_cache.binary_version'), pg_catalog.current_setting('pg_local_cache.binary_build_id')")"
    [[ "$binary_identity" == "$packaged_version|$packaged_build_id" ]] \
        || fail "active binary identity $binary_identity does not match package $packaged_version|$packaged_build_id"
    saved_package_digest="${saved_package_digest:-$package_digest}"
    saved_packaged_build_id="${saved_packaged_build_id:-$packaged_build_id}"
    validate_installed_files
    read_catalog_identity
    if catalog_matches_target; then
        lifecycle="already current"
    elif [[ "$current_state" == "activation_committed" ]]; then
        fail "activation_committed state does not match the live catalog/grants"
    elif catalog_matches_baseline; then
        run_activation_transaction
        read_catalog_identity
        catalog_matches_target \
            || fail "activation committed but target catalog/grants are incomplete; state: $state_directory"
        if [[ -z "$extension_version_before" ]]; then
            lifecycle=fresh
        elif [[ "$extension_version_before" == "$packaged_version" ]]; then
            lifecycle="already current"
        else
            lifecycle="$extension_version_before -> $packaged_version"
        fi
    else
        fail "extension identity changed after staging; refusing catalog mutation"
    fi
    write_state activation_committed
    failure_point after_activation_commit
    wait_until_extension_ready \
        || fail "extension did not become healthy within ${readiness_timeout}s"
    state="$(psql_scalar "SELECT current_setting('pg_local_cache.port'), (SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE backend_type = 'pg_local_cache RESP worker')")"
    if [[ "$mode" == "sql-only" ]]; then
        [[ "$state" == "0|0" ]] \
            || fail "SQL-only verification expected port=0 and zero RESP workers, got $state"
    else
        [[ "$state" == "${resp_port}|${workers}" ]] \
            || fail "RESP verification expected ${workers} workers on port ${resp_port}, got $state"
    fi
    write_state complete
    release_matching_lock
    log "verification PASS: $lifecycle; extension active, health ready, mode $mode"
}

adopt_saved_state() {
    current_state="$saved_state"
    lock_nonce="$saved_nonce"
    package_digest="$saved_package_digest"
    packaged_version="$saved_packaged_version"
    packaged_build_id="$saved_packaged_build_id"
    extension_oid_before="$saved_extension_oid_before"
    extension_schema_oid_before="$saved_extension_schema_oid_before"
    extension_owner_oid_before="$saved_extension_owner_oid_before"
    extension_version_before="$saved_extension_version_before"
    expected_extension_owner_oid="$saved_expected_extension_owner_oid"
    initial_postmaster_start_time="$saved_postmaster_start"
    postmaster_start_after="$saved_postmaster_start_after"
    role_existed="$saved_role_existed"
    role_oid_before="$saved_role_oid_before"
    role_had_direct_connect="$saved_role_had_direct_connect"
    role_oid_after="$saved_role_oid_after"
    role_created="$saved_role_created"
    config_mutating="$saved_config_mutating"
    auto_conf_before_digest="$saved_auto_conf_before_digest"
    auto_conf_before_mode="$saved_auto_conf_before_mode"
    auto_conf_before_uid="$saved_auto_conf_before_uid"
    auto_conf_before_gid="$saved_auto_conf_before_gid"
    config_after_digest="$saved_config_after_digest"
    preload_before="$saved_preload_before"
    preload_after="$saved_preload_written"
    max_workers_before="$saved_max_workers_before"
    max_workers_after="$saved_max_workers_written"
}

remove_incomplete_state_directory() {
    local owner_pid="$1" entry name uid mode
    local -a files=()
    [[ -e "$state_directory" || -L "$state_directory" ]] || return 0
    validate_private_directory "$state_root"
    validate_private_directory "$state_directory"
    [[ ! -e "$state_directory/state.tsv" && ! -L "$state_directory/state.tsv" ]] \
        || fail "prepared state exists; refusing incomplete-state cleanup"
    while IFS= read -r -d '' entry; do
        name="${entry##*/}"
        case "$name" in
            backups)
                validate_private_directory "$entry"
                while IFS= read -r -d '' entry; do
                    name="${entry##*/}"
                    [[ "$name" == "postgresql.auto.conf.before" || "$name" =~ ^[A-Za-z0-9_]+\.before$ ]] \
                        || fail "incomplete state contains unexpected backup: $entry"
                    validate_private_file "$entry"
                    files+=("$entry")
                done < <(find "$entry" -mindepth 1 -maxdepth 1 -print0)
                ;;
            files.tsv)
                validate_private_file "$entry"
                files+=("$entry")
                ;;
            "files.tsv.tmp.${owner_pid}" | "state.tsv.tmp.${owner_pid}")
                [[ -f "$entry" && ! -L "$entry" && "$(stat -c '%h' "$entry")" == "1" ]] \
                    || fail "incomplete state temporary file is not trusted: $entry"
                uid="$(stat -c '%u' "$entry")"; mode="$(stat -c '%a' "$entry")"
                [[ ( "$uid" == "$postgres_uid" || "$uid" == "$installer_uid" ) && "$mode" == "600" ]] \
                    || fail "incomplete state temporary file has unsafe ownership or mode: $entry"
                files+=("$entry")
                ;;
            *) fail "incomplete state contains unexpected entry: $entry" ;;
        esac
    done < <(find "$state_directory" -mindepth 1 -maxdepth 1 -print0)
    ((${#files[@]} == 0)) || rm -f -- "${files[@]}"
    [[ ! -d "$state_directory/backups" ]] || rmdir -- "$state_directory/backups"
    rmdir -- "$state_directory" \
        || fail "incomplete state contains unexpected entries; refusing removal"
}

recover_ownerless_lock() {
    local owner_file="$lock_directory/owner.tsv" owner_pid owner_start live_start owner_nonce state_name
    validate_private_directory "$lock_directory"
    if [[ -e "$owner_file" || -L "$owner_file" ]]; then
        validate_private_file "$owner_file"
        [[ "$(state_value "$owner_file" state_directory)" == "$state_directory" ]] \
            || fail "ownerless recovery state path does not match lock"
        [[ "$(state_value "$owner_file" cluster_system_identifier)" == "$cluster_system_identifier" ]] \
            || fail "ownerless recovery cluster identity does not match lock"
        owner_nonce="$(state_value "$owner_file" nonce)"
        [[ "$owner_nonce" =~ ^[0-9a-f]{32}$ ]] || fail "lock nonce is malformed"
        state_root="$(canonical_directory "$state_root")"
        [[ "$(dirname -- "$state_directory")" == "$state_root" ]] \
            || fail "ownerless recovery state path is outside --state-root"
        state_name="$(basename -- "$state_directory")"
        [[ "$state_name" =~ ^install-[0-9]{8}T[0-9]{6}Z-${owner_nonce}$ ]] \
            || fail "ownerless recovery state name does not match lock nonce"
        owner_pid="$(state_value "$owner_file" pid)"
        owner_start="$(state_value "$owner_file" process_start)"
        if live_start="$(process_start_marker "$owner_pid" 2>/dev/null)" && [[ "$live_start" == "$owner_start" ]]; then
            fail "install lock is still owned by live process $owner_pid"
        fi
        remove_incomplete_state_directory "$owner_pid"
        rm -f -- "$owner_file"
    elif [[ -e "$state_directory" || -L "$state_directory" ]]; then
        fail "incomplete state exists without a trusted lock owner"
    fi
    rmdir -- "$lock_directory" \
        || fail "ownerless lock contains unexpected entries; refusing removal"
    log "recovery PASS: removed stale lock with no prepared journal"
}

verify_command() {
    local index
    validate_state_binding
    adopt_saved_state
    prepare_release_files
    [[ "$package_digest" == "$saved_package_digest" ]] \
        || fail "release package digest does not match state"
    inspect_targets
    for index in "${!target_actions[@]}"; do
        [[ "${target_actions[index]}" == "unchanged" ]] \
            || fail "installed file identity does not match the state-bound package: ${target_files[index]}"
    done
    validate_installed_files
    case "$saved_state" in
        prepared) fail "installation did not finish staging; use recover" ;;
        files_staged)
            [[ "$postmaster_start_time" != "$saved_postmaster_start" ]] \
                || fail "PostgreSQL has not restarted; recover remains available"
            postmaster_start_after="$postmaster_start_time"
            write_state restart_observed
            ;;
        restart_requested)
            [[ "$postmaster_start_time" != "$saved_postmaster_start" ]] \
                || fail "restart was requested but postmaster identity is unchanged; automatic recovery is forbidden"
            postmaster_start_after="$postmaster_start_time"
            write_state restart_observed
            ;;
        restart_observed | activation_committed)
            [[ -n "$saved_postmaster_start_after" && "$postmaster_start_time" == "$saved_postmaster_start_after" ]] \
                || fail "postmaster identity drifted after restart observation"
            ;;
        complete | recovered) fail "state is terminal: $saved_state" ;;
    esac
    activate_and_verify
}

verify_current_command() {
    local active_preload binary_identity health_ready state
    read_package_identity
    active_preload="$(psql_scalar "SHOW shared_preload_libraries")"
    preload_contains_extension "$active_preload" \
        || fail "pg_local_cache is not active"
    binary_identity="$(psql_scalar "SELECT pg_catalog.current_setting('pg_local_cache.binary_version'), pg_catalog.current_setting('pg_local_cache.binary_build_id')")"
    [[ "$binary_identity" == "$packaged_version|$packaged_build_id" ]] \
        || fail "active binary identity $binary_identity does not match package $packaged_version|$packaged_build_id"
    capture_extension_baseline
    read_catalog_identity
    catalog_matches_target \
        || fail "live catalog version/identity/grants do not match the package"
    health_ready="$(psql_scalar "SELECT (local_cache.health() ->> 'ready')::boolean")"
    [[ "$health_ready" == "t" ]] || fail "health().ready is false"
    state="$(psql_scalar "SELECT current_setting('pg_local_cache.port'), (SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE backend_type = 'pg_local_cache RESP worker')")"
    if [[ "$mode" == "sql-only" ]]; then
        [[ "$state" == "0|0" ]] || fail "SQL-only verification expected port=0 and zero RESP workers, got $state"
    else
        [[ "$state" == "${resp_port}|${workers}" ]] || fail "RESP verification expected ${workers} workers on port ${resp_port}, got $state"
    fi
    log "verification PASS: already current; read-only; mode $mode"
}

recover_command() {
    validate_state_binding
    adopt_saved_state
    recover_pre_restart_state
}

install_command() {
    preflight
    if [[ "$dry_run" == true ]]; then
        log "dry-run complete; no PostgreSQL restart was performed; no build, state, lock, file, role, ACL or config writes"
        return
    fi
    prepare_release_files
    inspect_targets
    create_state_journal
    install_extension_files
    ensure_worker_role
    write_configuration
    restart_or_restore
    if [[ "$restart_method" == "none" ]]; then
        log "online stage complete; no PostgreSQL restart was performed"
        log "restart the cluster once, then run: $program_name verify --state-directory $state_directory"
        return
    fi
    activate_and_verify
}

main() {
    parse_arguments "$@"
    postgres_uid="$(id -u "$postgres_os_user")" \
        || fail "operating-system user does not exist: $postgres_os_user"
    postgres_gid="$(id -g "$postgres_os_user")"
    validate_options
    if [[ "$command_name" == "recover" || ( "$command_name" == "verify" && -n "$requested_state_directory" ) ]]; then
        if ! load_requested_state; then
            [[ "$command_name" == "recover" ]] \
                || fail "state directory does not exist: $requested_state_directory"
            state_directory="$requested_state_directory"
            preflight
            recover_ownerless_lock
            return
        fi
        validate_options
    fi
    case "$command_name" in
        preflight) preflight ;;
        install) install_command ;;
        verify)
            preflight
            if [[ -n "$requested_state_directory" ]]; then verify_command; else verify_current_command; fi
            ;;
        recover)
            preflight
            recover_command
            ;;
    esac
}

main "$@"
