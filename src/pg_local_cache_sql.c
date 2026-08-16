#include "postgres.h"

#include "access/table.h"
#include "access/tableam.h"
#include "access/parallel.h"
#include "access/transam.h"
#include "access/xact.h"
#include "access/xlog.h"
#include "catalog/dependency.h"
#include "catalog/namespace.h"
#include "catalog/pg_am_d.h"
#include "catalog/pg_class.h"
#include "catalog/pg_index.h"
#include "catalog/pg_inherits.h"
#include "catalog/pg_namespace_d.h"
#include "catalog/pg_trigger.h"
#include "catalog/pg_type_d.h"
#include "commands/extension.h"
#include "commands/trigger.h"
#include "executor/executor.h"
#include "executor/spi.h"
#include "fmgr.h"
#include "miscadmin.h"
#include "utils/acl.h"
#include "utils/array.h"
#include "utils/builtins.h"
#include "utils/fmgroids.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/rel.h"
#include "utils/reltrigger.h"
#include "utils/snapmgr.h"
#include "utils/syscache.h"
#include "utils/typcache.h"

#include "pg_local_cache.h"
#include "key_codec.h"
#include "row_payload.h"

PG_FUNCTION_INFO_V1(pg_local_cache_sql_mget);

#define PGLC_SQL_ARRAY_MAX_KEYS 1024

typedef struct PgLocalCacheSqlMeta
{
	char		nspace[PGLC_NAMESPACE_MAX];
	char		key_columns[PGLC_MAX_KEY_COLUMNS][NAMEDATALEN];
	Oid			relation_oid;
	int			key_count;
	AttrNumber key_attnos[PGLC_MAX_KEY_COLUMNS];
	Oid			key_types[PGLC_MAX_KEY_COLUMNS];
	int32		key_typmods[PGLC_MAX_KEY_COLUMNS];
	Oid			row_type_oid;
	int32		row_typmod;
	int			row_natts;
	uint64		row_fingerprint;
	uint64		config_generation;
} PgLocalCacheSqlMeta;

typedef struct PgLocalCacheSqlMgetState
{
	MemoryContext context;
	Oid			relation_oid;
	Oid			user_oid;
	uint64		config_generation;
	PgLocalCacheMapping mapping;
	SPIPlanPtr	source_plan;
	char	   *payload;
} PgLocalCacheSqlMgetState;

typedef struct PgLocalCacheSqlMgetKey
{
	Datum		values[PGLC_MAX_KEY_COLUMNS];
	char		canonical[PGLC_KEY_MAX];
	Size		canonical_len;
} PgLocalCacheSqlMgetKey;

static bool
pglc_sql_read_mapping_once(Oid relation_oid, uint64 generation,
						   PgLocalCacheSqlMeta *meta)
{
	Oid			namespace_oid;
	Oid			mapping_oid;
	Relation	mapping_relation;
	TableScanDesc scan;
	TupleTableSlot *slot;
	Snapshot	snapshot;
	AttrNumber namespace_attno;
	AttrNumber relation_attno;
	AttrNumber key_columns_attno;
	bool		found = false;

	namespace_oid = get_namespace_oid("local_cache", true);
	if (!OidIsValid(namespace_oid))
		return false;
	mapping_oid = get_relname_relid("mapping", namespace_oid);
	if (!OidIsValid(mapping_oid))
		return false;

	mapping_relation = try_table_open(mapping_oid, AccessShareLock);
	if (mapping_relation == NULL)
		return false;
	namespace_attno = get_attnum(mapping_oid, "namespace");
	relation_attno = get_attnum(mapping_oid, "relation");
	key_columns_attno = get_attnum(mapping_oid, "key_columns");
	if (namespace_attno == InvalidAttrNumber ||
		relation_attno == InvalidAttrNumber ||
		key_columns_attno == InvalidAttrNumber)
	{
		table_close(mapping_relation, AccessShareLock);
		return false;
	}

	snapshot = RegisterSnapshot(GetLatestSnapshot());
	scan = table_beginscan(mapping_relation, snapshot, 0, NULL);
	slot = table_slot_create(mapping_relation, NULL);
	while (table_scan_getnextslot(scan, ForwardScanDirection, slot))
	{
		Datum		datum;
		bool		isnull;

		datum = slot_getattr(slot, relation_attno, &isnull);
		if (isnull || DatumGetObjectId(datum) != relation_oid)
		{
			ExecClearTuple(slot);
			continue;
		}

		datum = slot_getattr(slot, namespace_attno, &isnull);
		if (!isnull)
		{
			char	   *nspace = TextDatumGetCString(datum);
			int			key_index;

			if (strlen(nspace) >= sizeof(meta->nspace))
				break;

			MemSet(meta, 0, sizeof(*meta));
			strlcpy(meta->nspace, nspace, sizeof(meta->nspace));
			meta->relation_oid = relation_oid;
			meta->config_generation = generation;

			{
				ArrayType  *key_array;
				Datum	   *key_datums;
				bool	   *key_nulls;
				int			key_count;
				int16		type_length;
				bool		type_by_value;
				char		type_alignment;

				datum = slot_getattr(slot, key_columns_attno, &isnull);
				if (isnull)
					break;
				key_array = DatumGetArrayTypeP(datum);
				get_typlenbyvalalign(NAMEOID, &type_length, &type_by_value,
								 &type_alignment);
				deconstruct_array(key_array, NAMEOID, type_length,
							  type_by_value, type_alignment,
							  &key_datums, &key_nulls, &key_count);
				if (ARR_NDIM(key_array) != 1 || ARR_LBOUND(key_array)[0] != 1 ||
					key_count < 1 || key_count > PGLC_MAX_KEY_COLUMNS)
					break;
				meta->key_count = key_count;
				for (key_index = 0; key_index < key_count; key_index++)
				{
					Name		key_name;

					if (key_nulls[key_index])
						break;
					key_name = DatumGetName(key_datums[key_index]);
					strlcpy(meta->key_columns[key_index],
							NameStr(*key_name), NAMEDATALEN);
				}
				if (key_index != key_count)
					break;
			}

			found = true;
			for (key_index = 0; key_index < meta->key_count; key_index++)
			{
				meta->key_attnos[key_index] = get_attnum(
					relation_oid, meta->key_columns[key_index]);
				if (meta->key_attnos[key_index] == InvalidAttrNumber)
					found = false;
			}
		}
		break;
	}

	ExecDropSingleTupleTableSlot(slot);
	table_endscan(scan);
	UnregisterSnapshot(snapshot);
	table_close(mapping_relation, AccessShareLock);
	return found;
}

