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
retry_apt "postgresql-$PG" "postgresql-server-dev-$PG" build-essential \
    python3 git openssl libssl-dev
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
make -C "$temp/current" PG_CONFIG="$pg_config" COPT=-Werror \
    PGLC_TEST_HOOKS=1
make -C "$temp/current" PG_CONFIG="$pg_config" install \
    PGLC_TEST_HOOKS=1
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
CREATE ROLE local_cache_test_monitor NOLOGIN;
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
pg_local_cache.lock_timeout_ms = 3000
pg_local_cache.statement_timeout_ms = 2000
pg_local_cache.singleflight_wait_ms = 25
max_prepared_transactions = 10
CONF
pg_ctlcluster "$PG" ci restart
runuser -u postgres -- env PGPORT=5433 PGHOST=127.0.0.1 PGUSER=postgres \
    "$psql" -X -v ON_ERROR_STOP=1 -d postgres -c \
    'CREATE EXTENSION IF NOT EXISTS pg_local_cache;
     GRANT USAGE ON SCHEMA local_cache TO local_cache_worker;
     GRANT SELECT ON local_cache.mapping TO local_cache_worker;
     GRANT EXECUTE ON FUNCTION local_cache._effective_write_mode(regclass, text)
         TO local_cache_worker;'

export PG_LOCAL_CACHE_PSQL="$psql" PGHOST=127.0.0.1 PGPORT=5433
export PGDATABASE=postgres PGUSER=postgres
export PG_LOCAL_CACHE_RESP_HOST=127.0.0.1 PG_LOCAL_CACHE_RESP_PORT=6390
export PG_LOCAL_CACHE_AUTH_TOKEN="$auth_token"
export PG_LOCAL_CACHE_TEST_ROLE=local_cache_worker
export PG_LOCAL_CACHE_TEST_APP_ROLE=local_cache_test_app
export PG_LOCAL_CACHE_TEST_APP_PASSWORD=ci_test_password_123456
export PG_LOCAL_CACHE_TEST_APP_HOST=127.0.0.1
export PGLC_TEST_HOOKS_REQUIRED=1
for integration in whole_row_integration pipeline_integration \
    memory_integration oom_monitoring_integration; do
    echo "==> $integration"
    if [[ "$integration" == pipeline_integration ]]; then
        PGLC_SKIP_PAUSE_HOOK_TESTS=1 python3 "$repo/tests/$integration.py"
    else
        python3 "$repo/tests/$integration.py"
    fi
done

echo "==> pipeline refresh integration"
PGLC_REFRESH_ONLY=1 python3 "$repo/tests/pipeline_integration.py"

# Verify compact-entry capacity with a sized arena and a full 200k-row pass.
sed -i 's/pg_local_cache.cache_entries = 512/pg_local_cache.cache_entries = 262144/' \
    /etc/postgresql/"$PG"/ci/pglc.conf
sed -i 's/pg_local_cache.memory_budget_mb = 64/pg_local_cache.memory_budget_mb = 256/' \
    /etc/postgresql/"$PG"/ci/pglc.conf
pg_ctlcluster "$PG" ci restart
echo "==> capacity_integration"
python3 "$repo/tests/capacity_integration.py"
sed -i 's/pg_local_cache.cache_entries = 262144/pg_local_cache.cache_entries = 512/' \
    /etc/postgresql/"$PG"/ci/pglc.conf
sed -i 's/pg_local_cache.memory_budget_mb = 256/pg_local_cache.memory_budget_mb = 64/' \
    /etc/postgresql/"$PG"/ci/pglc.conf
pg_ctlcluster "$PG" ci restart

echo "==> stress_integration over plaintext RESP"
python3 "$repo/tests/stress_integration.py"
echo "==> stress_integration in refresh mode"
# Set PGLC_REFRESH_STRESS_SECONDS=3600 for the one-hour freshness gate.
PGLC_STRESS_WRITE_MODE=refresh \
    PGLC_STRESS_SECONDS="${PGLC_REFRESH_STRESS_SECONDS:-10}" \
    python3 "$repo/tests/stress_integration.py"

