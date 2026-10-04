/* SPDX-License-Identifier: MIT */
#include "postgres.h"

#include "catalog/pg_attribute.h"
#include "port/pg_crc32c.h"

#include "pg_local_cache.h"
#include "row_payload.h"

/* Version 2 binary header offsets. All integers use big-endian byte order. */
#define PGLC_ROW_OFF_MAGIC 0
#define PGLC_ROW_OFF_VERSION 4
#define PGLC_ROW_OFF_FLAGS 6
#define PGLC_ROW_OFF_TYPE_OID 8
#define PGLC_ROW_OFF_TYPMOD 12
#define PGLC_ROW_OFF_NATTS 16
#define PGLC_ROW_OFF_FINGERPRINT 20
#define PGLC_ROW_OFF_JSON_LEN 28
#define PGLC_ROW_OFF_CHECKSUM 32

#define PGLC_FNV1A_OFFSET UINT64CONST(14695981039346656037)
#define PGLC_FNV1A_PRIME UINT64CONST(1099511628211)

static void
pglc_row_put_u16(char *destination, uint16 value)
{
	destination[0] = (char) (value >> 8);
	destination[1] = (char) value;
}

static void
pglc_row_put_u32(char *destination, uint32 value)
{
	destination[0] = (char) (value >> 24);
	destination[1] = (char) (value >> 16);
	destination[2] = (char) (value >> 8);
	destination[3] = (char) value;
}

static void
pglc_row_put_u64(char *destination, uint64 value)
{
	int			byte;

	for (byte = 7; byte >= 0; byte--)
	{
		destination[byte] = (char) value;
		value >>= 8;
	}
}

static uint16
pglc_row_get_u16(const char *source)
{
	const unsigned char *bytes = (const unsigned char *) source;

	return ((uint16) bytes[0] << 8) | (uint16) bytes[1];
}

static uint32
pglc_row_get_u32(const char *source)
{
	const unsigned char *bytes = (const unsigned char *) source;

	return ((uint32) bytes[0] << 24) |
		((uint32) bytes[1] << 16) |
		((uint32) bytes[2] << 8) |
		(uint32) bytes[3];
}

static uint64
pglc_row_get_u64(const char *source)
{
	const unsigned char *bytes = (const unsigned char *) source;
	uint64		value = 0;
	int			byte;

	for (byte = 0; byte < 8; byte++)
		value = (value << 8) | bytes[byte];
	return value;
}

static void
pglc_fingerprint_byte(uint64 *hash, unsigned char value)
{
	*hash ^= value;
	*hash *= PGLC_FNV1A_PRIME;
}

static void
pglc_fingerprint_uint(uint64 *hash, uint64 value, int width)
{
	int			byte;

	for (byte = 0; byte < width; byte++)
	{
		pglc_fingerprint_byte(hash, (unsigned char) value);
		value >>= 8;
	}
}

static void
pglc_fingerprint_name(uint64 *hash, const NameData *name)
{
	const char *bytes = NameStr(*name);
	Size		length = strlen(bytes);
	Size		position;

	pglc_fingerprint_uint(hash, length, 2);
	for (position = 0; position < length; position++)
		pglc_fingerprint_byte(hash, (unsigned char) bytes[position]);
}

static bool
pglc_row_descriptor_supported(TupleDesc descriptor)
{
	int			attribute_number;

	if (descriptor == NULL || !OidIsValid(descriptor->tdtypeid) ||
		descriptor->natts < 0 ||
		descriptor->natts > MaxTupleAttributeNumber)
		return false;
	for (attribute_number = 0; attribute_number < descriptor->natts;
		 attribute_number++)
	{
		Form_pg_attribute attribute =
			TupleDescAttr(descriptor, attribute_number);

		if (attribute->attnum != attribute_number + 1)
			return false;
	}
	return true;
}

