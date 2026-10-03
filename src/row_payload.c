/* SPDX-License-Identifier: MIT */
#include "postgres.h"

#include "access/detoast.h"
#include "access/htup_details.h"
#include "catalog/pg_attribute.h"
#include "executor/tuptable.h"
#include "fmgr.h"
#include "port/pg_crc32c.h"
#include "utils/builtins.h"
#include "utils/fmgroids.h"
#include "utils/memutils.h"

#include "pg_local_cache.h"
#include "row_payload.h"

/* Version 1 binary header offsets. All integers use big-endian byte order. */
#define PGLC_ROW_OFF_MAGIC 0
#define PGLC_ROW_OFF_VERSION 4
#define PGLC_ROW_OFF_FLAGS 6
#define PGLC_ROW_OFF_TYPE_OID 8
#define PGLC_ROW_OFF_TYPMOD 12
#define PGLC_ROW_OFF_NATTS 16
#define PGLC_ROW_OFF_COMPOSITE_LEN 20
#define PGLC_ROW_OFF_JSON_LEN 24
#define PGLC_ROW_OFF_CHECKSUM 28
#define PGLC_ROW_OFF_FINGERPRINT 32

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
pglc_row_payload_get_json_checked(const char *payload, Size payload_len,
								  Oid expected_row_type_oid,
								  int32 expected_row_typmod,
								  uint32 expected_natts,
								  uint64 expected_descriptor_fingerprint,
								  const char **json, Size *json_len)
{
	uint16		flags;
	uint32		composite_len;
	uint32		stored_json_len;
	Size		json_offset;

	*json = NULL;
	*json_len = 0;
	if (payload == NULL || payload_len < PGLC_ROW_PAYLOAD_HEADER_SIZE ||
		payload_len > PGLC_VALUE_MAX || expected_descriptor_fingerprint == 0 ||
		pglc_row_get_u32(payload + PGLC_ROW_OFF_MAGIC) !=
			PGLC_ROW_PAYLOAD_MAGIC ||
		pglc_row_get_u16(payload + PGLC_ROW_OFF_VERSION) !=
			PGLC_ROW_PAYLOAD_VERSION)
		return false;
	flags = pglc_row_get_u16(payload + PGLC_ROW_OFF_FLAGS);
	if ((flags & ~PGLC_ROW_PAYLOAD_KNOWN_FLAGS) != 0 ||
		(flags & PGLC_ROW_PAYLOAD_FLAG_HAS_JSON) == 0 ||
		pglc_row_get_u32(payload + PGLC_ROW_OFF_CHECKSUM) !=
			pglc_row_payload_checksum(payload, payload_len) ||
		(Oid) pglc_row_get_u32(payload + PGLC_ROW_OFF_TYPE_OID) !=
			expected_row_type_oid ||
		(int32) pglc_row_get_u32(payload + PGLC_ROW_OFF_TYPMOD) !=
			expected_row_typmod ||
		pglc_row_get_u32(payload + PGLC_ROW_OFF_NATTS) != expected_natts ||
		pglc_row_get_u64(payload + PGLC_ROW_OFF_FINGERPRINT) !=
			expected_descriptor_fingerprint)
		return false;
	composite_len = pglc_row_get_u32(payload + PGLC_ROW_OFF_COMPOSITE_LEN);
	stored_json_len = pglc_row_get_u32(payload + PGLC_ROW_OFF_JSON_LEN);
	if (composite_len < SizeofHeapTupleHeader ||
		composite_len > payload_len - PGLC_ROW_PAYLOAD_HEADER_SIZE)
		return false;
	json_offset = PGLC_ROW_PAYLOAD_HEADER_SIZE + composite_len;
	if (stored_json_len < 2 || stored_json_len > payload_len - json_offset ||
		json_offset + stored_json_len != payload_len ||
		payload[json_offset] != '{' || payload[payload_len - 1] != '}')
		return false;
	*json = payload + json_offset;
	*json_len = stored_json_len;
	return true;
}

/*
 * Reject obviously oversized external attributes before
 * heap_copy_tuple_as_datum() detoasts them.  This is a conservative lower
 * bound; the exact composite and JSON lengths are checked after construction.
 */
static bool
pglc_row_slot_may_fit(TupleTableSlot *slot, TupleDesc descriptor,
					  Size capacity)
{
	Size		lower_bound = PGLC_ROW_PAYLOAD_HEADER_SIZE +
		SizeofHeapTupleHeader;
	int			attribute_number;

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

		if (attribute_size > capacity ||
			lower_bound > capacity - attribute_size)
			return false;
		lower_bound += attribute_size;
	}
	return true;
}

