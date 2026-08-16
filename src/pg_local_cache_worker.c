#include "postgres.h"

#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <signal.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/socket.h>
#include <unistd.h>

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
#include "storage/fd.h"
#include "storage/ipc.h"
#include "storage/latch.h"
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

#define PGLC_OUTPUT_BATCH_BYTES (16 * 1024)
#define PGLC_OUTPUT_BUFFER_MAX \
	(PGLC_RESPONSE_MAX + PGLC_OUTPUT_BATCH_BYTES)
#define PGLC_READY_CLIENTS_PER_TURN 8
#define PGLC_AUTH_TOKEN_FILE_MAX 256

typedef struct PgLocalCacheClient
{
	int			fd;
	bool		authenticated;
	bool		close_after_flush;
	bool		input_ready;
	uint8		authentication_failures;
	Size		input_start;
	Size		used;
	Size		output_used;
	Size		output_sent;
	TimestampTz last_activity;
	char		input[PGLC_REQUEST_MAX];
	char		output[PGLC_OUTPUT_BUFFER_MAX];
} PgLocalCacheClient;

static MemoryContext mapping_context = NULL;
static MemoryContext reload_context = NULL;
static MemoryContext command_context = NULL;
static PgLocalCacheMapping *worker_mappings = NULL;
static int	worker_mapping_count = 0;
static uint64 worker_mapping_generation = 0;
static TimestampTz worker_next_mapping_retry = 0;
static uint64 worker_retry_generation = 0;
static bool worker_mappings_incomplete = false;
static int	worker_slot = -1;
static char *worker_auth_token = NULL;
static uint64 worker_client_reservations = 0;
static bool worker_counted_active = false;

static void load_auth_token(void);
static void worker_before_exit(int code, Datum arg);
static void validate_file_descriptor_limit(void);
static int create_listener(void);
static void run_server(int listener);
static void close_client(PgLocalCacheClient *client);
static void compact_client_input(PgLocalCacheClient *client);
static bool flush_client_output(PgLocalCacheClient *client);
static bool queue_response(PgLocalCacheClient *client,
						   const char *response, Size response_length,
						   bool close_after);
static bool process_client(PgLocalCacheClient *client);
static char *execute_command(PgLocalCacheClient *client,
							 PgLocalCacheRespArg *args, int argc,
							 Size *response_length, bool *close_after);
static char *execute_command_inner(PgLocalCacheClient *client,
								   PgLocalCacheRespArg *args, int argc,
								   Size *response_length, bool *close_after);
static void maybe_reload_mappings(void);
static bool reload_mappings(uint64 target_generation);
static void set_worker_mappings_incomplete(bool incomplete);
static void set_worker_mapping_generation(uint64 generation);
static bool resolve_wire_key(const PgLocalCacheRespArg *wire_key,
								 PgLocalCacheMapping **mapping, char **raw_key,
								 char **error);
static bool canonicalize_key(PgLocalCacheMapping *mapping, const char *raw_key,
								 Datum *key_values, char **canonical, char **error);
static bool row_json_validate(PgLocalCacheMapping *mapping, Jsonb *row,
							  Datum *key_values, char **error);
static bool cached_row_json(PgLocalCacheMapping *mapping,
							const char *payload, Size payload_length,
							MemoryContext result_context,
							char **json, Size *json_length);
static void ensure_mapping_current(const PgLocalCacheMapping *mapping);
static char *command_mget_one(PgLocalCacheMapping *mapping, const char *raw_key,
							  TimestampTz deadline, Size *response_length);
static char *command_mget(PgLocalCacheRespArg *args, int argc,
							  Size *response_length);
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
	pglc_note_worker_start();
	worker_counted_active = true;

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

	listener = create_listener();
	ereport(LOG,
			(errmsg("pg_local_cache worker %d listening on %s:%d for database \"%s\"",
					DatumGetInt32(main_arg), pglc_bind_address, pglc_port,
					pglc_database)));
	run_server(listener);
	close(listener);
	proc_exit(0);
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
	if (pglc_port == 0)
		return 0;
	return mul_size((Size) pglc_worker_count,
					pglc_worker_memory_bytes_per_worker());
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
	rlim_t		required = (rlim_t) pglc_max_clients_per_worker + 33;

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
				 errhint("Raise the container/process nofile limit or lower pg_local_cache.max_clients_per_worker.")));
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

	if (strcmp(pglc_bind_address, "0.0.0.0") == 0 &&
		strlen(worker_auth_token) < 32)
		ereport(FATAL,
				(errmsg("a non-loopback pg_local_cache listener requires an auth token of at least 32 bytes")));
}

