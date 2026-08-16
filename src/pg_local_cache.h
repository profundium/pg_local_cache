#ifndef PG_LOCAL_CACHE_H
#define PG_LOCAL_CACHE_H

#include "postgres.h"

#include "access/transam.h"
#include "access/tupdesc.h"
#include "executor/spi.h"
#include "fmgr.h"
#include "port/atomics.h"
#include "storage/lwlock.h"
#include "utils/hsearch.h"

#include "resp_limits.h"

#define PGLC_VERSION "1.3.0"
#define PGLC_VERSION_LENGTH "5"
#ifndef PGLC_BUILD_ID
#error "PGLC_BUILD_ID must be supplied by the build"
#endif

#define PGLC_NAMESPACE_MAX 64
#define PGLC_MAX_KEY_COLUMNS 16
#define PGLC_KEY_MAX 1024
#define PGLC_VALUE_MAX 8192
#define PGLC_RESPONSE_VALUE_MAX (64 * 1024)
#define PGLC_MAX_MAPPINGS 128
#define PGLC_MAX_WORKERS 32
#define PGLC_MAX_CLIENTS_PER_WORKER 128
#define PGLC_RESPONSE_MAX (PGLC_RESPONSE_VALUE_MAX + 1024)
#define PGLC_AUTH_TOKEN_MAX 1024
#define PGLC_MAX_AUTH_FAILURES 5
#define PGLC_EVICTION_SAMPLE 64

typedef struct PgLocalCacheCacheKey
{
	Oid			database_oid;
	char		nspace[PGLC_NAMESPACE_MAX];
	char		key[PGLC_KEY_MAX];
} PgLocalCacheCacheKey;

typedef struct PgLocalCacheCacheEntry
{
	PgLocalCacheCacheKey key;
	Oid			relation_oid;
	uint64		global_epoch;
	uint64		relation_version;
	uint64		version;
	uint64		load_id;
	uint64		load_global_version;
	uint64		load_relation_version;
	uint64		load_key_version;
	TimestampTz load_started;
	pg_atomic_uint64 last_access;
	uint32		dirty_writers;
	uint32		value_len;
	TransactionId source_xmin;
	/* Full-XID horizon observed when source_xmin was admitted. */
	uint64		source_observed_full_xid;
	bool		valid;
	bool		negative;
	bool		loading;
	char		value[PGLC_VALUE_MAX];
} PgLocalCacheCacheEntry;

typedef struct PgLocalCacheRelationKey
{
	Oid			database_oid;
	char		nspace[PGLC_NAMESPACE_MAX];
} PgLocalCacheRelationKey;

typedef struct PgLocalCacheRelationState
{
	PgLocalCacheRelationKey key;
	Oid			relation_oid;
	uint64		version;
	uint32		dirty_writers;
	bool		pending_forget;
} PgLocalCacheRelationState;

/*
 * One SQL counter slot belongs to one PostgreSQL process slot for its entire
 * lifetime.  The union fixes the stride at a cache line; shared-memory
 * startup additionally aligns the first element to the same boundary.
 */
typedef union PgLocalCacheSqlCounterSlot
{
	struct
	{
		pg_atomic_uint64 hits;
		pg_atomic_uint64 misses;
		pg_atomic_uint64 fills;
		pg_atomic_uint64 bypasses;
	} counters;
	char		padding[PG_CACHE_LINE_SIZE];
} PgLocalCacheSqlCounterSlot;

StaticAssertDecl(sizeof(PgLocalCacheSqlCounterSlot) == PG_CACHE_LINE_SIZE,
				 "SQL counter slot must occupy exactly one cache line");

