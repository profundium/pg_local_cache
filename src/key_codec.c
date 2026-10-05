/* SPDX-License-Identifier: MIT */
#include "key_codec.h"

#include <limits.h>
#include <stdint.h>
#include <string.h>

#ifndef PGLC_KEY_CODEC_STANDALONE
#include "catalog/pg_type_d.h"
#include "utils/fmgroids.h"
#include "utils/builtins.h"
#include "pg_local_cache.h"
#endif

static void
skip_json_space(const char *input, Size input_length, Size *position)
{
	while (*position < input_length &&
		   (input[*position] == ' ' || input[*position] == '\t' ||
			input[*position] == '\n' || input[*position] == '\r'))
		(*position)++;
}

static bool
valid_utf8(const unsigned char *value, Size length)
{
	Size		i = 0;

	while (i < length)
	{
		unsigned char c = value[i++];
		unsigned int remaining;
		unsigned char second_min = 0x80;
		unsigned char second_max = 0xbf;

		if (c == 0)
			return false;
		if (c < 0x80)
			continue;
		if (c >= 0xc2 && c <= 0xdf)
			remaining = 1;
		else if (c >= 0xe0 && c <= 0xef)
		{
			remaining = 2;
			if (c == 0xe0)
				second_min = 0xa0;
			else if (c == 0xed)
				second_max = 0x9f;
		}
		else if (c >= 0xf0 && c <= 0xf4)
		{
			remaining = 3;
			if (c == 0xf0)
				second_min = 0x90;
			else if (c == 0xf4)
				second_max = 0x8f;
		}
		else
			return false;

		if (length - i < remaining || value[i] < second_min ||
			value[i] > second_max)
			return false;
		i++;
		while (--remaining > 0)
		{
			if (value[i] < 0x80 || value[i] > 0xbf)
				return false;
			i++;
		}
	}
	return true;
}

static Size
decimal_length(Size value)
{
	Size		digits = 1;

	while (value >= 10)
	{
		value /= 10;
		digits++;
	}
	return digits;
}

static void
write_decimal(Size value, char *destination)
{
	char		reversed[3 * sizeof(Size)];
	Size		count = 0;
	Size		i;

	do
	{
		reversed[count++] = (char) ('0' + value % 10);
		value /= 10;
	} while (value != 0);
	for (i = 0; i < count; i++)
		destination[i] = reversed[count - i - 1];
}

static bool
parse_json_integer(const char *input, Size input_length, Size *position,
				   Oid key_type, char *rendered, Size *rendered_length)
{
	Size		start = *position;
	bool		negative = false;
	uint64_t	magnitude = 0;
	int64_t		value;
	uint64_t	limit;
	Size		digits = 0;
	char		reversed[20];
	Size		reversed_length = 0;

	if (start < input_length && input[start] == '-')
	{
		negative = true;
		start++;
	}
	if (start >= input_length || input[start] < '0' || input[start] > '9')
		return false;
	if (input[start] == '0' && negative)
		return false;
	if (input[start] == '0' && start + 1 < input_length &&
		input[start + 1] >= '0' && input[start + 1] <= '9')
		return false;

	while (start < input_length && input[start] >= '0' && input[start] <= '9')
	{
		unsigned int digit = (unsigned int) (input[start] - '0');

		if (magnitude > (UINT64_MAX - digit) / 10)
			return false;
		magnitude = magnitude * 10 + digit;
		start++;
		digits++;
	}
	if (digits == 0)
		return false;

	if (key_type == PGLC_KEY_SCAN_INT2OID)
		limit = negative ? (uint64_t) INT16_MAX + 1 : INT16_MAX;
	else if (key_type == PGLC_KEY_SCAN_INT4OID)
		limit = negative ? (uint64_t) INT32_MAX + 1 : INT32_MAX;
	else
		limit = negative ? (uint64_t) INT64_MAX + 1 : INT64_MAX;
	if (magnitude > limit)
		return false;
	value = negative ? -(int64_t) (magnitude - 1) - 1 : (int64_t) magnitude;

	if (value < 0)
	{
		rendered[0] = '-';
		magnitude = (uint64_t) (-(value + 1)) + 1;
	}
	else
		magnitude = (uint64_t) value;
	do
	{
		reversed[reversed_length++] = (char) ('0' + magnitude % 10);
		magnitude /= 10;
	} while (magnitude != 0);
	*rendered_length = (value < 0 ? 1 : 0) + reversed_length;
	for (Size i = 0; i < reversed_length; i++)
		rendered[(value < 0 ? 1 : 0) + i] =
			reversed[reversed_length - i - 1];
	*position = start;
	return true;
}

