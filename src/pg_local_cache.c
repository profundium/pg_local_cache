#include "postgres.h"

#include <limits.h>

#include "access/htup_details.h"
#include "access/xact.h"
#include "catalog/pg_trigger.h"
#include "catalog/pg_type_d.h"
#include "commands/trigger.h"
#include "common/hashfn.h"
#include "executor/spi.h"
#include "funcapi.h"
#include "lib/stringinfo.h"
#include "miscadmin.h"
#include "postmaster/bgworker.h"
#include "storage/ipc.h"
#include "storage/lmgr.h"
#include "storage/proc.h"
#include "storage/shmem.h"
#include "utils/acl.h"
#include "utils/builtins.h"
#include "utils/guc.h"
#include "utils/hsearch.h"
#include "utils/jsonb.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/rel.h"
#include "utils/timestamp.h"

#include "key_codec.h"
#include "pg_local_cache.h"

PG_MODULE_MAGIC;

int			pglc_port = 6380;
int			pglc_worker_count = 4;
int			pglc_cache_entries = 16384;
int			pglc_relation_states = 1024;
int			pglc_max_clients = 256;
int			pglc_max_clients_per_worker = 64;
int			pglc_memory_budget_mb = 384;
int			pglc_idle_timeout_ms = 300000;
int			pglc_statement_timeout_ms = 2000;
int			pglc_lock_timeout_ms = 250;
int			pglc_singleflight_wait_ms = 25;
int			pglc_max_pipeline_commands = 256;
int			pglc_max_dirty_keys = 4096;
char	   *pglc_bind_address = NULL;
char	   *pglc_database = NULL;
char	   *pglc_role = NULL;
char	   *pglc_auth_token = NULL;
char	   *pglc_auth_token_file = NULL;
bool		pglc_allow_superuser = false;

PgLocalCacheSharedState *pglc_shared = NULL;
HTAB	   *pglc_cache_hash = NULL;
HTAB	   *pglc_relation_hash = NULL;

static PgLocalCacheSqlCounterSlot *pglc_sql_counter_slots = NULL;
static PgLocalCacheSqlCounterSlot *pglc_my_sql_counter_slot = NULL;
static int	pglc_sql_counter_slot_count = 0;
static char *pglc_binary_version = NULL;
static char *pglc_binary_build_id = NULL;

#if PG_VERSION_NUM >= 150000
static shmem_request_hook_type previous_shmem_request_hook = NULL;
#endif
static shmem_startup_hook_type previous_shmem_startup_hook = NULL;
static bool pglc_was_preloaded = false;

typedef enum PgLocalCacheDirtyKind
{
	PGLC_DIRTY_KEY = 1,
	PGLC_DIRTY_RELATION = 2,
	PGLC_DIRTY_GLOBAL = 3,
	PGLC_DIRTY_FORGET_RELATION = 4
} PgLocalCacheDirtyKind;

typedef struct PgLocalCacheLocalDirtyKey
{
	uint8		kind;
	Oid			database_oid;
	char		nspace[PGLC_NAMESPACE_MAX];
	char		key[PGLC_KEY_MAX];
} PgLocalCacheLocalDirtyKey;

typedef struct PgLocalCacheLocalDirtyEntry
{
	PgLocalCacheLocalDirtyKey key;
	Oid			relation_oid;
	bool		shared_marker_reserved;
} PgLocalCacheLocalDirtyEntry;

static HTAB *local_dirty_hash = NULL;
static bool local_dirty_published = false;
static bool local_global_fallback = false;
static bool local_bump_config = false;

void		_PG_init(void);

PG_FUNCTION_INFO_V1(pg_local_cache_row_invalidate);
PG_FUNCTION_INFO_V1(pg_local_cache_truncate_invalidate);
PG_FUNCTION_INFO_V1(pg_local_cache_statement_guard);
PG_FUNCTION_INFO_V1(pg_local_cache_lock_relation);
PG_FUNCTION_INFO_V1(pg_local_cache_reload);
PG_FUNCTION_INFO_V1(pg_local_cache_invalidate);
PG_FUNCTION_INFO_V1(pg_local_cache_stats);
PG_FUNCTION_INFO_V1(pg_local_cache_metrics_json);
PG_FUNCTION_INFO_V1(pg_local_cache_forget);

static void pglc_shmem_request(void);
static void pglc_shmem_startup(void);
static void pglc_validate_startup_limits(void);
static void pglc_xact_callback(XactEvent event, void *arg);
static void pglc_backend_exit(int code, Datum arg);
static void pglc_publish_dirty(void);
static void pglc_finish_dirty(bool committed);
static void pglc_collect_key(Oid database_oid, Oid relation_oid,
							const char *nspace, const char *key);
static void pglc_collect_relation(Oid database_oid, Oid relation_oid,
								 const char *nspace);
static void pglc_collect_forget_relation(Oid database_oid, Oid relation_oid,
										const char *nspace);
static void pglc_collect_global(bool bump_config);
static bool pglc_mapping_exists(const char *nspace);
static uint64 pglc_workers_without_current_mappings(void);
static uint32 pglc_cache_key_hash(const void *key, Size keysize);
static int pglc_cache_key_match(const void *left, const void *right,
								Size keysize);
static PgLocalCacheSqlCounterSlot *pglc_current_sql_counter_slot(void);
static inline void pglc_increment_owned_sql_counter(
	pg_atomic_uint64 *counter);

typedef struct PgLocalCacheSqlCounterSnapshot
{
	uint64		hits;
	uint64		misses;
	uint64		fills;
	uint64		bypasses;
} PgLocalCacheSqlCounterSnapshot;

static void pglc_read_sql_counter_snapshot(
	PgLocalCacheSqlCounterSnapshot *snapshot);

static void
pglc_define_gucs(void)
{
	DefineCustomStringVariable("pg_local_cache.binary_version",
							   "Version compiled into the active pg_local_cache library.",
							   NULL,
							   &pglc_binary_version,
							   PGLC_VERSION,
							   PGC_INTERNAL,
							   GUC_NOT_IN_SAMPLE | GUC_DISALLOW_IN_FILE,
							   NULL,
							   NULL,
							   NULL);

	DefineCustomStringVariable("pg_local_cache.binary_build_id",
							   "Build identifier compiled into the active pg_local_cache library.",
							   NULL,
							   &pglc_binary_build_id,
							   PGLC_BUILD_ID,
							   PGC_INTERNAL,
							   GUC_NOT_IN_SAMPLE | GUC_DISALLOW_IN_FILE,
							   NULL,
							   NULL,
							   NULL);

	DefineCustomIntVariable("pg_local_cache.port",
							"TCP port for the RESP2 listener; 0 disables it.",
							NULL,
							&pglc_port,
							6380,
							0,
							65535,
							PGC_POSTMASTER,
							0,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_local_cache.workers",
							"Number of RESP background workers.",
							NULL,
							&pglc_worker_count,
							4,
							1,
							PGLC_MAX_WORKERS,
							PGC_POSTMASTER,
							0,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_local_cache.cache_entries",
							"Maximum number of shared row-cache entries.",
							NULL,
							&pglc_cache_entries,
							16384,
							128,
							65536,
							PGC_POSTMASTER,
							0,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_local_cache.relation_states",
							"Maximum number of shared namespace relation states.",
							NULL,
							&pglc_relation_states,
							1024,
							128,
							8192,
							PGC_POSTMASTER,
							0,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_local_cache.max_clients",
							"Global maximum number of concurrent RESP clients.",
							NULL,
							&pglc_max_clients,
							256,
							1,
							4096,
							PGC_POSTMASTER,
							0,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_local_cache.max_clients_per_worker",
							"Preallocated RESP client slots in each worker.",
							NULL,
							&pglc_max_clients_per_worker,
							64,
							1,
							PGLC_MAX_CLIENTS_PER_WORKER,
							PGC_POSTMASTER,
							0,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_local_cache.memory_budget_mb",
							"Hard startup budget for deterministic pg_local_cache shared memory and RESP buffers.",
							NULL,
							&pglc_memory_budget_mb,
							384,
							64,
							8192,
							PGC_POSTMASTER,
							GUC_UNIT_MB,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_local_cache.idle_timeout_ms",
							"Close idle RESP clients after this interval.",
							NULL,
							&pglc_idle_timeout_ms,
							300000,
							1000,
							86400000,
							PGC_POSTMASTER,
							GUC_UNIT_MS,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_local_cache.statement_timeout_ms",
							"Maximum duration of a database operation issued by a RESP worker.",
							NULL,
							&pglc_statement_timeout_ms,
							2000,
							100,
							60000,
							PGC_POSTMASTER,
							GUC_UNIT_MS,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_local_cache.lock_timeout_ms",
							"Maximum lock wait for a database operation issued by a RESP worker.",
							NULL,
							&pglc_lock_timeout_ms,
							250,
							10,
							60000,
							PGC_POSTMASTER,
							GUC_UNIT_MS,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_local_cache.singleflight_wait_ms",
							"Maximum time a RESP MGET waits for another worker loading the same key.",
							NULL,
							&pglc_singleflight_wait_ms,
							25,
							0,
							1000,
							PGC_POSTMASTER,
							GUC_UNIT_MS,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_local_cache.max_pipeline_commands",
							"Maximum RESP commands processed for one client per event-loop turn.",
							NULL,
							&pglc_max_pipeline_commands,
							256,
							1,
							4096,
							PGC_POSTMASTER,
							0,
							NULL,
							NULL,
							NULL);

	DefineCustomIntVariable("pg_local_cache.max_dirty_keys",
							"Maximum per-key invalidations collected by one transaction before falling back to relation invalidation.",
							NULL,
							&pglc_max_dirty_keys,
							4096,
							128,
							16384,
							PGC_POSTMASTER,
							0,
							NULL,
							NULL,
							NULL);

	DefineCustomStringVariable("pg_local_cache.bind_address",
							   "IPv4 address for the RESP2 listener.",
							   NULL,
							   &pglc_bind_address,
							   "127.0.0.1",
							   PGC_POSTMASTER,
							   0,
							   NULL,
							   NULL,
							   NULL);

	DefineCustomStringVariable("pg_local_cache.database",
							   "Database served by this pg_local_cache instance.",
							   NULL,
							   &pglc_database,
							   "postgres",
							   PGC_POSTMASTER,
							   0,
							   NULL,
							   NULL,
							   NULL);

	DefineCustomStringVariable("pg_local_cache.role",
							   "Dedicated LOGIN role used by RESP workers.",
							   NULL,
							   &pglc_role,
							   "local_cache_worker",
							   PGC_POSTMASTER,
							   GUC_SUPERUSER_ONLY,
							   NULL,
							   NULL,
							   NULL);

	DefineCustomStringVariable("pg_local_cache.auth_token",
							   "Inline RESP AUTH token for development; prefer auth_token_file.",
							   NULL,
							   &pglc_auth_token,
							   "",
							   PGC_POSTMASTER,
							   GUC_SUPERUSER_ONLY | GUC_NO_SHOW_ALL,
							   NULL,
							   NULL,
							   NULL);

	DefineCustomStringVariable("pg_local_cache.auth_token_file",
							   "PostgreSQL OS-user-owned mode-0400/0600 file containing the RESP AUTH token.",
							   NULL,
							   &pglc_auth_token_file,
							   "",
							   PGC_POSTMASTER,
							   GUC_SUPERUSER_ONLY | GUC_NO_SHOW_ALL,
							   NULL,
							   NULL,
							   NULL);

	DefineCustomBoolVariable("pg_local_cache.allow_superuser",
							 "Allow RESP workers to run as a superuser (development only).",
							 NULL,
							 &pglc_allow_superuser,
							 false,
							 PGC_POSTMASTER,
							 GUC_SUPERUSER_ONLY,
							 NULL,
							 NULL,
							 NULL);

#if PG_VERSION_NUM >= 150000
	MarkGUCPrefixReserved("pg_local_cache");
#else
	EmitWarningsOnPlaceholders("pg_local_cache");
#endif
}

void
_PG_init(void)
{
	BackgroundWorker worker;
	int			i;

	pglc_define_gucs();
	RegisterXactCallback(pglc_xact_callback, NULL);
	before_shmem_exit(pglc_backend_exit, (Datum) 0);

	if (!process_shared_preload_libraries_in_progress)
		return;

	pglc_was_preloaded = true;
	pglc_validate_startup_limits();

#if PG_VERSION_NUM >= 150000
	previous_shmem_request_hook = shmem_request_hook;
	shmem_request_hook = pglc_shmem_request;
#else
	pglc_shmem_request();
#endif
	previous_shmem_startup_hook = shmem_startup_hook;
	shmem_startup_hook = pglc_shmem_startup;

	if (pglc_port == 0)
		return;

	for (i = 0; i < pglc_worker_count; i++)
	{
		memset(&worker, 0, sizeof(worker));
		snprintf(worker.bgw_name, BGW_MAXLEN, "pg_local_cache RESP worker %d", i);
		strlcpy(worker.bgw_type, "pg_local_cache RESP worker", BGW_MAXLEN);
		worker.bgw_flags = BGWORKER_SHMEM_ACCESS |
			BGWORKER_BACKEND_DATABASE_CONNECTION;
		worker.bgw_start_time = BgWorkerStart_RecoveryFinished;
		worker.bgw_restart_time = 1;
		strlcpy(worker.bgw_library_name, "pg_local_cache", BGW_MAXLEN);
		strlcpy(worker.bgw_function_name, "pg_local_cache_worker_main", BGW_MAXLEN);
		worker.bgw_main_arg = Int32GetDatum(i);
		worker.bgw_notify_pid = 0;
		RegisterBackgroundWorker(&worker);
	}
}