uint64
pglc_row_payload_tupledesc_fingerprint(TupleDesc descriptor)
{
	uint64		hash = PGLC_FNV1A_OFFSET;
	int			attribute_number;

	if (!pglc_row_descriptor_supported(descriptor))
		return 0;

	pglc_fingerprint_uint(&hash, descriptor->tdtypeid, sizeof(Oid));
	pglc_fingerprint_uint(&hash, (uint32) descriptor->tdtypmod,
						 sizeof(int32));
	pglc_fingerprint_uint(&hash, descriptor->natts, sizeof(uint32));
	for (attribute_number = 0; attribute_number < descriptor->natts;
		 attribute_number++)
	{
		Form_pg_attribute attribute =
			TupleDescAttr(descriptor, attribute_number);

		pglc_fingerprint_uint(&hash, (uint16) attribute->attnum,
						 sizeof(int16));
		pglc_fingerprint_name(&hash, &attribute->attname);
		pglc_fingerprint_uint(&hash, attribute->atttypid, sizeof(Oid));
		pglc_fingerprint_uint(&hash, (uint32) attribute->atttypmod,
						 sizeof(int32));
		pglc_fingerprint_uint(&hash, attribute->attcollation, sizeof(Oid));
		pglc_fingerprint_uint(&hash, (uint16) attribute->attlen,
						 sizeof(int16));
		pglc_fingerprint_uint(&hash, (uint16) attribute->attndims,
						 sizeof(int16));
		pglc_fingerprint_byte(&hash, attribute->attbyval ? 1 : 0);
		pglc_fingerprint_byte(&hash, (unsigned char) attribute->attalign);
		pglc_fingerprint_byte(&hash, (unsigned char) attribute->attstorage);
		pglc_fingerprint_byte(&hash, (unsigned char) attribute->attcompression);
		pglc_fingerprint_byte(&hash, attribute->attisdropped ? 1 : 0);
		pglc_fingerprint_byte(&hash, attribute->atthasmissing ? 1 : 0);
		pglc_fingerprint_byte(&hash, (unsigned char) attribute->attgenerated);
		pglc_fingerprint_byte(&hash, (unsigned char) attribute->attidentity);
	}
	return hash;
}

static uint32
pglc_row_payload_checksum(const char *payload, Size payload_len)
{
	static const char zero_checksum[sizeof(uint32)] = {0, 0, 0, 0};
	pg_crc32c	crc;

	Assert(payload_len >= PGLC_ROW_PAYLOAD_HEADER_SIZE);
	INIT_CRC32C(crc);
	COMP_CRC32C(crc, payload, PGLC_ROW_OFF_CHECKSUM);
	COMP_CRC32C(crc, zero_checksum, sizeof(zero_checksum));
	COMP_CRC32C(crc, payload + PGLC_ROW_OFF_CHECKSUM + sizeof(uint32),
				payload_len - PGLC_ROW_OFF_CHECKSUM - sizeof(uint32));
	FIN_CRC32C(crc);
	return (uint32) crc;
}