/*
 * Accept only one unescaped ASCII field and a raw integer or UTF-8 string.
 * Strict decimal values encoded as JSON strings are accepted too; every
 * uncertain spelling returns false for jsonb_in and typed-input fallback.
 */
bool
pglc_key_scan_json_single(const char *input, Size input_length,
						  const char *column, Size column_length,
						  Oid key_type, int32 typmod, bool database_is_utf8,
						  char *destination, Size destination_capacity,
						  Size *key_length)
{
	Size		position = 0;
	Size		name_start;
	Size		name_length;
	Size		rendered_length = 0;
	char		integer[32];
	const char *rendered = integer;
	bool		is_integer;
	Size		prefix_length;
	Size		used;

	if (key_length != NULL)
		*key_length = 0;
	if (input == NULL || column == NULL || destination == NULL ||
		key_length == NULL || column_length == 0 ||
		(destination_capacity == 0))
		return false;
	if (!((key_type == PGLC_KEY_SCAN_INT2OID ||
		   key_type == PGLC_KEY_SCAN_INT4OID ||
		   key_type == PGLC_KEY_SCAN_INT8OID) && typmod == -1) &&
		!((key_type == PGLC_KEY_SCAN_TEXTOID ||
		   key_type == PGLC_KEY_SCAN_VARCHAROID) && typmod == -1))
		return false;
	for (Size i = 0; i < column_length; i++)
		if ((unsigned char) column[i] < 0x21 ||
			(unsigned char) column[i] > 0x7e || column[i] == '"' ||
			column[i] == '\\')
			return false;

	skip_json_space(input, input_length, &position);
	if (position >= input_length || input[position++] != '{')
		return false;
	skip_json_space(input, input_length, &position);
	if (position >= input_length || input[position++] != '"')
		return false;
	name_start = position;
	while (position < input_length && input[position] != '"')
	{
		unsigned char ch = (unsigned char) input[position];

		if (ch < 0x20 || ch >= 0x80 || ch == '\\' || ch == 0)
			return false;
		position++;
	}
	if (position >= input_length)
		return false;
	name_length = position - name_start;
	if (name_length != column_length ||
		memcmp(input + name_start, column, column_length) != 0)
		return false;
	position++;
	skip_json_space(input, input_length, &position);
	if (position >= input_length || input[position++] != ':')
		return false;
	skip_json_space(input, input_length, &position);
	is_integer = key_type == PGLC_KEY_SCAN_INT2OID ||
		key_type == PGLC_KEY_SCAN_INT4OID || key_type == PGLC_KEY_SCAN_INT8OID;
	if (!database_is_utf8 && !is_integer)
		return false;
	if (is_integer)
	{
		bool		quoted = position < input_length && input[position] == '"';

		if (quoted)
			position++;
		if (!parse_json_integer(input, input_length, &position, key_type,
								integer, &rendered_length))
			return false;
		if (quoted)
		{
			if (position >= input_length || input[position++] != '"')
				return false;
		}
	}
	else
	{
		Size		string_start;

		if (position >= input_length || input[position++] != '"')
			return false;
		string_start = position;
		while (position < input_length && input[position] != '"')
		{
			unsigned char ch = (unsigned char) input[position];

			if (ch < 0x20 || ch == '\\' || ch == 0)
				return false;
			position++;
		}
		if (position >= input_length || !valid_utf8(
				(const unsigned char *) input + string_start,
				position - string_start))
			return false;
		rendered = input + string_start;
		rendered_length = position - string_start;
		position++;
	}
	skip_json_space(input, input_length, &position);
	if (position >= input_length || input[position++] != '}')
		return false;
	skip_json_space(input, input_length, &position);
	if (position != input_length)
		return false;

	prefix_length = decimal_length(rendered_length);
	used = prefix_length + 1 + rendered_length + 1;
	if (used >= 1024 || used >= destination_capacity)
		return false;
	(void) write_decimal(rendered_length, destination);
	destination[prefix_length] = ':';
	if (rendered_length > 0)
		memcpy(destination + prefix_length + 1, rendered, rendered_length);
	destination[used - 1] = ';';
	destination[used] = '\0';
	*key_length = used;
	return true;
}

