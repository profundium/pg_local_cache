#!/usr/bin/env bash
set -Eeuo pipefail

repository_directory="$(
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
    pwd
)"
temporary_directory="$(mktemp -d "${TMPDIR:-/tmp}/pg_local_cache_sql_only.XXXXXX")"
project="pg_local_cache_sql_only_$$"
override_file="${temporary_directory}/compose.override.yaml"
psql_wrapper="${temporary_directory}/psql"
postgres_secret="${temporary_directory}/postgres_password"
runner_image="pg_local_cache-sql-only-runner:$$"
benchmark_output_directory="${PGLC_SQL_ONLY_BENCH_OUTPUT_DIR:-${temporary_directory}/benchmark-results}"
postgres_host_port="${POSTGRES_HOST_PORT:-$(
    python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
)}"
database="pg_local_cache_sql_only"
app_role="sql_only_app"
app_password="SqlOnlyAppPassword_0123456789"
postgres_major="${POSTGRES_MAJOR:-16}"
postgres_variant="${POSTGRES_VARIANT:-bookworm}"

case "$postgres_major" in
    14|15|16|17|18) ;;
    *) printf 'unsupported POSTGRES_MAJOR: %s\n' "$postgres_major" >&2; exit 1 ;;
esac
case "$postgres_variant" in
    bookworm|alpine3.23) ;;
    *) printf 'unsupported POSTGRES_VARIANT: %s\n' "$postgres_variant" >&2; exit 1 ;;
esac

compose() {
    docker compose \
        --project-name "$project" \
        --file "${repository_directory}/compose.sql-only.yaml" \
        --file "$override_file" \
        "$@"
}

cleanup() {
    compose down --volumes --remove-orphans >/dev/null 2>&1 || true
    docker image rm "$runner_image" >/dev/null 2>&1 || true
    if [[ "$temporary_directory" == "${TMPDIR:-/tmp}"/pg_local_cache_sql_only.* ]]; then
        rm -rf -- "$temporary_directory"
    fi
}
trap cleanup EXIT

install -m 0600 /dev/null "$postgres_secret"
printf '%s\n' 'SqlOnlyPostgresPassword_0123456789' >"$postgres_secret"

cat >"$override_file" <<YAML
services:
  postgres:
    environment:
      POSTGRES_DB: ${database}
      PG_LOCAL_CACHE_DATABASE: ${database}
  postgres_stock:
    environment:
      POSTGRES_DB: ${database}
secrets:
  postgres_password:
    file: ${postgres_secret}
YAML

cat >"$psql_wrapper" <<SCRIPT
#!/usr/bin/env bash
extra_env=()
if [[ -n "\${PGPASSWORD:-}" ]]; then
    extra_env+=(--env PGPASSWORD)
fi
exec docker compose \
    --project-name "$project" \
    --file "${repository_directory}/compose.sql-only.yaml" \
    --file "$override_file" \
    exec -T "\${extra_env[@]}" postgres psql --username postgres "\$@"
SCRIPT
chmod 0700 "$psql_wrapper"

export POSTGRES_HOST_PORT="$postgres_host_port"
compose up --detach --build --wait

sql_only_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 --command \
        "SELECT current_setting('pg_local_cache.port'), (SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE backend_type = 'pg_local_cache RESP worker')"
)"
[[ "$sql_only_state" == "0|0" ]]

stock_state="$(
    compose exec -T postgres_stock \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 --command \
        "SELECT current_setting('server_version_num'), NOT EXISTS (SELECT 1 FROM pg_catalog.pg_extension WHERE extname = 'pg_local_cache'), position('pg_local_cache' in current_setting('shared_preload_libraries')) = 0"
)"
mapped_version="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 --command \
        "SELECT current_setting('server_version_num')"
)"
[[ "$stock_state" == "${mapped_version}|t|t" ]]

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --set app_role="$app_role" \
    --set app_password="$app_password" --set cache_database="$database" <<'SQL'
SELECT pg_catalog.format(
    'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
    :'app_role', :'app_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = :'app_role'
)
\gexec
GRANT CONNECT ON DATABASE :"cache_database" TO :"app_role";
GRANT USAGE ON SCHEMA public TO :"app_role";
SQL

