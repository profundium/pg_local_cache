\echo Use "ALTER EXTENSION pg_local_cache UPDATE TO '3.0.0'" to load this file. \quit

DROP FUNCTION mget(regclass, anyarray);

CREATE OR REPLACE FUNCTION _row_invalidate()
RETURNS trigger
AS 'MODULE_PATHNAME', 'pg_local_cache_row_invalidate'
LANGUAGE C;

CREATE OR REPLACE FUNCTION _truncate_invalidate()
RETURNS trigger
AS 'MODULE_PATHNAME', 'pg_local_cache_truncate_invalidate'
LANGUAGE C;

CREATE OR REPLACE FUNCTION _statement_guard()
RETURNS trigger
AS 'MODULE_PATHNAME', 'pg_local_cache_statement_guard'
LANGUAGE C;

CREATE OR REPLACE FUNCTION _lock_relation(relation_oid oid)
RETURNS boolean
AS 'MODULE_PATHNAME', 'pg_local_cache_lock_relation'
LANGUAGE C STRICT VOLATILE PARALLEL UNSAFE;

CREATE OR REPLACE FUNCTION _reload()
RETURNS void
AS 'MODULE_PATHNAME', 'pg_local_cache_reload'
LANGUAGE C;

CREATE OR REPLACE FUNCTION _forget(namespace text, relation oid)
RETURNS void
AS 'MODULE_PATHNAME', 'pg_local_cache_forget'
LANGUAGE C STRICT;

CREATE OR REPLACE FUNCTION invalidate(namespace text)
RETURNS bigint
AS 'MODULE_PATHNAME', 'pg_local_cache_invalidate'
LANGUAGE C STRICT;

CREATE OR REPLACE FUNCTION stats()
RETURNS jsonb
AS 'MODULE_PATHNAME', 'pg_local_cache_stats'
LANGUAGE C STABLE;

CREATE OR REPLACE FUNCTION _metrics_json()
RETURNS jsonb
AS 'MODULE_PATHNAME', 'pg_local_cache_metrics_json'
LANGUAGE C STABLE PARALLEL RESTRICTED;

ALTER FUNCTION local_cache.metrics() RENAME TO _metrics_2_0_4;

CREATE FUNCTION metrics()
RETURNS TABLE (
    up bigint,
    cache_capacity bigint,
    entries bigint,
    relation_states bigint,
    relation_state_capacity bigint,
    global_dirty_writers bigint,
    active_clients bigint,
    peak_active_clients bigint,
    max_clients bigint,
    client_slots bigint,
    workers_configured bigint,
    workers_running bigint,
    shared_memory_bytes bigint,
    worker_memory_bytes bigint,
    estimated_memory_bytes bigint,
    memory_budget_bytes bigint,
    cache_hits_total bigint,
    cache_misses_total bigint,
    negative_hits_total bigint,
    database_reads_total bigint,
    database_writes_total bigint,
    invalidations_total bigint,
    evictions_total bigint,
    singleflight_leaders_total bigint,
    singleflight_waiters_total bigint,
    singleflight_reuses_total bigint,
    singleflight_timeouts_total bigint,
    rejected_connections_total bigint,
    client_limit_rejections_total bigint,
    authentication_failures_total bigint,
    protocol_errors_total bigint,
    output_backpressure_events_total bigint,
    slow_client_drops_total bigint,
    worker_starts_total bigint,
    dirty_key_limit_fallbacks_total bigint,
    mapping_reload_failures_total bigint,
    workers_with_incomplete_mappings bigint,
    mapping_reload_incomplete_retries_total bigint
)
LANGUAGE sql
STABLE
PARALLEL RESTRICTED
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH snapshot AS MATERIALIZED (
    SELECT local_cache._metrics_json() AS payload
)
SELECT
    (payload ->> 'up')::bigint,
    (payload ->> 'cache_capacity')::bigint,
    (payload ->> 'entries')::bigint,
    (payload ->> 'relation_states')::bigint,
    (payload ->> 'relation_state_capacity')::bigint,
    (payload ->> 'global_dirty_writers')::bigint,
    (payload ->> 'active_clients')::bigint,
    (payload ->> 'peak_active_clients')::bigint,
    (payload ->> 'max_clients')::bigint,
    (payload ->> 'client_slots')::bigint,
    (payload ->> 'workers_configured')::bigint,
    (payload ->> 'workers_running')::bigint,
    (payload ->> 'shared_memory_bytes')::bigint,
    (payload ->> 'worker_memory_bytes')::bigint,
    (payload ->> 'estimated_memory_bytes')::bigint,
    (payload ->> 'memory_budget_bytes')::bigint,
    (payload ->> 'cache_hits_total')::bigint,
    (payload ->> 'cache_misses_total')::bigint,
    (payload ->> 'negative_hits_total')::bigint,
    (payload ->> 'database_reads_total')::bigint,
    (payload ->> 'database_writes_total')::bigint,
    (payload ->> 'invalidations_total')::bigint,
    (payload ->> 'evictions_total')::bigint,
    (payload ->> 'singleflight_leaders_total')::bigint,
    (payload ->> 'singleflight_waiters_total')::bigint,
    (payload ->> 'singleflight_reuses_total')::bigint,
    (payload ->> 'singleflight_timeouts_total')::bigint,
    (payload ->> 'rejected_connections_total')::bigint,
    (payload ->> 'client_limit_rejections_total')::bigint,
    (payload ->> 'authentication_failures_total')::bigint,
    (payload ->> 'protocol_errors_total')::bigint,
    (payload ->> 'output_backpressure_events_total')::bigint,
    (payload ->> 'slow_client_drops_total')::bigint,
    (payload ->> 'worker_starts_total')::bigint,
    (payload ->> 'dirty_key_limit_fallbacks_total')::bigint,
    (payload ->> 'mapping_reload_failures_total')::bigint,
    (payload ->> 'workers_with_incomplete_mappings')::bigint,
    (payload ->> 'mapping_reload_incomplete_retries_total')::bigint
