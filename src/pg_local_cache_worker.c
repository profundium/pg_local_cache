/* SPDX-License-Identifier: MIT */
#include "postgres.h"

#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <signal.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/socket.h>
#include <unistd.h>

#ifdef USE_OPENSSL
#include <openssl/err.h>
#include <openssl/ssl.h>
#endif

#include "access/detoast.h"
#include "access/htup_details.h"
#include "access/table.h"
#include "access/xact.h"
#include "catalog/namespace.h"
#include "catalog/pg_attribute.h"
#include "catalog/pg_type_d.h"
#include "executor/executor.h"
#include "executor/spi.h"
#include "lib/stringinfo.h"
#include "mb/pg_wchar.h"
#include "miscadmin.h"
#include "postmaster/bgworker.h"
#include "postmaster/interrupt.h"
#include "storage/fd.h"
#include "storage/ipc.h"
#include "storage/latch.h"
#include "storage/lmgr.h"
#include "storage/pmsignal.h"
#include "utils/builtins.h"
#include "utils/array.h"
#include "utils/jsonb.h"
#include "utils/guc.h"
#include "utils/fmgroids.h"
#include "utils/lsyscache.h"
#include "utils/memutils.h"
#include "utils/numeric.h"
#include "utils/rel.h"
#include "utils/snapmgr.h"
#include "utils/syscache.h"
#include "utils/timeout.h"
#include "utils/timestamp.h"
#include "utils/wait_event.h"
#include "tcop/tcopprot.h"

#include "pg_local_cache.h"
#include "key_codec.h"
#include "resp.h"
#include "row_payload.h"

#ifdef PGLC_TEST_HOOKS
/* Request-scoped test probe; worker maintenance stays outside this window. */
static uint64 pglc_test_current_request_palloc_count = 0;
static bool pglc_test_tracking_request_palloc = false;

void
pglc_test_record_palloc(void)
{
	if (pglc_test_tracking_request_palloc)
		pglc_test_current_request_palloc_count++;
}

static void *
pglc_test_count_palloc(Size size)
{
	pglc_test_record_palloc();
	return MemoryContextAlloc(CurrentMemoryContext, size);
}

static void
pglc_test_begin_request_palloc_count(void)
{
	pglc_test_current_request_palloc_count = 0;
	pglc_test_tracking_request_palloc = true;
}

static uint64
pglc_test_end_request_palloc_count(bool complete_request)
{
	pglc_test_tracking_request_palloc = false;
	return complete_request ? pglc_test_current_request_palloc_count : 0;
}

#undef palloc
#define palloc(size) pglc_test_count_palloc(size)

PG_FUNCTION_INFO_V1(pg_local_cache_test_key_scan_matches);
#endif

#define PGLC_OUTPUT_BATCH_BYTES (16 * 1024)
#define PGLC_OUTPUT_BUFFER_MAX \
	(PGLC_RESPONSE_MAX + PGLC_OUTPUT_BATCH_BYTES)
#define PGLC_READY_CLIENTS_PER_TURN 8
#define PGLC_AUTH_TOKEN_FILE_MAX 256
#define PGLC_TLS_READ_MAX 8192
#define PGLC_MAX_LOAD_RETRIES 3
#define PGLC_DEFERRED_REQUEST_BYTES_MAX (512 * 1024)
#define PGLC_DEFERRED_RETRIES_PER_TURN 8

typedef struct PgLocalCacheDeferredMiss PgLocalCacheDeferredMiss;

typedef struct PgLocalCacheClient
{
	int			fd;
	bool		authenticated;
	bool		close_after_flush;
	bool		input_ready;
	bool		read_activity_pending;
	bool		output_ready;
	bool		output_backpressure_reported;
	bool		input_eof;
	bool		peer_hung_up;
	uint8		authentication_failures;
	Size		input_start;
	Size		used;
	Size		output_used;
	Size		output_sent;
	PgLocalCacheDeferredMiss *deferred_miss;
	TimestampTz deferred_deadline;
	bool		deferred_deadline_expired;
	bool		retrying_deferred_miss;
#ifdef PGLC_TEST_HOOKS
	uint64		test_last_request_palloc_count;
#endif
	TimestampTz last_activity;
#ifdef USE_OPENSSL
	SSL		   *ssl;
	bool		tls_ready;
	bool		tls_skip_shutdown;
	bool		tls_handshake_failure_counted;
	TimestampTz tls_accepted_at;
	/* Handshake wait is separate; SSL_read and SSL_write waits never alias. */
	int			tls_handshake_wait;
	int			tls_read_wait;
	int			tls_write_wait;
	Size		tls_write_retry_offset;
	Size		tls_write_retry_length;
#endif
	char	   *input;
	char	   *output;
} PgLocalCacheClient;

struct PgLocalCacheDeferredMiss
{
	PgLocalCacheDeferredMiss *next;
	PgLocalCacheClient *client;
	Size		request_offset;
	Size		request_length;
	TimestampTz deadline;
	uint64		mapping_generation;
};

typedef enum PgLocalCacheDeferredResult
{
	PGLC_DEFERRED_QUEUED,
	PGLC_DEFERRED_QUEUE_FULL,
	PGLC_DEFERRED_EXPIRED
} PgLocalCacheDeferredResult;

static MemoryContext mapping_context = NULL;
static MemoryContext reload_context = NULL;
static MemoryContext command_context = NULL;
static PgLocalCacheMapping *worker_mappings = NULL;
static int	worker_mapping_count = 0;
static uint64 worker_mapping_generation = 0;
static TimestampTz worker_next_mapping_retry = 0;
static uint64 worker_retry_generation = 0;
static bool worker_mappings_incomplete = false;
static TimestampTz worker_turn_timestamp = 0;
static int	worker_slot = -1;
static char *worker_auth_token = NULL;
static bool bind_address_is_loopback(const char *address);
static uint64 worker_client_reservations = 0;
static bool worker_counted_active = false;
static PgLocalCacheDeferredMiss *deferred_misses_head = NULL;
static PgLocalCacheDeferredMiss *deferred_misses_tail = NULL;
static int deferred_misses_count = 0;
static Size deferred_misses_bytes = 0;
static uint64 deferred_misses_total = 0;
static uint64 deferred_timeouts_total = 0;
static uint64 deferred_rejections_total = 0;
#ifdef USE_OPENSSL
static SSL_CTX *worker_ssl_ctx = NULL;
#endif

static void load_auth_token(void);
static void worker_before_exit(int code, Datum arg);
static void validate_file_descriptor_limit(void);
static int create_listener(void);
static void initialize_tls_server(void);
static void run_server(int listener);
static void close_client(PgLocalCacheClient *client);
static void compact_client_input(PgLocalCacheClient *client);
static void record_client_output_backpressure(PgLocalCacheClient *client);
static bool flush_client_output(PgLocalCacheClient *client);
static void flush_ready_client_outputs(PgLocalCacheClient *clients,
									  int client_slots);
static void refresh_client_read_activity(PgLocalCacheClient *clients,
									 int client_slots);
static bool queue_response(PgLocalCacheClient *client,
						   const char *response, Size response_length,
						   bool close_after);
static bool process_client(PgLocalCacheClient *client,
						   bool retry_tls_read);
static void retry_deferred_misses(void);
static int deferred_miss_poll_timeout(int current_timeout);
static PgLocalCacheDeferredResult enqueue_deferred_miss(
	PgLocalCacheClient *client, Size request_length, TimestampTz deadline);
static void remove_deferred_miss(PgLocalCacheClient *client);
static bool try_fast_mget_hit(PgLocalCacheClient *client,
							  PgLocalCacheRespArg *args, int argc);
static void note_resp_cache_lookup(bool hit, bool negative);
#ifdef USE_OPENSSL
static bool drive_tls_handshake(PgLocalCacheClient *client);
static bool client_has_tls_input(PgLocalCacheClient *client);
static ssize_t client_recv(PgLocalCacheClient *client, void *buffer, Size length);
static ssize_t client_send(PgLocalCacheClient *client, const void *buffer, Size length);
static void count_tls_handshake_failure(PgLocalCacheClient *client);
#else
static ssize_t client_recv(PgLocalCacheClient *client, void *buffer, Size length);
static ssize_t client_send(PgLocalCacheClient *client, const void *buffer, Size length);
#endif
static char *execute_command(PgLocalCacheClient *client,
							 PgLocalCacheRespArg *args, int argc,
							 Size request_length,
							 Size *response_length, bool *close_after);
static char *execute_command_inner(PgLocalCacheClient *client,
								   PgLocalCacheRespArg *args, int argc,
								   Size request_length,
								   Size *response_length, bool *close_after);
static void maybe_reload_mappings(void);
static void worker_process_config_reload(void);
static bool reload_mappings(uint64 target_generation);
static void set_worker_mappings_incomplete(bool incomplete);
static void set_worker_mapping_generation(uint64 generation);
static void abort_spi_transaction(MemoryContext caller_context);
static bool resolve_wire_key(const PgLocalCacheRespArg *wire_key,
								 PgLocalCacheMapping **mapping, char **raw_key,
								 char **error);
static bool canonicalize_key(PgLocalCacheMapping *mapping, const char *raw_key,
								 Datum *key_values, char **canonical, char **error);
static bool row_json_validate(PgLocalCacheMapping *mapping, Jsonb *row,
							  Datum *key_values, char **error);
static bool cached_row_json(PgLocalCacheMapping *mapping,
							const char *payload, Size payload_length,
							char **json, Size *json_length);
static void ensure_mapping_current(const PgLocalCacheMapping *mapping);
static char *command_mget_one(PgLocalCacheMapping *mapping,
								  const char *canonical, Datum *key_values,
								  TimestampTz deadline,
								  bool waiter_already_counted,
								  bool *relation_locked,
								  Size *response_length);
static char *command_mget(PgLocalCacheClient *client,
						  PgLocalCacheRespArg *args, int argc,
						  Size request_length,
						  Size *response_length);

typedef struct PgLocalCacheMgetItem
{
	PgLocalCacheMapping *mapping;
	char	   *canonical;
	Datum		key_values[PGLC_MAX_KEY_COLUMNS];
	PgLocalCacheReadToken token;
	char	   *json;
	Size		json_length;
	char	   *payload;
	Size		payload_length;
	char	   *response_element;
	Size		response_element_length;
	TransactionId database_xmin;
	Size		response_slots;
	uint64		load_id;
	bool		cache_enabled;
	bool		result_ready;
	bool		null_result;
	bool		deferred;
	bool		owns_load;
	bool		payload_cacheable;
	bool		database_read;
} PgLocalCacheMgetItem;

typedef struct PgLocalCacheHitScratch
{
	PgLocalCacheMapping *mapping;
	PgLocalCacheReadToken token;
	char		canonical[PGLC_KEY_MAX];
	char		value[PGLC_VALUE_MAX];
} PgLocalCacheHitScratch;

typedef enum PgLocalCacheFastPathFallbackReason
{
	PGLC_FAST_PATH_FALLBACK_KEY_FORM,
	PGLC_FAST_PATH_FALLBACK_MAPPING_SHAPE,
	PGLC_FAST_PATH_FALLBACK_MULTI_KEY,
	PGLC_FAST_PATH_FALLBACK_CACHE_STATE
} PgLocalCacheFastPathFallbackReason;

static PgLocalCacheHitScratch worker_hit_scratch;

static char *command_set(PgLocalCacheMapping *mapping, const char *raw_key,
							 const PgLocalCacheRespArg *value_arg,
							 Size *response_length);
static char *command_delete(PgLocalCacheMapping *mapping, const char *raw_key,
								Size *response_length);

PGDLLEXPORT void
pg_local_cache_worker_main(Datum main_arg)
{
	int			listener;
	const char *role;
	int			requested_slot = DatumGetInt32(main_arg);

	if (requested_slot < 0 || requested_slot >= PGLC_MAX_WORKERS)
		ereport(FATAL,
				(errmsg("invalid pg_local_cache worker slot %d", requested_slot)));
	worker_slot = requested_slot;
	pglc_set_worker_slot(worker_slot);

	pqsignal(SIGHUP, SignalHandlerForConfigReload);
	pqsignal(SIGTERM, die);
	BackgroundWorkerUnblockSignals();

	pglc_require_preload();
	role = (pglc_role != NULL && pglc_role[0] != '\0') ? pglc_role : NULL;
	BackgroundWorkerInitializeConnection(pglc_database, role, 0);
	if (superuser() && !pglc_allow_superuser)
		ereport(FATAL,
				(errmsg("pg_local_cache refuses to run RESP workers as a superuser"),
				 errhint("Create a dedicated LOGIN role and set pg_local_cache.role, or enable pg_local_cache.allow_superuser only for development.")));
	load_auth_token();
	validate_file_descriptor_limit();
	before_shmem_exit(worker_before_exit, (Datum) 0);
	set_worker_mapping_generation(0);
	set_worker_mappings_incomplete(true);

	mapping_context = AllocSetContextCreate(TopMemoryContext,
										"pg_local_cache mappings",
										ALLOCSET_DEFAULT_SIZES);
	reload_context = AllocSetContextCreate(TopMemoryContext,
									   "pg_local_cache mapping reload scratch",
									   ALLOCSET_SMALL_SIZES);
	command_context = AllocSetContextCreate(TopMemoryContext,
										"pg_local_cache command",
										ALLOCSET_SMALL_SIZES);
	maybe_reload_mappings();

	initialize_tls_server();
	listener = create_listener();
	/* Count the worker only once it accepts connections: health() reads this. */
	pglc_note_worker_start();
	worker_counted_active = true;
	ereport(LOG,
			(errmsg("pg_local_cache worker %d listening on %s:%d for database \"%s\"",
					DatumGetInt32(main_arg), pglc_bind_address, pglc_port,
					pglc_database)));
	run_server(listener);
	close(listener);
	proc_exit(0);
}

static void
worker_process_config_reload(void)
{
	if (ConfigReloadPending)
	{
		ConfigReloadPending = false;
		ProcessConfigFile(PGC_SIGHUP);
	}
	pglc_sync_cache_enabled();
}

Size
pglc_worker_memory_bytes_per_worker(void)
{
	Size		slots;
	Size		bytes;
	Size		mapping_bytes;
	Size		descriptor_bytes;

	if (pglc_port == 0)
		return 0;
	slots = (Size) pglc_max_clients_per_worker;
	bytes = mul_size(slots, sizeof(PgLocalCacheClient));
	bytes = add_size(bytes,
				 mul_size((Size) pglc_max_deferred_misses,
						  MAXALIGN(sizeof(PgLocalCacheDeferredMiss))));
	bytes = add_size(bytes,
				 mul_size(slots + 1, sizeof(struct pollfd)));
	bytes = add_size(bytes, mul_size(slots + 1, sizeof(int)));
	/*
	 * Whole-row mappings retain a copied TupleDesc so cache hits never need a
	 * catalog transaction.  Budget the supported maximum shape for every
	 * mapping; actual tables are normally much narrower, but startup OOM
	 * protection must not depend on that assumption.
	 */
	descriptor_bytes = add_size(sizeof(TupleDescData),
		mul_size((Size) MaxTupleAttributeNumber,
				 sizeof(FormData_pg_attribute)));
	mapping_bytes = add_size(sizeof(PgLocalCacheMapping), descriptor_bytes);
	bytes = add_size(bytes,
				 mul_size((Size) PGLC_MAX_MAPPINGS, mapping_bytes));
	return bytes;
}

Size
pglc_worker_memory_bytes(void)
{
	Size		total_slots;
	Size		active_clients;
	Size		worker_bytes;
	Size		client_buffer_bytes;

	if (pglc_port == 0)
		return 0;
	total_slots = mul_size((Size) pglc_worker_count,
						   (Size) pglc_max_clients_per_worker);
	active_clients = Min((Size) pglc_max_clients, total_slots);
	worker_bytes = mul_size((Size) pglc_worker_count,
							pglc_worker_memory_bytes_per_worker());
	client_buffer_bytes = mul_size(active_clients,
									 (Size) PGLC_REQUEST_MAX +
									 PGLC_OUTPUT_BUFFER_MAX);
	return add_size(worker_bytes, client_buffer_bytes);
}

static void
worker_before_exit(int code, Datum arg)
{
	set_worker_mapping_generation(0);
	set_worker_mappings_incomplete(false);
	if (worker_client_reservations > 0)
	{
		pglc_release_clients(worker_client_reservations);
		worker_client_reservations = 0;
	}
	if (worker_counted_active)
	{
		pglc_note_worker_stop();
		worker_counted_active = false;
	}
}

static void
validate_file_descriptor_limit(void)
{
	struct rlimit descriptor_limit;
	rlim_t		required = (rlim_t) Min(pglc_max_clients_per_worker,
										pglc_max_clients) + 33;

	if (getrlimit(RLIMIT_NOFILE, &descriptor_limit) != 0)
		ereport(FATAL,
				(errmsg("could not read the pg_local_cache worker file descriptor limit: %m")));
	if (descriptor_limit.rlim_cur != RLIM_INFINITY &&
		descriptor_limit.rlim_cur < required)
		ereport(FATAL,
				(errmsg("file descriptor limit is too low for pg_local_cache"),
				 errdetail("Each RESP worker needs at least %llu descriptors; the soft RLIMIT_NOFILE is %llu.",
						   (unsigned long long) required,
						   (unsigned long long) descriptor_limit.rlim_cur),
					 errhint("Raise the container/process nofile limit or lower pg_local_cache.max_clients or pg_local_cache.max_clients_per_worker.")));
}

static bool
bind_address_is_loopback(const char *address)
{
	struct in_addr ipv4_address;

	return address != NULL && inet_pton(AF_INET, address, &ipv4_address) == 1 &&
		(ntohl(ipv4_address.s_addr) & 0xff000000U) == 0x7f000000U;
}

static void
load_auth_token(void)
{
	const char *inline_token =
		(pglc_auth_token != NULL) ? pglc_auth_token : "";
	const char *token_file =
		(pglc_auth_token_file != NULL) ? pglc_auth_token_file : "";

	if (inline_token[0] != '\0' && token_file[0] != '\0')
		ereport(FATAL,
				(errmsg("set only one of pg_local_cache.auth_token and pg_local_cache.auth_token_file")));

	if (token_file[0] != '\0')
	{
		struct stat file_stat;
		FILE	   *file;
		char	   *buffer;
		Size		length;
		Size		i;
		int			extra;

		if (token_file[0] != '/')
			ereport(FATAL,
					(errmsg("pg_local_cache.auth_token_file must be an absolute path")));
		if (lstat(token_file, &file_stat) != 0)
			ereport(FATAL,
					(errmsg("could not stat pg_local_cache auth token file \"%s\": %m",
							token_file)));
		if (!S_ISREG(file_stat.st_mode))
			ereport(FATAL,
					(errmsg("pg_local_cache auth token file must be a regular file")));
		if (file_stat.st_uid != geteuid())
			ereport(FATAL,
					(errmsg("pg_local_cache auth token file must be owned by the RESP worker operating-system user")));
		if ((file_stat.st_mode & (S_IRWXG | S_IRWXO)) != 0)
			ereport(FATAL,
					(errmsg("pg_local_cache auth token file permissions are too broad"),
					 errhint("Use mode 0600 or 0400.")));

		file = AllocateFile(token_file, "r");
		if (file == NULL)
			ereport(FATAL,
					(errmsg("could not open pg_local_cache auth token file \"%s\": %m",
							token_file)));
		buffer = palloc0(PGLC_AUTH_TOKEN_FILE_MAX + 3);
		length = fread(buffer, 1, PGLC_AUTH_TOKEN_FILE_MAX + 2, file);
		if (length == 0)
		{
			FreeFile(file);
			ereport(FATAL,
					(errmsg("pg_local_cache auth token file is empty")));
		}
		extra = fgetc(file);
		if (extra != EOF)
		{
			FreeFile(file);
			ereport(FATAL,
					(errmsg("pg_local_cache auth token file must contain exactly one token of at most %d bytes",
							PGLC_AUTH_TOKEN_FILE_MAX)));
		}
		if (ferror(file))
		{
			FreeFile(file);
			ereport(FATAL,
					(errmsg("could not read pg_local_cache auth token file \"%s\": %m",
							token_file)));
		}
		if (FreeFile(file) != 0)
			ereport(FATAL,
					(errmsg("could not close pg_local_cache auth token file \"%s\": %m",
							token_file)));

		if (memchr(buffer, '\0', length) != NULL)
			ereport(FATAL,
					(errmsg("pg_local_cache auth token contains a non-base64url byte")));
		if (length > 0 && buffer[length - 1] == '\n')
		{
			buffer[--length] = '\0';
			if (length > 0 && buffer[length - 1] == '\r')
				buffer[--length] = '\0';
		}
		else if (length > 0 && buffer[length - 1] == '\r')
			ereport(FATAL,
					(errmsg("pg_local_cache auth token permits only one terminal LF or CRLF")));
		if (length < 32 || length > PGLC_AUTH_TOKEN_FILE_MAX)
			ereport(FATAL,
					(errmsg("pg_local_cache auth token must contain 32-256 base64url bytes")));
		for (i = 0; i < length; i++)
		{
			if (!((buffer[i] >= 'A' && buffer[i] <= 'Z') ||
				  (buffer[i] >= 'a' && buffer[i] <= 'z') ||
				  (buffer[i] >= '0' && buffer[i] <= '9') ||
				  buffer[i] == '_' || buffer[i] == '-'))
				ereport(FATAL,
						(errmsg("pg_local_cache auth token contains a non-base64url byte")));
		}
		worker_auth_token = buffer;
	}
	else
	{
		if (strlen(inline_token) > PGLC_AUTH_TOKEN_MAX)
			ereport(FATAL,
					(errmsg("pg_local_cache.auth_token exceeds %d bytes",
							PGLC_AUTH_TOKEN_MAX)));
		worker_auth_token = pstrdup(inline_token);
		if (inline_token[0] != '\0')
			ereport(WARNING,
					(errmsg("pg_local_cache.auth_token is configured inline"),
					 errhint("Use pg_local_cache.auth_token_file in production.")));
	}

}

