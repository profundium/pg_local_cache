/* SPDX-License-Identifier: MIT */
#ifndef PGLC_CACHE_ARENA_H
#define PGLC_CACHE_ARENA_H

#include <stdbool.h>
#include <stdint.h>

#define PGLC_ARENA_PAGE_SIZE 65536U
#define PGLC_ARENA_MIN_BLOCK 256U
#define PGLC_ARENA_MAX_BLOCK 16384U
#define PGLC_ARENA_CLASS_COUNT 7U
#define PGLC_ARENA_NO_BLOCK UINT32_MAX

typedef struct PglcArenaPage
{
	uint32_t	free_head;
	uint16_t	block_count;
	uint16_t	live_count;
	uint8_t		class_index;
	uint8_t		allocated[32];
} PglcArenaPage;

typedef struct PglcArena
{
	uint8_t	   *memory;
	PglcArenaPage *pages;
	uint32_t	page_count;
	uint32_t	first_unassigned_page;
	uint64_t	used_bytes;
	uint64_t	class_slack_bytes;
	uint64_t	assigned_bytes;
} PglcArena;

uint32_t pglc_arena_class_size(uint32_t request_size);
bool pglc_arena_init(PglcArena *arena, void *memory, PglcArenaPage *pages,
					 uint32_t page_count);
bool pglc_arena_alloc(PglcArena *arena, uint32_t request_size,
					  uint32_t *block_ref, uint32_t *class_size);
bool pglc_arena_free(PglcArena *arena, uint32_t block_ref,
					 uint32_t request_size);
bool pglc_arena_resize(PglcArena *arena, uint32_t block_ref,
					   uint32_t old_request_size,
					   uint32_t new_request_size);
uint32_t pglc_arena_block_class(const PglcArena *arena, uint32_t block_ref);
void *pglc_arena_block(PglcArena *arena, uint32_t block_ref);

#endif