Size
pglc_sql_counter_memory_bytes(void)
{
	Size		array_bytes;

	array_bytes = mul_size((Size) MaxBackends,
						   sizeof(PgLocalCacheSqlCounterSlot));
	/* ShmemInitStruct guarantees MAXALIGN, not cache-line alignment. */
	return MAXALIGN(add_size(array_bytes, PG_CACHE_LINE_SIZE - 1));
}

Size
pglc_shared_memory_bytes(void)
{
	Size		size = MAXALIGN(sizeof(PgLocalCacheSharedState));

	size = add_size(size, pglc_sql_counter_memory_bytes());
	size = add_size(size,
					hash_estimate_size(pglc_cache_entries,
									   sizeof(PgLocalCacheCacheEntry)));
	size = add_size(size,
					hash_estimate_size(pglc_relation_states,
									   sizeof(PgLocalCacheRelationState)));
	return size;
}

Size
pglc_estimated_memory_bytes(void)
{
	return add_size(pglc_shared_memory_bytes(), pglc_worker_memory_bytes());
}

static void
pglc_validate_startup_limits(void)
{
	Size		budget_bytes;
	Size		estimated_bytes;
	uint64		client_slots;

	if (pglc_port != 0)
	{
		client_slots = (uint64) pglc_worker_count *
			(uint64) pglc_max_clients_per_worker;
		if ((uint64) pglc_max_clients > client_slots)
			ereport(FATAL,
					(errmsg("pg_local_cache.max_clients exceeds allocated RESP client slots"),
					 errdetail("max_clients is %d, but %d workers x %d slots provides " UINT64_FORMAT " slots.",
							   pglc_max_clients, pglc_worker_count,
							   pglc_max_clients_per_worker, client_slots),
					 errhint("Increase pg_local_cache.workers or pg_local_cache.max_clients_per_worker, or lower pg_local_cache.max_clients.")));
	}

	budget_bytes = mul_size((Size) pglc_memory_budget_mb,
							(Size) 1024 * 1024);
	estimated_bytes = pglc_estimated_memory_bytes();
	if (estimated_bytes > budget_bytes)
			ereport(FATAL,
					(errmsg("pg_local_cache estimated memory exceeds its configured budget"),
					 errdetail("Estimated deterministic extension memory is %zu bytes; pg_local_cache.memory_budget_mb allows %zu bytes. SQL counters reserve %zu bytes for %d PostgreSQL backend slots.",
							   estimated_bytes, budget_bytes,
							   pglc_sql_counter_memory_bytes(), MaxBackends),
					 errhint("Raise pg_local_cache.memory_budget_mb; lower cache_entries, relation_states, workers, or max_clients_per_worker; or lower PostgreSQL backend limits.")));
}

static void
pglc_shmem_request(void)
{
#if PG_VERSION_NUM >= 150000
	if (previous_shmem_request_hook)
		previous_shmem_request_hook();
#endif

	RequestAddinShmemSpace(pglc_shared_memory_bytes());
	RequestNamedLWLockTranche("pg_local_cache", 1);
}

static void
pglc_shmem_startup(void)
{
	bool		found;
	bool		counter_slots_found;
	HASHCTL		control;
	int		worker_index;
	int		counter_slot_index;
	void	   *counter_slots_raw;

	if (previous_shmem_startup_hook)
		previous_shmem_startup_hook();

	LWLockAcquire(AddinShmemInitLock, LW_EXCLUSIVE);

	pglc_shared = ShmemInitStruct("pg_local_cache shared state",
								 sizeof(PgLocalCacheSharedState),
								 &found);
	counter_slots_raw = ShmemInitStruct(
		"pg_local_cache SQL counter slots",
		pglc_sql_counter_memory_bytes(), &counter_slots_found);
	pglc_sql_counter_slots = (PgLocalCacheSqlCounterSlot *) TYPEALIGN(
		PG_CACHE_LINE_SIZE, counter_slots_raw);
	pglc_sql_counter_slot_count = MaxBackends;
	if (found != counter_slots_found)
		elog(PANIC, "pg_local_cache shared state is inconsistent");
	if (!counter_slots_found)
	{
		for (counter_slot_index = 0;
			 counter_slot_index < pglc_sql_counter_slot_count;
			 counter_slot_index++)
		{
			PgLocalCacheSqlCounterSlot *slot =
				&pglc_sql_counter_slots[counter_slot_index];

			MemSet(slot, 0, sizeof(*slot));
			pg_atomic_init_u64(&slot->counters.hits, 0);
			pg_atomic_init_u64(&slot->counters.misses, 0);
			pg_atomic_init_u64(&slot->counters.fills, 0);
			pg_atomic_init_u64(&slot->counters.bypasses, 0);
		}
	}
	if (!found)
	{
		memset(pglc_shared, 0, sizeof(PgLocalCacheSharedState));
		pglc_shared->lock = &(GetNamedLWLockTranche("pg_local_cache"))->lock;
		pg_atomic_init_u64(&pglc_shared->clock, 0);
		pg_atomic_init_u64(&pglc_shared->entry_generation, 0);
		pg_atomic_init_u64(&pglc_shared->config_generation, 1);
		pg_atomic_init_u64(&pglc_shared->cache_hits, 0);
		pg_atomic_init_u64(&pglc_shared->cache_misses, 0);
		pg_atomic_init_u64(&pglc_shared->negative_hits, 0);
		pg_atomic_init_u64(&pglc_shared->negative_writes, 0);
		pg_atomic_init_u64(&pglc_shared->sql_cache_hits, 0);
		pg_atomic_init_u64(&pglc_shared->sql_cache_misses, 0);
		pg_atomic_init_u64(&pglc_shared->sql_cache_fills, 0);
		pg_atomic_init_u64(&pglc_shared->sql_cache_bypasses, 0);
		pg_atomic_init_u64(&pglc_shared->database_reads, 0);
		pg_atomic_init_u64(&pglc_shared->database_writes, 0);
		pg_atomic_init_u64(&pglc_shared->invalidations, 0);
		pg_atomic_init_u64(&pglc_shared->key_invalidations, 0);
		pg_atomic_init_u64(&pglc_shared->table_invalidations, 0);
		pg_atomic_init_u64(&pglc_shared->evictions, 0);
		pg_atomic_init_u64(&pglc_shared->singleflight_leaders, 0);
		pg_atomic_init_u64(&pglc_shared->singleflight_waiters, 0);
		pg_atomic_init_u64(&pglc_shared->singleflight_reuses, 0);
		pg_atomic_init_u64(&pglc_shared->singleflight_timeouts, 0);
		pg_atomic_init_u64(&pglc_shared->active_clients, 0);
		pg_atomic_init_u64(&pglc_shared->peak_active_clients, 0);
		pg_atomic_init_u64(&pglc_shared->rejected_connections, 0);
		pg_atomic_init_u64(&pglc_shared->client_limit_rejections, 0);
		pg_atomic_init_u64(&pglc_shared->authentication_failures, 0);
		pg_atomic_init_u64(&pglc_shared->protocol_errors, 0);
		pg_atomic_init_u64(&pglc_shared->output_backpressure_events, 0);
		pg_atomic_init_u64(&pglc_shared->slow_client_drops, 0);
		pg_atomic_init_u64(&pglc_shared->worker_starts, 0);
		pg_atomic_init_u64(&pglc_shared->active_workers, 0);
		for (worker_index = 0; worker_index < PGLC_MAX_WORKERS;
			 worker_index++)
			pg_atomic_init_u64(
				&pglc_shared->worker_mapping_generations[worker_index], 0);
		pg_atomic_init_u64(&pglc_shared->cache_admission_rejections, 0);
		pg_atomic_init_u64(&pglc_shared->relation_state_admission_rejections, 0);
		pg_atomic_init_u64(&pglc_shared->dirty_key_limit_fallbacks, 0);
		pg_atomic_init_u64(&pglc_shared->mapping_reload_attempts, 0);
		pg_atomic_init_u64(&pglc_shared->mapping_reload_failures, 0);
		pg_atomic_init_u64(&pglc_shared->mapping_reload_incomplete_retries, 0);
		pg_atomic_init_u64(&pglc_shared->client_connects, 0);
		pg_atomic_init_u64(&pglc_shared->client_disconnects, 0);
		pg_atomic_init_u64(&pglc_shared->client_requests, 0);
		pg_atomic_init_u64(&pglc_shared->client_request_errors, 0);
		pg_atomic_init_u64(&pglc_shared->client_mget_keys, 0);
		pg_atomic_init_u64(&pglc_shared->client_sets, 0);
		pg_atomic_init_u64(&pglc_shared->client_dels, 0);
		pg_atomic_init_u64(&pglc_shared->pass_to_main, 0);
		pg_atomic_init_u64(&pglc_shared->sql_sets, 0);
		pg_atomic_init_u64(&pglc_shared->sql_dels, 0);
	}

	memset(&control, 0, sizeof(control));
	control.keysize = sizeof(PgLocalCacheCacheKey);
	control.entrysize = sizeof(PgLocalCacheCacheEntry);
	control.hash = pglc_cache_key_hash;
	control.match = pglc_cache_key_match;
	pglc_cache_hash = ShmemInitHash("pg_local_cache cache",
								   pglc_cache_entries,
								   pglc_cache_entries,
								   &control,
								   HASH_ELEM | HASH_FUNCTION | HASH_COMPARE);

	memset(&control, 0, sizeof(control));
	control.keysize = sizeof(PgLocalCacheRelationKey);
	control.entrysize = sizeof(PgLocalCacheRelationState);
	pglc_relation_hash = ShmemInitHash("pg_local_cache relation state",
									  pglc_relation_states,
									  pglc_relation_states,
									  &control,
									  HASH_ELEM | HASH_BLOBS);

	LWLockRelease(AddinShmemInitLock);
}

/*
 * PostgreSQL 14-16 call this identifier pgprocno; PostgreSQL 17+ exposes
 * ProcNumber as vxid.procNumber. It is stable for the lifetime of a backend
 * and unique among live backends.
 * Slots are never reset on process reuse, so aggregation cannot lose counts.
 */
static PgLocalCacheSqlCounterSlot *
pglc_current_sql_counter_slot(void)
{
	int			proc_number;

	if (pglc_my_sql_counter_slot != NULL)
		return pglc_my_sql_counter_slot;
	if (pglc_sql_counter_slots == NULL || MyProc == NULL)
		return NULL;

#if PG_VERSION_NUM >= 170000
	proc_number = MyProc->vxid.procNumber;
#else
	proc_number = MyProc->pgprocno;
#endif
	if (proc_number < 0 || proc_number >= pglc_sql_counter_slot_count)
		return NULL;

	pglc_my_sql_counter_slot = &pglc_sql_counter_slots[proc_number];
	return pglc_my_sql_counter_slot;
}

/* A live ProcNumber has one writer, while scrapers only read the value. */
static inline void
pglc_increment_owned_sql_counter(pg_atomic_uint64 *counter)
{
	pg_atomic_write_u64(counter, pg_atomic_read_u64(counter) + 1);
}

void
pglc_note_sql_cache_hits(uint64 count)
{
	PgLocalCacheSqlCounterSlot *slot = pglc_current_sql_counter_slot();

	if (count == 0)
		return;
	if (slot != NULL)
		pg_atomic_write_u64(&slot->counters.hits,
			pg_atomic_read_u64(&slot->counters.hits) + count);
	else if (pglc_shared != NULL)
		pg_atomic_fetch_add_u64(&pglc_shared->sql_cache_hits, count);
}

void
pglc_note_sql_cache_hit(void)
{
	pglc_note_sql_cache_hits(1);
}

void
pglc_note_sql_cache_miss(void)
{
	PgLocalCacheSqlCounterSlot *slot = pglc_current_sql_counter_slot();

	if (slot != NULL)
		pglc_increment_owned_sql_counter(&slot->counters.misses);
	else if (pglc_shared != NULL)
		pg_atomic_fetch_add_u64(&pglc_shared->sql_cache_misses, 1);
}

void
pglc_note_sql_cache_fill(void)
{
	PgLocalCacheSqlCounterSlot *slot = pglc_current_sql_counter_slot();

	if (slot != NULL)
		pglc_increment_owned_sql_counter(&slot->counters.fills);
	else if (pglc_shared != NULL)
		pg_atomic_fetch_add_u64(&pglc_shared->sql_cache_fills, 1);
}

