\echo Use "ALTER EXTENSION pg_local_cache UPDATE TO '3.2.0'" to load this file. \quit

-- W7 adds per-mapping write refresh while keeping invalidation as the default.
ALTER TABLE local_cache.mapping
    ADD COLUMN write_mode text NOT NULL DEFAULT 'invalidate'
        CONSTRAINT mapping_write_mode_check
        CHECK (write_mode IN ('invalidate', 'refresh'));

CREATE FUNCTION local_cache._effective_write_mode(
    p_relation regclass,
    p_write_mode text
)
RETURNS text
LANGUAGE sql
STABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT CASE
    WHEN p_write_mode = 'refresh'
     AND NOT EXISTS (
         SELECT 1
           FROM pg_catalog.pg_attribute AS a
          WHERE a.attrelid = p_relation
            AND a.attnum > 0
            AND NOT a.attisdropped
            AND (a.attgenerated = 'v' OR a.atttypid NOT IN (
                    'pg_catalog.bool'::pg_catalog.regtype,
                    'pg_catalog.int2'::pg_catalog.regtype,
                    'pg_catalog.int4'::pg_catalog.regtype,
                    'pg_catalog.int8'::pg_catalog.regtype,
                    'pg_catalog.text'::pg_catalog.regtype,
                    'pg_catalog.varchar'::pg_catalog.regtype
                ))
     ) THEN 'refresh'
    ELSE 'invalidate'
END;
$function$;