static int
create_listener(void)
{
	int			fd;
	int			enabled = 1;
	int			flags;
	struct sockaddr_in address;

	if (pglc_bind_address == NULL ||
		(strcmp(pglc_bind_address, "127.0.0.1") != 0 &&
		 strcmp(pglc_bind_address, "0.0.0.0") != 0))
		ereport(FATAL,
					(errmsg("pg_local_cache.bind_address must be an IPv4 literal"),
					 errhint("Supported values are 127.0.0.1 and 0.0.0.0.")));

	if (strcmp(pglc_bind_address, "0.0.0.0") == 0 &&
		(worker_auth_token == NULL || worker_auth_token[0] == '\0'))
		ereport(FATAL,
				(errmsg("pg_local_cache refuses a non-loopback listener without authentication")));

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
	if (inet_pton(AF_INET, pglc_bind_address, &address.sin_addr) != 1)
		ereport(FATAL,
				(errmsg("invalid pg_local_cache.bind_address \"%s\"",
						pglc_bind_address)));

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
		int			poll_count = 1;
		int			poll_result;
		int			ready_clients_processed = 0;
		int			ready_scan_start = next_ready_client;
		int			latch_result;
		int			step;
		TimestampTz now = GetCurrentTimestamp();

		maybe_reload_mappings();

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

			if (client->fd < 0 || !client->input_ready ||
				client->output_sent < client->output_used)
				continue;
			client->input_ready = false;
			if (!process_client(client))
				close_client(client);
			next_ready_client =
				(client_index + 1) % client_slots;
			if (++ready_clients_processed >= PGLC_READY_CLIENTS_PER_TURN)
				break;
		}
		if (ready_clients_processed == 0)
			next_ready_client =
				(next_ready_client + 1) % client_slots;

		poll_fds[0].fd = listener;
		poll_fds[0].events = POLLIN;
		poll_fds[0].revents = 0;
		poll_to_client[0] = -1;

		for (i = 0; i < client_slots; i++)
		{
			if (clients[i].fd >= 0)
			{
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
				poll_fds[poll_count].events =
					(clients[i].output_sent < clients[i].output_used) ?
					POLLOUT : POLLIN;
				poll_fds[poll_count].revents = 0;
				poll_to_client[poll_count] = i;
				poll_count++;
				if (clients[i].input_ready &&
					clients[i].output_sent == clients[i].output_used)
					have_buffered_ready = true;
			}
		}

		poll_result = poll(poll_fds, poll_count,
						   have_buffered_ready ? 0 : 250);
		if (poll_result < 0 && errno != EINTR)
			ereport(LOG,
					(errcode_for_socket_access(),
					 errmsg("pg_local_cache poll failed: %m")));

		latch_result = WaitLatch(MyLatch,
								 WL_LATCH_SET | WL_TIMEOUT |
								 WL_POSTMASTER_DEATH,
								 0,
								 PG_WAIT_EXTENSION);
		ResetLatch(MyLatch);
		if (latch_result & WL_POSTMASTER_DEATH)
			proc_exit(1);
		CHECK_FOR_INTERRUPTS();

		if (poll_result <= 0)
			continue;

		if (poll_fds[0].revents & POLLIN)
		{
			int			accepted = 0;

			while (accepted++ < 32)
			{
				int			client_fd;
				int			slot = -1;
				int			flags;
				int			enabled = 1;

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

				clients[slot].fd = client_fd;
				clients[slot].input_start = 0;
				clients[slot].used = 0;
				clients[slot].output_used = 0;
				clients[slot].output_sent = 0;
				clients[slot].close_after_flush = false;
				clients[slot].input_ready = false;
				clients[slot].authentication_failures = 0;
				clients[slot].last_activity = GetCurrentTimestamp();
				clients[slot].authenticated =
					worker_auth_token == NULL || worker_auth_token[0] == '\0';
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
				close_client(&clients[client_index]);
				continue;
			}
			if (poll_fds[i].revents & POLLOUT)
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
					clients[client_index].input_start <
					clients[client_index].used)
					clients[client_index].input_ready = true;
				if (!(poll_fds[i].revents & POLLHUP))
					continue;
			}
			if (poll_fds[i].revents & POLLIN)
			{
				if (!process_client(&clients[client_index]))
					close_client(&clients[client_index]);
			}
			if (clients[client_index].fd < 0)
				continue;
			if (poll_fds[i].revents & POLLHUP)
			{
				/*
				 * POLLHUP can accompany the final POLLIN.  Drain complete requests
				 * already copied into userspace when their replies were flushed.  A
				 * full hangup with backpressured output cannot make progress and must
				 * close instead of spinning because poll reports HUP unconditionally.
				 */
				if (clients[client_index].input_start <
					clients[client_index].used &&
					clients[client_index].output_sent ==
					clients[client_index].output_used)
				{
					clients[client_index].input_ready = true;
					continue;
				}
				close_client(&clients[client_index]);
			}
		}
	}

	for (i = 0; i < client_slots; i++)
		close_client(&clients[i]);
	pfree(poll_to_client);
	pfree(poll_fds);
	pfree(clients);
}