void
pglc_note_sql_cache_bypass(void)
{
	PgLocalCacheSqlCounterSlot *slot = pglc_current_sql_counter_slot();

	if (slot != NULL)
		pglc_increment_owned_sql_counter(&slot->counters.bypasses);
	else if (pglc_shared != NULL)
		pg_atomic_fetch_add_u64(&pglc_shared->sql_cache_bypasses, 1);
}

static void
pglc_read_sql_counter_snapshot(PgLocalCacheSqlCounterSnapshot *snapshot)
{
	int			counter_slot_index;

	MemSet(snapshot, 0, sizeof(*snapshot));
	/* Keep counters written by older callers and non-backend processes. */
	snapshot->hits = pg_atomic_read_u64(&pglc_shared->sql_cache_hits);
	snapshot->misses = pg_atomic_read_u64(&pglc_shared->sql_cache_misses);
	snapshot->fills = pg_atomic_read_u64(&pglc_shared->sql_cache_fills);
	snapshot->bypasses = pg_atomic_read_u64(
		&pglc_shared->sql_cache_bypasses);

	for (counter_slot_index = 0;
		 counter_slot_index < pglc_sql_counter_slot_count;
		 counter_slot_index++)
	{
		PgLocalCacheSqlCounterSlot *slot =
			&pglc_sql_counter_slots[counter_slot_index];

		snapshot->hits += pg_atomic_read_u64(&slot->counters.hits);
		snapshot->misses += pg_atomic_read_u64(&slot->counters.misses);
		snapshot->fills += pg_atomic_read_u64(&slot->counters.fills);
		snapshot->bypasses += pg_atomic_read_u64(&slot->counters.bypasses);
	}
}

void
pglc_require_preload(void)
{
	if (!pglc_was_preloaded || pglc_shared == NULL ||
		pglc_cache_hash == NULL || pglc_relation_hash == NULL)
		ereport(ERROR,
				(errcode(ERRCODE_OBJECT_NOT_IN_PREREQUISITE_STATE),
				 errmsg("pg_local_cache must be loaded through shared_preload_libraries")));
}

uint64
pglc_config_generation(void)
{
	pglc_require_preload();
	return pg_atomic_read_u64(&pglc_shared->config_generation);
}

/*
 * Cache keys reserve room for the largest supported namespace and encoded
 * primary key.  Hashing the entire fixed-size struct would process more than
 * a kilobyte for every lookup even when the key itself is only a few bytes.
 * Hash and compare only the initialized fields; dynahash still verifies the
 * complete logical key, so hash collisions cannot alias entries.
 */
static uint32
pglc_cache_key_hash(const void *key, Size keysize)
{
	const PgLocalCacheCacheKey *cache_key =
		(const PgLocalCacheCacheKey *) key;
	Size		namespace_len;
	Size		key_len;
	uint64		hash;

	namespace_len = strnlen(cache_key->nspace, sizeof(cache_key->nspace));
	key_len = strnlen(cache_key->key, sizeof(cache_key->key));
	hash = hash_bytes_extended((const unsigned char *) &cache_key->database_oid,
								 sizeof(cache_key->database_oid), 0);
	hash = hash_bytes_extended((const unsigned char *) cache_key->nspace,
								 namespace_len, hash);
	hash = hash_bytes_extended((const unsigned char *) cache_key->key,
								 key_len, hash);
	(void) keysize;
	return (uint32) (hash ^ (hash >> 32));
}

static int
pglc_cache_key_match(const void *left, const void *right, Size keysize)
{
	const PgLocalCacheCacheKey *left_key =
		(const PgLocalCacheCacheKey *) left;
	const PgLocalCacheCacheKey *right_key =
		(const PgLocalCacheCacheKey *) right;

	(void) keysize;
	if (left_key->database_oid != right_key->database_oid)
		return 1;
	if (strncmp(left_key->nspace, right_key->nspace,
				 sizeof(left_key->nspace)) != 0)
		return 1;
	return strncmp(left_key->key, right_key->key,
				   sizeof(left_key->key));
}

static void
make_cache_key(PgLocalCacheCacheKey *result, Oid database_oid,
				   const char *nspace, const char *key, bool initialize_padding)
{
	/* New shared entries must never retain uninitialized backend memory. */
	if (initialize_padding)
		memset(result, 0, sizeof(*result));
	result->database_oid = database_oid;
	strlcpy(result->nspace, nspace, sizeof(result->nspace));
	strlcpy(result->key, key, sizeof(result->key));
}

static void
make_relation_key(PgLocalCacheRelationKey *result, Oid database_oid,
				  const char *nspace)
{
	memset(result, 0, sizeof(*result));
	result->database_oid = database_oid;
	strlcpy(result->nspace, nspace, sizeof(result->nspace));
}

static PgLocalCacheRelationState *
get_relation_state(Oid database_oid, Oid relation_oid,
				   const char *nspace, bool create)
{
	PgLocalCacheRelationKey key;
	PgLocalCacheRelationState *state;
	bool		found;

	make_relation_key(&key, database_oid, nspace);
	state = hash_search(pglc_relation_hash, &key, HASH_FIND, NULL);
	found = state != NULL;
	if (state == NULL && create)
	{
		/*
		 * dynahash's max_size is a sizing hint unless callers enforce it.
		 * Keep the configured memory estimate true at runtime by refusing a
		 * new namespace before HASH_ENTER can grow the shared hash.
		 */
		if (hash_get_num_entries(pglc_relation_hash) >=
			(uint64) pglc_relation_states)
		{
			pg_atomic_fetch_add_u64(
				&pglc_shared->relation_state_admission_rejections, 1);
			return NULL;
		}
		state = hash_search(pglc_relation_hash, &key, HASH_ENTER_NULL,
							&found);
		if (state == NULL)
			pg_atomic_fetch_add_u64(
				&pglc_shared->relation_state_admission_rejections, 1);
	}
	if (state != NULL && !found)
	{
		PgLocalCacheRelationKey saved_key = state->key;

		memset(state, 0, sizeof(*state));
		state->key = saved_key;
		state->relation_oid = relation_oid;
		/*
		 * Seed recycled namespace state from the monotonically increasing
		 * transaction generation.  Otherwise removing and later recreating a
		 * namespace could make an old cache entry with relation_version == 0
		 * current again.
		 */
		state->version = pglc_shared->global_version;
	}
	else if (state != NULL && create && OidIsValid(relation_oid) &&
			 state->relation_oid != relation_oid)
	{
		/*
		 * A namespace can be remapped while an older worker still holds its
		 * previous mapping.  Never silently retag version state: force every
		 * entry for either relation to miss.
		 */
		state->version++;
		state->relation_oid = relation_oid;
	}
	return state;
}

static bool
cache_entry_is_current_locked(PgLocalCacheCacheEntry *entry,
							  PgLocalCacheRelationState *relation_state)
{
	return entry->valid &&
			relation_state != NULL &&
			entry->relation_oid == relation_state->relation_oid &&
			entry->global_epoch == pglc_shared->global_epoch &&
			entry->relation_version == relation_state->version;
}

static void
advance_global_version_locked(void)
{
	pglc_shared->global_version++;
}

static uint64
next_entry_generation(void)
{
	uint64		generation;

	generation =
		pg_atomic_fetch_add_u64(&pglc_shared->entry_generation, 1) + 1;
	if (generation == 0)
		generation =
			pg_atomic_fetch_add_u64(&pglc_shared->entry_generation, 1) + 1;
	return generation;
}

static int
cache_load_lease_ms(void)
{
	return Max(1000, pglc_statement_timeout_ms * 2);
}

/*
 * Return true only while the current owner still has a valid lease.  Revoking
 * an orphaned lease also changes the entry generation, fencing a late owner
 * from filling an entry that another worker has subsequently reclaimed.
 */
static bool
cache_load_is_active_locked(PgLocalCacheCacheEntry *entry, TimestampTz now)
{
	if (!entry->loading)
		return false;
	if (!TimestampDifferenceExceeds(entry->load_started, now,
								cache_load_lease_ms()))
		return true;

	entry->loading = false;
	entry->load_id++;
	entry->version = next_entry_generation();
	return false;
}

static bool
evict_one_cache_entry(void)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheCacheEntry *entry;
	PgLocalCacheCacheKey victim;
	uint64		oldest = PG_UINT64_MAX;
	uint32		initial_cursor = pglc_shared->eviction_bucket_cursor;
	uint32		start_bucket;
	int			scanned = 0;
	int			pass;
	bool		have_victim = false;
	TimestampTz now = GetCurrentTimestamp();

	/*
	 * Rotate a strictly bounded dynahash sample so no bucket is permanently
	 * pinned.  Reclaim a stale entry immediately when the sample encounters one;
	 * otherwise evict the least recently used live candidate in the sample.
	 * Counting protected entries keeps admission work bounded even during long
	 * transactions or abandoned loader leases.
	 */
	for (pass = 0; pass < 2; pass++)
	{
		if (pass == 1 && initial_cursor == 0)
			break;
		start_bucket = pass == 0 ? initial_cursor : 0;
		hash_seq_init(&sequence, pglc_cache_hash);
		sequence.curBucket = start_bucket;
		while ((entry = hash_seq_search(&sequence)) != NULL)
		{
			PgLocalCacheRelationState *relation_state;
			uint64		last_access;

			scanned++;
			if (entry->dirty_writers == 0 &&
				!cache_load_is_active_locked(entry, now))
			{
				relation_state = get_relation_state(entry->key.database_oid,
											entry->relation_oid,
											entry->key.nspace,
											false);
				if (!cache_entry_is_current_locked(entry, relation_state))
				{
					victim = entry->key;
					have_victim = true;
					pglc_shared->eviction_bucket_cursor =
						sequence.curBucket;
					if (sequence.curEntry != NULL)
						pglc_shared->eviction_bucket_cursor++;
					hash_seq_term(&sequence);
					goto remove_victim;
				}

				last_access = pg_atomic_read_u64(&entry->last_access);
				if (last_access <= oldest)
				{
					oldest = last_access;
					victim = entry->key;
					have_victim = true;
				}
			}

			if (scanned >= PGLC_EVICTION_SAMPLE)
			{
				pglc_shared->eviction_bucket_cursor = sequence.curBucket;
				if (sequence.curEntry != NULL)
					pglc_shared->eviction_bucket_cursor++;
				hash_seq_term(&sequence);
				goto sample_complete;
			}
		}

		/* hash_seq_search reached the end and terminated the scan. */
		pglc_shared->eviction_bucket_cursor = 0;
		if (have_victim || start_bucket == 0 ||
			scanned >= PGLC_EVICTION_SAMPLE)
			break;
	}

sample_complete:
	if (!have_victim)
		return false;

remove_victim:
	if (hash_search(pglc_cache_hash, &victim, HASH_REMOVE, NULL) == NULL)
		return false;
	pg_atomic_fetch_add_u64(&pglc_shared->evictions, 1);
	return true;
}

static PgLocalCacheCacheEntry *
get_cache_entry(Oid database_oid, Oid relation_oid,
				const char *nspace, const char *key, bool create)
{
	PgLocalCacheCacheKey cache_key;
	PgLocalCacheCacheEntry *entry;
	bool		found;

	make_cache_key(&cache_key, database_oid, nspace, key, create);
	entry = hash_search(pglc_cache_hash, &cache_key, HASH_FIND, NULL);
	found = entry != NULL;
	if (entry == NULL && create)
	{
		/* Enforce capacity explicitly; dynahash does not do so by default. */
		if (hash_get_num_entries(pglc_cache_hash) >=
			(uint64) pglc_cache_entries && !evict_one_cache_entry())
		{
			pg_atomic_fetch_add_u64(
				&pglc_shared->cache_admission_rejections, 1);
			return NULL;
		}
		entry = hash_search(pglc_cache_hash, &cache_key,
							HASH_ENTER_NULL, &found);
		if (entry == NULL)
			pg_atomic_fetch_add_u64(
				&pglc_shared->cache_admission_rejections, 1);
	}

	if (entry != NULL && !found)
	{
		PgLocalCacheCacheKey saved_key = entry->key;

		memset(entry, 0, sizeof(*entry));
		entry->key = saved_key;
		entry->relation_oid = relation_oid;
		entry->version = next_entry_generation();
		pg_atomic_init_u64(&entry->last_access, 0);
	}
	else if (entry != NULL && create && OidIsValid(relation_oid) &&
			 entry->relation_oid != relation_oid)
	{
		/*
		 * Retagging a valid entry would let a value read from the old
		 * relation become a hit for the new relation.
		 */
		entry->valid = false;
		entry->version = next_entry_generation();
		entry->loading = false;
		entry->load_id++;
		entry->relation_oid = relation_oid;
	}
	return entry;
}

