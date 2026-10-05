/* SPDX-License-Identifier: MIT */
#ifndef PGLC_CACHE_INDEX_H
#define PGLC_CACHE_INDEX_H

#include <stdbool.h>
#include <stdint.h>

#define PGLC_INDEX_EMPTY UINT32_C(0)
#define PGLC_INDEX_TOMBSTONE UINT32_MAX
#define PGLC_INDEX_MAX_PROBES 64U

typedef struct PglcCacheIndex
{
	uint32_t   *buckets;
	uint32_t   *scratch;
	uint32_t	bucket_count;
	uint32_t	allocated_bucket_count;
	uint32_t	tombstones;
	uint64_t	probe_rejections;
} PglcCacheIndex;

typedef uint64_t (*PglcIndexEntryHash)(uint32_t entry_id, void *context);
typedef bool (*PglcIndexEntryMatches)(uint32_t entry_id, void *context);

typedef enum PglcIndexInsertResult
{
	PGLC_INDEX_INSERTED = 1,
	PGLC_INDEX_EXISTS = 2,
	PGLC_INDEX_PROBE_LIMIT = 3,
	PGLC_INDEX_CORRUPT = 4
} PglcIndexInsertResult;

void pglc_index_init(PglcCacheIndex *index, uint32_t *buckets,
					 uint32_t *scratch, uint32_t bucket_count);
bool pglc_index_valid(const PglcCacheIndex *index,
				  uint32_t allocated_bucket_count);
uint32_t pglc_index_find(const PglcCacheIndex *index, uint64_t hash,
						PglcIndexEntryHash entry_hash,
						PglcIndexEntryMatches matches, void *context);
PglcIndexInsertResult pglc_index_insert(PglcCacheIndex *index,
										 uint64_t hash, uint32_t entry_id,
										 PglcIndexEntryHash entry_hash,
										 PglcIndexEntryMatches matches,
										 void *context);
uint32_t pglc_index_remove(PglcCacheIndex *index, uint64_t hash,
						   PglcIndexEntryHash entry_hash,
						   PglcIndexEntryMatches matches, void *context);
bool pglc_index_rebuild(PglcCacheIndex *index, const uint32_t *entry_ids,
						uint32_t entry_count, PglcIndexEntryHash entry_hash,
						void *context);
bool pglc_index_rebuild_existing(PglcCacheIndex *index,
								 PglcIndexEntryHash entry_hash,
								 void *context);
bool pglc_index_needs_rebuild(const PglcCacheIndex *index);

#endif