#ifdef USE_OPENSSL
static int
reject_ssl_passphrase(char *buffer, int size, int rwflag, void *userdata)
{
	/* This worker must never prompt for, or retain, a private-key passphrase. */
	(void) buffer;
	(void) size;
	(void) rwflag;
	(void) userdata;
	return 0;
}

static void
report_ssl_configuration_error(const char *operation)
{
	unsigned long error_code = ERR_get_error();
	char		ssl_error[256];

	if (error_code != 0)
		ERR_error_string_n(error_code, ssl_error, sizeof(ssl_error));
	else
		strlcpy(ssl_error, "no OpenSSL error details", sizeof(ssl_error));
	ereport(FATAL,
			(errmsg("pg_local_cache TLS %s failed: %s", operation, ssl_error),
			 errhint("Check the RESP TLS certificate, key, CA files, protocol version, and key permissions.")));
}

static char *
resolve_tls_path(const char *path)
{
	if (path[0] == '/')
		return pstrdup(path);
	return psprintf("%s/%s", DataDir, path);
}

static void
check_tls_key_file_permissions(const char *path)
{
	struct stat statbuf;
	uid_t		server_uid = geteuid();
	mode_t		mode;

	if (stat(path, &statbuf) < 0)
		ereport(FATAL,
				(errcode_for_file_access(),
				 errmsg("could not stat pg_local_cache TLS private key file: %m"),
				 errhint("Set pg_local_cache.tls_key_file to a regular file readable by the PostgreSQL server user.")));
	if (!S_ISREG(statbuf.st_mode))
		ereport(FATAL,
				(errmsg("pg_local_cache TLS private key file must be a regular file"),
				 errhint("Use a regular file owned by the PostgreSQL server user or root.")));
	if (statbuf.st_uid != server_uid && statbuf.st_uid != 0)
		ereport(FATAL,
				(errmsg("pg_local_cache TLS private key file must be owned by the PostgreSQL server user or root"),
				 errhint("Change the file owner to the PostgreSQL server user or root.")));

	mode = statbuf.st_mode;
	if ((statbuf.st_uid == server_uid && (mode & (S_IRWXG | S_IRWXO)) != 0) ||
		(statbuf.st_uid == 0 &&
		 (mode & (S_IWGRP | S_IXGRP | S_IRWXO)) != 0))
		ereport(FATAL,
				(errmsg("pg_local_cache TLS private key file permissions are too permissive"),
				 errhint("For a server-owned key, remove all group and other permissions; for a root-owned key, allow at most root and group read.")));
}

static void
initialize_tls_server(void)
{
	char	   *certificate_path;
	char	   *key_path;
	int			protocol_version;

	if (!pglc_tls)
		return;
	if (pglc_tls_cert_file == NULL || pglc_tls_cert_file[0] == '\0' ||
		pglc_tls_key_file == NULL || pglc_tls_key_file[0] == '\0')
		ereport(FATAL,
				(errmsg("pg_local_cache.tls requires tls_cert_file and tls_key_file"),
				 errhint("Set pg_local_cache.tls_cert_file and pg_local_cache.tls_key_file to PEM files.")));

	ERR_clear_error();
	worker_ssl_ctx = SSL_CTX_new(TLS_server_method());
	if (worker_ssl_ctx == NULL)
		report_ssl_configuration_error("context creation");

	SSL_CTX_set_options(worker_ssl_ctx, SSL_OP_NO_COMPRESSION);
#ifdef SSL_OP_NO_RENEGOTIATION
	SSL_CTX_set_options(worker_ssl_ctx, SSL_OP_NO_RENEGOTIATION);
#endif
	protocol_version = pglc_tls_min_protocol_version == 13 ?
		TLS1_3_VERSION : TLS1_2_VERSION;
	if (SSL_CTX_set_min_proto_version(worker_ssl_ctx, protocol_version) != 1)
		report_ssl_configuration_error("minimum protocol configuration");
	if (SSL_CTX_set_session_id_context(worker_ssl_ctx,
									  (const unsigned char *) "pg_local_cache",
									  strlen("pg_local_cache")) != 1)
		report_ssl_configuration_error("session ID context configuration");

	certificate_path = resolve_tls_path(pglc_tls_cert_file);
	key_path = resolve_tls_path(pglc_tls_key_file);
	ERR_clear_error();
	if (SSL_CTX_use_certificate_chain_file(worker_ssl_ctx, certificate_path) != 1)
		report_ssl_configuration_error("certificate chain loading");
	check_tls_key_file_permissions(key_path);
	SSL_CTX_set_default_passwd_cb(worker_ssl_ctx, reject_ssl_passphrase);
	ERR_clear_error();
	if (SSL_CTX_use_PrivateKey_file(worker_ssl_ctx, key_path,
									SSL_FILETYPE_PEM) != 1)
		report_ssl_configuration_error("private key loading");
	ERR_clear_error();
	if (SSL_CTX_check_private_key(worker_ssl_ctx) != 1)
		report_ssl_configuration_error("certificate and private key validation");

	if (pglc_tls_ca_file != NULL && pglc_tls_ca_file[0] != '\0')
	{
		char *ca_path = resolve_tls_path(pglc_tls_ca_file);

		ERR_clear_error();
		if (SSL_CTX_load_verify_locations(worker_ssl_ctx, ca_path, NULL) != 1)
			report_ssl_configuration_error("client CA loading");
		SSL_CTX_set_verify(worker_ssl_ctx,
						   SSL_VERIFY_PEER | SSL_VERIFY_FAIL_IF_NO_PEER_CERT,
						   NULL);
	}
}
#else
static void
initialize_tls_server(void)
{
	if (pglc_tls)
		ereport(FATAL,
				(errmsg("pg_local_cache.tls requires PostgreSQL built with OpenSSL"),
				 errhint("Install or build PostgreSQL with OpenSSL support, or set pg_local_cache.tls = off.")));
}
#endif

static int
create_listener(void)
{
	int			fd;
	int			enabled = 1;
	int			flags;
	struct sockaddr_in address;
	struct in_addr bind_address;
	bool		loopback;

	if (pglc_bind_address == NULL ||
		inet_pton(AF_INET, pglc_bind_address, &bind_address) != 1)
		ereport(FATAL,
				(errmsg("pg_local_cache.bind_address must be an IPv4 literal")));
	loopback = bind_address_is_loopback(pglc_bind_address);
	if (!loopback && !pglc_tls && !pglc_allow_plaintext_network)
		ereport(FATAL,
				(errmsg("pg_local_cache refuses a non-TLS RESP listener on non-loopback address %s",
						pglc_bind_address),
				 errhint("Enable pg_local_cache.tls, or set pg_local_cache.allow_plaintext_network = on only on a trusted network.")));

	if (!loopback &&
		(worker_auth_token == NULL || worker_auth_token[0] == '\0'))
		ereport(FATAL,
				(errmsg("pg_local_cache refuses a non-loopback listener without authentication")));
	if (!loopback && strlen(worker_auth_token) < 32)
		ereport(FATAL,
				(errmsg("a non-loopback pg_local_cache listener requires an auth token of at least 32 bytes")));

	fd = socket(AF_INET, SOCK_STREAM, 0);
	if (fd < 0)
		ereport(FATAL,
				(errcode_for_socket_access(),
				 errmsg("could not create pg_local_cache listener socket: %m")));

	if (setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &enabled, sizeof(enabled)) < 0)
		ereport(FATAL,
				(errcode_for_socket_access(),
				 errmsg("could not set SO_REUSEADDR on pg_local_cache socket: %m")));

	if (pglc_worker_count > 1)
	{
#ifdef SO_REUSEPORT
		if (setsockopt(fd, SOL_SOCKET, SO_REUSEPORT,
					   &enabled, sizeof(enabled)) < 0)
			ereport(FATAL,
					(errcode_for_socket_access(),
					 errmsg("could not set SO_REUSEPORT on pg_local_cache socket: %m")));
#else
		ereport(FATAL,
				(errmsg("pg_local_cache.workers > 1 requires SO_REUSEPORT")));
#endif
	}

	memset(&address, 0, sizeof(address));
	address.sin_family = AF_INET;
	address.sin_port = htons((uint16) pglc_port);
	address.sin_addr = bind_address;

	if (bind(fd, (struct sockaddr *) &address, sizeof(address)) < 0)
		ereport(FATAL,
				(errcode_for_socket_access(),
				 errmsg("could not bind pg_local_cache to %s:%d: %m",
						pglc_bind_address, pglc_port)));
	if (listen(fd, SOMAXCONN) < 0)
		ereport(FATAL,
				(errcode_for_socket_access(),
				 errmsg("could not listen on pg_local_cache socket: %m")));

	flags = fcntl(fd, F_GETFL, 0);
	if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0)
		ereport(FATAL,
				(errcode_for_socket_access(),
				 errmsg("could not make pg_local_cache socket nonblocking: %m")));
	return fd;
}

static ssize_t
client_recv(PgLocalCacheClient *client, void *buffer, Size length)
{
#ifdef USE_OPENSSL
	if (client->ssl != NULL)
	{
		int			result;
		int			ssl_error;
		int			read_length;

		Assert(length <= INT_MAX);
		Assert(client->tls_read_wait == 0 ||
			   client->tls_read_wait == POLLIN ||
			   client->tls_read_wait == POLLOUT);
		read_length = length > PGLC_TLS_READ_MAX ?
			PGLC_TLS_READ_MAX : (int) length;
		ERR_clear_error();
		result = SSL_read(client->ssl, buffer, read_length);
		ssl_error = SSL_get_error(client->ssl, result);
		if (result > 0)
		{
			client->tls_read_wait = 0;
			return result;
		}
		if (ssl_error == SSL_ERROR_WANT_READ ||
			ssl_error == SSL_ERROR_WANT_WRITE)
		{
			client->tls_read_wait = ssl_error == SSL_ERROR_WANT_WRITE ?
				POLLOUT : POLLIN;
			errno = EAGAIN;
			return -1;
		}
		if (ssl_error == SSL_ERROR_ZERO_RETURN)
		{
			client->tls_read_wait = 0;
			return 0;
		}
		client->tls_skip_shutdown = true;
		errno = ECONNRESET;
		return -1;
	}
#endif
	return recv(client->fd, buffer, length, 0);
}

static ssize_t
client_send(PgLocalCacheClient *client, const void *buffer, Size length)
{
#ifdef USE_OPENSSL
	if (client->ssl != NULL)
	{
		int			result;
		int			ssl_error;

		Assert(length <= INT_MAX);
		Assert(client->tls_write_wait == 0 ||
			   client->tls_write_wait == POLLIN ||
			   client->tls_write_wait == POLLOUT);
		if (client->tls_write_wait != 0)
		{
			Assert(buffer == client->output + client->tls_write_retry_offset);
			Assert(length == client->tls_write_retry_length);
		}
		ERR_clear_error();
		result = SSL_write(client->ssl, buffer, (int) length);
		ssl_error = SSL_get_error(client->ssl, result);
		if (result > 0)
		{
			client->tls_write_wait = 0;
			client->tls_write_retry_length = 0;
			return result;
		}
		if (ssl_error == SSL_ERROR_WANT_READ ||
			ssl_error == SSL_ERROR_WANT_WRITE)
		{
			client->tls_write_wait = ssl_error == SSL_ERROR_WANT_WRITE ?
				POLLOUT : POLLIN;
			client->tls_write_retry_offset =
				(Size) ((const char *) buffer - client->output);
			client->tls_write_retry_length = length;
			errno = EAGAIN;
			return -1;
		}
		client->tls_skip_shutdown = true;
		errno = ECONNRESET;
		return -1;
	}
#endif
	{
		int			flags = 0;

#ifdef MSG_DONTWAIT
		flags |= MSG_DONTWAIT;
#endif
#ifdef MSG_NOSIGNAL
		flags |= MSG_NOSIGNAL;
#endif
		return send(client->fd, buffer, length, flags);
	}
}

#ifdef USE_OPENSSL
static bool
client_has_tls_input(PgLocalCacheClient *client)
{
	return client->ssl != NULL && client->tls_ready &&
		client->tls_read_wait == 0 && client->tls_write_wait == 0 &&
		client->used < PGLC_REQUEST_MAX && !client->input_eof &&
		SSL_pending(client->ssl) > 0;
}

static TimestampTz
tls_handshake_deadline(PgLocalCacheClient *client)
{
	Assert(client->ssl != NULL && !client->tls_ready);
	return client->tls_accepted_at + (int64) pglc_idle_timeout_ms * 1000;
}

static void
count_tls_handshake_failure(PgLocalCacheClient *client)
{
	if (client->ssl == NULL || client->tls_ready ||
		client->tls_handshake_failure_counted)
		return;
	client->tls_handshake_failure_counted = true;
	pg_atomic_fetch_add_u64(&pglc_shared->tls_handshake_failures, 1);
	ereport(DEBUG1,
			(errmsg("pg_local_cache TLS handshake failed or timed out")));
}

static bool
drive_tls_handshake(PgLocalCacheClient *client)
{
	int			result;
	int			ssl_error;

	Assert(client->ssl != NULL && !client->tls_ready);
	Assert(client->tls_handshake_wait == 0 ||
		   client->tls_handshake_wait == POLLIN ||
		   client->tls_handshake_wait == POLLOUT);
	if (GetCurrentTimestamp() > tls_handshake_deadline(client))
	{
		client->tls_skip_shutdown = true;
		count_tls_handshake_failure(client);
		return false;
	}
	ERR_clear_error();
	result = SSL_do_handshake(client->ssl);
	ssl_error = SSL_get_error(client->ssl, result);
	if (result == 1)
	{
		TimestampTz handshake_completed_at = GetCurrentTimestamp();

		if (handshake_completed_at > tls_handshake_deadline(client))
		{
			client->tls_skip_shutdown = true;
			count_tls_handshake_failure(client);
			return false;
		}
		client->tls_ready = true;
		client->tls_handshake_wait = 0;
		/* Probe once for application data buffered with the handshake. */
		client->input_ready = true;
		client->last_activity = handshake_completed_at;
		pg_atomic_fetch_add_u64(&pglc_shared->tls_handshakes, 1);
		return true;
	}
	if (ssl_error == SSL_ERROR_WANT_READ ||
		ssl_error == SSL_ERROR_WANT_WRITE)
	{
		client->tls_handshake_wait = ssl_error == SSL_ERROR_WANT_WRITE ?
			POLLOUT : POLLIN;
		return true;
	}
	client->tls_skip_shutdown = true;
	count_tls_handshake_failure(client);
	return false;
}
#endif