# Exercise MGET claim ownership and pause-hook races with four workers.
sed -i 's/pg_local_cache.memory_budget_mb = 64/pg_local_cache.memory_budget_mb = 128/' \
    /etc/postgresql/"$PG"/ci/pglc.conf
sed -i 's/pg_local_cache.workers = 1/pg_local_cache.workers = 4/' \
    /etc/postgresql/"$PG"/ci/pglc.conf
sed -i 's/pg_local_cache.statement_timeout_ms = 2000/pg_local_cache.statement_timeout_ms = 30000/' \
    /etc/postgresql/"$PG"/ci/pglc.conf
sed -i 's/pg_local_cache.lock_timeout_ms = 3000/pg_local_cache.lock_timeout_ms = 30000/' \
    /etc/postgresql/"$PG"/ci/pglc.conf
sed -i 's/pg_local_cache.singleflight_wait_ms = 25/pg_local_cache.singleflight_wait_ms = 1000/' \
    /etc/postgresql/"$PG"/ci/pglc.conf
pg_ctlcluster "$PG" ci restart
echo "==> pipeline multi-worker cases with four workers"
PGLC_MULTI_WORKER_ONLY=1 python3 "$repo/tests/pipeline_integration.py"
echo "==> pipeline pause-hook fence/race cases with long MGET deadline"
PGLC_PAUSE_HOOKS_ONLY=1 python3 "$repo/tests/pipeline_integration.py"
sed -i 's/pg_local_cache.memory_budget_mb = 128/pg_local_cache.memory_budget_mb = 64/' \
    /etc/postgresql/"$PG"/ci/pglc.conf
sed -i 's/pg_local_cache.workers = 4/pg_local_cache.workers = 2/' \
    /etc/postgresql/"$PG"/ci/pglc.conf
sed -i 's/pg_local_cache.statement_timeout_ms = 30000/pg_local_cache.statement_timeout_ms = 2000/' \
    /etc/postgresql/"$PG"/ci/pglc.conf
sed -i 's/pg_local_cache.lock_timeout_ms = 30000/pg_local_cache.lock_timeout_ms = 3000/' \
    /etc/postgresql/"$PG"/ci/pglc.conf
sed -i 's/pg_local_cache.singleflight_wait_ms = 1000/pg_local_cache.singleflight_wait_ms = 25/' \
    /etc/postgresql/"$PG"/ci/pglc.conf
pg_ctlcluster "$PG" ci restart
echo "==> stress_integration with two workers"
stress_status=0
if PGLC_STRESS_SECONDS=10 python3 "$repo/tests/stress_integration.py"; then
    stress_status=0
else
    stress_status=$?
fi
sed -i 's/pg_local_cache.workers = 2/pg_local_cache.workers = 1/' \
    /etc/postgresql/"$PG"/ci/pglc.conf
pg_ctlcluster "$PG" ci restart
if (( stress_status != 0 )); then
    exit "$stress_status"
fi

# Keep TLS fixtures inside the cluster data directory so PostgreSQL can read
# them with relative paths and tests never depend on a host certificate store.
tls_dir="$cluster_data/tls"
install -d -o postgres -g postgres -m 0700 "$tls_dir"
old_umask=$(umask)
umask 077
openssl genrsa -out "$tls_dir/ca.key" 2048
openssl req -x509 -new -key "$tls_dir/ca.key" -sha256 -days 2 \
    -subj '/CN=pg_local_cache test CA' \
    -addext 'basicConstraints=critical,CA:TRUE' \
    -addext 'keyUsage=critical,keyCertSign,cRLSign' \
    -out "$tls_dir/ca.crt"
openssl genrsa -out "$tls_dir/server.key" 2048
openssl req -new -key "$tls_dir/server.key" -subj '/CN=127.0.0.1' \
    -out "$tls_dir/server.csr"