static uint64
invalidate_namespace_locked(Oid database_oid, const char *nspace)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheCacheEntry *entry;
	PgLocalCacheRelationState *relation_state;
	uint64		count = 0;

	relation_state = get_relation_state(database_oid, InvalidOid,
									   nspace, false);
	if (relation_state == NULL)
		return 0;

	hash_seq_init(&sequence, pglc_cache_hash);
	while ((entry = hash_seq_search(&sequence)) != NULL)
	{
		if (entry->key.database_oid == database_oid &&
			strncmp(entry->key.nspace, nspace, PGLC_NAMESPACE_MAX) == 0 &&
			cache_entry_is_current_locked(entry, relation_state))
			count++;
	}
	relation_state->version++;
	return count;
}

static uint64
invalidate_all_locked(void)
{
	pglc_shared->global_epoch++;
	return 1;
}

static bool
cache_lookup_locked(const PgLocalCacheMapping *mapping,
					const char *canonical_key,
					char *value, Size value_capacity, Size *value_len,
					bool *negative, TransactionId *source_xmin,
					PgLocalCacheReadToken *token,
					bool create, bool *complete)
{
	PgLocalCacheRelationState *relation_state;
	PgLocalCacheCacheEntry *entry;
	bool		mapping_matches;
	bool		mapping_current;
	bool		hit = false;

	relation_state = get_relation_state(MyDatabaseId, mapping->relation_oid,
										mapping->nspace, create);
	entry = get_cache_entry(MyDatabaseId, mapping->relation_oid,
							mapping->nspace, canonical_key, create);
	mapping_matches = relation_state != NULL && entry != NULL &&
		relation_state->relation_oid == mapping->relation_oid &&
		entry->relation_oid == mapping->relation_oid;
	mapping_current =
		pg_atomic_read_u64(&pglc_shared->config_generation) ==
		mapping->config_generation;
	*complete = mapping_matches;

	token->config_generation = mapping->config_generation;
	token->global_version = pglc_shared->global_version;
	token->relation_version = relation_state ? relation_state->version : 0;
	token->key_version = entry ? entry->version : 0;
	token->source_observed_full_xid =
		entry ? entry->source_observed_full_xid : 0;
	token->has_entry = entry != NULL;
	token->cacheable = mapping_matches && mapping_current &&
		pglc_shared->global_dirty_writers == 0 &&
		relation_state->dirty_writers == 0 &&
		entry->dirty_writers == 0;

	if (token->cacheable &&
		cache_entry_is_current_locked(entry, relation_state))
	{
		uint64		access_clock;

		if (entry->negative)
		{
			*negative = true;
			hit = true;
		}
		else if (entry->value_len <= value_capacity)
		{
			memcpy(value, entry->value, entry->value_len);
			*value_len = entry->value_len;
			*source_xmin = entry->source_xmin;
			hit = true;
		}
		/*
		 * Eviction only runs when admitting a new entry, and admission advances
		 * the clock.  Marking a hit with the current admission epoch gives the
		 * entry a second chance without a globally contended fetch-add on every
		 * lookup.
		 */
		access_clock = pg_atomic_read_u64(&pglc_shared->clock);
		if (pg_atomic_read_u64(&entry->last_access) != access_clock)
			pg_atomic_write_u64(&entry->last_access, access_clock);
	}
	return hit;
}

static bool
pglc_cache_lookup_internal(const PgLocalCacheMapping *mapping,
						   const char *canonical_key,
						   char *value, Size value_capacity,
						   Size *value_len, bool *negative,
						   TransactionId *source_xmin,
						   PgLocalCacheReadToken *token,
						   bool count_stats)
{
	bool		complete = false;
	bool		hit;

	pglc_require_preload();
	memset(token, 0, sizeof(*token));
	*negative = false;
	*value_len = 0;
	*source_xmin = InvalidTransactionId;

	LWLockAcquire(pglc_shared->lock, LW_SHARED);
	hit = cache_lookup_locked(mapping, canonical_key,
							  value, value_capacity, value_len,
							  negative, source_xmin, token,
							  false, &complete);
	LWLockRelease(pglc_shared->lock);

	if (!complete)
	{
		*negative = false;
		*value_len = 0;
		LWLockAcquire(pglc_shared->lock, LW_EXCLUSIVE);
		hit = cache_lookup_locked(mapping, canonical_key,
								  value, value_capacity, value_len,
								  negative, source_xmin, token,
								  true, &complete);
		LWLockRelease(pglc_shared->lock);
	}

	if (count_stats && hit)
	{
		pg_atomic_fetch_add_u64(&pglc_shared->cache_hits, 1);
		if (*negative)
			pg_atomic_fetch_add_u64(&pglc_shared->negative_hits, 1);
	}
	else if (count_stats)
		pg_atomic_fetch_add_u64(&pglc_shared->cache_misses, 1);
	return hit;
}

bool
pglc_cache_lookup(const PgLocalCacheMapping *mapping, const char *canonical_key,
				 char *value, Size value_capacity, Size *value_len,
				 bool *negative, TransactionId *source_xmin,
				 PgLocalCacheReadToken *token)
{
	return pglc_cache_lookup_internal(mapping, canonical_key,
								  value, value_capacity, value_len,
								  negative, source_xmin, token, true);
}

bool
pglc_cache_lookup_quiet(const PgLocalCacheMapping *mapping,
						const char *canonical_key,
						char *value, Size value_capacity, Size *value_len,
						bool *negative, TransactionId *source_xmin,
						PgLocalCacheReadToken *token)
{
	return pglc_cache_lookup_internal(mapping, canonical_key,
								  value, value_capacity, value_len,
									  negative, source_xmin, token, false);
}

bool
pglc_cache_retire_positive(const PgLocalCacheMapping *mapping,
						   const char *canonical_key,
						   const PgLocalCacheReadToken *token,
						   TransactionId expected_xmin)
{
	PgLocalCacheRelationState *relation_state;
	PgLocalCacheCacheEntry *entry;
	bool		retired = false;

	pglc_require_preload();
	if (!token->cacheable || !token->has_entry ||
		mapping->config_generation != token->config_generation)
		return false;

	LWLockAcquire(pglc_shared->lock, LW_EXCLUSIVE);
	relation_state = get_relation_state(MyDatabaseId, mapping->relation_oid,
										mapping->nspace, false);
	entry = get_cache_entry(MyDatabaseId, mapping->relation_oid,
							mapping->nspace, canonical_key, false);
	if (relation_state != NULL && entry != NULL &&
		pg_atomic_read_u64(&pglc_shared->config_generation) ==
			token->config_generation &&
		relation_state->relation_oid == mapping->relation_oid &&
		entry->relation_oid == mapping->relation_oid &&
		pglc_shared->global_dirty_writers == 0 &&
		relation_state->dirty_writers == 0 && entry->dirty_writers == 0 &&
		pglc_shared->global_version == token->global_version &&
		relation_state->version == token->relation_version &&
		entry->version == token->key_version &&
		cache_entry_is_current_locked(entry, relation_state) &&
		!entry->negative &&
		TransactionIdEquals(entry->source_xmin, expected_xmin) &&
		entry->source_observed_full_xid == token->source_observed_full_xid)
	{
		entry->valid = false;
		entry->loading = false;
		entry->load_id++;
		entry->version = next_entry_generation();
		entry->source_xmin = InvalidTransactionId;
		entry->source_observed_full_xid = 0;
		retired = true;
	}
	LWLockRelease(pglc_shared->lock);
	return retired;
}

bool
pglc_cache_store(const PgLocalCacheMapping *mapping, const char *canonical_key,
				const PgLocalCacheReadToken *token, const char *value,
				Size value_len, bool negative, uint64 load_id,
				TransactionId source_xmin)
{
	PgLocalCacheRelationState *relation_state;
	PgLocalCacheCacheEntry *entry;
	bool		stored = false;
	uint64		observed_full_xid;

	if (!token->cacheable || !token->has_entry || value_len > PGLC_VALUE_MAX ||
		mapping->config_generation != token->config_generation ||
		pg_atomic_read_u64(&pglc_shared->config_generation) !=
		token->config_generation)
		return false;

	/*
	 * Read PostgreSQL's FullTransactionId horizon before taking our cache
	 * LWLock.  ReadNextFullTransactionId() takes XidGenLock, so this ordering
	 * avoids nesting PostgreSQL's transaction lock inside the extension lock.
	 * The horizon lets SQL readers reject an entry before a 32-bit heap xmin
	 * can become ambiguous after wraparound.
	 */
	observed_full_xid =
		U64FromFullTransactionId(ReadNextFullTransactionId());

	LWLockAcquire(pglc_shared->lock, LW_EXCLUSIVE);
	relation_state = get_relation_state(MyDatabaseId, mapping->relation_oid,
										mapping->nspace, false);
	entry = get_cache_entry(MyDatabaseId, mapping->relation_oid,
							mapping->nspace, canonical_key, false);

	if (relation_state != NULL && entry != NULL &&
		pg_atomic_read_u64(&pglc_shared->config_generation) ==
		token->config_generation &&
		relation_state->relation_oid == mapping->relation_oid &&
		entry->relation_oid == mapping->relation_oid &&
		pglc_shared->global_dirty_writers == 0 &&
		relation_state->dirty_writers == 0 &&
		entry->dirty_writers == 0 &&
		load_id != 0 && entry->loading && entry->load_id == load_id &&
		pglc_shared->global_version == token->global_version &&
		relation_state->version == token->relation_version &&
		entry->version == token->key_version)
	{
		entry->negative = negative;
		entry->value_len = negative ? 0 : value_len;
		entry->source_xmin = negative ? InvalidTransactionId : source_xmin;
		entry->source_observed_full_xid = observed_full_xid;
		if (!negative && value_len > 0)
			memcpy(entry->value, value, value_len);
		entry->global_epoch = pglc_shared->global_epoch;
		entry->relation_version = relation_state->version;
		/*
		 * The first successful fill wins.  Moving to a fresh generation
		 * prevents a timed-out or orphaned former loader from overwriting it.
		 * The successful owner fill also completes its outstanding lease.
		 */
		entry->version = next_entry_generation();
		entry->loading = false;
		entry->load_id++;
		entry->valid = true;
		stored = true;
		pg_atomic_write_u64(
			&entry->last_access,
			pg_atomic_fetch_add_u64(&pglc_shared->clock, 1) + 1);
	}
	LWLockRelease(pglc_shared->lock);
	if (stored && negative)
		pg_atomic_fetch_add_u64(&pglc_shared->negative_writes, 1);
	return stored;
}

PgLocalCacheLoadClaim
pglc_cache_claim_load(const PgLocalCacheMapping *mapping,
					  const char *canonical_key,
					  const PgLocalCacheReadToken *token,
					  uint64 *load_id)
{
	PgLocalCacheRelationState *relation_state;
	PgLocalCacheCacheEntry *entry;
	PgLocalCacheLoadClaim result = PGLC_LOAD_BYPASS;
	TimestampTz now = GetCurrentTimestamp();

	*load_id = 0;
	if (!token->cacheable || !token->has_entry)
		return result;

	LWLockAcquire(pglc_shared->lock, LW_EXCLUSIVE);
	relation_state = get_relation_state(MyDatabaseId, mapping->relation_oid,
									   mapping->nspace, false);
	entry = get_cache_entry(MyDatabaseId, mapping->relation_oid,
							mapping->nspace, canonical_key, false);
	if (relation_state == NULL || entry == NULL ||
		mapping->config_generation != token->config_generation ||
		pg_atomic_read_u64(&pglc_shared->config_generation) !=
			token->config_generation ||
		relation_state->relation_oid != mapping->relation_oid ||
		entry->relation_oid != mapping->relation_oid ||
		pglc_shared->global_dirty_writers != 0 ||
		relation_state->dirty_writers != 0 || entry->dirty_writers != 0 ||
		pglc_shared->global_version != token->global_version ||
		relation_state->version != token->relation_version)
		goto done;

	/*
	 * A follower can observe the miss and then be descheduled until the owner
	 * publishes a value.  A successful publish advances entry->version, so the
	 * follower's token is stale even though the entry is now usable.  Let the
	 * caller repeat its quiet lookup instead of bypassing to a duplicate SQL
	 * read.  An invalid entry with a changed generation reaches the explicit
	 * version retry immediately below.  That retry must also happen before
	 * loader cleanup: a stale follower must not cancel a newer owner.  Global/
	 * relation and dirty-writer fences above stay conservative because they
	 * represent transaction invalidation, not an owner completing this load.
	 */
	if (cache_entry_is_current_locked(entry, relation_state))
	{
		result = PGLC_LOAD_RETRY;
		goto done;
	}
	if (entry->version != token->key_version)
	{
		result = PGLC_LOAD_RETRY;
		goto done;
	}

	if (entry->loading &&
		(entry->load_global_version != token->global_version ||
		 entry->load_relation_version != token->relation_version ||
		 entry->load_key_version != token->key_version))
	{
		entry->loading = false;
		entry->load_id++;
	}

	if (cache_load_is_active_locked(entry, now))
	{
		result = PGLC_LOAD_WAIT;
		goto done;
	}

	entry->loading = true;
	entry->load_started = now;
	entry->load_global_version = token->global_version;
	entry->load_relation_version = token->relation_version;
	entry->load_key_version = token->key_version;
	entry->load_id++;
	if (entry->load_id == 0)
		entry->load_id = 1;
	*load_id = entry->load_id;
	result = PGLC_LOAD_OWNER;
	pg_atomic_fetch_add_u64(&pglc_shared->singleflight_leaders, 1);

done:
	LWLockRelease(pglc_shared->lock);
	return result;
}