static void
run_server(int listener)
{
	PgLocalCacheClient *clients;
	struct pollfd *poll_fds;
	int		   *poll_to_client;
	int			client_slots = pglc_max_clients_per_worker;
	int			next_ready_client = 0;
	int			i;

	/*
	 * Each client owns a bounded 64 KiB request buffer.  Keep the client
	 * array out of the background worker's small process stack.
	 */
	clients = MemoryContextAllocZero(TopMemoryContext,
									 sizeof(PgLocalCacheClient) *
									 client_slots);
	poll_fds = MemoryContextAlloc(TopMemoryContext,
								  sizeof(struct pollfd) * (client_slots + 1));
	poll_to_client = MemoryContextAlloc(TopMemoryContext,
									   sizeof(int) * (client_slots + 1));
	for (i = 0; i < client_slots; i++)
		clients[i].fd = -1;

	for (;;)
	{
		bool		have_buffered_ready = false;
		int			poll_timeout = 250;
		int			poll_count = 1;
		int			poll_result;
		int			ready_clients_processed = 0;
		int			ready_scan_start = next_ready_client;
		int			latch_result;
		int			step;
		TimestampTz now = GetCurrentTimestamp();

		worker_turn_timestamp = now;

		worker_process_config_reload();
		maybe_reload_mappings();
		pglc_reap_pending_relation_forgets();
		retry_deferred_misses();
		poll_timeout = deferred_miss_poll_timeout(poll_timeout);

		/*
		 * A fairness yield leaves complete requests in the client buffer.  Give
		 * a bounded round-robin set of runnable clients one turn before waiting
		 * for more socket events; TCP does not generate another POLLIN edge for
		 * bytes which are already in userspace.
		 */
		for (step = 0; step < client_slots; step++)
		{
			int			client_index =
				(ready_scan_start + step) % client_slots;
			PgLocalCacheClient *client = &clients[client_index];
#ifdef USE_OPENSSL
			bool		buffered_tls_input = client_has_tls_input(client);
#else
			bool		buffered_tls_input = false;
#endif

			if (client->fd < 0 ||
				(!client->input_ready && !buffered_tls_input) ||
				client->output_sent < client->output_used)
				continue;
#ifdef USE_OPENSSL
			Assert(client->tls_handshake_wait == 0 ||
				   client->tls_handshake_wait == POLLIN ||
				   client->tls_handshake_wait == POLLOUT);
			Assert(client->tls_read_wait == 0 ||
				   client->tls_read_wait == POLLIN ||
				   client->tls_read_wait == POLLOUT);
			Assert(client->tls_write_wait == 0 ||
				   client->tls_write_wait == POLLIN ||
				   client->tls_write_wait == POLLOUT);
			if (client->tls_handshake_wait != 0 ||
				client->tls_write_wait != 0 ||
				(client->tls_read_wait != 0 && !client->input_ready))
				continue;
#endif
			client->input_ready = false;
			if (!process_client(client, false))
				close_client(client);
			next_ready_client =
				(client_index + 1) % client_slots;
			if (++ready_clients_processed >= PGLC_READY_CLIENTS_PER_TURN)
				break;
		}
		if (ready_clients_processed == 0)
			next_ready_client =
				(next_ready_client + 1) % client_slots;
		refresh_client_read_activity(clients, client_slots);
		flush_ready_client_outputs(clients, client_slots);

		poll_fds[0].fd = listener;
		poll_fds[0].events = POLLIN;
		poll_fds[0].revents = 0;
		poll_to_client[0] = -1;

		for (i = 0; i < client_slots; i++)
		{
			if (clients[i].fd >= 0)
			{
#ifdef USE_OPENSSL
				if (clients[i].ssl != NULL && !clients[i].tls_ready)
				{
					TimestampTz deadline =
						tls_handshake_deadline(&clients[i]);
					TimestampTz current_time = now;
					int64		remaining_us;

					if (current_time > deadline)
					{
						clients[i].tls_skip_shutdown = true;
						count_tls_handshake_failure(&clients[i]);
						close_client(&clients[i]);
						continue;
					}
					remaining_us = deadline - current_time;
					poll_timeout = Min(poll_timeout,
										 (int) (remaining_us / 1000));
				}
				else
#endif
				if (TimestampDifferenceExceeds(clients[i].last_activity,
										  now,
										  pglc_idle_timeout_ms))
				{
					if (clients[i].output_sent < clients[i].output_used)
						pg_atomic_fetch_add_u64(
							&pglc_shared->slow_client_drops, 1);
					close_client(&clients[i]);
					continue;
				}
				poll_fds[poll_count].fd = clients[i].fd;
#ifdef USE_OPENSSL
				if (clients[i].ssl != NULL)
				{
					poll_fds[poll_count].events = 0;
					Assert(clients[i].tls_read_wait == 0 ||
						   clients[i].tls_read_wait == POLLIN ||
						   clients[i].tls_read_wait == POLLOUT);
					Assert(clients[i].tls_write_wait == 0 ||
						   clients[i].tls_write_wait == POLLIN ||
						   clients[i].tls_write_wait == POLLOUT);
					if (!clients[i].tls_ready)
						poll_fds[poll_count].events =
							clients[i].tls_handshake_wait != 0 ?
							clients[i].tls_handshake_wait : POLLIN;
					else
					{
						if (clients[i].output_sent < clients[i].output_used &&
							clients[i].tls_write_wait == 0)
							poll_fds[poll_count].events |= POLLOUT;
						poll_fds[poll_count].events |=
							clients[i].tls_write_wait |
							clients[i].tls_read_wait;
						if (clients[i].tls_read_wait == 0 &&
							clients[i].output_sent == clients[i].output_used &&
							clients[i].used < PGLC_REQUEST_MAX &&
							!clients[i].input_eof)
							poll_fds[poll_count].events |= POLLIN;
					}
				}
				else
#endif
				poll_fds[poll_count].events =
					(clients[i].output_sent < clients[i].output_used) ?
					POLLOUT :
					(clients[i].used < PGLC_REQUEST_MAX &&
					 !clients[i].input_eof ? POLLIN : 0);
				if ((clients[i].input_ready
#ifdef USE_OPENSSL
					 || client_has_tls_input(&clients[i])
#endif
					) &&
					clients[i].output_sent == clients[i].output_used)
				{
#ifdef USE_OPENSSL
					if (clients[i].tls_handshake_wait == 0 &&
						clients[i].tls_write_wait == 0 &&
						(clients[i].tls_read_wait == 0 ||
						 clients[i].input_ready))
#endif
						have_buffered_ready = true;
				}
				if (poll_fds[poll_count].events == 0)
					continue;
				poll_fds[poll_count].revents = 0;
				poll_to_client[poll_count] = i;
				poll_count++;
			}
		}

		poll_result = poll(poll_fds, poll_count,
						   have_buffered_ready ? 0 : poll_timeout);
		if (poll_result < 0 && errno != EINTR)
			ereport(LOG,
					(errcode_for_socket_access(),
					 errmsg("pg_local_cache poll failed: %m")));

#ifdef USE_POSTMASTER_DEATH_SIGNAL
		if (poll_result <= 0)
#endif
		{
			latch_result = WaitLatch(MyLatch,
								 WL_LATCH_SET | WL_TIMEOUT |
								 WL_POSTMASTER_DEATH,
								 0,
								 PG_WAIT_EXTENSION);
			ResetLatch(MyLatch);
			if (latch_result & WL_POSTMASTER_DEATH)
				proc_exit(1);
		}
#ifdef USE_POSTMASTER_DEATH_SIGNAL
		else if (!PostmasterIsAlive())
			proc_exit(1);
#endif
		CHECK_FOR_INTERRUPTS();

		if (poll_result <= 0)
			continue;
		worker_turn_timestamp = GetCurrentTimestamp();

		if (poll_fds[0].revents & POLLIN)
		{
			int			accepted = 0;

			while (accepted++ < 32)
			{
				int			client_fd;
				int			slot = -1;
				int			flags;
				int			enabled = 1;
				TimestampTz accepted_at;
#ifdef USE_OPENSSL
				SSL		   *ssl = NULL;
#endif

				client_fd = accept(listener, NULL, NULL);
				if (client_fd < 0)
				{
					if (errno == EAGAIN || errno == EWOULDBLOCK)
						break;
					if (errno == EINTR)
						continue;
					ereport(LOG,
							(errcode_for_socket_access(),
							 errmsg("pg_local_cache accept failed: %m")));
					break;
				}
				accepted_at = worker_turn_timestamp;

				for (i = 0; i < client_slots; i++)
				{
					if (clients[i].fd < 0)
					{
						slot = i;
						break;
					}
				}
				if (slot < 0)
				{
					pglc_note_client_limit_rejection();
					close(client_fd);
					continue;
				}
				if (!pglc_try_reserve_client())
				{
					close(client_fd);
					continue;
				}
				worker_client_reservations++;

				flags = fcntl(client_fd, F_GETFL, 0);
				if (flags < 0 ||
					fcntl(client_fd, F_SETFL, flags | O_NONBLOCK) < 0)
				{
					pg_atomic_fetch_add_u64(
						&pglc_shared->rejected_connections, 1);
					pglc_release_clients(1);
					worker_client_reservations--;
					close(client_fd);
					continue;
				}
				if (setsockopt(client_fd, IPPROTO_TCP, TCP_NODELAY,
							   &enabled, sizeof(enabled)) < 0 ||
					setsockopt(client_fd, SOL_SOCKET, SO_KEEPALIVE,
							   &enabled, sizeof(enabled)) < 0)
				{
					pg_atomic_fetch_add_u64(
						&pglc_shared->rejected_connections, 1);
					pglc_release_clients(1);
					worker_client_reservations--;
					close(client_fd);
					continue;
				}
#ifdef USE_OPENSSL
				if (pglc_tls)
				{
					ERR_clear_error();
					ssl = SSL_new(worker_ssl_ctx);
					if (ssl == NULL || SSL_set_fd(ssl, client_fd) != 1)
					{
						if (ssl != NULL)
							SSL_free(ssl);
						pg_atomic_fetch_add_u64(
							&pglc_shared->rejected_connections, 1);
						pglc_release_clients(1);
						worker_client_reservations--;
						close(client_fd);
						continue;
					}
					SSL_set_accept_state(ssl);
					SSL_set_mode(ssl, SSL_MODE_ENABLE_PARTIAL_WRITE);
				}
#endif

				clients[slot].input = MemoryContextAlloc(
					TopMemoryContext, PGLC_REQUEST_MAX);
				clients[slot].output = MemoryContextAlloc(
					TopMemoryContext, PGLC_OUTPUT_BUFFER_MAX);
				clients[slot].fd = client_fd;
				clients[slot].input_start = 0;
				clients[slot].used = 0;
				clients[slot].output_used = 0;
				clients[slot].output_sent = 0;
				clients[slot].close_after_flush = false;
				clients[slot].input_ready = false;
				clients[slot].read_activity_pending = false;
				clients[slot].output_ready = false;
				clients[slot].output_backpressure_reported = false;
				clients[slot].input_eof = false;
				clients[slot].peer_hung_up = false;
				clients[slot].authentication_failures = 0;
#ifdef USE_OPENSSL
				clients[slot].last_activity = ssl == NULL ? accepted_at : 0;
				clients[slot].ssl = ssl;
				clients[slot].tls_ready = ssl == NULL;
				clients[slot].tls_accepted_at = ssl != NULL ?
					accepted_at : 0;
				clients[slot].tls_skip_shutdown = false;
				clients[slot].tls_handshake_failure_counted = false;
				clients[slot].tls_handshake_wait =
					ssl == NULL ? 0 : POLLIN;
				clients[slot].tls_read_wait = 0;
				clients[slot].tls_write_wait = 0;
				clients[slot].tls_write_retry_offset = 0;
				clients[slot].tls_write_retry_length = 0;
#else
				clients[slot].last_activity = accepted_at;
#endif
				clients[slot].authenticated =
					worker_auth_token == NULL || worker_auth_token[0] == '\0';
#ifdef PGLC_TEST_HOOKS
				clients[slot].test_last_request_palloc_count = 0;
#endif
				pg_atomic_fetch_add_u64(&pglc_shared->client_connects, 1);
			}
		}

		for (i = 1; i < poll_count; i++)
		{
			int			client_index = poll_to_client[i];

			if (client_index < 0 || clients[client_index].fd < 0)
				continue;
			if (poll_fds[i].revents & (POLLERR | POLLNVAL))
			{
#ifdef USE_OPENSSL
				clients[client_index].tls_skip_shutdown = true;
#endif
				close_client(&clients[client_index]);
				continue;
			}
#ifdef USE_OPENSSL
			if (clients[client_index].ssl != NULL)
			{
				short		revents = poll_fds[i].revents;
				bool		operation_ok = true;
				bool		read_retry_ready;
				bool		write_retry_ready;

				Assert(clients[client_index].tls_read_wait == 0 ||
					   clients[client_index].tls_read_wait == POLLIN ||
					   clients[client_index].tls_read_wait == POLLOUT);
				Assert(clients[client_index].tls_write_wait == 0 ||
					   clients[client_index].tls_write_wait == POLLIN ||
					   clients[client_index].tls_write_wait == POLLOUT);
				if (!clients[client_index].tls_ready)
				{
					short		required_event =
						clients[client_index].tls_handshake_wait != 0 ?
						clients[client_index].tls_handshake_wait : POLLIN;

					if (revents & (required_event | POLLHUP))
						operation_ok =
							drive_tls_handshake(&clients[client_index]);
					if (!operation_ok || (revents & POLLHUP))
					{
						clients[client_index].tls_skip_shutdown = true;
						close_client(&clients[client_index]);
					}
					continue;
				}

				read_retry_ready =
					clients[client_index].tls_read_wait != 0 &&
					(revents & clients[client_index].tls_read_wait) != 0;
				write_retry_ready =
					clients[client_index].tls_write_wait != 0 &&
					(revents & clients[client_index].tls_write_wait) != 0;
				if (write_retry_ready)
				{
					operation_ok = flush_client_output(&clients[client_index]);
					if (operation_ok && clients[client_index].close_after_flush &&
						clients[client_index].output_sent ==
						clients[client_index].output_used)
						operation_ok = false;
					if (operation_ok &&
						clients[client_index].output_sent ==
						clients[client_index].output_used &&
						(clients[client_index].input_start <
						 clients[client_index].used ||
						 clients[client_index].input_eof))
						clients[client_index].input_ready = true;
				}
				if (operation_ok && read_retry_ready &&
					clients[client_index].fd >= 0)
					operation_ok =
						process_client(&clients[client_index], true);
				if (!operation_ok)
				{
					close_client(&clients[client_index]);
					continue;
				}
				if (clients[client_index].fd < 0)
					continue;

				if (clients[client_index].output_sent <
					clients[client_index].output_used &&
					clients[client_index].tls_write_wait == 0 &&
					(revents & POLLOUT))
				{
					if (!flush_client_output(&clients[client_index]) ||
						(clients[client_index].close_after_flush &&
						 clients[client_index].output_sent ==
						 clients[client_index].output_used))
					{
						close_client(&clients[client_index]);
						continue;
					}
					if (clients[client_index].output_sent ==
						clients[client_index].output_used &&
						(clients[client_index].input_start <
						 clients[client_index].used ||
						 clients[client_index].input_eof))
						clients[client_index].input_ready = true;
				}
				if (clients[client_index].fd >= 0 &&
					clients[client_index].output_sent ==
					clients[client_index].output_used &&
					clients[client_index].tls_read_wait == 0 &&
					(revents & POLLIN))
				{
					if (!process_client(&clients[client_index], false))
						close_client(&clients[client_index]);
				}
				if (clients[client_index].fd < 0)
					continue;
				if (revents & POLLHUP)
				{
					if (clients[client_index].tls_read_wait != 0 ||
						clients[client_index].tls_write_wait != 0 ||
						clients[client_index].output_sent <
						clients[client_index].output_used)
					{
						clients[client_index].tls_skip_shutdown = true;
						close_client(&clients[client_index]);
						continue;
					}
					if (!(revents & POLLIN))
					{
						if (!process_client(&clients[client_index], false))
						{
							close_client(&clients[client_index]);
							continue;
						}
						if (clients[client_index].fd < 0)
							continue;
					}
					if (clients[client_index].tls_read_wait != 0 ||
						clients[client_index].tls_write_wait != 0 ||
						clients[client_index].output_sent <
						clients[client_index].output_used)
					{
						clients[client_index].tls_skip_shutdown = true;
						close_client(&clients[client_index]);
						continue;
					}
					clients[client_index].input_ready = true;
				}
				continue;
			}
#endif
			if (poll_fds[i].revents & POLLOUT)
				clients[client_index].output_ready = true;
			if (poll_fds[i].revents & POLLIN)
			{
				if (!process_client(&clients[client_index], false))
					close_client(&clients[client_index]);
			}
			if (clients[client_index].fd < 0)
				continue;
			if (poll_fds[i].revents & POLLHUP)
				clients[client_index].peer_hung_up = true;
		}
		refresh_client_read_activity(clients, client_slots);
		flush_ready_client_outputs(clients, client_slots);
	}

	for (i = 0; i < client_slots; i++)
		close_client(&clients[i]);
	pfree(poll_to_client);
	pfree(poll_fds);
	pfree(clients);
}

static PgLocalCacheDeferredResult
enqueue_deferred_miss(PgLocalCacheClient *client, Size request_length,
					  TimestampTz deadline)
{
	PgLocalCacheDeferredMiss *miss;
	MemoryContext old_context;

	if (GetCurrentTimestamp() >= deadline)
		return PGLC_DEFERRED_EXPIRED;
	if (client->deferred_miss != NULL ||
		deferred_misses_count >= pglc_max_deferred_misses ||
		request_length > PGLC_DEFERRED_REQUEST_BYTES_MAX -
		deferred_misses_bytes)
	{
		if (!client->retrying_deferred_miss)
			deferred_rejections_total++;
		return PGLC_DEFERRED_QUEUE_FULL;
	}

	old_context = MemoryContextSwitchTo(TopMemoryContext);
	miss = MemoryContextAllocZero(TopMemoryContext, sizeof(*miss));
	MemoryContextSwitchTo(old_context);
	miss->client = client;
	miss->request_offset = client->input_start;
	miss->request_length = request_length;
	miss->deadline = deadline;
	miss->mapping_generation = worker_mapping_generation;
	if (deferred_misses_tail == NULL)
		deferred_misses_head = miss;
	else
		deferred_misses_tail->next = miss;
	deferred_misses_tail = miss;
	client->deferred_miss = miss;
	client->deferred_deadline = deadline;
	client->deferred_deadline_expired = false;
	deferred_misses_count++;
	deferred_misses_bytes += request_length;
	if (!client->retrying_deferred_miss)
		deferred_misses_total++;
	return PGLC_DEFERRED_QUEUED;
}

static void
remove_deferred_miss(PgLocalCacheClient *client)
{
	PgLocalCacheDeferredMiss *miss = client->deferred_miss;
	PgLocalCacheDeferredMiss *previous = NULL;
	PgLocalCacheDeferredMiss *current;

	if (miss == NULL)
		return;
	for (current = deferred_misses_head; current != NULL;
		 current = current->next)
	{
		if (current == miss)
			break;
		previous = current;
	}
	Assert(current == miss);
	if (previous == NULL)
		deferred_misses_head = miss->next;
	else
		previous->next = miss->next;
	if (deferred_misses_tail == miss)
		deferred_misses_tail = previous;
	Assert(deferred_misses_count > 0);
	Assert(deferred_misses_bytes >= miss->request_length);
	deferred_misses_count--;
	deferred_misses_bytes -= miss->request_length;
	client->deferred_miss = NULL;
	pfree(miss);
}

static void
rotate_deferred_miss_head(void)
{
	PgLocalCacheDeferredMiss *miss = deferred_misses_head;

	if (miss == NULL || miss->next == NULL)
		return;
	deferred_misses_head = miss->next;
	miss->next = NULL;
	deferred_misses_tail->next = miss;
	deferred_misses_tail = miss;
}

static void
retry_deferred_misses(void)
{
	int			attempts = Min(deferred_misses_count,
								 PGLC_DEFERRED_RETRIES_PER_TURN);
	int			attempt;

	for (attempt = 0; attempt < attempts && deferred_misses_head != NULL;
		 attempt++)
	{
		PgLocalCacheDeferredMiss *miss = deferred_misses_head;
		PgLocalCacheClient *client = miss->client;

		if (GetCurrentTimestamp() >= miss->deadline)
			client->deferred_deadline_expired = true;
		if (client->output_sent < client->output_used)
		{
			rotate_deferred_miss_head();
			continue;
		}
#ifdef USE_OPENSSL
		if (client->ssl != NULL && client->tls_write_wait != 0)
		{
			rotate_deferred_miss_head();
			continue;
		}
#endif
		if (client->deferred_deadline_expired)
		{
			MemoryContext old_context =
				MemoryContextSwitchTo(command_context);
			Size		response_length;
			char	   *response = pglc_resp_error(
				"ERR MGET deadline exceeded", &response_length);
			bool		queued = queue_response(client, response,
											 response_length, false);

			MemoryContextSwitchTo(old_context);
			MemoryContextReset(command_context);
			if (!queued)
			{
				rotate_deferred_miss_head();
				continue;
			}
			Assert(client->input_start == miss->request_offset);
			client->input_start += miss->request_length;
			client->deferred_miss = NULL;
			client->deferred_deadline = 0;
			client->deferred_deadline_expired = false;
			client->retrying_deferred_miss = false;
			client->input_ready = client->input_start < client->used ||
				client->input_eof;
			deferred_timeouts_total++;
			deferred_misses_head = miss->next;
			deferred_misses_count--;
			deferred_misses_bytes -= miss->request_length;
			if (deferred_misses_head == NULL)
				deferred_misses_tail = NULL;
			pfree(miss);
			continue;
		}

		/* The input cursor retains wire bytes, never mapping pointers. */
		if (miss->mapping_generation != worker_mapping_generation)
			maybe_reload_mappings();
		Assert(miss->request_offset + miss->request_length <= client->used);
		deferred_misses_head = miss->next;
		if (deferred_misses_head == NULL)
			deferred_misses_tail = NULL;
		miss->next = NULL;
		deferred_misses_count--;
		deferred_misses_bytes -= miss->request_length;
		client->deferred_miss = NULL;
		client->retrying_deferred_miss = true;
		client->deferred_deadline = miss->deadline;
		client->deferred_deadline_expired = false;
		pfree(miss);
		if (!process_client(client, false))
			close_client(client);
	}
}

static int
deferred_miss_poll_timeout(int current_timeout)
{
	PgLocalCacheDeferredMiss *miss;
	TimestampTz now = GetCurrentTimestamp();
	int64		remaining_us;
	int64		remaining_ms;

	for (miss = deferred_misses_head; miss != NULL; miss = miss->next)
	{
		PgLocalCacheClient *client = miss->client;
		bool		transport_blocked =
			client->output_sent < client->output_used;

#ifdef USE_OPENSSL
		transport_blocked |= client->ssl != NULL &&
			client->tls_write_wait != 0;
#endif
		remaining_us = miss->deadline - now;
		if (remaining_us <= 0)
		{
			client->deferred_deadline_expired = true;
			if (!transport_blocked)
				return 0;
			continue;
		}
		remaining_ms = (remaining_us + 999) / 1000;
		current_timeout = Min(current_timeout,
							  (int) Min(remaining_ms, INT_MAX));
	}
	return current_timeout;
}

static void
close_client(PgLocalCacheClient *client)
{
	remove_deferred_miss(client);
	if (client->fd >= 0)
	{
#ifdef USE_OPENSSL
		if (client->ssl != NULL)
		{
			if (!client->tls_ready)
				count_tls_handshake_failure(client);
			if (client->tls_ready && !client->tls_skip_shutdown &&
				client->tls_read_wait == 0 &&
				client->tls_write_wait == 0)
			{
				int			shutdown_result;
				int			ssl_error;

				ERR_clear_error();
				shutdown_result = SSL_shutdown(client->ssl);
				ssl_error = SSL_get_error(client->ssl, shutdown_result);
				(void) ssl_error;
			}
			SSL_free(client->ssl);
			client->ssl = NULL;
		}
#endif
		close(client->fd);
		pg_atomic_fetch_add_u64(&pglc_shared->client_disconnects, 1);
		pglc_release_clients(1);
		Assert(worker_client_reservations > 0);
		worker_client_reservations--;
	}
	if (client->input != NULL)
	{
		pfree(client->input);
		client->input = NULL;
	}
	if (client->output != NULL)
	{
		pfree(client->output);
		client->output = NULL;
	}
	client->fd = -1;
	client->input_start = 0;
	client->used = 0;
	client->output_used = 0;
	client->output_sent = 0;
	client->deferred_deadline = 0;
	client->deferred_deadline_expired = false;
	client->retrying_deferred_miss = false;
	client->close_after_flush = false;
	client->input_ready = false;
	client->read_activity_pending = false;
	client->output_ready = false;
	client->output_backpressure_reported = false;
	client->input_eof = false;
	client->peer_hung_up = false;
	client->authentication_failures = 0;
	client->authenticated = false;
#ifdef USE_OPENSSL
	client->tls_ready = false;
	client->tls_accepted_at = 0;
	client->tls_skip_shutdown = false;
	client->tls_handshake_failure_counted = false;
	client->tls_handshake_wait = 0;
	client->tls_read_wait = 0;
	client->tls_write_wait = 0;
	client->tls_write_retry_offset = 0;
	client->tls_write_retry_length = 0;
#endif
}

static void
compact_client_input(PgLocalCacheClient *client)
{
	if (client->input_start == 0)
		return;
	if (client->input_start < client->used)
	{
		memmove(client->input, client->input + client->input_start,
				client->used - client->input_start);
		client->used -= client->input_start;
	}
	else
		client->used = 0;
	client->input_start = 0;
}

static void
record_client_output_backpressure(PgLocalCacheClient *client)
{
	if (!client->output_backpressure_reported)
	{
		pg_atomic_fetch_add_u64(
			&pglc_shared->output_backpressure_events, 1);
		client->output_backpressure_reported = true;
	}
}

