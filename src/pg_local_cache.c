/* SPDX-License-Identifier: MIT */
#include "postgres.h"

#include <limits.h>
#include <stddef.h>
#include <stdlib.h>

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
int			pglc_lock_partitions = 64;
static int	pglc_active_lock_partitions = 0;
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
bool		pglc_enabled = true;
bool		pglc_allow_plaintext_network = false;
bool		pglc_tls = false;
int			pglc_tls_min_protocol_version = 0;
char	   *pglc_tls_cert_file = NULL;
char	   *pglc_tls_key_file = NULL;
char	   *pglc_tls_ca_file = NULL;

PgLocalCacheSharedState *pglc_shared = NULL;
HTAB	   *pglc_relation_hash = NULL;
static HTAB *pglc_cache_hashes[PGLC_MAX_LOCK_PARTITIONS];
static PgLocalCacheRelationSlot *pglc_relation_slots = NULL;
static int pglc_worker_slot = -1;

static char *pglc_binary_version = NULL;
static char *pglc_binary_build_id = NULL;
#ifdef PGLC_TEST_HOOKS
static char *pglc_test_pause_point = NULL;
static int pglc_test_barrier_relation_oid = 0;
static bool pglc_test_abort_after_reservation = false;
static int pglc_test_partition_lock_depth = 0;
#endif

static const struct config_enum_entry pglc_tls_protocol_options[] =
{
	{"TLSv1.2", 12, false},
	{"TLSv1.3", 13, false},
	{NULL, 0, false}
};

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
	bool		shared_relation_reserved;
	bool		shared_relation_fence_published;
	bool		shared_identity_pin;
	uint32		shared_slot;
	uint64		shared_slot_generation;
	uint64		shared_relation_incarnation;
	uint64		target_relation_incarnation;
} PgLocalCacheLocalDirtyEntry;

