/* SPDX-License-Identifier: MIT */
#ifndef PGLC_KEY_CODEC_H
#define PGLC_KEY_CODEC_H

#ifdef PGLC_KEY_CODEC_STANDALONE
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
typedef size_t Size;
typedef uint32_t Oid;
typedef int32_t int32;
#else
#include "postgres.h"
#include "fmgr.h"
#endif

#define PGLC_KEY_SCAN_INT2OID 21U
#define PGLC_KEY_SCAN_INT4OID 23U
#define PGLC_KEY_SCAN_TEXTOID 25U
#define PGLC_KEY_SCAN_INT8OID 20U
#define PGLC_KEY_SCAN_VARCHAROID 1043U

/* False means fallback to jsonb_in and the typed input path. */
extern bool pglc_key_scan_json_single(const char *input, Size input_length,
										 const char *column, Size column_length,
										 Oid key_type, int32 typmod,
										 bool database_is_utf8,
										 char *destination,
										 Size destination_capacity,
										 Size *key_length);

#ifndef PGLC_KEY_CODEC_STANDALONE
/*
 * Encode each key component as <decimal byte length>:<bytes>; so composite
 * primary keys cannot alias through separators contained in type output.
 */
extern bool pglc_canonical_key(const Datum *values,
							   const bool *nulls,
							   int key_count,
							   FmgrInfo *output_functions,
							   char *destination,
							   Size destination_capacity,
							   Size *key_len);

/* Avoid fmgr allocation for PostgreSQL's fixed-width integer key types. */
extern bool pglc_canonical_key_typed(const Datum *values,
									 const bool *nulls,
									 int key_count,
									 const Oid *key_types,
									 FmgrInfo *output_functions,
									 char *destination,
									 Size destination_capacity,
										 Size *key_len);

#endif

#endif