PG_LOCAL_CACHE_PSQL="$psql_wrapper" \
PGHOST="/var/run/postgresql" \
PGPORT="5432" \
PGDATABASE="$database" \
PG_LOCAL_CACHE_RESP_PORT="0" \
PG_LOCAL_CACHE_TEST_APP_ROLE="$app_role" \
PG_LOCAL_CACHE_TEST_APP_PASSWORD="$app_password" \
PG_LOCAL_CACHE_TEST_APP_HOST="127.0.0.1" \
POSTGRES_MAJOR="$postgres_major" \
    python3 -B "${repository_directory}/tests/sql_fastpath_integration.py"

sql_only_metrics="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 --command \
        "SELECT m.up, m.workers_configured, m.workers_running, m.active_clients, m.max_clients, m.worker_memory_bytes, m.estimated_memory_bytes <= m.memory_budget_bytes, (local_cache.health() ->> 'ready')::boolean FROM local_cache.metrics() AS m"
)"
[[ "$sql_only_metrics" == "1|0|0|0|0|0|t|t" ]]

mkdir -p -- "$benchmark_output_directory"
benchmark_output_directory="$(
    cd -- "$benchmark_output_directory"
    pwd -P
)"
docker build \
    --file "${repository_directory}/benchmarks/Dockerfile" \
    --tag "$runner_image" \
    "$repository_directory"

source_revision="$({
    git -C "$repository_directory" rev-parse --verify HEAD 2>/dev/null ||
        printf 'unknown'
} | head -n 1)"
if [[ -n "$(git -C "$repository_directory" status --porcelain 2>/dev/null)" ]]; then
    source_revision="${source_revision}-dirty"
fi
harness_sha256="$(
    sha256sum "${repository_directory}/benchmarks/sql_only.py" |
        awk '{print $1}'
)"

runner_limits=(--memory "${PGLC_SQL_ONLY_BENCH_CLIENT_MEMORY:-1g}")
if [[ -n "${PGLC_SQL_ONLY_BENCH_CLIENT_CPUS:-}" ]]; then
    runner_limits+=(--cpus "$PGLC_SQL_ONLY_BENCH_CLIENT_CPUS")
fi

