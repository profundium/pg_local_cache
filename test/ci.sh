#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
    echo "run this script as root" >&2
    exit 1
fi
PG=${1:-}
case "$PG" in
    14|15|16|17|18) ;;
    *) echo "usage: sudo test/ci.sh <14|15|16|17|18>" >&2; exit 2 ;;
esac
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
temp=$(mktemp -d)
chmod 0755 "$temp"
cluster_data="/var/lib/postgresql/$PG/ci"
token_file="$cluster_data/pglc.token"
pg_bindir="/usr/lib/postgresql/$PG/bin"
pg_config="$pg_bindir/pg_config"
psql="$pg_bindir/psql"
cluster_created=0
cleanup() {
    local status=$?
    if (( cluster_created )); then
        pg_ctlcluster "$PG" ci stop >/dev/null 2>&1 || true
    fi
    rm -rf -- "$temp"
    exit "$status"
}
trap cleanup EXIT
retry_apt() {
    local attempt
    for attempt in 1 2 3 4 5; do
        if apt-get update && apt-get install -y "$@"; then
            return 0
        fi
        echo "apt attempt $attempt/5 failed" >&2
        sleep $((attempt * 4))
    done
    return 1
}
retry_apt postgresql-common
for attempt in 1 2 3 4 5; do
    if /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y; then
        break
    fi
    [[ "$attempt" -lt 5 ]] || exit 1
    echo "PGDG repository setup attempt $attempt/5 failed" >&2
    sleep $((attempt * 4))
done
retry_apt "postgresql-$PG" "postgresql-server-dev-$PG" build-essential python3 git
# Runs as root over a checkout owned by another user (sudo on CI runners).
git config --global --add safe.directory "$repo"
# Build an isolated working copy so PGXS artifacts do not dirty the checkout.
mkdir "$temp/current"
# Copy tracked and untracked-but-not-ignored files only: stale ignored objects
# (src/*.o) would otherwise be linked instead of rebuilt.
git -C "$repo" ls-files -z --cached --others --exclude-standard \
    | (cd "$repo" && while IFS= read -r -d '' path; do
        if [[ -e $path ]]; then printf '%s\0' "$path"; fi
    done | xargs -0 tar -cf - --) | tar -C "$temp/current" -xf -
make -C "$temp/current" PG_CONFIG="$pg_config" COPT=-Werror
make -C "$temp/current" PG_CONFIG="$pg_config" install
[[ ! -e "/etc/postgresql/$PG/ci" ]] || {
    echo "cluster $PG/ci already exists" >&2
    exit 1
}
cluster_created=1
pg_createcluster "$PG" ci -p 5433 --start-conf=manual
cat >/etc/postgresql/"$PG"/ci/pg_hba.conf <<'HBA'
local all all trust
host all all 127.0.0.1/32 trust
host all all ::1/128 trust
HBA
pg_ctlcluster "$PG" ci start
runuser -u postgres -- "$psql" -X -v ON_ERROR_STOP=1 -p 5433 -d postgres <<'SQL'
CREATE ROLE local_cache_worker LOGIN NOINHERIT NOSUPERUSER
    NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE local_cache_test_app LOGIN PASSWORD 'ci_test_password_123456'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE postgres TO local_cache_worker, local_cache_test_app;
GRANT USAGE ON SCHEMA public TO local_cache_test_app;
SQL
pg_ctlcluster "$PG" ci stop
install -o postgres -g postgres -m 0600 /dev/null "$token_file"
auth_token=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
printf '%s\n' "$auth_token" >"$token_file"
cat >>/etc/postgresql/"$PG"/ci/postgresql.conf <<'CONF'
include_if_exists = 'pglc.conf'
CONF
cat >/etc/postgresql/"$PG"/ci/pglc.conf <<'CONF'
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'contrib_regression'
pg_local_cache.role = 'regress_pglc_worker'
pg_local_cache.port = 0
CONF
pg_ctlcluster "$PG" ci start
chown -R postgres:postgres "$temp/current"
if ! runuser -u postgres -- env PGPORT=5433 PGHOST=127.0.0.1 PGUSER=postgres \
    make -C "$temp/current" PG_CONFIG="$pg_config" installcheck; then
    [[ ! -f "$temp/current/regression.diffs" ]] || cat "$temp/current/regression.diffs"
    exit 1