void
pglc_cache_release_load(const PgLocalCacheMapping *mapping,
						const char *canonical_key,
						const PgLocalCacheReadToken *claim_token,
						uint64 load_id)
{
	PgLocalCacheCacheEntry *entry;

	pglc_require_preload();
	if (load_id == 0 || claim_token == NULL ||
		!claim_token->cacheable || !claim_token->has_entry)
		return;
	LWLockAcquire(pglc_shared->lock, LW_EXCLUSIVE);
	entry = get_cache_entry(MyDatabaseId, mapping->relation_oid,
							mapping->nspace, canonical_key, false);
	if (entry != NULL &&
		entry->key.database_oid == MyDatabaseId &&
		strcmp(entry->key.nspace, mapping->nspace) == 0 &&
		strcmp(entry->key.key, canonical_key) == 0 &&
		entry->relation_oid == mapping->relation_oid &&
		entry->version == claim_token->key_version &&
		entry->loading && entry->load_id == load_id &&
		entry->load_global_version == claim_token->global_version &&
		entry->load_relation_version == claim_token->relation_version &&
		entry->load_key_version == claim_token->key_version)
		entry->loading = false;
	LWLockRelease(pglc_shared->lock);
}

void
pglc_note_singleflight_waiter(void)
{
	pg_atomic_fetch_add_u64(&pglc_shared->singleflight_waiters, 1);
}

void
pglc_note_singleflight_reuse(void)
{
	pg_atomic_fetch_add_u64(&pglc_shared->singleflight_reuses, 1);
}

void
pglc_note_singleflight_timeout(void)
{
	pg_atomic_fetch_add_u64(&pglc_shared->singleflight_timeouts, 1);
}

bool
pglc_current_transaction_is_dirty(void)
{
	return local_dirty_hash != NULL || local_global_fallback;
}

uint64
pglc_cache_invalidate_namespace(Oid database_oid, const char *nspace)
{
	uint64		count;

	pglc_require_preload();
	LWLockAcquire(pglc_shared->lock, LW_EXCLUSIVE);
	advance_global_version_locked();
	count = invalidate_namespace_locked(database_oid, nspace);
	LWLockRelease(pglc_shared->lock);
	pg_atomic_fetch_add_u64(&pglc_shared->invalidations, 1);
	pg_atomic_fetch_add_u64(&pglc_shared->table_invalidations, 1);
	return count;
}

uint64
pglc_cache_invalidate_key(const PgLocalCacheMapping *mapping,
						  const char *canonical_key)
{
	PgLocalCacheRelationState *relation_state;
	PgLocalCacheCacheEntry *entry;
	uint64		count = 0;

	pglc_require_preload();
	LWLockAcquire(pglc_shared->lock, LW_EXCLUSIVE);
	advance_global_version_locked();
	relation_state = get_relation_state(MyDatabaseId, mapping->relation_oid,
									   mapping->nspace, false);
	entry = get_cache_entry(MyDatabaseId, mapping->relation_oid,
						mapping->nspace, canonical_key, false);
	if (entry != NULL &&
		cache_entry_is_current_locked(entry, relation_state))
		count = 1;
	if (entry != NULL)
	{
		entry->valid = false;
		entry->loading = false;
		entry->load_id++;
		entry->version = next_entry_generation();
		entry->source_xmin = InvalidTransactionId;
		entry->source_observed_full_xid = 0;
	}
	LWLockRelease(pglc_shared->lock);
	pg_atomic_fetch_add_u64(&pglc_shared->invalidations, count);
	pg_atomic_fetch_add_u64(&pglc_shared->key_invalidations, count);
	return count;
}

uint64
pglc_cache_invalidate_database(Oid database_oid)
{
	HASH_SEQ_STATUS cache_sequence;
	HASH_SEQ_STATUS relation_sequence;
	PgLocalCacheCacheEntry *entry;
	PgLocalCacheRelationState *relation_state;
	uint64		count = 0;

	pglc_require_preload();
	LWLockAcquire(pglc_shared->lock, LW_EXCLUSIVE);
	advance_global_version_locked();
	hash_seq_init(&cache_sequence, pglc_cache_hash);
	while ((entry = hash_seq_search(&cache_sequence)) != NULL)
	{
		if (entry->key.database_oid != database_oid)
			continue;
		relation_state = get_relation_state(database_oid, entry->relation_oid,
										entry->key.nspace, false);
		if (cache_entry_is_current_locked(entry, relation_state))
			count++;
	}
	hash_seq_init(&relation_sequence, pglc_relation_hash);
	while ((relation_state = hash_seq_search(&relation_sequence)) != NULL)
	{
		if (relation_state->key.database_oid == database_oid)
			relation_state->version++;
	}
	LWLockRelease(pglc_shared->lock);
	pg_atomic_fetch_add_u64(&pglc_shared->invalidations, count);
	pg_atomic_fetch_add_u64(&pglc_shared->table_invalidations, 1);
	return count;
}

uint64
pglc_cache_invalidate_all(void)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheCacheEntry *entry;
	PgLocalCacheRelationState *relation_state;
	uint64		count = 0;

	pglc_require_preload();
	LWLockAcquire(pglc_shared->lock, LW_EXCLUSIVE);
	hash_seq_init(&sequence, pglc_cache_hash);
	while ((entry = hash_seq_search(&sequence)) != NULL)
	{
		relation_state = get_relation_state(entry->key.database_oid,
										entry->relation_oid,
										entry->key.nspace, false);
		if (cache_entry_is_current_locked(entry, relation_state))
			count++;
	}
	advance_global_version_locked();
	(void) invalidate_all_locked();
	LWLockRelease(pglc_shared->lock);
	pg_atomic_fetch_add_u64(&pglc_shared->invalidations, count);
	pg_atomic_fetch_add_u64(&pglc_shared->table_invalidations, 1);
	return count;
}

void
pglc_note_database_read(void)
{
	pg_atomic_fetch_add_u64(&pglc_shared->database_reads, 1);
}

void
pglc_note_database_write(void)
{
	pg_atomic_fetch_add_u64(&pglc_shared->database_writes, 1);
}

void
pglc_note_client_limit_rejection(void)
{
	pg_atomic_fetch_add_u64(&pglc_shared->client_limit_rejections, 1);
	pg_atomic_fetch_add_u64(&pglc_shared->rejected_connections, 1);
}

bool
pglc_try_reserve_client(void)
{
	uint64		active = pg_atomic_read_u64(&pglc_shared->active_clients);

	while (active < (uint64) pglc_max_clients)
	{
		uint64		desired = active + 1;

		if (pg_atomic_compare_exchange_u64(&pglc_shared->active_clients,
										   &active, desired))
		{
			uint64		peak =
				pg_atomic_read_u64(&pglc_shared->peak_active_clients);

			while (desired > peak &&
				   !pg_atomic_compare_exchange_u64(
					   &pglc_shared->peak_active_clients, &peak, desired))
				;
			return true;
		}
	}
	pglc_note_client_limit_rejection();
	return false;
}

void
pglc_release_clients(uint64 count)
{
	uint64		active;

	if (count == 0 || pglc_shared == NULL)
		return;
	active = pg_atomic_read_u64(&pglc_shared->active_clients);
	for (;;)
	{
		uint64		desired;

		Assert(active >= count);
		desired = active >= count ? active - count : 0;
		if (pg_atomic_compare_exchange_u64(&pglc_shared->active_clients,
										   &active, desired))
			return;
	}
}

void
pglc_note_worker_start(void)
{
	pg_atomic_fetch_add_u64(&pglc_shared->worker_starts, 1);
	pg_atomic_fetch_add_u64(&pglc_shared->active_workers, 1);
}

void
pglc_note_worker_stop(void)
{
	uint64		previous;

	if (pglc_shared == NULL)
		return;
	previous = pg_atomic_fetch_sub_u64(&pglc_shared->active_workers, 1);
	Assert(previous > 0);
	if (previous == 0)
		pg_atomic_write_u64(&pglc_shared->active_workers, 0);
}

static uint64
pglc_workers_without_current_mappings(void)
{
	uint64		generation;
	uint64		observed_generation;
	uint64		workers = 0;
	int		worker_index;

	if (pglc_port == 0)
		return 0;
	generation = pglc_config_generation();
	for (worker_index = 0; worker_index < pglc_worker_count; worker_index++)
	{
		if (pg_atomic_read_u64(
				&pglc_shared->worker_mapping_generations[worker_index]) !=
			generation)
			workers++;
	}
	observed_generation = pglc_config_generation();
	if (observed_generation != generation)
		return (uint64) pglc_worker_count;
	return workers;
}

static HTAB *
get_local_dirty_hash(void)
{
	HASHCTL		control;

	if (local_dirty_hash != NULL)
		return local_dirty_hash;

	memset(&control, 0, sizeof(control));
	control.keysize = sizeof(PgLocalCacheLocalDirtyKey);
	control.entrysize = sizeof(PgLocalCacheLocalDirtyEntry);
	control.hcxt = TopTransactionContext;
	local_dirty_hash = hash_create("pg_local_cache transaction dirty keys",
								   64,
								   &control,
								   HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);
	return local_dirty_hash;
}

static PgLocalCacheLocalDirtyEntry *
collect_dirty(PgLocalCacheDirtyKind kind, Oid database_oid, Oid relation_oid,
			  const char *nspace, const char *key)
{
	PgLocalCacheLocalDirtyKey dirty_key;
	PgLocalCacheLocalDirtyEntry *entry;
	bool		found;

	pglc_require_preload();
	memset(&dirty_key, 0, sizeof(dirty_key));
	dirty_key.kind = (uint8) kind;
	dirty_key.database_oid = database_oid;
	if (nspace)
		strlcpy(dirty_key.nspace, nspace, sizeof(dirty_key.nspace));
	if (key)
		strlcpy(dirty_key.key, key, sizeof(dirty_key.key));

	entry = hash_search(get_local_dirty_hash(), &dirty_key, HASH_ENTER, &found);
	if (!found)
	{
		entry->relation_oid = relation_oid;
		entry->shared_marker_reserved = false;
	}
	return entry;
}

static void
pglc_collect_key(Oid database_oid, Oid relation_oid,
				const char *nspace, const char *key)
{
	PgLocalCacheLocalDirtyKey relation_key;
	HTAB	   *dirty = get_local_dirty_hash();

	memset(&relation_key, 0, sizeof(relation_key));
	relation_key.kind = (uint8) PGLC_DIRTY_RELATION;
	relation_key.database_oid = database_oid;
	strlcpy(relation_key.nspace, nspace, sizeof(relation_key.nspace));
	if (hash_search(dirty, &relation_key, HASH_FIND, NULL) != NULL)
		return;

	relation_key.kind = (uint8) PGLC_DIRTY_FORGET_RELATION;
	if (hash_search(dirty, &relation_key, HASH_FIND, NULL) != NULL)
		return;

	if (hash_get_num_entries(dirty) >= pglc_max_dirty_keys)
	{
		pg_atomic_fetch_add_u64(&pglc_shared->dirty_key_limit_fallbacks, 1);
		pglc_collect_relation(database_oid, relation_oid, nspace);
		return;
	}
	(void) collect_dirty(PGLC_DIRTY_KEY, database_oid, relation_oid,
						 nspace, key);
}

static void
pglc_collect_relation(Oid database_oid, Oid relation_oid,
					 const char *nspace)
{
	(void) collect_dirty(PGLC_DIRTY_RELATION, database_oid, relation_oid,
						 nspace, NULL);
}

static void
pglc_collect_forget_relation(Oid database_oid, Oid relation_oid,
							 const char *nspace)
{
	(void) collect_dirty(PGLC_DIRTY_FORGET_RELATION,
						 database_oid, relation_oid, nspace, NULL);
}

static void
pglc_collect_global(bool bump_config)
{
	(void) collect_dirty(PGLC_DIRTY_GLOBAL, MyDatabaseId, InvalidOid,
						 NULL, NULL);
	if (bump_config)
		local_bump_config = true;
}

static bool
local_has_global_dirty(void)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheLocalDirtyEntry *entry;

	hash_seq_init(&sequence, local_dirty_hash);
	while ((entry = hash_seq_search(&sequence)) != NULL)
	{
		if (entry->key.kind == PGLC_DIRTY_GLOBAL)
		{
			hash_seq_term(&sequence);
			return true;
		}
	}
	return false;
}