static void
close_client(PgLocalCacheClient *client)
{
	if (client->fd >= 0)
	{
		close(client->fd);
		pg_atomic_fetch_add_u64(&pglc_shared->client_disconnects, 1);
		pglc_release_clients(1);
		Assert(worker_client_reservations > 0);
		worker_client_reservations--;
	}
	client->fd = -1;
	client->input_start = 0;
	client->used = 0;
	client->output_used = 0;
	client->output_sent = 0;
	client->close_after_flush = false;
	client->input_ready = false;
	client->authentication_failures = 0;
	client->authenticated = false;
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

static bool
flush_client_output(PgLocalCacheClient *client)
{
	bool		wrote = false;

	while (client->output_sent < client->output_used)
	{
		ssize_t		written = send(client->fd,
								   client->output + client->output_sent,
								   client->output_used - client->output_sent,
#ifdef MSG_NOSIGNAL
								   MSG_NOSIGNAL
#else
								   0
#endif
			);

		if (written > 0)
		{
			client->output_sent += (Size) written;
			wrote = true;
			continue;
		}
		if (written < 0 && errno == EINTR)
			continue;
		if (written < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
		{
			pg_atomic_fetch_add_u64(
				&pglc_shared->output_backpressure_events, 1);
			if (wrote)
				client->last_activity = GetCurrentTimestamp();
			return true;
		}
		return false;
	}
	if (wrote)
		client->last_activity = GetCurrentTimestamp();
	client->output_used = 0;
	client->output_sent = 0;
	return true;
}

static bool
queue_response(PgLocalCacheClient *client,
			   const char *response, Size response_length,
			   bool close_after)
{
	if (client->output_sent != 0 ||
		response_length > sizeof(client->output) - client->output_used)
		return false;
	memcpy(client->output + client->output_used, response, response_length);
	client->output_used += response_length;
	client->close_after_flush |= close_after;
	return true;
}

static bool
finish_client_turn(PgLocalCacheClient *client)
{
	if (client->input_start == client->used)
	{
		client->input_start = 0;
		client->used = 0;
	}
	if (!flush_client_output(client))
		return false;
	return !(client->close_after_flush &&
			 client->output_sent == client->output_used);
}

static bool
process_client(PgLocalCacheClient *client)
{
	bool		read_attempted = false;
	int			commands_processed = 0;

	if (client->output_sent < client->output_used)
		return true;
	client->input_ready = false;
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
			if (sizeof(client->output) - client->output_used <
				PGLC_RESPONSE_MAX)
			{
				if (!flush_client_output(client))
					return false;
				if (client->output_sent < client->output_used)
				{
					client->input_ready = true;
					return true;
				}
			}

			CHECK_FOR_INTERRUPTS();
			parse_result = pglc_resp_parse(
				client->input + client->input_start,
				client->used - client->input_start,
				args, &argc, &consumed, &protocol_error);
			if (parse_result == 0)
			{
				compact_client_input(client);
				if (client->used == sizeof(client->input))
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
					&pglc_shared->client_request_errors, 1);
				error_response = pglc_resp_error(protocol_error, &error_length);
				queued = queue_response(client, error_response,
										error_length, true);
				pfree(error_response);
				if (!queued)
					return false;
				client->input_start = client->used;
				return finish_client_turn(client);
			}

			previous_context = MemoryContextSwitchTo(command_context);
			pg_atomic_fetch_add_u64(&pglc_shared->client_requests, 1);
			response = execute_command(client, args, argc,
								   &response_length, &close_after);
			if (response_length > 0 && response[0] == '-')
				pg_atomic_fetch_add_u64(&pglc_shared->client_request_errors, 1);
			queued = queue_response(client, response, response_length,
								close_after);
			MemoryContextSwitchTo(previous_context);
			if (queued)
				client->input_start += consumed;
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

		compact_client_input(client);
		for (;;)
		{
			ssize_t		received;

			received = recv(client->fd, client->input + client->used,
								sizeof(client->input) - client->used, 0);
			if (received < 0 && errno == EINTR)
				continue;
			read_attempted = true;
			if (received == 0)
			{
				client->close_after_flush = true;
				return finish_client_turn(client);
			}
			if (received > 0)
			{
				client->used += (Size) received;
				client->last_activity = GetCurrentTimestamp();
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
				Size *response_length, bool *close_after)
{
	MemoryContext error_context = CurrentMemoryContext;
	char	   *response = NULL;

	PG_TRY();
	{
		response = execute_command_inner(client, args, argc,
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
		if (IsTransactionState())
			AbortCurrentTransaction();
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
execute_command_inner(PgLocalCacheClient *client, PgLocalCacheRespArg *args, int argc,
					  Size *response_length, bool *close_after)
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
			pg_atomic_fetch_add_u64(&pglc_shared->client_sets, 1);
			return command_set(mapping, raw_key, &args[2], response_length);
		}
		pg_atomic_fetch_add_u64(&pglc_shared->client_dels, 1);
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
		return command_mget(args, argc, response_length);
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
		json = pglc_stats_json();
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
				MemoryContext result_context, char **json, Size *json_length)
{
	PgLocalCacheRowPayloadView view;
	const char *cached_json;

	if (!pglc_row_payload_decode(payload, payload_length, mapping->row_desc,
								 mapping->row_descriptor_fingerprint,
								 result_context, &view))
		return false;
	if (!pglc_row_payload_get_json(&view, &cached_json, json_length))
		return false;
	*json = (char *) cached_json;
	return true;
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
begin_spi_transaction(int statement_timeout_ms)
{
	char		timeout[32];

	Assert(statement_timeout_ms > 0);
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
commit_spi_transaction(void)
{
	PopActiveSnapshot();
	if (SPI_finish() != SPI_OK_FINISH)
		elog(ERROR, "pg_local_cache could not finish SPI");
	if (get_timeout_active(STATEMENT_TIMEOUT))
		disable_timeout(STATEMENT_TIMEOUT, false);
	(void) get_timeout_indicator(STATEMENT_TIMEOUT, true);
	CommitTransactionCommand();
}

static void
note_resp_cache_lookup(bool hit, bool negative)
{
	if (hit)
	{
		pg_atomic_fetch_add_u64(&pglc_shared->cache_hits, 1);
		if (negative)
			pg_atomic_fetch_add_u64(&pglc_shared->negative_hits, 1);
	}
	else
		pg_atomic_fetch_add_u64(&pglc_shared->cache_misses, 1);
}

static char *
command_mget_one(PgLocalCacheMapping *mapping, const char *raw_key,
				 TimestampTz deadline, Size *response_length)
{
	Datum		key_values[PGLC_MAX_KEY_COLUMNS];
	char	   *canonical;
	char	   *key_error = NULL;
	char		cached_value[PGLC_VALUE_MAX];
	Size		cached_length;
	bool		negative;
	TransactionId source_xmin;
	PgLocalCacheReadToken token;
	bool		hit;
	bool		owns_load = false;
	bool		waiter_counted = false;
	uint64		load_id = 0;
	TimestampTz wait_started;
	Datum		values[PGLC_MAX_KEY_COLUMNS];
	char	   *database_value = NULL;
	Size		database_value_length = 0;
	Size		database_payload_length = 0;
	bool		database_payload_cacheable = false;
	TransactionId database_xmin = InvalidTransactionId;
	MemoryContext result_context = CurrentMemoryContext;
	int			statement_timeout_ms = pglc_statement_timeout_ms;
	int			i;

	if (deadline != 0 && GetCurrentTimestamp() >= deadline)
		return pglc_resp_error("ERR MGET deadline exceeded", response_length);
	if (!canonicalize_key(mapping, raw_key, key_values,
						  &canonical, &key_error))
		return pglc_resp_error(key_error, response_length);
	(void) canonical;
	for (i = 0; i < mapping->key_count; i++)
		values[i] = key_values[i];

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
								result_context, &json, &json_length))
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
									result_context, &json, &json_length))
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
				continue;
			}
		}
		if (claim == PGLC_LOAD_RETRY)
			continue;
		if (TimestampDifferenceExceeds(wait_started, GetCurrentTimestamp(),
								   pglc_singleflight_wait_ms))
		{
			pglc_note_singleflight_timeout();
			break;
		}
		(void) WaitLatch(MyLatch,
						 WL_LATCH_SET | WL_TIMEOUT | WL_EXIT_ON_PM_DEATH,
						 1L, PG_WAIT_EXTENSION);
		ResetLatch(MyLatch);
		CHECK_FOR_INTERRUPTS();
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

	PG_TRY();
	{
		begin_spi_transaction(statement_timeout_ms);
		ensure_mapping_current(mapping);
		pg_atomic_fetch_add_u64(&pglc_shared->pass_to_main, 1);
		if (SPI_execute_plan(mapping->get_plan, values, NULL, true, 1) !=
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
				database_payload_cacheable = pglc_row_payload_encode(
					row_slot, mapping->row_desc,
					PGLC_ROW_PAYLOAD_FLAG_HAS_JSON,
					cached_value, sizeof(cached_value),
					&database_payload_length);
				if (!database_payload_cacheable)
				{
					/* Keep a SQL-usable tuple even when tuple+JSON cannot fit. */
					database_payload_cacheable = pglc_row_payload_encode(
						row_slot, mapping->row_desc, 0,
						cached_value, sizeof(cached_value),
						&database_payload_length);
				}
				if (database_payload_cacheable &&
					cached_row_json(mapping, cached_value,
								database_payload_length, result_context,
								&rendered_json, &rendered_json_length))
				{
					/* The encoded JSON is safe to copy out of the SPI context. */
				}
				else if (!source_row_json(row_slot, mapping->row_desc,
								  row_value, result_context,
								  &rendered_json, &rendered_json_length))
					ereport(ERROR,
								(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
								 errmsg("row JSON exceeds the RESP limit of %d bytes",
										PGLC_RESPONSE_VALUE_MAX)));
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
		commit_spi_transaction();
		pglc_note_database_read();

		if (database_value == NULL)
			pglc_cache_store(mapping, canonical, &token, NULL, 0, true,
							 owns_load ? load_id : 0,
							 InvalidTransactionId);
		else if (database_payload_cacheable)
			pglc_cache_store(mapping, canonical, &token,
							 cached_value, database_payload_length, false,
							 owns_load ? load_id : 0,
							 database_xmin);
		if (owns_load)
		{
			pglc_cache_release_load(mapping, canonical, &token, load_id);
			owns_load = false;
		}
	}
	PG_CATCH();
	{
		if (owns_load)
			pglc_cache_release_load(mapping, canonical, &token, load_id);
		PG_RE_THROW();
	}
	PG_END_TRY();

	if (database_value == NULL)
		return pglc_resp_null(response_length);

	return pglc_resp_bulk(database_value, database_value_length,
						 response_length);
}

static char *
command_mget(PgLocalCacheRespArg *args, int argc, Size *response_length)
{
	int			key_count = argc - 1;
	TimestampTz deadline = TimestampTzPlusMilliseconds(
		GetCurrentTimestamp(), pglc_statement_timeout_ms);
	PgLocalCacheMapping **mappings;
	char	  **raw_keys;
	int			key_index;
	StringInfoData response;

	mappings = palloc(mul_size(sizeof(*mappings), (Size) key_count));
	raw_keys = palloc(mul_size(sizeof(*raw_keys), (Size) key_count));
	for (key_index = 0; key_index < key_count; key_index++)
	{
		Datum		key_values[PGLC_MAX_KEY_COLUMNS];
		char	   *canonical;
		char	   *key_error = NULL;

		if (!resolve_wire_key(&args[key_index + 1], &mappings[key_index],
							  &raw_keys[key_index], &key_error) ||
			!canonicalize_key(mappings[key_index], raw_keys[key_index],
							  key_values, &canonical, &key_error))
			return pglc_resp_error(key_error, response_length);
		(void) canonical;
	}

	pg_atomic_fetch_add_u64(&pglc_shared->client_mget_keys, (uint64) key_count);
	initStringInfo(&response);
	appendStringInfo(&response, "*%d\r\n", key_count);
	for (key_index = 0; key_index < key_count; key_index++)
	{
		Size		element_length;
		char	   *element = command_mget_one(
			mappings[key_index], raw_keys[key_index], deadline, &element_length);

		if (element_length > 0 && element[0] == '-')
		{
			*response_length = element_length;
			return element;
		}
		if (element_length > PGLC_RESPONSE_MAX ||
			(Size) response.len > PGLC_RESPONSE_MAX - element_length)
			return pglc_resp_error("ERR response exceeds limit",
								  response_length);
		appendBinaryStringInfo(&response, element, (int) element_length);
	}
	if (GetCurrentTimestamp() >= deadline)
		return pglc_resp_error("ERR MGET deadline exceeded", response_length);
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
	begin_spi_transaction(pglc_statement_timeout_ms);
	ensure_mapping_current(mapping);
	if (!row_json_validate(mapping, row, key_values, &key_error))
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg_internal("%s",
							 key_error != NULL && strncmp(key_error, "ERR ", 4) == 0 ?
							 key_error + 4 : key_error)));
	values[mapping->key_count] = JsonbPGetDatum(row);

	pg_atomic_fetch_add_u64(&pglc_shared->pass_to_main, 1);
	pg_atomic_fetch_add_u64(&pglc_shared->sql_sets, 1);
	if (SPI_execute_plan(mapping->set_plan, values, NULL, false, 0) !=
		SPI_OK_INSERT)
		elog(ERROR, "pg_local_cache SET plan failed");
	ensure_mapping_current(mapping);
	commit_spi_transaction();
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
	uint64		deleted;

	if (!mapping->writable)
		return pglc_resp_error("ERR namespace is read-only", response_length);
	if (!canonicalize_key(mapping, raw_key, values,
						  &canonical, &key_error))
		return pglc_resp_error(key_error, response_length);
	(void) canonical;

	begin_spi_transaction(pglc_statement_timeout_ms);
	ensure_mapping_current(mapping);
	pg_atomic_fetch_add_u64(&pglc_shared->pass_to_main, 1);
	pg_atomic_fetch_add_u64(&pglc_shared->sql_dels, 1);
	if (SPI_execute_plan(mapping->delete_plan, values, NULL, false, 0) !=
		SPI_OK_DELETE)
		elog(ERROR, "pg_local_cache DEL plan failed");
	ensure_mapping_current(mapping);
	deleted = SPI_processed;
	commit_spi_transaction();
	pglc_note_database_write();
	return pglc_resp_integer((int64) deleted, response_length);
}

