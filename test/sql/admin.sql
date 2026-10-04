\set ECHO none
\set QUIET 1
\pset format unaligned
\pset tuples_only on
\pset fieldsep '|'

CREATE EXTENSION pg_local_cache;
SELECT 'create-extension=', EXISTS (
    SELECT 1 FROM pg_catalog.pg_extension WHERE extname = 'pg_local_cache'
);
SELECT 'enabled-guc-context=', (
    SELECT context = 'sighup' AND setting = 'on'
      FROM pg_catalog.pg_settings
     WHERE name = 'pg_local_cache.enabled'
);

WITH old_versions(version) AS (
    VALUES ('1.0.0'), ('1.1.0'), ('1.2.0'), ('1.2.1'), ('1.3.0'),
           ('2.0.0'), ('2.0.1'), ('2.0.2'), ('2.0.3'),
           ('2.0.4')
), paths AS (
    SELECT * FROM pg_catalog.pg_extension_update_paths('pg_local_cache')
)
SELECT 'update-paths=', (
    SELECT count(paths.source) = 10
       AND bool_and(paths.target = '3.0.0' AND paths.path IS NOT NULL)
      FROM old_versions
      LEFT JOIN paths ON paths.source = old_versions.version
                     AND paths.target = '3.0.0'
);

SET client_min_messages = warning;
CREATE ROLE regress_pglc_worker LOGIN NOINHERIT NOSUPERUSER
    NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE contrib_regression TO regress_pglc_worker;
GRANT USAGE ON SCHEMA local_cache TO regress_pglc_worker;
GRANT SELECT ON TABLE local_cache.mapping TO regress_pglc_worker;

SELECT 'worker-role-and-grants=', (
    SELECT r.rolcanlogin AND NOT r.rolsuper AND NOT r.rolinherit
       AND NOT r.rolcreatedb AND NOT r.rolcreaterole
       AND NOT r.rolreplication AND NOT r.rolbypassrls
       AND pg_catalog.has_database_privilege(r.oid, 'contrib_regression', 'CONNECT')
       AND pg_catalog.has_schema_privilege(r.oid, 'local_cache', 'USAGE')
       AND pg_catalog.has_table_privilege(r.oid, 'local_cache.mapping', 'SELECT')
      FROM pg_catalog.pg_roles AS r
     WHERE r.rolname = 'regress_pglc_worker'
);

CREATE FUNCTION pg_temp.attach_error_contains(p_relation regclass, p_fragment text)
RETURNS boolean
LANGUAGE plpgsql
AS $function$
BEGIN
    PERFORM local_cache.attach_table(p_relation, false, 'admin_rejected');
    RETURN false;
EXCEPTION WHEN OTHERS THEN
    RETURN pg_catalog.strpos(SQLERRM, p_fragment) > 0;
END
$function$;

DROP TABLE IF EXISTS public.pglc_admin_attach CASCADE;
DROP TABLE IF EXISTS public.pglc_admin_no_pk CASCADE;
DROP TABLE IF EXISTS public.pglc_admin_partitioned CASCADE;
DROP TABLE IF EXISTS public.pglc_admin_partition CASCADE;
DROP TABLE IF EXISTS public.pglc_admin_parent CASCADE;
DROP TABLE IF EXISTS public.pglc_admin_child CASCADE;
DROP TABLE IF EXISTS public.pglc_admin_rls CASCADE;
DROP TABLE IF EXISTS public.pglc_admin_unlogged CASCADE;
CREATE TABLE public.pglc_admin_attach (id bigint PRIMARY KEY, value text);
CREATE TABLE public.pglc_admin_no_pk (id bigint, value text);
CREATE TABLE public.pglc_admin_partitioned (id bigint PRIMARY KEY, value text)
    PARTITION BY RANGE (id);
CREATE TABLE public.pglc_admin_partition PARTITION OF public.pglc_admin_partitioned
    FOR VALUES FROM (0) TO (100);
CREATE TABLE public.pglc_admin_parent (id bigint PRIMARY KEY, value text);
CREATE TABLE public.pglc_admin_child (extra text)
    INHERITS (public.pglc_admin_parent);
CREATE TABLE public.pglc_admin_rls (id bigint PRIMARY KEY, value text);
ALTER TABLE public.pglc_admin_rls ENABLE ROW LEVEL SECURITY;
CREATE UNLOGGED TABLE public.pglc_admin_unlogged (id bigint PRIMARY KEY, value text);
CREATE TEMP TABLE pglc_admin_temp (id bigint PRIMARY KEY, value text);

SELECT 'reject-no-primary-key=', pg_temp.attach_error_contains(
    'public.pglc_admin_no_pk'::regclass, 'has no primary key'
);
SELECT 'reject-partitioned=', pg_temp.attach_error_contains(
    'public.pglc_admin_partitioned'::regclass, 'supports only permanent ordinary tables'
);
SELECT 'reject-inherited=', pg_temp.attach_error_contains(
    'public.pglc_admin_parent'::regclass, 'table inheritance is not supported'
);
SELECT 'reject-rls=', pg_temp.attach_error_contains(
    'public.pglc_admin_rls'::regclass, 'row-level security is not supported'
);
SELECT 'reject-unlogged=', pg_temp.attach_error_contains(
    'public.pglc_admin_unlogged'::regclass, 'supports only permanent ordinary tables'
);
SELECT 'reject-temporary=', pg_temp.attach_error_contains(
    'pg_temp.pglc_admin_temp'::regclass, 'supports only permanent ordinary tables'
);

