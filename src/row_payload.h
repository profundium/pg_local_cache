/* SPDX-License-Identifier: MIT */
#ifndef PGLC_ROW_PAYLOAD_H
#define PGLC_ROW_PAYLOAD_H

#include "postgres.h"

#include "access/tupdesc.h"
#include "executor/tuptable.h"
#include "utils/memutils.h"

/*
 * Version 1 is a fixed 40-byte, byte-order-independent header followed by a
 * native PostgreSQL composite Datum and, optionally, its row_to_json bytes:
 *
 *   magic:u32, version:u16, flags:u16, row_type_oid:u32, row_typmod:i32,
 *   natts:u32, composite_len:u32, json_len:u32, checksum_crc32c:u32,
 *   descriptor_fingerprint:u64
 *
 * The checksum field is treated as zero while CRC32C is computed.
 * The composite itself is deliberately process-local: it is suitable for the
 * extension's shared memory, not for disk or interchange between PG builds.
 */
#define PGLC_ROW_PAYLOAD_MAGIC 0x50474c43U /* "PGLC" */
#define PGLC_ROW_PAYLOAD_VERSION 1
#define PGLC_ROW_PAYLOAD_HEADER_SIZE 40
#define PGLC_ROW_PAYLOAD_FLAG_HAS_JSON 0x0001U
#define PGLC_ROW_PAYLOAD_KNOWN_FLAGS PGLC_ROW_PAYLOAD_FLAG_HAS_JSON

typedef struct PgLocalCacheRowPayloadView
{
	/* Aligned composite owned by result_context or the input buffer's owner. */
	Datum		composite;
	/* Slice into the input payload; it is not NUL-terminated or owned here. */
	const char *json;
	Size		json_len;
	Oid			row_type_oid;
	int32		row_typmod;
	uint32		natts;
	uint32		checksum_crc32c;
	uint64		descriptor_fingerprint;
	bool		has_json;
} PgLocalCacheRowPayloadView;

extern uint64 pglc_row_payload_tupledesc_fingerprint(TupleDesc descriptor);
extern bool pglc_row_payload_encode(TupleTableSlot *slot,
									TupleDesc relation_descriptor,
									uint16 flags,
									char *destination,
									Size destination_capacity,
									Size *payload_len);
/* expected_descriptor_fingerprint must describe expected_descriptor. */
extern bool pglc_row_payload_decode(const char *payload,
									Size payload_len,
									TupleDesc expected_descriptor,
									uint64 expected_descriptor_fingerprint,
									MemoryContext result_context,
									PgLocalCacheRowPayloadView *view);
/* The input must be MAXALIGNed and remain valid while view is in use. */
extern bool pglc_row_payload_decode_in_place(
	char *payload,
	Size payload_len,
	TupleDesc expected_descriptor,
	uint64 expected_descriptor_fingerprint,
	PgLocalCacheRowPayloadView *view);
extern bool pglc_row_payload_get_json(const PgLocalCacheRowPayloadView *view,
										  const char **json,
										  Size *json_len);
extern bool pglc_row_payload_get_json_checked(
	const char *payload,
	Size payload_len,
	Oid expected_row_type_oid,
	int32 expected_row_typmod,
	uint32 expected_natts,
	uint64 expected_descriptor_fingerprint,
	const char **json,
	Size *json_len);
extern bool pglc_row_payload_render_json(
	const PgLocalCacheRowPayloadView *view,
	MemoryContext result_context,
	char **json,
	Size *json_len);

#endif
