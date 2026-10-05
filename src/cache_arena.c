/* SPDX-License-Identifier: MIT */
#include "cache_arena.h"

#include <stddef.h>
#include <string.h>

static uint32_t
class_bytes(uint8_t class_index)
{
	if (class_index >= PGLC_ARENA_CLASS_COUNT)
		return 0;
	return PGLC_ARENA_MIN_BLOCK << class_index;
}

static bool
arena_ready(const PglcArena *arena)
{
	return arena != NULL && arena->page_count != 0 && arena->memory != NULL &&
		arena->pages != NULL &&
		arena->page_count <= UINT32_MAX / PGLC_ARENA_PAGE_SIZE
#if SIZE_MAX < UINT64_MAX
		&& arena->page_count <= SIZE_MAX / PGLC_ARENA_PAGE_SIZE
#endif
		;
}

static bool
arena_counters_valid(const PglcArena *arena)
{
	uint64_t	extent;

	if (!arena_ready(arena))
		return false;
	extent = (uint64_t) arena->page_count * PGLC_ARENA_PAGE_SIZE;
	return arena->assigned_bytes <= extent &&
		arena->used_bytes <= arena->assigned_bytes &&
		arena->class_slack_bytes <= arena->assigned_bytes - arena->used_bytes;
}

static bool
page_shape_valid(const PglcArenaPage *page, uint32_t *block_size)
{
	uint32_t	size;
	uint32_t	block_count;

	if (page == NULL || page->class_index == 0 ||
		page->class_index > PGLC_ARENA_CLASS_COUNT)
		return false;
	size = class_bytes(page->class_index - 1);
	if (size == 0)
		return false;
	block_count = PGLC_ARENA_PAGE_SIZE / size;
	if (page->block_count != block_count || page->live_count > block_count)
		return false;
	*block_size = size;
	return true;
}

static bool
block_info(const PglcArena *arena, uint32_t block_ref,
		   bool require_allocated, uint32_t *page_no_out,
		   uint32_t *block_size_out, uint32_t *block_no_out)
{
	uint32_t	page_no;
	uint32_t	page_offset;
	uint32_t	block_size;
	uint32_t	block_no;
	const PglcArenaPage *page;
	uint8_t		mask;
	uint64_t	extent;

	if (!arena_ready(arena))
		return false;
	page_no = block_ref / PGLC_ARENA_PAGE_SIZE;
	page_offset = block_ref % PGLC_ARENA_PAGE_SIZE;
	if (page_no >= arena->page_count)
		return false;
	page = &arena->pages[page_no];
	if (!page_shape_valid(page, &block_size) ||
		page_offset % block_size != 0)
		return false;
	if ((require_allocated && page->live_count == 0) ||
		(!require_allocated && page->live_count >= page->block_count))
		return false;
	block_no = page_offset / block_size;
	if (block_no >= page->block_count || block_no >= PGLC_ARENA_PAGE_SIZE /
		PGLC_ARENA_MIN_BLOCK)
		return false;
	mask = (uint8_t) (1U << (block_no % 8U));
	if (((page->allocated[block_no / 8U] & mask) != 0) != require_allocated)
		return false;
	extent = (uint64_t) arena->page_count * PGLC_ARENA_PAGE_SIZE;
	if ((uint64_t) block_ref + block_size > extent)
		return false;
	if (page_no_out != NULL)
		*page_no_out = page_no;
	if (block_size_out != NULL)
		*block_size_out = block_size;
	if (block_no_out != NULL)
		*block_no_out = block_no;
	return true;
}

static bool
free_head_valid(const PglcArena *arena, uint32_t page_no,
				const PglcArenaPage *page, uint32_t block_size)
{
	uint32_t	free_page;
	uint32_t	free_size;

	if (page->free_head == PGLC_ARENA_NO_BLOCK)
		return page->live_count == page->block_count;
	return block_info(arena, page->free_head, false, &free_page,
				   &free_size, NULL) && free_page == page_no &&
		free_size == block_size && page->live_count < page->block_count;
}

uint32_t
pglc_arena_class_size(uint32_t request_size)
{
	uint32_t	block_size = PGLC_ARENA_MIN_BLOCK;

	if (request_size <= PGLC_ARENA_MIN_BLOCK)
		return PGLC_ARENA_MIN_BLOCK;
	while (block_size < request_size && block_size < PGLC_ARENA_MAX_BLOCK)
		block_size <<= 1;
	return block_size >= request_size ? block_size : 0;
}