static bool
flush_client_output(PgLocalCacheClient *client)
{
	bool		wrote = false;
#ifdef USE_OPENSSL
	bool		retry_partial = client->ssl != NULL;
#else
	bool		retry_partial = false;
#endif

	client->output_ready = false;

	while (client->output_sent < client->output_used)
	{
		ssize_t		written = client_send(
			client, client->output + client->output_sent,
			client->output_used - client->output_sent);

		if (written > 0)
		{
			client->output_sent += (Size) written;
			wrote = true;
			/* A short nonblocking write is backpressure too.  The old flush loop
			 * immediately retried until EAGAIN; the readiness loop must record the
			 * same episode without issuing another write in this turn.
			 */
			if (!retry_partial &&
				client->output_sent < client->output_used)
				record_client_output_backpressure(client);
			if (retry_partial)
				continue;
			break;
		}
		if (written < 0 && errno == EINTR && retry_partial)
			continue;
		if (written < 0 && errno == EINTR)
			return true;
		if (written < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
		{
			record_client_output_backpressure(client);
			if (wrote)
			{
#ifdef USE_OPENSSL
				client->last_activity = client->ssl != NULL ?
					GetCurrentTimestamp() : worker_turn_timestamp;
#else
				client->last_activity = worker_turn_timestamp;
#endif
			}
			return true;
		}
		return false;
	}
	if (wrote)
	{
#ifdef USE_OPENSSL
		client->last_activity = client->ssl != NULL ?
			GetCurrentTimestamp() : worker_turn_timestamp;
#else
		client->last_activity = worker_turn_timestamp;
#endif
	}
	if (client->output_sent == client->output_used)
	{
		client->output_used = 0;
		client->output_sent = 0;
		client->output_backpressure_reported = false;
	}
	return true;
}

static void
refresh_client_read_activity(PgLocalCacheClient *clients, int client_slots)
{
	int			i;
	bool		have_pending_read = false;
	TimestampTz activity_timestamp;

	for (i = 0; i < client_slots; i++)
	{
		if (clients[i].fd >= 0 && clients[i].read_activity_pending)
		{
			have_pending_read = true;
			break;
		}
	}
	if (!have_pending_read)
		return;

	activity_timestamp = GetCurrentTimestamp();
	for (i = 0; i < client_slots; i++)
	{
		if (!clients[i].read_activity_pending)
			continue;
		if (clients[i].fd >= 0)
			clients[i].last_activity = activity_timestamp;
		clients[i].read_activity_pending = false;
	}
}

static void
flush_ready_client_outputs(PgLocalCacheClient *clients, int client_slots)
{
	int			i;
	bool		have_flush_timestamp = false;

	for (i = 0; i < client_slots; i++)
	{
		PgLocalCacheClient *client = &clients[i];

		if (client->fd < 0)
			continue;
#ifdef USE_OPENSSL
		/* TLS retains its existing write/retry path. */
		if (client->ssl != NULL)
			continue;
#endif
		if (client->output_ready &&
			client->output_sent < client->output_used)
		{
			if (!have_flush_timestamp)
			{
				worker_turn_timestamp = GetCurrentTimestamp();
				have_flush_timestamp = true;
			}
			if (!flush_client_output(client))
			{
				close_client(client);
				continue;
			}
			if (client->close_after_flush &&
				client->output_sent == client->output_used)
			{
				close_client(client);
				continue;
			}
			if (client->output_sent == client->output_used &&
				(client->input_start < client->used || client->input_eof))
				client->input_ready = true;
		}

		if (client->fd < 0)
			continue;
		if (client->close_after_flush &&
			client->output_sent == client->output_used)
		{
			close_client(client);
			continue;
		}
		if (client->peer_hung_up)
		{
			if (client->output_sent < client->output_used)
				close_client(client);
			else if (client->deferred_miss != NULL)
			{
				if (!client->input_eof &&
					client->used < PGLC_REQUEST_MAX)
					client->input_ready = true;
			}
			else if (client->input_start < client->used)
				client->input_ready = true;
			else
				close_client(client);
		}
	}
}

static bool
queue_response(PgLocalCacheClient *client,
			   const char *response, Size response_length,
			   bool close_after)
{
	if (client->output_sent != 0 ||
		response_length > PGLC_OUTPUT_BUFFER_MAX - client->output_used)
		return false;
	memcpy(client->output + client->output_used, response, response_length);
	client->output_used += response_length;
	client->output_ready = true;
	client->close_after_flush |= close_after;
	return true;
}

static bool
finish_client_turn(PgLocalCacheClient *client)
{
	if (client->input_start == client->used
#ifdef USE_OPENSSL
		&& (client->ssl == NULL || client->tls_write_wait != 0 ||
			client->tls_read_wait == 0)
#endif
		)
	{
		client->input_start = 0;
		client->used = 0;
	}
	if (client->input_eof && client->deferred_miss == NULL &&
		client->input_start == client->used)
		client->close_after_flush = true;
#ifdef USE_OPENSSL
	if (client->ssl != NULL && client->tls_write_wait != 0)
		return true;
	if (client->ssl != NULL)
	{
		if (!flush_client_output(client))
			return false;
		return !(client->close_after_flush &&
				 client->output_sent == client->output_used);
	}
#endif
	return true;
}

static bool
process_client(PgLocalCacheClient *client, bool retry_tls_read)
{
	bool		read_attempted = false;
	int			commands_processed = 0;

	(void) retry_tls_read;
#ifdef USE_OPENSSL
	Assert(client->tls_read_wait == 0 ||
		   client->tls_read_wait == POLLIN ||
		   client->tls_read_wait == POLLOUT);
#endif
	if (client->output_sent < client->output_used
#ifdef USE_OPENSSL
		&& (client->ssl == NULL || client->tls_read_wait == 0)
#endif
		)
		return true;
	client->input_ready = false;
	if (client->deferred_miss != NULL)
	{
		ssize_t		received = -1;

		if (client->used < PGLC_REQUEST_MAX && !client->input_eof
#ifdef USE_OPENSSL
			&& (client->ssl == NULL || client->tls_read_wait == 0 ||
				retry_tls_read)
#endif
			)
			received = client_recv(client, client->input + client->used,
								   PGLC_REQUEST_MAX - client->used);
		else
			return finish_client_turn(client);
		if (received < 0 && errno == EINTR)
			return finish_client_turn(client);
		if (received == 0)
		{
			client->input_eof = true;
			return finish_client_turn(client);
		}
		if (received > 0)
		{
			client->used += (Size) received;
			client->read_activity_pending = true;
			return finish_client_turn(client);
		}
		if (errno == EAGAIN || errno == EWOULDBLOCK)
			return finish_client_turn(client);
		return false;
	}
	maybe_reload_mappings();

	for (;;)
	{
		while (client->input_start < client->used)
		{
			PgLocalCacheRespArg args[PGLC_RESP_MAX_ARGS];
			int			argc;
			Size		consumed;
			const char *protocol_error;
			int			parse_result;
			MemoryContext previous_context;
			char	   *response;
			Size		response_length;
			bool		close_after = false;
			bool		queued;

			/*
			 * Reserve room for the largest possible response before executing a
			 * command.  SET and DEL must never be replayed merely because a
			 * nonblocking send could not accept their response.
			 */
			if (PGLC_OUTPUT_BUFFER_MAX - client->output_used <
				PGLC_RESPONSE_MAX)
			{
#ifdef USE_OPENSSL
				if (client->ssl != NULL)
				{
					if (client->tls_write_wait != 0)
					{
						client->input_ready = true;
						return true;
					}
					if (!flush_client_output(client))
						return false;
				}
				else
				{
					client->input_ready = true;
					return finish_client_turn(client);
				}
#else
				client->input_ready = true;
				return finish_client_turn(client);
#endif
				if (client->output_sent < client->output_used)
				{
					client->input_ready = true;
					return true;
				}
			}

			worker_process_config_reload();
			CHECK_FOR_INTERRUPTS();
#ifdef PGLC_TEST_HOOKS
			pglc_test_begin_request_palloc_count();
#endif
			parse_result = pglc_resp_parse(
				client->input + client->input_start,
				client->used - client->input_start,
				args, &argc, &consumed, &protocol_error);
			if (parse_result == 0)
			{
#ifdef PGLC_TEST_HOOKS
				(void) pglc_test_end_request_palloc_count(false);
#endif
#ifdef USE_OPENSSL
				if (client->ssl == NULL || client->tls_read_wait == 0)
					compact_client_input(client);
#else
				compact_client_input(client);
#endif
				if (client->input_eof)
				{
					client->input_start = client->used;
					break;
				}
				if (client->used == PGLC_REQUEST_MAX)
				{
					client->close_after_flush = true;
					return finish_client_turn(client);
				}
				break;
			}
			if (parse_result < 0)
			{
				Size		error_length;
				char	   *error_response;

				pg_atomic_fetch_add_u64(&pglc_shared->protocol_errors, 1);
				pg_atomic_fetch_add_u64(
					&pglc_shared->stats_shards[worker_slot].client_request_errors, 1);
				error_response = pglc_resp_error(protocol_error, &error_length);
				queued = queue_response(client, error_response,
										error_length, true);
				pfree(error_response);
				if (!queued)
				{
#ifdef PGLC_TEST_HOOKS
					client->test_last_request_palloc_count =
						pglc_test_end_request_palloc_count(true);
#endif
					return false;
				}
#ifdef PGLC_TEST_HOOKS
				client->test_last_request_palloc_count =
					pglc_test_end_request_palloc_count(true);
#endif
				client->input_start = client->used;
				return finish_client_turn(client);
			}

			if (!client->retrying_deferred_miss)
				pg_atomic_fetch_add_u64(
					&pglc_shared->stats_shards[worker_slot].client_requests, 1);
			if (try_fast_mget_hit(client, args, argc))
			{
#ifdef PGLC_TEST_HOOKS
				client->test_last_request_palloc_count =
					pglc_test_end_request_palloc_count(true);
#endif
				if (client->retrying_deferred_miss)
				{
					client->retrying_deferred_miss = false;
					client->deferred_deadline = 0;
					client->deferred_deadline_expired = false;
				}
				client->input_start += consumed;
				commands_processed++;
				if (commands_processed >= pglc_max_pipeline_commands)
				{
					client->input_ready = client->input_start < client->used;
					return finish_client_turn(client);
				}
				continue;
			}
			previous_context = MemoryContextSwitchTo(command_context);
			response = execute_command(client, args, argc, consumed,
							   &response_length, &close_after);
			if (response == NULL && client->deferred_miss != NULL)
			{
#ifdef PGLC_TEST_HOOKS
				client->test_last_request_palloc_count =
					pglc_test_end_request_palloc_count(true);
#endif
				Assert(CurrentMemoryContext == command_context);
				MemoryContextSwitchTo(previous_context);
				MemoryContextReset(command_context);
				return finish_client_turn(client);
			}
			if (response == NULL)
				elog(ERROR, "pg_local_cache deferred MGET lost its queue entry");
			if (response_length > 0 && response[0] == '-')
				pg_atomic_fetch_add_u64(
					&pglc_shared->stats_shards[worker_slot].client_request_errors, 1);
			queued = queue_response(client, response, response_length,
								close_after);
#ifdef PGLC_TEST_HOOKS
			client->test_last_request_palloc_count =
				pglc_test_end_request_palloc_count(true);
#endif
			if (queued)
				client->input_start += consumed;
			if (queued && client->retrying_deferred_miss)
			{
				client->retrying_deferred_miss = false;
				client->deferred_deadline = 0;
				client->deferred_deadline_expired = false;
			}
			Assert(CurrentMemoryContext == command_context);
			MemoryContextSwitchTo(previous_context);
			MemoryContextReset(command_context);

			if (!queued)
				return false;
			if (close_after)
			{
				client->input_start = client->used;
				return finish_client_turn(client);
			}

			commands_processed++;
			if (commands_processed >= pglc_max_pipeline_commands)
			{
				client->input_ready = client->input_start < client->used;
				return finish_client_turn(client);
			}
		}

		if (read_attempted)
			break;
		if (client->input_eof)
			break;
#ifdef USE_OPENSSL
		if (client->ssl != NULL && client->tls_read_wait != 0 &&
			!retry_tls_read)
			break;
#endif

#ifdef USE_OPENSSL
		if (client->tls_read_wait == 0)
#endif
			compact_client_input(client);
		for (;;)
		{
			ssize_t		received;

#ifdef USE_OPENSSL
			if (client->ssl != NULL && client->tls_read_wait == 0 &&
				client->output_sent < client->output_used)
			{
				if (client->tls_write_wait != 0)
					return finish_client_turn(client);
				if (!flush_client_output(client))
					return false;
				if (client->output_sent < client->output_used)
				{
					client->input_ready = true;
					return true;
				}
			}
			Assert(client->ssl == NULL || client->output_sent ==
				   client->output_used || client->tls_read_wait != 0);
#endif
			received = client_recv(client, client->input + client->used,
							   PGLC_REQUEST_MAX - client->used);
			if (received < 0 && errno == EINTR)
				continue;
			read_attempted = true;
			if (received == 0)
			{
				client->input_eof = true;
				break;
			}
			if (received > 0)
			{
				client->used += (Size) received;
				client->read_activity_pending = true;
#ifdef USE_OPENSSL
				if (client->ssl != NULL &&
					client->output_sent < client->output_used)
				{
					client->input_ready = true;
					return true;
				}
#endif
				break;
			}
			if (errno == EAGAIN || errno == EWOULDBLOCK)
				return finish_client_turn(client);
			return false;
		}
	}
	return finish_client_turn(client);
}

static char *
execute_command(PgLocalCacheClient *client, PgLocalCacheRespArg *args, int argc,
				Size request_length, Size *response_length,
				bool *close_after)
{
	MemoryContext error_context = CurrentMemoryContext;
	char	   *response = NULL;

	PG_TRY();
	{
		response = execute_command_inner(client, args, argc, request_length,
											 response_length, close_after);
	}
	PG_CATCH();
	{
		ErrorData  *error_data;
		char	   *message;

		MemoryContextSwitchTo(error_context);
		error_data = CopyErrorData();
		FlushErrorState();
		if (error_data->elevel >= FATAL || ProcDiePending)
			ReThrowError(error_data);
		disable_all_timeouts(false);
		QueryCancelPending = false;
		abort_spi_transaction(error_context);
		message = psprintf("ERR PostgreSQL: %s", error_data->message);
		response = pglc_resp_error(message, response_length);
		FreeErrorData(error_data);
	}
	PG_END_TRY();
	return response;
}

static bool
constant_time_token_equals(const PgLocalCacheRespArg *argument)
{
	Size		expected_length = strlen(worker_auth_token);
	Size		max_length = Max(expected_length, argument->len);
	Size		difference = expected_length ^ argument->len;
	Size		i;

	for (i = 0; i < max_length; i++)
	{
		unsigned char expected = i < expected_length ?
			(unsigned char) worker_auth_token[i] : 0;
		unsigned char actual = i < argument->len ?
			(unsigned char) argument->data[i] : 0;

		difference |= expected ^ actual;
	}
	return difference == 0;
}

static char *
raw_response(const char *value, Size *length)
{
	char	   *response = pstrdup(value);

	*length = strlen(value);
	return response;
}

static char *
worker_stats_json(void)
{
	char	   *base = pglc_stats_json();
	Size		base_length = strlen(base);
	StringInfoData expanded;

	if (base_length == 0 || base[base_length - 1] != '}')
		return base;
	initStringInfo(&expanded);
	appendBinaryStringInfo(&expanded, base, (int) base_length - 1);
	appendStringInfo(&expanded,
					 ",\"deferred_misses_total\":" UINT64_FORMAT
					 ",\"deferred_misses_current\":%d"
					 ",\"deferred_timeouts_total\":" UINT64_FORMAT
					 ",\"deferred_rejections_total\":" UINT64_FORMAT,
					 deferred_misses_total, deferred_misses_count,
					 deferred_timeouts_total, deferred_rejections_total);
	appendStringInfoChar(&expanded, '}');
	return expanded.data;
}

static char *
execute_command_inner(PgLocalCacheClient *client, PgLocalCacheRespArg *args, int argc,
					  Size request_length, Size *response_length,
					  bool *close_after)
{
	char	   *raw_key;
	char	   *key_error;
	PgLocalCacheMapping *mapping;
	bool		is_delete;
	bool		is_set;

	if (argc == 0)
		return pglc_resp_error("ERR empty command", response_length);

	if (pglc_resp_arg_equals(&args[0], "AUTH"))
	{
		const PgLocalCacheRespArg *token;
		bool		username_matches = true;

		if (argc != 2 && argc != 3)
			return pglc_resp_error("ERR wrong number of arguments for AUTH",
								  response_length);
		if (argc == 3)
		{
			const char *expected_username =
				(pglc_role != NULL && pglc_role[0] != '\0') ?
				pglc_role : "default";
			Size		expected_length = strlen(expected_username);

			username_matches =
				args[1].len == expected_length &&
				memcmp(args[1].data, expected_username, expected_length) == 0;
		}
		token = &args[argc - 1];
		if (username_matches && constant_time_token_equals(token))
		{
			client->authenticated = true;
			client->authentication_failures = 0;
			return pglc_resp_simple("OK", response_length);
		}
		client->authenticated = false;
		client->authentication_failures++;
		pg_atomic_fetch_add_u64(&pglc_shared->authentication_failures, 1);
		if (client->authentication_failures >= PGLC_MAX_AUTH_FAILURES)
			*close_after = true;
		return pglc_resp_error("WRONGPASS invalid authentication token",
							  response_length);
	}

	if (!client->authenticated)
		return pglc_resp_error("NOAUTH Authentication required",
							  response_length);

#ifdef PGLC_TEST_HOOKS
	if (argc == 1 &&
		pglc_resp_arg_equals(&args[0],
							 "PGLC_TEST_LAST_REQUEST_PALLOC_COUNT"))
		return pglc_resp_integer((int64) client->test_last_request_palloc_count,
							 response_length);
#endif

	/* Keep the dominant cache commands at the front of the dispatch path. */
	is_set = pglc_resp_arg_equals(&args[0], "SET");
	is_delete = !is_set && pglc_resp_arg_equals(&args[0], "DEL");
	if (is_set || is_delete)
	{
		if ((is_set && argc != 3) ||
			(is_delete && argc != 2))
			return pglc_resp_error("ERR wrong number of arguments",
								  response_length);
		if (!resolve_wire_key(&args[1], &mapping, &raw_key, &key_error))
			return pglc_resp_error(key_error, response_length);
		if (is_set)
		{
			pg_atomic_fetch_add_u64(
				&pglc_shared->stats_shards[worker_slot].client_sets, 1);
			return command_set(mapping, raw_key, &args[2], response_length);
		}
		pg_atomic_fetch_add_u64(
			&pglc_shared->stats_shards[worker_slot].client_dels, 1);
		return command_delete(mapping, raw_key, response_length);
	}
	if (pglc_resp_arg_equals(&args[0], "MGET"))
	{
		if (argc < 2)
			return pglc_resp_error("ERR wrong number of arguments",
								  response_length);
		if (argc > PGLC_MGET_MAX_KEYS + 1)
			return pglc_resp_error("ERR MGET accepts at most 1024 keys",
								  response_length);
		return command_mget(client, args, argc, request_length,
						response_length);
	}

	if (pglc_resp_arg_equals(&args[0], "PING"))
	{
		if (argc == 1)
			return pglc_resp_simple("PONG", response_length);
		if (argc == 2)
		{
			if (args[1].len > PGLC_VALUE_MAX)
				return pglc_resp_error("ERR PING payload is too large",
									  response_length);
			return pglc_resp_bulk(args[1].data, args[1].len, response_length);
		}
		return pglc_resp_error("ERR wrong number of arguments for PING",
							  response_length);
	}
	if (pglc_resp_arg_equals(&args[0], "ECHO"))
	{
		if (argc != 2)
			return pglc_resp_error("ERR wrong number of arguments for ECHO",
							  response_length);
		if (args[1].len > PGLC_VALUE_MAX)
			return pglc_resp_error("ERR ECHO payload is too large",
								  response_length);
		return pglc_resp_bulk(args[1].data, args[1].len, response_length);
	}
	if (pglc_resp_arg_equals(&args[0], "HELLO"))
	{
		if (argc != 2 || args[1].len != 1 || args[1].data[0] != '2')
			return pglc_resp_error("NOPROTO only RESP2 is supported",
								  response_length);
		return raw_response(
			"*14\r\n"
			"$6\r\nserver\r\n$14\r\npg_local_cache\r\n"
			"$7\r\nversion\r\n$" PGLC_VERSION_LENGTH "\r\n" PGLC_VERSION "\r\n"
			"$5\r\nproto\r\n:2\r\n"
			"$2\r\nid\r\n:0\r\n"
			"$4\r\nmode\r\n$10\r\nstandalone\r\n"
			"$4\r\nrole\r\n$6\r\nmaster\r\n"
			"$7\r\nmodules\r\n*0\r\n",
			response_length);
	}
	if (pglc_resp_arg_equals(&args[0], "INFO"))
	{
		const char *info =
			"# Server\r\n"
			"server:pg_local_cache\r\n"
			"pg_local_cache_version:" PGLC_VERSION "\r\n"
			"redis_mode:standalone\r\n";

		if (argc != 1 && argc != 2)
			return pglc_resp_error("ERR wrong number of arguments for INFO",
								  response_length);
		return pglc_resp_bulk(info, strlen(info), response_length);
	}
	if (pglc_resp_arg_equals(&args[0], "QUIT"))
	{
		*close_after = true;
		return pglc_resp_simple("OK", response_length);
	}
	if (pglc_resp_arg_equals(&args[0], "CLIENT"))
	{
		if (argc == 4 && pglc_resp_arg_equals(&args[1], "SETINFO"))
			return pglc_resp_simple("OK", response_length);
		if (argc == 3 && pglc_resp_arg_equals(&args[1], "SETNAME"))
			return pglc_resp_simple("OK", response_length);
		if (argc == 2 && pglc_resp_arg_equals(&args[1], "GETNAME"))
			return pglc_resp_null(response_length);
		if (argc == 2 && pglc_resp_arg_equals(&args[1], "ID"))
			return pglc_resp_integer((int64) MyProcPid, response_length);
		return pglc_resp_error("ERR unsupported CLIENT subcommand",
							  response_length);
	}
	if (pglc_resp_arg_equals(&args[0], "COMMAND"))
		return raw_response("*0\r\n", response_length);
	if (pglc_resp_arg_equals(&args[0], "SELECT"))
	{
		if (argc == 2 && args[1].len == 1 && args[1].data[0] == '0')
			return pglc_resp_simple("OK", response_length);
		return pglc_resp_error("ERR only RESP database 0 is supported",
							  response_length);
	}
	if (pglc_resp_arg_equals(&args[0], "STAT") ||
		pglc_resp_arg_equals(&args[0], "STATS"))
	{
		char	   *json;

		if (argc != 1)
			return pglc_resp_error("ERR wrong number of arguments for STAT",
								  response_length);
		json = worker_stats_json();
		return pglc_resp_bulk(json, strlen(json), response_length);
	}
	if (pglc_resp_arg_equals(&args[0], "INVALIDATE"))
	{
		char	   *scope;
		const char *database_name = pglc_database;
		char	   *raw_invalidate_key = NULL;
		char	   *invalidate_error = NULL;
		char	   *canonical = NULL;
		Datum		key_values[PGLC_MAX_KEY_COLUMNS];
		PgLocalCacheMapping *invalidate_mapping = NULL;
		uint64		count;

		if (argc != 2 || args[1].len == 0 ||
			args[1].len >= PGLC_REQUEST_MAX ||
			memchr(args[1].data, '\0', args[1].len) != NULL)
			return pglc_resp_error("ERR INVALIDATE expects one cache scope",
								  response_length);
		scope = pnstrdup(args[1].data, args[1].len);
		pg_verifymbstr(scope, args[1].len, false);
		if (strcmp(scope, "CRUD") == 0)
			count = pglc_cache_invalidate_all();
		else if (strcmp(scope, psprintf("CRUD:%s", database_name)) == 0)
			count = pglc_cache_invalidate_database(MyDatabaseId);
		else if (strncmp(scope, "CRUD:", 5) == 0)
		{
			if (resolve_wire_key(&args[1], &invalidate_mapping,
								 &raw_invalidate_key, &invalidate_error))
			{
				if (!canonicalize_key(invalidate_mapping, raw_invalidate_key,
									  key_values, &canonical,
									  &invalidate_error))
					return pglc_resp_error(invalidate_error, response_length);
				count = pglc_cache_invalidate_key(invalidate_mapping, canonical);
			}
			else
			{
				int			i;

				count = 0;
				for (i = 0; i < worker_mapping_count; i++)
				{
					char	   *table_scope;

					table_scope = psprintf("CRUD:%s.%s.%s",
							database_name, worker_mappings[i].schema_name,
							worker_mappings[i].relation_name);

					if (strcmp(scope, table_scope) == 0)
					{
						invalidate_mapping = &worker_mappings[i];
						break;
					}
				}
				if (invalidate_mapping == NULL)
					return pglc_resp_error(invalidate_error != NULL ?
						invalidate_error : "ERR unknown KVik cache scope",
						response_length);
				count = pglc_cache_invalidate_namespace(MyDatabaseId,
											   invalidate_mapping->nspace);
			}
		}
		else
			return pglc_resp_error("ERR invalid CRUD cache scope",
								  response_length);
		return pglc_resp_integer((int64) count, response_length);
	}

	return pglc_resp_error("ERR unsupported command", response_length);
}

static bool
resolve_wire_key(const PgLocalCacheRespArg *wire_key,
				 PgLocalCacheMapping **mapping, char **raw_key,
				 char **error)
{
	Size		key_length;
	const char *database_name = pglc_database;
	int			i;

	*mapping = NULL;
	*raw_key = NULL;
	if (wire_key->len == 0 || wire_key->len >= PGLC_REQUEST_MAX)
	{
		*error = "ERR invalid key";
		return false;
	}
	if (memchr(wire_key->data, '\0', wire_key->len) != NULL)
	{
		*error = "ERR NUL bytes are not supported in keys";
		return false;
	}

	pg_verifymbstr(wire_key->data, wire_key->len, false);
	if (wire_key->len > 5 && memcmp(wire_key->data, "CRUD:", 5) == 0)
	{
		for (i = 0; i < worker_mapping_count; i++)
		{
			PgLocalCacheMapping *candidate = &worker_mappings[i];
			char	   *prefix = psprintf("CRUD:%s.%s.%s:", database_name,
									  candidate->schema_name,
									  candidate->relation_name);
			Size		prefix_length = strlen(prefix);

			if (wire_key->len <= prefix_length ||
				memcmp(wire_key->data, prefix, prefix_length) != 0)
				continue;
			key_length = wire_key->len - prefix_length;
			if (wire_key->data[prefix_length] != '{' ||
				wire_key->data[wire_key->len - 1] != '}')
			{
				*error = "ERR KVik key must end with a primary-key JSON object";
				return false;
			}
			*mapping = candidate;
			*raw_key = pnstrdup(wire_key->data + prefix_length, key_length);
			return true;
		}
		{
			char	   *database_prefix = psprintf("CRUD:%s.", database_name);
			Size		database_prefix_length = strlen(database_prefix);

			if (wire_key->len < database_prefix_length ||
				memcmp(wire_key->data, database_prefix,
					   database_prefix_length) != 0)
				*error = "ERR KVik key targets a different database";
			else
				*error = "ERR unknown KVik table mapping";
		}
		return false;
	}

	*error = "ERR key must use CRUD:database.schema.table:{primary-key-json}";
	return false;
}

static char *
jsonb_key_value_as_cstring(const JsonbValue *value, const char *column_name,
						   char **error)
{
	switch (value->type)
	{
		case jbvString:
			return pnstrdup(value->val.string.val, value->val.string.len);
		case jbvNumeric:
			return DatumGetCString(DirectFunctionCall1(
				numeric_out, NumericGetDatum(value->val.numeric)));
		case jbvBool:
			return pstrdup(value->val.boolean ? "true" : "false");
		case jbvNull:
			*error = psprintf("ERR primary-key field \"%s\" cannot be null",
							  column_name);
			return NULL;
		default:
			*error = psprintf(
				"ERR primary-key field \"%s\" must be a JSON scalar",
				column_name);
			return NULL;
	}
}

static bool
canonicalize_key(PgLocalCacheMapping *mapping, const char *raw_key,
				 Datum *key_values, char **canonical, char **error)
{
	Jsonb	   *key_object = NULL;
	bool		nulls[PGLC_MAX_KEY_COLUMNS] = {false};
	char	   *result;
	Size		result_length;
	int			i;

	key_object = DatumGetJsonbP(DirectFunctionCall1(
		jsonb_in, CStringGetDatum(raw_key)));
	if (!JB_ROOT_IS_OBJECT(key_object))
	{
		*error = "ERR primary key must be a JSON object";
		return false;
	}
	if (JB_ROOT_COUNT(key_object) != mapping->key_count)
	{
		*error = psprintf(
			"ERR primary-key JSON must contain exactly %d field%s",
			mapping->key_count, mapping->key_count == 1 ? "" : "s");
		return false;
	}

	for (i = 0; i < mapping->key_count; i++)
	{
		char	   *input;
		JsonbValue found;
		JsonbValue *value = getKeyJsonValueFromContainer(
			&key_object->root, mapping->key_columns[i],
			strlen(mapping->key_columns[i]), &found);

		if (value == NULL)
		{
			*error = psprintf("ERR missing primary-key field \"%s\"",
							  mapping->key_columns[i]);
			return false;
		}
		input = jsonb_key_value_as_cstring(
			value, mapping->key_columns[i], error);
		if (input == NULL)
			return false;

		key_values[i] = InputFunctionCall(&mapping->key_inputs[i], input,
										 mapping->key_ioparams[i],
										 mapping->key_typmods[i]);
	}
	result = palloc(PGLC_KEY_MAX);
	if (!pglc_canonical_key(key_values, nulls, mapping->key_count,
						 mapping->key_outputs, result, PGLC_KEY_MAX,
						 &result_length))
	{
		*error = "ERR canonical primary key is too long";
		return false;
	}
	*canonical = result;
	return true;
}

#ifdef PGLC_TEST_HOOKS
static bool
pglc_test_key_parse(PgLocalCacheMapping *mapping, const char *input,
					Size input_length, char *canonical, Size *canonical_length)
{
	MemoryContext old_context = CurrentMemoryContext;
	volatile bool accepted = false;
	char	   *raw_key;

	*canonical_length = 0;
	if (memchr(input, '\0', input_length) != NULL)
		return false;
	raw_key = pnstrdup(input, input_length);
	PG_TRY();
	{
		Datum		values[PGLC_MAX_KEY_COLUMNS];
		char	   *parsed_key = NULL;
		char	   *error = NULL;

		pg_verifymbstr(input, input_length, false);
		if (canonicalize_key(mapping, raw_key, values, &parsed_key, &error))
		{
			*canonical_length = strlen(parsed_key);
			memcpy(canonical, parsed_key, *canonical_length + 1);
			accepted = true;
		}
	}
	PG_CATCH();
	{
		MemoryContextSwitchTo(old_context);
		FlushErrorState();
	}
	PG_END_TRY();
	MemoryContextSwitchTo(old_context);
	return accepted;
}

Datum
pg_local_cache_test_key_scan_matches(PG_FUNCTION_ARGS)
{
	bytea	   *raw = PG_GETARG_BYTEA_PP(0);
	text	   *column_text = PG_GETARG_TEXT_PP(1);
	Oid			key_type = PG_GETARG_OID(2);
	int32		typmod = PG_GETARG_INT32(3);
	const char *input = VARDATA_ANY(raw);
	Size		input_length = VARSIZE_ANY_EXHDR(raw);
	char	   *column = text_to_cstring(column_text);
	Size		column_length = strlen(column);
	PgLocalCacheMapping mapping;
	Oid			input_function;
	Oid			output_function;
	bool		output_is_varlena;
	char		reference[PGLC_KEY_MAX];
	char		optimized[PGLC_KEY_MAX];
	Size		reference_length = 0;
	Size		optimized_length = 0;
	bool		reference_accepted;
	bool		optimized_accepted;

	if (column_length == 0 || column_length >= NAMEDATALEN)
		PG_RETURN_BOOL(false);
	memset(&mapping, 0, sizeof(mapping));
	mapping.key_count = 1;
	strlcpy(mapping.key_columns[0], column, sizeof(mapping.key_columns[0]));
	mapping.key_types[0] = key_type;
	mapping.key_typmods[0] = typmod;
	getTypeInputInfo(key_type, &input_function, &mapping.key_ioparams[0]);
	fmgr_info(input_function, &mapping.key_inputs[0]);
	getTypeOutputInfo(key_type, &output_function, &output_is_varlena);
	fmgr_info(output_function, &mapping.key_outputs[0]);

	reference_accepted = pglc_test_key_parse(&mapping, input, input_length,
											reference, &reference_length);
	optimized_accepted = pglc_key_scan_json_single(
		input, input_length, column, column_length, key_type, typmod,
		GetDatabaseEncoding() == PG_UTF8, optimized, sizeof(optimized),
		&optimized_length);
	if (!optimized_accepted)
		optimized_accepted = pglc_test_key_parse(&mapping, input, input_length,
												optimized, &optimized_length);
	PG_RETURN_BOOL(reference_accepted == optimized_accepted &&
		(!reference_accepted ||
		 (reference_length == optimized_length &&
		  memcmp(reference, optimized, reference_length + 1) == 0)));
}
#endif

static bool
row_json_validate(PgLocalCacheMapping *mapping, Jsonb *row,
				  Datum *key_values, char **error)
{
	JsonbIterator *iterator;
	JsonbIteratorToken token;
	JsonbValue value;
	int			i;

	if (!JB_ROOT_IS_OBJECT(row))
	{
		*error = "ERR whole-row SET value must be a JSON object";
		return false;
	}

	iterator = JsonbIteratorInit(&row->root);
	token = JsonbIteratorNext(&iterator, &value, true);
	Assert(token == WJB_BEGIN_OBJECT);
	while ((token = JsonbIteratorNext(&iterator, &value, true)) != WJB_DONE)
	{
		if (token == WJB_END_OBJECT)
			break;
		if (token == WJB_KEY)
		{
			char	   *column_name;

			if (value.val.string.len >= NAMEDATALEN)
			{
				*error = "ERR row JSON contains an unknown column";
				return false;
			}
			column_name = pnstrdup(value.val.string.val,
								 value.val.string.len);
			if (get_attnum(mapping->relation_oid, column_name) <= 0)
			{
				*error = psprintf("ERR row JSON contains unknown column \"%s\"",
								  column_name);
				return false;
			}
		}
	}

	for (i = 0; i < mapping->key_count; i++)
	{
		JsonbValue found;
		JsonbValue *json_value = getKeyJsonValueFromContainer(
			&row->root, mapping->key_columns[i],
			strlen(mapping->key_columns[i]), &found);
		char	   *input;
		Datum		row_key;
		char	   *expected;
		char	   *actual;

		/* KVik permits the payload to omit PK fields; the wire key supplies them. */
		if (json_value == NULL)
			continue;
		input = jsonb_key_value_as_cstring(
			json_value, mapping->key_columns[i], error);
		if (input == NULL)
			return false;
		row_key = InputFunctionCall(&mapping->key_inputs[i], input,
								mapping->key_ioparams[i],
								mapping->key_typmods[i]);
		expected = OutputFunctionCall(&mapping->key_outputs[i], key_values[i]);
		actual = OutputFunctionCall(&mapping->key_outputs[i], row_key);
		if (strcmp(expected, actual) != 0)
		{
			*error = psprintf(
				"ERR row primary-key field \"%s\" does not match the wire key",
				mapping->key_columns[i]);
			return false;
		}
	}
	return true;
}

static bool
cached_row_json(PgLocalCacheMapping *mapping,
				const char *payload, Size payload_length,
				char **json, Size *json_length)
{
	const char *cached_json;

	if (!pglc_row_payload_get_json_checked(payload, payload_length,
										mapping->row_desc->tdtypeid,
										mapping->row_desc->tdtypmod,
										mapping->row_desc->natts,
										mapping->row_descriptor_fingerprint,
										&cached_json, json_length))
		return false;
	*json = (char *) cached_json;
	return true;
}

static void
note_fast_path_fallback(PgLocalCacheFastPathFallbackReason reason)
{
	PgLocalCacheWorkerStats *stats =
		&pglc_shared->stats_shards[worker_slot];
	pg_atomic_uint64 *counter;

	switch (reason)
	{
		case PGLC_FAST_PATH_FALLBACK_KEY_FORM:
			counter = &stats->fast_path_fallback_key_form;
			break;
		case PGLC_FAST_PATH_FALLBACK_MAPPING_SHAPE:
			counter = &stats->fast_path_fallback_mapping_shape;
			break;
		case PGLC_FAST_PATH_FALLBACK_MULTI_KEY:
			counter = &stats->fast_path_fallback_multi_key;
			break;
		case PGLC_FAST_PATH_FALLBACK_CACHE_STATE:
		default:
			counter = &stats->fast_path_fallback_cache_state;
			break;
	}
	pg_atomic_fetch_add_u64(counter, 1);
}

static bool
fast_path_mapping_shape_supported(const PgLocalCacheMapping *mapping)
{
	Oid			key_type;

	if (mapping->key_count != 1 || mapping->key_typmods[0] != -1)
		return false;
	key_type = mapping->key_types[0];
	return key_type == PGLC_KEY_SCAN_INT2OID ||
		key_type == PGLC_KEY_SCAN_INT4OID ||
		key_type == PGLC_KEY_SCAN_INT8OID ||
		key_type == PGLC_KEY_SCAN_TEXTOID ||
		key_type == PGLC_KEY_SCAN_VARCHAROID;
}

static bool
try_fast_mget_hit(PgLocalCacheClient *client, PgLocalCacheRespArg *args,
				  int argc)
{
	const PgLocalCacheRespArg *wire_key;
	Size		database_length;
	Size		database_prefix_length;
	bool		database_prefix_matches;
	int			i;

#ifdef PGLC_TEST_HOOKS
	/* Pause-hook race tests need the general lookup path and its hook points. */
	if (pglc_test_pause_configured())
		return false;
#endif
	if (argc <= 0 || !pglc_resp_arg_equals(&args[0], "MGET"))
		return false;
	if (argc != 2)
	{
		note_fast_path_fallback(PGLC_FAST_PATH_FALLBACK_MULTI_KEY);
		return false;
	}
	if (!client->authenticated || !pglc_cache_is_enabled() ||
		pglc_database == NULL)
	{
		note_fast_path_fallback(PGLC_FAST_PATH_FALLBACK_CACHE_STATE);
		return false;
	}
	wire_key = &args[1];
	if (wire_key->len == 0 || wire_key->len >= PGLC_REQUEST_MAX ||
		memchr(wire_key->data, '\0', wire_key->len) != NULL)
	{
		note_fast_path_fallback(PGLC_FAST_PATH_FALLBACK_KEY_FORM);
		return false;
	}
	database_length = strlen(pglc_database);
	database_prefix_length = 5 + database_length + 1;
	database_prefix_matches = wire_key->len >= database_prefix_length &&
		memcmp(wire_key->data, "CRUD:", 5) == 0 &&
		memcmp(wire_key->data + 5, pglc_database, database_length) == 0 &&
		wire_key->data[5 + database_length] == '.';

	for (i = 0; i < worker_mapping_count; i++)
	{
		PgLocalCacheMapping *mapping = &worker_mappings[i];
		Size		schema_length = strlen(mapping->schema_name);
		Size		relation_length = strlen(mapping->relation_name);
		Size		prefix_length = 5 + database_length + 1 + schema_length +
			1 + relation_length + 1;
		Size		position = 0;
		const char *raw_json;
		Size		raw_json_length;
		char		prefix[PGLC_NAMESPACE_MAX + 3 * NAMEDATALEN + 8];
		Size		prefix_used = 0;
		char	   *json = NULL;
		Size		json_length = 0;
		Size		cached_length;
		bool		negative;
		TransactionId source_xmin;
		bool		hit;
		Size		response_length = 0;
		Size		available;

		if (wire_key->len < prefix_length)
			continue;
		memcpy(prefix + prefix_used, "CRUD:", 5);
		prefix_used += 5;
		memcpy(prefix + prefix_used, pglc_database, database_length);
		prefix_used += database_length;
		prefix[prefix_used++] = '.';
		memcpy(prefix + prefix_used, mapping->schema_name, schema_length);
		prefix_used += schema_length;
		prefix[prefix_used++] = '.';
		memcpy(prefix + prefix_used, mapping->relation_name, relation_length);
		prefix_used += relation_length;
		prefix[prefix_used++] = ':';
		Assert(prefix_used == prefix_length);
		if (memcmp(wire_key->data, prefix, prefix_length) != 0)
			continue;
		if (!fast_path_mapping_shape_supported(mapping))
		{
			note_fast_path_fallback(
				PGLC_FAST_PATH_FALLBACK_MAPPING_SHAPE);
			return false;
		}
		if (wire_key->len <= prefix_length)
		{
			note_fast_path_fallback(PGLC_FAST_PATH_FALLBACK_KEY_FORM);
			return false;
		}
		raw_json = wire_key->data + prefix_length;
		raw_json_length = wire_key->len - prefix_length;
		if (raw_json_length < 2 || raw_json[0] != '{' ||
			raw_json[raw_json_length - 1] != '}')
		{
			note_fast_path_fallback(PGLC_FAST_PATH_FALLBACK_KEY_FORM);
			return false;
		}
		if (!pglc_key_scan_json_single(raw_json, raw_json_length,
									   mapping->key_columns[0],
									   strlen(mapping->key_columns[0]),
									   mapping->key_types[0],
									   mapping->key_typmods[0],
									   GetDatabaseEncoding() == PG_UTF8,
									   worker_hit_scratch.canonical,
									   sizeof(worker_hit_scratch.canonical),
									   &position))
		{
			note_fast_path_fallback(PGLC_FAST_PATH_FALLBACK_KEY_FORM);
			return false;
		}

		worker_hit_scratch.mapping = mapping;
		hit = pglc_cache_lookup_quiet(mapping,
									 worker_hit_scratch.canonical,
									 worker_hit_scratch.value,
									 sizeof(worker_hit_scratch.value),
									 &cached_length, &negative,
									 &source_xmin,
									 &worker_hit_scratch.token);
		if (!hit)
		{
			note_fast_path_fallback(
				PGLC_FAST_PATH_FALLBACK_CACHE_STATE);
			return false;
		}
		if (!negative && !cached_row_json(mapping, worker_hit_scratch.value,
										 cached_length, &json, &json_length))
		{
			(void) pglc_cache_invalidate_key(mapping,
											 worker_hit_scratch.canonical);
			note_fast_path_fallback(
				PGLC_FAST_PATH_FALLBACK_CACHE_STATE);
			return false;
		}
		if (client->output_sent != 0 ||
			client->output_used > PGLC_OUTPUT_BUFFER_MAX)
		{
			note_fast_path_fallback(
				PGLC_FAST_PATH_FALLBACK_CACHE_STATE);
			return false;
		}
		available = PGLC_OUTPUT_BUFFER_MAX - client->output_used;
		if (!pglc_resp_write_array(client->output + client->output_used,
								   available, &response_length, 1,
								   PGLC_RESPONSE_MAX) ||
			(negative ? !pglc_resp_write_null(
				 client->output + client->output_used, available,
				 &response_length, PGLC_RESPONSE_MAX) :
			 !pglc_resp_write_bulk(client->output + client->output_used,
								available, &response_length, json,
								json_length, PGLC_RESPONSE_MAX)))
		{
			note_fast_path_fallback(
				PGLC_FAST_PATH_FALLBACK_CACHE_STATE);
			return false;
		}
		client->output_used += response_length;
		client->output_ready = true;
		pg_atomic_fetch_add_u64(
			&pglc_shared->stats_shards[worker_slot].fast_path_hits, 1);
		if (!client->retrying_deferred_miss)
			pg_atomic_fetch_add_u64(
				&pglc_shared->stats_shards[worker_slot].client_mget_keys, 1);
		note_resp_cache_lookup(true, negative);
		return true;
	}
	note_fast_path_fallback(database_prefix_matches ?
		PGLC_FAST_PATH_FALLBACK_MAPPING_SHAPE :
		PGLC_FAST_PATH_FALLBACK_KEY_FORM);
	return false;
}

/*
 * A source row may be wider than one fixed-size cache entry.  An MGET element
 * must still return it from PostgreSQL, so render it in a bounded temporary
 * context and simply skip cache admission.  Inspect every source attribute
 * before row_to_json: a composite can contain tiny external TOAST pointers
 * whose referenced values are much larger than the top-level record Datum.
 */
static bool
source_row_json(TupleTableSlot *slot, TupleDesc descriptor, Datum row,
				MemoryContext result_context,
				char **json, Size *json_length)
{
	MemoryContext old_context = CurrentMemoryContext;
	MemoryContext temporary_context;
	char	   *copy = NULL;
	Size		raw_attribute_bytes = 0;
	int		attribute_number;

	*json = NULL;
	*json_length = 0;
	if (slot == NULL || descriptor == NULL ||
		slot->tts_tupleDescriptor == NULL ||
		slot->tts_tupleDescriptor->natts != descriptor->natts)
		return false;
	slot_getallattrs(slot);
	for (attribute_number = 0; attribute_number < descriptor->natts;
		 attribute_number++)
	{
		Form_pg_attribute attribute;
		Size		attribute_size;

		if (slot->tts_isnull[attribute_number])
			continue;
		attribute = TupleDescAttr(descriptor, attribute_number);
		if (attribute->attlen > 0)
			attribute_size = attribute->attlen;
		else if (attribute->attlen == -1)
			attribute_size = toast_raw_datum_size(
				slot->tts_values[attribute_number]);
		else
			attribute_size = strlen(DatumGetCString(
				slot->tts_values[attribute_number])) + 1;
		if (attribute_size > PGLC_RESPONSE_VALUE_MAX ||
			raw_attribute_bytes > PGLC_RESPONSE_VALUE_MAX - attribute_size)
			return false;
		raw_attribute_bytes += attribute_size;
	}

	temporary_context = AllocSetContextCreate(old_context,
		"pg_local_cache source row json",
		ALLOCSET_SMALL_SIZES);
	PG_TRY();
	{
		Datum		json_datum;
		text	   *json_text;
		const char *rendered;
		Size		rendered_length;

		MemoryContextSwitchTo(temporary_context);
		json_datum = OidFunctionCall1(F_ROW_TO_JSON_RECORD, row);
		json_text = DatumGetTextPP(json_datum);
		rendered = VARDATA_ANY(json_text);
		rendered_length = VARSIZE_ANY_EXHDR(json_text);
		if (rendered_length <= PGLC_RESPONSE_VALUE_MAX)
		{
			MemoryContextSwitchTo(result_context);
			copy = palloc(rendered_length + 1);
			memcpy(copy, rendered, rendered_length);
			copy[rendered_length] = '\0';
			*json = copy;
			*json_length = rendered_length;
		}
		MemoryContextSwitchTo(old_context);
	}
	PG_CATCH();
	{
		MemoryContextSwitchTo(old_context);
		MemoryContextDelete(temporary_context);
		PG_RE_THROW();
	}
	PG_END_TRY();
	MemoryContextDelete(temporary_context);
	return copy != NULL;
}

static void
abort_spi_transaction(MemoryContext caller_context)
{
	if (IsTransactionState())
		AbortCurrentTransaction();
	MemoryContextSwitchTo(caller_context);
}

static MemoryContext
begin_spi_transaction(int statement_timeout_ms)
{
	MemoryContext caller_context = CurrentMemoryContext;
	char		timeout[32];

	Assert(statement_timeout_ms > 0);
	PG_TRY();
	{
		StartTransactionCommand();
		if (SPI_connect() != SPI_OK_CONNECT)
			elog(ERROR, "pg_local_cache could not connect to SPI");
		PushActiveSnapshot(GetTransactionSnapshot());

		snprintf(timeout, sizeof(timeout), "%d", statement_timeout_ms);
		(void) set_config_option("statement_timeout", timeout,
							 PGC_USERSET, PGC_S_SESSION,
							 GUC_ACTION_LOCAL, true, ERROR, false);
		snprintf(timeout, sizeof(timeout), "%d", pglc_lock_timeout_ms);
		(void) set_config_option("lock_timeout", timeout,
							 PGC_USERSET, PGC_S_SESSION,
							 GUC_ACTION_LOCAL, true, ERROR, false);
		enable_timeout_after(STATEMENT_TIMEOUT, statement_timeout_ms);
	}
	PG_CATCH();
	{
		abort_spi_transaction(caller_context);
		PG_RE_THROW();
	}
	PG_END_TRY();
	return caller_context;
}

/*
 * Begin a source-read transaction only after all source relations have been
 * conditionally locked.  A failed conditional lock aborts the empty
 * transaction, which releases only the AccessShareLocks acquired by this
 * request, without entering SPI or waiting on the relation lock.
 */
static MemoryContext
begin_mget_spi_transaction(int statement_timeout_ms,
							 PgLocalCacheMgetItem *items, int item_count,
							 PgLocalCacheMapping *single_mapping,
							 bool *relation_locked)
{
	MemoryContext caller_context = CurrentMemoryContext;
	Oid			relation_oids[PGLC_MGET_MAX_KEYS];
	int			relation_count = 0;
	int			item_index;
	char		timeout[32];

	Assert(statement_timeout_ms > 0);
	*relation_locked = false;
	PG_TRY();
	{
		StartTransactionCommand();
		if (single_mapping != NULL)
		{
			relation_oids[relation_count++] = single_mapping->relation_oid;
		}
		else
		{
			for (item_index = 0; item_index < item_count; item_index++)
			{
				PgLocalCacheMgetItem *item = &items[item_index];
				int			prior;
				bool		already_locked = false;

				if (item->result_ready || item->deferred)
					continue;
				for (prior = 0; prior < relation_count; prior++)
				{
					if (relation_oids[prior] == item->mapping->relation_oid)
					{
						already_locked = true;
						break;
					}
				}
				if (!already_locked)
					relation_oids[relation_count++] =
						item->mapping->relation_oid;
			}
		}

		for (item_index = 0; item_index < relation_count; item_index++)
		{
			if (!ConditionalLockRelationOid(relation_oids[item_index],
											AccessShareLock))
			{
				abort_spi_transaction(caller_context);
				*relation_locked = true;
				break;
			}
		}
		if (!*relation_locked)
		{
			if (SPI_connect() != SPI_OK_CONNECT)
				elog(ERROR, "pg_local_cache could not connect to SPI");
			PushActiveSnapshot(GetTransactionSnapshot());

			snprintf(timeout, sizeof(timeout), "%d", statement_timeout_ms);
			(void) set_config_option("statement_timeout", timeout,
								 PGC_USERSET, PGC_S_SESSION,
								 GUC_ACTION_LOCAL, true, ERROR, false);
			snprintf(timeout, sizeof(timeout), "%d", pglc_lock_timeout_ms);
			(void) set_config_option("lock_timeout", timeout,
								 PGC_USERSET, PGC_S_SESSION,
								 GUC_ACTION_LOCAL, true, ERROR, false);
			enable_timeout_after(STATEMENT_TIMEOUT, statement_timeout_ms);
		}
	}
	PG_CATCH();
	{
		abort_spi_transaction(caller_context);
		PG_RE_THROW();
	}
	PG_END_TRY();
	if (*relation_locked)
		return NULL;
	return caller_context;
}

static void
ensure_mapping_current(const PgLocalCacheMapping *mapping)
{
	if (mapping->config_generation != pglc_config_generation())
		ereport(ERROR,
				(errcode(ERRCODE_T_R_SERIALIZATION_FAILURE),
				 errmsg("pg_local_cache mapping changed while the command was running"),
				 errhint("Retry the command.")));
}

static void
commit_spi_transaction(MemoryContext caller_context)
{
	PG_TRY();
	{
		PopActiveSnapshot();
		if (SPI_finish() != SPI_OK_FINISH)
			elog(ERROR, "pg_local_cache could not finish SPI");
		if (get_timeout_active(STATEMENT_TIMEOUT))
			disable_timeout(STATEMENT_TIMEOUT, false);
		(void) get_timeout_indicator(STATEMENT_TIMEOUT, true);
		CommitTransactionCommand();
	}
	PG_CATCH();
	{
		abort_spi_transaction(caller_context);
		PG_RE_THROW();
	}
	PG_END_TRY();
	MemoryContextSwitchTo(caller_context);
}

static void
note_resp_cache_lookup(bool hit, bool negative)
{
	if (hit)
	{
		pg_atomic_fetch_add_u64(
			&pglc_shared->stats_shards[worker_slot].cache_hits, 1);
		if (negative)
			pg_atomic_fetch_add_u64(
				&pglc_shared->stats_shards[worker_slot].negative_hits, 1);
	}
	else
		pg_atomic_fetch_add_u64(
			&pglc_shared->stats_shards[worker_slot].cache_misses, 1);
}

static bool
mget_quiet_lookup(PgLocalCacheMgetItem *item, bool count_lookup)
{
	char		cached_value[PGLC_VALUE_MAX];
	Size		cached_length;
	bool		negative;
	TransactionId source_xmin;
	bool		hit;

	hit = pglc_cache_lookup_quiet(item->mapping, item->canonical,
								 cached_value, sizeof(cached_value),
								 &cached_length, &negative, &source_xmin,
								 &item->token);
	if (hit)
	{
		if (negative)
		{
			if (count_lookup)
				note_resp_cache_lookup(true, true);
			else
				pglc_note_singleflight_reuse();
			item->null_result = true;
			item->result_ready = true;
			item->response_element = pglc_resp_null(
				&item->response_element_length);
			return true;
		}
		if (cached_row_json(item->mapping, cached_value, cached_length,
							&item->json,
							&item->json_length))
		{
			if (count_lookup)
				note_resp_cache_lookup(true, false);
			else
				pglc_note_singleflight_reuse();
			item->result_ready = true;
			item->response_element = pglc_resp_bulk(item->json,
				item->json_length, &item->response_element_length);
			return true;
		}

		/* Corrupt or descriptor-stale payloads are never exposed. */
		(void) pglc_cache_invalidate_key(item->mapping, item->canonical);
		(void) pglc_cache_lookup_quiet(item->mapping, item->canonical,
									cached_value, sizeof(cached_value),
									&cached_length, &negative, &source_xmin,
									&item->token);
	}
	if (count_lookup)
		note_resp_cache_lookup(false, false);
	return false;
}

static void
mget_release_claims(PgLocalCacheMgetItem *items, int item_count)
{
	int			i;

	for (i = 0; i < item_count; i++)
	{
		if (items[i].owns_load)
		{
			pglc_cache_release_load(items[i].mapping, items[i].canonical,
								&items[i].token, items[i].load_id);
			items[i].owns_load = false;
		}
	}
}

static Size
mget_decimal_digits(Size value)
{
	Size		digits = 1;

	while (value >= 10)
	{
		value /= 10;
		digits++;
	}
	return digits;
}

static bool
mget_account_result(PgLocalCacheMgetItem *item, Size *response_size)
{
	Size		element_length;

	if (item->response_element != NULL)
		element_length = item->response_element_length;
	else if (item->null_result)
		element_length = 5; /* $-1\r\n */
	else
		element_length = 1 + mget_decimal_digits(item->json_length) + 2 +
			item->json_length + 2;

	if (element_length > PGLC_RESPONSE_MAX ||
		item->response_slots >
		(PGLC_RESPONSE_MAX - *response_size) / element_length)
		return false;
	*response_size += element_length * item->response_slots;
	return true;
}

static void
mget_read_one(PgLocalCacheMgetItem *item, MemoryContext result_context)
{
	char		cached_value[PGLC_VALUE_MAX];
	Size		database_payload_length = 0;
	bool		database_payload_cacheable = false;

	ensure_mapping_current(item->mapping);
	pg_atomic_fetch_add_u64(
		&pglc_shared->stats_shards[worker_slot].pass_to_main, 1);
	if (SPI_execute_plan(item->mapping->get_plan, item->key_values,
						 NULL, true, 1) != SPI_OK_SELECT)
		elog(ERROR, "pg_local_cache MGET plan failed");
	ensure_mapping_current(item->mapping);
	item->database_read = true;
	if (SPI_processed != 1)
	{
		item->null_result = true;
		item->result_ready = true;
		return;
	}
	{
		bool		xmin_is_null;
		Datum		xmin_value;
		Datum		row_value;
		bool		row_is_null;
		TupleTableSlot *row_slot;
		char	   *rendered_json;
		Size		rendered_json_length;

		row_value = SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc,
								 1, &row_is_null);
		if (row_is_null)
			elog(ERROR, "pg_local_cache whole row unexpectedly became NULL");
		row_slot = MakeSingleTupleTableSlot(item->mapping->row_desc,
											&TTSOpsVirtual);
		ExecStoreHeapTupleDatum(row_value, row_slot);
		if (!source_row_json(row_slot, item->mapping->row_desc,
							 row_value, result_context,
							 &rendered_json, &rendered_json_length))
			ereport(ERROR,
					(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
					 errmsg("row JSON exceeds the RESP limit of %d bytes",
							PGLC_RESPONSE_VALUE_MAX)));
		database_payload_cacheable = pglc_row_payload_encode(
			item->mapping->row_desc->tdtypeid,
			item->mapping->row_desc->tdtypmod,
			(uint32) item->mapping->row_desc->natts,
			item->mapping->row_descriptor_fingerprint,
			rendered_json, rendered_json_length,
			cached_value, sizeof(cached_value), &database_payload_length);
		ExecDropSingleTupleTableSlot(row_slot);
		if (database_payload_cacheable)
		{
			item->payload = MemoryContextAlloc(result_context,
										 database_payload_length);
			memcpy(item->payload, cached_value, database_payload_length);
			item->payload_length = database_payload_length;
			item->payload_cacheable = true;
		}
		item->json = MemoryContextAlloc(result_context,
										 rendered_json_length + 1);
		memcpy(item->json, rendered_json, rendered_json_length);
		item->json[rendered_json_length] = '\0';
		item->json_length = rendered_json_length;
		xmin_value = SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc,
								  2, &xmin_is_null);
		if (xmin_is_null)
			elog(ERROR, "pg_local_cache row xmin unexpectedly became NULL");
		item->database_xmin = (TransactionId) DatumGetUInt32(xmin_value);
		item->result_ready = true;
	}
}