static bool
pglc_sql_read_mapping(Oid relation_oid, PgLocalCacheSqlMeta *meta)
{
	int			attempt;

	for (attempt = 0; attempt < 2; attempt++)
	{
		uint64		before = pglc_config_generation();
		bool		found = pglc_sql_read_mapping_once(
			relation_oid, before, meta);

		if (before == pglc_config_generation())
			return found;
	}
	return false;
}

static bool
pglc_sql_trigger_function(Oid function_oid, Oid namespace_oid,
						  const char *expected_name)
{
	char	   *actual_name;
	Oid		   *argument_types = NULL;
	Oid			return_type;
	int			argument_count = 0;
	bool		matches;

	if (get_func_namespace(function_oid) != namespace_oid)
		return false;
	return_type = get_func_signature(function_oid, &argument_types,
									  &argument_count);
	if (argument_types != NULL)
		pfree(argument_types);
	if (return_type != TRIGGEROID || argument_count != 0)
		return false;
	actual_name = get_func_name(function_oid);
	if (actual_name == NULL)
		return false;
	matches = strcmp(actual_name, expected_name) == 0;
	pfree(actual_name);
	return matches;
}

static bool
pglc_sql_trigger_owned_by_extension(Oid trigger_oid, Oid extension_oid)
{
	List	   *extension_oids;
	bool		owned;

	if (!OidIsValid(trigger_oid) || !OidIsValid(extension_oid))
		return false;
	extension_oids = getAutoExtensionsOfObject(TriggerRelationId, trigger_oid);
	owned = list_member_oid(extension_oids, extension_oid);
	list_free(extension_oids);
	return owned;
}

static bool
pglc_sql_triggers_valid(Relation relation, const PgLocalCacheSqlMeta *meta,
						bool check_extension_ownership)
{
	TriggerDesc *trigger_desc = relation->trigdesc;
	Oid			namespace_oid;
	Oid			extension_oid;
	bool		guard_found = false;
	bool		row_found = false;
	bool		truncate_found = false;
	int			index;

	if (trigger_desc == NULL)
		return false;
	namespace_oid = get_namespace_oid("local_cache", true);
	if (!OidIsValid(namespace_oid))
		return false;
	extension_oid = InvalidOid;
	if (check_extension_ownership)
	{
		extension_oid = get_extension_oid("pg_local_cache", true);
		if (!OidIsValid(extension_oid))
			return false;
	}

	for (index = 0; index < trigger_desc->numtriggers; index++)
	{
		Trigger    *trigger = &trigger_desc->triggers[index];
		bool		plain_trigger;

		plain_trigger = !trigger->tgisinternal && !trigger->tgisclone &&
			!trigger->tgdeferrable && !trigger->tginitdeferred &&
			!OidIsValid(trigger->tgconstraint) &&
			!OidIsValid(trigger->tgconstrrelid) &&
			!OidIsValid(trigger->tgconstrindid) && trigger->tgnattr == 0 &&
			trigger->tgqual == NULL && trigger->tgoldtable == NULL &&
			trigger->tgnewtable == NULL;

		if (strcmp(trigger->tgname, "pg_local_cache_statement_guard") == 0)
		{
			guard_found = trigger->tgenabled == TRIGGER_FIRES_ALWAYS &&
				plain_trigger &&
				(!check_extension_ownership ||
				 pglc_sql_trigger_owned_by_extension(trigger->tgoid,
											extension_oid)) &&
				trigger->tgtype == (TRIGGER_TYPE_BEFORE | TRIGGER_TYPE_INSERT |
					TRIGGER_TYPE_UPDATE | TRIGGER_TYPE_DELETE |
					TRIGGER_TYPE_TRUNCATE) &&
				trigger->tgnargs == 0 &&
				pglc_sql_trigger_function(trigger->tgfoid, namespace_oid,
									  "_statement_guard");
		}
		else if (strcmp(trigger->tgname, "pg_local_cache_row_invalidate") == 0)
		{
			bool		key_arguments_match =
				trigger->tgnargs == meta->key_count + 1;
			int			key_index;

			if (key_arguments_match &&
				strcmp(trigger->tgargs[0], meta->nspace) != 0)
				key_arguments_match = false;
			for (key_index = 0;
				 key_arguments_match && key_index < meta->key_count;
				 key_index++)
			{
				if (strcmp(trigger->tgargs[key_index + 1],
						   meta->key_columns[key_index]) != 0)
					key_arguments_match = false;
			}

			row_found = trigger->tgenabled == TRIGGER_FIRES_ALWAYS &&
				plain_trigger &&
				(!check_extension_ownership ||
				 pglc_sql_trigger_owned_by_extension(trigger->tgoid,
											extension_oid)) &&
				trigger->tgtype == (TRIGGER_TYPE_ROW | TRIGGER_TYPE_INSERT |
					TRIGGER_TYPE_UPDATE | TRIGGER_TYPE_DELETE) &&
				key_arguments_match &&
				pglc_sql_trigger_function(trigger->tgfoid, namespace_oid,
									  "_row_invalidate");
		}
		else if (strcmp(trigger->tgname,
						"pg_local_cache_truncate_invalidate") == 0)
		{
			truncate_found = trigger->tgenabled == TRIGGER_FIRES_ALWAYS &&
				plain_trigger &&
				(!check_extension_ownership ||
				 pglc_sql_trigger_owned_by_extension(trigger->tgoid,
											extension_oid)) &&
				trigger->tgtype == TRIGGER_TYPE_TRUNCATE &&
				trigger->tgnargs == 1 &&
				pglc_sql_trigger_function(trigger->tgfoid, namespace_oid,
									  "_truncate_invalidate") &&
				strcmp(trigger->tgargs[0], meta->nspace) == 0;
		}
	}
	return guard_found && row_found && truncate_found;
}