static void
pglc_row_to_json_bytes(Datum composite, const char **json, Size *json_len)
{
	FmgrInfo	row_to_json_finfo;
	Datum		json_datum;
	text	   *json_text;

	/* Function-manager lookup keeps this on PostgreSQL's supported call path. */
	fmgr_info(F_ROW_TO_JSON_RECORD, &row_to_json_finfo);
	json_datum = FunctionCall1(&row_to_json_finfo, composite);
	json_text = DatumGetTextPP(json_datum);
	*json = VARDATA_ANY(json_text);
	*json_len = VARSIZE_ANY_EXHDR(json_text);
}

static bool
pglc_row_payload_encode_internal(TupleTableSlot *slot,
								 TupleDesc descriptor, uint16 flags,
								 char *destination, Size capacity,
								 Size *payload_len)
{
	HeapTuple	tuple;
	Datum		composite_datum;
	HeapTupleHeader composite;
	const char *json = NULL;
	Size		json_len = 0;
	Size		composite_len;
	Size		total_len;
	uint64		fingerprint;
	uint32		checksum;

	if (TupIsNull(slot) || slot->tts_tupleDescriptor == NULL ||
		slot->tts_tupleDescriptor->natts != descriptor->natts ||
		pglc_row_payload_tupledesc_fingerprint(slot->tts_tupleDescriptor) !=
		pglc_row_payload_tupledesc_fingerprint(descriptor) ||
		!pglc_row_slot_may_fit(slot, descriptor, capacity))
		return false;

	tuple = ExecCopySlotHeapTuple(slot);
	composite_datum = heap_copy_tuple_as_datum(tuple, descriptor);
	heap_freetuple(tuple);
	composite = DatumGetHeapTupleHeader(composite_datum);
	composite_len = HeapTupleHeaderGetDatumLength(composite);
	if (!VARATT_IS_4B_U(composite) ||
		composite_len < SizeofHeapTupleHeader ||
		(composite->t_infomask & HEAP_HASEXTERNAL) != 0 ||
		HeapTupleHeaderGetTypeId(composite) != descriptor->tdtypeid ||
		HeapTupleHeaderGetTypMod(composite) != descriptor->tdtypmod ||
		HeapTupleHeaderGetNatts(composite) != descriptor->natts)
		return false;

	if ((flags & PGLC_ROW_PAYLOAD_FLAG_HAS_JSON) != 0)
		pglc_row_to_json_bytes(composite_datum, &json, &json_len);
	if (json_len > 0 && (json_len < 2 || json[0] != '{' ||
		json[json_len - 1] != '}'))
		return false;

	if (composite_len > capacity - PGLC_ROW_PAYLOAD_HEADER_SIZE)
		return false;
	total_len = PGLC_ROW_PAYLOAD_HEADER_SIZE + composite_len;
	if (json_len > capacity - total_len)
		return false;
	total_len += json_len;
	if (total_len > PGLC_VALUE_MAX)
		return false;

	fingerprint = pglc_row_payload_tupledesc_fingerprint(descriptor);
	MemSet(destination, 0, PGLC_ROW_PAYLOAD_HEADER_SIZE);
	pglc_row_put_u32(destination + PGLC_ROW_OFF_MAGIC,
					 PGLC_ROW_PAYLOAD_MAGIC);
	pglc_row_put_u16(destination + PGLC_ROW_OFF_VERSION,
					 PGLC_ROW_PAYLOAD_VERSION);
	pglc_row_put_u16(destination + PGLC_ROW_OFF_FLAGS, flags);
	pglc_row_put_u32(destination + PGLC_ROW_OFF_TYPE_OID,
					 descriptor->tdtypeid);
	pglc_row_put_u32(destination + PGLC_ROW_OFF_TYPMOD,
					 (uint32) descriptor->tdtypmod);
	pglc_row_put_u32(destination + PGLC_ROW_OFF_NATTS,
					 (uint32) descriptor->natts);
	pglc_row_put_u32(destination + PGLC_ROW_OFF_COMPOSITE_LEN,
					 (uint32) composite_len);
	pglc_row_put_u32(destination + PGLC_ROW_OFF_JSON_LEN, (uint32) json_len);
	pglc_row_put_u64(destination + PGLC_ROW_OFF_FINGERPRINT, fingerprint);
	memcpy(destination + PGLC_ROW_PAYLOAD_HEADER_SIZE, composite,
		   composite_len);
	if (json_len > 0)
		memcpy(destination + PGLC_ROW_PAYLOAD_HEADER_SIZE + composite_len,
			   json, json_len);
	checksum = pglc_row_payload_checksum(destination, total_len);
	pglc_row_put_u32(destination + PGLC_ROW_OFF_CHECKSUM, checksum);
	*payload_len = total_len;
	return true;
}

