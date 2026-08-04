\echo Use "CREATE EXTENSION pg_local_cache" to load this file. \quit

CREATE TABLE mapping (
    namespace text PRIMARY KEY
        CONSTRAINT mapping_namespace_shape CHECK (
            namespace ~ '^[A-Za-z0-9_.-]{1,63}$'
            AND namespace <> 'CRUD'
    ),
    relation regclass NOT NULL UNIQUE,
    key_columns name[] NOT NULL,
    writable boolean NOT NULL DEFAULT false,
    CONSTRAINT mapping_key_columns_shape CHECK (
        pg_catalog.array_ndims(key_columns) = 1
        AND pg_catalog.array_lower(key_columns, 1) = 1
        AND pg_catalog.cardinality(key_columns) BETWEEN 1 AND 16
        AND pg_catalog.array_position(key_columns, NULL::name) IS NULL
    )
);

REVOKE ALL ON TABLE mapping FROM PUBLIC;

SELECT pg_catalog.pg_extension_config_dump('local_cache.mapping', '');

CREATE FUNCTION _row_invalidate()
RETURNS trigger
AS 'MODULE_PATHNAME', 'pg_local_cache_row_invalidate'
LANGUAGE C;

CREATE FUNCTION _truncate_invalidate()
RETURNS trigger
AS 'MODULE_PATHNAME', 'pg_local_cache_truncate_invalidate'
LANGUAGE C;

CREATE FUNCTION _statement_guard()
RETURNS trigger
AS 'MODULE_PATHNAME', 'pg_local_cache_statement_guard'
LANGUAGE C;

CREATE FUNCTION _lock_relation(relation_oid oid)
RETURNS boolean
AS 'MODULE_PATHNAME', 'pg_local_cache_lock_relation'
LANGUAGE C STRICT VOLATILE PARALLEL UNSAFE;

CREATE FUNCTION _reload()
RETURNS void
AS 'MODULE_PATHNAME', 'pg_local_cache_reload'
LANGUAGE C;

CREATE FUNCTION _forget(namespace text, relation oid)
RETURNS void
AS 'MODULE_PATHNAME', 'pg_local_cache_forget'
LANGUAGE C STRICT;

CREATE FUNCTION _mapping_changed()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM local_cache._reload();
    RETURN NULL;
END;
$function$;

CREATE TRIGGER pg_local_cache_mapping_reload
    AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON mapping
    FOR EACH STATEMENT
    EXECUTE FUNCTION _mapping_changed();

CREATE FUNCTION _ddl_invalidate()
RETURNS event_trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    /* Collected GRANT/REVOKE commands lack dependable object addresses.
     * ACL drift can invalidate a worker mapping. */
    IF TG_TAG IN ('GRANT', 'REVOKE') THEN
        PERFORM local_cache._reload();
        RETURN;
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_event_trigger_ddl_commands() AS d
          JOIN local_cache.mapping AS m
            ON (
                d.objid = m.relation::oid
                OR (
                    d.classid =
                        'pg_catalog.pg_namespace'::pg_catalog.regclass
                    AND EXISTS (
                        SELECT 1
                          FROM pg_catalog.pg_class AS schema_relation
                         WHERE schema_relation.oid = m.relation
                           AND schema_relation.relnamespace = d.objid
                    )
                )
                OR (
                    d.classid = 'pg_catalog.pg_class'::regclass
                    AND EXISTS (
                        SELECT 1
                          FROM pg_catalog.pg_inherits AS inh
                         WHERE (
                                   inh.inhrelid = d.objid
                               AND inh.inhparent = m.relation
                               )
                            OR (
                                   inh.inhparent = d.objid
                               AND inh.inhrelid = m.relation
                               )
                    )
                )
                OR (
                    d.classid = 'pg_catalog.pg_class'::regclass
                    AND EXISTS (
                        SELECT 1
                          FROM pg_catalog.pg_index AS i
                         WHERE i.indexrelid = d.objid
                           AND i.indrelid = m.relation
                    )
                )
                OR (
                    d.classid = 'pg_catalog.pg_trigger'::regclass
                    AND EXISTS (
                        SELECT 1
                          FROM pg_catalog.pg_trigger AS t
                         WHERE t.oid = d.objid
                           AND t.tgrelid = m.relation
                    )
                )
                OR (
                    d.classid = 'pg_catalog.pg_constraint'::regclass
                    AND EXISTS (
                        SELECT 1
                          FROM pg_catalog.pg_constraint AS c
                         WHERE c.oid = d.objid
                           AND (
                               c.conrelid = m.relation
                               OR c.confrelid = m.relation
                           )
                    )
                )
                OR (
                    d.classid = 'pg_catalog.pg_rewrite'::regclass
                    AND EXISTS (
                        SELECT 1
                          FROM pg_catalog.pg_rewrite AS r
                         WHERE r.oid = d.objid
                           AND r.ev_class = m.relation
                    )
                )
            )
    ) OR EXISTS (
        /* Cached row JSON depends on type/output-function semantics even when
         * the mapped table's tuple descriptor itself is unchanged. */
        SELECT 1
          FROM pg_catalog.pg_event_trigger_ddl_commands() AS d
         WHERE d.classid IN (
             'pg_catalog.pg_type'::regclass,
             'pg_catalog.pg_proc'::regclass,
             'pg_catalog.pg_cast'::regclass,
             'pg_catalog.pg_collation'::regclass,
             /* ALTER EXTENSION ... ADD/DROP changes table provenance via
              * pg_depend without necessarily reporting the table itself. */
             'pg_catalog.pg_extension'::regclass
         )
    ) THEN
        PERFORM local_cache._reload();
    END IF;
END;
$function$;

CREATE FUNCTION _sql_drop_invalidate()
RETURNS event_trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_dropped_relations oid[] := ARRAY[]::oid[];
    v_removed record;
    v_removed_mapping boolean := false;
    v_reload_required boolean := false;