static bool
pglc_sql_key_type_supported(Oid type_oid)
{
	return type_oid == INT2OID || type_oid == INT4OID ||
		type_oid == INT8OID || type_oid == TEXTOID ||
		type_oid == VARCHAROID || type_oid == BPCHAROID ||
		type_oid == UUIDOID;
}

/* pg_class.relhassubclass can remain set after the last child is dropped. */
static bool
pglc_sql_relation_has_children(Relation relation)
{
	List	   *children;
	bool		has_children;
	Oid			relation_oid = RelationGetRelid(relation);

	if (!relation->rd_rel->relhassubclass)
		return false;
	children = find_inheritance_children(relation_oid, NoLock);
	has_children = children != NIL;
	list_free(children);
	return has_children;
}

/*
 * relispartition is an inexpensive relcache check for declarative
 * partitions.  Traditional inheritance children do not set it, so consult
 * pg_inherits as well.  has_superclass() is exact while the caller holds a
 * lock on the relation.
 */
static bool
pglc_sql_relation_has_parent(Relation relation)
{
	if (relation->rd_rel->relispartition)
		return true;
	return has_superclass(RelationGetRelid(relation));
}

static bool
pglc_sql_source_relation_allowed(Relation relation)
{
	Oid			namespace_oid = relation->rd_rel->relnamespace;
	Oid			extension_oid;
	char	   *namespace_name;
	bool		disallowed_namespace;

	namespace_name = get_namespace_name(namespace_oid);
	if (namespace_name == NULL)
		return false;
	disallowed_namespace = strncmp(namespace_name, "pg_", 3) == 0 ||
		strcmp(namespace_name, "information_schema") == 0 ||
		strcmp(namespace_name, "local_cache") == 0;
	pfree(namespace_name);
	if (disallowed_namespace)
		return false;

	extension_oid = get_extension_oid("pg_local_cache", true);
	if (OidIsValid(extension_oid) &&
		getExtensionOfObject(NamespaceRelationId, namespace_oid) == extension_oid)
		return false;

	return !OidIsValid(getExtensionOfObject(RelationRelationId,
										 RelationGetRelid(relation)));
}