WITH attached AS (
    SELECT local_cache.attach_table(
        'public.pglc_admin_attach'::regclass, true, 'admin_attached'
    ) AS payload
)
SELECT 'attach-shape=', (
    payload ?& ARRAY['relation', 'namespace', 'primary_key_columns', 'whole_row',
                     'writable', 'worker_role', 'templates']
    AND payload ->> 'namespace' = 'admin_attached'
)
FROM attached;
SELECT 'mapping-row=', (
    SELECT count(*) = 1 AND bool_and(key_columns = ARRAY['id']::name[] AND writable)
      FROM local_cache.mapping
     WHERE namespace = 'admin_attached'
       AND relation = 'public.pglc_admin_attach'::regclass
);
SELECT 'triggers-installed=', (
    SELECT count(*) = 3 AND bool_and(tgenabled = 'A')
      FROM pg_catalog.pg_trigger
     WHERE tgrelid = 'public.pglc_admin_attach'::regclass
       AND tgname IN ('pg_local_cache_statement_guard',
                      'pg_local_cache_row_invalidate',
                      'pg_local_cache_truncate_invalidate')
);
SELECT 'reconcile-table=', (
    local_cache.reconcile_table('public.pglc_admin_attach'::regclass)
        ->> 'namespace' = 'admin_attached'
);
SELECT 'reconcile-all=', (local_cache.reconcile_all() >= 1);
SELECT 'health-shape=', (
    jsonb_typeof(local_cache.health()) = 'object'
    AND local_cache.health() ?& ARRAY['ready', 'resp_enabled', 'cache_enabled',
                                      'workers_configured', 'workers_running',
                                      'active_clients', 'max_clients']
    AND jsonb_typeof(local_cache.health() -> 'ready') = 'boolean'
    AND jsonb_typeof(local_cache.health() -> 'cache_enabled') = 'boolean'
);
SELECT 'stats-shape=', (
    jsonb_typeof(local_cache.stats()) = 'object'
    AND local_cache.stats() ?& ARRAY['cache_hits', 'cache_misses', 'database_reads',
                                     'invalidations']
    AND NOT (local_cache.stats() ?| ARRAY[
        'sql_cache_hits', 'sql_cache_misses', 'sql_cache_fills', 'sql_cache_bypasses'
    ])
);
SELECT 'metrics-shape=', (
    (SELECT count(*) = 1 AND bool_and(up = 1 AND cache_capacity > 0
                                      AND workers_running = workers_configured)
       FROM local_cache.metrics())
    AND (SELECT pg_catalog.jsonb_typeof(pg_catalog.to_jsonb(m)) = 'object'
                AND pg_catalog.to_jsonb(m) ?& ARRAY['up', 'cache_capacity', 'workers_running']
                AND NOT (pg_catalog.to_jsonb(m) ?| ARRAY[
                    'sql_cache_hits_total', 'sql_cache_misses_total',
                    'sql_cache_fills_total', 'sql_cache_bypasses_total'
                ])
           FROM local_cache.metrics() AS m)
);
SELECT 'invalidate-namespace=', (local_cache.invalidate('admin_attached') >= 0);
ALTER TABLE public.pglc_admin_attach ADD COLUMN added text;
SELECT 'alter-keeps-mapping=', (
    local_cache.reconcile_table('public.pglc_admin_attach'::regclass) ->> 'namespace'
        = 'admin_attached'
    AND EXISTS (SELECT 1 FROM local_cache.mapping WHERE namespace = 'admin_attached')
);
-- Separate statements: subqueries in the detaching statement would still see
-- the pre-detach snapshot.
SELECT 'detach-returns-true=', local_cache.detach_table('public.pglc_admin_attach'::regclass);
SELECT 'detach-removes-trigger-and-mapping=', (
    NOT EXISTS (SELECT 1 FROM local_cache.mapping WHERE namespace = 'admin_attached')
    AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger
                     WHERE tgrelid = 'public.pglc_admin_attach'::regclass
                       AND tgname LIKE 'pg_local_cache_%')
);
CREATE TABLE public.pglc_admin_drop (id bigint PRIMARY KEY);
SELECT 'drop-fixture-attached=', (
    local_cache.attach_table(
        'public.pglc_admin_drop'::regclass, false, 'admin_drop'
    ) IS NOT NULL
);
DROP TABLE public.pglc_admin_drop;
SELECT 'drop-removes-mapping=', NOT EXISTS (
    SELECT 1 FROM local_cache.mapping WHERE namespace = 'admin_drop'
);

DROP TABLE public.pglc_admin_attach CASCADE;
DROP TABLE public.pglc_admin_no_pk CASCADE;
DROP TABLE public.pglc_admin_partitioned CASCADE;
DROP TABLE public.pglc_admin_parent CASCADE;
DROP TABLE public.pglc_admin_rls CASCADE;
DROP TABLE public.pglc_admin_unlogged CASCADE;
DROP OWNED BY regress_pglc_worker;
DROP ROLE regress_pglc_worker;