bool
pglc_arena_init(PglcArena *arena, void *memory, PglcArenaPage *pages,
				uint32_t page_count)
{
	uint32_t	page;

	if (arena == NULL || (page_count != 0 && (memory == NULL || pages == NULL)))
		return false;
	if (page_count > UINT32_MAX / PGLC_ARENA_PAGE_SIZE)
		return false;
	arena->memory = (uint8_t *) memory;
	arena->pages = pages;
	arena->page_count = page_count;
	arena->first_unassigned_page = 0;
	arena->used_bytes = 0;
	arena->class_slack_bytes = 0;
	arena->assigned_bytes = 0;
	for (page = 0; page < page_count; page++)
	{
		pages[page].free_head = PGLC_ARENA_NO_BLOCK;
		pages[page].block_count = 0;
		pages[page].live_count = 0;
		pages[page].class_index = 0;
		memset(pages[page].allocated, 0, sizeof(pages[page].allocated));
	}
	return true;
}

static bool
assign_page(PglcArena *arena, uint32_t page_no, uint8_t class_index)
{
	PglcArenaPage *page;
	uint32_t	block_size = class_bytes(class_index);
	uint32_t	block_count;
	uint32_t	block_no;

	if (!arena_counters_valid(arena) || page_no >= arena->page_count ||
		block_size == 0 ||
		arena->assigned_bytes >
		(uint64_t) arena->page_count * PGLC_ARENA_PAGE_SIZE -
		PGLC_ARENA_PAGE_SIZE)
		return false;
	page = &arena->pages[page_no];
	if (page->class_index != 0)
		return false;
	block_count = PGLC_ARENA_PAGE_SIZE / block_size;
	page->class_index = class_index + 1;
	page->block_count = (uint16_t) block_count;
	page->live_count = 0;
	memset(page->allocated, 0, sizeof(page->allocated));
	page->free_head = page_no * PGLC_ARENA_PAGE_SIZE;
	for (block_no = 0; block_no < block_count; block_no++)
	{
		uint32_t	ref = page_no * PGLC_ARENA_PAGE_SIZE + block_no * block_size;
		uint32_t	next = block_no + 1 < block_count ? ref + block_size :
			PGLC_ARENA_NO_BLOCK;

		memcpy(arena->memory + ref, &next, sizeof(next));
	}
	arena->assigned_bytes += PGLC_ARENA_PAGE_SIZE;
	return true;
}

bool
pglc_arena_alloc(PglcArena *arena, uint32_t request_size,
				 uint32_t *block_ref, uint32_t *class_size)
{
	uint32_t	block_size = pglc_arena_class_size(request_size);
	uint8_t		class_index = 0;
	uint32_t	page_no;

	if (!arena_counters_valid(arena) ||
		arena->first_unassigned_page > arena->page_count ||
		block_size == 0 || block_ref == NULL || class_size == NULL)
		return false;
	while (class_index < PGLC_ARENA_CLASS_COUNT &&
		   (PGLC_ARENA_MIN_BLOCK << class_index) != block_size)
		class_index++;
	if (class_index >= PGLC_ARENA_CLASS_COUNT)
		return false;
	for (page_no = 0; page_no < arena->page_count; page_no++)
	{
		PglcArenaPage *page = &arena->pages[page_no];
		uint32_t	page_size;

		if (page->class_index == 0)
		{
			if (page->block_count != 0 || page->live_count != 0 ||
				page->free_head != PGLC_ARENA_NO_BLOCK)
				return false;
			continue;
		}
		if (!page_shape_valid(page, &page_size) ||
			!free_head_valid(arena, page_no, page, page_size))
			return false;
		if (page->class_index == class_index + 1 &&
			page->free_head != PGLC_ARENA_NO_BLOCK)
			break;
	}
	if (page_no == arena->page_count)
	{
		for (page_no = arena->first_unassigned_page;
			 page_no < arena->page_count; page_no++)
			if (arena->pages[page_no].class_index == 0)
				break;
		if (page_no == arena->page_count)
			return false;
		while (arena->first_unassigned_page < arena->page_count &&
			   arena->pages[arena->first_unassigned_page].class_index != 0)
			arena->first_unassigned_page++;
		if (!assign_page(arena, page_no, class_index))
			return false;
	}
	{
		PglcArenaPage *page = &arena->pages[page_no];
		uint32_t	next;
		uint32_t	free_page;
		uint32_t	free_size;
		uint32_t	block_no;

		if (block_size > arena->assigned_bytes ||
			arena->used_bytes + arena->class_slack_bytes >
			arena->assigned_bytes - block_size)
			return false;

		*block_ref = page->free_head;
		if (!block_info(arena, *block_ref, false, &free_page, &free_size, NULL) ||
			free_page != page_no || free_size != block_size)
			return false;
		memcpy(&next, arena->memory + *block_ref, sizeof(next));
		if (next != PGLC_ARENA_NO_BLOCK &&
			(!block_info(arena, next, false, &free_page, &free_size, NULL) ||
			 free_page != page_no || free_size != block_size))
			return false;
		block_no = (*block_ref % PGLC_ARENA_PAGE_SIZE) / block_size;
		{
			uint8_t		mask = (uint8_t) (1U << (block_no % 8U));

			if ((page->allocated[block_no / 8U] & mask) != 0)
				return false;
			page->allocated[block_no / 8U] |= mask;
		}
		page->free_head = next;
		page->live_count++;
	}
	arena->used_bytes += request_size;
	arena->class_slack_bytes += block_size - request_size;
	*class_size = block_size;
	return true;
}