cat >"$tls_dir/server.ext" <<'EXT'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=IP:127.0.0.1
EXT
openssl x509 -req -in "$tls_dir/server.csr" -CA "$tls_dir/ca.crt" \
    -CAkey "$tls_dir/ca.key" -CAcreateserial -days 2 -sha256 \
    -extfile "$tls_dir/server.ext" -out "$tls_dir/server.crt"
openssl genrsa -out "$tls_dir/client.key" 2048
openssl req -new -key "$tls_dir/client.key" -subj '/CN=pg_local_cache test client' \
    -out "$tls_dir/client.csr"
cat >"$tls_dir/client.ext" <<'EXT'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=clientAuth
EXT
openssl x509 -req -in "$tls_dir/client.csr" -CA "$tls_dir/ca.crt" \
    -CAkey "$tls_dir/ca.key" -CAcreateserial -days 2 -sha256 \
    -extfile "$tls_dir/client.ext" -out "$tls_dir/client.crt"
umask "$old_umask"
chown -R postgres:postgres "$tls_dir"
chmod 0600 "$tls_dir/ca.key" "$tls_dir/server.key" "$tls_dir/client.key"
chmod 0644 "$tls_dir/ca.crt" "$tls_dir/server.crt" "$tls_dir/client.crt"

cat >/etc/postgresql/"$PG"/ci/pglc.conf <<CONF
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'postgres'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.port = 6391
pg_local_cache.bind_address = '127.0.0.1'
pg_local_cache.auth_token_file = '$token_file'
pg_local_cache.workers = 1
pg_local_cache.cache_entries = 512
pg_local_cache.max_clients = 16
pg_local_cache.max_clients_per_worker = 16
pg_local_cache.memory_budget_mb = 64
pg_local_cache.idle_timeout_ms = 1000
pg_local_cache.lock_timeout_ms = 3000
pg_local_cache.tls = on
pg_local_cache.tls_cert_file = 'tls/server.crt'
pg_local_cache.tls_key_file = 'tls/server.key'
CONF
pg_ctlcluster "$PG" ci restart
echo "==> TLS handshake deadline"
PG_LOCAL_CACHE_TLS_CA="$tls_dir/ca.crt" PG_LOCAL_CACHE_RESP_PORT=6391 \
    python3 "$repo/tests/tls_integration.py" --handshake-timeout
sed -i 's/pg_local_cache.idle_timeout_ms = 1000/pg_local_cache.idle_timeout_ms = 60000/' \
    /etc/postgresql/"$PG"/ci/pglc.conf
pg_ctlcluster "$PG" ci restart
echo "==> tls_integration"
PG_LOCAL_CACHE_TLS_CA="$tls_dir/ca.crt" PG_LOCAL_CACHE_RESP_PORT=6391 \
    python3 "$repo/tests/tls_integration.py"
echo "==> pipeline_integration over TLS"
PGLC_SKIP_PAUSE_HOOK_TESTS=1 PG_LOCAL_CACHE_TLS_CA="$tls_dir/ca.crt" PG_LOCAL_CACHE_RESP_PORT=6391 \
    python3 "$repo/tests/pipeline_integration.py"
echo "==> stress_integration over TLS"
PG_LOCAL_CACHE_TLS_CA="$tls_dir/ca.crt" PG_LOCAL_CACHE_RESP_PORT=6391 \
    PGLC_STRESS_SECONDS=10 python3 "$repo/tests/stress_integration.py"

cat >>/etc/postgresql/"$PG"/ci/pglc.conf <<'CONF'
pg_local_cache.tls_min_protocol_version = 'TLSv1.3'
CONF
pg_ctlcluster "$PG" ci restart
echo "==> TLS protocol floor"
PG_LOCAL_CACHE_TLS_CA="$tls_dir/ca.crt" PG_LOCAL_CACHE_RESP_PORT=6391 \
    python3 "$repo/tests/tls_integration.py" --protocol-floor

