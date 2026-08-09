#ifndef PG_LOCAL_CACHE_RESP_LIMITS_H
#define PG_LOCAL_CACHE_RESP_LIMITS_H

/*
 * Wire limits shared by the PostgreSQL build and the standalone source tests.
 * Keep this header free of PostgreSQL dependencies.
 */
#define PGLC_REQUEST_MAX 65536
#define PGLC_MGET_MAX_KEYS 1024
/* Parse one key beyond the MGET limit so the command returns a stable error. */
#define PGLC_RESP_MAX_ARGS (PGLC_MGET_MAX_KEYS + 2)

#endif
