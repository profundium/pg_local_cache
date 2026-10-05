/* SPDX-License-Identifier: MIT */
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "key_codec.h"

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

static void
expect_scan(const char *input, Oid type, int32 typmod, bool utf8,
			bool expected, const char *canonical)
{
	char		result[1024];
	Size		result_length = 99;
	bool		accepted;

	memset(result, 0xa5, sizeof(result));
	accepted = pglc_key_scan_json_single(input, strlen(input), "id", 2,
										 type, typmod, utf8, result,
										 sizeof(result), &result_length);
	CHECK(accepted == expected);
	if (accepted)
	{
		CHECK(result_length == strlen(canonical));
		CHECK(memcmp(result, canonical, result_length) == 0);
		CHECK(result[result_length] == '\0');
	}
	else
		CHECK(result_length == 0);
}

static void
test_integer_forms(void)
{
	static const struct
	{
		Oid type;
		const char *json;
		const char *canonical;
	} cases[] = {
		{PGLC_KEY_SCAN_INT2OID, "{\"id\":-32768}", "6:-32768;"},
		{PGLC_KEY_SCAN_INT2OID, "{\"id\":32767}", "5:32767;"},
		{PGLC_KEY_SCAN_INT4OID, "{ \"id\" : 2147483647 }", "10:2147483647;"},
		{PGLC_KEY_SCAN_INT4OID, "{\"id\":-2147483648}", "11:-2147483648;"},
		{PGLC_KEY_SCAN_INT8OID, "{\"id\":9223372036854775807}",
			"19:9223372036854775807;"},
		{PGLC_KEY_SCAN_INT8OID, "{\"id\":-9223372036854775808}",
			"20:-9223372036854775808;"},
		{PGLC_KEY_SCAN_INT8OID, "{\"id\":\"1\"}", "1:1;"},
		{PGLC_KEY_SCAN_INT8OID, "{\"id\":\"-9223372036854775808\"}",
			"20:-9223372036854775808;"},
		{PGLC_KEY_SCAN_INT4OID, "{\"id\":\"2147483647\"}",
			"10:2147483647;"},
		{PGLC_KEY_SCAN_INT8OID, "\n { \"id\" : 0 } \t", "1:0;"},
	};

	for (Size i = 0; i < sizeof(cases) / sizeof(cases[0]); i++)
		expect_scan(cases[i].json, cases[i].type, -1, true, true,
					cases[i].canonical);
	expect_scan("{\"id\":1}", PGLC_KEY_SCAN_INT8OID, -1, false,
				true, "1:1;");
	expect_scan("{\"id\":\"1\"}", PGLC_KEY_SCAN_INT8OID, -1, false,
				true, "1:1;");

	expect_scan("{\"id\":32768}", PGLC_KEY_SCAN_INT2OID, -1, true, false, NULL);
	expect_scan("{\"id\":-32769}", PGLC_KEY_SCAN_INT2OID, -1, true, false, NULL);
	expect_scan("{\"id\":2147483648}", PGLC_KEY_SCAN_INT4OID, -1, true, false, NULL);
	expect_scan("{\"id\":-2147483649}", PGLC_KEY_SCAN_INT4OID, -1, true, false, NULL);
	expect_scan("{\"id\":9223372036854775808}",
				PGLC_KEY_SCAN_INT8OID, -1, true, false, NULL);
	expect_scan("{\"id\":-9223372036854775809}",
				PGLC_KEY_SCAN_INT8OID, -1, true, false, NULL);
	expect_scan("{\"id\":\"9223372036854775808\"}",
				PGLC_KEY_SCAN_INT8OID, -1, true, false, NULL);
	expect_scan("{\"id\":\"01\"}", PGLC_KEY_SCAN_INT4OID, -1,
				true, false, NULL);
	expect_scan("{\"id\":\"+1\"}", PGLC_KEY_SCAN_INT4OID, -1,
				true, false, NULL);
	expect_scan("{\"id\":1e2}", PGLC_KEY_SCAN_INT4OID, -1, true, false, NULL);
	expect_scan("{\"id\":1.0}", PGLC_KEY_SCAN_INT4OID, -1, true, false, NULL);
	expect_scan("{\"id\":-0}", PGLC_KEY_SCAN_INT4OID, -1, true, false, NULL);
	expect_scan("{\"id\":01}", PGLC_KEY_SCAN_INT4OID, -1, true, false, NULL);
	expect_scan("{\"id\":999999999999999999999999999999999999999999999999}",
				PGLC_KEY_SCAN_INT8OID, -1, true, false, NULL);
}