static char *
command_mget_one(PgLocalCacheMapping *mapping, const char *canonical,
					Datum *key_values, TimestampTz deadline,
					bool waiter_already_counted,
					bool *relation_locked,
					Size *response_length)
{
	char		cached_value[PGLC_VALUE_MAX];
	Size		cached_length;
	bool		negative;
	TransactionId source_xmin;
	PgLocalCacheReadToken token;
	bool		cache_enabled;
	bool		hit;
	bool		owns_load = false;
	bool		waiter_counted = waiter_already_counted;
	uint64		load_id = 0;
	TimestampTz wait_started;
	int			retry_count = 0;
	char	   *database_value = NULL;
	Size		database_value_length = 0;
	Size		database_payload_length = 0;
	bool		database_payload_cacheable = false;
	TransactionId database_xmin = InvalidTransactionId;
	MemoryContext transaction_context;
	MemoryContext result_context = CurrentMemoryContext;
	int			statement_timeout_ms = pglc_statement_timeout_ms;

	if (deadline != 0 && GetCurrentTimestamp() >= deadline)
		return pglc_resp_error("ERR MGET deadline exceeded", response_length);

	cache_enabled = pglc_cache_is_enabled();
	if (cache_enabled)
	{
		hit = pglc_cache_lookup_quiet(mapping, canonical,
									 cached_value, sizeof(cached_value),
									 &cached_length, &negative, &source_xmin,
									 &token);
		if (hit)
		{
			if (negative)
			{
				note_resp_cache_lookup(true, true);
				return pglc_resp_null(response_length);
			}
			{
				char	   *json;
				Size		json_length;

				if (cached_row_json(mapping, cached_value, cached_length,
									&json, &json_length))
				{
					note_resp_cache_lookup(true, false);
					return pglc_resp_bulk(json, json_length, response_length);
				}

				/* Corrupt or descriptor-stale payloads are never exposed. */
				(void) pglc_cache_invalidate_key(mapping, canonical);
				(void) pglc_cache_lookup_quiet(mapping, canonical,
										  cached_value, sizeof(cached_value),
										  &cached_length, &negative, &source_xmin,
										  &token);
			}
		}
		note_resp_cache_lookup(false, false);

		wait_started = GetCurrentTimestamp();
		for (;;)
		{
			PgLocalCacheLoadClaim claim;

			if (deadline != 0 && GetCurrentTimestamp() >= deadline)
				return pglc_resp_error("ERR MGET deadline exceeded", response_length);
			claim = pglc_cache_claim_load(mapping, canonical, &token, &load_id);

			if (claim == PGLC_LOAD_OWNER)
			{
				owns_load = true;
				break;
			}
			if (claim == PGLC_LOAD_BYPASS)
				break;
			if (claim == PGLC_LOAD_WAIT && !waiter_counted)
			{
				pglc_note_singleflight_waiter();
				waiter_counted = true;
			}
			if (claim == PGLC_LOAD_RETRY &&
				++retry_count >= PGLC_MAX_LOAD_RETRIES)
				break;

			hit = pglc_cache_lookup_quiet(mapping, canonical,
										 cached_value, sizeof(cached_value),
										 &cached_length, &negative, &source_xmin,
										 &token);
			if (hit)
			{
				if (negative)
				{
					pglc_note_singleflight_reuse();
					return pglc_resp_null(response_length);
				}
				{
					char	   *json;
					Size		json_length;

					if (cached_row_json(mapping, cached_value, cached_length,
										&json, &json_length))
					{
						pglc_note_singleflight_reuse();
						return pglc_resp_bulk(json, json_length,
										  response_length);
					}
					(void) pglc_cache_invalidate_key(mapping, canonical);
					(void) pglc_cache_lookup_quiet(mapping, canonical,
										  cached_value, sizeof(cached_value),
										  &cached_length, &negative,
										  &source_xmin, &token);
					if (deadline != 0 && GetCurrentTimestamp() >= deadline)
						break;
					if (TimestampDifferenceExceeds(wait_started,
											   GetCurrentTimestamp(),
											   pglc_singleflight_wait_ms))
					{
						if (claim == PGLC_LOAD_WAIT)
							pglc_note_singleflight_timeout();
						break;
					}
					continue;
				}
			}
			if (TimestampDifferenceExceeds(wait_started, GetCurrentTimestamp(),
									   pglc_singleflight_wait_ms))
			{
				if (claim == PGLC_LOAD_WAIT)
				{
					pglc_note_singleflight_timeout();
				}
				break;
			}
			if (claim == PGLC_LOAD_RETRY)
				continue;
			(void) WaitLatch(MyLatch,
							 WL_LATCH_SET | WL_TIMEOUT | WL_EXIT_ON_PM_DEATH,
							 1L, PG_WAIT_EXTENSION);
			ResetLatch(MyLatch);
			CHECK_FOR_INTERRUPTS();
		}
	}
	if (deadline != 0)
	{
		long		remaining_ms = TimestampDifferenceMilliseconds(
			GetCurrentTimestamp(), deadline);

		if (remaining_ms <= 0)
		{
			if (owns_load)
				pglc_cache_release_load(mapping, canonical, &token, load_id);
			return pglc_resp_error("ERR MGET deadline exceeded", response_length);
		}
		statement_timeout_ms = (int) Min((long) pglc_statement_timeout_ms,
										 remaining_ms);
	}

	*relation_locked = false;
	PG_TRY();
	{
		transaction_context = begin_mget_spi_transaction(statement_timeout_ms,
												 NULL, 0, mapping,
												 relation_locked);
		if (transaction_context == NULL)
		{
			if (owns_load)
			{
				pglc_cache_release_load(mapping, canonical, &token, load_id);
				owns_load = false;
			}
		}
		else
		{
		ensure_mapping_current(mapping);
		pg_atomic_fetch_add_u64(
			&pglc_shared->stats_shards[worker_slot].pass_to_main, 1);
		if (SPI_execute_plan(mapping->get_plan, key_values, NULL, true, 1) !=
			SPI_OK_SELECT)
			elog(ERROR, "pg_local_cache MGET plan failed");
		ensure_mapping_current(mapping);
		if (SPI_processed == 1)
		{
			bool		xmin_is_null;
			Datum		xmin_value;
			int			xmin_column = 2;

			{
				bool		row_is_null;
				Datum		row_value = SPI_getbinval(SPI_tuptable->vals[0],
					SPI_tuptable->tupdesc, 1, &row_is_null);
				TupleTableSlot *row_slot;
				char	   *rendered_json;
				Size		rendered_json_length;

				if (row_is_null)
					elog(ERROR, "pg_local_cache whole row unexpectedly became NULL");
				row_slot = MakeSingleTupleTableSlot(mapping->row_desc,
												&TTSOpsVirtual);
				ExecStoreHeapTupleDatum(row_value, row_slot);
				if (!source_row_json(row_slot, mapping->row_desc,
								  row_value, result_context,
								  &rendered_json, &rendered_json_length))
					ereport(ERROR,
							(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
							 errmsg("row JSON exceeds the RESP limit of %d bytes",
									PGLC_RESPONSE_VALUE_MAX)));
				database_payload_cacheable = pglc_row_payload_encode(
					mapping->row_desc->tdtypeid,
					mapping->row_desc->tdtypmod,
					(uint32) mapping->row_desc->natts,
					mapping->row_descriptor_fingerprint,
					rendered_json, rendered_json_length,
					cached_value, sizeof(cached_value),
					&database_payload_length);
				ExecDropSingleTupleTableSlot(row_slot);
				database_value_length = rendered_json_length;
				database_value = MemoryContextAlloc(
					result_context, database_value_length + 1);
				memcpy(database_value, rendered_json, database_value_length);
				database_value[database_value_length] = '\0';
			}
			xmin_value = SPI_getbinval(SPI_tuptable->vals[0],
									   SPI_tuptable->tupdesc, xmin_column,
									   &xmin_is_null);
			if (xmin_is_null)
				elog(ERROR, "pg_local_cache row xmin unexpectedly became NULL");
			database_xmin = (TransactionId) DatumGetUInt32(xmin_value);
		}
		commit_spi_transaction(transaction_context);
		pglc_note_database_read();

		/* Only a load owner can publish this read. */
		if (cache_enabled && owns_load)
		{
			if (database_value == NULL)
				pglc_cache_store(mapping, canonical, &token, NULL, 0, true,
								 load_id,
								 InvalidTransactionId);
			else if (database_payload_cacheable)
				pglc_cache_store(mapping, canonical, &token,
								 cached_value, database_payload_length, false,
								 load_id,
								 database_xmin);
		}
		if (owns_load)
		{
			pglc_cache_release_load(mapping, canonical, &token, load_id);
			owns_load = false;
		}
		}
	}
	PG_CATCH();
	{
		if (owns_load)
			pglc_cache_release_load(mapping, canonical, &token, load_id);
		PG_RE_THROW();
	}
	PG_END_TRY();
	if (*relation_locked)
		return NULL;

	if (database_value == NULL)
		return pglc_resp_null(response_length);

	return pglc_resp_bulk(database_value, database_value_length,
						 response_length);
}