docker run --rm \
    --network "${project}_default" \
    --user "$(id -u):$(id -g)" \
    "${runner_limits[@]}" \
    --volume "${benchmark_output_directory}:/results" \
    --env PGHOST=postgres \
    --env PGPORT=5432 \
    --env PGDATABASE="$database" \
    --env PGUSER=postgres \
    --env PGPASSWORD=SqlOnlyPostgresPassword_0123456789 \
    --env PGLC_SQL_ONLY_BENCH_STOCK_HOST=postgres_stock \
    --env PGLC_SQL_ONLY_BENCH_STOCK_PORT=5432 \
    --env PGLC_SQL_ONLY_BENCH_STOCK_DATABASE="$database" \
    --env PGLC_SQL_ONLY_BENCH_STOCK_USER=postgres \
    --env PGLC_SQL_ONLY_BENCH_STOCK_PASSWORD=SqlOnlyPostgresPassword_0123456789 \
    --env POSTGRES_MAJOR="$postgres_major" \
    --env PGLC_SQL_ONLY_BENCH_DURATION="${PGLC_SQL_ONLY_BENCH_DURATION:-1}" \
    --env PGLC_SQL_ONLY_BENCH_WARMUP_SECONDS="${PGLC_SQL_ONLY_BENCH_WARMUP_SECONDS:-0}" \
    --env PGLC_SQL_ONLY_BENCH_LATENCY_DURATION="${PGLC_SQL_ONLY_BENCH_LATENCY_DURATION:-}" \
    --env PGLC_SQL_ONLY_BENCH_LATENCY_SAMPLE_RATE="${PGLC_SQL_ONLY_BENCH_LATENCY_SAMPLE_RATE:-}" \
    --env PGLC_SQL_ONLY_BENCH_LATENCY_MIN_SAMPLES="${PGLC_SQL_ONLY_BENCH_LATENCY_MIN_SAMPLES:-}" \
    --env PGLC_SQL_ONLY_BENCH_LATENCY_MAX_P99_MS="${PGLC_SQL_ONLY_BENCH_LATENCY_MAX_P99_MS:-}" \
    --env PGLC_SQL_ONLY_BENCH_REPETITIONS="${PGLC_SQL_ONLY_BENCH_REPETITIONS:-1}" \
    --env PGLC_SQL_ONLY_BENCH_CONCURRENCY="${PGLC_SQL_ONLY_BENCH_CONCURRENCY:-4}" \
    --env PGLC_SQL_ONLY_BENCH_JOBS="${PGLC_SQL_ONLY_BENCH_JOBS:-}" \
    --env PGLC_SQL_ONLY_BENCH_PIPELINE="${PGLC_SQL_ONLY_BENCH_PIPELINE:-8}" \
    --env PGLC_SQL_ONLY_BENCH_KEYS="${PGLC_SQL_ONLY_BENCH_KEYS:-4096}" \
    --env PGLC_SQL_ONLY_BENCH_PAYLOAD_BYTES="${PGLC_SQL_ONLY_BENCH_PAYLOAD_BYTES:-3000}" \
    --env PGLC_SQL_ONLY_BENCH_PREPARED_MIN_OPS="${PGLC_SQL_ONLY_BENCH_PREPARED_MIN_OPS:-10000}" \
    --env PGLC_SQL_ONLY_BENCH_EXTENDED_MIN_OPS="${PGLC_SQL_ONLY_BENCH_EXTENDED_MIN_OPS:-10000}" \
    --env PGLC_SQL_ONLY_BENCH_MIN_CACHED_TO_DIRECT_RATIO="${PGLC_SQL_ONLY_BENCH_MIN_CACHED_TO_DIRECT_RATIO:-}" \
    --env PGLC_SQL_ONLY_BENCH_MIN_CACHED_TO_STOCK_RATIO="${PGLC_SQL_ONLY_BENCH_MIN_CACHED_TO_STOCK_RATIO:-}" \
    --env PGLC_SQL_ONLY_BENCH_SCALING_SNAPSHOT="${PGLC_SQL_ONLY_BENCH_SCALING_SNAPSHOT:-false}" \
    --env PGLC_SQL_ONLY_BENCH_SCALING_DURATION="${PGLC_SQL_ONLY_BENCH_SCALING_DURATION:-3}" \
    --env PGLC_SQL_ONLY_BENCH_SCALING_WARMUP_SECONDS="${PGLC_SQL_ONLY_BENCH_SCALING_WARMUP_SECONDS:-1}" \
    --env PGLC_SQL_ONLY_BENCH_SCALING_LATENCY_DURATION="${PGLC_SQL_ONLY_BENCH_SCALING_LATENCY_DURATION:-3}" \
    --env PGLC_SQL_ONLY_BENCH_SCALING_LATENCY_SAMPLE_RATE="${PGLC_SQL_ONLY_BENCH_SCALING_LATENCY_SAMPLE_RATE:-0.10}" \
    --env PGLC_SQL_ONLY_BENCH_SCALING_LATENCY_MIN_SAMPLES="${PGLC_SQL_ONLY_BENCH_SCALING_LATENCY_MIN_SAMPLES:-500}" \
    --env PGLC_SQL_ONLY_BENCH_SCALING_REPETITIONS="${PGLC_SQL_ONLY_BENCH_SCALING_REPETITIONS:-1}" \
    --env PGLC_SQL_ONLY_BENCH_OUTPUT_DIR=/results \
    --env PGLC_BENCH_SOURCE_REVISION="$source_revision" \
    --env PGLC_BENCH_SQL_ONLY_HARNESS_SHA256="$harness_sha256" \
    --entrypoint python3 \
    "$runner_image" \
    /usr/local/lib/pg_local_cache/sql_only.py

test -s "${benchmark_output_directory}/sql-only.json"
test -s "${benchmark_output_directory}/sql-only.md"

printf 'ok: stock PostgreSQL comparison, SQL-only cache, throughput and latency gates\n'