static HTAB *local_dirty_hash = NULL;
static PgLocalCacheLocalDirtyEntry **local_dirty_ordered = NULL;
static Size local_dirty_ordered_count = 0;
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
#ifdef PGLC_TEST_HOOKS
PG_FUNCTION_INFO_V1(pg_local_cache_test_collect_key);
PG_FUNCTION_INFO_V1(pg_local_cache_test_partition);
PG_FUNCTION_INFO_V1(pg_local_cache_test_hash_bucket);
PG_FUNCTION_INFO_V1(pg_local_cache_test_relation_incarnation);
PG_FUNCTION_INFO_V1(pg_local_cache_test_relation_identity_pins);
PG_FUNCTION_INFO_V1(pg_local_cache_test_recreate_relation_state);
PG_FUNCTION_INFO_V1(pg_local_cache_test_collect_global);
PG_FUNCTION_INFO_V1(pg_local_cache_test_abort_after_reservation);
PG_FUNCTION_INFO_V1(pg_local_cache_test_corrupt_value_len);
PG_FUNCTION_INFO_V1(pg_local_cache_test_partition_lock_violations);
#endif

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
static uint32 pglc_cache_partition(const PgLocalCacheCacheKey *key);
static int pglc_cache_partition_count(void);
static Size pglc_cache_entries_per_partition(int partitions);
static int evict_cache_entries(uint32 partition);
static void
pglc_define_gucs(void)
{
	DefineCustomBoolVariable("pg_local_cache.enabled",
							 "Enable RESP cache lookups and fills.",
							 NULL,
							 &pglc_enabled,
							 true,
							 PGC_SIGHUP,
							 0,
							 NULL,
							 NULL,
							 NULL);

	DefineCustomBoolVariable("pg_local_cache.allow_plaintext_network",
							 "Allow plaintext RESP on non-loopback IPv4 addresses when TLS is disabled.",
							 NULL,
							 &pglc_allow_plaintext_network,
							 false,
							 PGC_POSTMASTER,
							 0,
							 NULL,
							 NULL,
							 NULL);

	DefineCustomBoolVariable("pg_local_cache.tls",
							 "Enable native TLS for the RESP listener.",
							 NULL,
							 &pglc_tls,
							 false,
							 PGC_POSTMASTER,
							 0,
							 NULL,
							 NULL,
							 NULL);

	DefineCustomStringVariable("pg_local_cache.tls_cert_file",
							   "PEM certificate chain file for native RESP TLS.",
							   NULL,
							   &pglc_tls_cert_file,
							   "",
							   PGC_POSTMASTER,
							   0,
							   NULL,
							   NULL,
							   NULL);

	DefineCustomStringVariable("pg_local_cache.tls_key_file",
							   "PEM private key file for native RESP TLS.",
							   NULL,
							   &pglc_tls_key_file,
							   "",
							   PGC_POSTMASTER,
							   0,
							   NULL,
							   NULL,
							   NULL);

	DefineCustomStringVariable("pg_local_cache.tls_ca_file",
							   "CA file used to require and verify RESP TLS client certificates.",
							   NULL,
							   &pglc_tls_ca_file,
							   "",
							   PGC_POSTMASTER,
							   0,
							   NULL,
							   NULL,
							   NULL);

	DefineCustomEnumVariable("pg_local_cache.tls_min_protocol_version",
							  "Minimum protocol version accepted by the RESP TLS listener.",
							  NULL,
							  &pglc_tls_min_protocol_version,
							  12,
							  pglc_tls_protocol_options,
							  PGC_POSTMASTER,
							  0,
							  NULL,
							  NULL,
							  NULL);

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

	DefineCustomIntVariable("pg_local_cache.lock_partitions",
							"Maximum independent shared cache lock partitions; small caches use fewer.",
							NULL,
							&pglc_lock_partitions,
							64,
							16,
							PGLC_MAX_LOCK_PARTITIONS,
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

#ifdef PGLC_TEST_HOOKS
	DefineCustomStringVariable("pg_local_cache.test_pause_point",
							   "Test-only RESP worker relation-lock barrier point.",
							   NULL,
							   &pglc_test_pause_point,
							   "",
							   PGC_SIGHUP,
							   GUC_SUPERUSER_ONLY,
							   NULL,
							   NULL,
							   NULL);

	DefineCustomIntVariable("pg_local_cache.test_barrier_relation",
							"Test-only relation OID used by RESP worker barriers.",
							NULL,
							&pglc_test_barrier_relation_oid,
							0,
							0,
							INT_MAX,
							PGC_SIGHUP,
							GUC_SUPERUSER_ONLY,
							NULL,
							NULL,
							NULL);
#endif

#if PG_VERSION_NUM >= 150000
	MarkGUCPrefixReserved("pg_local_cache");
#else
	EmitWarningsOnPlaceholders("pg_local_cache");
#endif
}

#ifdef PGLC_TEST_HOOKS
static void
pglc_test_pause_at(const char *point)
{
	LOCKTAG		tag;

	if (pglc_test_pause_point == NULL ||
		strcmp(pglc_test_pause_point, point) != 0 ||
		pglc_test_barrier_relation_oid <= 0)
		return;

	SET_LOCKTAG_RELATION(tag, MyDatabaseId,
						 (Oid) pglc_test_barrier_relation_oid);
	(void) LockAcquire(&tag, AccessShareLock, true, false);
	LockRelease(&tag, AccessShareLock, true);
}
#endif

static void
pglc_partition_lock_acquire(uint32 partition, LWLockMode mode)
{
#ifdef PGLC_TEST_HOOKS
	pglc_test_pause_at("before_partition_acquire");
	if (pglc_test_partition_lock_depth != 0)
		(void) pg_atomic_fetch_add_u64(
			&pglc_shared->test_partition_lock_violations, 1);
#endif
	LWLockAcquire(pglc_shared->partitions[partition].lock, mode);
#ifdef PGLC_TEST_HOOKS
	pglc_test_partition_lock_depth++;
#endif
}

static void
pglc_partition_lock_release(uint32 partition)
{
	LWLockRelease(pglc_shared->partitions[partition].lock);
#ifdef PGLC_TEST_HOOKS
	if (pglc_test_partition_lock_depth <= 0)
		(void) pg_atomic_fetch_add_u64(
			&pglc_shared->test_partition_lock_violations, 1);
	else
		pglc_test_partition_lock_depth--;
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

static Size
pglc_addin_shmem_bytes(void)
{
	Size		size = MAXALIGN(sizeof(PgLocalCacheSharedState));
	int			partitions = pglc_cache_partition_count();
	Size		per_partition = pglc_cache_entries_per_partition(partitions);
	int			partition;

	for (partition = 0; partition < partitions; partition++)
		size = add_size(size,
						hash_estimate_size(per_partition,
										   sizeof(PgLocalCacheCacheEntry)));
	size = add_size(size,
					hash_estimate_size(pglc_relation_states,
									   sizeof(PgLocalCacheRelationState)));
	size = add_size(size,
					mul_size((Size) pglc_relation_states,
							 sizeof(PgLocalCacheRelationSlot)));
	return size;
}

/*
 * cache_entries is the hard global maximum. Each independently bounded
 * partition gets 25% headroom so ordinary hash variance does not force
 * eviction before the configured aggregate working set is reached. This
 * exact per-partition cap is also used by the shared-memory estimate.
 */
static Size
pglc_cache_entries_per_partition(int partitions)
{
	Size		target = mul_size((Size) pglc_cache_entries, (Size) 5);
	Size		divisor = mul_size((Size) partitions, (Size) 4);

	return add_size(target, divisor - 1) / divisor;
}

/*
 * Dynahash allocates entries in batches of at least 32 per table. Avoid
 * multiplying that minimum across partitions when the configured cache is
 * small; lock_partitions remains an upper bound.
 */
static int
pglc_cache_partition_count(void)
{
	int			partitions;

	if (pglc_active_lock_partitions != 0)
		return pglc_active_lock_partitions;
	partitions = pglc_lock_partitions;

	while (partitions > 16 &&
		   pglc_cache_entries_per_partition(partitions) < 32)
		partitions >>= 1;
	pglc_active_lock_partitions = partitions;
	return pglc_active_lock_partitions;
}

Size
pglc_shared_memory_bytes(void)
{
	return add_size(pglc_addin_shmem_bytes(),
					mul_size((Size) pglc_cache_partition_count() + 1,
							 sizeof(LWLockPadded)));
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
	if (pglc_lock_partitions < 16 ||
		pglc_lock_partitions > PGLC_MAX_LOCK_PARTITIONS ||
		(pglc_lock_partitions & (pglc_lock_partitions - 1)) != 0)
		ereport(FATAL,
				(errmsg("pg_local_cache.lock_partitions must be a power of two from 16 through 256")));

	budget_bytes = mul_size((Size) pglc_memory_budget_mb,
							(Size) 1024 * 1024);
	estimated_bytes = pglc_estimated_memory_bytes();
	if (estimated_bytes > budget_bytes)
			ereport(FATAL,
					 (errmsg("pg_local_cache estimated memory exceeds its configured budget"),
					  errdetail("Estimated deterministic extension memory is %zu bytes; pg_local_cache.memory_budget_mb allows %zu bytes.",
								estimated_bytes, budget_bytes),
					 errhint("Raise pg_local_cache.memory_budget_mb; lower cache_entries, relation_states, workers, or max_clients_per_worker; or lower PostgreSQL backend limits.")));
}

static void
pglc_shmem_request(void)
{
#if PG_VERSION_NUM >= 150000
	if (previous_shmem_request_hook)
		previous_shmem_request_hook();
#endif

	RequestAddinShmemSpace(pglc_addin_shmem_bytes());
	RequestNamedLWLockTranche("pg_local_cache",
						  pglc_cache_partition_count() + 1);
}

static void
pglc_shmem_startup(void)
{
	bool		found;
	HASHCTL		control;
	int		worker_index;
	int		partition;
	int			partitions = pglc_cache_partition_count();
	Size		per_partition = pglc_cache_entries_per_partition(partitions);
	char		name[64];

	if (previous_shmem_startup_hook)
		previous_shmem_startup_hook();

	LWLockAcquire(AddinShmemInitLock, LW_EXCLUSIVE);

	pglc_shared = ShmemInitStruct("pg_local_cache shared state",
								 sizeof(PgLocalCacheSharedState),
								 &found);
	if (!found)
	{
		memset(pglc_shared, 0, sizeof(PgLocalCacheSharedState));
		pglc_shared->registry_lock =
			&(GetNamedLWLockTranche("pg_local_cache"))[0].lock;
		for (partition = 0; partition < partitions; partition++)
		{
			pglc_shared->partitions[partition].lock =
				&(GetNamedLWLockTranche("pg_local_cache"))[partition + 1].lock;
			pglc_shared->partitions[partition].capacity =
				(uint32) per_partition;
		}
		pg_atomic_init_u64(&pglc_shared->clock, 0);
		pg_atomic_init_u64(&pglc_shared->relation_incarnation_counter, 0);
		pg_atomic_init_u64(&pglc_shared->global_version, 0);
		pg_atomic_init_u64(&pglc_shared->global_epoch, 0);
		pg_atomic_init_u64(&pglc_shared->global_dirty_writers, 0);
		pg_atomic_init_u64(&pglc_shared->cache_entry_count, 0);
		pg_atomic_init_u64(&pglc_shared->config_generation, 1);
		pg_atomic_init_u64(&pglc_shared->cache_hits, 0);
		pg_atomic_init_u64(&pglc_shared->cache_misses, 0);
		pg_atomic_init_u64(&pglc_shared->negative_hits, 0);
		pg_atomic_init_u64(&pglc_shared->negative_writes, 0);
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
		pg_atomic_init_u64(&pglc_shared->tls_handshakes, 0);
		pg_atomic_init_u64(&pglc_shared->tls_handshake_failures, 0);
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
#ifdef PGLC_TEST_HOOKS
		pg_atomic_init_u64(&pglc_shared->test_partition_lock_violations, 0);
#endif
		for (worker_index = 0; worker_index < PGLC_MAX_STATS_SHARDS;
			 worker_index++)
		{
			PgLocalCacheWorkerStats *stats =
				&pglc_shared->stats_shards[worker_index];

			pg_atomic_init_u64(&stats->cache_hits, 0);
			pg_atomic_init_u64(&stats->cache_misses, 0);
			pg_atomic_init_u64(&stats->negative_hits, 0);
			pg_atomic_init_u64(&stats->negative_writes, 0);
			pg_atomic_init_u64(&stats->database_reads, 0);
			pg_atomic_init_u64(&stats->client_requests, 0);
			pg_atomic_init_u64(&stats->client_request_errors, 0);
			pg_atomic_init_u64(&stats->client_mget_keys, 0);
			pg_atomic_init_u64(&stats->client_sets, 0);
			pg_atomic_init_u64(&stats->client_dels, 0);
			pg_atomic_init_u64(&stats->pass_to_main, 0);
			pg_atomic_init_u64(&stats->database_writes, 0);
			pg_atomic_init_u64(&stats->sql_sets, 0);
			pg_atomic_init_u64(&stats->sql_dels, 0);
			pg_atomic_init_u64(&stats->cache_admission_rejections, 0);
			pg_atomic_init_u64(&stats->invalidations, 0);
			pg_atomic_init_u64(&stats->key_invalidations, 0);
			pg_atomic_init_u64(&stats->table_invalidations, 0);
			pg_atomic_init_u64(&stats->evictions, 0);
			pg_atomic_init_u64(&stats->singleflight_leaders, 0);
			pg_atomic_init_u64(&stats->singleflight_waiters, 0);
			pg_atomic_init_u64(&stats->singleflight_reuses, 0);
			pg_atomic_init_u64(&stats->singleflight_timeouts, 0);
		}
	}

	pglc_relation_slots = ShmemInitStruct("pg_local_cache relation slots",
											 mul_size((Size) pglc_relation_states,
											  sizeof(PgLocalCacheRelationSlot)),
											 &found);
	if (!found)
	{
		memset(pglc_relation_slots, 0,
			   mul_size((Size) pglc_relation_states,
						 sizeof(PgLocalCacheRelationSlot)));
		for (partition = 0; partition < pglc_relation_states; partition++)
		{
			pg_atomic_init_u64(&pglc_relation_slots[partition].generation, 0);
			pg_atomic_init_u64(&pglc_relation_slots[partition].incarnation, 0);
			pg_atomic_init_u64(&pglc_relation_slots[partition].version, 0);
			pg_atomic_init_u64(&pglc_relation_slots[partition].dirty_writers, 0);
		}
	}

	memset(&control, 0, sizeof(control));
	control.keysize = sizeof(PgLocalCacheCacheKey);
	control.entrysize = sizeof(PgLocalCacheCacheEntry);
	control.hash = pglc_cache_key_hash;
	control.match = pglc_cache_key_match;
	for (partition = 0; partition < partitions; partition++)
	{
		snprintf(name, sizeof(name), "pg_local_cache cache %d", partition);
		pglc_cache_hashes[partition] = ShmemInitHash(name,
												per_partition,
												per_partition,
												&control,
												HASH_ELEM | HASH_FUNCTION |
												HASH_COMPARE);
	}
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

void
pglc_require_preload(void)
{
	if (!pglc_was_preloaded || pglc_shared == NULL ||
		pglc_cache_hashes[0] == NULL || pglc_relation_hash == NULL ||
		pglc_relation_slots == NULL)
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

static void
pglc_worker_stat_add(pg_atomic_uint64 *fallback, Size offset, uint64 amount)
{
	int			shard;

	if (pglc_worker_slot >= 0 && pglc_worker_slot < PGLC_MAX_WORKERS)
		shard = pglc_worker_slot;
	else if (MyProc != NULL)
		shard = PGLC_MAX_WORKERS;
	else
	{
		(void) pg_atomic_fetch_add_u64(fallback, amount);
		return;
	}
	(void) pg_atomic_fetch_add_u64(
		(pg_atomic_uint64 *) ((char *) &pglc_shared->stats_shards[shard] + offset),
		amount);
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

static uint32
pglc_cache_partition(const PgLocalCacheCacheKey *key)
{
	uint32		hash;
	uint32		partition_bits = 0;
	uint32		partition_count = (uint32) pglc_cache_partition_count();

	Assert(partition_count >= 16 &&
		   (partition_count & (partition_count - 1)) == 0);
	while (partition_count > 1)
	{
		partition_bits++;
		partition_count >>= 1;
	}
	/* dynahash selects buckets from low hash bits; route partitions from high. */
	hash = pglc_cache_key_hash(key, sizeof(*key));
	return hash >> (32 - partition_bits);
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

static uint32
cache_partition_for(Oid database_oid, const char *nspace, const char *key)
{
	PgLocalCacheCacheKey cache_key;

	make_cache_key(&cache_key, database_oid, nspace, key, false);
	return pglc_cache_partition(&cache_key);
}

static void
make_relation_key(PgLocalCacheRelationKey *result, Oid database_oid,
				  const char *nspace)
{
	memset(result, 0, sizeof(*result));
	result->database_oid = database_oid;
	strlcpy(result->nspace, nspace, sizeof(result->nspace));
}

/* Compare/exchange is a full-barrier RMW, with overflow checked before change. */
static uint64
pglc_atomic_fetch_add_checked(pg_atomic_uint64 *counter, uint64 amount)
{
	uint64		observed = pg_atomic_read_u64(counter);

	for (;;)
	{
		uint64		expected = observed;

		if (observed > (uint64) -1 - amount)
			ereport(ERROR,
					(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
					 errmsg("pg_local_cache fence counter overflow")));
		if (pg_atomic_compare_exchange_u64(counter, &expected,
										 observed + amount))
			return observed;
		observed = expected;
	}
}

static uint64
pglc_atomic_fetch_sub_checked(pg_atomic_uint64 *counter, uint64 amount)
{
	uint64		observed = pg_atomic_read_u64(counter);

	for (;;)
	{
		uint64		expected = observed;

		if (observed < amount)
			ereport(ERROR,
					(errcode(ERRCODE_INTERNAL_ERROR),
					 errmsg("pg_local_cache fence counter underflow")));
		if (pg_atomic_compare_exchange_u64(counter, &expected,
										 observed - amount))
			return observed;
		observed = expected;
	}
}

/* Called under registry_lock.  Reused slots keep advancing both tags. */
static bool
initialize_relation_slot(PgLocalCacheRelationState *state, bool reuse)
{
	uint32		slot_index;
	PgLocalCacheRelationSlot *slot;
	uint64		generation;
	uint64		incarnation;
	uint64		previous_incarnation;
	uint64		version;

	if (reuse)
		slot_index = state->slot;
	else
	{
		for (slot_index = 0; slot_index < (uint32) pglc_relation_states;
			 slot_index++)
			if (!pglc_relation_slots[slot_index].in_use)
				break;
		if (slot_index == (uint32) pglc_relation_states)
			return false;
	}

	slot = &pglc_relation_slots[slot_index];
	generation = pglc_atomic_fetch_add_checked(&slot->generation, 1) + 1;
	incarnation = pglc_atomic_fetch_add_checked(
		&pglc_shared->relation_incarnation_counter, 1) + 1;
	previous_incarnation = pg_atomic_read_u64(&slot->incarnation);
	if (incarnation <= previous_incarnation)
		ereport(ERROR,
				(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
				 errmsg("pg_local_cache relation incarnation overflow")));
	(void) pglc_atomic_fetch_add_checked(&slot->incarnation,
									 incarnation - previous_incarnation);
	if (!reuse)
		slot->in_use = true;
	if (generation == 0 || incarnation == 0)
	{
		/* Exhausted identity slots stay poisoned and can never be reused. */
		slot->in_use = true;
		return false;
	}
	version = pg_atomic_read_u64(&slot->version);
	if (pg_atomic_read_u64(&slot->dirty_writers) != 0)
	{
		/* A slot with an outstanding fence cannot be safely recycled. */
		slot->in_use = true;
		return false;
	}
	if (version != 0)
		(void) pglc_atomic_fetch_sub_checked(&slot->version, version);
	state->slot = slot_index;
	state->slot_generation = generation;
	return true;
}

static void
release_relation_slot(PgLocalCacheRelationState *state)
{
	if (state->slot < (uint32) pglc_relation_states)
	{
		PgLocalCacheRelationSlot *slot = &pglc_relation_slots[state->slot];

		uint64		previous = pglc_atomic_fetch_add_checked(&slot->generation, 1);

		/* A wrapped generation poisons the slot instead of permitting ABA. */
		slot->in_use = previous == (uint64) -1;
	}
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
	if (state != NULL && create && state->pending_forget)
	{
		/* A forgotten identity stays reserved until every publisher releases it. */
		if (state->identity_pins != 0 ||
			pg_atomic_read_u64(
				&pglc_relation_slots[state->slot].dirty_writers) != 0)
			return NULL;
		release_relation_slot(state);
		(void) hash_search(pglc_relation_hash, &key, HASH_REMOVE, NULL);
		state = NULL;
		found = false;
	}
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
		if (!initialize_relation_slot(state, false))
		{
			(void) hash_search(pglc_relation_hash, &key, HASH_REMOVE, NULL);
			return NULL;
		}
	}
	else if (state != NULL && create && OidIsValid(relation_oid) &&
			 state->relation_oid != relation_oid)
	{
		/* Published handles own this identity until their finish callback. */
		if (state->identity_pins != 0 ||
			!initialize_relation_slot(state, true))
			return NULL;

		state->relation_oid = relation_oid;
		state->pending_forget = false;
	}
	return state;
}

/* Called under registry_lock.  Every acquired identity pin has one release. */
static void
release_relation_identity_pin(PgLocalCacheRelationState *state)
{
	Assert(state != NULL);
	Assert(state->identity_pins > 0);
	if (state->identity_pins == 0)
		ereport(ERROR,
				(errcode(ERRCODE_INTERNAL_ERROR),
				 errmsg("pg_local_cache relation identity pin underflow")));
	state->identity_pins--;
}

bool
pglc_resolve_mapping_slot(PgLocalCacheMapping *mapping)
{
	PgLocalCacheRelationState *state;
	bool		resolved = false;

	pglc_require_preload();
	mapping->relation_slot = UINT32_MAX;
	mapping->relation_slot_generation = 0;
	LWLockAcquire(pglc_shared->registry_lock, LW_EXCLUSIVE);
	state = get_relation_state(MyDatabaseId, mapping->relation_oid,
							   mapping->nspace, true);
	if (state != NULL && state->relation_oid == mapping->relation_oid &&
		!state->pending_forget)
	{
		mapping->relation_slot = state->slot;
		mapping->relation_slot_generation = state->slot_generation;
		resolved = true;
	}
	LWLockRelease(pglc_shared->registry_lock);
	return resolved;
}

bool
pglc_mapping_slot_is_current(const PgLocalCacheMapping *mapping)
{
	return mapping->relation_slot < (uint32) pglc_relation_states &&
		mapping->relation_slot_generation == pg_atomic_read_u64(
			&pglc_relation_slots[mapping->relation_slot].generation);
}

static bool
cache_entry_is_current_slot_locked(PgLocalCacheCacheEntry *entry)
{
	PgLocalCacheRelationSlot *slot;

	if (entry->relation_slot >= (uint32) pglc_relation_states)
		return false;
	slot = &pglc_relation_slots[entry->relation_slot];
	return entry->valid &&
		entry->slot_generation == pg_atomic_read_u64(&slot->generation) &&
		entry->global_epoch ==
		pg_atomic_read_u64(&pglc_shared->global_epoch) &&
		entry->relation_incarnation ==
		pg_atomic_read_u64(&slot->incarnation) &&
		entry->relation_version == pg_atomic_read_u64(&slot->version);
}

typedef struct PgLocalCacheFenceSnapshot
{
	uint64		global_version;
	uint64		global_epoch;
	uint64		global_dirty_writers;
	uint64		relation_version;
	uint64		relation_incarnation;
	uint64		relation_dirty_writers;
	uint64		config_generation;
	uint64		slot_generation;
	uint64		key_version;
	uint64		key_dirty_writers;
	uint64		entry_global_epoch;
	uint64		entry_relation_version;
	uint64		entry_relation_incarnation;
	uint64		entry_slot_generation;
	uint32		relation_slot;
} PgLocalCacheFenceSnapshot;

/* Caller holds key's partition lock, shared or exclusive. */
static bool
read_fence_snapshot(const PgLocalCacheMapping *mapping,
					PgLocalCacheCacheEntry *entry,
					PgLocalCacheFenceSnapshot *snapshot)
{
	PgLocalCacheRelationSlot *slot;
	uint64		global_version_after;
	uint64		relation_version_after;
	uint64		config_generation_after;
	uint64		slot_generation_after;
	uint64		relation_incarnation_after;

	if (mapping->relation_slot >= (uint32) pglc_relation_states)
		return false;
	memset(snapshot, 0, sizeof(*snapshot));
	slot = &pglc_relation_slots[mapping->relation_slot];
	snapshot->global_version =
		pg_atomic_read_u64(&pglc_shared->global_version);
	snapshot->relation_version = pg_atomic_read_u64(&slot->version);
	snapshot->config_generation =
		pg_atomic_read_u64(&pglc_shared->config_generation);
	snapshot->slot_generation = pg_atomic_read_u64(&slot->generation);
	snapshot->relation_incarnation = pg_atomic_read_u64(&slot->incarnation);
	pg_read_barrier();
	snapshot->global_dirty_writers =
		pg_atomic_read_u64(&pglc_shared->global_dirty_writers);
	snapshot->relation_dirty_writers =
		pg_atomic_read_u64(&slot->dirty_writers);
	snapshot->global_epoch = pg_atomic_read_u64(&pglc_shared->global_epoch);
	snapshot->relation_slot = mapping->relation_slot;
	snapshot->key_version = entry != NULL ? entry->version : 0;
	snapshot->key_dirty_writers = entry != NULL ? entry->dirty_writers : 0;
	snapshot->entry_global_epoch = entry != NULL ? entry->global_epoch : 0;
	snapshot->entry_relation_version =
		entry != NULL ? entry->relation_version : 0;
	snapshot->entry_relation_incarnation =
		entry != NULL ? entry->relation_incarnation : 0;
	snapshot->entry_slot_generation =
		entry != NULL ? entry->slot_generation : 0;
#ifdef PGLC_TEST_HOOKS
	pglc_test_pause_at("after_snapshot_first_read");
#endif
	pg_read_barrier();
	global_version_after = pg_atomic_read_u64(&pglc_shared->global_version);
	relation_version_after = pg_atomic_read_u64(&slot->version);
	config_generation_after =
		pg_atomic_read_u64(&pglc_shared->config_generation);
	slot_generation_after = pg_atomic_read_u64(&slot->generation);
	relation_incarnation_after = pg_atomic_read_u64(&slot->incarnation);
	return snapshot->global_version == global_version_after &&
		snapshot->relation_version == relation_version_after &&
		snapshot->config_generation == config_generation_after &&
		snapshot->slot_generation == slot_generation_after &&
		snapshot->relation_incarnation == relation_incarnation_after &&
		snapshot->config_generation == mapping->config_generation &&
		snapshot->slot_generation == mapping->relation_slot_generation &&
		snapshot->relation_incarnation != 0 &&
		snapshot->global_dirty_writers == 0 &&
		snapshot->relation_dirty_writers == 0 &&
		snapshot->key_dirty_writers == 0;
}

static bool
fence_snapshots_equal(const PgLocalCacheFenceSnapshot *left,
				  const PgLocalCacheFenceSnapshot *right)
{
	return memcmp(left, right, sizeof(*left)) == 0;
}

static bool
fence_snapshot_matches_token(const PgLocalCacheFenceSnapshot *snapshot,
							 const PgLocalCacheReadToken *token)
{
	return snapshot->config_generation == token->config_generation &&
		snapshot->global_version == token->global_version &&
		snapshot->global_epoch == token->global_epoch &&
		snapshot->relation_version == token->relation_version &&
		snapshot->relation_incarnation == token->relation_incarnation &&
		snapshot->slot_generation == token->slot_generation &&
		snapshot->relation_slot == token->relation_slot &&
		snapshot->key_version == token->key_version;
}

static bool
fence_snapshot_fences_match_token(const PgLocalCacheFenceSnapshot *snapshot,
								 const PgLocalCacheReadToken *token)
{
	return snapshot->config_generation == token->config_generation &&
		snapshot->global_version == token->global_version &&
		snapshot->global_epoch == token->global_epoch &&
		snapshot->relation_version == token->relation_version &&
		snapshot->relation_incarnation == token->relation_incarnation &&
		snapshot->slot_generation == token->slot_generation &&
		snapshot->relation_slot == token->relation_slot;
}

static void
advance_global_version_locked(void)
{
	(void) pglc_atomic_fetch_add_checked(&pglc_shared->global_version, 1);
}

static uint64
next_entry_generation(uint32 partition)
{
	PgLocalCachePartition *cache_partition =
		&pglc_shared->partitions[partition];

	/* Only called under this partition's exclusive lock. */
	if (cache_partition->entry_generation == (uint64) -1)
		ereport(ERROR,
				(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
				 errmsg("pg_local_cache key generation overflow")));
	return ++cache_partition->entry_generation;
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
cache_load_is_active_locked(PgLocalCacheCacheEntry *entry, TimestampTz now,
								uint32 partition)
{
	if (!entry->loading)
		return false;
	if (!TimestampDifferenceExceeds(entry->load_started, now,
								cache_load_lease_ms()))
		return true;

	entry->loading = false;
	entry->load_id++;
	entry->version = next_entry_generation(partition);
	return false;
}

static int
evict_cache_entries(uint32 partition)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheCacheEntry *entry;
	PgLocalCachePartition *cache_partition = &pglc_shared->partitions[partition];
	HTAB	   *cache_hash = pglc_cache_hashes[partition];
	PgLocalCacheCacheKey victims[PGLC_EVICTION_BATCH];
	PgLocalCacheCacheKey candidates[PGLC_EVICTION_BATCH];
	uint64		candidate_access[PGLC_EVICTION_BATCH];
	uint32		initial_cursor = cache_partition->eviction_bucket_cursor;
	uint32		start_bucket;
	int			victim_count = 0;
	int			candidate_count = 0;
	int			removed = 0;
	int			scanned = 0;
	int			pass;
	int			i;
	bool		sample_limited = false;
	TimestampTz now = GetCurrentTimestamp();

	/*
	 * Scan only this partition.  Relation and global tags are atomic, so no
	 * registry lock is needed while the partition lock is held.
	 */
	for (pass = 0; pass < 2; pass++)
	{
		if (pass == 1 && initial_cursor == 0)
			break;
		start_bucket = pass == 0 ? initial_cursor : 0;
		hash_seq_init(&sequence, cache_hash);
		sequence.curBucket = start_bucket;
		while ((entry = hash_seq_search(&sequence)) != NULL)
		{
			uint64		last_access;
			int			position;

			scanned++;
			if (entry->dirty_writers == 0 &&
				!cache_load_is_active_locked(entry, now, partition))
			{
				if (!cache_entry_is_current_slot_locked(entry))
				{
					if (victim_count < PGLC_EVICTION_BATCH)
						victims[victim_count++] = entry->key;
				}
				else
				{
					last_access = pg_atomic_read_u64(&entry->last_access);
					if (candidate_count < PGLC_EVICTION_BATCH ||
						last_access < candidate_access[candidate_count - 1])
					{
						if (candidate_count < PGLC_EVICTION_BATCH)
						{
							position = candidate_count;
							candidate_count++;
						}
						else
							position = PGLC_EVICTION_BATCH - 1;
						while (position > 0 &&
							   candidate_access[position - 1] > last_access)
						{
							candidate_access[position] =
								candidate_access[position - 1];
							candidates[position] = candidates[position - 1];
							position--;
						}
						candidate_access[position] = last_access;
						candidates[position] = entry->key;
					}
				}
			}

			/* Finish the current chain before advancing the bucket cursor. */
			if (scanned >= PGLC_EVICTION_SAMPLE &&
				sequence.curEntry == NULL)
			{
				cache_partition->eviction_bucket_cursor = sequence.curBucket;
				hash_seq_term(&sequence);
				sample_limited = true;
				break;
			}
		}
		if (sample_limited)
			break;

		/* hash_seq_search reached the end and terminated the scan. */
		cache_partition->eviction_bucket_cursor = 0;
		if (victim_count > 0 || candidate_count > 0 || start_bucket == 0 ||
			scanned >= PGLC_EVICTION_SAMPLE)
			break;
	}

	entry = NULL;
	for (i = 0; i < candidate_count &&
		 victim_count < PGLC_EVICTION_BATCH; i++)
		victims[victim_count++] = candidates[i];

	for (i = 0; i < victim_count; i++)
	{
		if (hash_search(cache_hash, &victims[i], HASH_REMOVE, NULL) != NULL)
		{
			pglc_worker_stat_add(&pglc_shared->evictions,
							 offsetof(PgLocalCacheWorkerStats, evictions), 1);
			if (cache_partition->entry_count > 0)
				cache_partition->entry_count--;
			Assert(pg_atomic_read_u64(&pglc_shared->cache_entry_count) > 0);
			(void) pg_atomic_fetch_sub_u64(&pglc_shared->cache_entry_count, 1);
			removed++;
		}
	}
	return removed;
}

/*
 * Reserve one global cache slot before inserting. fetch_add is a full-barrier
 * RMW; undo reservations that observed a full cache before trying local
 * eviction. The count may briefly include concurrent reservations, so stats
 * clamp their reported value to cache_entries.
 */
static bool
reserve_cache_entry(void)
{
	uint64		previous = pg_atomic_fetch_add_u64(
			&pglc_shared->cache_entry_count, 1);

	if (previous < (uint64) pglc_cache_entries)
		return true;

	(void) pg_atomic_fetch_sub_u64(&pglc_shared->cache_entry_count, 1);
	return false;
}

static void
release_cache_entry(void)
{
	Assert(pg_atomic_read_u64(&pglc_shared->cache_entry_count) > 0);
	(void) pg_atomic_fetch_sub_u64(&pglc_shared->cache_entry_count, 1);
}

static PgLocalCacheCacheEntry *
get_cache_entry(Oid database_oid, Oid relation_oid,
				const char *nspace, const char *key, bool create)
{
	PgLocalCacheCacheKey cache_key;
	PgLocalCacheCacheEntry *entry;
	PgLocalCachePartition *cache_partition;
	HTAB	   *cache_hash;
	uint32		partition;
	bool		found;

	make_cache_key(&cache_key, database_oid, nspace, key, create);
	partition = pglc_cache_partition(&cache_key);
	cache_partition = &pglc_shared->partitions[partition];
	cache_hash = pglc_cache_hashes[partition];
	entry = hash_search(cache_hash, &cache_key, HASH_FIND, NULL);
	found = entry != NULL;
	if (entry == NULL && create)
	{
		/* Enforce the local bound; dynahash does not do so by default. */
		if (cache_partition->entry_count >= cache_partition->capacity &&
			!evict_cache_entries(partition))
		{
			pglc_worker_stat_add(
				&pglc_shared->cache_admission_rejections,
				offsetof(PgLocalCacheWorkerStats,
							 cache_admission_rejections), 1);
			return NULL;
		}
		while (!reserve_cache_entry())
		{
			if (evict_cache_entries(partition) == 0)
			{
				pglc_worker_stat_add(
					&pglc_shared->cache_admission_rejections,
					offsetof(PgLocalCacheWorkerStats,
							 cache_admission_rejections), 1);
				return NULL;
			}
		}
		entry = hash_search(cache_hash, &cache_key,
							HASH_ENTER_NULL, &found);
		if (entry == NULL)
		{
			release_cache_entry();
			pglc_worker_stat_add(
				&pglc_shared->cache_admission_rejections,
				offsetof(PgLocalCacheWorkerStats,
						 cache_admission_rejections), 1);
		}
		else if (!found)
			cache_partition->entry_count++;
		else
			release_cache_entry();
	}

	if (entry != NULL && !found)
	{
		PgLocalCacheCacheKey saved_key = entry->key;

		memset(entry, 0, sizeof(*entry));
		entry->key = saved_key;
		entry->relation_oid = relation_oid;
		entry->relation_slot = UINT32_MAX;
		entry->version = next_entry_generation(partition);
		pg_atomic_init_u64(&entry->last_access, 0);
	}
	else if (entry != NULL && create && OidIsValid(relation_oid) &&
			 entry->relation_oid != relation_oid)
	{
		/* A published keyed handle keeps its placeholder identity pinned. */
		if (entry->dirty_writers != 0)
			return NULL;

		/*
		 * Retagging a valid entry would let a value read from the old
		 * relation become a hit for the new relation.
		 */
		entry->valid = false;
		entry->version = next_entry_generation(partition);
		entry->loading = false;
		entry->load_id++;
		entry->relation_incarnation = 0;
		entry->load_relation_incarnation = 0;
		entry->relation_slot = UINT32_MAX;
		entry->slot_generation = 0;
		entry->relation_oid = relation_oid;
	}
	return entry;
}

static uint64
invalidate_all_locked(void)
{
	(void) pglc_atomic_fetch_add_checked(&pglc_shared->global_epoch, 1);
	return 1;
}

/* Caller holds key's partition lock exclusively. */
static bool
cache_retire_malformed_entry_locked(const PgLocalCacheMapping *mapping,
									const char *canonical_key,
									uint32 partition)
{
	PgLocalCacheCacheEntry *entry;
	PgLocalCacheFenceSnapshot snapshot;

	entry = get_cache_entry(MyDatabaseId, mapping->relation_oid,
							mapping->nspace, canonical_key, false);
	if (entry == NULL ||
		entry->value_len <= PGLC_VALUE_MAX ||
		!read_fence_snapshot(mapping, entry, &snapshot) ||
		!cache_entry_is_current_slot_locked(entry))
		return false;

	entry->valid = false;
	entry->loading = false;
	entry->load_id++;
	entry->version = next_entry_generation(partition);
	entry->source_xmin = InvalidTransactionId;
	entry->source_observed_full_xid = 0;
	pglc_worker_stat_add(&pglc_shared->invalidations,
						 offsetof(PgLocalCacheWorkerStats, invalidations), 1);
	pglc_worker_stat_add(&pglc_shared->key_invalidations,
						 offsetof(PgLocalCacheWorkerStats, key_invalidations), 1);
	return true;
}

static bool
cache_lookup_locked(const PgLocalCacheMapping *mapping,
					const char *canonical_key,
					uint32 partition,
					char *value, Size value_capacity, Size *value_len,
					bool *negative, TransactionId *source_xmin,
					PgLocalCacheReadToken *token,
					bool create, bool *complete, bool *malformed)
{
	PgLocalCacheCacheEntry *entry;
	PgLocalCacheFenceSnapshot before;
	PgLocalCacheFenceSnapshot after;
	bool		stable;
	bool		mapping_matches;
	bool		hit = false;

	*malformed = false;
	if (mapping->relation_slot >= (uint32) pglc_relation_states)
	{
		*complete = true;
		token->cacheable = false;
		return false;
	}
	entry = get_cache_entry(MyDatabaseId, mapping->relation_oid,
							mapping->nspace, canonical_key, create);
	if (entry != NULL && create)
	{
		if (entry->dirty_writers != 0 &&
			(entry->relation_slot != mapping->relation_slot ||
			 entry->slot_generation != mapping->relation_slot_generation))
			entry = NULL;
		else if (entry->relation_oid != mapping->relation_oid ||
				 entry->relation_slot != mapping->relation_slot ||
				 entry->slot_generation != mapping->relation_slot_generation)
		{
			entry->valid = false;
			entry->loading = false;
			entry->load_id++;
			entry->version = next_entry_generation(partition);
			entry->relation_oid = mapping->relation_oid;
			entry->relation_slot = mapping->relation_slot;
			entry->slot_generation = mapping->relation_slot_generation;
		}
	}
	mapping_matches = entry != NULL &&
		entry->relation_oid == mapping->relation_oid &&
		entry->relation_slot == mapping->relation_slot &&
		entry->slot_generation == mapping->relation_slot_generation;
	*complete = mapping_matches;
	stable = read_fence_snapshot(mapping, entry, &before);
	token->config_generation = before.config_generation;
	token->global_version = before.global_version;
	token->global_epoch = before.global_epoch;
	token->relation_version = before.relation_version;
	token->relation_incarnation = before.relation_incarnation;
	token->slot_generation = before.slot_generation;
	token->relation_slot = mapping->relation_slot;
	token->key_version = entry != NULL ? entry->version : 0;
	token->source_observed_full_xid = entry != NULL ?
		entry->source_observed_full_xid : 0;
	token->has_entry = entry != NULL;
	token->cacheable = stable && mapping_matches;

	if (token->cacheable && cache_entry_is_current_slot_locked(entry))
	{
		uint64		access_clock;

		if (entry->value_len > PGLC_VALUE_MAX)
			*malformed = true;
		else if (entry->negative)
		{
			*negative = true;
			hit = true;
		}
		else if (entry->value_len <= value_capacity)
		{
#ifdef PGLC_TEST_HOOKS
			pglc_test_pause_at("before_lookup_copy");
#endif
			memcpy(value, entry->value, entry->value_len);
#ifdef PGLC_TEST_HOOKS
			pglc_test_pause_at("after_lookup_copy");
#endif
			hit = true;
		}
		if (hit)
		{
			stable = read_fence_snapshot(mapping, entry, &after);
			hit = stable && fence_snapshots_equal(&before, &after);
			if (hit && !entry->negative)
			{
				*value_len = entry->value_len;
				*source_xmin = entry->source_xmin;
			}
			else if (!hit)
			{
				*negative = false;
				*value_len = 0;
				*source_xmin = InvalidTransactionId;
				token->cacheable = false;
			}
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
	bool		malformed = false;
	bool		hit;

	pglc_require_preload();
	memset(token, 0, sizeof(*token));
	*negative = false;
	*value_len = 0;
	*source_xmin = InvalidTransactionId;

	{
		PgLocalCacheCacheKey cache_key;
		uint32		partition;

		make_cache_key(&cache_key, MyDatabaseId, mapping->nspace,
					   canonical_key, false);
		partition = pglc_cache_partition(&cache_key);
		pglc_partition_lock_acquire(partition, LW_SHARED);
	hit = cache_lookup_locked(mapping, canonical_key, partition,
							  value, value_capacity, value_len,
							  negative, source_xmin, token,
							  false, &complete, &malformed);
		pglc_partition_lock_release(partition);

	if (malformed)
	{
		/* Recheck, retire and refresh the token while holding the write lock. */
		*negative = false;
		*value_len = 0;
		pglc_partition_lock_acquire(partition, LW_EXCLUSIVE);
		(void) cache_retire_malformed_entry_locked(mapping, canonical_key,
											 partition);
		hit = cache_lookup_locked(mapping, canonical_key, partition,
								  value, value_capacity, value_len,
								  negative, source_xmin, token,
								  false, &complete, &malformed);
		pglc_partition_lock_release(partition);
	}
	else if (!complete)
	{
		*negative = false;
		*value_len = 0;
		pglc_partition_lock_acquire(partition, LW_EXCLUSIVE);
		hit = cache_lookup_locked(mapping, canonical_key, partition,
								  value, value_capacity, value_len,
								  negative, source_xmin, token,
								  true, &complete, &malformed);
		pglc_partition_lock_release(partition);
	}
	}

#ifdef PGLC_TEST_HOOKS
	if (token->cacheable)
		pglc_test_pause_at("after_token");
#endif

	if (count_stats && hit)
	{
		pglc_worker_stat_add(&pglc_shared->cache_hits,
						 offsetof(PgLocalCacheWorkerStats, cache_hits), 1);
		if (*negative)
			pglc_worker_stat_add(&pglc_shared->negative_hits,
							 offsetof(PgLocalCacheWorkerStats, negative_hits), 1);
	}
	else if (count_stats)
		pglc_worker_stat_add(&pglc_shared->cache_misses,
						 offsetof(PgLocalCacheWorkerStats, cache_misses), 1);
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
	PgLocalCacheCacheEntry *entry;
	PgLocalCacheFenceSnapshot snapshot;
	uint32		partition;
	bool		retired = false;

	pglc_require_preload();
	if (!token->cacheable || !token->has_entry ||
		mapping->config_generation != token->config_generation)
		return false;

	partition = cache_partition_for(MyDatabaseId, mapping->nspace,
									 canonical_key);
	pglc_partition_lock_acquire(partition, LW_EXCLUSIVE);
#ifdef PGLC_TEST_HOOKS
	pglc_test_pause_at("after_store_lock");
#endif
	entry = get_cache_entry(MyDatabaseId, mapping->relation_oid,
							mapping->nspace, canonical_key, false);
	if (entry != NULL && read_fence_snapshot(mapping, entry, &snapshot) &&
		fence_snapshot_matches_token(&snapshot, token) &&
		entry->relation_oid == mapping->relation_oid &&
		entry->relation_slot == mapping->relation_slot &&
		entry->slot_generation == mapping->relation_slot_generation &&
		cache_entry_is_current_slot_locked(entry) &&
		!entry->negative &&
		TransactionIdEquals(entry->source_xmin, expected_xmin) &&
		entry->source_observed_full_xid == token->source_observed_full_xid)
	{
		entry->valid = false;
		entry->loading = false;
		entry->load_id++;
		entry->version = next_entry_generation(partition);
		entry->source_xmin = InvalidTransactionId;
		entry->source_observed_full_xid = 0;
		retired = true;
	}
	pglc_partition_lock_release(partition);
	return retired;
}

bool
pglc_cache_store(const PgLocalCacheMapping *mapping, const char *canonical_key,
				const PgLocalCacheReadToken *token, const char *value,
				Size value_len, bool negative, uint64 load_id,
				TransactionId source_xmin)
{
	PgLocalCacheCacheEntry *entry;
	PgLocalCacheFenceSnapshot before;
	PgLocalCacheFenceSnapshot after;
	bool		stored = false;
	uint64		observed_full_xid;
	uint32		partition;

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

#ifdef PGLC_TEST_HOOKS
	pglc_test_pause_at("before_store");
#endif

	partition = cache_partition_for(MyDatabaseId, mapping->nspace,
									 canonical_key);
	pglc_partition_lock_acquire(partition, LW_EXCLUSIVE);
	entry = get_cache_entry(MyDatabaseId, mapping->relation_oid,
							mapping->nspace, canonical_key, false);

	if (entry != NULL && read_fence_snapshot(mapping, entry, &before) &&
		fence_snapshot_matches_token(&before, token) &&
		entry->relation_oid == mapping->relation_oid &&
		entry->relation_oid == mapping->relation_oid &&
		entry->relation_slot == mapping->relation_slot &&
		entry->slot_generation == mapping->relation_slot_generation &&
		load_id != 0 && entry->loading && entry->load_id == load_id &&
		entry->load_relation_incarnation == token->relation_incarnation &&
		entry->version == token->key_version)
	{
		entry->negative = negative;
		entry->value_len = negative ? 0 : value_len;
		entry->source_xmin = negative ? InvalidTransactionId : source_xmin;
		entry->source_observed_full_xid = observed_full_xid;
		if (!negative && value_len > 0)
		{
#ifdef PGLC_TEST_HOOKS
			pglc_test_pause_at("before_store_copy");
#endif
			memcpy(entry->value, value, value_len);
#ifdef PGLC_TEST_HOOKS
			pglc_test_pause_at("after_store_copy");
#endif
		}
		entry->global_epoch = token->global_epoch;
		entry->relation_version = token->relation_version;
		entry->relation_incarnation = token->relation_incarnation;
		entry->relation_slot = token->relation_slot;
		entry->slot_generation = token->slot_generation;
		/*
		 * The first successful fill wins.  Moving to a fresh generation
		 * prevents a timed-out or orphaned former loader from overwriting it.
		 * The successful owner fill also completes its outstanding lease.
		 */
		entry->version = next_entry_generation(partition);
		entry->loading = false;
		entry->load_id++;
		pg_write_barrier();
		entry->valid = true;
		stored = read_fence_snapshot(mapping, entry, &after) &&
			fence_snapshot_fences_match_token(&after, token) &&
			entry->valid;
		if (!stored)
		{
			entry->valid = false;
			entry->loading = false;
			entry->load_id++;
		}
		pg_atomic_write_u64(
			&entry->last_access,
			pg_atomic_fetch_add_u64(&pglc_shared->clock, 1) + 1);
	}
	pglc_partition_lock_release(partition);
	if (stored && negative)
		pglc_worker_stat_add(&pglc_shared->negative_writes,
						 offsetof(PgLocalCacheWorkerStats, negative_writes), 1);
	return stored;
}

PgLocalCacheLoadClaim
pglc_cache_claim_load(const PgLocalCacheMapping *mapping,
					  const char *canonical_key,
					  const PgLocalCacheReadToken *token,
					  uint64 *load_id)
{
	PgLocalCacheCacheEntry *entry;
	PgLocalCacheFenceSnapshot before;
	PgLocalCacheFenceSnapshot after;
	PgLocalCacheLoadClaim result = PGLC_LOAD_BYPASS;
	TimestampTz now = GetCurrentTimestamp();
	uint32		partition;

	*load_id = 0;
	if (!token->cacheable || !token->has_entry)
		return result;

#ifdef PGLC_TEST_HOOKS
	pglc_test_pause_at("before_claim");
#endif

	partition = cache_partition_for(MyDatabaseId, mapping->nspace,
									 canonical_key);
	pglc_partition_lock_acquire(partition, LW_EXCLUSIVE);
#ifdef PGLC_TEST_HOOKS
	pglc_test_pause_at("after_claim_lock");
#endif
	entry = get_cache_entry(MyDatabaseId, mapping->relation_oid,
							mapping->nspace, canonical_key, false);
	if (entry == NULL ||
		!read_fence_snapshot(mapping, entry, &before) ||
		!fence_snapshot_fences_match_token(&before, token) ||
		entry->relation_oid != mapping->relation_oid ||
		entry->relation_slot != mapping->relation_slot ||
		entry->slot_generation != mapping->relation_slot_generation ||
		entry->relation_oid != mapping->relation_oid ||
		entry->dirty_writers != 0)
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
	if (cache_entry_is_current_slot_locked(entry))
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
		 entry->load_relation_incarnation !=
		 token->relation_incarnation ||
		 entry->load_key_version != token->key_version))
	{
		entry->loading = false;
		entry->load_id++;
	}

	if (cache_load_is_active_locked(entry, now, partition))
	{
		result = PGLC_LOAD_WAIT;
		goto done;
	}
	/* An expired lease revokes this token; retry with the new entry version. */
	if (entry->version != token->key_version)
	{
		result = PGLC_LOAD_RETRY;
		goto done;
	}

	entry->loading = true;
	entry->load_started = now;
	entry->load_global_version = token->global_version;
	entry->load_relation_version = token->relation_version;
	entry->load_relation_incarnation = token->relation_incarnation;
	entry->load_key_version = token->key_version;
	entry->load_id++;
	if (entry->load_id == 0)
		entry->load_id = 1;
	*load_id = entry->load_id;
	result = PGLC_LOAD_OWNER;
#ifdef PGLC_TEST_HOOKS
	pglc_test_pause_at("after_claim_owner");
#endif
	pg_read_barrier();
	if (!read_fence_snapshot(mapping, entry, &after) ||
		!fence_snapshot_matches_token(&after, token))
	{
		if (entry->loading && entry->load_id == *load_id)
		{
			entry->loading = false;
			entry->load_id++;
		}
		*load_id = 0;
		result = PGLC_LOAD_RETRY;
	}
	if (result == PGLC_LOAD_OWNER)
		pglc_worker_stat_add(&pglc_shared->singleflight_leaders,
						 offsetof(PgLocalCacheWorkerStats,
								  singleflight_leaders), 1);

done:
	pglc_partition_lock_release(partition);
	return result;
}

void
pglc_cache_release_load(const PgLocalCacheMapping *mapping,
						const char *canonical_key,
						const PgLocalCacheReadToken *claim_token,
						uint64 load_id)
{
	PgLocalCacheCacheEntry *entry;
	uint32		partition;

	pglc_require_preload();
	if (load_id == 0 || claim_token == NULL ||
		!claim_token->cacheable || !claim_token->has_entry)
		return;
	partition = cache_partition_for(MyDatabaseId, mapping->nspace,
									 canonical_key);
	pglc_partition_lock_acquire(partition, LW_EXCLUSIVE);
	entry = get_cache_entry(MyDatabaseId, mapping->relation_oid,
							mapping->nspace, canonical_key, false);
	if (entry != NULL &&
		entry->key.database_oid == MyDatabaseId &&
		strcmp(entry->key.nspace, mapping->nspace) == 0 &&
		strcmp(entry->key.key, canonical_key) == 0 &&
		entry->relation_oid == mapping->relation_oid &&
		entry->relation_slot == mapping->relation_slot &&
		entry->slot_generation == mapping->relation_slot_generation &&
		entry->version == claim_token->key_version &&
		entry->loading && entry->load_id == load_id &&
		entry->load_global_version == claim_token->global_version &&
		entry->load_relation_version == claim_token->relation_version &&
		entry->load_relation_incarnation ==
		claim_token->relation_incarnation &&
		entry->load_key_version == claim_token->key_version)
		entry->loading = false;
	pglc_partition_lock_release(partition);
}

void
pglc_note_singleflight_waiter(void)
{
	pglc_worker_stat_add(&pglc_shared->singleflight_waiters,
					 offsetof(PgLocalCacheWorkerStats, singleflight_waiters), 1);
}

void
pglc_note_singleflight_reuse(void)
{
	pglc_worker_stat_add(&pglc_shared->singleflight_reuses,
					 offsetof(PgLocalCacheWorkerStats, singleflight_reuses), 1);
}

void
pglc_note_singleflight_timeout(void)
{
	pglc_worker_stat_add(&pglc_shared->singleflight_timeouts,
					 offsetof(PgLocalCacheWorkerStats, singleflight_timeouts), 1);
}

bool
pglc_current_transaction_is_dirty(void)
{
	return local_dirty_hash != NULL || local_global_fallback;
}

uint64
pglc_cache_invalidate_namespace(Oid database_oid, const char *nspace)
{
	PgLocalCacheRelationState *state;
	uint32		slot_index = UINT32_MAX;
	uint64		slot_generation = 0;
	uint64		relation_incarnation = 0;
	uint64		relation_version = 0;
	uint64		global_epoch = 0;
	uint64		count;

	pglc_require_preload();
	LWLockAcquire(pglc_shared->registry_lock, LW_EXCLUSIVE);
	state = get_relation_state(database_oid, InvalidOid, nspace, false);
	if (state != NULL)
	{
		if (state->identity_pins == (uint64) -1)
			state = NULL;
		else
			state->identity_pins++;
	}
	if (state != NULL)
	{
		slot_index = state->slot;
		slot_generation = state->slot_generation;
	}
	LWLockRelease(pglc_shared->registry_lock);
	if (slot_index != UINT32_MAX)
	{
		PgLocalCacheRelationSlot *slot = &pglc_relation_slots[slot_index];

		if (pg_atomic_read_u64(&slot->generation) == slot_generation)
		{
			relation_incarnation = pg_atomic_read_u64(&slot->incarnation);
			global_epoch = pg_atomic_read_u64(&pglc_shared->global_epoch);
			(void) pglc_atomic_fetch_add_checked(&slot->dirty_writers, 1);
			relation_version =
				pglc_atomic_fetch_add_checked(&slot->version, 1);
		}
	}
	count = 0;
	{
		uint32		partition;

		for (partition = 0; partition < (uint32) pglc_cache_partition_count();
			 partition++)
		{
			HASH_SEQ_STATUS sequence;
			PgLocalCacheCacheEntry *entry;

			pglc_partition_lock_acquire(partition, LW_SHARED);
			hash_seq_init(&sequence, pglc_cache_hashes[partition]);
			while ((entry = hash_seq_search(&sequence)) != NULL)
				if (entry->key.database_oid == database_oid &&
					strncmp(entry->key.nspace, nspace,
							PGLC_NAMESPACE_MAX) == 0 &&
					slot_index != UINT32_MAX && entry->valid &&
					entry->relation_slot == slot_index &&
					entry->slot_generation == slot_generation &&
					entry->global_epoch == global_epoch &&
					entry->relation_incarnation == relation_incarnation &&
					entry->relation_version == relation_version)
					count++;
			pglc_partition_lock_release(partition);
		}
	}
	if (slot_index != UINT32_MAX)
	{
		PgLocalCacheRelationSlot *slot = &pglc_relation_slots[slot_index];

		if (pg_atomic_read_u64(&slot->generation) == slot_generation)
		{
			(void) pglc_atomic_fetch_add_checked(&slot->version, 1);
			(void) pglc_atomic_fetch_sub_checked(&slot->dirty_writers, 1);
		}
		LWLockAcquire(pglc_shared->registry_lock, LW_EXCLUSIVE);
		state = get_relation_state(database_oid, InvalidOid, nspace, false);
		if (state != NULL && state->slot == slot_index &&
			state->slot_generation == slot_generation)
		{
			release_relation_identity_pin(state);
			if (state->identity_pins == 0 &&
				pg_atomic_read_u64(&slot->dirty_writers) == 0 &&
				state->pending_forget)
			{
				release_relation_slot(state);
				(void) hash_search(pglc_relation_hash, &state->key,
								   HASH_REMOVE, NULL);
			}
		}
		LWLockRelease(pglc_shared->registry_lock);
	}
	pglc_worker_stat_add(&pglc_shared->invalidations,
						 offsetof(PgLocalCacheWorkerStats, invalidations), 1);
	pglc_worker_stat_add(&pglc_shared->table_invalidations,
						 offsetof(PgLocalCacheWorkerStats, table_invalidations), 1);
	return count;
}

uint64
pglc_cache_invalidate_key(const PgLocalCacheMapping *mapping,
						  const char *canonical_key)
{
	PgLocalCacheCacheEntry *entry;
	uint64		count = 0;
	uint32		partition;

	pglc_require_preload();
	partition = cache_partition_for(MyDatabaseId, mapping->nspace,
									 canonical_key);
	pglc_partition_lock_acquire(partition, LW_EXCLUSIVE);
	entry = get_cache_entry(MyDatabaseId, mapping->relation_oid,
						mapping->nspace, canonical_key, false);
	if (entry != NULL && cache_entry_is_current_slot_locked(entry))
		count = 1;
	if (entry != NULL)
	{
		entry->valid = false;
		entry->loading = false;
		entry->load_id++;
		entry->version = next_entry_generation(partition);
		entry->source_xmin = InvalidTransactionId;
		entry->source_observed_full_xid = 0;
	}
	pglc_partition_lock_release(partition);
	pglc_worker_stat_add(&pglc_shared->invalidations,
						 offsetof(PgLocalCacheWorkerStats, invalidations), count);
	pglc_worker_stat_add(&pglc_shared->key_invalidations,
						 offsetof(PgLocalCacheWorkerStats, key_invalidations), count);
	return count;
}

uint64
pglc_cache_invalidate_database(Oid database_oid)
{
	HASH_SEQ_STATUS relation_sequence;
	PgLocalCacheCacheEntry *entry;
	PgLocalCacheRelationState *relation_state;
	uint32		partition;
	uint64		count = 0;

	pglc_require_preload();
	for (partition = 0; partition < (uint32) pglc_cache_partition_count();
		 partition++)
	{
		HASH_SEQ_STATUS cache_sequence;

		pglc_partition_lock_acquire(partition, LW_SHARED);
		hash_seq_init(&cache_sequence, pglc_cache_hashes[partition]);
		while ((entry = hash_seq_search(&cache_sequence)) != NULL)
			if (entry->key.database_oid == database_oid &&
				cache_entry_is_current_slot_locked(entry))
				count++;
		pglc_partition_lock_release(partition);
	}
	LWLockAcquire(pglc_shared->registry_lock, LW_EXCLUSIVE);
	hash_seq_init(&relation_sequence, pglc_relation_hash);
	while ((relation_state = hash_seq_search(&relation_sequence)) != NULL)
	{
		if (relation_state->key.database_oid == database_oid)
		{
			PgLocalCacheRelationSlot *slot =
				&pglc_relation_slots[relation_state->slot];

			(void) pglc_atomic_fetch_add_checked(&slot->dirty_writers, 1);
			(void) pglc_atomic_fetch_add_checked(&slot->version, 1);
			(void) pglc_atomic_fetch_add_checked(&slot->version, 1);
			(void) pglc_atomic_fetch_sub_checked(&slot->dirty_writers, 1);
		}
	}
	LWLockRelease(pglc_shared->registry_lock);
	pglc_worker_stat_add(&pglc_shared->invalidations,
						 offsetof(PgLocalCacheWorkerStats, invalidations), count);
	pglc_worker_stat_add(&pglc_shared->table_invalidations,
						 offsetof(PgLocalCacheWorkerStats, table_invalidations), 1);
	return count;
}

uint64
pglc_cache_invalidate_all(void)
{
	uint32		partition;
	uint64		count = 0;

	pglc_require_preload();
	(void) pglc_atomic_fetch_add_checked(&pglc_shared->global_dirty_writers, 1);
	advance_global_version_locked();
	for (partition = 0; partition < (uint32) pglc_cache_partition_count();
		 partition++)
	{
		HASH_SEQ_STATUS sequence;
		PgLocalCacheCacheEntry *entry;

		pglc_partition_lock_acquire(partition, LW_SHARED);
		hash_seq_init(&sequence, pglc_cache_hashes[partition]);
		while ((entry = hash_seq_search(&sequence)) != NULL)
			if (cache_entry_is_current_slot_locked(entry))
				count++;
		pglc_partition_lock_release(partition);
	}
	(void) invalidate_all_locked();
	advance_global_version_locked();
	(void) pglc_atomic_fetch_sub_checked(&pglc_shared->global_dirty_writers, 1);
	pglc_worker_stat_add(&pglc_shared->invalidations,
						 offsetof(PgLocalCacheWorkerStats, invalidations), count);
	pglc_worker_stat_add(&pglc_shared->table_invalidations,
						 offsetof(PgLocalCacheWorkerStats, table_invalidations), 1);
	return count;
}

bool
pglc_cache_is_enabled(void)
{
	return pglc_enabled;
}

/*
 * Called by each RESP worker after it reloads its configuration.  The switch is
 * process-local on purpose: every worker applies pg_local_cache.enabled when it
 * processes SIGHUP, and a worker that turns caching back on discards everything
 * cached before it serves cached reads again.  Several workers may each
 * invalidate once; that only costs refills.  Trigger invalidation keeps running
 * while the cache is off.
 */
void
pglc_sync_cache_enabled(void)
{
	static bool applied_enabled = true;

	if (pglc_enabled && !applied_enabled)
		(void) pglc_cache_invalidate_all();
	applied_enabled = pglc_enabled;
}

void
pglc_note_database_read(void)
{
	pglc_worker_stat_add(&pglc_shared->database_reads,
					 offsetof(PgLocalCacheWorkerStats, database_reads), 1);
}

void
pglc_set_worker_slot(int worker_slot)
{
	pglc_worker_slot =
		worker_slot >= 0 && worker_slot < PGLC_MAX_WORKERS ? worker_slot : -1;
}

void
pglc_note_database_write(void)
{
	pglc_worker_stat_add(&pglc_shared->database_writes,
						 offsetof(PgLocalCacheWorkerStats, database_writes), 1);
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
		entry->shared_relation_reserved = false;
		entry->shared_relation_fence_published = false;
		entry->shared_identity_pin = false;
		entry->shared_slot = UINT32_MAX;
		entry->shared_slot_generation = 0;
		entry->shared_relation_incarnation = 0;
		entry->target_relation_incarnation = 0;
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

#ifdef PGLC_TEST_HOOKS
Datum
pg_local_cache_test_partition_lock_violations(PG_FUNCTION_ARGS)
{
	pglc_require_preload();
	PG_RETURN_INT64((int64) pg_atomic_read_u64(
		&pglc_shared->test_partition_lock_violations));
}

Datum
pg_local_cache_test_partition(PG_FUNCTION_ARGS)
{
	Oid			database_oid = PG_GETARG_OID(0);
	char	   *nspace = text_to_cstring(PG_GETARG_TEXT_PP(1));
	char	   *key = text_to_cstring(PG_GETARG_TEXT_PP(2));

	pglc_require_preload();
	PG_RETURN_INT32((int32) cache_partition_for(database_oid, nspace, key));
}

Datum
pg_local_cache_test_hash_bucket(PG_FUNCTION_ARGS)
{
	Oid			database_oid = PG_GETARG_OID(0);
	char	   *nspace = text_to_cstring(PG_GETARG_TEXT_PP(1));
	char	   *key = text_to_cstring(PG_GETARG_TEXT_PP(2));
	PgLocalCacheCacheKey cache_key;

	pglc_require_preload();
	make_cache_key(&cache_key, database_oid, nspace, key, false);
	/* dynahash selects its buckets from the hash value's low-order bits. */
	PG_RETURN_INT32((int32) (pglc_cache_key_hash(&cache_key,
											 sizeof(cache_key)) & 0xff));
}

Datum
pg_local_cache_test_collect_key(PG_FUNCTION_ARGS)
{
	Oid			relation_oid = PG_GETARG_OID(0);
	char	   *nspace = text_to_cstring(PG_GETARG_TEXT_PP(1));
	char	   *key = text_to_cstring(PG_GETARG_TEXT_PP(2));

	pglc_collect_key(MyDatabaseId, relation_oid, nspace, key);
	PG_RETURN_VOID();
}

Datum
pg_local_cache_test_relation_incarnation(PG_FUNCTION_ARGS)
{
	Oid			relation_oid = PG_GETARG_OID(0);
	char	   *nspace = text_to_cstring(PG_GETARG_TEXT_PP(1));
	PgLocalCacheRelationState *state;
	uint64		incarnation;

	pglc_require_preload();
	LWLockAcquire(pglc_shared->registry_lock, LW_EXCLUSIVE);
	state = get_relation_state(MyDatabaseId, relation_oid, nspace, true);
	incarnation = state != NULL ? pg_atomic_read_u64(
		&pglc_relation_slots[state->slot].incarnation) : 0;
	LWLockRelease(pglc_shared->registry_lock);
	PG_RETURN_INT64((int64) incarnation);
}

Datum
pg_local_cache_test_relation_identity_pins(PG_FUNCTION_ARGS)
{
	Oid			relation_oid = PG_GETARG_OID(0);
	char	   *nspace = text_to_cstring(PG_GETARG_TEXT_PP(1));
	PgLocalCacheRelationState *state;
	int64		pins;

	pglc_require_preload();
	LWLockAcquire(pglc_shared->registry_lock, LW_SHARED);
	state = get_relation_state(MyDatabaseId, relation_oid, nspace, false);
	/* Missing state means no outstanding pins for this relation identity. */
	pins = state != NULL && state->relation_oid == relation_oid ?
		(int64) state->identity_pins : 0;
	LWLockRelease(pglc_shared->registry_lock);
	PG_RETURN_INT64(pins);
}

Datum
pg_local_cache_test_recreate_relation_state(PG_FUNCTION_ARGS)
{
	Oid			relation_oid = PG_GETARG_OID(0);
	char	   *nspace = text_to_cstring(PG_GETARG_TEXT_PP(1));
	PgLocalCacheRelationKey key;
	PgLocalCacheRelationState *state;
	uint64		version;
	uint64		incarnation;
	PgLocalCacheRelationSlot *slot;

	pglc_require_preload();
	LWLockAcquire(pglc_shared->registry_lock, LW_EXCLUSIVE);
	state = get_relation_state(MyDatabaseId, relation_oid, nspace, false);
	if (state == NULL || state->relation_oid != relation_oid ||
		state->pending_forget || state->identity_pins != 0 ||
		pg_atomic_read_u64(
			&pglc_relation_slots[state->slot].dirty_writers) != 0)
	{
		LWLockRelease(pglc_shared->registry_lock);
		ereport(ERROR,
				(errcode(ERRCODE_OBJECT_NOT_IN_PREREQUISITE_STATE),
				 errmsg("test relation state cannot be recreated while pinned")));
	}
	slot = &pglc_relation_slots[state->slot];
	version = pg_atomic_read_u64(&slot->version);
	make_relation_key(&key, MyDatabaseId, nspace);
	release_relation_slot(state);
	(void) hash_search(pglc_relation_hash, &key, HASH_REMOVE, NULL);
	state = get_relation_state(MyDatabaseId, relation_oid, nspace, true);
	if (state == NULL)
	{
		LWLockRelease(pglc_shared->registry_lock);
		ereport(ERROR,
				(errcode(ERRCODE_INTERNAL_ERROR),
				 errmsg("test relation state could not be recreated")));
	}
	/* Keep relation version stable; slot generation also fences old tokens. */
	pg_atomic_write_u64(&pglc_relation_slots[state->slot].version, version);
	incarnation = pg_atomic_read_u64(
		&pglc_relation_slots[state->slot].incarnation);
	LWLockRelease(pglc_shared->registry_lock);
	PG_RETURN_INT64((int64) incarnation);
}

Datum
pg_local_cache_test_collect_global(PG_FUNCTION_ARGS)
{
	pglc_collect_global(false);
	PG_RETURN_VOID();
}

Datum
pg_local_cache_test_abort_after_reservation(PG_FUNCTION_ARGS)
{
	pglc_test_abort_after_reservation = true;
	PG_RETURN_VOID();
}

Datum
pg_local_cache_test_corrupt_value_len(PG_FUNCTION_ARGS)
{
	Oid			relation_oid = PG_GETARG_OID(0);
	char	   *nspace = text_to_cstring(PG_GETARG_TEXT_PP(1));
	char	   *key = text_to_cstring(PG_GETARG_TEXT_PP(2));
	PgLocalCacheCacheEntry *entry;
	uint32		partition;
	bool		corrupted = false;

	pglc_require_preload();
	partition = cache_partition_for(MyDatabaseId, nspace, key);
	pglc_partition_lock_acquire(partition, LW_EXCLUSIVE);
	entry = get_cache_entry(MyDatabaseId, relation_oid, nspace, key, false);
	if (entry != NULL && entry->relation_oid == relation_oid && entry->valid)
	{
		entry->value_len = PGLC_VALUE_MAX + 1;
		corrupted = true;
	}
	pglc_partition_lock_release(partition);
	PG_RETURN_BOOL(corrupted);
}
#endif

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

static uint32
local_dirty_partition(const PgLocalCacheLocalDirtyEntry *local)
{
	PgLocalCacheCacheKey key;

	if (local->key.kind != PGLC_DIRTY_KEY)
		return UINT32_MAX;
	make_cache_key(&key, local->key.database_oid, local->key.nspace,
				   local->key.key, false);
	return pglc_cache_partition(&key);
}

static int
compare_local_dirty_partitions(const void *left, const void *right)
{
	const PgLocalCacheLocalDirtyEntry *l =
		*(PgLocalCacheLocalDirtyEntry *const *) left;
	const PgLocalCacheLocalDirtyEntry *r =
		*(PgLocalCacheLocalDirtyEntry *const *) right;
	uint32		lp = local_dirty_partition(l);
	uint32		rp = local_dirty_partition(r);

	return lp < rp ? -1 : lp > rp ? 1 : 0;
}

static PgLocalCacheLocalDirtyEntry **
ordered_local_dirty_entries(Size *count)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheLocalDirtyEntry *local;
	Size		index = 0;

	if (local_dirty_ordered == NULL)
	{
		Size		capacity = (Size) hash_get_num_entries(local_dirty_hash);

		local_dirty_ordered =
			palloc(sizeof(*local_dirty_ordered) * Max(capacity, (Size) 1));
		hash_seq_init(&sequence, local_dirty_hash);
		while ((local = hash_seq_search(&sequence)) != NULL)
			local_dirty_ordered[index++] = local;
		qsort(local_dirty_ordered, index, sizeof(*local_dirty_ordered),
			  compare_local_dirty_partitions);
		local_dirty_ordered_count = index;
	}
	*count = local_dirty_ordered_count;
	return local_dirty_ordered;
}

static PgLocalCacheRelationState *
local_relation_state_locked(PgLocalCacheLocalDirtyEntry *local)
{
	PgLocalCacheRelationKey key;

	make_relation_key(&key, local->key.database_oid, local->key.nspace);
	return hash_search(pglc_relation_hash, &key, HASH_FIND, NULL);
}

static bool
reserve_relation_handle_locked(PgLocalCacheLocalDirtyEntry *local)
{
	PgLocalCacheRelationState *state = local_relation_state_locked(local);
	PgLocalCacheRelationSlot *slot;

	if (state == NULL || local->shared_slot >= (uint32) pglc_relation_states ||
		state->slot != local->shared_slot ||
		state->slot_generation != local->shared_slot_generation ||
		state->pending_forget || state->identity_pins == 0)
		return false;
	slot = &pglc_relation_slots[state->slot];
	if (pg_atomic_read_u64(&slot->generation) !=
		local->shared_slot_generation ||
		pg_atomic_read_u64(&slot->incarnation) !=
		local->shared_relation_incarnation)
		return false;
	local->shared_relation_reserved = true;
	return true;
}

static void
release_relation_handles_locked(bool committed)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheLocalDirtyEntry *local;

	if (committed)
	{
		hash_seq_init(&sequence, local_dirty_hash);
		while ((local = hash_seq_search(&sequence)) != NULL)
			if (local->key.kind == PGLC_DIRTY_FORGET_RELATION)
			{
				PgLocalCacheRelationState *state =
					local_relation_state_locked(local);

				if (state != NULL && state->slot == local->shared_slot &&
					state->slot_generation == local->shared_slot_generation &&
					pg_atomic_read_u64(
						&pglc_relation_slots[state->slot].incarnation) ==
					local->target_relation_incarnation)
					state->pending_forget = true;
			}
	}

	hash_seq_init(&sequence, local_dirty_hash);
	while ((local = hash_seq_search(&sequence)) != NULL)
	{
		PgLocalCacheRelationState *state;
		bool		identity_matches;

		if (!local->shared_identity_pin && !local->shared_relation_reserved)
			continue;
		state = local_relation_state_locked(local);
		identity_matches = state != NULL && local->shared_slot <
			(uint32) pglc_relation_states &&
			state->slot == local->shared_slot &&
			state->slot_generation == local->shared_slot_generation &&
			pg_atomic_read_u64(
				&pglc_relation_slots[state->slot].incarnation) ==
			local->shared_relation_incarnation;
		Assert(!local->shared_identity_pin || identity_matches);
		if (identity_matches)
		{
			if (local->shared_identity_pin)
				release_relation_identity_pin(state);
			if (state->identity_pins == 0 &&
				pg_atomic_read_u64(
					&pglc_relation_slots[state->slot].dirty_writers) == 0 &&
				state->pending_forget)
			{
				release_relation_slot(state);
				(void) hash_search(pglc_relation_hash, &state->key,
								   HASH_REMOVE, NULL);
			}
		}
		local->shared_identity_pin = false;
		local->shared_relation_reserved = false;
		local->shared_relation_fence_published = false;
	}
}

static bool
resolve_and_pin_relations(void)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheLocalDirtyEntry *local;
	bool		ok = true;

	LWLockAcquire(pglc_shared->registry_lock, LW_EXCLUSIVE);
	hash_seq_init(&sequence, local_dirty_hash);
	while ((local = hash_seq_search(&sequence)) != NULL)
	{
		PgLocalCacheRelationState *state;

		if (local->key.kind == PGLC_DIRTY_GLOBAL ||
			local->key.kind == PGLC_DIRTY_FORGET_RELATION)
			continue;
		state = get_relation_state(local->key.database_oid,
								   local->relation_oid,
								   local->key.nspace, true);
		/* Namespace INVALIDATE uses InvalidOid; its namespace slot is exact. */
		if (state == NULL ||
			(OidIsValid(local->relation_oid) &&
			 state->relation_oid != local->relation_oid) ||
			state->pending_forget || state->identity_pins == (uint64) -1)
		{
			ok = false;
			break;
		}
		state->identity_pins++;
		local->shared_identity_pin = true;
		local->shared_slot = state->slot;
		local->shared_slot_generation = state->slot_generation;
		local->shared_relation_incarnation = pg_atomic_read_u64(
			&pglc_relation_slots[state->slot].incarnation);
		if (local->key.kind == PGLC_DIRTY_RELATION)
			if (!reserve_relation_handle_locked(local))
			{
				ok = false;
				break;
			}
	}
	if (!ok)
		release_relation_handles_locked(false);
	LWLockRelease(pglc_shared->registry_lock);
	return ok;
}

/*
 * Forget records carry an identity to retire at commit.  Capture them in a
 * separate pass so global publication fallback cannot skip them and a failed
 * reservation for another dirty record cannot discard their identity.
 */
static void
resolve_and_pin_forget_relations(void)
{
	HASH_SEQ_STATUS sequence;
	PgLocalCacheLocalDirtyEntry *local;

	LWLockAcquire(pglc_shared->registry_lock, LW_EXCLUSIVE);
	hash_seq_init(&sequence, local_dirty_hash);
	while ((local = hash_seq_search(&sequence)) != NULL)
	{
		PgLocalCacheRelationState *state;

		if (local->key.kind != PGLC_DIRTY_FORGET_RELATION)
			continue;
		state = local_relation_state_locked(local);
		if (state == NULL || state->relation_oid != local->relation_oid ||
			state->pending_forget)
			continue;
		if (state->slot >= (uint32) pglc_relation_states)
			ereport(ERROR,
					(errcode(ERRCODE_INTERNAL_ERROR),
					 errmsg("invalid pg_local_cache forget identity slot")));
		if (state->identity_pins == (uint64) -1)
			ereport(ERROR,
					(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
					 errmsg("pg_local_cache relation identity pin overflow")));

		state->identity_pins++;
		local->shared_identity_pin = true;
		local->shared_slot = state->slot;
		local->shared_slot_generation = state->slot_generation;
		local->shared_relation_incarnation = pg_atomic_read_u64(
			&pglc_relation_slots[state->slot].incarnation);
		local->target_relation_incarnation =
			local->shared_relation_incarnation;
		if (!reserve_relation_handle_locked(local))
			ereport(ERROR,
					(errcode(ERRCODE_INTERNAL_ERROR),
					 errmsg("could not preserve pg_local_cache forget identity")));
	}
	LWLockRelease(pglc_shared->registry_lock);
}

static bool
reserve_key_entries(PgLocalCacheLocalDirtyEntry **ordered, Size count)
{
	Size		index = 0;
	bool		ok = true;

	while (index < count)
	{
		uint32		partition = local_dirty_partition(ordered[index]);
		Size		end = index;

		if (partition == UINT32_MAX)
			break;
		while (end < count && local_dirty_partition(ordered[end]) == partition)
			end++;
		pglc_partition_lock_acquire(partition, LW_EXCLUSIVE);
		for (; index < end; index++)
		{
			PgLocalCacheLocalDirtyEntry *local = ordered[index];
			PgLocalCacheCacheEntry *entry = get_cache_entry(
				local->key.database_oid, local->relation_oid,
				local->key.nspace, local->key.key, true);

			if (entry == NULL || entry->dirty_writers == (uint32) -1)
			{
				ok = false;
				break;
			}
			if (entry->dirty_writers != 0 &&
				(entry->relation_slot != local->shared_slot ||
				 entry->slot_generation != local->shared_slot_generation))
			{
				ok = false;
				break;
			}
			if (entry->relation_oid != local->relation_oid ||
				entry->relation_slot != local->shared_slot ||
				entry->slot_generation != local->shared_slot_generation)
			{
				entry->valid = false;
				entry->loading = false;
				entry->load_id++;
				entry->version = next_entry_generation(partition);
				entry->relation_oid = local->relation_oid;
				entry->relation_slot = local->shared_slot;
				entry->slot_generation = local->shared_slot_generation;
			}
			entry->dirty_writers++;
			local->shared_marker_reserved = true;
#ifdef PGLC_TEST_HOOKS
			if (pglc_test_abort_after_reservation)
			{
				pglc_test_abort_after_reservation = false;
				pglc_partition_lock_release(partition);
				ereport(ERROR,
						(errcode(ERRCODE_INTERNAL_ERROR),
						 errmsg("test abort after dirty reservation")));
			}
#endif
		}
		pglc_partition_lock_release(partition);
		if (!ok)
			break;
	}
	return ok;
}

static void
begin_relation_fence(PgLocalCacheLocalDirtyEntry *local)
{
	PgLocalCacheRelationSlot *slot =
		&pglc_relation_slots[local->shared_slot];

	(void) pglc_atomic_fetch_add_checked(&slot->dirty_writers, 1);
	(void) pglc_atomic_fetch_add_checked(&slot->version, 1);
	local->shared_relation_fence_published = true;
#ifdef PGLC_TEST_HOOKS
	pglc_test_pause_at("after_relation_begin");
#endif
}

static void
begin_global_fence(void)
{
	(void) pglc_atomic_fetch_add_checked(&pglc_shared->global_dirty_writers, 1);
	advance_global_version_locked();
	(void) pglc_atomic_fetch_add_checked(&pglc_shared->global_epoch, 1);
#ifdef PGLC_TEST_HOOKS
	pglc_test_pause_at("after_global_begin");
#endif
}

static void
pglc_publish_dirty(void)
{
	PgLocalCacheLocalDirtyEntry **ordered;
	Size		count;
	Size		index;
	uint64		invalidated = 0;
	uint64		key_invalidated = 0;
	uint64		table_invalidated = 0;
	bool		global_fallback;

	if (local_dirty_hash == NULL || local_dirty_published ||
		hash_get_num_entries(local_dirty_hash) == 0)
		return;
	ordered = ordered_local_dirty_entries(&count);
	global_fallback = local_has_global_dirty();
	if (!global_fallback && !resolve_and_pin_relations())
		global_fallback = true;
	if (!global_fallback && !reserve_key_entries(ordered, count))
	{
		bool		fallback_ok = true;

		LWLockAcquire(pglc_shared->registry_lock, LW_EXCLUSIVE);
		for (index = 0; index < count; index++)
		{
			PgLocalCacheLocalDirtyEntry *local = ordered[index];

			if (local->key.kind == PGLC_DIRTY_KEY &&
				!local->shared_marker_reserved &&
				!reserve_relation_handle_locked(local))
			{
				fallback_ok = false;
				break;
			}
		}
		LWLockRelease(pglc_shared->registry_lock);
		if (!fallback_ok)
			global_fallback = true;
	}
	resolve_and_pin_forget_relations();

	if (global_fallback)
	{
		begin_global_fence();
		local_global_fallback = true;
	}
	else
	{
		for (index = 0; index < count; index++)
			if (ordered[index]->shared_relation_reserved)
				begin_relation_fence(ordered[index]);

		index = 0;
		while (index < count)
		{
			uint32		partition = local_dirty_partition(ordered[index]);
			Size		end = index;

			if (partition == UINT32_MAX)
				break;
			while (end < count &&
				   local_dirty_partition(ordered[end]) == partition)
				end++;
			pglc_partition_lock_acquire(partition, LW_EXCLUSIVE);
			for (; index < end; index++)
			{
				PgLocalCacheLocalDirtyEntry *local = ordered[index];
				PgLocalCacheCacheEntry *entry;

				if (!local->shared_marker_reserved)
					continue;
				entry = get_cache_entry(local->key.database_oid,
										local->relation_oid,
										local->key.nspace,
										local->key.key, false);
				Assert(entry != NULL && entry->dirty_writers > 0);
				if (entry != NULL)
				{
					if (entry->valid)
					{
						invalidated++;
						key_invalidated++;
					}
					entry->valid = false;
					entry->loading = false;
					entry->load_id++;
					entry->version = next_entry_generation(partition);
				}
			}
			pglc_partition_lock_release(partition);
		}
		for (index = 0; index < count; index++)
			if (ordered[index]->shared_relation_fence_published)
			{
				invalidated++;
				table_invalidated++;
			}
	}

	local_dirty_published = true;
	pglc_worker_stat_add(&pglc_shared->invalidations,
						 offsetof(PgLocalCacheWorkerStats, invalidations),
						 invalidated);
	pglc_worker_stat_add(&pglc_shared->key_invalidations,
						 offsetof(PgLocalCacheWorkerStats, key_invalidations),
						 key_invalidated);
	pglc_worker_stat_add(&pglc_shared->table_invalidations,
						 offsetof(PgLocalCacheWorkerStats, table_invalidations),
						 table_invalidated);
#ifdef PGLC_TEST_HOOKS
	pglc_test_pause_at("after_publish");
	if (pglc_test_pause_point != NULL &&
		strcmp(pglc_test_pause_point, "after_publish_abort") == 0)
		ereport(ERROR,
				(errcode(ERRCODE_INTERNAL_ERROR),
				 errmsg("test abort after dirty publication")));
#endif
}

static void
pglc_finish_dirty(bool committed)
{
	PgLocalCacheLocalDirtyEntry **ordered = NULL;
	Size		count = 0;
	Size		index;
	bool		bump_config = committed && local_bump_config;

	if (local_dirty_hash == NULL)
		return;
	if (local_dirty_published || !committed)
	{
		ordered = ordered_local_dirty_entries(&count);
		if (local_dirty_published && bump_config)
		{
			(void) pglc_atomic_fetch_add_checked(
				&pglc_shared->config_generation, 1);
			bump_config = false;
		}
		if (local_global_fallback)
		{
			advance_global_version_locked();
			(void) pg_atomic_fetch_sub_u64(
				&pglc_shared->global_dirty_writers, 1);
#ifdef PGLC_TEST_HOOKS
			pglc_test_pause_at("after_global_finish");
#endif
		}
		for (index = 0; index < count; index++)
			if (ordered[index]->shared_relation_fence_published)
			{
				PgLocalCacheRelationSlot *slot =
					&pglc_relation_slots[ordered[index]->shared_slot];

				(void) pglc_atomic_fetch_add_checked(&slot->version, 1);
				(void) pglc_atomic_fetch_sub_checked(&slot->dirty_writers, 1);
#ifdef PGLC_TEST_HOOKS
				pglc_test_pause_at("after_relation_finish");
#endif
			}
		index = 0;
		while (index < count)
		{
			uint32		partition = local_dirty_partition(ordered[index]);
			Size		end = index;

			if (partition == UINT32_MAX)
				break;
			while (end < count &&
				   local_dirty_partition(ordered[end]) == partition)
				end++;
			pglc_partition_lock_acquire(partition, LW_EXCLUSIVE);
			for (; index < end; index++)
			{
				PgLocalCacheLocalDirtyEntry *local = ordered[index];
				PgLocalCacheCacheEntry *entry;

				if (!local->shared_marker_reserved)
					continue;
				entry = get_cache_entry(local->key.database_oid,
										local->relation_oid,
										local->key.nspace,
										local->key.key, false);
				if (entry != NULL && entry->dirty_writers > 0)
				{
					entry->valid = false;
					entry->dirty_writers--;
				}
				local->shared_marker_reserved = false;
			}
			pglc_partition_lock_release(partition);
		}
		LWLockAcquire(pglc_shared->registry_lock, LW_EXCLUSIVE);
		release_relation_handles_locked(local_dirty_published && committed);
		LWLockRelease(pglc_shared->registry_lock);
	}
	if (bump_config)
		(void) pglc_atomic_fetch_add_checked(
			&pglc_shared->config_generation, 1);

	local_dirty_hash = NULL;
	local_dirty_ordered = NULL;
	local_dirty_ordered_count = 0;
	local_dirty_published = false;
	local_global_fallback = false;
	local_bump_config = false;
}

static void
pglc_xact_callback(XactEvent event, void *arg)
{
#ifdef PGLC_TEST_HOOKS
	if (event == XACT_EVENT_ABORT || event == XACT_EVENT_PARALLEL_ABORT)
	{
		if (pglc_test_partition_lock_depth != 0)
			(void) pg_atomic_fetch_add_u64(
				&pglc_shared->test_partition_lock_violations, 1);
		pglc_test_partition_lock_depth = 0;
	}
#endif
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
	uint32		partition;
	uint64		count = 0;

	for (partition = 0; partition < (uint32) pglc_cache_partition_count();
		 partition++)
	{
		HASH_SEQ_STATUS sequence;
		PgLocalCacheCacheEntry *entry;

		pglc_partition_lock_acquire(partition, LW_SHARED);
		hash_seq_init(&sequence, pglc_cache_hashes[partition]);
		while ((entry = hash_seq_search(&sequence)) != NULL)
			if (entry->key.database_oid == database_oid &&
				strncmp(entry->key.nspace, nspace, PGLC_NAMESPACE_MAX) == 0 &&
				cache_entry_is_current_slot_locked(entry))
				count++;
		pglc_partition_lock_release(partition);
	}
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

static uint64
pglc_worker_stat_total(pg_atomic_uint64 *fallback, Size offset)
{
	uint64		total = pg_atomic_read_u64(fallback);
	int			worker_index;

	for (worker_index = 0; worker_index < PGLC_MAX_STATS_SHARDS;
		 worker_index++)
	{
		pg_atomic_uint64 *counter = (pg_atomic_uint64 *)
			((char *) &pglc_shared->stats_shards[worker_index] + offset);

		total += pg_atomic_read_u64(counter);
	}
	return total;
}

char *
pglc_stats_json(void)
{
	StringInfoData expanded;
	uint32		partition;
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
	uint64		global_dirty_writers;
	TimestampTz now = GetCurrentTimestamp();

	pglc_require_preload();
	total = pg_atomic_read_u64(&pglc_shared->cache_entry_count);
	if (total > (uint64) pglc_cache_entries)
		total = (uint64) pglc_cache_entries;
	for (partition = 0; partition < (uint32) pglc_cache_partition_count();
		 partition++)
	{
		pglc_partition_lock_acquire(partition, LW_SHARED);
		hash_seq_init(&sequence, pglc_cache_hashes[partition]);
		while ((entry = hash_seq_search(&sequence)) != NULL)
		{
			if (cache_entry_is_current_slot_locked(entry) && entry->negative)
				negative++;
			else if (cache_entry_is_current_slot_locked(entry))
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
		pglc_partition_lock_release(partition);
	}
	LWLockAcquire(pglc_shared->registry_lock, LW_SHARED);
	hash_seq_init(&relation_sequence, pglc_relation_hash);
	while ((relation_state = hash_seq_search(&relation_sequence)) != NULL)
	{
		relation_states++;
		if (pg_atomic_read_u64(
				&pglc_relation_slots[relation_state->slot].dirty_writers) > 0)
			dirty_relations++;
		if (relation_state->pending_forget)
			pending_forget++;
	}
	global_dirty_writers =
		pg_atomic_read_u64(&pglc_shared->global_dirty_writers);
	LWLockRelease(pglc_shared->registry_lock);
	cache_hits = pglc_worker_stat_total(&pglc_shared->cache_hits,
										  offsetof(PgLocalCacheWorkerStats,
										   cache_hits));
	cache_misses = pglc_worker_stat_total(&pglc_shared->cache_misses,
											offsetof(PgLocalCacheWorkerStats,
												 cache_misses));
	negative_hits = pglc_worker_stat_total(&pglc_shared->negative_hits,
										 offsetof(PgLocalCacheWorkerStats,
											  negative_hits));
	database_reads = pglc_worker_stat_total(&pglc_shared->database_reads,
											offsetof(PgLocalCacheWorkerStats,
												 database_reads));
	database_writes = pglc_worker_stat_total(&pglc_shared->database_writes,
											 offsetof(PgLocalCacheWorkerStats,
											  database_writes));
	invalidations = pglc_worker_stat_total(&pglc_shared->invalidations,
										  offsetof(PgLocalCacheWorkerStats,
										   invalidations));
	evictions = pglc_worker_stat_total(&pglc_shared->evictions,
									   offsetof(PgLocalCacheWorkerStats,
										evictions));
	singleflight_leaders =
		pglc_worker_stat_total(&pglc_shared->singleflight_leaders,
							  offsetof(PgLocalCacheWorkerStats,
								   singleflight_leaders));
	singleflight_waiters =
		pglc_worker_stat_total(&pglc_shared->singleflight_waiters,
							  offsetof(PgLocalCacheWorkerStats,
								   singleflight_waiters));
	singleflight_reuses =
		pglc_worker_stat_total(&pglc_shared->singleflight_reuses,
							  offsetof(PgLocalCacheWorkerStats,
								   singleflight_reuses));
	singleflight_timeouts =
		pglc_worker_stat_total(&pglc_shared->singleflight_timeouts,
							  offsetof(PgLocalCacheWorkerStats,
								   singleflight_timeouts));
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
		",\"global_dirty_writers\":" UINT64_FORMAT
		",\"store_size\":" UINT64_FORMAT
		",\"cache_hits\":" UINT64_FORMAT
		",\"cache_misses\":" UINT64_FORMAT
		",\"negative_hits\":" UINT64_FORMAT
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
		",\"lock_partitions\":%d"
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
		",\"sql_result_reuses\":" UINT64_FORMAT
		",\"tls_handshakes_total\":" UINT64_FORMAT
		",\"tls_handshake_failures_total\":" UINT64_FORMAT "}",
		pglc_cache_entries, pglc_cache_partition_count(), pglc_relation_states,
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
		pglc_worker_stat_total(&pglc_shared->cache_admission_rejections,
							  offsetof(PgLocalCacheWorkerStats,
								   cache_admission_rejections)),
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
		pglc_worker_stat_total(&pglc_shared->client_requests,
							  offsetof(PgLocalCacheWorkerStats, client_requests)),
		pglc_worker_stat_total(&pglc_shared->client_request_errors,
							  offsetof(PgLocalCacheWorkerStats,
								   client_request_errors)),
		pglc_worker_stat_total(&pglc_shared->client_mget_keys,
							  offsetof(PgLocalCacheWorkerStats, client_mget_keys)),
		pglc_worker_stat_total(&pglc_shared->client_sets,
							  offsetof(PgLocalCacheWorkerStats, client_sets)),
		pglc_worker_stat_total(&pglc_shared->client_dels,
							  offsetof(PgLocalCacheWorkerStats, client_dels)),
		/* Every RESP hit comes from the shared/global hash in this design. */
		cache_hits,
		pglc_worker_stat_total(&pglc_shared->negative_writes,
							  offsetof(PgLocalCacheWorkerStats, negative_writes)),
		pglc_worker_stat_total(&pglc_shared->key_invalidations,
							  offsetof(PgLocalCacheWorkerStats,
								   key_invalidations)),
		pglc_worker_stat_total(&pglc_shared->table_invalidations,
							  offsetof(PgLocalCacheWorkerStats,
								   table_invalidations)),
		pglc_worker_stat_total(&pglc_shared->pass_to_main,
							  offsetof(PgLocalCacheWorkerStats, pass_to_main)),
		pg_atomic_read_u64(&pglc_shared->mapping_reload_attempts),
		pglc_worker_stat_total(&pglc_shared->sql_sets,
							  offsetof(PgLocalCacheWorkerStats, sql_sets)),
		pglc_worker_stat_total(&pglc_shared->sql_dels,
							  offsetof(PgLocalCacheWorkerStats, sql_dels)),
		pglc_worker_stat_total(&pglc_shared->singleflight_reuses,
							  offsetof(PgLocalCacheWorkerStats,
								   singleflight_reuses)),
		pg_atomic_read_u64(&pglc_shared->tls_handshakes),
		pg_atomic_read_u64(&pglc_shared->tls_handshake_failures));
	return expanded.data;
}

char *
pglc_metrics_json(void)
{
	StringInfoData result;
	uint64		entries;
	uint64		relation_states;
	uint64		global_dirty_writers;
	uint64		workers_with_incomplete_mappings;

	pglc_require_preload();
	entries = pg_atomic_read_u64(&pglc_shared->cache_entry_count);
	if (entries > (uint64) pglc_cache_entries)
		entries = (uint64) pglc_cache_entries;
	LWLockAcquire(pglc_shared->registry_lock, LW_SHARED);
	relation_states = hash_get_num_entries(pglc_relation_hash);
	LWLockRelease(pglc_shared->registry_lock);
	global_dirty_writers =
		pg_atomic_read_u64(&pglc_shared->global_dirty_writers);
	workers_with_incomplete_mappings =
		pglc_workers_without_current_mappings();
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
	appendStringInfo(&result, ",\"cache_enabled\":%s,\"tls_enabled\":%s",
					 pglc_cache_is_enabled() ? "true" : "false",
					 pglc_tls ? "true" : "false");

#define PGLC_APPEND_METRIC_COUNTER(json_name, field_name) \
	appendStringInfo(&result, ",\"" json_name "\":" UINT64_FORMAT, \
					 pg_atomic_read_u64(&pglc_shared->field_name))
	appendStringInfo(&result, ",\"cache_hits_total\":" UINT64_FORMAT,
					 pglc_worker_stat_total(&pglc_shared->cache_hits,
									   offsetof(PgLocalCacheWorkerStats,
										cache_hits)));
	appendStringInfo(&result, ",\"cache_misses_total\":" UINT64_FORMAT,
					 pglc_worker_stat_total(&pglc_shared->cache_misses,
									   offsetof(PgLocalCacheWorkerStats,
										cache_misses)));
	appendStringInfo(&result, ",\"negative_hits_total\":" UINT64_FORMAT,
					 pglc_worker_stat_total(&pglc_shared->negative_hits,
									   offsetof(PgLocalCacheWorkerStats,
										negative_hits)));
	appendStringInfo(&result, ",\"database_reads_total\":" UINT64_FORMAT,
					 pglc_worker_stat_total(&pglc_shared->database_reads,
									   offsetof(PgLocalCacheWorkerStats,
										database_reads)));
	appendStringInfo(&result, ",\"database_writes_total\":" UINT64_FORMAT,
					 pglc_worker_stat_total(&pglc_shared->database_writes,
									   offsetof(PgLocalCacheWorkerStats,
										database_writes)));
	appendStringInfo(&result, ",\"invalidations_total\":" UINT64_FORMAT,
					 pglc_worker_stat_total(&pglc_shared->invalidations,
									   offsetof(PgLocalCacheWorkerStats,
										invalidations)));
	appendStringInfo(&result, ",\"evictions_total\":" UINT64_FORMAT,
					 pglc_worker_stat_total(&pglc_shared->evictions,
									   offsetof(PgLocalCacheWorkerStats,
										evictions)));
	appendStringInfo(&result, ",\"singleflight_leaders_total\":" UINT64_FORMAT,
					 pglc_worker_stat_total(&pglc_shared->singleflight_leaders,
									   offsetof(PgLocalCacheWorkerStats,
											singleflight_leaders)));
	appendStringInfo(&result, ",\"singleflight_waiters_total\":" UINT64_FORMAT,
					 pglc_worker_stat_total(&pglc_shared->singleflight_waiters,
									   offsetof(PgLocalCacheWorkerStats,
											singleflight_waiters)));
	appendStringInfo(&result, ",\"singleflight_reuses_total\":" UINT64_FORMAT,
					 pglc_worker_stat_total(&pglc_shared->singleflight_reuses,
									   offsetof(PgLocalCacheWorkerStats,
											singleflight_reuses)));
	appendStringInfo(&result, ",\"singleflight_timeouts_total\":" UINT64_FORMAT,
					 pglc_worker_stat_total(&pglc_shared->singleflight_timeouts,
									   offsetof(PgLocalCacheWorkerStats,
											singleflight_timeouts)));
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
	PGLC_APPEND_METRIC_COUNTER("tls_handshakes_total", tls_handshakes);
	PGLC_APPEND_METRIC_COUNTER("tls_handshake_failures_total", tls_handshake_failures);
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