cat >/etc/postgresql/"$PG"/ci/pglc.conf <<CONF
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'postgres'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.port = 6391
pg_local_cache.bind_address = '0.0.0.0'
pg_local_cache.auth_token_file = '$token_file'
pg_local_cache.workers = 1
pg_local_cache.cache_entries = 512
pg_local_cache.max_clients = 16
pg_local_cache.max_clients_per_worker = 16
pg_local_cache.memory_budget_mb = 64
pg_local_cache.idle_timeout_ms = 60000
pg_local_cache.tls = on
pg_local_cache.tls_cert_file = 'tls/server.crt'
pg_local_cache.tls_key_file = 'tls/server.key'
CONF
pg_ctlcluster "$PG" ci restart
echo "==> TLS non-loopback health"
PG_LOCAL_CACHE_TLS_CA="$tls_dir/ca.crt" PG_LOCAL_CACHE_RESP_PORT=6391 \
    python3 "$repo/tests/tls_integration.py" --bind-health

cat >/etc/postgresql/"$PG"/ci/pglc.conf <<CONF
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = 'postgres'
pg_local_cache.role = 'local_cache_worker'
pg_local_cache.port = 6391
pg_local_cache.bind_address = '127.0.0.1'
pg_local_cache.auth_token_file = '$token_file'
pg_local_cache.workers = 1
pg_local_cache.cache_entries = 512
pg_local_cache.max_clients = 16
pg_local_cache.max_clients_per_worker = 16
pg_local_cache.memory_budget_mb = 64
pg_local_cache.idle_timeout_ms = 60000
pg_local_cache.tls = on
pg_local_cache.tls_cert_file = 'tls/server.crt'
pg_local_cache.tls_key_file = 'tls/server.key'
pg_local_cache.tls_ca_file = 'tls/ca.crt'
CONF
pg_ctlcluster "$PG" ci restart
echo "==> mTLS integration"
PG_LOCAL_CACHE_TLS_CA="$tls_dir/ca.crt" \
    PG_LOCAL_CACHE_TLS_CERT="$tls_dir/client.crt" \
    PG_LOCAL_CACHE_TLS_KEY="$tls_dir/client.key" \
    PG_LOCAL_CACHE_TLS_WRONG_CERT="$tls_dir/server.crt" \
    PG_LOCAL_CACHE_TLS_WRONG_KEY="$tls_dir/server.key" \
    PG_LOCAL_CACHE_RESP_PORT=6391 \
    python3 "$repo/tests/tls_integration.py" --mtls

