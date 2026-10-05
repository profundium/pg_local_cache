/* SPDX-License-Identifier: MIT */
#include "cache_index.h"

#include <string.h>

typedef struct TestEntry
{
	uint64_t hash;
	uint32_t key;
} TestEntry;

typedef struct TestContext
{
	TestEntry entries[80];
	uint32_t key;
} TestContext;

static uint64_t
test_entry_hash(uint32_t entry_id, void *opaque)
{
	TestContext *context = (TestContext *) opaque;

	return context->entries[entry_id - 1].hash;
}

static bool
test_entry_matches(uint32_t entry_id, void *opaque)
{
	TestContext *context = (TestContext *) opaque;

	return context->entries[entry_id - 1].key == context->key;
}

static int
test_bucket_zero_tombstone_reuse_after_wraparound(void)
{
	uint32_t	buckets[128];
	uint32_t	scratch[128];
	PglcCacheIndex index;
	TestContext context = {0};
	uint32_t	entry_id;

	pglc_index_init(&index, buckets, scratch, 8);
	for (entry_id = 1; entry_id <= 4; entry_id++)
	{
		context.entries[entry_id - 1].hash = 6;
		context.entries[entry_id - 1].key = entry_id;
	}
	for (entry_id = 1; entry_id <= 3; entry_id++)
		if (pglc_index_insert(&index, 6, entry_id, test_entry_hash,
							  test_entry_matches, &context) != PGLC_INDEX_INSERTED)
			return 1;
	if (buckets[6] != 1 || buckets[7] != 2 || buckets[0] != 3)
		return 2;
	context.key = 3;
	if (pglc_index_remove(&index, 6, test_entry_hash,
						  test_entry_matches, &context) != 3 ||
		index.tombstones != 1)
		return 3;
	if (pglc_index_insert(&index, 6, 4, test_entry_hash,
					  test_entry_matches, &context) != PGLC_INDEX_INSERTED ||
		buckets[0] != 4 || index.tombstones != 0)
		return 4;
	context.key = 4;
	if (pglc_index_find(&index, 6, test_entry_hash,
						 test_entry_matches, &context) != 4)
		return 5;

	pglc_index_init(&index, buckets, scratch, 128);
	for (entry_id = 1; entry_id <= 64; entry_id++)
	{
		context.entries[entry_id - 1].hash = 0;
		context.entries[entry_id - 1].key = entry_id;
		context.key = entry_id;
		if (pglc_index_insert(&index, 0, entry_id, test_entry_hash,
							  test_entry_matches, &context) != PGLC_INDEX_INSERTED)
			return 6;
	}
	context.key = 1;
	if (pglc_index_remove(&index, 0, test_entry_hash,
					  test_entry_matches, &context) != 1 ||
		buckets[0] != PGLC_INDEX_TOMBSTONE)
		return 7;
	context.entries[64].hash = 0;
	context.entries[64].key = 65;
	if (pglc_index_insert(&index, 0, 65, test_entry_hash,
					  test_entry_matches, &context) != PGLC_INDEX_INSERTED ||
		buckets[0] != 65 || index.tombstones != 0)
		return 8;
	context.key = 65;
	if (pglc_index_find(&index, 0, test_entry_hash,
					 test_entry_matches, &context) != 65)
		return 9;
	return 0;
}

static int
test_failed_rebuild_preserves_index(void)
{
	uint32_t	buckets[64];
	uint32_t	scratch[64];
	uint32_t	original_buckets[64];
	uint32_t	entry_ids[65];
	PglcCacheIndex index;
	TestContext context = {0};
	uint32_t	entry_id;

	pglc_index_init(&index, buckets, scratch, 64);
	for (entry_id = 1; entry_id <= 65; entry_id++)
	{
		context.entries[entry_id - 1].hash = 5;
		context.entries[entry_id - 1].key = entry_id;
		entry_ids[entry_id - 1] = entry_id;
	}
	for (entry_id = 1; entry_id <= 63; entry_id++)
		if (pglc_index_insert(&index, 5, entry_id, test_entry_hash,
							  test_entry_matches, &context) != PGLC_INDEX_INSERTED)
			return 1;
	context.key = 32;
	if (pglc_index_remove(&index, 5, test_entry_hash,
						  test_entry_matches, &context) != 32)
		return 2;
	memcpy(original_buckets, buckets, sizeof(buckets));
	if (pglc_index_rebuild(&index, entry_ids, 65, test_entry_hash,
						   &context))
		return 3;
	if (memcmp(buckets, original_buckets, sizeof(buckets)) != 0 ||
		index.tombstones != 1)
		return 4;
	context.key = 63;
	if (pglc_index_find(&index, 5, test_entry_hash,
						 test_entry_matches, &context) != 63)
		return 5;
	return 0;
}

static int
test_corrupt_bucket_count_blocks_index_operations(void)
{
	uint32_t	buckets[8];
	uint32_t	scratch[8];
	uint32_t	original_buckets[8];
	uint32_t	original_scratch[8];
	uint32_t	entry_ids[] = {1};
	PglcCacheIndex index;
	TestContext context = {0};

	pglc_index_init(&index, buckets, scratch, 8);
	context.entries[0].hash = 3;
	context.entries[0].key = 1;
	if (!pglc_index_valid(&index, 8))
		return 1;
	memset(buckets, 0xA5, sizeof(buckets));
	memcpy(scratch, buckets, sizeof(scratch));
	memcpy(original_buckets, buckets, sizeof(buckets));
	memcpy(original_scratch, scratch, sizeof(scratch));

	index.bucket_count = 0;
	if (pglc_index_valid(&index, 8) ||
		pglc_index_find(&index, 3, test_entry_hash,
						test_entry_matches, &context) != PGLC_INDEX_TOMBSTONE ||
		pglc_index_insert(&index, 3, 1, test_entry_hash,
					   test_entry_matches, &context) != PGLC_INDEX_CORRUPT ||
		pglc_index_remove(&index, 3, test_entry_hash,
					   test_entry_matches, &context) != PGLC_INDEX_TOMBSTONE ||
		pglc_index_rebuild(&index, entry_ids, 1, test_entry_hash, &context) ||
		pglc_index_rebuild_existing(&index, test_entry_hash, &context) ||
		!pglc_index_needs_rebuild(&index) ||
		memcmp(buckets, original_buckets, sizeof(buckets)) != 0 ||
		memcmp(scratch, original_scratch, sizeof(scratch)) != 0)
		return 2;

	index.bucket_count = 16;
	if (pglc_index_valid(&index, 8) ||
		pglc_index_find(&index, 3, test_entry_hash,
						test_entry_matches, &context) != PGLC_INDEX_TOMBSTONE ||
		pglc_index_insert(&index, 3, 1, test_entry_hash,
					   test_entry_matches, &context) != PGLC_INDEX_CORRUPT ||
		pglc_index_rebuild(&index, entry_ids, 1, test_entry_hash, &context) ||
		pglc_index_rebuild_existing(&index, test_entry_hash, &context) ||
		memcmp(buckets, original_buckets, sizeof(buckets)) != 0 ||
		memcmp(scratch, original_scratch, sizeof(scratch)) != 0)
		return 3;
	return 0;
}

int
main(void)
{
	int result;

	result = test_bucket_zero_tombstone_reuse_after_wraparound();
	if (result != 0)
		return 10 + result;
	result = test_failed_rebuild_preserves_index();
	if (result != 0)
		return 20 + result;
	result = test_corrupt_bucket_count_blocks_index_operations();
	if (result != 0)
		return 30 + result;
	return 0;
}
