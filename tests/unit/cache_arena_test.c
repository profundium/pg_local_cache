/* SPDX-License-Identifier: MIT */
#include "cache_arena.h"
#include "cache_index.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct TestEntry
{
	uint64_t hash;
	uint32_t key;
} TestEntry;

typedef struct TestIndexContext
{
	TestEntry entries[80];
	uint32_t key;
} TestIndexContext;

static uint64_t
test_entry_hash(uint32_t entry_id, void *opaque)
{
	TestIndexContext *context = (TestIndexContext *) opaque;

	return context->entries[entry_id - 1].hash;
}

static bool
test_entry_matches(uint32_t entry_id, void *opaque)
{
	TestIndexContext *context = (TestIndexContext *) opaque;

	return context->entries[entry_id - 1].key == context->key;
}

static int
test_arena_classes_and_page_reuse(void)
{
	uint8_t		memory[PGLC_ARENA_PAGE_SIZE];
	PglcArenaPage page;
	PglcArena	arena;
	uint32_t	block_refs[64];
	uint32_t	class_size;
	uint32_t	block_ref;
	uint32_t	index;
	static const struct
	{
		uint32_t request;
		uint32_t expected;
	} classes[] = {
		{1, 256}, {256, 256}, {257, 512}, {512, 512},
		{513, 1024}, {1024, 1024}, {1025, 2048}, {2048, 2048},
		{2049, 4096}, {4096, 4096}, {4097, 8192}, {8192, 8192},
		{8193, 16384}, {16384, 16384}, {16385, 0}
	};

	for (index = 0; index < sizeof(classes) / sizeof(classes[0]); index++)
		if (pglc_arena_class_size(classes[index].request) !=
			classes[index].expected)
			return 1;
	if (!pglc_arena_init(&arena, memory, &page, 1))
		return 2;
	for (index = 0; index < 64; index++)
		if (!pglc_arena_alloc(&arena, 1000, &block_refs[index], &class_size) ||
			class_size != 1024)
			return 3;
	if (pglc_arena_alloc(&arena, 1000, &block_ref, &class_size))
		return 4;
	for (index = 0; index < 64; index++)
		if (!pglc_arena_free(&arena, block_refs[index], 1000))
			return 5;
	if (arena.assigned_bytes != 0 || arena.used_bytes != 0 ||
		arena.class_slack_bytes != 0 || pglc_arena_free(&arena, block_refs[0], 1000))
		return 6;
	if (!pglc_arena_alloc(&arena, 1500, &block_ref, &class_size) ||
		class_size != 2048 || block_ref / PGLC_ARENA_PAGE_SIZE != 0 ||
		arena.assigned_bytes != PGLC_ARENA_PAGE_SIZE ||
		arena.class_slack_bytes != 548 ||
		pglc_arena_block(&arena, block_ref) != memory + block_ref)
		return 7;
	return 0;
}

static int
test_arena_rejects_corrupt_metadata(void)
{
	uint8_t		memory[PGLC_ARENA_PAGE_SIZE];
	PglcArenaPage page;
	PglcArena	arena;
	uint32_t	block_ref;
	uint32_t	class_size;

	if (!pglc_arena_init(&arena, memory, &page, 1) ||
		!pglc_arena_alloc(&arena, 100, &block_ref, &class_size))
		return 1;
	page.class_index = UINT8_MAX;
	if (pglc_arena_block(&arena, block_ref) != NULL ||
		pglc_arena_block_class(&arena, block_ref) != 0 ||
		pglc_arena_free(&arena, block_ref, 100) ||
		pglc_arena_alloc(&arena, 100, &block_ref, &class_size))
		return 2;

	if (!pglc_arena_init(&arena, memory, &page, 1) ||
		!pglc_arena_alloc(&arena, 100, &block_ref, &class_size))
		return 3;
	page.free_head = PGLC_ARENA_PAGE_SIZE;
	if (pglc_arena_alloc(&arena, 100, &block_ref, &class_size))
		return 4;
	return 0;
}

static int
test_index_bound_and_tombstone_rebuild(void)
{
	uint32_t	buckets[256];
	uint32_t	scratch[256];
	uint32_t	live_ids[80];
	uint32_t	index_no;
	uint32_t	live_count = 0;
	PglcCacheIndex index;
	TestIndexContext context;

	pglc_index_init(&index, buckets, scratch, 256);
	memset(&context, 0, sizeof(context));
	for (index_no = 0; index_no < 65; index_no++)
	{
		context.entries[index_no].hash = 3;
		context.entries[index_no].key = index_no;
		context.key = index_no;
		if (index_no < 64)
		{
			if (pglc_index_insert(&index, 3, index_no + 1,
									  test_entry_hash, test_entry_matches,
									  &context) != PGLC_INDEX_INSERTED)
				return 1;
		}
		else if (pglc_index_insert(&index, 3, index_no + 1,
									   test_entry_hash, test_entry_matches,
									   &context) != PGLC_INDEX_PROBE_LIMIT)
			return 2;
	}
	context.key = 63;
	if (pglc_index_find(&index, 3, test_entry_hash,
						test_entry_matches, &context) != 64 ||
		index.probe_rejections != 1)
		return 3;

	pglc_index_init(&index, buckets, scratch, 64);
	for (index_no = 0; index_no < 24; index_no++)
	{
		context.entries[index_no].hash = index_no * 17U;
		context.entries[index_no].key = index_no;
		context.key = index_no;
		if (pglc_index_insert(&index, context.entries[index_no].hash,
								  index_no + 1, test_entry_hash,
								  test_entry_matches, &context) !=
			PGLC_INDEX_INSERTED)
			return 4;
	}
	for (index_no = 0; index_no < 9; index_no++)
	{
		context.key = index_no;
		if (pglc_index_remove(&index, context.entries[index_no].hash,
							  test_entry_hash, test_entry_matches,
							  &context) != index_no + 1)
			return 5;
	}
	if (!pglc_index_needs_rebuild(&index))
		return 6;
	for (index_no = 9; index_no < 24; index_no++)
		live_ids[live_count++] = index_no + 1;
	if (!pglc_index_rebuild(&index, live_ids, live_count,
							test_entry_hash, &context) ||
		index.tombstones != 0 || pglc_index_needs_rebuild(&index))
		return 7;
	for (index_no = 9; index_no < 24; index_no++)
	{
		context.key = index_no;
		if (pglc_index_find(&index, context.entries[index_no].hash,
							test_entry_hash, test_entry_matches,
							&context) != index_no + 1)
			return 8;
	}
	return 0;
}

int
main(void)
{
	int			result = test_arena_classes_and_page_reuse();

	if (result != 0)
	{
		fprintf(stderr, "cache arena test failed: %d\n", result);
		return result;
	}
	result = test_arena_rejects_corrupt_metadata();
	if (result != 0)
	{
		fprintf(stderr, "cache arena corruption test failed: %d\n", result);
		return 30 + result;
	}
	result = test_index_bound_and_tombstone_rebuild();
	if (result != 0)
	{
		fprintf(stderr, "cache index test failed: %d\n", result);
		return result;
	}
	puts("cache arena/index tests passed");
	return 0;
}
