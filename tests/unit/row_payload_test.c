/* SPDX-License-Identifier: MIT */
#include "postgres.h"

#include <stdio.h>
#include <string.h>

#undef fprintf

#include "port/pg_crc32c.h"

#include "pg_local_cache.h"
#include "row_payload.h"

#define TEST_ROW_TYPE_OID 4242
#define TEST_ROW_TYPMOD (-1)
#define TEST_ROW_NATTS 2
#define TEST_DESCRIPTOR_FINGERPRINT UINT64CONST(0x97a31f826bc450de)

static pg_crc32c
test_crc32c(pg_crc32c crc, const void *data, size_t length)
{
	const unsigned char *bytes = (const unsigned char *) data;
	size_t		index;

	for (index = 0; index < length; index++)
	{
		int			bit;

		crc ^= bytes[index];
		for (bit = 0; bit < 8; bit++)
			crc = (crc >> 1) ^ (0x82f63b78U & (uint32) -(int32) (crc & 1));
	}
	return crc;
}

/* PostgreSQL's server headers select an optimized implementation at build time. */
pg_crc32c
pg_comp_crc32c_sb8(pg_crc32c crc, const void *data, size_t length)
{
	return test_crc32c(crc, data, length);
}

pg_crc32c
pg_comp_crc32c_armv8(pg_crc32c crc, const void *data, size_t length)
{
	return test_crc32c(crc, data, length);
}

pg_crc32c
pg_comp_crc32c_sse42(pg_crc32c crc, const void *data, size_t length)
{
	return test_crc32c(crc, data, length);
}

pg_crc32c
pg_comp_crc32c_avx512(pg_crc32c crc, const void *data, size_t length)
{
	return test_crc32c(crc, data, length);
}

pg_crc32c (*pg_comp_crc32c)(pg_crc32c crc, const void *data, size_t length) =
	test_crc32c;

static bool
decode_payload(const char *payload, Size payload_len,
			   const char **json, Size *json_len)
{
	return pglc_row_payload_get_json_checked(payload, payload_len,
										 TEST_ROW_TYPE_OID,
										 TEST_ROW_TYPMOD,
										 TEST_ROW_NATTS,
										 TEST_DESCRIPTOR_FINGERPRINT,
										 json, json_len);
}

int
main(void)
{
	static const char json[] = "{\"id\":17,\"name\":\"cache\"}";
	static const struct
	{
		Size		offset;
		const char *field;
	} header_fields[] = {
		{0, "magic"},
		{5, "version"},
		{7, "flags"},
		{11, "row type OID"},
		{15, "row typmod"},
		{19, "natts"},
		{27, "descriptor fingerprint"},
		{31, "JSON length"},
		{35, "CRC32C"}
	};
	char		payload[PGLC_VALUE_MAX];
	char		oversized_json[PGLC_VALUE_MAX];
	const char *decoded_json;
	Size		decoded_json_len;
	Size		max_json_len = PGLC_VALUE_MAX - PGLC_ROW_PAYLOAD_HEADER_SIZE;
	Size		oversized_json_len = max_json_len + 1;
	Size		payload_len = 0;
	Size		index;

	if (!pglc_row_payload_encode(TEST_ROW_TYPE_OID, TEST_ROW_TYPMOD,
								 TEST_ROW_NATTS,
								 TEST_DESCRIPTOR_FINGERPRINT,
								 json, sizeof(json) - 1,
								 payload, sizeof(payload), &payload_len))
	{
		fprintf(stderr, "payload encode failed\n");
		return 1;
	}
	if (!decode_payload(payload, payload_len, &decoded_json, &decoded_json_len) ||
		decoded_json_len != sizeof(json) - 1 ||
		memcmp(decoded_json, json, sizeof(json) - 1) != 0)
	{
		fprintf(stderr, "payload round-trip failed\n");
		return 1;
	}

	for (index = 0; index < lengthof(header_fields); index++)
	{
		char		saved = payload[header_fields[index].offset];

		payload[header_fields[index].offset] ^= 1;
		if (decode_payload(payload, payload_len, &decoded_json,
						   &decoded_json_len))
		{
			fprintf(stderr, "corrupt %s accepted\n", header_fields[index].field);
			return 1;
		}
		payload[header_fields[index].offset] = saved;
	}

	payload[PGLC_ROW_PAYLOAD_HEADER_SIZE + 2] ^= 1;
	if (decode_payload(payload, payload_len, &decoded_json, &decoded_json_len))
	{
		fprintf(stderr, "corrupt JSON accepted\n");
		return 1;
	}
	payload[PGLC_ROW_PAYLOAD_HEADER_SIZE + 2] ^= 1;

	if (pglc_row_payload_get_json_checked(payload, payload_len,
										 TEST_ROW_TYPE_OID, TEST_ROW_TYPMOD,
										 TEST_ROW_NATTS,
										 TEST_DESCRIPTOR_FINGERPRINT + 1,
										 &decoded_json, &decoded_json_len))
	{
		fprintf(stderr, "descriptor mismatch accepted\n");
		return 1;
	}

	/* Valid JSON exactly fills the maximum payload after its header. */
	memset(oversized_json, 'x', max_json_len);
	memcpy(oversized_json, "{\"x\":\"", 6);
	oversized_json[max_json_len - 2] = '"';
	oversized_json[max_json_len - 1] = '}';
	if (!pglc_row_payload_encode(TEST_ROW_TYPE_OID, TEST_ROW_TYPMOD,
								 TEST_ROW_NATTS,
								 TEST_DESCRIPTOR_FINGERPRINT,
								 oversized_json, max_json_len,
								 payload, sizeof(payload), &payload_len) ||
		payload_len != sizeof(payload) ||
		!decode_payload(payload, payload_len, &decoded_json,
					 &decoded_json_len) ||
		decoded_json_len != max_json_len ||
		memcmp(decoded_json, oversized_json, max_json_len) != 0)
	{
		fprintf(stderr, "maximum-length JSON rejected or corrupted\n");
		return 1;
	}

	/* Valid JSON one byte beyond the output limit must fail on capacity. */
	memset(oversized_json, 'x', oversized_json_len);
	memcpy(oversized_json, "{\"x\":\"", 6);
	oversized_json[oversized_json_len - 2] = '"';
	oversized_json[oversized_json_len - 1] = '}';
	payload_len = 0;
	if (pglc_row_payload_encode(TEST_ROW_TYPE_OID, TEST_ROW_TYPMOD,
								 TEST_ROW_NATTS,
								 TEST_DESCRIPTOR_FINGERPRINT,
								 oversized_json,
								 oversized_json_len,
								 payload, sizeof(payload), &payload_len) ||
		payload_len != 0)
	{
		fprintf(stderr, "oversized JSON accepted\n");
		return 1;
	}

	puts("row_payload_test: ok");
	return 0;
}