static void
test_text_forms(void)
{
	expect_scan("{\"id\":\"abc\"}", PGLC_KEY_SCAN_TEXTOID, -1,
				true, true, "3:abc;");
	expect_scan("{ \"id\" : \"a:b;c\" }", PGLC_KEY_SCAN_VARCHAROID, -1,
				true, true, "5:a:b;c;");
	expect_scan("{\"id\":\"\"}", PGLC_KEY_SCAN_TEXTOID, -1,
				true, true, "0:;");
	expect_scan("{\"id\":\"é\"}", PGLC_KEY_SCAN_TEXTOID, -1,
				true, true, "2:é;");
	expect_scan("{\"id\":\"a\\nb\"}", PGLC_KEY_SCAN_TEXTOID, -1,
				true, false, NULL);
	expect_scan("{\"id\":\"a\\u0062\"}", PGLC_KEY_SCAN_TEXTOID, -1,
				true, false, NULL);
	expect_scan("{\"i\\u0064\":\"abc\"}", PGLC_KEY_SCAN_TEXTOID, -1,
				true, false, NULL);
	expect_scan("{\"id\":\"\\u0000\"}", PGLC_KEY_SCAN_TEXTOID, -1,
				true, false, NULL);
	expect_scan("{\"id\":\"\\ud83d\\ude00\"}",
				PGLC_KEY_SCAN_TEXTOID, -1, true, false, NULL);
	expect_scan("{\"id\":\"x\",\"id\":\"y\"}",
				PGLC_KEY_SCAN_TEXTOID, -1, true, false, NULL);
	expect_scan("{\"id\":\"x\",\"extra\":1}",
				PGLC_KEY_SCAN_TEXTOID, -1, true, false, NULL);
	expect_scan("{\"id\":\"x\"}", PGLC_KEY_SCAN_VARCHAROID, 8,
				true, false, NULL);
	expect_scan("{\"id\":\"x\"}", PGLC_KEY_SCAN_TEXTOID, -1,
				false, false, NULL);
	expect_scan("{\"id\":\"x\"}", 1042, -1, true, false, NULL);
	expect_scan("{\"id\":1}", 999999, -1, true, false, NULL);
}

static void
test_invalid_and_capacity_fallback(void)
{
	char		invalid_utf8[] = "{\"id\":\"\xc0\xaf\"}";
	char		overlong[1100];
	char		too_small[4];
	Size		key_length;

	expect_scan("{\"id\":}", PGLC_KEY_SCAN_INT4OID, -1, true, false, NULL);
	expect_scan("{\"other\":1}", PGLC_KEY_SCAN_INT4OID, -1, true, false, NULL);
	expect_scan(invalid_utf8, PGLC_KEY_SCAN_TEXTOID, -1, true, false, NULL);

	memset(overlong, 'x', sizeof(overlong));
	memcpy(overlong, "{\"id\":\"", 7);
	memcpy(overlong + sizeof(overlong) - 3, "\"}", 3);
	expect_scan(overlong, PGLC_KEY_SCAN_TEXTOID, -1, true, false, NULL);

	CHECK(!pglc_key_scan_json_single("{\"id\":12345}", 12, "id", 2,
									PGLC_KEY_SCAN_INT4OID, -1, true,
									too_small, sizeof(too_small), &key_length));
	CHECK(key_length == 0);
}

static void
test_generated_numbers(void)
{
	char		json[64];
	char		expected[64];

	for (int value = -2048; value <= 2048; value++)
	{
		Size		canonical_length;
		char		canonical[64];
		int		json_length = snprintf(json, sizeof(json), "{\"id\":%d}", value);
		int		value_length = snprintf(expected, sizeof(expected), "%d", value);
		char		expected_canonical[64];
		int		expected_length = snprintf(expected_canonical,
											 sizeof(expected_canonical), "%d:%s;",
											 value_length, expected);

		CHECK(json_length > 0 && (Size) json_length < sizeof(json));
		canonical_length = 0;
		CHECK(pglc_key_scan_json_single(json, (Size) json_length, "id", 2,
										 PGLC_KEY_SCAN_INT4OID, -1, true,
										 canonical, sizeof(canonical),
										 &canonical_length));
		CHECK(expected_length > 0);
		CHECK(canonical_length == (Size) expected_length);
		CHECK(memcmp(canonical, expected_canonical, canonical_length) == 0);
	}
}

int
main(void)
{
	test_integer_forms();
	test_text_forms();
	test_invalid_and_capacity_fallback();
	test_generated_numbers();
	printf("key codec scanner tests: %u assertions passed\n", assertions);
	return EXIT_SUCCESS;
}