static bool
precreate_shared_entries_locked(void)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheLocalDirtyEntry *local;
	bool		success = true;

	hash_seq_init(&sequence, local_dirty_hash);
	while ((local = hash_seq_search(&sequence)) != NULL)
	{
		if (local->key.kind == PGLC_DIRTY_KEY)
		{
			PgLocalCacheCacheEntry *entry;

			entry = get_cache_entry(local->key.database_oid,
									local->relation_oid,
									local->key.nspace,
									local->key.key,
									true);
			if (entry == NULL)
			{
				hash_seq_term(&sequence);
				success = false;
				break;
			}
			entry->dirty_writers++;
			local->shared_marker_reserved = true;
		}
		else if (local->key.kind == PGLC_DIRTY_RELATION ||
				 local->key.kind == PGLC_DIRTY_FORGET_RELATION)
		{
			PgLocalCacheRelationState *state;

			state = get_relation_state(local->key.database_oid,
									   local->relation_oid,
									   local->key.nspace,
									   true);
			if (state == NULL)
			{
				hash_seq_term(&sequence);
				success = false;
				break;
			}
			state->dirty_writers++;
			local->shared_marker_reserved = true;
		}
	}

	if (!success)
	{
		hash_seq_init(&sequence, local_dirty_hash);
		while ((local = hash_seq_search(&sequence)) != NULL)
		{
			if (!local->shared_marker_reserved)
				continue;
			if (local->key.kind == PGLC_DIRTY_KEY)
			{
				PgLocalCacheCacheEntry *entry;

				entry = get_cache_entry(local->key.database_oid,
										local->relation_oid,
										local->key.nspace,
										local->key.key,
										false);
				Assert(entry != NULL && entry->dirty_writers > 0);
				if (entry != NULL && entry->dirty_writers > 0)
					entry->dirty_writers--;
			}
			else if (local->key.kind == PGLC_DIRTY_RELATION ||
					 local->key.kind == PGLC_DIRTY_FORGET_RELATION)
			{
				PgLocalCacheRelationState *state;

				state = get_relation_state(local->key.database_oid,
										   local->relation_oid,
										   local->key.nspace,
										   false);
				Assert(state != NULL && state->dirty_writers > 0);
				if (state != NULL && state->dirty_writers > 0)
					state->dirty_writers--;
			}
			local->shared_marker_reserved = false;
		}
	}
	return success;
}

static void
pglc_publish_dirty(void)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheLocalDirtyEntry *local;
	uint64		invalidated = 0;
	uint64		key_invalidated = 0;
	uint64		table_invalidated = 0;

	if (local_dirty_hash == NULL || local_dirty_published)
		return;
	/* A statement guard alone is a backend-local fence, not an invalidation. */
	if (hash_get_num_entries(local_dirty_hash) == 0)
		return;

	LWLockAcquire(pglc_shared->lock, LW_EXCLUSIVE);
	advance_global_version_locked();

	if (local_has_global_dirty() || !precreate_shared_entries_locked())
	{
		pglc_shared->global_dirty_writers++;
		invalidated += invalidate_all_locked();
		local_global_fallback = true;
	}
	else
	{
		hash_seq_init(&sequence, local_dirty_hash);
		while ((local = hash_seq_search(&sequence)) != NULL)
		{
			if (local->key.kind == PGLC_DIRTY_KEY)
			{
				PgLocalCacheCacheEntry *entry;

				entry = get_cache_entry(local->key.database_oid,
										local->relation_oid,
										local->key.nspace,
										local->key.key,
										false);
				Assert(entry != NULL);
				if (entry->valid)
				{
					invalidated++;
					key_invalidated++;
				}
				entry->valid = false;
				entry->loading = false;
				entry->load_id++;
				entry->version = next_entry_generation();
			}
			else if (local->key.kind == PGLC_DIRTY_RELATION ||
					 local->key.kind == PGLC_DIRTY_FORGET_RELATION)
			{
				PgLocalCacheRelationState *state;

				state = get_relation_state(local->key.database_oid,
										   local->relation_oid,
										   local->key.nspace,
										   false);
				Assert(state != NULL);
				state->version++;
				invalidated++;
				table_invalidated++;
			}
		}
	}

	local_dirty_published = true;
	LWLockRelease(pglc_shared->lock);
	pg_atomic_fetch_add_u64(&pglc_shared->invalidations, invalidated);
	pg_atomic_fetch_add_u64(&pglc_shared->key_invalidations,
						key_invalidated);
	pg_atomic_fetch_add_u64(&pglc_shared->table_invalidations,
						table_invalidated);
}

static void
forget_relation_states_locked(bool committed)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheLocalDirtyEntry *local;

	if (!committed)
		return;

	hash_seq_init(&sequence, local_dirty_hash);
	while ((local = hash_seq_search(&sequence)) != NULL)
	{
		PgLocalCacheRelationKey relation_key;
		PgLocalCacheRelationState *state;

		if (local->key.kind != PGLC_DIRTY_FORGET_RELATION)
			continue;
		make_relation_key(&relation_key, local->key.database_oid,
						  local->key.nspace);
		state = hash_search(pglc_relation_hash, &relation_key,
							HASH_FIND, NULL);
		if (state != NULL)
		{
			state->pending_forget = true;
			if (state->dirty_writers == 0)
				(void) hash_search(pglc_relation_hash, &relation_key,
								   HASH_REMOVE, NULL);
		}
	}
}

static void
pglc_finish_dirty(bool committed)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheLocalDirtyEntry *local;
	bool		bump_config_after_unlock = committed && local_bump_config;

	if (local_dirty_hash == NULL)
		return;

	if (local_dirty_published)
	{
		LWLockAcquire(pglc_shared->lock, LW_EXCLUSIVE);
		if (local_global_fallback)
		{
			Assert(pglc_shared->global_dirty_writers > 0);
			pglc_shared->global_dirty_writers--;
		}
		else
		{
			hash_seq_init(&sequence, local_dirty_hash);
			while ((local = hash_seq_search(&sequence)) != NULL)
			{
				if (local->key.kind == PGLC_DIRTY_KEY)
				{
					PgLocalCacheCacheEntry *entry;

					entry = get_cache_entry(local->key.database_oid,
											local->relation_oid,
											local->key.nspace,
											local->key.key,
											false);
					if (entry != NULL)
					{
						entry->valid = false;
						Assert(entry->dirty_writers > 0);
						entry->dirty_writers--;
					}
				}
				else if (local->key.kind == PGLC_DIRTY_RELATION ||
						 local->key.kind == PGLC_DIRTY_FORGET_RELATION)
				{
					PgLocalCacheRelationState *state;

					state = get_relation_state(local->key.database_oid,
											   local->relation_oid,
											   local->key.nspace,
											   false);
					if (state != NULL)
					{
						PgLocalCacheRelationKey relation_key;

						Assert(state->dirty_writers > 0);
						state->dirty_writers--;
						if (state->dirty_writers == 0 &&
							state->pending_forget)
						{
							make_relation_key(
								&relation_key,
								local->key.database_oid,
								local->key.nspace);
							(void) hash_search(
								pglc_relation_hash,
								&relation_key,
								HASH_REMOVE, NULL);
						}
					}
				}
			}
		}
		forget_relation_states_locked(committed);
		if (bump_config_after_unlock)
		{
			pg_atomic_fetch_add_u64(&pglc_shared->config_generation, 1);
			bump_config_after_unlock = false;
		}
		LWLockRelease(pglc_shared->lock);
	}

	if (bump_config_after_unlock)
		pg_atomic_fetch_add_u64(&pglc_shared->config_generation, 1);

	local_dirty_hash = NULL;
	local_dirty_published = false;
	local_global_fallback = false;
	local_bump_config = false;
}

static void
pglc_xact_callback(XactEvent event, void *arg)
{
	switch (event)
	{
		case XACT_EVENT_PRE_COMMIT:
		case XACT_EVENT_PARALLEL_PRE_COMMIT:
			pglc_publish_dirty();
			break;
		case XACT_EVENT_COMMIT:
		case XACT_EVENT_PARALLEL_COMMIT:
			pglc_finish_dirty(true);
			break;
		case XACT_EVENT_ABORT:
		case XACT_EVENT_PARALLEL_ABORT:
			pglc_finish_dirty(false);
			break;
		case XACT_EVENT_PRE_PREPARE:
			if (local_dirty_hash != NULL)
				ereport(ERROR,
						(errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
						 errmsg("PREPARE TRANSACTION is not supported after modifying a pg_local_cache mapping")));
			break;
		default:
			break;
	}
}

static void
pglc_backend_exit(int code, Datum arg)
{
	if (local_dirty_hash != NULL && pglc_shared != NULL)
		pglc_finish_dirty(false);
}

static void
collect_tuple_key(TriggerData *trigger_data, HeapTuple tuple,
				  const char *nspace, int key_count, char **column_names)
{
	TupleDesc	descriptor = RelationGetDescr(trigger_data->tg_relation);
	char		canonical[PGLC_KEY_MAX];
	Datum		key_values[PGLC_MAX_KEY_COLUMNS];
	bool		key_nulls[PGLC_MAX_KEY_COLUMNS];
	FmgrInfo	key_outputs[PGLC_MAX_KEY_COLUMNS];
	Size		canonical_len;
	int			key_index;

	MemSet(key_values, 0, sizeof(key_values));
	MemSet(key_nulls, 0, sizeof(key_nulls));
	MemSet(key_outputs, 0, sizeof(key_outputs));
	for (key_index = 0; key_index < key_count; key_index++)
	{
		AttrNumber	attribute_number;
		Form_pg_attribute attribute;
		Oid			output_function;
		bool		type_is_varlena;

		attribute_number = get_attnum(
			RelationGetRelid(trigger_data->tg_relation),
			column_names[key_index]);
		if (attribute_number == InvalidAttrNumber)
			ereport(ERROR,
					(errcode(ERRCODE_UNDEFINED_COLUMN),
					 errmsg("pg_local_cache key column \"%s\" no longer exists",
							column_names[key_index])));

		attribute = TupleDescAttr(descriptor, attribute_number - 1);
		key_values[key_index] = heap_getattr(
			tuple, attribute_number, descriptor, &key_nulls[key_index]);
		if (key_nulls[key_index])
			goto relation_fallback;

		getTypeOutputInfo(attribute->atttypid, &output_function,
						  &type_is_varlena);
		fmgr_info(output_function, &key_outputs[key_index]);
	}
	if (!pglc_canonical_key(key_values, key_nulls, key_count, key_outputs,
							canonical, sizeof(canonical), &canonical_len))
		goto relation_fallback;

	pglc_collect_key(MyDatabaseId,
					RelationGetRelid(trigger_data->tg_relation),
					nspace, canonical);
	return;

relation_fallback:
	pglc_collect_relation(MyDatabaseId,
						 RelationGetRelid(trigger_data->tg_relation),
						 nspace);
}

Datum
pg_local_cache_statement_guard(PG_FUNCTION_ARGS)
{
	TriggerData *trigger_data;
	int16		expected_type;

	if (!CALLED_AS_TRIGGER(fcinfo))
		ereport(ERROR,
				(errcode(ERRCODE_E_R_I_E_TRIGGER_PROTOCOL_VIOLATED),
				 errmsg("pg_local_cache statement guard must be called as a trigger")));

	trigger_data = (TriggerData *) fcinfo->context;
	expected_type = TRIGGER_TYPE_BEFORE | TRIGGER_TYPE_INSERT |
		TRIGGER_TYPE_UPDATE | TRIGGER_TYPE_DELETE | TRIGGER_TYPE_TRUNCATE;
	if (!TRIGGER_FIRED_BEFORE(trigger_data->tg_event) ||
		!TRIGGER_FIRED_FOR_STATEMENT(trigger_data->tg_event) ||
		trigger_data->tg_trigger->tgtype != expected_type ||
		trigger_data->tg_trigger->tgnargs != 0)
		ereport(ERROR,
				(errcode(ERRCODE_E_R_I_E_TRIGGER_PROTOCOL_VIOLATED),
				 errmsg("invalid pg_local_cache statement guard definition")));

	/*
	 * The empty transaction-local hash is a read-your-writes and 2PC fence.
	 * Exact keys remain the responsibility of the AFTER invalidators, so this
	 * does not invalidate shared entries or broaden commit invalidation.
	 */
	(void) get_local_dirty_hash();
	PG_RETURN_POINTER(NULL);
}