default_version=$(sed -n "s/^default_version = '\([^']*\)'$/\1/p" "$repo/pg_local_cache.control")
current_major=${default_version%%.*}
mapfile -t sorted_versions < <(
    {
        git -C "$repo" tag --list 'v*' | sed -n 's/^v//p'
        printf '%s\n' "$default_version"
    } | sort -V
)
upgrade_tag=
previous_major_tag=
for version in "${sorted_versions[@]}"; do
    [[ "$version" != "$default_version" ]] || break
    upgrade_tag="v$version"
    version_major=${version%%.*}
    if [[ "$version_major" =~ ^[0-9]+$ ]] && (( 10#$version_major < 10#$current_major )); then
        previous_major_tag=$upgrade_tag
    fi
done
[[ -n "$upgrade_tag" ]] || {
    echo "no v* tag sorts below default_version $default_version for upgrade test" >&2
    exit 1
}
upgrade_tags=("$upgrade_tag")
if [[ -n "$previous_major_tag" && "$previous_major_tag" != "$upgrade_tag" ]]; then
    upgrade_tags+=("$previous_major_tag")
fi

run_upgrade_test() {
    local upgrade_tag=$1 upgrade_n=$2
    local db_old="upgrade_old_$upgrade_n" db_new="upgrade_new_$upgrade_n"
    local old_dir="$temp/old_$upgrade_n" baseline_major=${upgrade_tag#v}
    baseline_major=${baseline_major%%.*}
    echo "==> upgrade from $upgrade_tag to $default_version"
    mkdir "$old_dir"
    git -C "$repo" archive "$upgrade_tag" | tar -C "$old_dir" -xf -
    # Old releases refuse to build from an archive without an explicit build id.
    make -C "$old_dir" PG_CONFIG="$pg_config" PGLC_BUILD_ID="$upgrade_tag" COPT=-Werror
    make -C "$old_dir" PG_CONFIG="$pg_config" PGLC_BUILD_ID="$upgrade_tag" install
    "$psql" -X -v ON_ERROR_STOP=1 -p 5433 -d postgres -c "CREATE DATABASE $db_old;"
    "$psql" -X -v ON_ERROR_STOP=1 -p 5433 -d postgres -c "CREATE DATABASE $db_new;"
    cat >/etc/postgresql/"$PG"/ci/pglc.conf <<CONF
shared_preload_libraries = 'pg_local_cache'
pg_local_cache.database = '$db_old'
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
    "$psql" -X -v ON_ERROR_STOP=1 -p 5433 -d "$db_old" <<SQL
CREATE EXTENSION pg_local_cache;
GRANT CONNECT ON DATABASE $db_old TO local_cache_worker;
GRANT USAGE ON SCHEMA local_cache TO local_cache_worker;
GRANT SELECT ON local_cache.mapping TO local_cache_worker;
CREATE TABLE public.upgrade_attached (id bigint PRIMARY KEY, value text);
INSERT INTO public.upgrade_attached VALUES (1, 'before-upgrade');
GRANT SELECT, INSERT, UPDATE, DELETE ON public.upgrade_attached TO local_cache_worker;
SELECT local_cache.attach_table('public.upgrade_attached'::regclass, true);
GRANT EXECUTE ON FUNCTION local_cache.invalidate(text) TO local_cache_test_app;
GRANT EXECUTE ON FUNCTION local_cache.metrics() TO local_cache_test_monitor;
CREATE VIEW public.upgrade_health_view AS SELECT local_cache.health() AS payload;
CREATE VIEW public.upgrade_metrics_dependency AS SELECT * FROM local_cache.metrics();
SQL

    # Fill an attached key while the old library is loaded, then replace the
    # library and warm it again before the catalog migration.
    PG_LOCAL_CACHE_PSQL="$psql" PGPORT=5433 PGHOST=127.0.0.1 \
        PGDATABASE="$db_old" PGUSER=postgres \
        PG_LOCAL_CACHE_RESP_HOST=127.0.0.1 PG_LOCAL_CACHE_RESP_PORT=6390 \
        PG_LOCAL_CACHE_AUTH_TOKEN="$auth_token" python3 - "$repo" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/tests")
from pipeline_integration import RespConnection, crud_key, wait_for_mapping

client = RespConnection()
try:
    value = wait_for_mapping(client, crud_key("upgrade_attached", 1))
    assert value == b'{"id":1,"value":"before-upgrade"}', value
finally:
    client.close()
PY
    make -C "$temp/current" PG_CONFIG="$pg_config" install
    pg_ctlcluster "$PG" ci restart
    "$psql" -X -v ON_ERROR_STOP=1 -p 5433 -d "$db_new" <<'SQL'
CREATE EXTENSION pg_local_cache;
-- Same operator grants as upgrade_old, so the snapshots differ only by migration drift.
GRANT USAGE ON SCHEMA local_cache TO local_cache_worker;
GRANT SELECT ON local_cache.mapping TO local_cache_worker;
GRANT EXECUTE ON FUNCTION local_cache.invalidate(text) TO local_cache_test_app;
GRANT EXECUTE ON FUNCTION local_cache.metrics() TO local_cache_test_monitor;
SQL
    # Mixed-state regression: current library serves the old catalog and trigger args.
    PG_LOCAL_CACHE_PSQL="$psql" PGPORT=5433 PGHOST=127.0.0.1 \
        PGDATABASE="$db_old" PGUSER=postgres \
        PG_LOCAL_CACHE_RESP_HOST=127.0.0.1 PG_LOCAL_CACHE_RESP_PORT=6390 \
        PG_LOCAL_CACHE_AUTH_TOKEN="$auth_token" python3 - "$repo" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/tests")
from pipeline_integration import RespConnection, crud_key, wait_for_mapping

client = RespConnection()
try:
    value = wait_for_mapping(client, crud_key("upgrade_attached", 1))
    assert value == b'{"id":1,"value":"before-upgrade"}', value
finally:
    client.close()
PY
    if (( baseline_major < 3 )); then
        if ! "$psql" -X -v ON_ERROR_STOP=1 -p 5433 -d "$db_old" \
            -c 'ALTER EXTENSION pg_local_cache UPDATE;' >"$temp/dependency-error.log" 2>&1; then
            grep -Eq 'cannot drop function (local_cache\.)?_metrics_2_0_4\(\) because other objects depend on it' \
                "$temp/dependency-error.log" || {
                cat "$temp/dependency-error.log" >&2
                echo "upgrade failed for an unexpected dependency" >&2
                exit 1
            }
        else
            echo "upgrade unexpectedly dropped metrics() despite a dependent user view" >&2
            exit 1
        fi
        "$psql" -X -v ON_ERROR_STOP=1 -p 5433 -d "$db_old" -c \
            'DROP VIEW public.upgrade_metrics_dependency;'
    fi
    "$psql" -X -v ON_ERROR_STOP=1 -p 5433 -d "$db_old" -c 'ALTER EXTENSION pg_local_cache UPDATE;'
    "$psql" -X -v ON_ERROR_STOP=1 -p 5433 -d "$db_old" -c \
        "DO \$\$ BEGIN
            IF NOT pg_catalog.has_function_privilege(
                'local_cache_test_app', 'local_cache.invalidate(text)', 'EXECUTE') THEN
                RAISE EXCEPTION 'custom extension function grant was lost';
            END IF;
            IF NOT pg_catalog.has_function_privilege(
                'local_cache_test_monitor', 'local_cache.metrics()', 'EXECUTE') THEN
                RAISE EXCEPTION 'custom metrics() grant was lost';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_trigger
                 WHERE tgrelid = 'public.upgrade_attached'::regclass
                   AND tgname IN ('pg_local_cache_statement_guard',
                                  'pg_local_cache_row_invalidate',
                                  'pg_local_cache_truncate_invalidate')
                 GROUP BY tgrelid HAVING count(*) = 3
            ) THEN
                RAISE EXCEPTION 'attached-table triggers were lost';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM public.upgrade_health_view
                           WHERE payload ? 'ready') THEN
                RAISE EXCEPTION 'dependent health view stopped working';
            END IF;
            IF to_regclass('public.upgrade_metrics_dependency') IS NOT NULL THEN
                PERFORM 1 FROM public.upgrade_metrics_dependency;
            END IF;
            IF current_setting('pg_local_cache.binary_version') <> '$default_version' THEN
                RAISE EXCEPTION 'loaded binary version is %',
                    current_setting('pg_local_cache.binary_version');
            END IF;
         END \$\$;"
    # The updated catalog and reconciled triggers must keep existing reads working.
    PG_LOCAL_CACHE_PSQL="$psql" PGPORT=5433 PGHOST=127.0.0.1 \
        PGDATABASE="$db_old" PGUSER=postgres \
        PG_LOCAL_CACHE_RESP_HOST=127.0.0.1 PG_LOCAL_CACHE_RESP_PORT=6390 \
        PG_LOCAL_CACHE_AUTH_TOKEN="$auth_token" python3 - "$repo" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/tests")
from pipeline_integration import RespConnection, crud_key, wait_for_mapping

client = RespConnection()
try:
    value = wait_for_mapping(client, crud_key("upgrade_attached", 1))
    assert value == b'{"id":1,"value":"before-upgrade"}', value
finally:
    client.close()
PY
    before_invalidations=$("$psql" -X -qAt -v ON_ERROR_STOP=1 -p 5433 \
        -d "$db_old" -c "SELECT (local_cache.stats() ->> 'invalidations')::bigint")
    "$psql" -X -v ON_ERROR_STOP=1 -p 5433 -d "$db_old" -c \
        "UPDATE public.upgrade_attached SET value = 'after-upgrade' WHERE id = 1;"
    after_invalidations=$("$psql" -X -qAt -v ON_ERROR_STOP=1 -p 5433 \
        -d "$db_old" -c "SELECT (local_cache.stats() ->> 'invalidations')::bigint")
    (( after_invalidations > before_invalidations )) || {
        echo "attached-table UPDATE did not increment invalidations" >&2
        exit 1
    }
    PG_LOCAL_CACHE_PSQL="$psql" PGPORT=5433 PGHOST=127.0.0.1 \
        PGDATABASE="$db_old" PGUSER=postgres \
        PG_LOCAL_CACHE_RESP_HOST=127.0.0.1 PG_LOCAL_CACHE_RESP_PORT=6390 \
        PG_LOCAL_CACHE_AUTH_TOKEN="$auth_token" python3 - "$repo" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/tests")
from pipeline_integration import RespConnection, crud_key, wait_for_mapping

client = RespConnection()
try:
    value = wait_for_mapping(client, crud_key("upgrade_attached", 1))
    assert value == b'{"id":1,"value":"after-upgrade"}', value
finally:
    client.close()
PY
    "$psql" -X -v ON_ERROR_STOP=1 -p 5433 -d "$db_old" <<'SQL'
DO $$
DECLARE
    result jsonb;
BEGIN
    result := local_cache.set_write_mode(
        'public.upgrade_attached'::regclass, 'refresh'
    );
    IF result ->> 'write_mode_requested' IS DISTINCT FROM 'refresh'
       OR result ->> 'write_mode_effective' IS DISTINCT FROM 'refresh' THEN
        RAISE EXCEPTION 'set_write_mode(refresh) returned %', result;
    END IF;
END;
$$;
UPDATE public.upgrade_attached
   SET value = 'after-refresh-upgrade'
 WHERE id = 1;
SQL
    PG_LOCAL_CACHE_PSQL="$psql" PGPORT=5433 PGHOST=127.0.0.1 \
        PGDATABASE="$db_old" PGUSER=postgres \
        PG_LOCAL_CACHE_RESP_HOST=127.0.0.1 PG_LOCAL_CACHE_RESP_PORT=6390 \
        PG_LOCAL_CACHE_AUTH_TOKEN="$auth_token" python3 - "$repo" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/tests")
from pipeline_integration import RespConnection, crud_key, wait_for_mapping

client = RespConnection()
try:
    value = wait_for_mapping(client, crud_key("upgrade_attached", 1))
    assert value == b'{"id":1,"value":"after-refresh-upgrade"}', value
finally:
    client.close()
PY
    "$psql" -X -qAt -v ON_ERROR_STOP=1 -p 5433 -d "$db_old" \
        -f "$temp/current/test/extension_snapshot.sql" >"$temp/$db_old.snapshot"
    "$psql" -X -qAt -v ON_ERROR_STOP=1 -p 5433 -d "$db_new" \
        -f "$temp/current/test/extension_snapshot.sql" >"$temp/$db_new.snapshot"
    diff -u "$temp/$db_old.snapshot" "$temp/$db_new.snapshot"
}

for i in "${!upgrade_tags[@]}"; do
    run_upgrade_test "${upgrade_tags[$i]}" "$((i + 1))"
done
echo "all PostgreSQL $PG regression, RESP, and upgrade checks passed"