BEGIN
    /* DROP EXTENSION/DROP SCHEMA may remove the catalog before this trigger
     * itself disappears.  In that case there is no mapping left to clean. */
    IF pg_catalog.to_regclass('local_cache.mapping') IS NULL THEN
        RETURN;
    END IF;

    SELECT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_event_trigger_dropped_objects() AS d
          JOIN local_cache.mapping AS m
            ON (
                d.objid = m.relation::oid
                OR (
                    d.object_type IN (
                        'table column',
                        'table constraint',
                        'trigger',
                        'rule'
                    )
                    AND d.address_names[1] = (
                        SELECT n.nspname
                          FROM pg_catalog.pg_class AS c
                          JOIN pg_catalog.pg_namespace AS n
                            ON n.oid = c.relnamespace
                         WHERE c.oid = m.relation
                    )
                    AND d.address_names[2] = (
                        SELECT c.relname
                          FROM pg_catalog.pg_class AS c
                         WHERE c.oid = m.relation
                    )
                )
                /*
                 * Once an index has been dropped, its former owning table
                 * cannot be resolved from the catalogs.  Permanent index drops
                 * are rare and are conservatively treated as mapping changes.
                 * Temporary objects are excluded below.
                 */
                OR (
                    d.classid = 'pg_catalog.pg_class'::regclass
                    AND d.object_type = 'index'
                    AND d.original
                )
            )
         WHERE NOT d.is_temporary
    ) INTO v_reload_required;

    SELECT COALESCE(
               pg_catalog.array_agg(DISTINCT d.objid ORDER BY d.objid),
               ARRAY[]::oid[]
           )
      INTO v_dropped_relations
      FROM pg_catalog.pg_event_trigger_dropped_objects() AS d
      JOIN local_cache.mapping AS m
        ON m.relation::oid = d.objid
     WHERE d.classid = 'pg_catalog.pg_class'::pg_catalog.regclass
       AND d.objsubid = 0
       AND d.object_type = 'table'
       AND NOT d.is_temporary;

    IF pg_catalog.cardinality(v_dropped_relations) > 0 THEN
        FOR v_removed IN
            DELETE FROM local_cache.mapping AS m
             WHERE m.relation::oid = ANY (v_dropped_relations)
             RETURNING m.namespace, m.relation::oid AS relation_oid
        LOOP
            v_removed_mapping := true;
            PERFORM local_cache._forget(
                v_removed.namespace, v_removed.relation_oid
            );
        END LOOP;
    END IF;

    IF v_removed_mapping OR v_reload_required THEN
        PERFORM local_cache._reload();
    END IF;
END;
$function$;

CREATE FUNCTION invalidate(namespace text)
RETURNS bigint
AS 'MODULE_PATHNAME', 'pg_local_cache_invalidate'
LANGUAGE C STRICT;

CREATE FUNCTION get(relation regclass, key_values text[])
RETURNS text
AS 'MODULE_PATHNAME', 'pg_local_cache_sql_get'
LANGUAGE C STRICT VOLATILE PARALLEL UNSAFE
SECURITY INVOKER;

CREATE FUNCTION get(relation regclass, key_value anyelement)
RETURNS text
AS 'MODULE_PATHNAME', 'pg_local_cache_sql_get_scalar'
LANGUAGE C STRICT VOLATILE PARALLEL UNSAFE
SECURITY INVOKER;

CREATE FUNCTION mget(relation regclass, key_values anyarray)
RETURNS text[]
AS 'MODULE_PATHNAME', 'pg_local_cache_sql_mget'
LANGUAGE C STRICT VOLATILE PARALLEL UNSAFE
SECURITY INVOKER;

CREATE FUNCTION stats()
RETURNS jsonb
AS 'MODULE_PATHNAME', 'pg_local_cache_stats'
LANGUAGE C STABLE;

CREATE FUNCTION _metrics_json()
RETURNS jsonb
AS 'MODULE_PATHNAME', 'pg_local_cache_metrics_json'
LANGUAGE C STABLE PARALLEL RESTRICTED;

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
    sql_cache_hits_total bigint,
    sql_cache_misses_total bigint,
    sql_cache_fills_total bigint,
    sql_cache_bypasses_total bigint,
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
    (payload ->> 'sql_cache_hits_total')::bigint,
    (payload ->> 'sql_cache_misses_total')::bigint,
    (payload ->> 'sql_cache_fills_total')::bigint,
    (payload ->> 'sql_cache_bypasses_total')::bigint,
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

CREATE FUNCTION health()
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