Datum
pg_local_cache_row_invalidate(PG_FUNCTION_ARGS)
{
	TriggerData *trigger_data;
	const char *nspace;
	int			key_count;
	char	  **column_names;

	if (!CALLED_AS_TRIGGER(fcinfo))
		ereport(ERROR,
				(errcode(ERRCODE_E_R_I_E_TRIGGER_PROTOCOL_VIOLATED),
				 errmsg("pg_local_cache row invalidator must be called as a trigger")));

	trigger_data = (TriggerData *) fcinfo->context;
	if (!TRIGGER_FIRED_AFTER(trigger_data->tg_event) ||
		!TRIGGER_FIRED_FOR_ROW(trigger_data->tg_event) ||
		trigger_data->tg_trigger->tgnargs < 2 ||
		trigger_data->tg_trigger->tgnargs > PGLC_MAX_KEY_COLUMNS + 1)
		ereport(ERROR,
				(errcode(ERRCODE_E_R_I_E_TRIGGER_PROTOCOL_VIOLATED),
				 errmsg("invalid pg_local_cache row trigger definition")));

	nspace = trigger_data->tg_trigger->tgargs[0];
	key_count = trigger_data->tg_trigger->tgnargs - 1;
	column_names = &trigger_data->tg_trigger->tgargs[1];

	if (TRIGGER_FIRED_BY_INSERT(trigger_data->tg_event))
		collect_tuple_key(trigger_data, trigger_data->tg_trigtuple,
						  nspace, key_count, column_names);
	else if (TRIGGER_FIRED_BY_DELETE(trigger_data->tg_event))
		collect_tuple_key(trigger_data, trigger_data->tg_trigtuple,
						  nspace, key_count, column_names);
	else if (TRIGGER_FIRED_BY_UPDATE(trigger_data->tg_event))
	{
		collect_tuple_key(trigger_data, trigger_data->tg_trigtuple,
						  nspace, key_count, column_names);
		collect_tuple_key(trigger_data, trigger_data->tg_newtuple,
						  nspace, key_count, column_names);
	}

	if (TRIGGER_FIRED_BY_INSERT(trigger_data->tg_event) ||
		TRIGGER_FIRED_BY_DELETE(trigger_data->tg_event))
		PG_RETURN_POINTER(trigger_data->tg_trigtuple);
	PG_RETURN_POINTER(trigger_data->tg_newtuple);
}

Datum
pg_local_cache_truncate_invalidate(PG_FUNCTION_ARGS)
{
	TriggerData *trigger_data;
	const char *nspace;

	if (!CALLED_AS_TRIGGER(fcinfo))
		ereport(ERROR,
				(errcode(ERRCODE_E_R_I_E_TRIGGER_PROTOCOL_VIOLATED),
				 errmsg("pg_local_cache truncate invalidator must be called as a trigger")));

	trigger_data = (TriggerData *) fcinfo->context;
	if (!TRIGGER_FIRED_AFTER(trigger_data->tg_event) ||
		!TRIGGER_FIRED_FOR_STATEMENT(trigger_data->tg_event) ||
		!TRIGGER_FIRED_BY_TRUNCATE(trigger_data->tg_event) ||
		trigger_data->tg_trigger->tgnargs != 1)
		ereport(ERROR,
				(errcode(ERRCODE_E_R_I_E_TRIGGER_PROTOCOL_VIOLATED),
				 errmsg("invalid pg_local_cache truncate trigger definition")));

	nspace = trigger_data->tg_trigger->tgargs[0];
	pglc_collect_relation(MyDatabaseId,
						 RelationGetRelid(trigger_data->tg_relation),
						 nspace);
	PG_RETURN_POINTER(NULL);
}

Datum
pg_local_cache_lock_relation(PG_FUNCTION_ARGS)
{
	Oid			relation_oid = PG_GETARG_OID(0);

	/*
	 * Administrative SQL must address tables by their already-resolved OID.
	 * Holding the same lock mode used by CREATE TRIGGER closes rename/drop and
	 * catalog-shape races until the surrounding transaction finishes.
	 */
	if (!OidIsValid(relation_oid))
		PG_RETURN_BOOL(false);
	LockRelationOid(relation_oid, ShareRowExclusiveLock);
	if (get_rel_name(relation_oid) == NULL)
	{
		UnlockRelationOid(relation_oid, ShareRowExclusiveLock);
		PG_RETURN_BOOL(false);
	}

	PG_RETURN_BOOL(true);
}

Datum
pg_local_cache_reload(PG_FUNCTION_ARGS)
{
	if (!superuser())
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser to reload pg_local_cache mappings")));
	pglc_collect_global(true);
	PG_RETURN_VOID();
}

Datum
pg_local_cache_forget(PG_FUNCTION_ARGS)
{
	text	   *namespace_text = PG_GETARG_TEXT_PP(0);
	char	   *nspace = text_to_cstring(namespace_text);
	Oid			relation_oid = PG_GETARG_OID(1);

	if (!superuser())
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser to unregister pg_local_cache mappings")));
	if (strlen(nspace) >= PGLC_NAMESPACE_MAX)
		ereport(ERROR,
				(errcode(ERRCODE_NAME_TOO_LONG),
				 errmsg("pg_local_cache namespace is too long")));

	pglc_collect_forget_relation(MyDatabaseId, relation_oid, nspace);
	PG_RETURN_VOID();
}

static uint64
count_namespace_entries(Oid database_oid, const char *nspace)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheCacheEntry *entry;
	PgLocalCacheRelationState *relation_state;
	uint64		count = 0;

	LWLockAcquire(pglc_shared->lock, LW_SHARED);
	relation_state = get_relation_state(database_oid, InvalidOid,
									   nspace, false);
	hash_seq_init(&sequence, pglc_cache_hash);
	while ((entry = hash_seq_search(&sequence)) != NULL)
	{
		if (relation_state != NULL &&
			entry->key.database_oid == database_oid &&
			strncmp(entry->key.nspace, nspace, PGLC_NAMESPACE_MAX) == 0 &&
			cache_entry_is_current_locked(entry, relation_state))
			count++;
	}
	LWLockRelease(pglc_shared->lock);
	return count;
}

static bool
pglc_mapping_exists(const char *nspace)
{
	Oid			argument_types[1] = {TEXTOID};
	Datum		arguments[1];
	int			result;
	bool		exists;

	arguments[0] = CStringGetTextDatum(nspace);
	if (SPI_connect() != SPI_OK_CONNECT)
		elog(ERROR, "pg_local_cache could not connect to SPI");
	result = SPI_execute_with_args(
		"SELECT 1 FROM local_cache.mapping WHERE namespace = $1",
		1, argument_types, arguments, NULL, true, 1);
	if (result != SPI_OK_SELECT)
		elog(ERROR, "pg_local_cache could not validate a namespace");
	exists = SPI_processed == 1;
	if (SPI_finish() != SPI_OK_FINISH)
		elog(ERROR, "pg_local_cache could not finish SPI");
	return exists;
}

Datum
pg_local_cache_invalidate(PG_FUNCTION_ARGS)
{
	text	   *namespace_text = PG_GETARG_TEXT_PP(0);
	char	   *nspace = text_to_cstring(namespace_text);
	uint64		count;

	if (!superuser())
		ereport(ERROR,
				(errcode(ERRCODE_INSUFFICIENT_PRIVILEGE),
				 errmsg("must be superuser to invalidate pg_local_cache")));
	if (strlen(nspace) >= PGLC_NAMESPACE_MAX)
		ereport(ERROR,
				(errcode(ERRCODE_NAME_TOO_LONG),
				 errmsg("pg_local_cache namespace is too long")));

	pglc_require_preload();
	if (!pglc_mapping_exists(nspace))
		ereport(ERROR,
				(errcode(ERRCODE_UNDEFINED_OBJECT),
				 errmsg("unknown pg_local_cache namespace \"%s\"", nspace)));
	count = count_namespace_entries(MyDatabaseId, nspace);
	pglc_collect_relation(MyDatabaseId, InvalidOid, nspace);
	PG_RETURN_INT64((int64) count);
}

