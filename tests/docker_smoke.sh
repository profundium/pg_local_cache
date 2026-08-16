#!/usr/bin/env bash
set -Eeuo pipefail

repository_directory="$(
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
    pwd
)"
temporary_directory="$(mktemp -d "${TMPDIR:-/tmp}/pg_local_cache_smoke.XXXXXX")"
project="pg_local_cache_smoke_$$"
override_file="${temporary_directory}/compose.override.yaml"
psql_wrapper="${temporary_directory}/psql"
postgres_secret="${temporary_directory}/postgres_password"
cache_secret="${temporary_directory}/pg_local_cache_auth_token"
database="${PG_LOCAL_CACHE_SMOKE_DATABASE:-app}"
mismatch_database="postgres"
[[ "$database" != "$mismatch_database" ]] || mismatch_database="template1"
worker_role="${PG_LOCAL_CACHE_SMOKE_ROLE:-local_cache_worker}"
app_role="${PG_LOCAL_CACHE_SMOKE_APP_ROLE:-app_user}"
app_password="${PG_LOCAL_CACHE_SMOKE_APP_PASSWORD:-SmokeAppPassword_0123456789}"
cache_admin_role="${PG_LOCAL_CACHE_SMOKE_ADMIN_ROLE:-local_cache_admin}"
cache_entries="${PG_LOCAL_CACHE_SMOKE_CACHE_ENTRIES:-65536}"
max_clients="${PG_LOCAL_CACHE_SMOKE_MAX_CLIENTS:-32}"
max_clients_per_worker="${PG_LOCAL_CACHE_SMOKE_MAX_CLIENTS_PER_WORKER:-16}"
max_prepared_transactions="${PG_LOCAL_CACHE_SMOKE_MAX_PREPARED_TRANSACTIONS:-0}"
require_small_cache="${PG_LOCAL_CACHE_SMOKE_REQUIRE_SMALL_CACHE:-0}"
require_2pc="${PG_LOCAL_CACHE_SMOKE_REQUIRE_2PC:-0}"
postgres_major="${POSTGRES_MAJOR:-16}"
postgres_variant="${POSTGRES_VARIANT:-bookworm}"
build_id="$(git -C "$repository_directory" rev-parse --verify HEAD)"
release_root="${PGLC_RELEASE_ROOT:-}"