REVOKE ALL ON FUNCTION _reload() FROM PUBLIC;
REVOKE ALL ON FUNCTION _lock_relation(oid) FROM PUBLIC;
REVOKE ALL ON FUNCTION _mapping_changed() FROM PUBLIC;
REVOKE ALL ON FUNCTION _forget(text, oid) FROM PUBLIC;
REVOKE ALL ON FUNCTION invalidate(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION stats() FROM PUBLIC;
REVOKE ALL ON FUNCTION _metrics_json() FROM PUBLIC;
REVOKE ALL ON FUNCTION metrics() FROM PUBLIC;
REVOKE ALL ON FUNCTION health() FROM PUBLIC;

CREATE FUNCTION _validate_attach_relation(p_relation regclass)
RETURNS void
LANGUAGE plpgsql
STABLE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_relkind "char";
    v_relpersistence "char";
    v_relispartition boolean;
    v_relrowsecurity boolean;
    v_relforcerowsecurity boolean;
BEGIN
    SELECT c.relkind, c.relpersistence, c.relispartition,
           c.relrowsecurity, c.relforcerowsecurity
      INTO v_relkind, v_relpersistence, v_relispartition,
           v_relrowsecurity, v_relforcerowsecurity
      FROM pg_catalog.pg_class AS c
     WHERE c.oid = p_relation;
    IF NOT FOUND OR v_relkind <> 'r' OR v_relpersistence <> 'p' THEN
        RAISE EXCEPTION
            'pg_local_cache supports only permanent ordinary tables: %',
            p_relation;
    END IF;
    IF v_relispartition OR EXISTS (
        SELECT 1
          FROM pg_catalog.pg_inherits AS inh
         WHERE inh.inhparent = p_relation
            OR inh.inhrelid = p_relation
    ) THEN
        RAISE EXCEPTION 'table inheritance is not supported by pg_local_cache'
            USING HINT =
                'Attach a standalone table with no inheritance parent or children.';
    END IF;
    IF v_relrowsecurity OR v_relforcerowsecurity THEN
        RAISE EXCEPTION 'row-level security is not supported by pg_local_cache'
            USING HINT =
                'Use a dedicated table or a security-barrier API instead.';
    END IF;
END;
$function$;

CREATE FUNCTION _primary_key_columns(p_relation regclass)
RETURNS name[]
LANGUAGE sql
STABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT ARRAY(
           SELECT a.attname
             FROM pg_catalog.unnest(i.indkey::smallint[])
                  WITH ORDINALITY AS key(attnum, key_position)
             JOIN pg_catalog.pg_attribute AS a
               ON a.attrelid = i.indrelid
              AND a.attnum = key.attnum
              AND a.attnum > 0
              AND NOT a.attisdropped
            WHERE key.key_position <= i.indnkeyatts
            ORDER BY key.key_position
       )::name[]
  FROM pg_catalog.pg_index AS i
 WHERE i.indrelid = p_relation
   AND i.indisprimary;
$function$;

CREATE FUNCTION _default_namespace(p_relation regclass)
RETURNS text
LANGUAGE sql
STABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT CASE
       WHEN (n.nspname || '.' || c.relname) ~ '^[A-Za-z0-9_.-]{1,63}$'
       THEN n.nspname || '.' || c.relname
       ELSE 'rel_' || c.oid::text
       END
  FROM pg_catalog.pg_class AS c
  JOIN pg_catalog.pg_namespace AS n
    ON n.oid = c.relnamespace
 WHERE c.oid = p_relation;
$function$;

CREATE FUNCTION _mapping_result(
    p_namespace text,
    p_relation regclass,
    p_key_columns name[],
    p_writable boolean
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_schema_name name;
    v_relation_name name;
    v_qualified_relation text;
    v_wire_relation text;
    v_key_object text;
    v_key_template text;
BEGIN
    SELECT n.nspname, c.relname
      INTO STRICT v_schema_name, v_relation_name
      FROM pg_catalog.pg_class AS c
      JOIN pg_catalog.pg_namespace AS n
        ON n.oid = c.relnamespace
     WHERE c.oid = p_relation;

    SELECT '{' || pg_catalog.string_agg(
               pg_catalog.to_json(key_column::text)::text || ':' ||
               pg_catalog.to_json('<' || key_column::text || '>')::text,
               ',' ORDER BY key_position
           ) || '}'
      INTO v_key_object
      FROM pg_catalog.unnest(p_key_columns)
           WITH ORDINALITY AS key(key_column, key_position);

    v_qualified_relation := pg_catalog.format(
        '%I.%I', v_schema_name, v_relation_name
    );
    /* KVik wire names are literal components, not SQL identifiers. */
    v_wire_relation := pg_catalog.current_database() || '.' ||
        v_schema_name || '.' || v_relation_name;
    v_key_template := 'CRUD:' || v_wire_relation || ':' || v_key_object;

    RETURN pg_catalog.jsonb_build_object(
        'relation', v_qualified_relation,
        'namespace', p_namespace,
        'primary_key_columns', pg_catalog.to_jsonb(p_key_columns),
        'whole_row', true,
        'writable', p_writable,
        'worker_role', pg_catalog.current_setting('pg_local_cache.role', true),
        'templates', pg_catalog.jsonb_build_object(
            'key', v_key_template,
            'get', 'GET ' || v_key_template,
            'set', CASE WHEN p_writable THEN
                'SET ' || v_key_template || ' <row-json>'
                ELSE NULL
            END,
            'del', CASE WHEN p_writable THEN 'DEL ' || v_key_template ELSE NULL END,
            'invalidate', 'INVALIDATE CRUD:' || v_wire_relation,
            'invalidate_key', 'INVALIDATE ' || v_key_template,
            'invalidate_database', 'INVALIDATE CRUD:' ||
                pg_catalog.current_database(),
            'invalidate_all', 'INVALIDATE CRUD'
        )
    );
END;
$function$;

CREATE FUNCTION _prepare_trigger_slots(
    p_relation oid,
    p_namespace text,
    p_key_columns name[]
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_conflicting_trigger name;
    v_owned_trigger record;
    v_encoding text := pg_catalog.current_setting('server_encoding');
    v_expected_row_args bytea;
    v_expected_truncate_args bytea;
    v_key_column name;
    v_zero bytea := pg_catalog.decode('00', 'hex');
BEGIN
    v_expected_row_args :=
        pg_catalog.convert_to(p_namespace, v_encoding) || v_zero;
    FOREACH v_key_column IN ARRAY p_key_columns LOOP
        v_expected_row_args := v_expected_row_args ||
            pg_catalog.convert_to(v_key_column::text, v_encoding) || v_zero;
    END LOOP;
    v_expected_truncate_args :=
        pg_catalog.convert_to(p_namespace, v_encoding) || v_zero;

    SELECT t.tgname
      INTO v_conflicting_trigger
      FROM pg_catalog.pg_trigger AS t
     WHERE t.tgrelid = p_relation
       AND t.tgname IN (
           'pg_local_cache_statement_guard',
           'pg_local_cache_row_invalidate',
           'pg_local_cache_truncate_invalidate'
       )
       AND NOT (
           t.tgfoid IN (
               'local_cache._statement_guard()'::pg_catalog.regprocedure,
               'local_cache._row_invalidate()'::pg_catalog.regprocedure,
               'local_cache._truncate_invalidate()'::pg_catalog.regprocedure
           )
           AND EXISTS (
               SELECT 1
                 FROM pg_catalog.pg_depend AS dep
                 JOIN pg_catalog.pg_extension AS ext
                   ON ext.oid = dep.refobjid
                  AND ext.extname = 'pg_local_cache'
                WHERE dep.classid =
                      'pg_catalog.pg_trigger'::pg_catalog.regclass
                  AND dep.objid = t.oid
                  AND dep.objsubid = 0
                  AND dep.refclassid =
                      'pg_catalog.pg_extension'::pg_catalog.regclass
                  AND dep.refobjsubid = 0
                  AND dep.deptype = 'x'
           )
       )
     ORDER BY t.tgname
     LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION
            'reserved pg_local_cache trigger name % is not owned by this mapping on relation OID %',
            v_conflicting_trigger, p_relation
            USING ERRCODE = '55000',
                  HINT =
                      'Restore or rename the conflicting trigger before retrying.';
    END IF;

    /* A trigger carrying our extension dependency is ours to repair.  Drop
     * only damaged, renamed, or duplicate owned triggers; exact trigger slots
     * keep their OIDs and are merely re-enabled by _register_mapping(). */
    FOR v_owned_trigger IN
        SELECT t.oid, t.tgname
          FROM pg_catalog.pg_trigger AS t
         WHERE t.tgrelid = p_relation
           AND t.tgfoid IN (
               'local_cache._statement_guard()'::pg_catalog.regprocedure,
               'local_cache._row_invalidate()'::pg_catalog.regprocedure,
               'local_cache._truncate_invalidate()'::pg_catalog.regprocedure
           )
           AND EXISTS (
               SELECT 1
                 FROM pg_catalog.pg_depend AS dep
                 JOIN pg_catalog.pg_extension AS ext
                   ON ext.oid = dep.refobjid
                  AND ext.extname = 'pg_local_cache'
                WHERE dep.classid =
                      'pg_catalog.pg_trigger'::pg_catalog.regclass
                  AND dep.objid = t.oid
                  AND dep.objsubid = 0
                  AND dep.refclassid =
                      'pg_catalog.pg_extension'::pg_catalog.regclass
                  AND dep.refobjsubid = 0
                  AND dep.deptype = 'x'
           )
           AND NOT COALESCE((
               NOT t.tgisinternal
               AND t.tgconstraint = 0
               AND t.tgconstrrelid = 0
               AND t.tgparentid = 0
               AND NOT t.tgdeferrable
               AND NOT t.tginitdeferred
               AND t.tgqual IS NULL
               AND t.tgoldtable IS NULL
               AND t.tgnewtable IS NULL
               AND t.tgattr = ''::pg_catalog.int2vector
               AND t.tgfoid = CASE t.tgname
                   WHEN 'pg_local_cache_statement_guard' THEN
                       'local_cache._statement_guard()'::pg_catalog.regprocedure
                   WHEN 'pg_local_cache_row_invalidate' THEN
                       'local_cache._row_invalidate()'::pg_catalog.regprocedure
                   WHEN 'pg_local_cache_truncate_invalidate' THEN
                       'local_cache._truncate_invalidate()'::pg_catalog.regprocedure
               END
               AND t.tgtype = CASE t.tgname
                   WHEN 'pg_local_cache_statement_guard' THEN 62
                   WHEN 'pg_local_cache_row_invalidate' THEN 29
                   WHEN 'pg_local_cache_truncate_invalidate' THEN 32
               END
               AND t.tgnargs = CASE t.tgname
                   WHEN 'pg_local_cache_statement_guard' THEN 0
                   WHEN 'pg_local_cache_row_invalidate' THEN
                       1 + pg_catalog.cardinality(p_key_columns)
                   WHEN 'pg_local_cache_truncate_invalidate' THEN 1
               END
               AND t.tgargs = CASE t.tgname
                   WHEN 'pg_local_cache_statement_guard' THEN
                       pg_catalog.decode('', 'hex')
                   WHEN 'pg_local_cache_row_invalidate' THEN
                       v_expected_row_args
                   WHEN 'pg_local_cache_truncate_invalidate' THEN
                       v_expected_truncate_args
               END
           ), false)
         ORDER BY t.oid
    LOOP
        EXECUTE pg_catalog.format(
            'DROP TRIGGER %I ON %s',
            v_owned_trigger.tgname,
            p_relation::pg_catalog.regclass
        );
    END LOOP;
END;
$function$;

CREATE FUNCTION _register_mapping(
    p_namespace text,
    p_relation regclass,
    p_key_columns name[],
    p_writable boolean
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_schema_name name;
    v_schema_oid oid;
    v_relation_name name;
    v_relkind "char";
    v_relpersistence "char";
    v_relispartition boolean;
    v_relrowsecurity boolean;
    v_relforcerowsecurity boolean;
    v_primary_key_count integer;
    v_primary_key_columns name[];
    v_primary_key_valid boolean;
    v_has_primary_key boolean;
    v_key_attribute_count integer;
    v_key_column name;
    v_key_position integer;
    v_key_type oid;
    v_key_collation oid;
    v_key_not_null boolean;
    v_worker_role text;
    v_worker_role_oid oid;
    v_configured_database text;
    v_worker_is_superuser boolean;
    v_worker_is_dedicated boolean;
    v_existing_namespace text;
    v_relation_is_attached boolean := false;
    v_old_relation oid;
    v_trigger_arguments text;
    v_ready_trigger_count integer;
BEGIN
    IF p_namespace IS NULL OR p_namespace = 'CRUD' OR
       p_namespace !~ '^[A-Za-z0-9_.-]{1,63}$' THEN
        RAISE EXCEPTION 'invalid pg_local_cache namespace: %', p_namespace
            USING HINT =
                'Use 1-63 ASCII letters, digits, dot, dash, or underscore.';
    END IF;
    IF p_relation IS NULL THEN
        RAISE EXCEPTION 'pg_local_cache relation must not be NULL';
    END IF;
    IF p_writable IS NULL THEN
        RAISE EXCEPTION 'pg_local_cache writable flag must not be NULL';
    END IF;
    IF NOT local_cache._lock_relation(p_relation::oid) THEN
        RAISE EXCEPTION 'pg_local_cache relation no longer exists: %',
            p_relation::oid
            USING ERRCODE = '42P01';
    END IF;

    v_configured_database := pg_catalog.current_setting(
        'pg_local_cache.database', true
    );
    IF v_configured_database IS NULL OR
       v_configured_database <> pg_catalog.current_database() THEN
        RAISE EXCEPTION
            'database % is not served by pg_local_cache workers (configured: %)',
            pg_catalog.current_database(), v_configured_database
            USING HINT =
                'Set pg_local_cache.database at postmaster start or attach the table in the configured database.';
    END IF;
    IF p_key_columns IS NULL OR
       COALESCE(pg_catalog.array_ndims(p_key_columns), 0) <> 1 OR
       COALESCE(pg_catalog.array_lower(p_key_columns, 1), 0) <> 1 OR
       pg_catalog.cardinality(p_key_columns) NOT BETWEEN 1 AND 16 OR
       pg_catalog.array_position(p_key_columns, NULL::name) IS NOT NULL THEN
        RAISE EXCEPTION 'pg_local_cache key_columns must contain 1 to 16 names'
            USING HINT =
                'Pass a one-dimensional, one-based array with no NULL entries.';
    END IF;

    SELECT n.nspname, n.oid, c.relname, c.relkind, c.relpersistence,
           c.relispartition, c.relrowsecurity, c.relforcerowsecurity
      INTO v_schema_name, v_schema_oid, v_relation_name,
           v_relkind, v_relpersistence,
           v_relispartition, v_relrowsecurity, v_relforcerowsecurity
      FROM pg_catalog.pg_class AS c
      JOIN pg_catalog.pg_namespace AS n
        ON n.oid = c.relnamespace
     WHERE c.oid = p_relation;

    IF NOT FOUND OR v_relkind <> 'r' OR v_relpersistence <> 'p' THEN
        RAISE EXCEPTION
            'pg_local_cache supports only permanent ordinary tables: %',
            p_relation;
    END IF;
    IF v_schema_name::text ~ '^pg_' OR
       v_schema_name = 'information_schema' OR EXISTS (
           SELECT 1
             FROM pg_catalog.pg_extension AS ext
            WHERE ext.extname = 'pg_local_cache'
              AND ext.extnamespace = v_schema_oid
       ) OR EXISTS (
           SELECT 1
             FROM pg_catalog.pg_depend AS dep
            WHERE dep.classid =
                  'pg_catalog.pg_class'::pg_catalog.regclass
              AND dep.objid = p_relation
              AND dep.objsubid = 0
              AND dep.refclassid =
                  'pg_catalog.pg_extension'::pg_catalog.regclass
              AND dep.refobjsubid = 0
              AND dep.deptype = 'e'
       ) THEN
        RAISE EXCEPTION
            'pg_local_cache cannot attach extension or system table %',
            p_relation
            USING HINT =
                'Attach a permanent application table in a dedicated application schema.';
    END IF;
    IF pg_catalog.current_database() ~ '[.:]' OR
       v_schema_name::text ~ '[.:]' OR v_relation_name::text ~ '[.:]' THEN
        RAISE EXCEPTION
            'KVik wire names cannot contain dot or colon: %.%',
            v_schema_name, v_relation_name
            USING HINT =
                'Rename the database, schema, or table before attaching it.';
    END IF;
    IF v_relispartition OR EXISTS (
        SELECT 1
          FROM pg_catalog.pg_inherits AS inh
         WHERE inh.inhparent = p_relation
            OR inh.inhrelid = p_relation
    ) THEN
        RAISE EXCEPTION 'table inheritance is not supported by pg_local_cache'
            USING HINT =
                'Attach a standalone table with no inheritance parent or children.';
    END IF;
    IF v_relrowsecurity OR v_relforcerowsecurity THEN
        RAISE EXCEPTION 'row-level security is not supported by pg_local_cache'
            USING HINT =
                'Use a dedicated table or a security-barrier API instead.';
    END IF;

    SELECT i.indnkeyatts,
           ARRAY(
               SELECT a.attname
                 FROM pg_catalog.unnest(i.indkey::smallint[])
                      WITH ORDINALITY AS key(attnum, key_position)
                 JOIN pg_catalog.pg_attribute AS a
                   ON a.attrelid = i.indrelid
                  AND a.attnum = key.attnum
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                WHERE key.key_position <= i.indnkeyatts
                ORDER BY key.key_position
           )::name[],
           i.indisunique
           AND i.indimmediate
           AND i.indisvalid
           AND i.indisready
           AND i.indpred IS NULL
           AND i.indexprs IS NULL
           AND am.amname = 'btree'
      INTO v_primary_key_count, v_primary_key_columns, v_primary_key_valid
      FROM pg_catalog.pg_index AS i
      JOIN pg_catalog.pg_class AS ic
        ON ic.oid = i.indexrelid
      JOIN pg_catalog.pg_am AS am
        ON am.oid = ic.relam
     WHERE i.indrelid = p_relation
       AND i.indisprimary;

    v_has_primary_key := FOUND;
    IF NOT v_has_primary_key THEN
        RAISE EXCEPTION 'table % has no primary key', p_relation
            USING HINT =
                'Add a PRIMARY KEY with at most 16 supported columns before attaching the table.';
    END IF;
    IF v_primary_key_count NOT BETWEEN 1 AND 16 THEN
        RAISE EXCEPTION 'table % primary key has % columns; maximum is 16',
            p_relation, v_primary_key_count;
    END IF;
    IF NOT v_primary_key_valid OR
       pg_catalog.cardinality(v_primary_key_columns) <>
           v_primary_key_count THEN
        RAISE EXCEPTION 'table % primary key is not cache-safe', p_relation
            USING HINT =
                'Use a valid, ready, immediate, non-partial btree PRIMARY KEY over table columns.';
    END IF;
    IF p_key_columns <> v_primary_key_columns THEN
        RAISE EXCEPTION
            'key_columns % do not exactly match primary key columns % on %',
            p_key_columns, v_primary_key_columns, p_relation
            USING HINT =
                'Use every PRIMARY KEY column exactly once and in primary-key order.';
    END IF;

    SELECT pg_catalog.count(*)
      INTO v_key_attribute_count
      FROM pg_catalog.unnest(p_key_columns) AS key(key_column)
      JOIN pg_catalog.pg_attribute AS a
        ON a.attrelid = p_relation
       AND a.attname = key.key_column
       AND a.attnum > 0
       AND NOT a.attisdropped;
    IF v_key_attribute_count <> pg_catalog.cardinality(p_key_columns) THEN
        RAISE EXCEPTION 'one or more key columns % do not exist on %',
            p_key_columns, p_relation;
    END IF;

    FOR v_key_column, v_key_position, v_key_type,
        v_key_collation, v_key_not_null IN
        SELECT key.key_column, key.key_position,
               a.atttypid, a.attcollation, a.attnotnull
          FROM pg_catalog.unnest(p_key_columns)
               WITH ORDINALITY AS key(key_column, key_position)
          JOIN pg_catalog.pg_attribute AS a
            ON a.attrelid = p_relation
           AND a.attname = key.key_column
           AND a.attnum > 0
           AND NOT a.attisdropped
         ORDER BY key.key_position
    LOOP
        IF NOT v_key_not_null THEN
            RAISE EXCEPTION 'primary-key column %.% must be NOT NULL',
                p_relation, v_key_column;
        END IF;
        IF v_key_type NOT IN (
            'int2'::regtype, 'int4'::regtype, 'int8'::regtype,
            'text'::regtype, 'varchar'::regtype, 'bpchar'::regtype,
            'uuid'::regtype
        ) THEN
            RAISE EXCEPTION 'unsupported key type % for column %.%',
                v_key_type::regtype, p_relation, v_key_column
                USING HINT =
                    'Supported key types: int2, int4, int8, text, varchar, bpchar, uuid.';
        END IF;
        IF NOT EXISTS (
            SELECT 1
              FROM pg_catalog.pg_index AS i
              JOIN pg_catalog.pg_class AS ic
                ON ic.oid = i.indexrelid
              JOIN pg_catalog.pg_am AS am
                ON am.oid = ic.relam
               AND am.amname = 'btree'
              JOIN pg_catalog.pg_opclass AS opc
                ON opc.oid = i.indclass[v_key_position - 1]
               AND opc.opcmethod = am.oid
               AND opc.opcdefault
               AND (
                   opc.opcintype = v_key_type
                   OR EXISTS (
                       SELECT 1
                         FROM pg_catalog.pg_cast AS pc
                        WHERE pc.castsource = v_key_type
                          AND pc.casttarget = opc.opcintype
                          AND pc.castmethod = 'b'
                   )
               )
             WHERE i.indrelid = p_relation
               AND i.indisunique
               AND i.indimmediate
               AND i.indisvalid
               AND i.indisready
               AND i.indpred IS NULL
               AND i.indexprs IS NULL
               AND i.indnkeyatts = pg_catalog.cardinality(p_key_columns)
               AND i.indisprimary
               AND i.indkey[v_key_position - 1] = (
                   SELECT a.attnum
                     FROM pg_catalog.pg_attribute AS a
                    WHERE a.attrelid = p_relation
                      AND a.attname = v_key_column
                      AND a.attnum > 0
                      AND NOT a.attisdropped
               )
        ) THEN
            RAISE EXCEPTION
                'primary-key column %.% must use its default btree operator class',
                p_relation, v_key_column
                USING HINT =
                    'The cache uses PostgreSQL default equality semantics for key lookups.';
        END IF;
        IF v_key_collation <> 0 AND EXISTS (
            SELECT 1
              FROM pg_catalog.pg_collation AS coll
             WHERE coll.oid = v_key_collation
               AND NOT coll.collisdeterministic
        ) THEN
            RAISE EXCEPTION
                'nondeterministic key collation is not supported for %.%',
                p_relation, v_key_column
                USING HINT =
                    'Use deterministic collations so SQL equality and cache invalidation agree.';
        END IF;
    END LOOP;

    IF p_writable AND EXISTS (
        SELECT 1
          FROM pg_catalog.pg_attribute AS a
         WHERE a.attrelid = p_relation
           AND a.attnum > 0
           AND NOT a.attisdropped
           AND a.attname = ANY (p_key_columns)
           AND a.attgenerated <> ''
    ) THEN
        RAISE EXCEPTION
            'writable whole-row mappings do not support generated primary keys'
            USING HINT =
                'Use a read-only mapping or a non-generated primary key; identity columns are supported.';
    END IF;
    v_worker_role := pg_catalog.current_setting('pg_local_cache.role', true);
    IF v_worker_role IS NULL OR v_worker_role = '' THEN
        RAISE EXCEPTION 'pg_local_cache.role is not configured'
            USING HINT =
                'Configure a dedicated non-superuser worker role and restart PostgreSQL.';
    END IF;
    SELECT r.oid, r.rolsuper,
           r.rolcanlogin
           AND NOT r.rolsuper
           AND NOT r.rolinherit
           AND NOT r.rolcreatedb
           AND NOT r.rolcreaterole
           AND NOT r.rolreplication
           AND NOT r.rolbypassrls
           AND pg_catalog.has_database_privilege(
               r.oid, pg_catalog.current_database(), 'CONNECT'
           )
           AND pg_catalog.has_schema_privilege(
               r.oid, 'local_cache', 'USAGE'
           )
           AND pg_catalog.has_table_privilege(
               r.oid, 'local_cache.mapping', 'SELECT'
           )
      INTO v_worker_role_oid, v_worker_is_superuser, v_worker_is_dedicated
      FROM pg_catalog.pg_roles AS r
     WHERE r.rolname = v_worker_role;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'configured pg_local_cache worker role % does not exist',
            v_worker_role;
    END IF;
    IF v_worker_is_superuser THEN
        RAISE EXCEPTION
            'configured pg_local_cache worker role % must not be a superuser',
            v_worker_role;
    END IF;
    IF v_worker_is_dedicated IS DISTINCT FROM true THEN
        RAISE EXCEPTION
            'configured pg_local_cache worker role % is not a dedicated least-privilege role',
            v_worker_role
            USING HINT =
                'Require LOGIN NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS plus CONNECT and read-only local_cache metadata access.';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.pg_class AS c
         WHERE c.oid = p_relation
           AND c.relowner = v_worker_role_oid
    ) THEN
        RAISE EXCEPTION
            'configured pg_local_cache worker role % must not own mapped table %',
            v_worker_role, p_relation
            USING HINT =
                'Use a separate owner/deploy role and grant only the privileges managed by attach_table().';
    END IF;

    LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE;

    SELECT m.namespace
      INTO v_existing_namespace
      FROM local_cache.mapping AS m
     WHERE m.relation = p_relation;
    v_relation_is_attached := FOUND;
    IF v_relation_is_attached AND v_existing_namespace <> p_namespace THEN
        RAISE EXCEPTION 'table % is already attached as namespace %',
            p_relation, v_existing_namespace
            USING HINT =
                'Call detach_table() before changing the namespace.';
    END IF;

    SELECT relation::oid
      INTO v_old_relation
      FROM local_cache.mapping
     WHERE namespace = p_namespace;
    IF FOUND AND v_old_relation <> p_relation::oid THEN
        RAISE EXCEPTION
            'namespace % is already attached to table %',
            p_namespace, v_old_relation::regclass
            USING HINT =
                'Call detach_table() for the existing table first, or use docker/attach-table.sh --replace.';
    END IF;

    PERFORM local_cache._prepare_trigger_slots(
        p_relation::oid, p_namespace, p_key_columns
    );

    IF NOT EXISTS (
        SELECT 1 FROM local_cache.mapping WHERE namespace = p_namespace
    ) AND (SELECT pg_catalog.count(*) FROM local_cache.mapping) >= 128 THEN
        RAISE EXCEPTION 'pg_local_cache supports at most 128 mappings';
    END IF;

    IF NOT pg_catalog.has_schema_privilege(
        v_worker_role_oid, v_schema_oid, 'USAGE'
    ) THEN
        EXECUTE pg_catalog.format(
            'GRANT USAGE ON SCHEMA %s TO %I',
            v_schema_oid::pg_catalog.regnamespace, v_worker_role
        );
    END IF;
    IF p_writable THEN
        IF NOT pg_catalog.has_table_privilege(
               v_worker_role_oid, p_relation::oid, 'SELECT'
           ) OR NOT pg_catalog.has_table_privilege(
               v_worker_role_oid, p_relation::oid, 'INSERT'
           ) OR NOT pg_catalog.has_table_privilege(
               v_worker_role_oid, p_relation::oid, 'UPDATE'
           ) OR NOT pg_catalog.has_table_privilege(
               v_worker_role_oid, p_relation::oid, 'DELETE'
           ) THEN
            EXECUTE pg_catalog.format(
                'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %s TO %I',
                p_relation, v_worker_role
            );
        END IF;
    ELSE
        IF NOT pg_catalog.has_table_privilege(
            v_worker_role_oid, p_relation::oid, 'SELECT'
        ) THEN
            EXECUTE pg_catalog.format(
                'GRANT SELECT ON TABLE %s TO %I',
                p_relation, v_worker_role
            );
        END IF;
        IF pg_catalog.has_table_privilege(
               v_worker_role_oid, p_relation::oid, 'INSERT'
           ) OR pg_catalog.has_table_privilege(
               v_worker_role_oid, p_relation::oid, 'UPDATE'
           ) OR pg_catalog.has_table_privilege(
               v_worker_role_oid, p_relation::oid, 'DELETE'
           ) THEN
            EXECUTE pg_catalog.format(
                'REVOKE INSERT, UPDATE, DELETE ON TABLE %s FROM %I',
                p_relation, v_worker_role
            );
        END IF;
    END IF;

    IF NOT pg_catalog.has_schema_privilege(
               v_worker_role_oid, v_schema_oid, 'USAGE'
           ) OR NOT pg_catalog.has_table_privilege(
               v_worker_role_oid, p_relation::oid, 'SELECT'
           ) OR (
               p_writable AND (
                   NOT pg_catalog.has_table_privilege(
                       v_worker_role_oid, p_relation::oid, 'INSERT'
                   ) OR NOT pg_catalog.has_table_privilege(
                       v_worker_role_oid, p_relation::oid, 'UPDATE'
                   ) OR NOT pg_catalog.has_table_privilege(
                       v_worker_role_oid, p_relation::oid, 'DELETE'
                   )
               )
           ) OR (
               NOT p_writable AND (
                   pg_catalog.has_table_privilege(
                       v_worker_role_oid, p_relation::oid, 'INSERT'
                   ) OR pg_catalog.has_table_privilege(
                       v_worker_role_oid, p_relation::oid, 'UPDATE'
                   ) OR pg_catalog.has_table_privilege(
                       v_worker_role_oid, p_relation::oid, 'DELETE'
                   )
               )
           ) THEN
        RAISE EXCEPTION
            'could not establish exact worker privileges on relation OID %',
            p_relation::oid
            USING ERRCODE = '40001',
                  HINT =
                      'Retry after concurrent schema DDL finishes; remove inherited or PUBLIC write grants for read-only mappings.';
    END IF;

    INSERT INTO local_cache.mapping(
        namespace, relation, key_columns, writable
    )
    VALUES (
        p_namespace, p_relation, p_key_columns, p_writable
    )
    ON CONFLICT (namespace) DO UPDATE SET
        relation = EXCLUDED.relation,
        key_columns = EXCLUDED.key_columns,
        writable = EXCLUDED.writable;

    IF NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_trigger AS t
         WHERE t.tgrelid = p_relation
           AND t.tgname = 'pg_local_cache_statement_guard'
    ) THEN
        EXECUTE pg_catalog.format(
            'CREATE TRIGGER pg_local_cache_statement_guard
               BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON %s
               FOR EACH STATEMENT
               EXECUTE FUNCTION local_cache._statement_guard()',
            p_relation
        );
        EXECUTE pg_catalog.format(
            'ALTER TRIGGER pg_local_cache_statement_guard ON %s
               DEPENDS ON EXTENSION pg_local_cache',
            p_relation
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_trigger AS t
         WHERE t.tgrelid = p_relation
           AND t.tgname = 'pg_local_cache_statement_guard'
           AND t.tgenabled = 'A'
    ) THEN
        EXECUTE pg_catalog.format(
            'ALTER TABLE %s ENABLE ALWAYS TRIGGER pg_local_cache_statement_guard',
            p_relation
        );
    END IF;

    v_trigger_arguments := pg_catalog.quote_literal(p_namespace);
    FOREACH v_key_column IN ARRAY p_key_columns LOOP
        v_trigger_arguments := v_trigger_arguments || ', ' ||
            pg_catalog.quote_literal(v_key_column::text);
    END LOOP;
    IF NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_trigger AS t
         WHERE t.tgrelid = p_relation
           AND t.tgname = 'pg_local_cache_row_invalidate'
    ) THEN
        EXECUTE pg_catalog.format(
            'CREATE TRIGGER pg_local_cache_row_invalidate
               AFTER INSERT OR UPDATE OR DELETE ON %s
               FOR EACH ROW
               EXECUTE FUNCTION local_cache._row_invalidate(%s)',
            p_relation, v_trigger_arguments
        );
        EXECUTE pg_catalog.format(
            'ALTER TRIGGER pg_local_cache_row_invalidate ON %s
               DEPENDS ON EXTENSION pg_local_cache',
            p_relation
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_trigger AS t
         WHERE t.tgrelid = p_relation
           AND t.tgname = 'pg_local_cache_row_invalidate'
           AND t.tgenabled = 'A'
    ) THEN
        EXECUTE pg_catalog.format(
            'ALTER TABLE %s ENABLE ALWAYS TRIGGER pg_local_cache_row_invalidate',
            p_relation
        );
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_trigger AS t
         WHERE t.tgrelid = p_relation
           AND t.tgname = 'pg_local_cache_truncate_invalidate'
    ) THEN
        EXECUTE pg_catalog.format(
            'CREATE TRIGGER pg_local_cache_truncate_invalidate
               AFTER TRUNCATE ON %s
               FOR EACH STATEMENT
               EXECUTE FUNCTION local_cache._truncate_invalidate(%L)',
            p_relation, p_namespace
        );
        EXECUTE pg_catalog.format(
            'ALTER TRIGGER pg_local_cache_truncate_invalidate ON %s
               DEPENDS ON EXTENSION pg_local_cache',
            p_relation
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_trigger AS t
         WHERE t.tgrelid = p_relation
           AND t.tgname = 'pg_local_cache_truncate_invalidate'
           AND t.tgenabled = 'A'
    ) THEN
        EXECUTE pg_catalog.format(
            'ALTER TABLE %s ENABLE ALWAYS TRIGGER pg_local_cache_truncate_invalidate',
            p_relation
        );
    END IF;

    SELECT pg_catalog.count(*)
      INTO v_ready_trigger_count
      FROM pg_catalog.pg_trigger AS t
     WHERE t.tgrelid = p_relation
       AND t.tgenabled = 'A'
       AND (
           (
               t.tgname = 'pg_local_cache_statement_guard'
               AND t.tgfoid =
                   'local_cache._statement_guard()'::pg_catalog.regprocedure
               AND t.tgtype = 62
               AND t.tgnargs = 0
           ) OR (
               t.tgname = 'pg_local_cache_row_invalidate'
               AND t.tgfoid =
                   'local_cache._row_invalidate()'::pg_catalog.regprocedure
               AND t.tgtype = 29
               AND t.tgnargs =
                   1 + pg_catalog.cardinality(p_key_columns)
           ) OR (
               t.tgname = 'pg_local_cache_truncate_invalidate'
               AND t.tgfoid =
                   'local_cache._truncate_invalidate()'::pg_catalog.regprocedure
               AND t.tgtype = 32
               AND t.tgnargs = 1
           )
       )
       AND EXISTS (
           SELECT 1
             FROM pg_catalog.pg_depend AS dep
             JOIN pg_catalog.pg_extension AS ext
               ON ext.oid = dep.refobjid
              AND ext.extname = 'pg_local_cache'
            WHERE dep.classid =
                  'pg_catalog.pg_trigger'::pg_catalog.regclass
              AND dep.objid = t.oid
              AND dep.objsubid = 0
              AND dep.refclassid =
                  'pg_catalog.pg_extension'::pg_catalog.regclass
              AND dep.refobjsubid = 0
              AND dep.deptype = 'x'
       );
    IF v_ready_trigger_count <> 3 THEN
        RAISE EXCEPTION
            'could not install pg_local_cache triggers on relation OID %',
            p_relation::oid
            USING ERRCODE = '40001',
                  HINT =
                      'Retry after concurrent schema DDL finishes.';
    END IF;

    PERFORM local_cache._reload();