bool
pglc_arena_free(PglcArena *arena, uint32_t block_ref, uint32_t request_size)
{
	uint32_t	page_no;
	uint32_t	page_offset;
	PglcArenaPage *page;
	uint32_t	block_size;
	uint32_t	next;
	uint32_t	block_no;
	uint8_t		mask;

	if (!block_info(arena, block_ref, true, &page_no, &block_size, &block_no))
		return false;
	page_offset = block_ref % PGLC_ARENA_PAGE_SIZE;
	page = &arena->pages[page_no];
	if (page->live_count == 0)
		return false;
	if (!arena_counters_valid(arena) || page_offset % block_size != 0 ||
		request_size > block_size ||
		arena->used_bytes < request_size ||
		arena->class_slack_bytes < block_size - request_size ||
		(page->live_count == 1 &&
		 arena->assigned_bytes < PGLC_ARENA_PAGE_SIZE))
		return false;
	mask = (uint8_t) (1U << (block_no % 8U));
	if (!free_head_valid(arena, page_no, page, block_size))
		return false;
	page->allocated[block_no / 8U] &= (uint8_t) ~mask;
	next = page->free_head;
	memcpy(arena->memory + block_ref, &next, sizeof(next));
	page->free_head = block_ref;
	page->live_count--;
	arena->used_bytes -= request_size;
	arena->class_slack_bytes -= block_size - request_size;
	if (page->live_count == 0)
	{
		page->class_index = 0;
		page->block_count = 0;
		page->free_head = PGLC_ARENA_NO_BLOCK;
		arena->assigned_bytes -= PGLC_ARENA_PAGE_SIZE;
		if (page_no < arena->first_unassigned_page)
			arena->first_unassigned_page = page_no;
	}
	return true;
}

bool
pglc_arena_resize(PglcArena *arena, uint32_t block_ref,
				  uint32_t old_request_size, uint32_t new_request_size)
{
	uint32_t	block_size = pglc_arena_block_class(arena, block_ref);

	if (!arena_counters_valid(arena) || block_size == 0 ||
		old_request_size > block_size ||
		new_request_size > block_size || arena->used_bytes < old_request_size ||
		arena->class_slack_bytes < block_size - old_request_size)
		return false;
	arena->used_bytes -= old_request_size;
	arena->used_bytes += new_request_size;
	arena->class_slack_bytes -= block_size - old_request_size;
	arena->class_slack_bytes += block_size - new_request_size;
	return true;
}

void *
pglc_arena_block(PglcArena *arena, uint32_t block_ref)
{
	if (!block_info(arena, block_ref, true, NULL, NULL, NULL))
		return NULL;
	return arena->memory + block_ref;
}

uint32_t
pglc_arena_block_class(const PglcArena *arena, uint32_t block_ref)
{
	uint32_t	block_size;

	if (!block_info(arena, block_ref, true, NULL, &block_size, NULL))
		return 0;
	return block_size;
}