char *
pglc_stats_json(void)
{
	StringInfoData expanded;
	PgLocalCacheSqlCounterSnapshot sql_counters;
	HASH_SEQ_STATUS sequence;
	PgLocalCacheCacheEntry *entry;
	uint64		positive = 0;
	uint64		negative = 0;
	uint64		dirty = 0;
	uint64		loading = 0;
	uint64		expired_loading = 0;
	uint64		dirty_relations = 0;
	uint64		relation_states = 0;
	uint64		pending_forget = 0;
	uint64		total;
	uint64		cache_hits;
	uint64		cache_misses;
	uint64		negative_hits;
	uint64		sql_cache_hits;
	uint64		sql_cache_misses;
	uint64		sql_cache_fills;
	uint64		sql_cache_bypasses;
	uint64		database_reads;
	uint64		database_writes;
	uint64		invalidations;
	uint64		evictions;
	uint64		singleflight_leaders;
	uint64		singleflight_waiters;
	uint64		singleflight_reuses;
	uint64		singleflight_timeouts;
	uint64		active_clients;
	uint64		rejected_connections;
	uint64		authentication_failures;
	uint64		protocol_errors;
	uint64		output_backpressure_events;
	uint64		slow_client_drops;
	uint64		worker_starts;
	uint64		workers_with_incomplete_mappings = 0;
	HASH_SEQ_STATUS relation_sequence;
	PgLocalCacheRelationState *relation_state;
	uint32		global_dirty_writers;
	TimestampTz now = GetCurrentTimestamp();

	pglc_require_preload();
	LWLockAcquire(pglc_shared->lock, LW_SHARED);
	hash_seq_init(&sequence, pglc_cache_hash);
	while ((entry = hash_seq_search(&sequence)) != NULL)
	{
		relation_state = get_relation_state(entry->key.database_oid,
										   entry->relation_oid,
										   entry->key.nspace,
										   false);
		if (cache_entry_is_current_locked(entry, relation_state) &&
			entry->negative)
			negative++;
		else if (cache_entry_is_current_locked(entry, relation_state))
			positive++;
		if (entry->dirty_writers > 0)
			dirty++;
		if (entry->loading)
		{
			if (TimestampDifferenceExceeds(entry->load_started, now,
									   cache_load_lease_ms()))
				expired_loading++;
			else
				loading++;
		}
	}
	hash_seq_init(&relation_sequence, pglc_relation_hash);
	while ((relation_state = hash_seq_search(&relation_sequence)) != NULL)
	{
		relation_states++;
		if (relation_state->dirty_writers > 0)
			dirty_relations++;
		if (relation_state->pending_forget)
			pending_forget++;
	}
	global_dirty_writers = pglc_shared->global_dirty_writers;
	total = hash_get_num_entries(pglc_cache_hash);
	LWLockRelease(pglc_shared->lock);
	cache_hits = pg_atomic_read_u64(&pglc_shared->cache_hits);
	cache_misses = pg_atomic_read_u64(&pglc_shared->cache_misses);
	negative_hits = pg_atomic_read_u64(&pglc_shared->negative_hits);
	pglc_read_sql_counter_snapshot(&sql_counters);
	sql_cache_hits = sql_counters.hits;
	sql_cache_misses = sql_counters.misses;
	sql_cache_fills = sql_counters.fills;
	sql_cache_bypasses = sql_counters.bypasses;
	database_reads = pg_atomic_read_u64(&pglc_shared->database_reads);
	database_writes = pg_atomic_read_u64(&pglc_shared->database_writes);
	invalidations = pg_atomic_read_u64(&pglc_shared->invalidations);
	evictions = pg_atomic_read_u64(&pglc_shared->evictions);
	singleflight_leaders =
		pg_atomic_read_u64(&pglc_shared->singleflight_leaders);
	singleflight_waiters =
		pg_atomic_read_u64(&pglc_shared->singleflight_waiters);
	singleflight_reuses =
		pg_atomic_read_u64(&pglc_shared->singleflight_reuses);
	singleflight_timeouts =
		pg_atomic_read_u64(&pglc_shared->singleflight_timeouts);
	active_clients = pg_atomic_read_u64(&pglc_shared->active_clients);
	rejected_connections =
		pg_atomic_read_u64(&pglc_shared->rejected_connections);
	authentication_failures =
		pg_atomic_read_u64(&pglc_shared->authentication_failures);
	protocol_errors = pg_atomic_read_u64(&pglc_shared->protocol_errors);
	output_backpressure_events =
		pg_atomic_read_u64(&pglc_shared->output_backpressure_events);
	slow_client_drops =
		pg_atomic_read_u64(&pglc_shared->slow_client_drops);
	worker_starts = pg_atomic_read_u64(&pglc_shared->worker_starts);
	workers_with_incomplete_mappings =
		pglc_workers_without_current_mappings();

	expanded.data = psprintf(
		"{\"entries\":" UINT64_FORMAT
		",\"positive_entries\":" UINT64_FORMAT
		",\"negative_entries\":" UINT64_FORMAT
		",\"dirty_entries\":" UINT64_FORMAT
		",\"loading_entries\":" UINT64_FORMAT
		",\"expired_loading_entries\":" UINT64_FORMAT
		",\"dirty_relations\":" UINT64_FORMAT
		",\"relation_states\":" UINT64_FORMAT
		",\"pending_forget\":" UINT64_FORMAT
		",\"global_dirty_writers\":%u"
		",\"store_size\":" UINT64_FORMAT
		",\"cache_hits\":" UINT64_FORMAT
		",\"cache_misses\":" UINT64_FORMAT
		",\"negative_hits\":" UINT64_FORMAT
		",\"sql_cache_hits\":" UINT64_FORMAT
		",\"sql_cache_misses\":" UINT64_FORMAT
		",\"sql_cache_fills\":" UINT64_FORMAT
		",\"sql_cache_bypasses\":" UINT64_FORMAT
		",\"database_reads\":" UINT64_FORMAT
		",\"database_writes\":" UINT64_FORMAT
		",\"invalidations\":" UINT64_FORMAT
		",\"evictions\":" UINT64_FORMAT
		",\"singleflight_leaders\":" UINT64_FORMAT
		",\"singleflight_waiters\":" UINT64_FORMAT
		",\"singleflight_reuses\":" UINT64_FORMAT
		",\"singleflight_timeouts\":" UINT64_FORMAT
		",\"active_clients\":" UINT64_FORMAT
		",\"rejected_connections\":" UINT64_FORMAT
		",\"authentication_failures\":" UINT64_FORMAT
		",\"protocol_errors\":" UINT64_FORMAT
		",\"output_backpressure_events\":" UINT64_FORMAT
		",\"slow_client_drops\":" UINT64_FORMAT
		",\"worker_starts\":" UINT64_FORMAT
		",\"cache_hit\":" UINT64_FORMAT
		",\"cache_miss\":" UINT64_FORMAT
		",\"cache_evict\":" UINT64_FORMAT
		",\"sql_gets\":" UINT64_FORMAT "}",
		total, positive, negative, dirty, loading, expired_loading,
		dirty_relations,
		relation_states, pending_forget,
		global_dirty_writers, positive + negative,
		cache_hits, cache_misses, negative_hits,
		sql_cache_hits, sql_cache_misses, sql_cache_fills,
		sql_cache_bypasses,
		database_reads, database_writes, invalidations, evictions,
		singleflight_leaders, singleflight_waiters,
		singleflight_reuses, singleflight_timeouts,
		active_clients, rejected_connections, authentication_failures,
		protocol_errors, output_backpressure_events, slow_client_drops,
		worker_starts,
		cache_hits, cache_misses, evictions, database_reads);
	expanded.len = strlen(expanded.data);
	expanded.maxlen = expanded.len + 1;
	expanded.cursor = 0;
	Assert(expanded.len > 0 && expanded.data[expanded.len - 1] == '}');
	expanded.data[--expanded.len] = '\0';
	appendStringInfo(
		&expanded,
		",\"cache_capacity\":%d"
		",\"relation_state_capacity\":%d"
		",\"max_clients\":%d"
		",\"max_clients_per_worker\":%d"
		",\"client_slots\":%d"
		",\"resp_enabled\":%d"
		",\"peak_active_clients\":" UINT64_FORMAT
		",\"client_limit_rejections\":" UINT64_FORMAT
		",\"workers_configured\":%d"
		",\"workers_running\":" UINT64_FORMAT
		",\"workers_with_incomplete_mappings\":" UINT64_FORMAT
		",\"shared_memory_bytes\":%zu"
		",\"worker_memory_bytes\":%zu"
		",\"estimated_memory_bytes\":%zu"
		",\"memory_budget_bytes\":%zu"
		",\"dirty_memory_limit_bytes\":%zu"
		",\"cache_admission_rejections\":" UINT64_FORMAT
		",\"relation_state_admission_rejections\":" UINT64_FORMAT
		",\"dirty_key_limit_fallbacks\":" UINT64_FORMAT
		",\"mapping_reload_attempts\":" UINT64_FORMAT
		",\"mapping_reload_failures\":" UINT64_FORMAT
		",\"mapping_reload_incomplete_retries\":" UINT64_FORMAT
		/* RESP STAT aliases retained for client compatibility. */
		",\"store_memory\":%zu"
		",\"client_connect\":" UINT64_FORMAT
		",\"client_disconnect\":" UINT64_FORMAT
		",\"client_requests\":" UINT64_FORMAT
		",\"client_request_errors\":" UINT64_FORMAT
		",\"client_mget_keys\":" UINT64_FORMAT
		",\"client_sets\":" UINT64_FORMAT
		",\"client_dels\":" UINT64_FORMAT
		",\"cache_hit_in_main\":" UINT64_FORMAT
		",\"cache_neg_write_count\":" UINT64_FORMAT
		",\"cache_invalidate_entry\":" UINT64_FORMAT
		",\"cache_invalidate_table\":" UINT64_FORMAT
		",\"pass_to_main\":" UINT64_FORMAT
		",\"sql_meta\":" UINT64_FORMAT
		",\"sql_sets\":" UINT64_FORMAT
		",\"sql_dels\":" UINT64_FORMAT
		",\"sql_result_reuses\":" UINT64_FORMAT "}",
		pglc_cache_entries, pglc_relation_states,
		pglc_port == 0 ? 0 : pglc_max_clients,
		pglc_port == 0 ? 0 : pglc_max_clients_per_worker,
		pglc_port == 0 ? 0 :
			pglc_worker_count * pglc_max_clients_per_worker,
		pglc_port == 0 ? 0 : 1,
		pg_atomic_read_u64(&pglc_shared->peak_active_clients),
		pg_atomic_read_u64(&pglc_shared->client_limit_rejections),
		pglc_port == 0 ? 0 : pglc_worker_count,
		pg_atomic_read_u64(&pglc_shared->active_workers),
		workers_with_incomplete_mappings,
		pglc_shared_memory_bytes(), pglc_worker_memory_bytes(),
		pglc_estimated_memory_bytes(),
		mul_size((Size) pglc_memory_budget_mb, (Size) 1024 * 1024),
		hash_estimate_size(pglc_max_dirty_keys,
						   sizeof(PgLocalCacheLocalDirtyEntry)),
		pg_atomic_read_u64(&pglc_shared->cache_admission_rejections),
		pg_atomic_read_u64(
			&pglc_shared->relation_state_admission_rejections),
		pg_atomic_read_u64(&pglc_shared->dirty_key_limit_fallbacks),
		pg_atomic_read_u64(&pglc_shared->mapping_reload_attempts),
		pg_atomic_read_u64(&pglc_shared->mapping_reload_failures),
		pg_atomic_read_u64(
			&pglc_shared->mapping_reload_incomplete_retries),
		mul_size((Size) (positive + negative),
				 sizeof(PgLocalCacheCacheEntry)),
		pg_atomic_read_u64(&pglc_shared->client_connects),
		pg_atomic_read_u64(&pglc_shared->client_disconnects),
		pg_atomic_read_u64(&pglc_shared->client_requests),
		pg_atomic_read_u64(&pglc_shared->client_request_errors),
		pg_atomic_read_u64(&pglc_shared->client_mget_keys),
		pg_atomic_read_u64(&pglc_shared->client_sets),
		pg_atomic_read_u64(&pglc_shared->client_dels),
		/* Every RESP hit comes from the shared/global hash in this design. */
		cache_hits,
		pg_atomic_read_u64(&pglc_shared->negative_writes),
		pg_atomic_read_u64(&pglc_shared->key_invalidations),
		pg_atomic_read_u64(&pglc_shared->table_invalidations),
		pg_atomic_read_u64(&pglc_shared->pass_to_main),
		pg_atomic_read_u64(&pglc_shared->mapping_reload_attempts),
		pg_atomic_read_u64(&pglc_shared->sql_sets),
		pg_atomic_read_u64(&pglc_shared->sql_dels),
		pg_atomic_read_u64(&pglc_shared->singleflight_reuses));
	return expanded.data;
}

char *
pglc_metrics_json(void)
{
	StringInfoData result;
	PgLocalCacheSqlCounterSnapshot sql_counters;
	uint64		entries;
	uint64		relation_states;
	uint64		global_dirty_writers;
	uint64		workers_with_incomplete_mappings;

	pglc_require_preload();
	LWLockAcquire(pglc_shared->lock, LW_SHARED);
	entries = hash_get_num_entries(pglc_cache_hash);
	relation_states = hash_get_num_entries(pglc_relation_hash);
	global_dirty_writers = pglc_shared->global_dirty_writers;
	LWLockRelease(pglc_shared->lock);
	workers_with_incomplete_mappings =
		pglc_workers_without_current_mappings();
	pglc_read_sql_counter_snapshot(&sql_counters);

	initStringInfo(&result);
	appendStringInfo(
		&result,
		"{\"up\":1"
		",\"cache_capacity\":%d"
		",\"entries\":" UINT64_FORMAT
		",\"relation_states\":" UINT64_FORMAT
		",\"relation_state_capacity\":%d"
		",\"global_dirty_writers\":" UINT64_FORMAT
		",\"active_clients\":" UINT64_FORMAT
		",\"peak_active_clients\":" UINT64_FORMAT
		",\"max_clients\":%d"
		",\"client_slots\":%d"
		",\"workers_configured\":%d"
		",\"workers_running\":" UINT64_FORMAT
		",\"workers_with_incomplete_mappings\":" UINT64_FORMAT
		",\"shared_memory_bytes\":%zu"
		",\"worker_memory_bytes\":%zu"
		",\"estimated_memory_bytes\":%zu"
		",\"memory_budget_bytes\":%zu",
		pglc_cache_entries, entries, relation_states, pglc_relation_states,
		global_dirty_writers,
		pg_atomic_read_u64(&pglc_shared->active_clients),
		pg_atomic_read_u64(&pglc_shared->peak_active_clients),
		pglc_port == 0 ? 0 : pglc_max_clients,
		pglc_port == 0 ? 0 :
			pglc_worker_count * pglc_max_clients_per_worker,
		pglc_port == 0 ? 0 : pglc_worker_count,
		pg_atomic_read_u64(&pglc_shared->active_workers),
		workers_with_incomplete_mappings,
		pglc_shared_memory_bytes(), pglc_worker_memory_bytes(),
		pglc_estimated_memory_bytes(),
		mul_size((Size) pglc_memory_budget_mb, (Size) 1024 * 1024));

#define PGLC_APPEND_METRIC_COUNTER(json_name, field_name) \
	appendStringInfo(&result, ",\"" json_name "\":" UINT64_FORMAT, \
					 pg_atomic_read_u64(&pglc_shared->field_name))
	PGLC_APPEND_METRIC_COUNTER("cache_hits_total", cache_hits);
	PGLC_APPEND_METRIC_COUNTER("cache_misses_total", cache_misses);
	PGLC_APPEND_METRIC_COUNTER("negative_hits_total", negative_hits);
	appendStringInfo(&result, ",\"sql_cache_hits_total\":" UINT64_FORMAT,
					 sql_counters.hits);
	appendStringInfo(&result, ",\"sql_cache_misses_total\":" UINT64_FORMAT,
					 sql_counters.misses);
	appendStringInfo(&result, ",\"sql_cache_fills_total\":" UINT64_FORMAT,
					 sql_counters.fills);
	appendStringInfo(&result, ",\"sql_cache_bypasses_total\":" UINT64_FORMAT,
					 sql_counters.bypasses);
	PGLC_APPEND_METRIC_COUNTER("database_reads_total", database_reads);
	PGLC_APPEND_METRIC_COUNTER("database_writes_total", database_writes);
	PGLC_APPEND_METRIC_COUNTER("invalidations_total", invalidations);
	PGLC_APPEND_METRIC_COUNTER("evictions_total", evictions);
	PGLC_APPEND_METRIC_COUNTER("singleflight_leaders_total", singleflight_leaders);
	PGLC_APPEND_METRIC_COUNTER("singleflight_waiters_total", singleflight_waiters);
	PGLC_APPEND_METRIC_COUNTER("singleflight_reuses_total", singleflight_reuses);
	PGLC_APPEND_METRIC_COUNTER("singleflight_timeouts_total", singleflight_timeouts);
	PGLC_APPEND_METRIC_COUNTER("rejected_connections_total", rejected_connections);
	PGLC_APPEND_METRIC_COUNTER("client_limit_rejections_total", client_limit_rejections);
	PGLC_APPEND_METRIC_COUNTER("authentication_failures_total", authentication_failures);
	PGLC_APPEND_METRIC_COUNTER("protocol_errors_total", protocol_errors);
	PGLC_APPEND_METRIC_COUNTER("output_backpressure_events_total", output_backpressure_events);
	PGLC_APPEND_METRIC_COUNTER("slow_client_drops_total", slow_client_drops);
	PGLC_APPEND_METRIC_COUNTER("worker_starts_total", worker_starts);
	PGLC_APPEND_METRIC_COUNTER("dirty_key_limit_fallbacks_total", dirty_key_limit_fallbacks);
	PGLC_APPEND_METRIC_COUNTER("mapping_reload_failures_total", mapping_reload_failures);
	PGLC_APPEND_METRIC_COUNTER("mapping_reload_incomplete_retries_total", mapping_reload_incomplete_retries);
#undef PGLC_APPEND_METRIC_COUNTER
	appendStringInfoChar(&result, '}');
	return result.data;
}

Datum
pg_local_cache_stats(PG_FUNCTION_ARGS)
{
	char	   *json = pglc_stats_json();

	PG_RETURN_DATUM(DirectFunctionCall1(jsonb_in, CStringGetDatum(json)));
}

Datum
pg_local_cache_metrics_json(PG_FUNCTION_ARGS)
{
	char	   *json = pglc_metrics_json();

	PG_RETURN_DATUM(DirectFunctionCall1(jsonb_in, CStringGetDatum(json)));
}