static bool
pglc_sql_relation_base_meta(Relation relation, PgLocalCacheSqlMeta *meta,
							bool check_catalog_provenance)
{
	TupleDesc	descriptor;
	HeapTuple	index_tuple;
	Oid			primary_index_oid;
	int			key_index;

	if (relation->rd_rel->relkind != RELKIND_RELATION ||
		relation->rd_rel->relpersistence != RELPERSISTENCE_PERMANENT ||
		relation->rd_rel->relam != HEAP_TABLE_AM_OID ||
		relation->rd_rel->relispartition ||
		relation->rd_rel->relrowsecurity || relation->rd_rel->relforcerowsecurity ||
		(check_catalog_provenance &&
		 !pglc_sql_source_relation_allowed(relation)) ||
		!pglc_sql_triggers_valid(relation, meta, check_catalog_provenance))
		return false;

#if PG_VERSION_NUM >= 180000
	primary_index_oid = RelationGetPrimaryKeyIndex(relation, false);
#else
	primary_index_oid = RelationGetPrimaryKeyIndex(relation);
#endif
	if (!OidIsValid(primary_index_oid))
		return false;
	index_tuple = SearchSysCache1(INDEXRELID,
							  ObjectIdGetDatum(primary_index_oid));
	if (!HeapTupleIsValid(index_tuple))
		return false;
	{
		Form_pg_index index = (Form_pg_index) GETSTRUCT(index_tuple);
		bool		valid = index->indisprimary && index->indisvalid &&
			index->indisready && index->indimmediate;

		ReleaseSysCache(index_tuple);
		if (!valid)
			return false;
	}

	descriptor = RelationGetDescr(relation);
	if (meta->key_count < 1 || meta->key_count > PGLC_MAX_KEY_COLUMNS)
		return false;
	for (key_index = 0; key_index < meta->key_count; key_index++)
	{
		AttrNumber	key_attno = meta->key_attnos[key_index];
		Form_pg_attribute key_attribute;
		int			previous;

		if (key_attno <= 0 || key_attno > descriptor->natts)
			return false;
		key_attribute = TupleDescAttr(descriptor, key_attno - 1);
		if (key_attribute->attisdropped || !key_attribute->attnotnull ||
			!pglc_sql_key_type_supported(key_attribute->atttypid) ||
			(OidIsValid(key_attribute->attcollation) &&
			 !get_collation_isdeterministic(key_attribute->attcollation)))
			return false;
		for (previous = 0; previous < key_index; previous++)
		{
			if (meta->key_attnos[previous] == key_attno)
				return false;
		}
		meta->key_types[key_index] = key_attribute->atttypid;
		meta->key_typmods[key_index] = key_attribute->atttypmod;
		if (!OidIsValid(lookup_type_cache(
				key_attribute->atttypid,
				TYPECACHE_BTREE_OPFAMILY)->btree_opf))
			return false;
	}

	meta->row_type_oid = relation->rd_rel->reltype;
	meta->row_typmod = -1;
	meta->row_natts = descriptor->natts;
	meta->row_fingerprint =
		pglc_row_payload_tupledesc_fingerprint(descriptor);
	if (!OidIsValid(meta->row_type_oid) || meta->row_natts <= 0)
		return false;
	return true;
}

static bool
pglc_sql_relation_meta(Relation relation, PgLocalCacheSqlMeta *meta,
					   bool check_catalog_provenance)
{
	return pglc_sql_relation_base_meta(relation, meta,
									   check_catalog_provenance) &&
		!pglc_sql_relation_has_children(relation) &&
		!pglc_sql_relation_has_parent(relation);
}

static bool
pglc_sql_same_mapping(const PgLocalCacheSqlMeta *left,
					  const PgLocalCacheSqlMeta *right)
{
	int			key_index;

	if (left->relation_oid != right->relation_oid ||
		left->config_generation != right->config_generation ||
		strcmp(left->nspace, right->nspace) != 0 ||
		left->key_count != right->key_count)
		return false;
	for (key_index = 0; key_index < left->key_count; key_index++)
	{
		if (left->key_attnos[key_index] != right->key_attnos[key_index] ||
			strcmp(left->key_columns[key_index],
				   right->key_columns[key_index]) != 0)
			return false;
	}
	return true;
}

typedef enum PgLocalCacheSourceVisibility
{
	PGLC_SOURCE_VISIBLE = 0,
	PGLC_SOURCE_SNAPSHOT_REJECTED,
	PGLC_SOURCE_AGE_EXPIRED
} PgLocalCacheSourceVisibility;

/*
 * source_xmin is the raw heap xmin.  Do not consult pg_xact here: the status
 * of an old, frozen tuple may already have been truncated.  A cache entry is
 * admitted only after a latest-snapshot visibility proof.  The FullXID
 * observation horizon bounds the lifetime of the raw 32-bit value to less
 * than half its ID space; after that we conservatively use the child scan.
 * For a very old/frozen raw xmin, snapshot membership can only cause a false
 * miss, never expose a version that was invisible when it was admitted.
 */
static PgLocalCacheSourceVisibility
pglc_sql_source_visibility_at(TransactionId source_xmin,
                              uint64 source_observed_full_xid,
                              Snapshot snapshot, uint64 current_full_xid)
{
	if (source_observed_full_xid == 0 ||
		current_full_xid < source_observed_full_xid ||
		current_full_xid - source_observed_full_xid >=
		UINT64CONST(0x80000000))
		return PGLC_SOURCE_AGE_EXPIRED;
	if (TransactionIdEquals(source_xmin, FrozenTransactionId) ||
		TransactionIdEquals(source_xmin, BootstrapTransactionId))
		return PGLC_SOURCE_VISIBLE;
	if (!TransactionIdIsNormal(source_xmin) ||
		TransactionIdIsCurrentTransactionId(source_xmin) ||
		XidInMVCCSnapshot(source_xmin, snapshot))
		return PGLC_SOURCE_SNAPSHOT_REJECTED;
	return PGLC_SOURCE_VISIBLE;
}

static PgLocalCacheSourceVisibility
pglc_sql_source_visibility(TransactionId source_xmin,
							   uint64 source_observed_full_xid,
							   Snapshot snapshot)
{
	return pglc_sql_source_visibility_at(
		source_xmin, source_observed_full_xid, snapshot,
		U64FromFullTransactionId(ReadNextFullTransactionId()));
}

static void
pglc_sql_mget_state_free(PgLocalCacheSqlMgetState *state)
{
	if (state == NULL)
		return;
	if (state->source_plan != NULL)
		SPI_freeplan(state->source_plan);
	MemoryContextDelete(state->context);
	pfree(state);
}