END;
$function$;

CREATE FUNCTION _drop_owned_triggers(p_relation oid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_owned_trigger record;
BEGIN
    FOR v_owned_trigger IN
        SELECT t.oid, t.tgname
          FROM pg_catalog.pg_trigger AS t
         WHERE t.tgrelid = p_relation
           AND t.tgfoid IN (
               'local_cache._statement_guard()'::pg_catalog.regprocedure,
               'local_cache._row_invalidate()'::pg_catalog.regprocedure,
               'local_cache._truncate_invalidate()'::pg_catalog.regprocedure
           )
           AND EXISTS (
               SELECT 1
                 FROM pg_catalog.pg_depend AS dep
                 JOIN pg_catalog.pg_extension AS ext
                   ON ext.oid = dep.refobjid
                  AND ext.extname = 'pg_local_cache'
                WHERE dep.classid =
                      'pg_catalog.pg_trigger'::pg_catalog.regclass
                  AND dep.objid = t.oid
                  AND dep.objsubid = 0
                  AND dep.refclassid =
                      'pg_catalog.pg_extension'::pg_catalog.regclass
                  AND dep.refobjsubid = 0
                  AND dep.deptype = 'x'
           )
         ORDER BY t.oid
    LOOP
        EXECUTE pg_catalog.format(
            'DROP TRIGGER %I ON %s',
            v_owned_trigger.tgname,
            p_relation::pg_catalog.regclass
        );
    END LOOP;
END;
$function$;

CREATE FUNCTION detach_table(p_relation regclass)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_namespace text;
    v_worker_role text;
    v_worker_role_oid oid;
    v_worker_has_direct_acl boolean;
BEGIN
    IF p_relation IS NULL THEN
        RAISE EXCEPTION 'pg_local_cache relation must not be NULL';
    END IF;
    IF NOT local_cache._lock_relation(p_relation::oid) THEN
        RAISE EXCEPTION 'pg_local_cache relation no longer exists: %',
            p_relation::oid
            USING ERRCODE = '42P01';
    END IF;
    LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE;
    DELETE FROM local_cache.mapping AS m
     WHERE m.relation = p_relation
     RETURNING m.namespace INTO v_namespace;
    IF NOT FOUND THEN
        RETURN false;
    END IF;

    PERFORM local_cache._forget(v_namespace, p_relation::oid);
    PERFORM local_cache._drop_owned_triggers(p_relation::oid);
    v_worker_role := pg_catalog.current_setting('pg_local_cache.role', true);
    SELECT r.oid
      INTO v_worker_role_oid
      FROM pg_catalog.pg_roles AS r
     WHERE r.rolname = v_worker_role;
    IF FOUND THEN
        SELECT EXISTS (
            SELECT 1
              FROM pg_catalog.pg_class AS c
              CROSS JOIN LATERAL pg_catalog.aclexplode(
                  COALESCE(c.relacl, ARRAY[]::pg_catalog.aclitem[])
              ) AS acl
             WHERE c.oid = p_relation
               AND acl.grantee = v_worker_role_oid
        ) INTO v_worker_has_direct_acl;
        IF v_worker_has_direct_acl THEN
            EXECUTE pg_catalog.format(
                'REVOKE ALL PRIVILEGES ON TABLE %s FROM %I',
                p_relation, v_worker_role
            );
            IF EXISTS (
                SELECT 1
                  FROM pg_catalog.pg_class AS c
                  CROSS JOIN LATERAL pg_catalog.aclexplode(
                      COALESCE(c.relacl, ARRAY[]::pg_catalog.aclitem[])
                  ) AS acl
                 WHERE c.oid = p_relation
                   AND acl.grantee = v_worker_role_oid
            ) THEN
                RAISE EXCEPTION
                    'could not revoke worker privileges from relation OID %',
                    p_relation::oid
                    USING ERRCODE = '40001',
                          HINT = 'Retry after concurrent schema DDL finishes.';
            END IF;
        END IF;
    END IF;
    PERFORM local_cache._reload();
    RETURN true;
END;
$function$;

CREATE FUNCTION attach_table(
    p_relation regclass,
    p_writable boolean DEFAULT false,
    p_namespace text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_key_columns name[];
    v_namespace text;
BEGIN
    IF p_relation IS NULL THEN
        RAISE EXCEPTION 'pg_local_cache relation must not be NULL';
    END IF;
    IF NOT local_cache._lock_relation(p_relation::oid) THEN
        RAISE EXCEPTION 'pg_local_cache relation no longer exists: %',
            p_relation::oid
            USING ERRCODE = '42P01';
    END IF;
    PERFORM local_cache._validate_attach_relation(p_relation);
    v_key_columns := local_cache._primary_key_columns(p_relation);
    IF v_key_columns IS NULL THEN
        RAISE EXCEPTION 'table % has no primary key', p_relation
            USING HINT =
                'Add a PRIMARY KEY with at most 16 supported columns before attaching the table.';
    END IF;
    v_namespace := COALESCE(p_namespace, local_cache._default_namespace(p_relation));
    PERFORM local_cache._register_mapping(
        v_namespace, p_relation, v_key_columns, p_writable
    );
    RETURN local_cache._mapping_result(
        v_namespace, p_relation, v_key_columns, p_writable
    );
END;
$function$;

CREATE FUNCTION reconcile_table(p_relation regclass)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_namespace text;
    v_key_columns name[];
    v_writable boolean;
BEGIN
    IF p_relation IS NULL THEN
        RAISE EXCEPTION 'pg_local_cache relation must not be NULL';
    END IF;
    IF NOT local_cache._lock_relation(p_relation::oid) THEN
        RAISE EXCEPTION 'pg_local_cache relation no longer exists: %',
            p_relation::oid
            USING ERRCODE = '42P01';
    END IF;
    LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE;
    SELECT m.namespace, m.key_columns, m.writable
      INTO v_namespace, v_key_columns, v_writable
      FROM local_cache.mapping AS m
     WHERE m.relation = p_relation;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'table % is not attached to pg_local_cache', p_relation
            USING HINT =
                'Use attach_table() to create a mapping.';
    END IF;

    PERFORM local_cache._register_mapping(
        v_namespace, p_relation, v_key_columns, v_writable
    );
    RETURN local_cache._mapping_result(
        v_namespace, p_relation, v_key_columns, v_writable
    );
END;
$function$;

CREATE FUNCTION reconcile_all()
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_relations_before oid[];
    v_relations_after oid[];
    v_relation oid;
    v_mapping record;
    v_count integer := 0;
BEGIN
    SELECT COALESCE(
               pg_catalog.array_agg(m.relation::oid ORDER BY m.relation::oid),
               ARRAY[]::oid[]
           )
      INTO v_relations_before
      FROM local_cache.mapping AS m;

    FOREACH v_relation IN ARRAY v_relations_before LOOP
        IF NOT local_cache._lock_relation(v_relation) THEN
            RAISE EXCEPTION
                'pg_local_cache mapping references missing relation OID %',
                v_relation
                USING ERRCODE = '42P01',
                      HINT =
                          'Remove the orphan mapping or restore the table before reconciling.';
        END IF;
    END LOOP;

    LOCK TABLE local_cache.mapping IN EXCLUSIVE MODE;
    SELECT COALESCE(
               pg_catalog.array_agg(m.relation::oid ORDER BY m.relation::oid),
               ARRAY[]::oid[]
           )
      INTO v_relations_after
      FROM local_cache.mapping AS m;
    IF v_relations_before IS DISTINCT FROM v_relations_after THEN
        RAISE EXCEPTION
            'pg_local_cache mappings changed concurrently; retry the transaction'
            USING ERRCODE = '40001';
    END IF;

    FOR v_mapping IN
        SELECT m.namespace, m.relation, m.key_columns, m.writable
          FROM local_cache.mapping AS m
         ORDER BY m.relation::oid
    LOOP
        PERFORM local_cache._register_mapping(
            v_mapping.namespace,
            v_mapping.relation,
            v_mapping.key_columns,
            v_mapping.writable
        );
        v_count := v_count + 1;
    END LOOP;
    RETURN v_count;
END;
$function$;

REVOKE ALL ON FUNCTION _validate_attach_relation(regclass) FROM PUBLIC;
REVOKE ALL ON FUNCTION _primary_key_columns(regclass) FROM PUBLIC;
REVOKE ALL ON FUNCTION _default_namespace(regclass) FROM PUBLIC;
REVOKE ALL ON FUNCTION _mapping_result(text, regclass, name[], boolean)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION _prepare_trigger_slots(oid, text, name[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION _register_mapping(text, regclass, name[], boolean)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION _drop_owned_triggers(oid) FROM PUBLIC;
REVOKE ALL ON FUNCTION detach_table(regclass) FROM PUBLIC;
REVOKE ALL ON FUNCTION attach_table(regclass, boolean, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION reconcile_table(regclass) FROM PUBLIC;
REVOKE ALL ON FUNCTION reconcile_all() FROM PUBLIC;
REVOKE ALL ON FUNCTION _statement_guard() FROM PUBLIC;
REVOKE ALL ON FUNCTION _row_invalidate() FROM PUBLIC;
REVOKE ALL ON FUNCTION _truncate_invalidate() FROM PUBLIC;
REVOKE ALL ON FUNCTION _ddl_invalidate() FROM PUBLIC;
REVOKE ALL ON FUNCTION _sql_drop_invalidate() FROM PUBLIC;

CREATE EVENT TRIGGER pg_local_cache_ddl_invalidate
    ON ddl_command_end
    EXECUTE FUNCTION local_cache._ddl_invalidate();

CREATE EVENT TRIGGER pg_local_cache_sql_drop_invalidate
    ON sql_drop
    EXECUTE FUNCTION local_cache._sql_drop_invalidate();

SELECT local_cache._reload();