CREATE OR REPLACE FUNCTION local_cache._mapping_result(
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
    v_write_mode text;
    v_effective_write_mode text;
BEGIN
    SELECT n.nspname, c.relname
      INTO STRICT v_schema_name, v_relation_name
      FROM pg_catalog.pg_class AS c
      JOIN pg_catalog.pg_namespace AS n
        ON n.oid = c.relnamespace
     WHERE c.oid = p_relation;

    SELECT m.write_mode,
           local_cache._effective_write_mode(m.relation, m.write_mode)
      INTO STRICT v_write_mode, v_effective_write_mode
      FROM local_cache.mapping AS m
     WHERE m.relation = p_relation;

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
        'write_mode_requested', v_write_mode,
        'write_mode_effective', v_effective_write_mode,
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

/* Extend existing trigger-slot repair and registration in place. Keeping
 * their signatures lets existing attach/reconcile functions keep working. */
DO $w7_function_updates$
DECLARE
    v_definition text;
    v_before text;
    v_after text;
BEGIN
    v_definition := pg_catalog.pg_get_functiondef(
        'local_cache._prepare_trigger_slots(oid,text,name[])'::pg_catalog.regprocedure
    );
    v_before := $old$
    v_expected_row_args bytea;
    v_expected_truncate_args bytea;$old$;
    v_after := $new$
    v_expected_row_args bytea;
    v_expected_truncate_args bytea;
    v_write_mode text;
    v_effective_write_mode text;$new$;
    IF pg_catalog.strpos(v_definition, v_before) = 0 THEN
        RAISE EXCEPTION 'could not patch pg_local_cache trigger-slot declarations';
    END IF;
    v_definition := pg_catalog.replace(v_definition, v_before, v_after);

    v_before := $old$
BEGIN
    v_expected_row_args :=
        pg_catalog.convert_to(p_namespace, v_encoding) || v_zero;$old$;
    v_after := $new$
BEGIN
    SELECT COALESCE(m.write_mode, 'invalidate')
      INTO v_write_mode
      FROM local_cache.mapping AS m
     WHERE m.relation::oid = p_relation;
    v_write_mode := COALESCE(v_write_mode, 'invalidate');
    v_effective_write_mode := local_cache._effective_write_mode(
        p_relation::regclass, v_write_mode
    );
    v_expected_row_args :=
        pg_catalog.convert_to(p_namespace, v_encoding) || v_zero ||
        pg_catalog.convert_to(v_effective_write_mode, v_encoding) || v_zero;$new$;
    IF pg_catalog.strpos(v_definition, v_before) = 0 THEN
        RAISE EXCEPTION 'could not patch pg_local_cache trigger-slot arguments';
    END IF;
    v_definition := pg_catalog.replace(v_definition, v_before, v_after);

    v_before := $old$
                   WHEN 'pg_local_cache_row_invalidate' THEN
                       1 + pg_catalog.cardinality(p_key_columns)$old$;
    v_after := $new$
                   WHEN 'pg_local_cache_row_invalidate' THEN
                       2 + pg_catalog.cardinality(p_key_columns)$new$;
    IF pg_catalog.strpos(v_definition, v_before) = 0 THEN
        RAISE EXCEPTION 'could not patch pg_local_cache trigger-slot arity';
    END IF;
    v_definition := pg_catalog.replace(v_definition, v_before, v_after);
    EXECUTE v_definition;

    v_definition := pg_catalog.pg_get_functiondef(
        'local_cache._register_mapping(text,regclass,name[],boolean)'::pg_catalog.regprocedure
    );
    v_before := $old$
    v_trigger_arguments text;
    v_ready_trigger_count integer;$old$;
    v_after := $new$
    v_trigger_arguments text;
    v_write_mode text;
    v_effective_write_mode text;
    v_ready_trigger_count integer;$new$;
    IF pg_catalog.strpos(v_definition, v_before) = 0 THEN
        RAISE EXCEPTION 'could not patch pg_local_cache register declarations';
    END IF;
    v_definition := pg_catalog.replace(v_definition, v_before, v_after);

    v_before := $old$
    ON CONFLICT (namespace) DO UPDATE SET
        relation = EXCLUDED.relation,
        key_columns = EXCLUDED.key_columns,
        writable = EXCLUDED.writable;

    IF NOT EXISTS ($old$;
    v_after := $new$
    ON CONFLICT (namespace) DO UPDATE SET
        relation = EXCLUDED.relation,
        key_columns = EXCLUDED.key_columns,
        writable = EXCLUDED.writable;

    SELECT m.write_mode
      INTO STRICT v_write_mode
      FROM local_cache.mapping AS m
     WHERE m.relation = p_relation;
    v_effective_write_mode := local_cache._effective_write_mode(
        p_relation, v_write_mode
    );

    IF NOT EXISTS ($new$;
    IF pg_catalog.strpos(v_definition, v_before) = 0 THEN
        RAISE EXCEPTION 'could not patch pg_local_cache registered mode';
    END IF;
    v_definition := pg_catalog.replace(v_definition, v_before, v_after);

    v_before := $old$
    v_trigger_arguments := pg_catalog.quote_literal(p_namespace);$old$;
    v_after := $new$
    v_trigger_arguments := pg_catalog.quote_literal(p_namespace) || ', ' ||
        pg_catalog.quote_literal(v_effective_write_mode);$new$;
    IF pg_catalog.strpos(v_definition, v_before) = 0 THEN
        RAISE EXCEPTION 'could not patch pg_local_cache row-trigger arguments';
    END IF;
    v_definition := pg_catalog.replace(v_definition, v_before, v_after);

    v_before := $old$
               AND t.tgnargs =
                   1 + pg_catalog.cardinality(p_key_columns)$old$;
    v_after := $new$
               AND t.tgnargs =
                   2 + pg_catalog.cardinality(p_key_columns)$new$;
    IF pg_catalog.strpos(v_definition, v_before) = 0 THEN
        RAISE EXCEPTION 'could not patch pg_local_cache registered trigger arity';
    END IF;
    v_definition := pg_catalog.replace(v_definition, v_before, v_after);
    EXECUTE v_definition;
END;
$w7_function_updates$;

CREATE FUNCTION local_cache.set_write_mode(
    p_relation regclass,
    p_write_mode text
)
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
    IF NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_roles AS r
         WHERE r.rolname = SESSION_USER
           AND r.rolsuper
    ) THEN
        RAISE EXCEPTION 'only a superuser may change pg_local_cache write mode'
            USING ERRCODE = '42501';
    END IF;
    IF p_relation IS NULL THEN
        RAISE EXCEPTION 'pg_local_cache relation must not be NULL';
    END IF;
    IF p_write_mode IS NULL OR
       p_write_mode NOT IN ('invalidate', 'refresh') THEN
        RAISE EXCEPTION 'unknown pg_local_cache write mode: %', p_write_mode
            USING ERRCODE = '22023',
                  HINT = 'Use invalidate or refresh.';
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
            USING HINT = 'Use attach_table() to create a mapping.';
    END IF;
    IF p_write_mode = 'refresh' AND NOT v_writable THEN
        RAISE EXCEPTION 'refresh write mode requires a writable mapping for %',
            p_relation
            USING ERRCODE = '55000',
                  HINT = 'Attach the table with p_writable = true first.';
    END IF;

    /* The mapping statement trigger reloads the catalog and fences stale
     * entries. Re-registering replaces row-trigger args with the new mode. */
    UPDATE local_cache.mapping
       SET write_mode = p_write_mode
     WHERE relation = p_relation;
    PERFORM local_cache._register_mapping(
        v_namespace, p_relation, v_key_columns, v_writable
    );
    RETURN local_cache._mapping_result(
        v_namespace, p_relation, v_key_columns, v_writable
    );
END;
$function$;

REVOKE ALL ON FUNCTION local_cache._effective_write_mode(regclass, text)
    FROM PUBLIC;

/* Reconcile through the extension's SECURITY DEFINER path so existing
 * mappings receive mode arguments. Missing 3.1 mode defaults to invalidate. */
SELECT local_cache.reconcile_all();
SELECT local_cache._reload();

REVOKE ALL ON FUNCTION local_cache.set_write_mode(regclass, text)
    FROM PUBLIC;
