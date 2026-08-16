#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(< /proc/1/comm)" == "postgres" ]]

postgres_user="${POSTGRES_USER:-postgres}"
database="${PG_LOCAL_CACHE_DATABASE:-${POSTGRES_DB:-$postgres_user}}"
port="${PG_LOCAL_CACHE_PORT:-6380}"
runtime_token="/run/pg_local_cache/auth_token"
source_token="${PG_LOCAL_CACHE_AUTH_TOKEN_FILE:-/run/secrets/pg_local_cache_auth_token}"

pg_isready \
    --quiet \
    --host /var/run/postgresql \
    --username "$postgres_user" \
    --dbname "$database"

sql_status="$(
    psql \
        --no-psqlrc \
        --quiet \
        --tuples-only \
        --no-align \
        --set ON_ERROR_STOP=1 \
        --host /var/run/postgresql \
        --username "$postgres_user" \
        --dbname "$database" \
        <<'SQL'
SELECT CASE
    WHEN EXISTS (
        SELECT 1
          FROM pg_catalog.pg_extension
         WHERE extname = 'pg_local_cache'
    )
    AND pg_catalog.jsonb_typeof(local_cache.stats()) = 'object'
    AND COALESCE(
        (local_cache.health() ->> 'ready')::boolean,
        false
    )
    AND (
        SELECT count(*)
          FROM pg_catalog.pg_stat_activity
         WHERE backend_type = 'pg_local_cache RESP worker'
    ) = CASE
        WHEN pg_catalog.current_setting('pg_local_cache.port')::integer = 0
            THEN 0
        ELSE pg_catalog.current_setting('pg_local_cache.workers')::integer
        END
    THEN 'ready'
    ELSE 'not-ready'
END;
SQL
)"
[[ "$sql_status" == "ready" ]]

if (( port == 0 )); then
    exit 0
fi

token_file="$source_token"
if [[ -f "$runtime_token" ]]; then
    token_file="$runtime_token"
fi
[[ -f "$token_file" && ! -L "$token_file" ]]
[[ "$(stat -c '%a' "$token_file")" == "600" ]]
token_bytes="$(stat -c '%s' "$token_file")"
auth_token="$(<"$token_file")"
token_suffix_bytes=0
token_last_byte="$(tail -c 1 "$token_file" | od -An -tu1 | tr -d ' \n')"
token_last_two="$(tail -c 2 "$token_file" | od -An -tx1 | tr -d ' \n')"
if [[ "$auth_token" == *$'\r' && "$token_last_two" == "0d0a" ]]; then
    auth_token="${auth_token%$'\r'}"
    token_suffix_bytes=2
elif (( token_bytes == ${#auth_token} + 1 )) && [[ "$token_last_byte" == "10" ]]; then
    token_suffix_bytes=1
fi
(( token_bytes == ${#auth_token} + token_suffix_bytes ))
(( ${#auth_token} >= 32 && ${#auth_token} <= 256 ))
[[ "$auth_token" != *[!A-Za-z0-9_-]* ]]

exec 3<>"/dev/tcp/127.0.0.1/${port}"
printf '*2\r\n$4\r\nAUTH\r\n$%d\r\n%s\r\n' \
    "${#auth_token}" "$auth_token" >&3
IFS= read -r -t 2 auth_response <&3
[[ "$auth_response" == $'+OK\r' ]]

printf '*1\r\n$4\r\nPING\r\n' >&3
IFS= read -r -t 2 ping_response <&3
[[ "$ping_response" == $'+PONG\r' ]]

exec 3>&-
exec 3<&-