typedef struct PgLocalCacheSharedState
{
	LWLock	   *lock;
	pg_atomic_uint64 clock;
	pg_atomic_uint64 entry_generation;
	uint64		global_version;
	uint64		global_epoch;
	uint32		global_dirty_writers;
	/* Next dynahash bucket for the bounded eviction sample. */
	uint32		eviction_bucket_cursor;
	pg_atomic_uint64 config_generation;
	/* Fences backend-local SQL row copies across committed invalidations. */
	pg_atomic_uint64 data_epoch;
	pg_atomic_uint64 cache_hits;
	pg_atomic_uint64 cache_misses;
	pg_atomic_uint64 negative_hits;
	pg_atomic_uint64 negative_writes;
	/* Compatibility/fallback totals for callers without a backend slot. */
	pg_atomic_uint64 sql_cache_hits;
	pg_atomic_uint64 sql_cache_misses;
	pg_atomic_uint64 sql_cache_fills;
	pg_atomic_uint64 sql_cache_bypasses;
	pg_atomic_uint64 database_reads;
	pg_atomic_uint64 database_writes;
	pg_atomic_uint64 invalidations;
	pg_atomic_uint64 key_invalidations;
	pg_atomic_uint64 table_invalidations;
	pg_atomic_uint64 evictions;
	pg_atomic_uint64 singleflight_leaders;
	pg_atomic_uint64 singleflight_waiters;
	pg_atomic_uint64 singleflight_reuses;
	pg_atomic_uint64 singleflight_timeouts;
	pg_atomic_uint64 active_clients;
	pg_atomic_uint64 peak_active_clients;
	pg_atomic_uint64 rejected_connections;
	pg_atomic_uint64 client_limit_rejections;
	pg_atomic_uint64 authentication_failures;
	pg_atomic_uint64 protocol_errors;
	pg_atomic_uint64 output_backpressure_events;
	pg_atomic_uint64 slow_client_drops;
	pg_atomic_uint64 worker_starts;
	pg_atomic_uint64 active_workers;
	/* Generation fully loaded by each statically registered RESP worker. */
	pg_atomic_uint64 worker_mapping_generations[PGLC_MAX_WORKERS];
	pg_atomic_uint64 cache_admission_rejections;
	pg_atomic_uint64 relation_state_admission_rejections;
	pg_atomic_uint64 dirty_key_limit_fallbacks;
	pg_atomic_uint64 mapping_reload_attempts;
	pg_atomic_uint64 mapping_reload_failures;
	pg_atomic_uint64 mapping_reload_incomplete_retries;
	pg_atomic_uint64 client_connects;
	pg_atomic_uint64 client_disconnects;
	pg_atomic_uint64 client_requests;
	pg_atomic_uint64 client_request_errors;
	pg_atomic_uint64 client_gets;
	pg_atomic_uint64 client_sets;
	pg_atomic_uint64 client_dels;
	pg_atomic_uint64 pass_to_main;
	pg_atomic_uint64 sql_sets;
	pg_atomic_uint64 sql_dels;
} PgLocalCacheSharedState;

typedef struct PgLocalCacheReadToken
{
	uint64		config_generation;
	uint64		global_version;
	uint64		relation_version;
	uint64		key_version;
	uint64		source_observed_full_xid;
	uint64		data_epoch;
	bool		cacheable;
	bool		has_entry;
} PgLocalCacheReadToken;

typedef enum PgLocalCacheLoadClaim
{
	PGLC_LOAD_BYPASS = 0,
	PGLC_LOAD_OWNER,
	PGLC_LOAD_WAIT,
	PGLC_LOAD_RETRY
} PgLocalCacheLoadClaim;

typedef struct PgLocalCacheMapping
{
	char		nspace[PGLC_NAMESPACE_MAX];
	char		schema_name[NAMEDATALEN];
	char		relation_name[NAMEDATALEN];
	char		key_columns[PGLC_MAX_KEY_COLUMNS][NAMEDATALEN];
	Oid			relation_oid;
	int			key_count;
	AttrNumber	key_attnos[PGLC_MAX_KEY_COLUMNS];
	Oid			key_types[PGLC_MAX_KEY_COLUMNS];
	Oid			key_ioparams[PGLC_MAX_KEY_COLUMNS];
	int32		key_typmods[PGLC_MAX_KEY_COLUMNS];
	Oid			row_type_oid;
	int32		row_typmod;
	int			row_natts;
	uint64		row_descriptor_fingerprint;
	uint64		config_generation;
	bool		writable;
	FmgrInfo	key_inputs[PGLC_MAX_KEY_COLUMNS];
	FmgrInfo	key_outputs[PGLC_MAX_KEY_COLUMNS];
	TupleDesc	row_desc;
	SPIPlanPtr	get_plan;
	SPIPlanPtr	set_plan;
	SPIPlanPtr	delete_plan;
} PgLocalCacheMapping;

