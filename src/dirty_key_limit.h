/* SPDX-License-Identifier: MIT */
#ifndef PGLC_DIRTY_KEY_LIMIT_H
#define PGLC_DIRTY_KEY_LIMIT_H

#include <stdbool.h>
#include <stddef.h>

static inline bool
pglc_dirty_key_limit_requires_fallback(bool key_already_collected,
									  size_t dirty_key_count,
									  size_t max_dirty_keys)
{
	return !key_already_collected && dirty_key_count >= max_dirty_keys;
}

static inline size_t
pglc_dirty_key_count_after_entry(size_t dirty_key_count, bool is_key_entry)
{
	return dirty_key_count + (is_key_entry ? 1 : 0);
}

#endif