static void
maybe_reload_mappings(void)
{
	uint64		generation = pglc_config_generation();

	if (generation == worker_mapping_generation && !worker_mappings_incomplete)
		return;
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
		const char *mapping_query;

		begin_spi_transaction(pglc_statement_timeout_ms);
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

		mapping_query =
			"WITH pglc_mapping AS ("
			"SELECT source_mapping.namespace, source_mapping.relation, "
			"       source_mapping.key_columns, source_mapping.writable "
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
			"   AND rt.tgnargs = 1 + pg_catalog.cardinality(m.key_columns) "
			"   AND rt.tgfoid = 'local_cache._row_invalidate()'::regprocedure "
			"   AND rt.tgargs = "
			"       convert_to(m.namespace, current_setting('server_encoding')) "
			"       || decode('00', 'hex') "
			"       || COALESCE((SELECT pg_catalog.string_agg("
			"              convert_to(k.column_name::text, current_setting('server_encoding')) "
			"              || decode('00', 'hex'), ''::bytea "
			"              ORDER BY k.ordinality) "
			"            FROM pg_catalog.unnest(m.key_columns) WITH ORDINALITY "
			"              AS k(column_name, ordinality)), ''::bytea) "
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
			" ORDER BY m.namespace LIMIT 129";
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

		commit_spi_transaction();
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
		if (IsTransactionState())
			AbortCurrentTransaction();
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
