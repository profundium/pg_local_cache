/* SPDX-License-Identifier: MIT */
#ifndef PGLC_ROW_PAYLOAD_H
#define PGLC_ROW_PAYLOAD_H

#include "postgres.h"

#include "access/tupdesc.h"

/*
 * Version 2 is a 36-byte, byte-order-independent header followed by rendered
 * row JSON. The CRC32C covers the complete payload with its own field zeroed:
 *
 *   magic:u32, version:u16, flags:u16, row_type_oid:u32, row_typmod:i32,
 *   natts:u32, descriptor_fingerprint:u64, json_len:u32, checksum_crc32c:u32
 */
#define PGLC_ROW_PAYLOAD_MAGIC 0x50474c43U /* "PGLC" */
#define PGLC_ROW_PAYLOAD_VERSION 2
#define PGLC_ROW_PAYLOAD_HEADER_SIZE 36
#define PGLC_ROW_PAYLOAD_FLAG_HAS_JSON 0x0001U

extern uint64 pglc_row_payload_tupledesc_fingerprint(TupleDesc descriptor);
extern bool pglc_row_payload_encode(Oid row_type_oid,
									int32 row_typmod,
									uint32 natts,
									uint64 descriptor_fingerprint,
									const char *json,
									Size json_len,
									char *destination,
									Size destination_capacity,
									Size *payload_len);
extern bool pglc_row_payload_get_json_checked(
	const char *payload,
	Size payload_len,
	Oid expected_row_type_oid,
	int32 expected_row_typmod,
	uint32 expected_natts,
	uint64 expected_descriptor_fingerprint,
	const char **json,
	Size *json_len);

#endif