static PgLocalCacheSqlMgetState *
pglc_sql_mget_state(FunctionCallInfo fcinfo, Oid relation_oid)
{
	PgLocalCacheSqlMgetState *state = fcinfo->flinfo->fn_extra;
	PgLocalCacheSqlMeta meta;
	PgLocalCacheSqlMeta validated_meta;
	MemoryContext function_context = fcinfo->flinfo->fn_mcxt;
	MemoryContext old_context;
	Relation	relation;
	StringInfoData where_clause;
	char	   *qualified_relation;
	char	   *query;
	Oid			argument_types[PGLC_MAX_KEY_COLUMNS];
	int			key_index;

	if (state != NULL && state->relation_oid == relation_oid &&
		state->user_oid == GetUserId() &&
		state->config_generation == pglc_config_generation())
		return state;
	pglc_sql_mget_state_free(state);
	fcinfo->flinfo->fn_extra = NULL;
	if (!pglc_sql_read_mapping(relation_oid, &meta))
		ereport(ERROR,
				(errcode(ERRCODE_UNDEFINED_OBJECT),
				 errmsg("relation %u is not attached to pg_local_cache",
						relation_oid)));

	relation = table_open(relation_oid, AccessShareLock);
	validated_meta = meta;
	if (!pglc_sql_relation_meta(relation, &validated_meta, true) ||
		!pglc_sql_same_mapping(&meta, &validated_meta))
	{
		table_close(relation, NoLock);
		ereport(ERROR,
				(errcode(ERRCODE_OBJECT_NOT_IN_PREREQUISITE_STATE),
					 errmsg("relation %u is not a valid pg_local_cache mapping",
							relation_oid)));
	}
	meta = validated_meta;

	old_context = MemoryContextSwitchTo(function_context);
	state = palloc0(sizeof(*state));
	state->context = AllocSetContextCreate(function_context,
											"pg_local_cache SQL mget",
											ALLOCSET_DEFAULT_SIZES);
	MemoryContextSwitchTo(state->context);
	state->relation_oid = relation_oid;
	state->user_oid = GetUserId();
	state->config_generation = meta.config_generation;
	state->payload = palloc(PGLC_VALUE_MAX);
	state->mapping.relation_oid = relation_oid;
	state->mapping.key_count = meta.key_count;
	state->mapping.row_type_oid = meta.row_type_oid;
	state->mapping.row_typmod = meta.row_typmod;
	state->mapping.row_natts = meta.row_natts;
	state->mapping.row_descriptor_fingerprint = meta.row_fingerprint;
	state->mapping.config_generation = meta.config_generation;
	state->mapping.row_desc = CreateTupleDescCopy(RelationGetDescr(relation));
	strlcpy(state->mapping.nspace, meta.nspace,
			sizeof(state->mapping.nspace));
	strlcpy(state->mapping.schema_name,
			get_namespace_name(RelationGetNamespace(relation)),
			sizeof(state->mapping.schema_name));
	strlcpy(state->mapping.relation_name, RelationGetRelationName(relation),
			sizeof(state->mapping.relation_name));
	for (key_index = 0; key_index < meta.key_count; key_index++)
	{
		Oid			input_function;
		Oid			output_function;
		bool		is_varlena;

		state->mapping.key_attnos[key_index] = meta.key_attnos[key_index];
		state->mapping.key_types[key_index] = meta.key_types[key_index];
		state->mapping.key_typmods[key_index] = meta.key_typmods[key_index];
		strlcpy(state->mapping.key_columns[key_index],
				meta.key_columns[key_index], NAMEDATALEN);
		getTypeInputInfo(meta.key_types[key_index], &input_function,
						 &state->mapping.key_ioparams[key_index]);
		getTypeOutputInfo(meta.key_types[key_index], &output_function,
						  &is_varlena);
		fmgr_info_cxt(input_function, &state->mapping.key_inputs[key_index],
					  state->context);
		fmgr_info_cxt(output_function, &state->mapping.key_outputs[key_index],
					  state->context);
		argument_types[key_index] = meta.key_types[key_index];
	}

	qualified_relation = quote_qualified_identifier(
		state->mapping.schema_name, state->mapping.relation_name);
	initStringInfo(&where_clause);
	for (key_index = 0; key_index < meta.key_count; key_index++)
	{
		if (key_index > 0)
			appendStringInfoString(&where_clause, " AND ");
		appendStringInfo(&where_clause, "pglc_source.%s = $%d",
						 quote_identifier(meta.key_columns[key_index]),
						 key_index + 1);
	}
	query = psprintf(
		"SELECT pglc_source, pglc_source.xmin "
		"FROM ONLY %s AS pglc_source WHERE %s LIMIT 1",
		qualified_relation, where_clause.data);
	if (SPI_connect() != SPI_OK_CONNECT)
		elog(ERROR, "pg_local_cache SQL mget could not connect to SPI");
	state->source_plan = SPI_prepare(query, meta.key_count, argument_types);
	if (state->source_plan == NULL || SPI_keepplan(state->source_plan) != 0)
		elog(ERROR, "pg_local_cache SQL mget could not retain its source plan");
	if (SPI_finish() != SPI_OK_FINISH)
		elog(ERROR, "pg_local_cache SQL mget could not finish SPI setup");
	table_close(relation, NoLock);
	MemoryContextSwitchTo(old_context);
	fcinfo->flinfo->fn_extra = state;
	return state;
}