bool
pglc_row_payload_encode(Oid row_type_oid, int32 row_typmod, uint32 natts,
						uint64 descriptor_fingerprint, const char *json,
						Size json_len, char *destination,
						Size destination_capacity, Size *payload_len)
{
	Size		capacity = Min(destination_capacity, (Size) PGLC_VALUE_MAX);
	Size		total_len;
	uint32		checksum;

	if (payload_len != NULL)
		*payload_len = 0;
	if (!OidIsValid(row_type_oid) || natts > MaxTupleAttributeNumber ||
		descriptor_fingerprint == 0 || json == NULL || destination == NULL ||
		payload_len == NULL || capacity < PGLC_ROW_PAYLOAD_HEADER_SIZE ||
		json_len < 2 ||
		json_len > capacity - PGLC_ROW_PAYLOAD_HEADER_SIZE)
		return false;
	if (json[0] != '{' || json[json_len - 1] != '}')
		return false;

	total_len = PGLC_ROW_PAYLOAD_HEADER_SIZE + json_len;
	MemSet(destination, 0, PGLC_ROW_PAYLOAD_HEADER_SIZE);
	pglc_row_put_u32(destination + PGLC_ROW_OFF_MAGIC,
					 PGLC_ROW_PAYLOAD_MAGIC);
	pglc_row_put_u16(destination + PGLC_ROW_OFF_VERSION,
					 PGLC_ROW_PAYLOAD_VERSION);
	pglc_row_put_u16(destination + PGLC_ROW_OFF_FLAGS,
					 PGLC_ROW_PAYLOAD_FLAG_HAS_JSON);
	pglc_row_put_u32(destination + PGLC_ROW_OFF_TYPE_OID, row_type_oid);
	pglc_row_put_u32(destination + PGLC_ROW_OFF_TYPMOD, (uint32) row_typmod);
	pglc_row_put_u32(destination + PGLC_ROW_OFF_NATTS, natts);
	pglc_row_put_u64(destination + PGLC_ROW_OFF_FINGERPRINT,
					 descriptor_fingerprint);
	pglc_row_put_u32(destination + PGLC_ROW_OFF_JSON_LEN, (uint32) json_len);
	memcpy(destination + PGLC_ROW_PAYLOAD_HEADER_SIZE, json, json_len);
	checksum = pglc_row_payload_checksum(destination, total_len);
	pglc_row_put_u32(destination + PGLC_ROW_OFF_CHECKSUM, checksum);
	*payload_len = total_len;
	return true;
}

bool
pglc_row_payload_get_json_checked(const char *payload, Size payload_len,
								  Oid expected_row_type_oid,
								  int32 expected_row_typmod,
								  uint32 expected_natts,
								  uint64 expected_descriptor_fingerprint,
								  const char **json, Size *json_len)
{
	uint16		flags;
	uint32		stored_json_len;

	if (json != NULL)
		*json = NULL;
	if (json_len != NULL)
		*json_len = 0;
	if (json == NULL || json_len == NULL || payload == NULL ||
		payload_len < PGLC_ROW_PAYLOAD_HEADER_SIZE ||
		payload_len > PGLC_VALUE_MAX || expected_descriptor_fingerprint == 0 ||
		pglc_row_get_u32(payload + PGLC_ROW_OFF_MAGIC) !=
		PGLC_ROW_PAYLOAD_MAGIC ||
		pglc_row_get_u16(payload + PGLC_ROW_OFF_VERSION) !=
		PGLC_ROW_PAYLOAD_VERSION)
		return false;

	flags = pglc_row_get_u16(payload + PGLC_ROW_OFF_FLAGS);
	if (flags != PGLC_ROW_PAYLOAD_FLAG_HAS_JSON ||
		pglc_row_get_u32(payload + PGLC_ROW_OFF_TYPE_OID) !=
		expected_row_type_oid ||
		(int32) pglc_row_get_u32(payload + PGLC_ROW_OFF_TYPMOD) !=
		expected_row_typmod ||
		pglc_row_get_u32(payload + PGLC_ROW_OFF_NATTS) != expected_natts ||
		pglc_row_get_u64(payload + PGLC_ROW_OFF_FINGERPRINT) !=
		expected_descriptor_fingerprint)
		return false;

	stored_json_len = pglc_row_get_u32(payload + PGLC_ROW_OFF_JSON_LEN);
	if (stored_json_len < 2 ||
		stored_json_len != payload_len - PGLC_ROW_PAYLOAD_HEADER_SIZE ||
		payload[PGLC_ROW_PAYLOAD_HEADER_SIZE] != '{' ||
		payload[payload_len - 1] != '}' ||
		pglc_row_get_u32(payload + PGLC_ROW_OFF_CHECKSUM) !=
		pglc_row_payload_checksum(payload, payload_len))
		return false;

	*json = payload + PGLC_ROW_PAYLOAD_HEADER_SIZE;
	*json_len = stored_json_len;
	return true;
}