#ifndef PGLC_KEY_CODEC_STANDALONE

static int
pglc_key_length_digits(Size value, char *digits)
{
	char		reversed[3 * sizeof(Size)];
	int		count = 0;
	int		i;

	do
	{
		reversed[count++] = (char) ('0' + (value % 10));
		value /= 10;
	} while (value != 0);
	for (i = 0; i < count; i++)
		digits[i] = reversed[count - i - 1];
	return count;
}

bool
pglc_canonical_key_typed(const Datum *values, const bool *nulls, int key_count,
					 const Oid *key_types, FmgrInfo *output_functions,
					 char *destination, Size destination_capacity,
					 Size *key_len)
{
	Size		used = 0;
	int		component;

	if (key_len != NULL)
		*key_len = 0;
	if (destination != NULL && destination_capacity > 0)
		destination[0] = '\0';
	if (key_count <= 0 || key_count > PGLC_MAX_KEY_COLUMNS ||
		output_functions == NULL || values == NULL || nulls == NULL ||
		destination == NULL || key_len == NULL || destination_capacity == 0)
		return false;

	for (component = 0; component < key_count; component++)
	{
		char		integer_buffer[64];
		char	   *rendered = integer_buffer;
		char		digits[3 * sizeof(Size)];
		Size		rendered_len;
		int			digits_len;
		Size		part_len;
		bool		free_rendered = false;

		if (nulls[component])
			return false;
		if (key_types != NULL && key_types[component] == INT2OID)
			pg_ltoa((int32) DatumGetInt16(values[component]), rendered);
		else if (key_types != NULL && key_types[component] == INT4OID)
			pg_ltoa(DatumGetInt32(values[component]), rendered);
		else if (key_types != NULL && key_types[component] == INT8OID)
			pg_lltoa(DatumGetInt64(values[component]), rendered);
		else
		{
			rendered = OutputFunctionCall(&output_functions[component],
										  values[component]);
			free_rendered = true;
		}
		rendered_len = strlen(rendered);
		/*
		 * bpchar equality ignores trailing ASCII spaces, while bpcharout can
		 * expose a different amount of typmod padding for a query expression,
		 * a stored tuple and a trigger Datum.  Strip it here so every producer
		 * of a logically equal character(n) key reaches the same cache entry.
		 */
		if (output_functions[component].fn_oid == F_BPCHAROUT)
		{
			while (rendered_len > 0 && rendered[rendered_len - 1] == ' ')
				rendered_len--;
		}
		digits_len = pglc_key_length_digits(rendered_len, digits);
		part_len = (Size) digits_len + 1 + rendered_len + 1;
		if (part_len >= PGLC_KEY_MAX || used >= PGLC_KEY_MAX - part_len ||
			part_len >= destination_capacity ||
			used >= destination_capacity - part_len)
		{
			if (free_rendered)
				pfree(rendered);
			return false;
		}

		memcpy(destination + used, digits, digits_len);
		used += digits_len;
		destination[used++] = ':';
		if (rendered_len > 0)
		{
			memcpy(destination + used, rendered, rendered_len);
			used += rendered_len;
		}
		destination[used++] = ';';
		if (free_rendered)
			pfree(rendered);
	}

	destination[used] = '\0';
	*key_len = used;
	return true;
}

bool
pglc_canonical_key(const Datum *values, const bool *nulls, int key_count,
				   FmgrInfo *output_functions,
				   char *destination, Size destination_capacity,
				   Size *key_len)
{
	return pglc_canonical_key_typed(values, nulls, key_count, NULL,
								output_functions, destination,
									destination_capacity, key_len);
}
#endif