static bool
pglc_sql_mget_values(PgLocalCacheSqlMgetState *state, Datum *elements,
					bool *nulls, int count, Datum *values,
					char *canonical, Size *canonical_len)
{
	bool		key_nulls[PGLC_MAX_KEY_COLUMNS] = {false};
	int			key_index;

	for (key_index = 0; key_index < count; key_index++)
	{
		char	   *input;

		if (nulls[key_index])
			ereport(ERROR,
					(errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
					 errmsg("primary-key values cannot be NULL")));
		input = TextDatumGetCString(elements[key_index]);
		values[key_index] = InputFunctionCall(
			&state->mapping.key_inputs[key_index], input,
			state->mapping.key_ioparams[key_index],
			state->mapping.key_typmods[key_index]);
	}
	return pglc_canonical_key_typed(
		values, key_nulls, count, state->mapping.key_types,
		state->mapping.key_outputs, canonical, PGLC_KEY_MAX, canonical_len);
}

static bool
pglc_sql_mget_can_use_cache(PgLocalCacheSqlMgetState *state)
{
	Snapshot	snapshot = GetActiveSnapshot();

	return XactIsoLevel == XACT_READ_COMMITTED &&
		!RecoveryInProgress() && !IsParallelWorker() && !IsInParallelMode() &&
		!pglc_current_transaction_is_dirty() && snapshot != NULL &&
		snapshot->snapshot_type == SNAPSHOT_MVCC &&
		state->config_generation == pglc_config_generation();
}

static bool
pglc_sql_mget_latest_matches(PgLocalCacheSqlMgetState *state,
							Datum *key_values, bool found,
							TransactionId source_xmin)
{
	volatile Snapshot latest_snapshot = NULL;
	volatile bool matches = false;

	PG_TRY();
	{
		bool		isnull;

		latest_snapshot = RegisterSnapshot(GetLatestSnapshot());
		if (SPI_execute_snapshot(
				state->source_plan, key_values, NULL, (Snapshot) latest_snapshot,
				InvalidSnapshot, true, false, 1) != SPI_OK_SELECT)
			elog(ERROR, "pg_local_cache SQL mget latest-snapshot proof failed");
		if (!found)
			matches = SPI_processed == 0;
		else if (SPI_processed == 1)
		{
			Datum		latest_xmin = SPI_getbinval(
				SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 2, &isnull);

			matches = !isnull && TransactionIdEquals(
				DatumGetTransactionId(latest_xmin), source_xmin);
		}
		UnregisterSnapshot((Snapshot) latest_snapshot);
		latest_snapshot = NULL;
	}
	PG_CATCH();
	{
		if (latest_snapshot != NULL)
			UnregisterSnapshot((Snapshot) latest_snapshot);
		PG_RE_THROW();
	}
	PG_END_TRY();
	return matches;
}

static Datum
pglc_sql_mget_source(PgLocalCacheSqlMgetState *state, Datum *key_values,
					bool prove_fill, TransactionId *source_xmin, Size *payload_len,
					bool *payload_cacheable, bool *found)
{
	MemoryContext result_context = CurrentMemoryContext;
	TupleTableSlot *row_slot;
	Datum		row;
	Datum		xmin;
	Datum		result;
	bool		isnull;
	const char *json;
	Size		json_len;
	MemoryContext old_context;

	*found = false;
	*payload_cacheable = false;
	*payload_len = 0;
	*source_xmin = InvalidTransactionId;
	if (SPI_connect() != SPI_OK_CONNECT)
		elog(ERROR, "pg_local_cache SQL mget could not connect to SPI");
	if (SPI_execute_plan(state->source_plan, key_values, NULL, true, 1) !=
		SPI_OK_SELECT)
		elog(ERROR, "pg_local_cache SQL mget source plan failed");
	if (SPI_processed == 0)
	{
		*payload_cacheable = prove_fill && pglc_sql_mget_latest_matches(
			state, key_values, false, InvalidTransactionId);
		if (SPI_finish() != SPI_OK_FINISH)
			elog(ERROR, "pg_local_cache SQL mget could not finish SPI");
		pglc_note_database_read();
		return (Datum) 0;
	}
	if (SPI_processed != 1)
		elog(ERROR, "pg_local_cache SQL mget returned more than one primary-key row");
	row = SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 1,
						&isnull);
	if (isnull)
		elog(ERROR, "pg_local_cache SQL mget row unexpectedly became NULL");
	xmin = SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 2,
						 &isnull);
	if (isnull)
		elog(ERROR, "pg_local_cache SQL mget xmin unexpectedly became NULL");
	*source_xmin = DatumGetTransactionId(xmin);
	row_slot = MakeSingleTupleTableSlot(state->mapping.row_desc, &TTSOpsVirtual);
	ExecStoreHeapTupleDatum(row, row_slot);
	*payload_cacheable = pglc_row_payload_encode(
		row_slot, state->mapping.row_desc, PGLC_ROW_PAYLOAD_FLAG_HAS_JSON,
		state->payload, PGLC_VALUE_MAX, payload_len);
	if (*payload_cacheable && pglc_row_payload_get_json_checked(
			state->payload, *payload_len, state->mapping.row_type_oid,
			state->mapping.row_typmod, state->mapping.row_natts,
			state->mapping.row_descriptor_fingerprint, &json, &json_len))
	{
		/* JSON already belongs to the payload copied outside SPI memory. */
	}
	else
	{
		text	   *json_text = DatumGetTextPP(
			OidFunctionCall1(F_ROW_TO_JSON_RECORD, row));

		*payload_cacheable = false;
		json = VARDATA_ANY(json_text);
		json_len = VARSIZE_ANY_EXHDR(json_text);
	}
	old_context = MemoryContextSwitchTo(result_context);
	result = PointerGetDatum(cstring_to_text_with_len(json, json_len));
	MemoryContextSwitchTo(old_context);
	ExecDropSingleTupleTableSlot(row_slot);
	if (*payload_cacheable && prove_fill)
		*payload_cacheable = pglc_sql_mget_latest_matches(
			state, key_values, true, *source_xmin);
	else
		*payload_cacheable = false;
	*found = true;
	if (SPI_finish() != SPI_OK_FINISH)
		elog(ERROR, "pg_local_cache SQL mget could not finish SPI");
	pglc_note_database_read();
	return result;
}