extern int	pglc_port;
extern int	pglc_worker_count;
extern int	pglc_cache_entries;
extern int	pglc_relation_states;
extern int	pglc_max_clients;
extern int	pglc_max_clients_per_worker;
extern int	pglc_memory_budget_mb;
extern int	pglc_idle_timeout_ms;
extern int	pglc_statement_timeout_ms;
extern int	pglc_lock_timeout_ms;
extern int	pglc_singleflight_wait_ms;
extern int	pglc_max_pipeline_commands;
extern int	pglc_max_dirty_keys;
extern char *pglc_bind_address;
extern char *pglc_database;
extern char *pglc_role;
extern char *pglc_auth_token;
extern char *pglc_auth_token_file;
extern bool pglc_allow_superuser;
extern bool pglc_sql_cache;

extern PgLocalCacheSharedState *pglc_shared;
extern HTAB *pglc_cache_hash;
extern HTAB *pglc_relation_hash;

extern void pglc_require_preload(void);
extern uint64 pglc_config_generation(void);
extern uint64 pglc_data_epoch(void);
extern bool pglc_cache_lookup(const PgLocalCacheMapping *mapping,
							 const char *canonical_key,
							 char *value,
							 Size value_capacity,
							 Size *value_len,
							 bool *negative,
							 TransactionId *source_xmin,
							 PgLocalCacheReadToken *token);
extern bool pglc_cache_lookup_quiet(const PgLocalCacheMapping *mapping,
								   const char *canonical_key,
								   char *value,
								   Size value_capacity,
								   Size *value_len,
								   bool *negative,
								   TransactionId *source_xmin,
									   PgLocalCacheReadToken *token);
extern bool pglc_cache_retire_positive(
	const PgLocalCacheMapping *mapping, const char *canonical_key,
	const PgLocalCacheReadToken *token, TransactionId expected_xmin);
extern bool pglc_cache_store(const PgLocalCacheMapping *mapping,
							const char *canonical_key,
							const PgLocalCacheReadToken *token,
							const char *value,
							Size value_len,
							bool negative,
							uint64 load_id,
							TransactionId source_xmin);
extern PgLocalCacheLoadClaim pglc_cache_claim_load(
	const PgLocalCacheMapping *mapping, const char *canonical_key,
	const PgLocalCacheReadToken *token, uint64 *load_id);
extern void pglc_cache_release_load(const PgLocalCacheMapping *mapping,
									const char *canonical_key,
									const PgLocalCacheReadToken *claim_token,
									uint64 load_id);
extern void pglc_note_singleflight_waiter(void);
extern void pglc_note_singleflight_reuse(void);
extern void pglc_note_singleflight_timeout(void);
extern void pglc_note_sql_cache_hit(void);
extern void pglc_note_sql_cache_hits(uint64 count);
extern void pglc_note_sql_cache_miss(void);
extern void pglc_note_sql_cache_fill(void);
extern void pglc_note_sql_cache_bypass(void);
extern bool pglc_current_transaction_is_dirty(void);
extern uint64 pglc_cache_invalidate_namespace(Oid database_oid,
											 const char *nspace);
extern uint64 pglc_cache_invalidate_key(const PgLocalCacheMapping *mapping,
										const char *canonical_key);
extern uint64 pglc_cache_invalidate_database(Oid database_oid);
extern uint64 pglc_cache_invalidate_all(void);
extern char *pglc_stats_json(void);
extern char *pglc_metrics_json(void);
extern void pglc_note_database_read(void);
extern void pglc_note_database_write(void);
extern bool pglc_try_reserve_client(void);
extern void pglc_release_clients(uint64 count);
extern void pglc_note_client_limit_rejection(void);
extern void pglc_note_worker_start(void);
extern void pglc_note_worker_stop(void);
extern Size pglc_shared_memory_bytes(void);
extern Size pglc_sql_counter_memory_bytes(void);
extern Size pglc_worker_memory_bytes(void);
extern Size pglc_worker_memory_bytes_per_worker(void);
extern Size pglc_estimated_memory_bytes(void);
extern void pglc_sql_init(void);
extern void pg_local_cache_worker_main(Datum main_arg);

#endif