FROM snapshot;
$function$;

DO $function$
DECLARE
    v_acl record;
BEGIN
    REVOKE ALL ON FUNCTION local_cache.metrics() FROM PUBLIC;

    FOR v_acl IN
        SELECT acl.grantee,
               acl.privilege_type,
               acl.is_grantable,
               pg_catalog.pg_get_userbyid(acl.grantee) AS grantee_name
        FROM pg_catalog.pg_proc AS p
        CROSS JOIN LATERAL pg_catalog.aclexplode(p.proacl) AS acl
        WHERE p.oid = 'local_cache._metrics_2_0_4()'::pg_catalog.regprocedure
          AND acl.grantee <> p.proowner
    LOOP
        EXECUTE pg_catalog.format(
            'GRANT %s ON FUNCTION local_cache.metrics() TO %s%s',
            v_acl.privilege_type,
            CASE WHEN v_acl.grantee = 0 THEN 'PUBLIC'
                 ELSE pg_catalog.quote_ident(v_acl.grantee_name)
            END,
            CASE WHEN v_acl.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END
        );
    END LOOP;
END;
$function$;

DROP FUNCTION local_cache._metrics_2_0_4();

CREATE OR REPLACE FUNCTION health()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL RESTRICTED
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH snapshot AS MATERIALIZED (
    SELECT local_cache._metrics_json() AS payload
)
SELECT pg_catalog.jsonb_build_object(
    'ready',
        (payload ->> 'estimated_memory_bytes')::bigint <=
            (payload ->> 'memory_budget_bytes')::bigint
        AND (payload ->> 'workers_running')::bigint =
            (payload ->> 'workers_configured')::bigint
        AND (payload ->> 'workers_with_incomplete_mappings')::bigint = 0
        AND (payload ->> 'active_clients')::bigint <=
            (payload ->> 'max_clients')::bigint,
    'resp_enabled', (payload ->> 'workers_configured')::bigint > 0,
    'cache_enabled', (payload ->> 'cache_enabled')::boolean,
    'workers_configured', (payload ->> 'workers_configured')::bigint,
    'workers_running', (payload ->> 'workers_running')::bigint,
    'workers_with_incomplete_mappings',
        (payload ->> 'workers_with_incomplete_mappings')::bigint,
    'mapping_reload_incomplete_retries_total',
        (payload ->> 'mapping_reload_incomplete_retries_total')::bigint,
    'active_clients', (payload ->> 'active_clients')::bigint,
    'max_clients', (payload ->> 'max_clients')::bigint,
    'estimated_memory_bytes', (payload ->> 'estimated_memory_bytes')::bigint,
    'memory_budget_bytes', (payload ->> 'memory_budget_bytes')::bigint
)
FROM snapshot;
$function$;