bool
pglc_row_payload_encode(TupleTableSlot *slot, TupleDesc relation_descriptor,
						uint16 flags, char *destination,
						Size destination_capacity, Size *payload_len)
{
	MemoryContext old_context = CurrentMemoryContext;
	MemoryContext temporary_context;
	volatile bool encoded = false;
	Size		capacity = Min(destination_capacity, (Size) PGLC_VALUE_MAX);

	if (payload_len != NULL)
		*payload_len = 0;
	if (slot == NULL || !pglc_row_descriptor_supported(relation_descriptor) ||
		(flags & ~PGLC_ROW_PAYLOAD_KNOWN_FLAGS) != 0 ||
		destination == NULL || payload_len == NULL ||
		capacity < PGLC_ROW_PAYLOAD_HEADER_SIZE + SizeofHeapTupleHeader)
		return false;

	temporary_context = AllocSetContextCreate(old_context,
		"pg_local_cache row payload encode",
		ALLOCSET_SMALL_SIZES);
	PG_TRY();
	{
		MemoryContextSwitchTo(temporary_context);
		encoded = pglc_row_payload_encode_internal(slot,
			relation_descriptor, flags, destination, capacity, payload_len);
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
	if (!encoded)
		*payload_len = 0;
	return encoded;
}

static bool
pglc_row_payload_decode_internal(const char *payload, Size payload_len,
								 TupleDesc expected_descriptor,
								 uint64 expected_descriptor_fingerprint,
								 MemoryContext result_context,
								 bool use_input_buffer,
								 PgLocalCacheRowPayloadView *view)
{
	uint16		version;
	uint16		flags;
	Oid			row_type_oid;
	int32		row_typmod;
	uint32		natts;
	uint32		composite_len;
	uint32		json_len;
	uint32		stored_checksum;
	uint64		fingerprint;
	Size		expected_total;
	HeapTupleHeader composite;
	Size		expected_header_offset;
	MemoryContext old_context;

	if (view != NULL)
		MemSet(view, 0, sizeof(*view));
	if (payload == NULL || view == NULL ||
		(!use_input_buffer && result_context == NULL) ||
		!pglc_row_descriptor_supported(expected_descriptor) ||
		expected_descriptor_fingerprint == 0 ||
		payload_len < PGLC_ROW_PAYLOAD_HEADER_SIZE ||
		payload_len > PGLC_VALUE_MAX)
		return false;
	if (pglc_row_get_u32(payload + PGLC_ROW_OFF_MAGIC) !=
		PGLC_ROW_PAYLOAD_MAGIC)
		return false;
	version = pglc_row_get_u16(payload + PGLC_ROW_OFF_VERSION);
	flags = pglc_row_get_u16(payload + PGLC_ROW_OFF_FLAGS);
	if (version != PGLC_ROW_PAYLOAD_VERSION ||
		(flags & ~PGLC_ROW_PAYLOAD_KNOWN_FLAGS) != 0)
		return false;

	stored_checksum = pglc_row_get_u32(payload + PGLC_ROW_OFF_CHECKSUM);
	if (stored_checksum != pglc_row_payload_checksum(payload, payload_len))
		return false;

	row_type_oid = (Oid) pglc_row_get_u32(payload + PGLC_ROW_OFF_TYPE_OID);
	row_typmod = (int32) pglc_row_get_u32(payload + PGLC_ROW_OFF_TYPMOD);
	natts = pglc_row_get_u32(payload + PGLC_ROW_OFF_NATTS);
	composite_len = pglc_row_get_u32(payload + PGLC_ROW_OFF_COMPOSITE_LEN);
	json_len = pglc_row_get_u32(payload + PGLC_ROW_OFF_JSON_LEN);
	fingerprint = pglc_row_get_u64(payload + PGLC_ROW_OFF_FINGERPRINT);

	if (((flags & PGLC_ROW_PAYLOAD_FLAG_HAS_JSON) == 0 && json_len != 0) ||
		((flags & PGLC_ROW_PAYLOAD_FLAG_HAS_JSON) != 0 && json_len < 2) ||
		composite_len < SizeofHeapTupleHeader ||
		composite_len > payload_len - PGLC_ROW_PAYLOAD_HEADER_SIZE)
		return false;
	expected_total = PGLC_ROW_PAYLOAD_HEADER_SIZE + composite_len;
	if (json_len > payload_len - expected_total ||
		expected_total + json_len != payload_len)
		return false;
	if (row_type_oid != expected_descriptor->tdtypeid ||
		row_typmod != expected_descriptor->tdtypmod ||
		natts != (uint32) expected_descriptor->natts ||
		fingerprint != expected_descriptor_fingerprint)
		return false;
	if (json_len > 0 &&
		(payload[expected_total] != '{' || payload[payload_len - 1] != '}'))
		return false;

	if (use_input_buffer)
	{
		uintptr_t	composite_address = (uintptr_t)
			(payload + PGLC_ROW_PAYLOAD_HEADER_SIZE);

		if (MAXALIGN(composite_address) != composite_address)
			return false;
		composite = (HeapTupleHeader) composite_address;
	}
	else
	{
		old_context = MemoryContextSwitchTo(result_context);
		composite = (HeapTupleHeader) palloc(composite_len);
		MemoryContextSwitchTo(old_context);
		memcpy(composite, payload + PGLC_ROW_PAYLOAD_HEADER_SIZE,
			   composite_len);
	}

	/*
	 * Shared-memory bytes are never cast in place.  The caller can either ask
	 * for an aligned copy or provide an aligned backend-owned buffer.  Tuple
	 * bytes are interpreted only after CRC, length and descriptor validation.
	 * heap_copy_tuple_as_datum() flattened external TOAST fields during encode,
	 * so a positive cache entry owns every byte it references.
	 */
	if (!VARATT_IS_4B_U(composite) ||
		HeapTupleHeaderGetDatumLength(composite) != composite_len ||
		HeapTupleHeaderGetTypeId(composite) != row_type_oid ||
		HeapTupleHeaderGetTypMod(composite) != row_typmod ||
		HeapTupleHeaderGetNatts(composite) != natts ||
		(composite->t_infomask & HEAP_HASEXTERNAL) != 0)
	{
		if (!use_input_buffer)
			pfree(composite);
		return false;
	}
	expected_header_offset = SizeofHeapTupleHeader;
	if ((composite->t_infomask & HEAP_HASNULL) != 0)
		expected_header_offset += BITMAPLEN(natts);
	expected_header_offset = MAXALIGN(expected_header_offset);
	if (composite->t_hoff != expected_header_offset ||
		expected_header_offset > composite_len)
	{
		if (!use_input_buffer)
			pfree(composite);
		return false;
	}

	view->composite = PointerGetDatum(composite);
	view->has_json = (flags & PGLC_ROW_PAYLOAD_FLAG_HAS_JSON) != 0;
	view->json = view->has_json ? payload + expected_total : NULL;
	view->json_len = json_len;
	view->row_type_oid = row_type_oid;
	view->row_typmod = row_typmod;
	view->natts = natts;
	view->checksum_crc32c = stored_checksum;
	view->descriptor_fingerprint = fingerprint;
	return true;
}

bool
pglc_row_payload_decode(const char *payload, Size payload_len,
							TupleDesc expected_descriptor,
							uint64 expected_descriptor_fingerprint,
							MemoryContext result_context,
							PgLocalCacheRowPayloadView *view)
{
	return pglc_row_payload_decode_internal(payload, payload_len,
		expected_descriptor, expected_descriptor_fingerprint, result_context,
		false, view);
}

bool
pglc_row_payload_decode_in_place(char *payload, Size payload_len,
								 TupleDesc expected_descriptor,
								 uint64 expected_descriptor_fingerprint,
								 PgLocalCacheRowPayloadView *view)
{
	return pglc_row_payload_decode_internal(payload, payload_len,
		expected_descriptor, expected_descriptor_fingerprint, NULL, true, view);
}

bool
pglc_row_payload_get_json(const PgLocalCacheRowPayloadView *view,
						  const char **json, Size *json_len)
{
	if (json != NULL)
		*json = NULL;
	if (json_len != NULL)
		*json_len = 0;
	if (view == NULL || json == NULL || json_len == NULL ||
		!view->has_json || view->json == NULL)
		return false;
	*json = view->json;
	*json_len = view->json_len;
	return true;
}

bool
pglc_row_payload_render_json(const PgLocalCacheRowPayloadView *view,
							 MemoryContext result_context,
							 char **json, Size *json_len)
{
	MemoryContext old_context = CurrentMemoryContext;
	MemoryContext temporary_context;
	const char *rendered = NULL;
	Size		rendered_len = 0;
	char	   *copy = NULL;

	if (json != NULL)
		*json = NULL;
	if (json_len != NULL)
		*json_len = 0;
	if (view == NULL || !PointerIsValid(DatumGetPointer(view->composite)) ||
		result_context == NULL || json == NULL || json_len == NULL)
		return false;

	temporary_context = AllocSetContextCreate(old_context,
		"pg_local_cache row json render",
		ALLOCSET_SMALL_SIZES);
	PG_TRY();
	{
		MemoryContextSwitchTo(temporary_context);
		pglc_row_to_json_bytes(view->composite, &rendered, &rendered_len);
		if (rendered_len <= PGLC_RESPONSE_VALUE_MAX)
		{
			MemoryContextSwitchTo(result_context);
			copy = palloc(rendered_len + 1);
			if (rendered_len > 0)
				memcpy(copy, rendered, rendered_len);
			copy[rendered_len] = '\0';
			*json = copy;
			*json_len = rendered_len;
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
	return *json != NULL;
}
