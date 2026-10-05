/* SPDX-License-Identifier: MIT */
#include "cache_index.h"

#include <stddef.h>
#include <string.h>

static uint32_t
index_start(const PglcCacheIndex *index, uint64_t hash)
{
	return (uint32_t) hash & (index->bucket_count - 1U);
}

void
pglc_index_init(PglcCacheIndex *index, uint32_t *buckets, uint32_t *scratch,
				uint32_t bucket_count)
{
	index->buckets = buckets;
	index->scratch = scratch;
	index->bucket_count = bucket_count;
	index->tombstones = 0;
	index->probe_rejections = 0;
	memset(buckets, 0, (size_t) bucket_count * sizeof(*buckets));
	memset(scratch, 0, (size_t) bucket_count * sizeof(*scratch));
}

uint32_t
pglc_index_find(const PglcCacheIndex *index, uint64_t hash,
				PglcIndexEntryHash entry_hash,
				PglcIndexEntryMatches matches, void *context)
{
	uint32_t	start = index_start(index, hash);
	uint32_t	probe;

	for (probe = 0; probe < PGLC_INDEX_MAX_PROBES; probe++)
	{
		uint32_t	bucket = (start + probe) & (index->bucket_count - 1U);
		uint32_t	entry_id = index->buckets[bucket];

		if (entry_id == PGLC_INDEX_EMPTY)
			return PGLC_INDEX_EMPTY;
		if (entry_id != PGLC_INDEX_TOMBSTONE &&
			entry_hash(entry_id, context) == hash &&
			matches(entry_id, context))
			return entry_id;
	}
	return PGLC_INDEX_EMPTY;
}

PglcIndexInsertResult
pglc_index_insert(PglcCacheIndex *index, uint64_t hash, uint32_t entry_id,
				  PglcIndexEntryHash entry_hash,
				  PglcIndexEntryMatches matches, void *context)
{
	uint32_t	start = index_start(index, hash);
	uint32_t	first_tombstone = PGLC_INDEX_TOMBSTONE;
	uint32_t	probe;

	for (probe = 0; probe < PGLC_INDEX_MAX_PROBES; probe++)
	{
		uint32_t	bucket = (start + probe) & (index->bucket_count - 1U);
		uint32_t	candidate = index->buckets[bucket];

		if (candidate == PGLC_INDEX_TOMBSTONE)
		{
			if (first_tombstone == PGLC_INDEX_TOMBSTONE)
				first_tombstone = bucket;
			continue;
		}
		if (candidate == PGLC_INDEX_EMPTY)
		{
			uint32_t	destination = first_tombstone != PGLC_INDEX_TOMBSTONE ?
				first_tombstone : bucket;

			index->buckets[destination] = entry_id;
			if (first_tombstone != PGLC_INDEX_TOMBSTONE)
				index->tombstones--;
			return PGLC_INDEX_INSERTED;
		}
		if (entry_hash(candidate, context) == hash &&
			matches(candidate, context))
			return PGLC_INDEX_EXISTS;
	}
	if (first_tombstone != PGLC_INDEX_TOMBSTONE)
	{
		index->buckets[first_tombstone] = entry_id;
		index->tombstones--;
		return PGLC_INDEX_INSERTED;
	}
	index->probe_rejections++;
	return PGLC_INDEX_PROBE_LIMIT;
}

uint32_t
pglc_index_remove(PglcCacheIndex *index, uint64_t hash,
				   PglcIndexEntryHash entry_hash,
				   PglcIndexEntryMatches matches, void *context)
{
	uint32_t	start = index_start(index, hash);
	uint32_t	probe;

	for (probe = 0; probe < PGLC_INDEX_MAX_PROBES; probe++)
	{
		uint32_t	bucket = (start + probe) & (index->bucket_count - 1U);
		uint32_t	entry_id = index->buckets[bucket];

		if (entry_id == PGLC_INDEX_EMPTY)
			return PGLC_INDEX_EMPTY;
		if (entry_id != PGLC_INDEX_TOMBSTONE &&
			entry_hash(entry_id, context) == hash &&
			matches(entry_id, context))
		{
			index->buckets[bucket] = PGLC_INDEX_TOMBSTONE;
			index->tombstones++;
			return entry_id;
		}
	}
	return PGLC_INDEX_EMPTY;
}

bool
pglc_index_rebuild(PglcCacheIndex *index, const uint32_t *entry_ids,
				   uint32_t entry_count, PglcIndexEntryHash entry_hash,
				   void *context)
{
	uint32_t	index_no;

	memset(index->scratch, 0,
		   (size_t) index->bucket_count * sizeof(*index->scratch));
	for (index_no = 0; index_no < entry_count; index_no++)
	{
		uint32_t	entry_id = entry_ids[index_no];
		uint64_t	hash;
		uint32_t	start;
		uint32_t	probe;
		bool		inserted = false;

		if (entry_id == PGLC_INDEX_EMPTY ||
			entry_id == PGLC_INDEX_TOMBSTONE)
			continue;
		hash = entry_hash(entry_id, context);
		start = index_start(index, hash);
		for (probe = 0; probe < PGLC_INDEX_MAX_PROBES; probe++)
		{
			uint32_t	bucket = (start + probe) & (index->bucket_count - 1U);

			if (index->scratch[bucket] == PGLC_INDEX_EMPTY)
			{
				index->scratch[bucket] = entry_id;
				inserted = true;
				break;
			}
		}
		if (!inserted)
			return false;
	}
	memcpy(index->buckets, index->scratch,
		   (size_t) index->bucket_count * sizeof(*index->buckets));
	index->tombstones = 0;
	return true;
}

bool
pglc_index_needs_rebuild(const PglcCacheIndex *index)
{
	return index->tombstones > index->bucket_count / 8U;
}

bool
pglc_index_rebuild_existing(PglcCacheIndex *index,
							PglcIndexEntryHash entry_hash, void *context)
{
	uint32_t	bucket_no;

	memset(index->scratch, 0,
		   (size_t) index->bucket_count * sizeof(*index->scratch));
	for (bucket_no = 0; bucket_no < index->bucket_count; bucket_no++)
	{
		uint32_t	entry_id = index->buckets[bucket_no];
		uint64_t	hash;
		uint32_t	start;
		uint32_t	probe;
		bool		inserted = false;

		if (entry_id == PGLC_INDEX_EMPTY ||
			entry_id == PGLC_INDEX_TOMBSTONE)
			continue;
		hash = entry_hash(entry_id, context);
		start = index_start(index, hash);
		for (probe = 0; probe < PGLC_INDEX_MAX_PROBES; probe++)
		{
			uint32_t	bucket = (start + probe) & (index->bucket_count - 1U);

			if (index->scratch[bucket] == PGLC_INDEX_EMPTY)
			{
				index->scratch[bucket] = entry_id;
				inserted = true;
				break;
			}
		}
		if (!inserted)
			return false;
	}
	memcpy(index->buckets, index->scratch,
		   (size_t) index->bucket_count * sizeof(*index->buckets));
	index->tombstones = 0;
	return true;
}
