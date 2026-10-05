/* SPDX-License-Identifier: MIT */
#ifndef PG_LOCAL_CACHE_RESP_H
#define PG_LOCAL_CACHE_RESP_H

#include "resp_limits.h"

#ifdef PGLC_RESP_STANDALONE
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/*
 * Keep the wire codec independently testable without linking a PostgreSQL
 * backend.  The production build takes these definitions from postgres.h and
 * pg_local_cache.h instead.
 */
typedef size_t Size;
typedef int64_t int64;
typedef uint64_t uint64;
#else
#include "postgres.h"

#include "pg_local_cache.h"
#ifdef PGLC_TEST_HOOKS
extern void pglc_test_record_palloc(void);
#endif
#endif

typedef struct PgLocalCacheRespArg
{
	const char *data;
	Size		len;
} PgLocalCacheRespArg;

/*
 * Returns 1 for a complete request, 0 for incomplete input, and -1 for a
 * protocol error. On success, consumed is the number of bytes to remove.
 */
extern int pglc_resp_parse(const char *buffer, Size length,
						  PgLocalCacheRespArg *args, int *argc,
						  Size *consumed, const char **error);

extern bool pglc_resp_arg_equals(const PgLocalCacheRespArg *arg, const char *literal);
extern char *pglc_resp_simple(const char *message, Size *length);
extern char *pglc_resp_error(const char *message, Size *length);
extern char *pglc_resp_integer(int64 value, Size *length);
extern char *pglc_resp_bulk(const char *value, Size value_len, Size *length);
extern char *pglc_resp_null(Size *length);
extern bool pglc_resp_write_array(char *destination, Size capacity,
								  Size *length, Size count, Size response_max);
extern bool pglc_resp_write_bulk(char *destination, Size capacity,
								 Size *length, const char *value,
								 Size value_len, Size response_max);
extern bool pglc_resp_write_null(char *destination, Size capacity,
								 Size *length, Size response_max);

#endif
