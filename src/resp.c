/* SPDX-License-Identifier: MIT */
#ifdef PGLC_RESP_STANDALONE
#include <ctype.h>
#include <inttypes.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#define PG_INT64_MAX INT64_MAX
#define INT64_FORMAT "%" PRId64
#define pg_strncasecmp strncasecmp

static void *
palloc(size_t size)
{
	void	   *result = malloc(size);

	if (result == NULL)
		abort();
	return result;
}

static char *
pstrdup(const char *value)
{
	size_t		length = strlen(value) + 1;
	char	   *result = palloc(length);

	memcpy(result, value, length);
	return result;
}
#else
#include "postgres.h"

#include <ctype.h>
#include <limits.h>

#include "utils/builtins.h"
#endif

#include "resp.h"

static int
parse_decimal_line(const char *buffer, Size length, Size *position,
				   int64 *result, const char **error)
{
	Size		i = *position;
	int64		value = 0;
	bool		negative = false;
	bool		have_digit = false;

	if (i >= length)
		return 0;

	if (buffer[i] == '-')
	{
		negative = true;
		i++;
	}

	while (i < length && buffer[i] != '\r')
	{
		unsigned char ch = (unsigned char) buffer[i];

		if (!isdigit(ch))
		{
			*error = "invalid decimal length";
			return -1;
		}
		have_digit = true;
		if (value > (PG_INT64_MAX - (ch - '0')) / 10)
		{
			*error = "decimal length overflow";
			return -1;
		}
		value = value * 10 + (ch - '0');
		i++;
	}

	if (!have_digit)
	{
		*error = "empty decimal length";
		return -1;
	}
	if (i + 1 >= length)
		return 0;
	if (buffer[i] != '\r' || buffer[i + 1] != '\n')
	{
		*error = "expected CRLF";
		return -1;
	}

	*position = i + 2;
	*result = negative ? -value : value;
	return 1;
}

int
pglc_resp_parse(const char *buffer, Size length, PgLocalCacheRespArg *args,
			   int *argc, Size *consumed, const char **error)
{
	Size		position = 0;
	int64		nargs;
	int			status;
	int			i;

	*argc = 0;
	*consumed = 0;
	*error = NULL;

	if (length == 0)
		return 0;
	if (buffer[position++] != '*')
	{
		*error = "only RESP2 arrays are accepted";
		return -1;
	}

	status = parse_decimal_line(buffer, length, &position, &nargs, error);
	if (status <= 0)
		return status;
	if (nargs <= 0 || nargs > PGLC_RESP_MAX_ARGS)
	{
		*error = "invalid argument count";
		return -1;
	}

	for (i = 0; i < nargs; i++)
	{
		int64		argument_length;

		if (position >= length)
			return 0;
		if (buffer[position++] != '$')
		{
			*error = "command arguments must be bulk strings";
			return -1;
		}

		status = parse_decimal_line(buffer, length, &position,
									&argument_length, error);
		if (status <= 0)
			return status;
		if (argument_length < 0 || argument_length > PGLC_REQUEST_MAX)
		{
			*error = "invalid bulk string length";
			return -1;
		}
		if ((uint64) position + (uint64) argument_length + 2 >
			(uint64) length)
			return 0;

		args[i].data = buffer + position;
		args[i].len = (Size) argument_length;
		position += (Size) argument_length;

		if (buffer[position] != '\r' || buffer[position + 1] != '\n')
		{
			*error = "bulk string is not terminated by CRLF";
			return -1;
		}
		position += 2;
	}

	*argc = (int) nargs;
	*consumed = position;
	return 1;
}

bool
pglc_resp_arg_equals(const PgLocalCacheRespArg *arg, const char *literal)
{
	Size		literal_length = strlen(literal);

	return arg->len == literal_length &&
		pg_strncasecmp(arg->data, literal, literal_length) == 0;
}

static char *
line_response(char prefix, const char *message, Size *length)
{
	Size		message_length = strlen(message);
	char	   *response = palloc(message_length + 4);
	Size		i;

	response[0] = prefix;
	for (i = 0; i < message_length; i++)
	{
		char		ch = message[i];

		response[i + 1] = (ch == '\r' || ch == '\n') ? ' ' : ch;
	}
	response[message_length + 1] = '\r';
	response[message_length + 2] = '\n';
	response[message_length + 3] = '\0';
	*length = message_length + 3;
	return response;
}

char *
pglc_resp_simple(const char *message, Size *length)
{
	return line_response('+', message, length);
}

char *
pglc_resp_error(const char *message, Size *length)
{
	return line_response('-', message, length);
}

char *
pglc_resp_integer(int64 value, Size *length)
{
	char		number[64];
	int			number_length;
	char	   *response;

	number_length = snprintf(number, sizeof(number), INT64_FORMAT, value);
	response = palloc((Size) number_length + 4);
	response[0] = ':';
	memcpy(response + 1, number, number_length);
	response[number_length + 1] = '\r';
	response[number_length + 2] = '\n';
	response[number_length + 3] = '\0';
	*length = (Size) number_length + 3;
	return response;
}

char *
pglc_resp_bulk(const char *value, Size value_len, Size *length)
{
	char		header[64];
	int			header_length;
	char	   *response;

	header_length = snprintf(header, sizeof(header), "$%zu\r\n", value_len);
	response = palloc((Size) header_length + value_len + 3);
	memcpy(response, header, header_length);
	if (value_len > 0)
		memcpy(response + header_length, value, value_len);
	response[header_length + value_len] = '\r';
	response[header_length + value_len + 1] = '\n';
	response[header_length + value_len + 2] = '\0';
	*length = (Size) header_length + value_len + 2;
	return response;
}

char *
pglc_resp_null(Size *length)
{
	char	   *response = pstrdup("$-1\r\n");

	*length = 5;
	return response;
}