static Datum
pglc_sql_mget_canonical(FunctionCallInfo fcinfo,
						PgLocalCacheSqlMgetState *state, Datum *key_values,
						const char *canonical, Size canonical_len)
{
	Size		cached_len;
	bool		negative;
	TransactionId source_xmin;
	PgLocalCacheReadToken token;
	PgLocalCacheSourceVisibility visibility;
	const char *json;
	Size		json_len;
	bool		cacheable = pglc_sql_mget_can_use_cache(state);
	bool		owns_load = false;
	bool		found;
	bool		payload_cacheable;
	Size		payload_len;
	uint64		load_id = 0;
	Datum		result;

	if (cacheable && pglc_cache_lookup_quiet(
			&state->mapping, canonical, state->payload, PGLC_VALUE_MAX,
			&cached_len, &negative, &source_xmin, &token))
	{
		if (!negative)
		{
			visibility = pglc_sql_source_visibility(
				source_xmin, token.source_observed_full_xid,
				GetActiveSnapshot());
			if (visibility == PGLC_SOURCE_VISIBLE &&
				pglc_row_payload_get_json_checked(
					state->payload, cached_len, state->mapping.row_type_oid,
					state->mapping.row_typmod, state->mapping.row_natts,
					state->mapping.row_descriptor_fingerprint,
					&json, &json_len))
			{
				pglc_note_sql_cache_hit();
				PG_RETURN_TEXT_P(cstring_to_text_with_len(json, json_len));
			}
			if (visibility == PGLC_SOURCE_AGE_EXPIRED ||
				visibility == PGLC_SOURCE_VISIBLE)
				(void) pglc_cache_retire_positive(
					&state->mapping, canonical, &token, source_xmin);
		}
	}
	if (cacheable)
	{
		(void) pglc_cache_lookup_quiet(
			&state->mapping, canonical, state->payload, PGLC_VALUE_MAX,
			&cached_len, &negative, &source_xmin, &token);
		pglc_note_sql_cache_miss();
		owns_load = pglc_cache_claim_load(
			&state->mapping, canonical, &token, &load_id) == PGLC_LOAD_OWNER;
	}
	else
		pglc_note_sql_cache_bypass();

	PG_TRY();
	{
		result = pglc_sql_mget_source(
			state, key_values, owns_load, &source_xmin,
			&payload_len, &payload_cacheable, &found);
		if (owns_load)
		{
			bool		stored = false;

			if (payload_cacheable)
				stored = pglc_cache_store(
					&state->mapping, canonical, &token,
					found ? state->payload : NULL,
					found ? payload_len : 0, !found, load_id,
					found ? source_xmin : InvalidTransactionId);
			if (stored)
				pglc_note_sql_cache_fill();
			pglc_cache_release_load(
				&state->mapping, canonical, &token, load_id);
			owns_load = false;
		}
	}
	PG_CATCH();
	{
		if (owns_load)
			pglc_cache_release_load(
				&state->mapping, canonical, &token, load_id);
		PG_RE_THROW();
	}
	PG_END_TRY();
	if (!found)
		PG_RETURN_NULL();
	PG_RETURN_DATUM(result);
}

static void
pglc_sql_mget_acl_check(const PgLocalCacheSqlMgetState *state)
{
	if (pg_class_aclcheck(state->relation_oid, GetUserId(), ACL_SELECT) != ACLCHECK_OK)
		aclcheck_error(ACLCHECK_NO_PRIV, OBJECT_TABLE,
					   state->mapping.relation_name);
}