static char *
command_mget(PgLocalCacheClient *client, PgLocalCacheRespArg *args, int argc,
			Size request_length, Size *response_length)
{
	int			key_count = argc - 1;
	TimestampTz deadline = client->retrying_deferred_miss ?
		client->deferred_deadline : TimestampTzPlusMilliseconds(
			GetCurrentTimestamp(), pglc_statement_timeout_ms);
	PgLocalCacheMgetItem *items;
	int		   *slot_items;
	int			item_count = 0;
	int			key_index;
	Size		response_size;
	volatile bool failed = false;
	volatile bool relation_blocked = false;
	volatile int failure_code = 0; /* 1 = deadline, 2 = response limit */
	char	   * volatile command_error = NULL;
	volatile Size command_error_length = 0;
	MemoryContext volatile transaction_context = NULL;
	MemoryContext result_context = CurrentMemoryContext;
	StringInfoData response;

	items = palloc0(mul_size(sizeof(*items), (Size) key_count));
	slot_items = palloc(mul_size(sizeof(*slot_items), (Size) key_count));
	for (key_index = 0; key_index < key_count; key_index++)
	{
		PgLocalCacheMapping *mapping;
		char	   *raw_key;
		char	   *canonical;
		char	   *key_error = NULL;
		Datum		key_values[PGLC_MAX_KEY_COLUMNS];
		int			item_index;

		if (!resolve_wire_key(&args[key_index + 1], &mapping,
							  &raw_key, &key_error) ||
			!canonicalize_key(mapping, raw_key, key_values, &canonical,
							  &key_error))
			return pglc_resp_error(key_error, response_length);

		for (item_index = 0; item_index < item_count; item_index++)
		{
			if (items[item_index].mapping->relation_oid == mapping->relation_oid &&
				strcmp(items[item_index].mapping->nspace, mapping->nspace) == 0 &&
				strcmp(items[item_index].canonical, canonical) == 0)
				break;
		}
		if (item_index == item_count)
		{
			items[item_index].mapping = mapping;
			items[item_index].canonical = canonical;
			memcpy(items[item_index].key_values, key_values,
				   sizeof(items[item_index].key_values));
			items[item_index].cache_enabled = pglc_cache_is_enabled();
			item_count++;
		}
		slot_items[key_index] = item_index;
		items[item_index].response_slots++;
	}

	if (!client->retrying_deferred_miss)
		pg_atomic_fetch_add_u64(
			&pglc_shared->stats_shards[worker_slot].client_mget_keys,
			(uint64) key_count);
	response_size = 1 + mget_decimal_digits((Size) key_count) + 2;
	for (key_index = 0; key_index < item_count; key_index++)
	{
		PgLocalCacheMgetItem *item = &items[key_index];

		if (item->cache_enabled && mget_quiet_lookup(item, true) &&
			!mget_account_result(item, &response_size))
		{
			mget_release_claims(items, item_count);
			return pglc_resp_error("ERR response exceeds limit", response_length);
		}
	}

	PG_TRY();
	{
		int			item_index;
		bool		have_database_reads = false;

		/* Claim every miss first. WAIT entries are deferred; never wait here. */
		for (item_index = 0; item_index < item_count && !failed; item_index++)
		{
			PgLocalCacheMgetItem *item = &items[item_index];
			TimestampTz claim_started = GetCurrentTimestamp();
			int			retry_count = 0;

			if (item->result_ready || !item->cache_enabled)
				continue;
			for (;;)
			{
				PgLocalCacheLoadClaim claim;

				if (GetCurrentTimestamp() >= deadline)
				{
					failed = true;
					failure_code = 1;
					break;
				}
				if (retry_count > 0 &&
					TimestampDifferenceExceeds(claim_started,
											   GetCurrentTimestamp(),
											   pglc_singleflight_wait_ms))
					break;
				claim = pglc_cache_claim_load(item->mapping, item->canonical,
												&item->token, &item->load_id);
				if (claim == PGLC_LOAD_OWNER)
				{
					item->owns_load = true;
					break;
				}
				if (claim == PGLC_LOAD_BYPASS)
					break;
				if (claim == PGLC_LOAD_WAIT)
				{
					item->deferred = true;
					pglc_note_singleflight_waiter();
					break;
				}
				if (++retry_count >= PGLC_MAX_LOAD_RETRIES)
					break;

				/* RETRY only: refresh quietly, then retry the non-blocking claim. */
				if (mget_quiet_lookup(item, false))
				{
					if (!mget_account_result(item, &response_size))
					{
						failed = true;
						failure_code = 2;
					}
					break;
				}
			}
		}

		for (item_index = 0; item_index < item_count; item_index++)
			if (!items[item_index].result_ready && !items[item_index].deferred)
				have_database_reads = true;

		if (!failed && have_database_reads)
		{
			long		remaining_ms = TimestampDifferenceMilliseconds(
				GetCurrentTimestamp(), deadline);

			if (remaining_ms <= 0)
			{
				failed = true;
				failure_code = 1;
			}
			else
			{
				int			statement_timeout_ms = (int) Min(
					(long) pglc_statement_timeout_ms, remaining_ms);
				bool		lock_failed = false;

				transaction_context = begin_mget_spi_transaction(
					statement_timeout_ms, items, item_count, NULL,
					&lock_failed);
				relation_blocked = lock_failed;
				if (transaction_context == NULL)
					relation_blocked = true;
				else
				{
					for (item_index = 0; item_index < item_count; item_index++)
					{
						PgLocalCacheMgetItem *item = &items[item_index];

						if (item->result_ready || item->deferred)
							continue;
						if (GetCurrentTimestamp() >= deadline)
						{
							failed = true;
							failure_code = 1;
							break;
						}
						mget_read_one(item, result_context);
						if (!mget_account_result(item, &response_size))
						{
							failed = true;
							failure_code = 2;
							break;
						}
					}
					{
						MemoryContext commit_context =
							(MemoryContext) transaction_context;

						/* commit_spi_transaction aborts internally before rethrowing. */
						transaction_context = NULL;
						commit_spi_transaction(commit_context);
					}
					for (item_index = 0; item_index < item_count; item_index++)
					{
						PgLocalCacheMgetItem *item = &items[item_index];

						if (!item->database_read)
							continue;
						pglc_note_database_read();
						if (!failed)
						{
							if (item->null_result)
								item->response_element = pglc_resp_null(
										&item->response_element_length);
							else
								item->response_element = pglc_resp_bulk(
										item->json, item->json_length,
										&item->response_element_length);
						}
					}
					if (!failed && GetCurrentTimestamp() >= deadline)
					{
						failed = true;
						failure_code = 1;
					}
				}
			}
		}

		if (!failed)
		{
			for (item_index = 0; item_index < item_count; item_index++)
			{
				PgLocalCacheMgetItem *item = &items[item_index];
				bool		stored = false;

				if (!item->owns_load)
					continue;
				if (item->null_result)
					stored = pglc_cache_store(item->mapping,
						item->canonical, &item->token, NULL, 0, true,
						item->load_id, InvalidTransactionId);
				else if (item->payload_cacheable)
					stored = pglc_cache_store(item->mapping,
						item->canonical, &item->token, item->payload,
						item->payload_length, false, item->load_id,
						item->database_xmin);
				if (!stored)
					pglc_cache_release_load(item->mapping, item->canonical,
										&item->token, item->load_id);
				item->owns_load = false;
			}
		}

		/* WAIT handling starts only after every owner claim is stored/released. */
		for (item_index = 0; item_index < item_count && !failed &&
			 !relation_blocked; item_index++)
		{
			PgLocalCacheMgetItem *item = &items[item_index];
			bool		single_relation_locked = false;

			if (!item->deferred)
				continue;
			if (GetCurrentTimestamp() >= deadline)
			{
				failed = true;
				failure_code = 1;
				break;
			}
			item->response_element = command_mget_one(
				item->mapping, item->canonical, item->key_values,
				deadline, true, &single_relation_locked,
				&item->response_element_length);
			if (single_relation_locked)
			{
				relation_blocked = true;
				break;
			}
			if (item->response_element_length > 0 &&
				item->response_element[0] == '-')
			{
				command_error = item->response_element;
				command_error_length = item->response_element_length;
				failed = true;
				break;
			}
			item->result_ready = true;
			if (!mget_account_result(item, &response_size))
			{
				failed = true;
				failure_code = 2;
			}
		}
	}
	PG_CATCH();
	{
		if (transaction_context != NULL)
			abort_spi_transaction((MemoryContext) transaction_context);
		mget_release_claims(items, item_count);
		PG_RE_THROW();
	}
	PG_END_TRY();
	if (relation_blocked)
	{
		PgLocalCacheDeferredResult deferred_result;

		mget_release_claims(items, item_count);
		deferred_result = enqueue_deferred_miss(client, request_length,
											deadline);
		if (deferred_result == PGLC_DEFERRED_QUEUED)
		{
			*response_length = 0;
			return NULL;
		}
		if (deferred_result == PGLC_DEFERRED_EXPIRED)
			return pglc_resp_error("ERR MGET deadline exceeded",
								  response_length);
		return pglc_resp_error("ERR busy: relation locked, retry",
							  response_length);
	}

	if (failed)
	{
		mget_release_claims(items, item_count);
		if (command_error != NULL)
		{
			*response_length = command_error_length;
			return command_error;
		}
		return pglc_resp_error(failure_code == 2 ?
							   "ERR response exceeds limit" :
							   "ERR MGET deadline exceeded",
							   response_length);
	}
	if (GetCurrentTimestamp() >= deadline)
	{
		mget_release_claims(items, item_count);
		return pglc_resp_error("ERR MGET deadline exceeded", response_length);
	}

	initStringInfo(&response);
	appendStringInfo(&response, "*%d\r\n", key_count);
	for (key_index = 0; key_index < key_count; key_index++)
	{
		PgLocalCacheMgetItem *item = &items[slot_items[key_index]];

		if (!item->result_ready || item->response_element == NULL)
			elog(ERROR, "pg_local_cache MGET result was not prepared");
		if (item->response_element_length > PGLC_RESPONSE_MAX ||
			(Size) response.len > PGLC_RESPONSE_MAX -
			item->response_element_length)
		{
			mget_release_claims(items, item_count);
			return pglc_resp_error("ERR response exceeds limit",
							  response_length);
		}
		appendBinaryStringInfo(&response, item->response_element,
							   (int) item->response_element_length);
	}
	*response_length = (Size) response.len;
	return response.data;
}