[[ "$database" =~ ^[A-Za-z_][A-Za-z0-9_$]{0,62}$ ]]
[[ "$worker_role" =~ ^[A-Za-z_][A-Za-z0-9_$]{0,62}$ ]]
[[ "$app_role" =~ ^[A-Za-z_][A-Za-z0-9_$]{0,62}$ ]]
[[ "$cache_admin_role" =~ ^[A-Za-z_][A-Za-z0-9_$]{0,62}$ ]]
[[ ${#app_password} -ge 16 ]]
[[ "$cache_entries" =~ ^[0-9]+$ ]]
[[ "$max_clients" =~ ^[0-9]+$ ]]
[[ "$max_clients_per_worker" =~ ^[0-9]+$ ]]
[[ "$max_prepared_transactions" =~ ^[0-9]+$ ]]
[[ "$require_small_cache" == "0" || "$require_small_cache" == "1" ]]
[[ "$require_2pc" == "0" || "$require_2pc" == "1" ]]
case "$postgres_major" in
    14|15|16|17|18) ;;
    *) printf 'unsupported POSTGRES_MAJOR: %s\n' "$postgres_major" >&2; exit 1 ;;
esac
[[ "$build_id" =~ ^[0-9a-f]{40}$ ]]
if [[ -n "$release_root" ]]; then
    [[ "$release_root" == /* && -d "$release_root" ]]
    [[ -x "$release_root/install.sh" ]]
    [[ -f "$release_root/BUILD-ID" && -f "$release_root/RELEASE-METADATA" ]]
fi
export PGLC_BUILD_ID="$build_id"
case "$postgres_variant" in
    bookworm|alpine3.23) ;;
    *) printf 'unsupported POSTGRES_VARIANT: %s\n' "$postgres_variant" >&2; exit 1 ;;
esac

choose_port() {
    python3 -c \
        'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

postgres_host_port="${POSTGRES_HOST_PORT:-$(choose_port)}"
cache_host_port="${PG_LOCAL_CACHE_HOST_PORT:-$(choose_port)}"
while [[ "$cache_host_port" == "$postgres_host_port" ]]; do
    cache_host_port="$(choose_port)"
done
postgres_password="SmokePostgresPassword_0123456789"
auth_token="SmokeAuthToken_0123456789abcdef0123456789abcdef"

compose() {
    docker compose \
        --project-name "$project" \
        --file "${repository_directory}/compose.yaml" \
        --file "$override_file" \
        "$@"
}

check_auth_framing() {
    PG_LOCAL_CACHE_AUTH_TOKEN="$auth_token" \
    PG_LOCAL_CACHE_RESP_PORT="$cache_host_port" \
        python3 -B - <<'PY'
import os
import socket


def auth(token: bytes) -> bytes:
    request = (
        b"*2\r\n$4\r\nAUTH\r\n$"
        + str(len(token)).encode("ascii")
        + b"\r\n"
        + token
        + b"\r\n"
    )
    with socket.create_connection(
        ("127.0.0.1", int(os.environ["PG_LOCAL_CACHE_RESP_PORT"])), timeout=5
    ) as client:
        client.sendall(request)
        return client.recv(256)


token = os.environ["PG_LOCAL_CACHE_AUTH_TOKEN"].encode("ascii")
assert auth(token) == b"+OK\r\n"
changed = token[:-1] + (b"A" if token[-1:] != b"A" else b"B")
assert auth(changed).startswith(b"-WRONGPASS ")
PY
}

cleanup() {
    local exit_status=$?
    if (( exit_status != 0 )); then
        printf 'docker smoke failed; PostgreSQL diagnostics follow\n' >&2
        compose logs --no-color postgres >&2 || true
    fi
    compose down --volumes --remove-orphans >/dev/null 2>&1 || true
    if [[ "$temporary_directory" == "${TMPDIR:-/tmp}"/pg_local_cache_smoke.* ]]; then
        rm -rf -- "$temporary_directory"
    fi
    return "$exit_status"
}
trap cleanup EXIT
trap 'printf "docker smoke command failed at line %s: %s\n" "$LINENO" "$BASH_COMMAND" >&2' ERR

install -m 0600 /dev/null "$postgres_secret"
install -m 0600 /dev/null "$cache_secret"
printf '%s\n' "$postgres_password" >"$postgres_secret"
printf '%s\n' "$auth_token" >"$cache_secret"

cat >"$override_file" <<YAML
services:
  postgres:
    environment:
      POSTGRES_DB: ${database}
      PG_LOCAL_CACHE_DATABASE: ${database}
      PG_LOCAL_CACHE_ROLE: ${worker_role}
      PG_LOCAL_CACHE_CACHE_ENTRIES: "${cache_entries}"
      PG_LOCAL_CACHE_MAX_CLIENTS: "${max_clients}"
      PG_LOCAL_CACHE_MAX_CLIENTS_PER_WORKER: "${max_clients_per_worker}"
    command:
      - postgres
      - -c
      - max_prepared_transactions=${max_prepared_transactions}
secrets:
  postgres_password:
    file: ${postgres_secret}
  pg_local_cache_auth_token:
    file: ${cache_secret}
YAML

cat >"$psql_wrapper" <<SCRIPT
#!/usr/bin/env bash
extra_env=()
if [[ -n "\${PGPASSWORD:-}" ]]; then
    extra_env+=(--env PGPASSWORD)
fi
exec docker compose \
    --project-name "$project" \
    --file "${repository_directory}/compose.yaml" \
    --file "$override_file" \
    exec -T "\${extra_env[@]}" postgres psql --username postgres "\$@"
SCRIPT
chmod 0700 "$psql_wrapper"

export POSTGRES_HOST_PORT="$postgres_host_port"
export PG_LOCAL_CACHE_HOST_PORT="$cache_host_port"

compose up --detach --build --wait

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

app_identity="$(
    PGPASSWORD="$app_password" "$psql_wrapper" \
        --host 127.0.0.1 --username "$app_role" --dbname "$database" \
        --no-psqlrc --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT current_user, session_user, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = current_user"
)"
[[ "$app_identity" == "${app_role}|${app_role}|f|f|f|f|f" ]]

app_acl="$(
    PGPASSWORD="$app_password" "$psql_wrapper" \
        --host 127.0.0.1 --username "$app_role" --dbname "$database" \
        --no-psqlrc --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT pg_catalog.has_schema_privilege(current_user, 'local_cache', 'USAGE'), COALESCE((SELECT pg_catalog.has_table_privilege(current_user, c.oid, 'SELECT') FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'local_cache' AND c.relname = 'mapping'), false), COALESCE((SELECT bool_or(pg_catalog.has_function_privilege(current_user, p.oid, 'EXECUTE')) FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'local_cache' AND p.proname = 'detach_table'), false), COALESCE((SELECT bool_or(pg_catalog.has_function_privilege(current_user, p.oid, 'EXECUTE')) FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'local_cache' AND p.proname = 'invalidate'), false), COALESCE((SELECT bool_or(pg_catalog.has_function_privilege(current_user, p.oid, 'EXECUTE')) FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'local_cache' AND p.proname = 'attach_table'), false)"
)"
[[ "$app_acl" == "f|f|f|f|f" ]]

protected_table_state_before="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT (SELECT count(*) FROM local_cache.mapping), pg_catalog.has_table_privilege('$worker_role', 'local_cache.mapping', 'SELECT'), pg_catalog.has_table_privilege('$worker_role', 'local_cache.mapping', 'INSERT'), pg_catalog.has_table_privilege('$worker_role', 'local_cache.mapping', 'UPDATE'), pg_catalog.has_table_privilege('$worker_role', 'local_cache.mapping', 'DELETE'), (SELECT count(*) FROM pg_catalog.pg_trigger WHERE tgrelid = 'local_cache.mapping'::regclass AND tgname IN ('pg_local_cache_statement_guard', 'pg_local_cache_row_invalidate', 'pg_local_cache_truncate_invalidate')), (SELECT pg_catalog.string_agg(oid::text || ':' || tgname, ',' ORDER BY oid) FROM pg_catalog.pg_trigger WHERE tgrelid = 'local_cache.mapping'::regclass)"
)"

protected_whole_error="${temporary_directory}/attach-protected-whole.error"
if compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "SELECT local_cache.attach_table('local_cache.mapping'::regclass, true, 'forbidden-system-whole')" \
    >"$protected_whole_error" 2>&1; then
    printf 'extension-owned mapping table was unexpectedly accepted for whole-row attach\n' >&2
    exit 1
fi
grep -Fq 'cannot attach extension or system table' "$protected_whole_error"

protected_table_state_after="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT (SELECT count(*) FROM local_cache.mapping), pg_catalog.has_table_privilege('$worker_role', 'local_cache.mapping', 'SELECT'), pg_catalog.has_table_privilege('$worker_role', 'local_cache.mapping', 'INSERT'), pg_catalog.has_table_privilege('$worker_role', 'local_cache.mapping', 'UPDATE'), pg_catalog.has_table_privilege('$worker_role', 'local_cache.mapping', 'DELETE'), (SELECT count(*) FROM pg_catalog.pg_trigger WHERE tgrelid = 'local_cache.mapping'::regclass AND tgname IN ('pg_local_cache_statement_guard', 'pg_local_cache_row_invalidate', 'pg_local_cache_truncate_invalidate')), (SELECT pg_catalog.string_agg(oid::text || ':' || tgname, ',' ORDER BY oid) FROM pg_catalog.pg_trigger WHERE tgrelid = 'local_cache.mapping'::regclass)"
)"
[[ "$protected_table_state_after" == "$protected_table_state_before" ]]
[[ "$protected_table_state_after" == 0\|t\|f\|f\|f\|0\|* ]]
[[ "$protected_table_state_after" == *':pg_local_cache_mapping_reload'* ]]

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 <<'SQL'
CREATE TABLE public.pglc_attach_smoke (
    id bigint PRIMARY KEY,
    value text NOT NULL
);
INSERT INTO public.pglc_attach_smoke VALUES (1, 'attached');
CREATE TABLE public.pglc_attach_composite_smoke (
    tenant_id bigint NOT NULL,
    id bigint NOT NULL,
    value text NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE public.pglc_attach_other_smoke (
    id bigint PRIMARY KEY,
    value text NOT NULL
);
CREATE TABLE public.pglc_attach_native_smoke (
    id bigint PRIMARY KEY,
    value text NOT NULL
);
CREATE TABLE public.pglc_attach_no_pk_smoke (
    id bigint NOT NULL,
    value text NOT NULL
);
CREATE TABLE public.pglc_attach_bad_key_smoke (
    id bigint PRIMARY KEY,
    value text NOT NULL
);
CREATE TABLE public.pglc_attach_rls_smoke (
    id bigint PRIMARY KEY,
    value text NOT NULL
);
ALTER TABLE public.pglc_attach_rls_smoke ENABLE ROW LEVEL SECURITY;
SQL

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --set cache_admin_role="$cache_admin_role" <<'SQL'
SELECT pg_catalog.format(
    'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
    :'cache_admin_role'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = :'cache_admin_role'
)
\gexec
GRANT USAGE ON SCHEMA local_cache TO :"cache_admin_role";
GRANT EXECUTE ON FUNCTION local_cache.attach_table(regclass, boolean, text)
    TO :"cache_admin_role";
SET ROLE :"cache_admin_role";
SELECT local_cache.attach_table('public.pglc_attach_native_smoke'::regclass);
RESET ROLE;
SQL

native_attach_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
		"SELECT m.key_columns::text, m.writable, (SELECT count(*) FROM pg_catalog.pg_trigger t WHERE t.tgrelid = m.relation AND t.tgname IN ('pg_local_cache_statement_guard', 'pg_local_cache_row_invalidate', 'pg_local_cache_truncate_invalidate') AND t.tgenabled = 'A'), pg_catalog.has_table_privilege('$worker_role', m.relation, 'SELECT') FROM local_cache.mapping m WHERE m.namespace = 'public.pglc_attach_native_smoke'"
)"
[[ "$native_attach_state" == "{id}|f|3|t" ]]

for rejected_relation in pglc_attach_no_pk_smoke pglc_attach_rls_smoke; do
    native_attach_error="${temporary_directory}/${rejected_relation}.error"
    if compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --set ON_ERROR_STOP=1 --set cache_admin_role="$cache_admin_role" \
        --command \
        "SET ROLE \"$cache_admin_role\"; SELECT local_cache.attach_table('public.${rejected_relation}'::regclass);" \
        >"$native_attach_error" 2>&1; then
        printf 'native attach unexpectedly accepted %s\n' "$rejected_relation" >&2
        exit 1
    fi
done

native_failure_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT (SELECT count(*) FROM local_cache.mapping WHERE relation IN ('public.pglc_attach_no_pk_smoke'::regclass, 'public.pglc_attach_rls_smoke'::regclass)), pg_catalog.has_table_privilege('$worker_role', 'public.pglc_attach_no_pk_smoke', 'SELECT'), pg_catalog.has_table_privilege('$worker_role', 'public.pglc_attach_rls_smoke', 'SELECT')"
)"
[[ "$native_failure_state" == "0|f|f" ]]

reserved_namespace_error="${temporary_directory}/attach-reserved-namespace.error"
if compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "SELECT local_cache.attach_table('public.pglc_attach_bad_key_smoke'::regclass, false, 'CRUD')" \
    >"$reserved_namespace_error" 2>&1; then
    printf 'reserved CRUD namespace was unexpectedly accepted\n' >&2
    exit 1
fi
grep -Fq 'invalid pg_local_cache namespace' "$reserved_namespace_error"

bad_registration_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT (SELECT count(*) FROM local_cache.mapping WHERE relation = 'public.pglc_attach_bad_key_smoke'::regclass), (SELECT count(*) FROM pg_catalog.pg_trigger WHERE tgrelid = 'public.pglc_attach_bad_key_smoke'::regclass AND tgname LIKE 'pg_local_cache_%'), pg_catalog.has_table_privilege('$worker_role', 'public.pglc_attach_bad_key_smoke', 'SELECT')"
)"
[[ "$bad_registration_state" == "0|0|f" ]]

whole_two_args="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 --command \
        "SELECT (result->>'whole_row')::boolean, (result->>'writable')::boolean FROM (SELECT local_cache.attach_table('public.pglc_attach_smoke'::regclass, false) AS result) AS attached"
)"
[[ "$whole_two_args" == "t|f" ]] || {
    printf 'unexpected two-argument attach result: %q\n' "$whole_two_args" >&2
    exit 1
}
[[ "$(compose exec -T postgres psql --username postgres --dbname "$database" \
    --no-psqlrc --tuples-only --no-align --set ON_ERROR_STOP=1 --command \
    "SELECT local_cache.detach_table('public.pglc_attach_smoke'::regclass)")" == "t" ]]

whole_three_args="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 --command \
        "SELECT (result->>'whole_row')::boolean, result->>'namespace', (result->>'writable')::boolean FROM (SELECT local_cache.attach_table('public.pglc_attach_smoke'::regclass, p_writable => true, p_namespace => 'whole-three') AS result) AS attached"
)"
[[ "$whole_three_args" == "t|whole-three|t" ]]
[[ "$(compose exec -T postgres psql --username postgres --dbname "$database" \
    --no-psqlrc --tuples-only --no-align --set ON_ERROR_STOP=1 --command \
    "SELECT local_cache.detach_table('public.pglc_attach_smoke'::regclass)")" == "t" ]]
[[ "$(compose exec -T postgres psql --username postgres --dbname "$database" \
    --no-psqlrc --tuples-only --no-align --set ON_ERROR_STOP=1 --command \
    "SELECT local_cache.detach_table('public.pglc_attach_smoke'::regclass)")" == "f" ]]

database_mismatch_error="${temporary_directory}/attach-database.error"
if compose exec -T postgres pg_local_cache_attach \
    --database "$mismatch_database" \
    --table public.pglc_attach_smoke >"$database_mismatch_error" 2>&1; then
    printf 'non-configured database was unexpectedly accepted\n' >&2
    exit 1
fi
grep -Fq 'is not served by pg_local_cache workers' "$database_mismatch_error"

unprivileged_attach_error="${temporary_directory}/attach-unprivileged.error"
if compose exec -T \
    --env "POSTGRES_USER=${app_role}" \
    --env "PGPASSWORD=${app_password}" \
    postgres pg_local_cache_attach \
        --database "$database" \
        --table public.pglc_attach_smoke >"$unprivileged_attach_error" 2>&1; then
    printf 'unprivileged attach unexpectedly succeeded\n' >&2
    exit 1
fi
grep -Eq \
    'permission denied|must be superuser or (a member of|have privileges of) pg_read_all_settings' \
    "$unprivileged_attach_error"

unprivileged_mapping_count="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT count(*) FROM local_cache.mapping WHERE namespace = 'pglc_attach_smoke'"
)"
[[ "$unprivileged_mapping_count" == "0" ]]

attach_output="$(
    compose exec -T postgres pg_local_cache_attach \
        --database "$database" \
        --namespace pglc_attach_smoke \
        --table public.pglc_attach_smoke \
        --writable
)"
[[ "$attach_output" == *'"namespace": "pglc_attach_smoke"'* ]]
[[ "$attach_output" == *'"whole_row": true'* ]]
[[ "$attach_output" == *'"primary_key_columns": ["id"]'* ]]

attach_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
		"SELECT m.key_columns::text, m.writable, (SELECT count(*) FROM pg_catalog.pg_trigger t WHERE t.tgrelid = m.relation AND t.tgname IN ('pg_local_cache_statement_guard', 'pg_local_cache_row_invalidate', 'pg_local_cache_truncate_invalidate') AND t.tgenabled = 'A'), pg_catalog.has_schema_privilege('$worker_role', 'public', 'USAGE'), pg_catalog.has_table_privilege('$worker_role', m.relation, 'SELECT') AND pg_catalog.has_table_privilege('$worker_role', m.relation, 'INSERT') AND pg_catalog.has_table_privilege('$worker_role', m.relation, 'UPDATE') AND pg_catalog.has_table_privilege('$worker_role', m.relation, 'DELETE') FROM local_cache.mapping m WHERE m.namespace = 'pglc_attach_smoke'"
)"
[[ "$attach_state" == "{id}|t|3|t|t" ]]

namespace_conflict_error="${temporary_directory}/attach-namespace.error"
if compose exec -T postgres pg_local_cache_attach \
    --database "$database" \
    --namespace pglc_attach_smoke \
    --table public.pglc_attach_other_smoke >"$namespace_conflict_error" 2>&1; then
    printf 'occupied namespace was unexpectedly replaced without --replace\n' >&2
    exit 1
fi
grep -Fq 'pass --replace to remap it' "$namespace_conflict_error"

namespace_after_conflict="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT relation = 'public.pglc_attach_smoke'::pg_catalog.regclass FROM local_cache.mapping WHERE namespace = 'pglc_attach_smoke'"
)"
[[ "$namespace_after_conflict" == "t" ]]

replace_output="$(
    compose exec -T postgres pg_local_cache_attach \
        --database "$database" \
        --namespace pglc_attach_smoke \
        --table public.pglc_attach_other_smoke \
        --replace
)"
[[ "$replace_output" == *'"relation": "public.pglc_attach_other_smoke"'* ]]

namespace_after_replace="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT relation = 'public.pglc_attach_other_smoke'::pg_catalog.regclass FROM local_cache.mapping WHERE namespace = 'pglc_attach_smoke'"
)"
[[ "$namespace_after_replace" == "t" ]]

composite_output="$(
    compose exec -T postgres pg_local_cache_attach \
        --database "$database" \
        --namespace pglc_attach_composite_smoke \
        --table public.pglc_attach_composite_smoke
)"
[[ "$composite_output" == *'"whole_row": true'* ]]
[[ "$composite_output" == *'"primary_key_columns": ["tenant_id", "id"]'* ]]

composite_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT m.key_columns::text, m.writable FROM local_cache.mapping AS m WHERE m.namespace = 'pglc_attach_composite_smoke'"
)"
[[ "$composite_state" == "{tenant_id,id}|f" ]]

owned_trigger_oids_before="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT pg_catalog.string_agg(t.oid::text, ',' ORDER BY t.tgname) FROM pg_catalog.pg_trigger AS t WHERE t.tgrelid = 'public.pglc_attach_other_smoke'::regclass AND t.tgname IN ('pg_local_cache_statement_guard', 'pg_local_cache_row_invalidate', 'pg_local_cache_truncate_invalidate')"
)"
[[ -n "$owned_trigger_oids_before" ]]

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --tuples-only --no-align --set ON_ERROR_STOP=1 \
    --command \
    "SELECT local_cache.attach_table('public.pglc_attach_other_smoke'::regclass, false, 'pglc_attach_smoke')" \
    >/dev/null

owned_trigger_oids_after="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT pg_catalog.string_agg(t.oid::text, ',' ORDER BY t.tgname) FROM pg_catalog.pg_trigger AS t WHERE t.tgrelid = 'public.pglc_attach_other_smoke'::regclass AND t.tgname IN ('pg_local_cache_statement_guard', 'pg_local_cache_row_invalidate', 'pg_local_cache_truncate_invalidate')"
)"
[[ "$owned_trigger_oids_after" == "$owned_trigger_oids_before" ]]

owned_trigger_dependencies="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "WITH managed AS (SELECT t.oid FROM pg_catalog.pg_trigger AS t WHERE t.tgrelid = 'public.pglc_attach_other_smoke'::regclass AND t.tgname IN ('pg_local_cache_statement_guard', 'pg_local_cache_row_invalidate', 'pg_local_cache_truncate_invalidate')), dependency_counts AS (SELECT managed.oid, pg_catalog.count(dep.objid) AS dependency_count FROM managed LEFT JOIN (SELECT d.objid FROM pg_catalog.pg_depend AS d JOIN pg_catalog.pg_extension AS e ON e.oid = d.refobjid AND e.extname = 'pg_local_cache' WHERE d.classid = 'pg_catalog.pg_trigger'::regclass AND d.objsubid = 0 AND d.refclassid = 'pg_catalog.pg_extension'::regclass AND d.refobjsubid = 0 AND d.deptype = 'x') AS dep ON dep.objid = managed.oid GROUP BY managed.oid) SELECT pg_catalog.count(*), pg_catalog.sum(dependency_count), pg_catalog.min(dependency_count), pg_catalog.max(dependency_count) FROM dependency_counts"
)"
[[ "$owned_trigger_dependencies" == "3|3|1|1" ]]

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 <<'SQL'
CREATE FUNCTION public.pglc_foreign_reserved_trigger()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RETURN NULL;
END;
$function$;
CREATE TABLE public.pglc_foreign_trigger_smoke (
    id bigint PRIMARY KEY,
    value text NOT NULL
);
CREATE TRIGGER pg_local_cache_statement_guard
    BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE
    ON public.pglc_foreign_trigger_smoke
    FOR EACH STATEMENT
    EXECUTE FUNCTION public.pglc_foreign_reserved_trigger();
SQL

foreign_trigger_oid_before="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT oid FROM pg_catalog.pg_trigger WHERE tgrelid = 'public.pglc_foreign_trigger_smoke'::regclass AND tgname = 'pg_local_cache_statement_guard'"
)"
[[ -n "$foreign_trigger_oid_before" ]]

foreign_trigger_error="${temporary_directory}/attach-foreign-trigger.error"
if compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "SELECT local_cache.attach_table('public.pglc_foreign_trigger_smoke'::regclass, false, 'foreign-trigger-smoke')" \
    >"$foreign_trigger_error" 2>&1; then
    printf 'foreign reserved trigger was unexpectedly accepted\n' >&2
    exit 1
fi
grep -Fq 'reserved pg_local_cache trigger name' "$foreign_trigger_error"

foreign_trigger_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT (SELECT count(*) FROM local_cache.mapping WHERE namespace = 'foreign-trigger-smoke'), t.oid = ${foreign_trigger_oid_before}::oid, t.tgfoid = 'public.pglc_foreign_reserved_trigger()'::regprocedure, (SELECT count(*) FROM pg_catalog.pg_depend AS d JOIN pg_catalog.pg_extension AS e ON e.oid = d.refobjid AND e.extname = 'pg_local_cache' WHERE d.classid = 'pg_catalog.pg_trigger'::regclass AND d.objid = t.oid AND d.deptype = 'x'), pg_catalog.has_table_privilege('$worker_role', t.tgrelid, 'SELECT') FROM pg_catalog.pg_trigger AS t WHERE t.tgrelid = 'public.pglc_foreign_trigger_smoke'::regclass AND t.tgname = 'pg_local_cache_statement_guard'"
)"
[[ "$foreign_trigger_state" == "0|t|t|0|f" ]]

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "DROP TABLE public.pglc_foreign_trigger_smoke; DROP FUNCTION public.pglc_foreign_reserved_trigger()"

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "CREATE TABLE public.pglc_unmarked_trigger_smoke (id bigint PRIMARY KEY, value text NOT NULL); SELECT local_cache.attach_table('public.pglc_unmarked_trigger_smoke'::regclass, false, 'unmarked-trigger-smoke'); DROP TRIGGER pg_local_cache_statement_guard ON public.pglc_unmarked_trigger_smoke; CREATE TRIGGER pg_local_cache_statement_guard BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON public.pglc_unmarked_trigger_smoke FOR EACH STATEMENT EXECUTE FUNCTION local_cache._statement_guard()" \
    >/dev/null

unmarked_trigger_oid_before="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT oid FROM pg_catalog.pg_trigger WHERE tgrelid = 'public.pglc_unmarked_trigger_smoke'::regclass AND tgname = 'pg_local_cache_statement_guard'"
)"
[[ -n "$unmarked_trigger_oid_before" ]]

unmarked_trigger_error="${temporary_directory}/reconcile-unmarked-trigger.error"
if compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "SELECT local_cache.reconcile_table('public.pglc_unmarked_trigger_smoke'::regclass)" \
    >"$unmarked_trigger_error" 2>&1; then
    printf 'unmarked replacement trigger was unexpectedly accepted\n' >&2
    exit 1
fi
grep -Fq 'reserved pg_local_cache trigger name' "$unmarked_trigger_error"

unmarked_trigger_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT (SELECT count(*) FROM local_cache.mapping WHERE namespace = 'unmarked-trigger-smoke'), t.oid = ${unmarked_trigger_oid_before}::oid, t.tgfoid = 'local_cache._statement_guard()'::regprocedure, (SELECT count(*) FROM pg_catalog.pg_depend AS d JOIN pg_catalog.pg_extension AS e ON e.oid = d.refobjid AND e.extname = 'pg_local_cache' WHERE d.classid = 'pg_catalog.pg_trigger'::regclass AND d.objid = t.oid AND d.deptype = 'x') FROM pg_catalog.pg_trigger AS t WHERE t.tgrelid = 'public.pglc_unmarked_trigger_smoke'::regclass AND t.tgname = 'pg_local_cache_statement_guard'"
)"
[[ "$unmarked_trigger_state" == "1|t|t|0" ]]

[[ "$(compose exec -T postgres psql --username postgres --dbname "$database" \
    --no-psqlrc --tuples-only --no-align --set ON_ERROR_STOP=1 --command \
    "SELECT local_cache.detach_table('public.pglc_unmarked_trigger_smoke'::regclass)")" == "t" ]]
unmarked_detach_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT (SELECT count(*) FROM local_cache.mapping WHERE namespace = 'unmarked-trigger-smoke'), (SELECT count(*) FROM pg_catalog.pg_trigger WHERE tgrelid = 'public.pglc_unmarked_trigger_smoke'::regclass AND tgname LIKE 'pg_local_cache_%'), (SELECT oid = ${unmarked_trigger_oid_before}::oid FROM pg_catalog.pg_trigger WHERE tgrelid = 'public.pglc_unmarked_trigger_smoke'::regclass AND tgname = 'pg_local_cache_statement_guard'), pg_catalog.has_table_privilege('$worker_role', 'public.pglc_unmarked_trigger_smoke', 'SELECT')"
)"
[[ "$unmarked_detach_state" == "0|1|t|f" ]]

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "DROP TABLE public.pglc_unmarked_trigger_smoke"

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "CREATE TABLE public.pglc_drop_cleanup_smoke (id bigint PRIMARY KEY, value text NOT NULL); SELECT local_cache.attach_table('public.pglc_drop_cleanup_smoke'::regclass, false, 'drop-cleanup-smoke')" \
    >/dev/null
compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "DROP TABLE public.pglc_drop_cleanup_smoke"

drop_cleanup_count="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT count(*) FROM local_cache.mapping WHERE namespace = 'drop-cleanup-smoke'"
)"
[[ "$drop_cleanup_count" == "0" ]]

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "CREATE TABLE public.pglc_drop_cleanup_smoke (id bigint PRIMARY KEY, value text NOT NULL)"
drop_namespace_reuse="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT result->>'namespace' FROM (SELECT local_cache.attach_table('public.pglc_drop_cleanup_smoke'::regclass, false, 'drop-cleanup-smoke') AS result) AS attached"
)"
[[ "$drop_namespace_reuse" == "drop-cleanup-smoke" ]]
compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "SELECT local_cache.detach_table('public.pglc_drop_cleanup_smoke'::regclass); DROP TABLE public.pglc_drop_cleanup_smoke" \
    >/dev/null

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "CREATE TABLE public.pglc_drop_rollback_smoke (id bigint PRIMARY KEY, value text NOT NULL); SELECT local_cache.attach_table('public.pglc_drop_rollback_smoke'::regclass, false, 'drop-rollback-smoke')" \
    >/dev/null
compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "BEGIN; DROP TABLE public.pglc_drop_rollback_smoke; ROLLBACK" \
    >/dev/null

drop_rollback_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT pg_catalog.to_regclass('public.pglc_drop_rollback_smoke') IS NOT NULL, (SELECT count(*) FROM local_cache.mapping WHERE namespace = 'drop-rollback-smoke'), (SELECT count(*) FROM pg_catalog.pg_trigger WHERE tgrelid = 'public.pglc_drop_rollback_smoke'::regclass AND tgname LIKE 'pg_local_cache_%')"
)"
[[ "$drop_rollback_state" == "t|1|3" ]]
compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "SELECT local_cache.detach_table('public.pglc_drop_rollback_smoke'::regclass); DROP TABLE public.pglc_drop_rollback_smoke" \
    >/dev/null

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "DROP TRIGGER pg_local_cache_row_invalidate ON public.pglc_attach_composite_smoke; REVOKE ALL PRIVILEGES ON TABLE public.pglc_attach_composite_smoke FROM \"$worker_role\"" \
    >/dev/null

reconcile_precondition="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT (SELECT count(*) FROM pg_catalog.pg_trigger WHERE tgrelid = 'public.pglc_attach_composite_smoke'::regclass AND tgname LIKE 'pg_local_cache_%'), pg_catalog.has_table_privilege('$worker_role', 'public.pglc_attach_composite_smoke', 'SELECT')"
)"
[[ "$reconcile_precondition" == "2|f" ]]

reconcile_result="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT result->>'namespace' FROM (SELECT local_cache.reconcile_table('public.pglc_attach_composite_smoke'::regclass) AS result) AS reconciled"
)"
[[ "$reconcile_result" == "pglc_attach_composite_smoke" ]]

reconcile_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT (SELECT count(*) FROM pg_catalog.pg_trigger WHERE tgrelid = 'public.pglc_attach_composite_smoke'::regclass AND tgname LIKE 'pg_local_cache_%'), (SELECT count(*) FROM pg_catalog.pg_depend AS d JOIN pg_catalog.pg_extension AS e ON e.oid = d.refobjid AND e.extname = 'pg_local_cache' JOIN pg_catalog.pg_trigger AS t ON t.oid = d.objid WHERE d.classid = 'pg_catalog.pg_trigger'::regclass AND d.deptype = 'x' AND t.tgrelid = 'public.pglc_attach_composite_smoke'::regclass AND t.tgname LIKE 'pg_local_cache_%'), pg_catalog.has_table_privilege('$worker_role', 'public.pglc_attach_composite_smoke', 'SELECT')"
)"
[[ "$reconcile_state" == "3|3|t" ]]

reconcile_all_oids_before="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT pg_catalog.string_agg(t.oid::text, ',' ORDER BY t.oid) FROM pg_catalog.pg_trigger AS t JOIN local_cache.mapping AS m ON m.relation = t.tgrelid WHERE t.tgname LIKE 'pg_local_cache_%'"
)"
reconcile_all_state_first="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT local_cache.reconcile_all(), (SELECT count(*) FROM local_cache.mapping)"
)"
reconcile_all_state_second="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT local_cache.reconcile_all(), (SELECT count(*) FROM local_cache.mapping)"
)"
reconcile_all_oids_after="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT pg_catalog.string_agg(t.oid::text, ',' ORDER BY t.oid) FROM pg_catalog.pg_trigger AS t JOIN local_cache.mapping AS m ON m.relation = t.tgrelid WHERE t.tgname LIKE 'pg_local_cache_%'"
)"
mapping_count="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command "SELECT count(*) FROM local_cache.mapping"
)"
[[ "$reconcile_all_state_first" == "${mapping_count}|${mapping_count}" ]]
[[ "$reconcile_all_state_second" == "${mapping_count}|${mapping_count}" ]]
[[ "$reconcile_all_oids_after" == "$reconcile_all_oids_before" ]]

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "CREATE TABLE public.pglc_owned_trigger_rename_smoke (id bigint PRIMARY KEY, value text NOT NULL); SELECT local_cache.attach_table('public.pglc_owned_trigger_rename_smoke'::regclass, false, 'owned-trigger-rename-smoke')" \
    >/dev/null
owned_row_trigger_oid="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT oid FROM pg_catalog.pg_trigger WHERE tgrelid = 'public.pglc_owned_trigger_rename_smoke'::regclass AND tgname = 'pg_local_cache_row_invalidate'"
)"
[[ "$owned_row_trigger_oid" =~ ^[0-9]+$ ]]

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "ALTER TRIGGER pg_local_cache_row_invalidate ON public.pglc_owned_trigger_rename_smoke RENAME TO pglc_renamed_owned_row_invalidate; SELECT local_cache.reconcile_table('public.pglc_owned_trigger_rename_smoke'::regclass)" \
    >/dev/null

owned_trigger_repair_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "WITH owned AS (SELECT t.*, d.objid AS extension_dependency FROM pg_catalog.pg_trigger AS t JOIN pg_catalog.pg_depend AS d ON d.classid = 'pg_catalog.pg_trigger'::regclass AND d.objid = t.oid AND d.objsubid = 0 AND d.refclassid = 'pg_catalog.pg_extension'::regclass AND d.refobjsubid = 0 AND d.deptype = 'x' JOIN pg_catalog.pg_extension AS e ON e.oid = d.refobjid AND e.extname = 'pg_local_cache' WHERE t.tgrelid = 'public.pglc_owned_trigger_rename_smoke'::regclass AND t.tgfoid IN ('local_cache._statement_guard()'::regprocedure, 'local_cache._row_invalidate()'::regprocedure, 'local_cache._truncate_invalidate()'::regprocedure)) SELECT count(DISTINCT oid), count(DISTINCT oid) FILTER (WHERE tgname IN ('pg_local_cache_statement_guard', 'pg_local_cache_row_invalidate', 'pg_local_cache_truncate_invalidate')), (SELECT count(*) FROM pg_catalog.pg_trigger WHERE tgrelid = 'public.pglc_owned_trigger_rename_smoke'::regclass AND tgname = 'pglc_renamed_owned_row_invalidate'), bool_and(tgenabled = 'A'), count(extension_dependency), count(*) FILTER (WHERE tgname = 'pg_local_cache_row_invalidate' AND tgfoid = 'local_cache._row_invalidate()'::regprocedure AND tgtype = 29 AND oid <> ${owned_row_trigger_oid}::oid) FROM owned"
)"
[[ "$owned_trigger_repair_state" == "3|3|0|t|3|1" ]]

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "ALTER TRIGGER pg_local_cache_truncate_invalidate ON public.pglc_owned_trigger_rename_smoke RENAME TO pglc_renamed_owned_truncate_invalidate"
[[ "$(compose exec -T postgres psql --username postgres --dbname "$database" \
    --no-psqlrc --tuples-only --no-align --set ON_ERROR_STOP=1 --command \
    "SELECT local_cache.detach_table('public.pglc_owned_trigger_rename_smoke'::regclass)")" == "t" ]]
owned_trigger_detach_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT (SELECT count(*) FROM local_cache.mapping WHERE namespace = 'owned-trigger-rename-smoke'), (SELECT count(*) FROM pg_catalog.pg_trigger AS t JOIN pg_catalog.pg_depend AS d ON d.classid = 'pg_catalog.pg_trigger'::regclass AND d.objid = t.oid AND d.deptype = 'x' JOIN pg_catalog.pg_extension AS e ON e.oid = d.refobjid AND e.extname = 'pg_local_cache' WHERE t.tgrelid = 'public.pglc_owned_trigger_rename_smoke'::regclass AND t.tgfoid IN ('local_cache._statement_guard()'::regprocedure, 'local_cache._row_invalidate()'::regprocedure, 'local_cache._truncate_invalidate()'::regprocedure)), (SELECT count(*) FROM pg_catalog.pg_trigger WHERE tgrelid = 'public.pglc_owned_trigger_rename_smoke'::regclass AND tgname IN ('pg_local_cache_statement_guard', 'pg_local_cache_row_invalidate', 'pg_local_cache_truncate_invalidate', 'pglc_renamed_owned_truncate_invalidate')), pg_catalog.has_table_privilege('$worker_role', 'public.pglc_owned_trigger_rename_smoke', 'SELECT')"
)"
[[ "$owned_trigger_detach_state" == "0|0|0|f" ]]
compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "DROP TABLE public.pglc_owned_trigger_rename_smoke"

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "CREATE SCHEMA pglc_schema_rename_smoke; CREATE TABLE pglc_schema_rename_smoke.pglc_schema_table_smoke (id bigint PRIMARY KEY, value text NOT NULL); INSERT INTO pglc_schema_rename_smoke.pglc_schema_table_smoke VALUES (1, 'schema-row'); SELECT local_cache.attach_table('pglc_schema_rename_smoke.pglc_schema_table_smoke'::regclass, false, 'schema-rename-smoke')" \
    >/dev/null
schema_relation_oid="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT 'pglc_schema_rename_smoke.pglc_schema_table_smoke'::regclass::oid"
)"
schema_trigger_oids_before="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT pg_catalog.string_agg(t.oid::text, ',' ORDER BY t.tgname) FROM pg_catalog.pg_trigger AS t WHERE t.tgrelid = ${schema_relation_oid}::oid AND t.tgname IN ('pg_local_cache_statement_guard', 'pg_local_cache_row_invalidate', 'pg_local_cache_truncate_invalidate')"
)"
[[ "$schema_relation_oid" =~ ^[0-9]+$ ]]
[[ -n "$schema_trigger_oids_before" ]]

check_schema_wire_name() {
    local expected_schema="$1"
    local rejected_schema="${2:-}"
    PGLC_RESP_HELPER="${repository_directory}/tests/whole_row_integration.py" \
    PGLC_EXPECTED_SCHEMA="$expected_schema" \
    PGLC_REJECTED_SCHEMA="$rejected_schema" \
    PGDATABASE="$database" \
    PG_LOCAL_CACHE_RESP_HOST="127.0.0.1" \
    PG_LOCAL_CACHE_RESP_PORT="$cache_host_port" \
    PG_LOCAL_CACHE_AUTH_TOKEN="$auth_token" \
    PG_LOCAL_CACHE_TEST_APP_ROLE="$app_role" \
    PG_LOCAL_CACHE_TEST_APP_PASSWORD="$app_password" \
        python3 -B - <<'PY'
import importlib.util
import json
import os
import time

spec = importlib.util.spec_from_file_location(
    "pglc_whole_row_helper", os.environ["PGLC_RESP_HELPER"]
)
assert spec is not None and spec.loader is not None
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

database = os.environ["PGDATABASE"]
expected_schema = os.environ["PGLC_EXPECTED_SCHEMA"]
rejected_schema = os.environ["PGLC_REJECTED_SCHEMA"]


def key(schema: str) -> str:
    return (
        f"CRUD:{database}.{schema}.pglc_schema_table_smoke:"
        + json.dumps({"id": 1}, separators=(",", ":"))
    )


client = helper.RespClient()
try:
    deadline = time.monotonic() + 10
    last = None
    while time.monotonic() < deadline:
        try:
            last = helper.mget_one(client, key(expected_schema))
            if isinstance(last, str) and json.loads(last).get("value") == "schema-row":
                break
        except (helper.RespError, json.JSONDecodeError) as error:
            last = error
        time.sleep(0.05)
    else:
        raise AssertionError(
            f"whole-row mapping did not load schema {expected_schema!r}: {last!r}"
        )

    if rejected_schema:
        deadline = time.monotonic() + 10
        last = None
        while time.monotonic() < deadline:
            try:
                last = helper.mget_one(client, key(rejected_schema))
            except helper.RespError as error:
                last = error
                if "unknown KVik table mapping" in str(error):
                    break
            time.sleep(0.05)
        else:
            raise AssertionError(
                f"stale whole-row mapping remained for schema {rejected_schema!r}: "
                f"{last!r}"
            )
finally:
    client.close()
PY
}

wait_for_mapping_health() {
    local mapping_health=""
    for _attempt in {1..60}; do
        mapping_health="$(
            compose exec -T postgres \
                psql --username postgres --dbname "$database" --no-psqlrc \
                --tuples-only --no-align --set ON_ERROR_STOP=1 \
                --command \
                "SELECT (local_cache.health()->>'ready')::boolean, (local_cache.health()->>'workers_with_incomplete_mappings')::bigint"
        )"
        if [[ "$mapping_health" == "t|0" ]]; then
            return 0
        fi
        sleep 0.1
    done
    printf 'mapping health did not recover after DDL: %s\n' \
        "$mapping_health" >&2
    return 1
}

wait_for_mapping_incomplete() {
    local mapping_health=""
    for _attempt in {1..60}; do
        mapping_health="$(
            compose exec -T postgres \
                psql --username postgres --dbname "$database" --no-psqlrc \
                --tuples-only --no-align --set ON_ERROR_STOP=1 \
                --command \
                "SELECT (local_cache.health()->>'ready')::boolean, (local_cache.health()->>'workers_with_incomplete_mappings')::bigint"
        )"
        if [[ "$mapping_health" =~ ^f\|[1-9][0-9]*$ ]]; then
            return 0
        fi
        sleep 0.1
    done
    printf 'mapping health did not fail closed after invalid DDL: %s\n' \
        "$mapping_health" >&2
    return 1
}

check_schema_wire_name pglc_schema_rename_smoke
compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "ALTER SCHEMA pglc_schema_rename_smoke RENAME TO pglc_schema_renamed_smoke"
check_schema_wire_name pglc_schema_renamed_smoke pglc_schema_rename_smoke
wait_for_mapping_health

schema_rename_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT m.relation::oid = ${schema_relation_oid}::oid, pg_catalog.to_regclass('pglc_schema_renamed_smoke.pglc_schema_table_smoke')::oid = ${schema_relation_oid}::oid, (SELECT count(*) FROM pg_catalog.pg_trigger WHERE tgrelid = ${schema_relation_oid}::oid AND tgname IN ('pg_local_cache_statement_guard', 'pg_local_cache_row_invalidate', 'pg_local_cache_truncate_invalidate')), (SELECT count(*) FROM pg_catalog.pg_trigger WHERE tgrelid = ${schema_relation_oid}::oid AND tgname IN ('pg_local_cache_statement_guard', 'pg_local_cache_row_invalidate', 'pg_local_cache_truncate_invalidate') AND tgenabled = 'A'), (SELECT count(*) FROM pg_catalog.pg_trigger AS t JOIN pg_catalog.pg_depend AS d ON d.classid = 'pg_catalog.pg_trigger'::regclass AND d.objid = t.oid AND d.deptype = 'x' JOIN pg_catalog.pg_extension AS e ON e.oid = d.refobjid AND e.extname = 'pg_local_cache' WHERE t.tgrelid = ${schema_relation_oid}::oid), pg_catalog.has_schema_privilege('$worker_role', 'pglc_schema_renamed_smoke', 'USAGE'), pg_catalog.has_table_privilege('$worker_role', ${schema_relation_oid}::oid, 'SELECT'), (local_cache.health()->>'ready')::boolean, (local_cache.health()->>'workers_with_incomplete_mappings')::bigint FROM local_cache.mapping AS m WHERE m.namespace = 'schema-rename-smoke'"
)"
[[ "$schema_rename_state" == "t|t|3|3|3|t|t|t|0" ]]

schema_reconcile_result="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT result->>'relation' FROM (SELECT local_cache.reconcile_table(${schema_relation_oid}::oid::regclass) AS result) AS reconciled"
)"
[[ "$schema_reconcile_result" == "pglc_schema_renamed_smoke.pglc_schema_table_smoke" ]]

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "BEGIN; ALTER SCHEMA pglc_schema_renamed_smoke RENAME TO pglc_schema_rollback_smoke; ROLLBACK" \
    >/dev/null
check_schema_wire_name pglc_schema_renamed_smoke pglc_schema_rollback_smoke
wait_for_mapping_health

schema_trigger_oids_after="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT pg_catalog.string_agg(t.oid::text, ',' ORDER BY t.tgname) FROM pg_catalog.pg_trigger AS t WHERE t.tgrelid = ${schema_relation_oid}::oid AND t.tgname IN ('pg_local_cache_statement_guard', 'pg_local_cache_row_invalidate', 'pg_local_cache_truncate_invalidate')"
)"
schema_rollback_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT pg_catalog.to_regnamespace('pglc_schema_renamed_smoke') IS NOT NULL, pg_catalog.to_regnamespace('pglc_schema_rollback_smoke') IS NULL, (SELECT relation::oid = ${schema_relation_oid}::oid FROM local_cache.mapping WHERE namespace = 'schema-rename-smoke'), pg_catalog.has_schema_privilege('$worker_role', 'pglc_schema_renamed_smoke', 'USAGE'), pg_catalog.has_table_privilege('$worker_role', ${schema_relation_oid}::oid, 'SELECT'), (local_cache.health()->>'ready')::boolean, (local_cache.health()->>'workers_with_incomplete_mappings')::bigint"
)"
[[ "$schema_trigger_oids_after" == "$schema_trigger_oids_before" ]]
[[ "$schema_rollback_state" == "t|t|t|t|t|t|0" ]]

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "SELECT local_cache.detach_table(${schema_relation_oid}::oid::regclass); DROP TABLE pglc_schema_renamed_smoke.pglc_schema_table_smoke; DROP SCHEMA pglc_schema_renamed_smoke" \
    >/dev/null

# Worker-side catalog validation must fail closed if privileges, ownership, or
# extension membership drift after a successful attach.  Each recovery path
# keeps the mapping and restores readiness only after the invariant is fixed.
compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "CREATE TABLE public.pglc_worker_drift_smoke (id bigint PRIMARY KEY, value text NOT NULL); SELECT local_cache.attach_table('public.pglc_worker_drift_smoke'::regclass, false, 'worker-drift-smoke')" \
    >/dev/null

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "GRANT INSERT ON TABLE public.pglc_worker_drift_smoke TO \"$worker_role\""
wait_for_mapping_incomplete

read_only_drift_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT count(*), pg_catalog.has_table_privilege('$worker_role', relation, 'INSERT') FROM local_cache.mapping WHERE namespace = 'worker-drift-smoke' GROUP BY relation"
)"
[[ "$read_only_drift_state" == "1|t" ]]

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "SELECT local_cache.reconcile_table('public.pglc_worker_drift_smoke'::regclass)" \
    >/dev/null
wait_for_mapping_health

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "ALTER TABLE public.pglc_worker_drift_smoke OWNER TO \"$worker_role\""
wait_for_mapping_incomplete

worker_owner_drift_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT count(*), c.relowner = '$worker_role'::regrole FROM local_cache.mapping AS m JOIN pg_catalog.pg_class AS c ON c.oid = m.relation WHERE m.namespace = 'worker-drift-smoke' GROUP BY c.relowner"
)"
[[ "$worker_owner_drift_state" == "1|t" ]]

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "ALTER TABLE public.pglc_worker_drift_smoke OWNER TO postgres; SELECT local_cache.reconcile_table('public.pglc_worker_drift_smoke'::regclass)" \
    >/dev/null
wait_for_mapping_health

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "ALTER EXTENSION plpgsql ADD TABLE public.pglc_worker_drift_smoke"
wait_for_mapping_incomplete

extension_member_drift_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT count(*), EXISTS (SELECT 1 FROM pg_catalog.pg_depend AS d JOIN pg_catalog.pg_extension AS e ON e.oid = d.refobjid AND e.extname = 'plpgsql' WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = 'public.pglc_worker_drift_smoke'::regclass AND d.deptype = 'e') FROM local_cache.mapping WHERE namespace = 'worker-drift-smoke'"
)"
[[ "$extension_member_drift_state" == "1|t" ]]

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "ALTER EXTENSION plpgsql DROP TABLE public.pglc_worker_drift_smoke"
wait_for_mapping_health

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "SELECT local_cache.detach_table('public.pglc_worker_drift_smoke'::regclass); DROP TABLE public.pglc_worker_drift_smoke" \
    >/dev/null

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "CREATE TABLE public.pglc_oid_race_smoke (id bigint PRIMARY KEY, value text NOT NULL)"
race_original_oid="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command "SELECT 'public.pglc_oid_race_smoke'::regclass::oid"
)"
[[ "$race_original_oid" =~ ^[0-9]+$ ]]

race_ddl_log="${temporary_directory}/oid-race-ddl.log"
compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "BEGIN; ALTER TABLE public.pglc_oid_race_smoke RENAME TO pglc_oid_race_original_smoke; CREATE TABLE public.pglc_oid_race_smoke (id bigint PRIMARY KEY, value text NOT NULL); SELECT pg_catalog.pg_sleep(3); COMMIT" \
    >"$race_ddl_log" 2>&1 &
race_ddl_pid=$!

race_lock_observed=false
for _attempt in {1..30}; do
    race_lock_state="$(
        compose exec -T postgres \
            psql --username postgres --dbname "$database" --no-psqlrc \
            --tuples-only --no-align --set ON_ERROR_STOP=1 \
            --command \
            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_locks WHERE relation = ${race_original_oid}::oid AND mode = 'AccessExclusiveLock' AND granted)"
    )"
    if [[ "$race_lock_state" == "t" ]]; then
        race_lock_observed=true
        break
    fi
    sleep 0.1
done
if [[ "$race_lock_observed" != true ]]; then
    wait "$race_ddl_pid" || true
    cat "$race_ddl_log" >&2
    printf 'did not observe the concurrent rename lock\n' >&2
    exit 1
fi

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "SELECT local_cache.attach_table(${race_original_oid}::oid::regclass, false, 'oid-race-smoke')" \
    >/dev/null
wait "$race_ddl_pid"

race_attach_state="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT m.relation::oid = ${race_original_oid}::oid, (SELECT count(*) FROM pg_catalog.pg_trigger WHERE tgrelid = ${race_original_oid}::oid AND tgname LIKE 'pg_local_cache_%'), pg_catalog.has_table_privilege('$worker_role', ${race_original_oid}::oid, 'SELECT'), (SELECT count(*) FROM pg_catalog.pg_trigger WHERE tgrelid = 'public.pglc_oid_race_smoke'::regclass AND tgname LIKE 'pg_local_cache_%'), pg_catalog.has_table_privilege('$worker_role', 'public.pglc_oid_race_smoke', 'SELECT') FROM local_cache.mapping AS m WHERE m.namespace = 'oid-race-smoke'"
)"
[[ "$race_attach_state" == "t|3|t|0|f" ]]
compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "SELECT local_cache.detach_table(${race_original_oid}::oid::regclass); DROP TABLE public.pglc_oid_race_original_smoke, public.pglc_oid_race_smoke" \
    >/dev/null

mapping_dump="$(
    compose exec -T postgres pg_dump --username postgres --dbname "$database" \
        --data-only --no-owner --no-privileges
)"
grep -Fq 'COPY local_cache.mapping' <<<"$mapping_dump"
grep -Fq 'pglc_attach_composite_smoke' <<<"$mapping_dump"

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "DROP TABLE public.pglc_attach_smoke, public.pglc_attach_composite_smoke, public.pglc_attach_other_smoke, public.pglc_attach_native_smoke, public.pglc_attach_no_pk_smoke, public.pglc_attach_bad_key_smoke, public.pglc_attach_rls_smoke"

extension_version="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT extversion FROM pg_catalog.pg_extension WHERE extname = 'pg_local_cache'"
)"
expected_extension_version="$(
    sed -n "s/^default_version = '\([^']*\)'$/\1/p" pg_local_cache.control
)"
[[ -n "$expected_extension_version" ]]
[[ "$extension_version" == "$expected_extension_version" ]]

check_auth_framing
printf '%s\r\n' "$auth_token" >"$cache_secret"
compose restart postgres >/dev/null
compose up --detach --wait >/dev/null
check_auth_framing
printf '%s' "$auth_token" >"$cache_secret"
compose restart postgres >/dev/null
compose up --detach --wait >/dev/null
check_auth_framing

worker_count="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 \
        --command \
        "SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE backend_type = 'pg_local_cache RESP worker'"
)"
[[ "$worker_count" == "8" ]]

PG_LOCAL_CACHE_PSQL="$psql_wrapper" \
PGHOST="/var/run/postgresql" \
PGPORT="5432" \
PGDATABASE="$database" \
PG_LOCAL_CACHE_RESP_HOST="127.0.0.1" \
PG_LOCAL_CACHE_RESP_PORT="$cache_host_port" \
PG_LOCAL_CACHE_AUTH_TOKEN="$auth_token" \
PG_LOCAL_CACHE_TEST_ROLE="$worker_role" \
PG_LOCAL_CACHE_TEST_WRITER_ROLE="$app_role" \
PG_LOCAL_CACHE_TEST_WRITER_PASSWORD="$app_password" \
PG_LOCAL_CACHE_TEST_WRITER_HOST="127.0.0.1" \
    python3 -B "${repository_directory}/tests/pipeline_integration.py"

PG_LOCAL_CACHE_PSQL="$psql_wrapper" \
PGHOST="/var/run/postgresql" \
PGPORT="5432" \
PGDATABASE="$database" \
PG_LOCAL_CACHE_RESP_HOST="127.0.0.1" \
PG_LOCAL_CACHE_RESP_PORT="$cache_host_port" \
PG_LOCAL_CACHE_AUTH_TOKEN="$auth_token" \
PG_LOCAL_CACHE_TEST_APP_ROLE="$app_role" \
PG_LOCAL_CACHE_TEST_APP_PASSWORD="$app_password" \
PG_LOCAL_CACHE_TEST_APP_HOST="127.0.0.1" \
POSTGRES_MAJOR="$postgres_major" \
    python3 -B "${repository_directory}/tests/sql_mget_integration.py"

PG_LOCAL_CACHE_PSQL="$psql_wrapper" \
PGHOST="/var/run/postgresql" \
PGPORT="5432" \
PGDATABASE="$database" \
PG_LOCAL_CACHE_RESP_HOST="127.0.0.1" \
PG_LOCAL_CACHE_RESP_PORT="$cache_host_port" \
PG_LOCAL_CACHE_AUTH_TOKEN="$auth_token" \
PG_LOCAL_CACHE_TEST_APP_ROLE="$app_role" \
PG_LOCAL_CACHE_TEST_APP_PASSWORD="$app_password" \
PG_LOCAL_CACHE_TEST_APP_HOST="127.0.0.1" \
    python3 -B "${repository_directory}/tests/whole_row_integration.py"

PG_LOCAL_CACHE_PSQL="$psql_wrapper" \
PGHOST="/var/run/postgresql" \
PGPORT="5432" \
PGDATABASE="$database" \
PG_LOCAL_CACHE_RESP_HOST="127.0.0.1" \
PG_LOCAL_CACHE_RESP_PORT="$cache_host_port" \
PG_LOCAL_CACHE_AUTH_TOKEN="$auth_token" \
PG_LOCAL_CACHE_TEST_APP_ROLE="$app_role" \
    python3 -B "${repository_directory}/tests/oom_monitoring_integration.py"

compose exec -T postgres /usr/local/bin/pg_local_cache_healthcheck

binary_identity="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 --command \
        "SELECT current_setting('pg_local_cache.binary_version'), current_setting('pg_local_cache.binary_build_id')"
)"
[[ "$binary_identity" == "${expected_extension_version}|${build_id}" ]]
if compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "SET pg_local_cache.binary_version = 'spoof'" >/dev/null 2>&1; then
    printf 'session SET unexpectedly overrode binary identity\n' >&2
    exit 1
fi
if compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command \
    "ALTER SYSTEM SET pg_local_cache.binary_build_id = 'spoof'" >/dev/null 2>&1; then
    printf 'ALTER SYSTEM unexpectedly overrode binary identity\n' >&2
    exit 1
fi
if compose exec -T --env PGOPTIONS="-c pg_local_cache.binary_version=spoof" postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --command "SELECT 1" >/dev/null 2>&1; then
    printf 'PGOPTIONS unexpectedly overrode binary identity\n' >&2
    exit 1
fi
compose exec -T --user postgres postgres sh -eu <<'SH'
cp "$PGDATA/postgresql.auto.conf" /tmp/postgresql.auto.conf.phase2
printf "pg_local_cache.binary_version = 'spoof'\n" >> "$PGDATA/postgresql.auto.conf"
SH
config_spoof_errors="$(
    compose exec -T postgres \
        psql --username postgres --dbname "$database" --no-psqlrc \
        --tuples-only --no-align --set ON_ERROR_STOP=1 --command \
        "SELECT pg_reload_conf(); SELECT count(*) FROM pg_catalog.pg_file_settings WHERE name = 'pg_local_cache.binary_version' AND error IS NOT NULL"
)"
[[ "$config_spoof_errors" == $'t\n1' ]]
compose exec -T --user postgres postgres sh -eu <<'SH'
cp /tmp/postgresql.auto.conf.phase2 "$PGDATA/postgresql.auto.conf"
rm /tmp/postgresql.auto.conf.phase2
SH
compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command "SELECT pg_reload_conf()" >/dev/null

compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 --command "DROP EXTENSION pg_local_cache CASCADE"
container_id="$(compose ps --quiet postgres)"
package_root="/tmp/pg_local_cache-phase2"
if [[ -n "$release_root" ]]; then
    docker cp "$release_root/." "$container_id:${package_root}/"
else
    compose exec -T --user root \
        --env PGLC_BUILD_ID="$build_id" \
        --env PGLC_VERSION="$expected_extension_version" \
        --env PGLC_MAJOR="$postgres_major" \
        --env PGLC_VARIANT="$postgres_variant" \
        postgres sh -eu <<'SH'
package_root=/tmp/pg_local_cache-phase2
pkglibdir="$(pg_config --pkglibdir)"
extension_dir="$(pg_config --sharedir)/extension"
case "$PGLC_VARIANT" in bookworm) libc=glibc ;; alpine3.23) libc=musl ;; esac
architecture="$(uname -m)"
case "$architecture" in x86_64|amd64) architecture=amd64 ;; esac
install -d "$package_root/lib" "$package_root/share/extension"
install -m 0755 "$pkglibdir/pg_local_cache.so" "$package_root/lib/"
install -m 0644 "$extension_dir/pg_local_cache.control" "$package_root/share/extension/"
for sql_file in "$extension_dir"/pg_local_cache--*.sql; do
    install -m 0644 "$sql_file" "$package_root/share/extension/"
done
printf '%s\n' "$PGLC_BUILD_ID" > "$package_root/BUILD-ID"
{
    printf 'format=1\nversion=%s\npostgres_major=%s\nos=linux\n' "$PGLC_VERSION" "$PGLC_MAJOR"
    printf 'libc=%s\narchitecture=%s\ncommit=%s\n' "$libc" "$architecture" "$PGLC_BUILD_ID"
    printf 'build_image=phase2-runtime\n'
} > "$package_root/RELEASE-METADATA"
SH
    docker cp "${repository_directory}/scripts/install-existing.sh" \
        "$container_id:${package_root}/install.sh"
fi
compose exec -T --user root postgres chmod 0755 "$package_root/install.sh"
install_output="$(
    compose exec -T --user root postgres \
        "$package_root/install.sh" install \
        --database "$database" --postgres-os-user postgres \
        --pg-config pg_config --psql psql \
        --state-root /var/lib/postgresql/data/pg_local_cache-install-state
)"
state_directory="$(printf '%s\n' "$install_output" | sed -n 's/^.*state directory: //p' | tail -n 1)"
[[ "$state_directory" == /var/lib/postgresql/data/pg_local_cache-install-state/* ]]
compose restart postgres >/dev/null
for _attempt in {1..60}; do
    if compose exec -T postgres pg_isready --username postgres --dbname "$database" \
        >/dev/null 2>&1; then
        break
    fi
    sleep 0.2
done
compose exec -T postgres pg_isready --username postgres --dbname "$database" \
    >/dev/null
compose exec -T --user postgres postgres \
    "$package_root/install.sh" verify --state-directory "$state_directory" \
    --postgres-os-user postgres --pg-config pg_config --psql psql
compose up --detach --wait >/dev/null
compose exec -T postgres \
    psql --username postgres --dbname "$database" --no-psqlrc \
    --set ON_ERROR_STOP=1 <<'SQL'
CREATE TABLE public.phase2_resp (id bigint PRIMARY KEY, payload text NOT NULL);
INSERT INTO public.phase2_resp VALUES (1, 'fresh-installer-cache');
SELECT local_cache.attach_table('public.phase2_resp'::regclass);
SQL
PG_LOCAL_CACHE_AUTH_TOKEN="$auth_token" \
PG_LOCAL_CACHE_RESP_PORT="$cache_host_port" \
PG_LOCAL_CACHE_DATABASE="$database" \
    python3 -B - <<'PY'
import os
import socket


def command(*parts: bytes) -> bytes:
    payload = b"*" + str(len(parts)).encode() + b"\r\n"
    payload += b"".join(
        b"$" + str(len(part)).encode() + b"\r\n" + part + b"\r\n"
        for part in parts
    )
    with socket.create_connection(
        ("127.0.0.1", int(os.environ["PG_LOCAL_CACHE_RESP_PORT"])), timeout=5
    ) as client:
        token = os.environ["PG_LOCAL_CACHE_AUTH_TOKEN"].encode()
        auth = b"*2\r\n$4\r\nAUTH\r\n$" + str(len(token)).encode() + b"\r\n" + token + b"\r\n"
        client.sendall(auth)
        assert client.recv(256) == b"+OK\r\n"
        client.sendall(payload)
        return client.recv(4096)


key = f'CRUD:{os.environ["PG_LOCAL_CACHE_DATABASE"]}.public.phase2_resp:{{"id":1}}'.encode()
response = command(b"MGET", key)
assert response.startswith(b"*1\r\n$") and b"fresh-installer-cache" in response, response
PY
check_auth_framing

printf 'docker smoke test passed (PostgreSQL %s, RESP %s)\n' \
    "$postgres_host_port" "$cache_host_port"
