/* SPDX-License-Identifier: MIT */
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>

#include "dirty_key_limit.h"

static unsigned int assertions;

#define CHECK(condition) \
	do { \
		assertions++; \
		if (!(condition)) \
		{ \
			fprintf(stderr, "%s:%d: assertion failed: %s\n", \
					__FILE__, __LINE__, #condition); \
			exit(EXIT_FAILURE); \
		} \
	} while (0)

int
main(void)
{
	CHECK(!pglc_dirty_key_limit_requires_fallback(true, 1, 1));
	CHECK(pglc_dirty_key_limit_requires_fallback(false, 1, 1));
	CHECK(!pglc_dirty_key_limit_requires_fallback(false, 0, 1));
	CHECK(pglc_dirty_key_count_after_entry(1, true) == 2);
	CHECK(pglc_dirty_key_count_after_entry(1, false) == 1);
	printf("dirty key limit tests: %u assertions passed\n", assertions);
	return EXIT_SUCCESS;
}