static char *
command_set(PgLocalCacheMapping *mapping, const char *raw_key,
			const PgLocalCacheRespArg *value_arg,
			Size *response_length)
{
	Datum		key_values[PGLC_MAX_KEY_COLUMNS];
	Datum		values[PGLC_MAX_KEY_COLUMNS + 1];
	Jsonb	   *row = NULL;
	char	   *canonical;
	char	   *key_error = NULL;
	char	   *value_text;
	MemoryContext transaction_context;
	int			i;

	if (!mapping->writable)
		return pglc_resp_error("ERR namespace is read-only", response_length);
	if (value_arg->len >= PGLC_REQUEST_MAX ||
		memchr(value_arg->data, '\0', value_arg->len) != NULL)
		return pglc_resp_error("ERR value is too large or contains NUL",
							  response_length);
	pg_verifymbstr(value_arg->data, value_arg->len, false);

	if (!canonicalize_key(mapping, raw_key, key_values,
						  &canonical, &key_error))
		return pglc_resp_error(key_error, response_length);
	value_text = pnstrdup(value_arg->data, value_arg->len);
	for (i = 0; i < mapping->key_count; i++)
		values[i] = key_values[i];
	row = DatumGetJsonbP(DirectFunctionCall1(jsonb_in,
										 CStringGetDatum(value_text)));
	transaction_context = begin_spi_transaction(pglc_statement_timeout_ms);
	ensure_mapping_current(mapping);
	if (!row_json_validate(mapping, row, key_values, &key_error))
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg_internal("%s",
							 key_error != NULL && strncmp(key_error, "ERR ", 4) == 0 ?
							 key_error + 4 : key_error)));
	values[mapping->key_count] = JsonbPGetDatum(row);

	pg_atomic_fetch_add_u64(
		&pglc_shared->stats_shards[worker_slot].pass_to_main, 1);
	pg_atomic_fetch_add_u64(&pglc_shared->stats_shards[worker_slot].sql_sets, 1);
	if (SPI_execute_plan(mapping->set_plan, values, NULL, false, 0) !=
		SPI_OK_INSERT)
		elog(ERROR, "pg_local_cache SET plan failed");
	ensure_mapping_current(mapping);
	commit_spi_transaction(transaction_context);
	pglc_note_database_write();
	return pglc_resp_simple("OK", response_length);
}

static char *
command_delete(PgLocalCacheMapping *mapping, const char *raw_key,
			   Size *response_length)
{
	Datum		values[PGLC_MAX_KEY_COLUMNS];
	char	   *canonical;
	char	   *key_error = NULL;
	MemoryContext transaction_context;
	uint64		deleted;

	if (!mapping->writable)
		return pglc_resp_error("ERR namespace is read-only", response_length);
	if (!canonicalize_key(mapping, raw_key, values,
						  &canonical, &key_error))
		return pglc_resp_error(key_error, response_length);
	(void) canonical;

	transaction_context = begin_spi_transaction(pglc_statement_timeout_ms);
	ensure_mapping_current(mapping);
	pg_atomic_fetch_add_u64(
		&pglc_shared->stats_shards[worker_slot].pass_to_main, 1);
	pg_atomic_fetch_add_u64(&pglc_shared->stats_shards[worker_slot].sql_dels, 1);
	if (SPI_execute_plan(mapping->delete_plan, values, NULL, false, 0) !=
		SPI_OK_DELETE)
		elog(ERROR, "pg_local_cache DEL plan failed");
	ensure_mapping_current(mapping);
	deleted = SPI_processed;
	commit_spi_transaction(transaction_context);
	pglc_note_database_write();
	return pglc_resp_integer((int64) deleted, response_length);
}

static void
maybe_reload_mappings(void)
{
	uint64		generation = pglc_config_generation();
	int			mapping_index;

	if (generation == worker_mapping_generation && !worker_mappings_incomplete)
	{
		/* Keep serviceable mappings uncached while relation-slot pressure clears. */
		for (mapping_index = 0; mapping_index < worker_mapping_count;
			 mapping_index++)
			if (!pglc_mapping_slot_is_current(&worker_mappings[mapping_index]))
				(void) pglc_resolve_mapping_slot(
					&worker_mappings[mapping_index]);
		return;
	}
	if (worker_next_mapping_retry != 0 &&
		generation == worker_retry_generation &&
		GetCurrentTimestamp() < worker_next_mapping_retry)
		return;
	(void) reload_mappings(generation);
}

static void
set_worker_mapping_generation(uint64 generation)
{
	if (pglc_shared == NULL || worker_slot < 0 ||
		worker_slot >= PGLC_MAX_WORKERS)
		return;
	pg_atomic_write_u64(
		&pglc_shared->worker_mapping_generations[worker_slot], generation);
}

static void
set_worker_mappings_incomplete(bool incomplete)
{
	worker_mappings_incomplete = incomplete;
}

static void
free_mapping_plans(void)
{
	int			i;

	for (i = 0; i < worker_mapping_count; i++)
	{
		if (worker_mappings[i].get_plan)
			SPI_freeplan(worker_mappings[i].get_plan);
		if (worker_mappings[i].set_plan)
			SPI_freeplan(worker_mappings[i].set_plan);
		if (worker_mappings[i].delete_plan)
			SPI_freeplan(worker_mappings[i].delete_plan);
	}
}

static SPIPlanPtr
prepare_kept_plan(const char *query, int nargs, Oid *types)
{
	SPIPlanPtr	plan = SPI_prepare(query, nargs, types);

	if (plan == NULL)
		elog(ERROR, "could not prepare pg_local_cache query: %s", query);
	if (SPI_keepplan(plan) != 0)
		elog(ERROR, "could not retain pg_local_cache query plan");
	return plan;
}