fi
cat >/etc/postgresql/"$PG"/ci/pglc.conf <<CONF
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'postgres'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.port = 6390
pg_local_cache.bind_address = '127.0.0.1'
pg_local_cache.auth_token_file = '$token_file'
pg_local_cache.workers = 1
pg_local_cache.cache_entries = 512
pg_local_cache.max_clients = 16
pg_local_cache.max_clients_per_worker = 16
pg_local_cache.memory_budget_mb = 64
CONF
pg_ctlcluster "$PG" ci restart
runuser -u postgres -- env PGPORT=5433 PGHOST=127.0.0.1 PGUSER=postgres \
    "$psql" -X -v ON_ERROR_STOP=1 -d postgres -c \
    'CREATE EXTENSION IF NOT EXISTS pg_local_cache; GRANT USAGE ON SCHEMA local_cache TO local_cache_worker; GRANT SELECT ON local_cache.mapping TO local_cache_worker;'

export PG_LOCAL_CACHE_PSQL="$psql" PGHOST=127.0.0.1 PGPORT=5433
export PGDATABASE=postgres PGUSER=postgres
export PG_LOCAL_CACHE_RESP_HOST=127.0.0.1 PG_LOCAL_CACHE_RESP_PORT=6390
export PG_LOCAL_CACHE_AUTH_TOKEN="$auth_token"
export PG_LOCAL_CACHE_TEST_ROLE=local_cache_worker
export PG_LOCAL_CACHE_TEST_APP_ROLE=local_cache_test_app
export PG_LOCAL_CACHE_TEST_APP_PASSWORD=ci_test_password_123456
export PG_LOCAL_CACHE_TEST_APP_HOST=127.0.0.1
for integration in whole_row_integration pipeline_integration \
    sql_mget_integration oom_monitoring_integration; do
    echo "==> $integration"
    python3 "$repo/tests/$integration.py"
done
default_version=$(sed -n "s/^default_version = '\([^']*\)'$/\1/p" "$repo/pg_local_cache.control")
mapfile -t sorted_versions < <(
    {
        git -C "$repo" tag --list 'v*' | sed -n 's/^v//p'
        printf '%s\n' "$default_version"
    } | sort -V
)
upgrade_tag=
for version in "${sorted_versions[@]}"; do
    [[ "$version" != "$default_version" ]] || break
    upgrade_tag="v$version"
done
[[ -n "$upgrade_tag" ]] || {
    echo "no v* tag sorts below default_version $default_version for upgrade test" >&2
    exit 1
}
echo "==> upgrade from $upgrade_tag to $default_version"
mkdir "$temp/old"
git -C "$repo" archive "$upgrade_tag" | tar -C "$temp/old" -xf -
# Old releases refuse to build from an archive without an explicit build id.
make -C "$temp/old" PG_CONFIG="$pg_config" PGLC_BUILD_ID="$upgrade_tag" COPT=-Werror
make -C "$temp/old" PG_CONFIG="$pg_config" PGLC_BUILD_ID="$upgrade_tag" install
pg_ctlcluster "$PG" ci restart
"$psql" -X -v ON_ERROR_STOP=1 -p 5433 -d postgres -c 'CREATE DATABASE upgrade_old;'
"$psql" -X -v ON_ERROR_STOP=1 -p 5433 -d postgres -c 'CREATE DATABASE upgrade_new;'
"$psql" -X -v ON_ERROR_STOP=1 -p 5433 -d upgrade_old -c \
    'CREATE EXTENSION pg_local_cache;'
make -C "$temp/current" PG_CONFIG="$pg_config" install
pg_ctlcluster "$PG" ci restart
"$psql" -X -v ON_ERROR_STOP=1 -p 5433 -d upgrade_old -c \
    'ALTER EXTENSION pg_local_cache UPDATE;'
"$psql" -X -qAt -v ON_ERROR_STOP=1 -p 5433 -d upgrade_old \
    -f "$temp/current/test/extension_snapshot.sql" >"$temp/upgrade_old.snapshot"
"$psql" -X -v ON_ERROR_STOP=1 -p 5433 -d upgrade_new -c \
    'CREATE EXTENSION pg_local_cache;'
"$psql" -X -qAt -v ON_ERROR_STOP=1 -p 5433 -d upgrade_new \
    -f "$temp/current/test/extension_snapshot.sql" >"$temp/new.snapshot"
diff -u "$temp/upgrade_old.snapshot" "$temp/new.snapshot"
echo "all PostgreSQL $PG regression, RESP, and upgrade checks passed"