static Datum
pglc_sql_mget_rows(FunctionCallInfo fcinfo,
				   PgLocalCacheSqlMgetState *state, ArrayType *key_array)
{
	Datum	   *elements;
	bool	   *nulls;
	int			element_count;
	int			key_count;
	int			key_index;
	int			component_count;
	PgLocalCacheSqlMgetKey *keys;
	ArrayBuildState *result = NULL;

	if (ARR_ELEMTYPE(key_array) != TEXTOID)
		ereport(ERROR,
				(errcode(ERRCODE_DATATYPE_MISMATCH),
				 errmsg("SQL mget for a composite primary key requires text[][]")));
	if (ARR_NDIM(key_array) == 0)
		return PointerGetDatum(construct_empty_array(TEXTOID));
	if (ARR_NDIM(key_array) != 2)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("SQL mget for a composite primary key requires a two-dimensional array")));
	key_count = ARR_DIMS(key_array)[0];
	component_count = ARR_DIMS(key_array)[1];
	if (component_count != state->mapping.key_count)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("expected %d primary-key values per key, got %d",
						state->mapping.key_count, component_count)));
	if (key_count > PGLC_SQL_ARRAY_MAX_KEYS)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("SQL composite mget accepts at most %d keys",
						PGLC_SQL_ARRAY_MAX_KEYS)));
	deconstruct_array(key_array, TEXTOID, -1, false, TYPALIGN_INT,
					  &elements, &nulls, &element_count);
	Assert(element_count == key_count * component_count);
	keys = palloc0(mul_size((Size) key_count, sizeof(*keys)));

	/* Validate and canonicalize the complete batch before any cache/source read. */
	for (key_index = 0; key_index < key_count; key_index++)
	{
		PgLocalCacheSqlMgetKey *key = &keys[key_index];
		int			component_offset = key_index * component_count;

		if (!pglc_sql_mget_values(
				state, &elements[component_offset], &nulls[component_offset],
				component_count, key->values, key->canonical,
				&key->canonical_len) || key->canonical_len == 0)
			ereport(ERROR,
					(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
					 errmsg("canonical primary key exceeds %d bytes",
							PGLC_KEY_MAX - 1)));
	}
	for (key_index = 0; key_index < key_count; key_index++)
	{
		PgLocalCacheSqlMgetKey *key = &keys[key_index];
		Datum		value;
		bool		isnull;

		fcinfo->isnull = false;
		value = pglc_sql_mget_canonical(
			fcinfo, state, key->values, key->canonical, key->canonical_len);
		isnull = fcinfo->isnull;
		fcinfo->isnull = false;
		result = accumArrayResult(
			result, value, isnull, TEXTOID, CurrentMemoryContext);
	}
	if (result == NULL)
		return PointerGetDatum(construct_empty_array(TEXTOID));
	return makeArrayResult(result, CurrentMemoryContext);
}

static Datum
pglc_sql_mget_common(FunctionCallInfo fcinfo,
					 PgLocalCacheSqlMgetState *state)
{
	ArrayType  *key_array;
	Oid			key_type;
	Datum	   *key_values;
	bool	   *key_nulls;
	int16		typlen;
	bool		typbyval;
	char		typalign;
	int			key_count;
	int			key_index;
	ArrayBuildState *result = NULL;

	if (PG_ARGISNULL(1))
		PG_RETURN_NULL();
	key_array = PG_GETARG_ARRAYTYPE_P(1);
	key_type = ARR_ELEMTYPE(key_array);
	pglc_sql_mget_acl_check(state);
	if (state->mapping.key_count > 1)
		return pglc_sql_mget_rows(fcinfo, state, key_array);
	if (state->mapping.key_count != 1 ||
		key_type != state->mapping.key_types[0])
		ereport(ERROR,
				(errcode(ERRCODE_DATATYPE_MISMATCH),
				 errmsg("SQL mget requires one primary-key column of type %s",
						format_type_be(state->mapping.key_types[0]))));
	get_typlenbyvalalign(key_type, &typlen, &typbyval, &typalign);
	deconstruct_array(key_array, key_type, typlen, typbyval, typalign,
					  &key_values, &key_nulls, &key_count);
	if (key_count > PGLC_SQL_ARRAY_MAX_KEYS)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("SQL mget accepts at most %d keys",
						PGLC_SQL_ARRAY_MAX_KEYS)));
	for (key_index = 0; key_index < key_count; key_index++)
	{
		char		canonical[PGLC_KEY_MAX];
		Size		canonical_len;
		Datum		value = (Datum) 0;
		bool		isnull = key_nulls[key_index];

		if (!isnull)
		{
			if (!pglc_canonical_key_typed(
					&key_values[key_index], &isnull, 1,
					state->mapping.key_types, state->mapping.key_outputs,
					canonical, sizeof(canonical), &canonical_len) ||
				canonical_len == 0)
				ereport(ERROR,
						(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
						 errmsg("canonical primary key exceeds %d bytes",
								PGLC_KEY_MAX - 1)));
			fcinfo->isnull = false;
			value = pglc_sql_mget_canonical(
				fcinfo, state, &key_values[key_index], canonical,
				canonical_len);
			isnull = fcinfo->isnull;
			fcinfo->isnull = false;
		}
		result = accumArrayResult(
			result, value, isnull, TEXTOID, CurrentMemoryContext);
	}
	if (result == NULL)
		PG_RETURN_ARRAYTYPE_P(construct_empty_array(TEXTOID));
	PG_RETURN_DATUM(makeArrayResult(result, CurrentMemoryContext));
}

Datum
pg_local_cache_sql_mget(PG_FUNCTION_ARGS)
{
	Oid			relation_oid = PG_GETARG_OID(0);
	PgLocalCacheSqlMgetState *state = pglc_sql_mget_state(fcinfo, relation_oid);

	return pglc_sql_mget_common(fcinfo, state);
}