static bool
reload_mappings(uint64 target_generation)
{
	MemoryContext old_context = CurrentMemoryContext;
	MemoryContext transaction_context;
	bool		success = false;

	MemoryContextReset(reload_context);
	pg_atomic_fetch_add_u64(&pglc_shared->mapping_reload_attempts, 1);
	set_worker_mappings_incomplete(true);

	PG_TRY();
	{
		int			result;
		uint64		row;
		uint64		mapping_count;
		uint64		configured_mapping_count;
		PgLocalCacheMapping *new_mappings;
		HeapTuple	count_tuple;
		TupleDesc	count_desc;
		bool		count_is_null;
		bool	mapping_has_write_mode;
		const char *mapping_query;
		MemoryContext mapping_query_old_context;
		StringInfoData mapping_query_buffer;

		transaction_context = begin_spi_transaction(pglc_statement_timeout_ms);
		free_mapping_plans();
		worker_mappings = NULL;
		worker_mapping_count = 0;
		MemoryContextReset(mapping_context);

		result = SPI_execute(
			"SELECT count(*) FROM ("
			"SELECT 1 FROM local_cache.mapping LIMIT 129"
			") AS bounded_mappings", true, 1);
		if (result != SPI_OK_SELECT || SPI_processed != 1)
			elog(ERROR, "could not count pg_local_cache mappings");
		count_tuple = SPI_tuptable->vals[0];
		count_desc = SPI_tuptable->tupdesc;
		configured_mapping_count = DatumGetInt64(
			SPI_getbinval(count_tuple, count_desc, 1, &count_is_null));
		if (count_is_null || configured_mapping_count > PGLC_MAX_MAPPINGS)
			elog(ERROR, "too many pg_local_cache mappings");

		result = SPI_execute(
			"SELECT EXISTS ("
			"    SELECT 1 FROM pg_catalog.pg_attribute AS a "
			"    JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid "
			"    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
			"     WHERE n.nspname = 'local_cache' AND c.relname = 'mapping' "
			"       AND a.attname = 'write_mode' AND a.attnum > 0 "
			"       AND NOT a.attisdropped)", true, 1);
		if (result != SPI_OK_SELECT || SPI_processed != 1)
			elog(ERROR, "could not inspect pg_local_cache mapping catalog");
		count_tuple = SPI_tuptable->vals[0];
		count_desc = SPI_tuptable->tupdesc;
		mapping_has_write_mode = DatumGetBool(
			SPI_getbinval(count_tuple, count_desc, 1, &count_is_null));
		if (count_is_null)
			elog(ERROR, "pg_local_cache mapping catalog shape is NULL");

		mapping_query_old_context = MemoryContextSwitchTo(reload_context);
		initStringInfo(&mapping_query_buffer);
		appendStringInfoString(&mapping_query_buffer,
			"WITH pglc_mapping AS ("
			"SELECT source_mapping.namespace, source_mapping.relation, "
			"       source_mapping.key_columns, source_mapping.writable, ");
		if (mapping_has_write_mode)
			appendStringInfoString(&mapping_query_buffer,
				"       CASE "
				"         WHEN source_mapping.write_mode = 'refresh' "
				"          AND NOT EXISTS ("
				"              SELECT 1 "
				"                FROM pg_catalog.pg_attribute AS mwa "
				"               WHERE mwa.attrelid = source_mapping.relation "
				"                 AND mwa.attnum > 0 "
				"                 AND NOT mwa.attisdropped "
				"                 AND (mwa.attgenerated = 'v' OR mwa.atttypid NOT IN ("
				"                     'pg_catalog.bool'::pg_catalog.regtype, "
				"                     'pg_catalog.int2'::pg_catalog.regtype, "
				"                     'pg_catalog.int4'::pg_catalog.regtype, "
				"                     'pg_catalog.int8'::pg_catalog.regtype, "
				"                     'pg_catalog.text'::pg_catalog.regtype, "
				"                     'pg_catalog.varchar'::pg_catalog.regtype))"
				"          ) THEN 'refresh' "
				"         ELSE 'invalidate' "
				"       END AS effective_write_mode ");
		else
			appendStringInfoString(&mapping_query_buffer,
				"       'invalidate'::text AS effective_write_mode ");
		appendStringInfoString(&mapping_query_buffer,
			"  FROM local_cache.mapping AS source_mapping) "
			"SELECT m.namespace, c.oid, n.nspname, c.relname, "
			"       m.key_columns, m.writable "
			"  FROM pglc_mapping AS m "
			"  JOIN pg_catalog.pg_class AS c ON c.oid = m.relation "
			"  JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
			"  JOIN pg_catalog.pg_trigger AS gt "
			"    ON gt.tgrelid = c.oid "
			"   AND gt.tgname = 'pg_local_cache_statement_guard' "
			"   AND gt.tgenabled = 'A' AND NOT gt.tgisinternal "
			"   AND gt.tgparentid = 0 AND NOT gt.tgdeferrable "
			"   AND NOT gt.tginitdeferred AND gt.tgconstraint = 0 "
			"   AND gt.tgconstrrelid = 0 AND gt.tgconstrindid = 0 "
			"   AND pg_catalog.cardinality(gt.tgattr) = 0 "
			"   AND gt.tgqual IS NULL "
			"   AND gt.tgoldtable IS NULL AND gt.tgnewtable IS NULL "
			"   AND gt.tgtype = 62 AND gt.tgnargs = 0 "
			"   AND pg_catalog.octet_length(gt.tgargs) = 0 "
			"   AND gt.tgfoid = 'local_cache._statement_guard()'::regprocedure "
			"   AND EXISTS ("
			"       SELECT 1 FROM pg_catalog.pg_depend AS gd "
			"       JOIN pg_catalog.pg_extension AS ge ON ge.oid = gd.refobjid "
			"        AND ge.extname = 'pg_local_cache' "
			"        WHERE gd.classid = 'pg_catalog.pg_trigger'::regclass "
			"          AND gd.objid = gt.oid AND gd.objsubid = 0 "
			"          AND gd.refclassid = 'pg_catalog.pg_extension'::regclass "
			"          AND gd.refobjsubid = 0 AND gd.deptype = 'x') "
			"  JOIN pg_catalog.pg_trigger AS rt "
			"    ON rt.tgrelid = c.oid "
			"   AND rt.tgname = 'pg_local_cache_row_invalidate' "
			"   AND rt.tgenabled = 'A' AND NOT rt.tgisinternal "
			"   AND rt.tgparentid = 0 AND NOT rt.tgdeferrable "
			"   AND NOT rt.tginitdeferred AND rt.tgconstraint = 0 "
			"   AND rt.tgconstrrelid = 0 AND rt.tgconstrindid = 0 "
			"   AND pg_catalog.cardinality(rt.tgattr) = 0 "
			"   AND rt.tgqual IS NULL "
			"   AND rt.tgoldtable IS NULL AND rt.tgnewtable IS NULL "
			"   AND rt.tgtype = 29 "
			"   AND rt.tgfoid = 'local_cache._row_invalidate()'::regprocedure ");
		if (mapping_has_write_mode)
			appendStringInfoString(&mapping_query_buffer,
				"   AND rt.tgnargs = 2 + pg_catalog.cardinality(m.key_columns) "
				"   AND rt.tgargs = "
				"       convert_to(m.namespace, current_setting('server_encoding')) "
				"       || decode('00', 'hex') "
				"       || convert_to(m.effective_write_mode, "
				"                      current_setting('server_encoding')) "
				"       || decode('00', 'hex') "
				"       || COALESCE((SELECT pg_catalog.string_agg("
				"              convert_to(k.column_name::text, current_setting('server_encoding')) "
				"              || decode('00', 'hex'), ''::bytea "
				"              ORDER BY k.ordinality) "
				"            FROM pg_catalog.unnest(m.key_columns) WITH ORDINALITY "
				"              AS k(column_name, ordinality)), ''::bytea) ");
		else
			appendStringInfoString(&mapping_query_buffer,
				"   AND rt.tgnargs = 1 + pg_catalog.cardinality(m.key_columns) "
				"   AND rt.tgargs = "
				"       convert_to(m.namespace, current_setting('server_encoding')) "
				"       || decode('00', 'hex') "
				"       || COALESCE((SELECT pg_catalog.string_agg("
				"              convert_to(k.column_name::text, current_setting('server_encoding')) "
				"              || decode('00', 'hex'), ''::bytea "
				"              ORDER BY k.ordinality) "
				"            FROM pg_catalog.unnest(m.key_columns) WITH ORDINALITY "
				"              AS k(column_name, ordinality)), ''::bytea) ");
		appendStringInfoString(&mapping_query_buffer,
			"   AND EXISTS ("
			"       SELECT 1 FROM pg_catalog.pg_depend AS rd "
			"       JOIN pg_catalog.pg_extension AS re ON re.oid = rd.refobjid "
			"        AND re.extname = 'pg_local_cache' "
			"        WHERE rd.classid = 'pg_catalog.pg_trigger'::regclass "
			"          AND rd.objid = rt.oid AND rd.objsubid = 0 "
			"          AND rd.refclassid = 'pg_catalog.pg_extension'::regclass "
			"          AND rd.refobjsubid = 0 AND rd.deptype = 'x') "
			"  JOIN pg_catalog.pg_trigger AS tt "
			"    ON tt.tgrelid = c.oid "
			"   AND tt.tgname = 'pg_local_cache_truncate_invalidate' "
			"   AND tt.tgenabled = 'A' AND NOT tt.tgisinternal "
			"   AND tt.tgparentid = 0 AND NOT tt.tgdeferrable "
			"   AND NOT tt.tginitdeferred AND tt.tgconstraint = 0 "
			"   AND tt.tgconstrrelid = 0 AND tt.tgconstrindid = 0 "
			"   AND pg_catalog.cardinality(tt.tgattr) = 0 "
			"   AND tt.tgqual IS NULL "
			"   AND tt.tgoldtable IS NULL AND tt.tgnewtable IS NULL "
			"   AND tt.tgtype = 32 AND tt.tgnargs = 1 "
			"   AND tt.tgfoid = 'local_cache._truncate_invalidate()'::regprocedure "
			"   AND tt.tgargs = "
			"       convert_to(m.namespace, current_setting('server_encoding')) "
			"       || decode('00', 'hex') "
			"   AND EXISTS ("
			"       SELECT 1 FROM pg_catalog.pg_depend AS td "
			"       JOIN pg_catalog.pg_extension AS te ON te.oid = td.refobjid "
			"        AND te.extname = 'pg_local_cache' "
			"        WHERE td.classid = 'pg_catalog.pg_trigger'::regclass "
			"          AND td.objid = tt.oid AND td.objsubid = 0 "
			"          AND td.refclassid = 'pg_catalog.pg_extension'::regclass "
			"          AND td.refobjsubid = 0 AND td.deptype = 'x') "
			" WHERE c.relkind = 'r' AND c.relpersistence = 'p' "
			"   AND n.nspname !~ '^pg_' "
			"   AND n.nspname <> 'information_schema' "
			"   AND c.relowner <> CURRENT_USER::pg_catalog.regrole "
			"   AND EXISTS ("
			"       SELECT 1 FROM pg_catalog.pg_roles AS worker_role "
			"        WHERE worker_role.rolname = CURRENT_USER "
			"          AND worker_role.rolcanlogin "
			"          AND NOT worker_role.rolsuper "
			"          AND NOT worker_role.rolinherit "
			"          AND NOT worker_role.rolcreatedb "
			"          AND NOT worker_role.rolcreaterole "
			"          AND NOT worker_role.rolreplication "
			"          AND NOT worker_role.rolbypassrls) "
			"   AND NOT EXISTS ("
			"       SELECT 1 FROM pg_catalog.pg_extension AS source_ext "
			"        WHERE source_ext.extname = 'pg_local_cache' "
			"          AND source_ext.extnamespace = n.oid) "
			"   AND NOT EXISTS ("
			"       SELECT 1 FROM pg_catalog.pg_depend AS source_dep "
			"        WHERE source_dep.classid = 'pg_catalog.pg_class'::regclass "
			"          AND source_dep.objid = c.oid AND source_dep.objsubid = 0 "
			"          AND source_dep.refclassid = 'pg_catalog.pg_extension'::regclass "
			"          AND source_dep.refobjsubid = 0 "
			"          AND source_dep.deptype = 'e') "
			"   AND m.namespace <> 'CRUD' "
			"   AND current_database() !~ '[.:]' "
			"   AND n.nspname !~ '[.:]' AND c.relname !~ '[.:]' "
			"   AND pg_catalog.cardinality(m.key_columns) BETWEEN 1 AND 16 "
			"   AND NOT c.relispartition "
			"   AND NOT EXISTS ("
			"       SELECT 1 FROM pg_catalog.pg_inherits AS inh "
			"        WHERE inh.inhparent = c.oid) "
			"   AND NOT EXISTS ("
			"       SELECT 1 FROM pg_catalog.pg_inherits AS inh "
			"        WHERE inh.inhrelid = c.oid) "
			"   AND NOT c.relrowsecurity AND NOT c.relforcerowsecurity "
			"   AND pg_catalog.has_schema_privilege(n.oid, 'USAGE') "
			"   AND pg_catalog.has_table_privilege(c.oid, 'SELECT') "
			"   AND (NOT m.writable OR ("
			"       pg_catalog.has_table_privilege(c.oid, 'INSERT') "
			"       AND pg_catalog.has_table_privilege(c.oid, 'UPDATE') "
			"       AND pg_catalog.has_table_privilege(c.oid, 'DELETE'))) "
			"   AND (m.writable OR ("
			"       NOT pg_catalog.has_table_privilege(c.oid, 'INSERT') "
			"       AND NOT pg_catalog.has_table_privilege(c.oid, 'UPDATE') "
			"       AND NOT pg_catalog.has_table_privilege(c.oid, 'DELETE'))) "
			"   AND NOT EXISTS ("
			"       SELECT 1 "
			"         FROM pg_catalog.unnest(m.key_columns) WITH ORDINALITY "
			"           AS k(column_name, ordinality) "
			"         LEFT JOIN pg_catalog.pg_attribute AS ka "
			"           ON ka.attrelid = c.oid AND ka.attname = k.column_name "
			"          AND ka.attnum > 0 AND NOT ka.attisdropped "
			"        WHERE ka.attnum IS NULL OR NOT ka.attnotnull "
			"           OR ka.atttypid NOT IN "
			"              ('int2'::regtype, 'int4'::regtype, 'int8'::regtype, "
			"               'text'::regtype, 'varchar'::regtype, 'bpchar'::regtype, "
			"               'uuid'::regtype) "
			"           OR (ka.attcollation <> 0 AND NOT EXISTS ("
			"               SELECT 1 FROM pg_catalog.pg_collation AS coll "
			"                WHERE coll.oid = ka.attcollation "
			"                  AND coll.collisdeterministic))) "
			"   AND EXISTS ("
			"       SELECT 1 FROM pg_catalog.pg_index AS i "
			"        WHERE i.indrelid = c.oid "
			"          AND i.indisprimary "
			"          AND i.indisunique AND i.indimmediate "
			"          AND i.indisvalid AND i.indisready "
			"          AND i.indpred IS NULL AND i.indexprs IS NULL "
			"          AND EXISTS ("
			"              SELECT 1 FROM pg_catalog.pg_class AS ic "
			"              JOIN pg_catalog.pg_am AS am ON am.oid = ic.relam "
			"               WHERE ic.oid = i.indexrelid AND am.amname = 'btree') "
			"          AND i.indnkeyatts = pg_catalog.cardinality(m.key_columns) "
			"          AND NOT EXISTS ("
			"              SELECT 1 "
			"                FROM pg_catalog.unnest(m.key_columns) WITH ORDINALITY "
			"                  AS k(column_name, ordinality) "
			"                JOIN pg_catalog.pg_attribute AS ka "
			"                  ON ka.attrelid = c.oid AND ka.attname = k.column_name "
			"               WHERE i.indkey[k.ordinality::integer - 1] <> ka.attnum) "
			"          AND NOT EXISTS ("
			"              SELECT 1 "
			"                FROM pg_catalog.unnest(m.key_columns) WITH ORDINALITY "
			"                  AS k(column_name, ordinality) "
			"                JOIN pg_catalog.pg_attribute AS ka "
			"                  ON ka.attrelid = c.oid AND ka.attname = k.column_name "
			"                LEFT JOIN pg_catalog.pg_opclass AS opc "
			"                  ON opc.oid = i.indclass[k.ordinality::integer - 1] "
			"               WHERE opc.oid IS NULL OR NOT opc.opcdefault "
			"                  OR NOT (opc.opcintype = ka.atttypid OR EXISTS ("
			"                      SELECT 1 FROM pg_catalog.pg_cast AS pc "
			"                       WHERE pc.castsource = ka.atttypid "
			"                         AND pc.casttarget = opc.opcintype "
			"                         AND pc.castmethod = 'b')))) "
			"   AND NOT (m.writable AND EXISTS ("
			"       SELECT 1 FROM pg_catalog.pg_attribute AS wa "
			"        WHERE wa.attrelid = c.oid AND wa.attnum > 0 "
			"          AND NOT wa.attisdropped "
			"          AND wa.attname = ANY (m.key_columns) "
			"          AND wa.attgenerated <> '')) "
			" ORDER BY m.namespace LIMIT 129");
		mapping_query = mapping_query_buffer.data;
		MemoryContextSwitchTo(mapping_query_old_context);
		result = SPI_execute(mapping_query, true, PGLC_MAX_MAPPINGS + 1);
		if (result != SPI_OK_SELECT)
			elog(ERROR, "could not load pg_local_cache mappings");
		if (SPI_processed > PGLC_MAX_MAPPINGS)
			elog(ERROR, "too many pg_local_cache mappings");
		mapping_count = SPI_processed;

		new_mappings = MemoryContextAllocZero(mapping_context,
										  sizeof(PgLocalCacheMapping) *
										  Max((uint64) 1, mapping_count));

		for (row = 0; row < mapping_count; row++)
		{
			HeapTuple	tuple = SPI_tuptable->vals[row];
			TupleDesc	desc = SPI_tuptable->tupdesc;
			PgLocalCacheMapping *mapping = &new_mappings[row];
			bool		is_null;
			Datum		key_array_datum;
			ArrayType  *key_array;
			Datum	   *key_names;
			bool	   *key_nulls;
			int			key_count;
			int			key_index;
			int			attribute_index;
			Relation	relation;
			LOCKMODE	relation_lockmode;
			TupleDesc	source_desc;
			MemoryContext mapping_old_context;

			strlcpy(mapping->nspace, SPI_getvalue(tuple, desc, 1),
					sizeof(mapping->nspace));
			mapping->relation_oid =
				DatumGetObjectId(SPI_getbinval(tuple, desc, 2, &is_null));
			Assert(!is_null);
			strlcpy(mapping->schema_name, SPI_getvalue(tuple, desc, 3),
					sizeof(mapping->schema_name));
			strlcpy(mapping->relation_name, SPI_getvalue(tuple, desc, 4),
					sizeof(mapping->relation_name));
			key_array_datum = SPI_getbinval(tuple, desc, 5, &is_null);
			if (is_null)
				elog(ERROR, "pg_local_cache key_columns unexpectedly became NULL");
			key_array = DatumGetArrayTypeP(key_array_datum);
			deconstruct_array(key_array, NAMEOID, NAMEDATALEN, false, 'c',
							  &key_names, &key_nulls, &key_count);
			if (key_count < 1 || key_count > PGLC_MAX_KEY_COLUMNS)
				elog(ERROR, "invalid pg_local_cache primary-key column count");
			mapping->key_count = key_count;
			for (key_index = 0; key_index < key_count; key_index++)
			{
				HeapTuple	attribute_tuple;
				Form_pg_attribute attribute;
				Oid			input_function;
				Oid			output_function;
				bool		is_varlena;

				if (key_nulls[key_index])
					elog(ERROR, "pg_local_cache primary-key column cannot be NULL");
				strlcpy(mapping->key_columns[key_index],
						NameStr(*DatumGetName(key_names[key_index])), NAMEDATALEN);
				attribute_tuple = SearchSysCache2(ATTNAME,
					ObjectIdGetDatum(mapping->relation_oid),
					CStringGetDatum(mapping->key_columns[key_index]));
				if (!HeapTupleIsValid(attribute_tuple))
					elog(ERROR, "could not load pg_local_cache primary-key column");
				attribute = (Form_pg_attribute) GETSTRUCT(attribute_tuple);
				mapping->key_attnos[key_index] = attribute->attnum;
				mapping->key_types[key_index] = attribute->atttypid;
				mapping->key_typmods[key_index] = attribute->atttypmod;
				ReleaseSysCache(attribute_tuple);

				getTypeInputInfo(mapping->key_types[key_index], &input_function,
								 &mapping->key_ioparams[key_index]);
				getTypeOutputInfo(mapping->key_types[key_index], &output_function,
								  &is_varlena);
				fmgr_info_cxt(input_function, &mapping->key_inputs[key_index],
							  mapping_context);
				fmgr_info_cxt(output_function, &mapping->key_outputs[key_index],
							  mapping_context);
			}
			mapping->writable =
				DatumGetBool(SPI_getbinval(tuple, desc, 6, &is_null));
			Assert(!is_null);
			mapping->config_generation = target_generation;
			/* Slot pressure disables caching for this mapping, not SQL service. */
			(void) pglc_resolve_mapping_slot(mapping);

			mapping_old_context = MemoryContextSwitchTo(mapping_context);
			relation_lockmode = mapping->writable ? RowExclusiveLock : AccessShareLock;
			relation = table_open(mapping->relation_oid, relation_lockmode);
			/* Constraints are not needed for decoding and are not size-bounded. */
			source_desc = RelationGetDescr(relation);
			mapping->row_desc = CreateTupleDescCopy(source_desc);
			/*
			 * CreateTupleDescCopy deliberately clears these two constraint flags.
			 * Keep only the fixed-size metadata needed to omit generated columns
			 * from writable plans and to fingerprint row-shape semantics.
			 */
			for (attribute_index = 0;
				 attribute_index < mapping->row_desc->natts;
				 attribute_index++)
			{
				Form_pg_attribute source_attribute =
					TupleDescAttr(source_desc, attribute_index);
				Form_pg_attribute copied_attribute =
					TupleDescAttr(mapping->row_desc, attribute_index);

				copied_attribute->attgenerated = source_attribute->attgenerated;
				copied_attribute->attidentity = source_attribute->attidentity;
			}
			mapping->row_type_oid = mapping->row_desc->tdtypeid;
			mapping->row_typmod = mapping->row_desc->tdtypmod;
			mapping->row_natts = mapping->row_desc->natts;
			mapping->row_descriptor_fingerprint =
				pglc_row_payload_tupledesc_fingerprint(mapping->row_desc);
			table_close(relation, NoLock);
			MemoryContextSwitchTo(mapping_old_context);
		}

		worker_mappings = new_mappings;
		worker_mapping_count = (int) mapping_count;

		/* SPI_prepare changes SPI_tuptable, so plans are built in a second pass. */
		for (row = 0; row < mapping_count; row++)
		{
			PgLocalCacheMapping *mapping = &new_mappings[row];
			MemoryContext query_old_context;
			char	   *qualified_relation;
			StringInfoData where_clause;
			StringInfoData conflict_columns;
			char	   *get_query;
			Oid			get_types[PGLC_MAX_KEY_COLUMNS];
			char	   *set_query;
			Oid			set_types[PGLC_MAX_KEY_COLUMNS + 1];
			char	   *delete_query;
			Oid			delete_types[PGLC_MAX_KEY_COLUMNS];
			int			key_index;

			query_old_context = MemoryContextSwitchTo(reload_context);
			qualified_relation = quote_qualified_identifier(
				mapping->schema_name, mapping->relation_name);
			initStringInfo(&where_clause);
			initStringInfo(&conflict_columns);
			for (key_index = 0; key_index < mapping->key_count; key_index++)
			{
				const char *quoted_key = quote_identifier(
					mapping->key_columns[key_index]);

				if (key_index > 0)
				{
					appendStringInfoString(&where_clause, " AND ");
					appendStringInfoString(&conflict_columns, ", ");
				}
				appendStringInfo(&where_clause, "pglc_source.%s = $%d",
								 quoted_key, key_index + 1);
				appendStringInfoString(&conflict_columns, quoted_key);
				get_types[key_index] = mapping->key_types[key_index];
				set_types[key_index] = mapping->key_types[key_index];
				delete_types[key_index] = mapping->key_types[key_index];
			}

			get_query = psprintf(
				"SELECT pglc_source, pglc_source.xmin "
				"FROM ONLY %s AS pglc_source "
				"WHERE %s LIMIT 1", qualified_relation, where_clause.data);
			mapping->get_plan = prepare_kept_plan(
				get_query, mapping->key_count, get_types);

			if (mapping->writable)
			{
				StringInfoData insert_columns;
				StringInfoData insert_values;
				StringInfoData updates;
				int			attribute_index;

				initStringInfo(&insert_columns);
				initStringInfo(&insert_values);
				initStringInfo(&updates);
				for (attribute_index = 0;
					 attribute_index < mapping->row_desc->natts;
					 attribute_index++)
				{
					Form_pg_attribute attribute =
						TupleDescAttr(mapping->row_desc, attribute_index);
					const char *quoted_column;
					int			component = -1;

					if (attribute->attisdropped || attribute->attgenerated != '\0')
						continue;
					quoted_column = quote_identifier(NameStr(attribute->attname));
					if (insert_columns.len > 0)
					{
						appendStringInfoString(&insert_columns, ", ");
						appendStringInfoString(&insert_values, ", ");
					}
					appendStringInfoString(&insert_columns, quoted_column);
					for (key_index = 0; key_index < mapping->key_count;
						 key_index++)
					{
						if (mapping->key_attnos[key_index] == attribute->attnum)
						{
							component = key_index;
							break;
						}
					}
					if (component >= 0)
						appendStringInfo(&insert_values, "$%d", component + 1);
					else
					{
						appendStringInfo(&insert_values, "pglc_input.%s",
										 quoted_column);
						if (updates.len > 0)
							appendStringInfoString(&updates, ", ");
						appendStringInfo(&updates, "%s = EXCLUDED.%s",
									 quoted_column, quoted_column);
					}
				}
				set_types[mapping->key_count] = JSONBOID;
				set_query = psprintf(
					"INSERT INTO %s (%s) OVERRIDING SYSTEM VALUE SELECT %s FROM "
					"pg_catalog.jsonb_populate_record(NULL::%s, $%d) "
					"AS pglc_input ON CONFLICT (%s) %s",
					qualified_relation, insert_columns.data,
					insert_values.data, qualified_relation,
					mapping->key_count + 1, conflict_columns.data,
					updates.len > 0 ? psprintf("DO UPDATE SET %s", updates.data) :
					"DO NOTHING");
				mapping->set_plan = prepare_kept_plan(
					set_query, mapping->key_count + 1, set_types);
				delete_query = psprintf(
					"DELETE FROM ONLY %s AS pglc_source WHERE %s",
					qualified_relation, where_clause.data);
				mapping->delete_plan = prepare_kept_plan(delete_query,
										 mapping->key_count,
										 delete_types);
			}
			MemoryContextSwitchTo(query_old_context);
		}

		commit_spi_transaction(transaction_context);
		worker_mapping_generation = target_generation;
		if (mapping_count != configured_mapping_count)
		{
			pg_atomic_fetch_add_u64(
				&pglc_shared->mapping_reload_incomplete_retries, 1);
			set_worker_mapping_generation(0);
			worker_retry_generation = target_generation;
		}
		else
		{
			set_worker_mapping_generation(target_generation);
			worker_retry_generation = 0;
		}
		set_worker_mappings_incomplete(
			mapping_count != configured_mapping_count);
		worker_next_mapping_retry = worker_mappings_incomplete ?
			TimestampTzPlusMilliseconds(GetCurrentTimestamp(), 1000) : 0;
		success = true;
	}
	PG_CATCH();
	{
		ErrorData  *error_data;

		MemoryContextSwitchTo(old_context);
		error_data = CopyErrorData();
		FlushErrorState();
		if (error_data->elevel >= FATAL || ProcDiePending)
			ReThrowError(error_data);
		disable_all_timeouts(false);
		QueryCancelPending = false;
		abort_spi_transaction(old_context);
		pg_atomic_fetch_add_u64(&pglc_shared->mapping_reload_failures, 1);
		free_mapping_plans();
		MemoryContextReset(mapping_context);
		worker_mappings = NULL;
		worker_mapping_count = 0;
		set_worker_mapping_generation(0);
		set_worker_mappings_incomplete(true);
		worker_retry_generation = target_generation;
		worker_next_mapping_retry =
			TimestampTzPlusMilliseconds(GetCurrentTimestamp(), 1000);
		ereport(LOG,
				(errmsg("pg_local_cache mappings are unavailable: %s",
						error_data->message)));
		FreeErrorData(error_data);
	}
	PG_END_TRY();
	MemoryContextReset(reload_context);
	return success;
}
