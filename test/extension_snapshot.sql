WITH target_extension AS (
    SELECT oid
      FROM pg_catalog.pg_extension
     WHERE extname = 'pg_local_cache'
), members AS (
    SELECT d.classid,
           d.objid,
           d.objsubid,
           pg_catalog.pg_describe_object(d.classid, d.objid, d.objsubid) AS description
      FROM pg_catalog.pg_depend AS d
      JOIN target_extension AS e
        ON d.refclassid = 'pg_catalog.pg_extension'::regclass
       AND d.refobjid = e.oid
       AND d.deptype = 'e'
),
snapshot_objects AS (
    SELECT classid, objid, objsubid, description
      FROM members
    UNION
    SELECT 'pg_catalog.pg_constraint'::regclass,
           con.oid,
           0,
           pg_catalog.pg_describe_object(
               'pg_catalog.pg_constraint'::regclass, con.oid, 0)
      FROM members AS m
      JOIN pg_catalog.pg_constraint AS con
        ON m.classid = 'pg_catalog.pg_class'::regclass
       AND m.objid = con.conrelid
       AND m.objsubid = 0
    UNION
    SELECT 'pg_catalog.pg_class'::regclass,
           i.indexrelid,
           0,
           pg_catalog.pg_describe_object(
               'pg_catalog.pg_class'::regclass, i.indexrelid, 0)
      FROM members AS m
      JOIN pg_catalog.pg_index AS i
        ON m.classid = 'pg_catalog.pg_class'::regclass
       AND m.objid = i.indrelid
       AND m.objsubid = 0
    UNION
    SELECT 'pg_catalog.pg_trigger'::regclass,
           t.oid,
           0,
           pg_catalog.pg_describe_object(
               'pg_catalog.pg_trigger'::regclass, t.oid, 0)
      FROM members AS m
      JOIN pg_catalog.pg_trigger AS t
        ON m.classid = 'pg_catalog.pg_class'::regclass
       AND m.objid = t.tgrelid
       AND m.objsubid = 0
), acl_rows AS (
    SELECT 'relation ACL'::text AS section,
           m.description,
           CASE WHEN c.relacl IS NULL THEN '<DEFAULT>'
                ELSE COALESCE((
                    SELECT string_agg(entry, ', ' ORDER BY entry COLLATE "C")
                      FROM (
                          SELECT CASE WHEN acl.grantee = 0 THEN 'PUBLIC'
                                      ELSE pg_catalog.quote_ident(
                                          pg_catalog.pg_get_userbyid(acl.grantee))
                                  END
                                 || ' -> '
                                 || pg_catalog.quote_ident(
                                     pg_catalog.pg_get_userbyid(acl.grantor))
                                 || ': ' || acl.privilege_type
                                 || ': grantable=' || acl.is_grantable AS entry
                            FROM pg_catalog.aclexplode(c.relacl) AS acl
                      ) AS entries
                ), '<EMPTY>')
           END AS definition
      FROM members AS m
      JOIN pg_catalog.pg_class AS c
        ON m.classid = 'pg_catalog.pg_class'::regclass
       AND m.objid = c.oid
       AND m.objsubid = 0
    UNION ALL
    SELECT 'function ACL',
           m.description,
           CASE WHEN p.proacl IS NULL THEN '<DEFAULT>'
                ELSE COALESCE((
                    SELECT string_agg(entry, ', ' ORDER BY entry COLLATE "C")
                      FROM (
                          SELECT CASE WHEN acl.grantee = 0 THEN 'PUBLIC'
                                      ELSE pg_catalog.quote_ident(
                                          pg_catalog.pg_get_userbyid(acl.grantee))
                                  END
                                 || ' -> '
                                 || pg_catalog.quote_ident(
                                     pg_catalog.pg_get_userbyid(acl.grantor))
                                 || ': ' || acl.privilege_type
                                 || ': grantable=' || acl.is_grantable AS entry
                            FROM pg_catalog.aclexplode(p.proacl) AS acl
                      ) AS entries
                ), '<EMPTY>')
           END
      FROM members AS m
      JOIN pg_catalog.pg_proc AS p
        ON m.classid = 'pg_catalog.pg_proc'::regclass
       AND m.objid = p.oid
       AND m.objsubid = 0
    UNION ALL
    SELECT 'schema ACL',
           m.description,
           CASE WHEN n.nspacl IS NULL THEN '<DEFAULT>'
                ELSE COALESCE((
                    SELECT string_agg(entry, ', ' ORDER BY entry COLLATE "C")
                      FROM (
                          SELECT CASE WHEN acl.grantee = 0 THEN 'PUBLIC'
                                      ELSE pg_catalog.quote_ident(
                                          pg_catalog.pg_get_userbyid(acl.grantee))
                                  END
                                 || ' -> '
                                 || pg_catalog.quote_ident(
                                     pg_catalog.pg_get_userbyid(acl.grantor))
                                 || ': ' || acl.privilege_type
                                 || ': grantable=' || acl.is_grantable AS entry
                            FROM pg_catalog.aclexplode(n.nspacl) AS acl
                      ) AS entries
                ), '<EMPTY>')
           END
      FROM members AS m
      JOIN pg_catalog.pg_namespace AS n
        ON m.classid = 'pg_catalog.pg_namespace'::regclass
       AND m.objid = n.oid
       AND m.objsubid = 0
), snapshot_rows AS (
    SELECT 'member'::text AS section, description, ''::text AS definition
      FROM members
    UNION ALL
    SELECT 'function', m.description, pg_catalog.pg_get_functiondef(p.oid)
      FROM members AS m
      JOIN pg_catalog.pg_proc AS p
        ON m.classid = 'pg_catalog.pg_proc'::regclass
       AND m.objid = p.oid
       AND m.objsubid = 0
     WHERE p.prokind IN ('f', 'p', 'w')
    UNION ALL
    SELECT 'column',
           pg_catalog.pg_describe_object(m.classid, m.objid, a.attnum),
           'name=' || a.attname
               || '; type=' || pg_catalog.format_type(a.atttypid, a.atttypmod)
               || '; not_null=' || a.attnotnull
               || '; default=' || COALESCE(
                   pg_catalog.pg_get_expr(ad.adbin, ad.adrelid, true), '<NULL>')
      FROM members AS m
      JOIN pg_catalog.pg_class AS c
        ON m.classid = 'pg_catalog.pg_class'::regclass
       AND m.objid = c.oid
       AND m.objsubid = 0
      JOIN pg_catalog.pg_attribute AS a
        ON a.attrelid = c.oid
       AND a.attnum > 0
       AND NOT a.attisdropped
      LEFT JOIN pg_catalog.pg_attrdef AS ad
        ON ad.adrelid = a.attrelid
       AND ad.adnum = a.attnum
     WHERE c.relkind IN ('r', 'p')
    UNION ALL
    SELECT 'view', m.description, pg_catalog.pg_get_viewdef(c.oid, true)
      FROM members AS m
      JOIN pg_catalog.pg_class AS c
        ON m.classid = 'pg_catalog.pg_class'::regclass
       AND m.objid = c.oid
       AND m.objsubid = 0
     WHERE c.relkind IN ('v', 'm')
    UNION ALL
    SELECT 'index', m.description, pg_catalog.pg_get_indexdef(c.oid)
      FROM snapshot_objects AS m
      JOIN pg_catalog.pg_class AS c
        ON m.classid = 'pg_catalog.pg_class'::regclass
       AND m.objid = c.oid
       AND m.objsubid = 0
     WHERE c.relkind IN ('i', 'I')
    UNION ALL
    SELECT 'constraint', m.description,
           pg_catalog.pg_get_constraintdef(c.oid, true)
      FROM snapshot_objects AS m
      JOIN pg_catalog.pg_constraint AS c
        ON m.classid = 'pg_catalog.pg_constraint'::regclass
       AND m.objid = c.oid
       AND m.objsubid = 0
    UNION ALL
    SELECT 'trigger', m.description,
           pg_catalog.pg_get_triggerdef(t.oid, true)
               || '; enabled=' || t.tgenabled::text
      FROM snapshot_objects AS m
      JOIN pg_catalog.pg_trigger AS t
        ON m.classid = 'pg_catalog.pg_trigger'::regclass
       AND m.objid = t.oid
       AND m.objsubid = 0
    UNION ALL
    SELECT 'event trigger', m.description,
           'name=' || e.evtname
               || '; event=' || e.evtevent
               || '; function=' || pg_catalog.pg_describe_object(
                   'pg_catalog.pg_proc'::regclass, e.evtfoid, 0)
               || '; enabled=' || e.evtenabled::text
               || '; tags=' || COALESCE(e.evttags::text, '<NULL>')
      FROM members AS m
      JOIN pg_catalog.pg_event_trigger AS e
        ON m.classid = 'pg_catalog.pg_event_trigger'::regclass
       AND m.objid = e.oid
       AND m.objsubid = 0
    UNION ALL
    SELECT 'sequence', m.description,
           'type=' || pg_catalog.format_type(s.seqtypid, NULL)
               || '; start=' || s.seqstart
               || '; increment=' || s.seqincrement
               || '; min=' || s.seqmin
               || '; max=' || s.seqmax
               || '; cache=' || s.seqcache
               || '; cycle=' || s.seqcycle
      FROM members AS m
      JOIN pg_catalog.pg_class AS c
        ON m.classid = 'pg_catalog.pg_class'::regclass
       AND m.objid = c.oid
       AND m.objsubid = 0
      JOIN pg_catalog.pg_sequence AS s
        ON s.seqrelid = c.oid
     WHERE c.relkind = 'S'
    UNION ALL
    SELECT section, description, definition
      FROM acl_rows
)
SELECT section, description, definition
  FROM snapshot_rows
 ORDER BY section COLLATE "C", description COLLATE "C", definition COLLATE "C";
