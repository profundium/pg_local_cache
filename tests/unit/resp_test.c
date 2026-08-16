#include <inttypes.h>
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "resp.h"

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
check_bytes(const char *actual, Size actual_length,
			const char *expected, Size expected_length)
{
	CHECK(actual_length == expected_length);
	CHECK(memcmp(actual, expected, expected_length) == 0);
	CHECK(actual[actual_length] == '\0');
}

static int
parse(const char *input, Size input_length, PgLocalCacheRespArg *args,
	  int *argc, Size *consumed, const char **error)
{
	return pglc_resp_parse(input, input_length, args, argc, consumed, error);
}

static void
test_complete_requests(void)
{
	const char *request =
		"*2\r\n$4\r\nMGET\r\n$10\r\nusers:0042\r\n"
		"*1\r\n$4\r\nPING\r\n";
	PgLocalCacheRespArg args[PGLC_RESP_MAX_ARGS];
	const char *error;
	Size		consumed;
	int			argc;
	int			status;

	status = parse(request, strlen(request), args, &argc, &consumed, &error);
	CHECK(status == 1);
	CHECK(error == NULL);
	CHECK(argc == 2);
	CHECK(consumed == strlen("*2\r\n$4\r\nMGET\r\n$10\r\nusers:0042\r\n"));
	CHECK(pglc_resp_arg_equals(&args[0], "mget"));
	CHECK(args[1].len == 10);
	CHECK(memcmp(args[1].data, "users:0042", 10) == 0);

	status = parse(request + consumed, strlen(request) - consumed,
				   args, &argc, &consumed, &error);
	CHECK(status == 1);
	CHECK(argc == 1);
	CHECK(pglc_resp_arg_equals(&args[0], "ping"));
}

static void
test_binary_and_empty_bulk_strings(void)
{
	static const char request[] =
		"*3\r\n$3\r\nSET\r\n$0\r\n\r\n$5\r\nx\0\r\ny\r\n";
	PgLocalCacheRespArg args[PGLC_RESP_MAX_ARGS];
	const char *error;
	Size		consumed;
	int			argc;

	CHECK(parse(request, sizeof(request) - 1, args, &argc, &consumed,
				&error) == 1);
	CHECK(error == NULL);
	CHECK(argc == 3);
	CHECK(args[1].len == 0);
	CHECK(args[2].len == 5);
	CHECK(memcmp(args[2].data, "x\0\r\ny", 5) == 0);
	CHECK(consumed == sizeof(request) - 1);
}

static void
test_all_valid_prefixes_are_incomplete(void)
{
	const char *request = "*2\r\n$4\r\nMGET\r\n$5\r\nns:k1\r\n";
	Size		length = strlen(request);
	Size		prefix;

	for (prefix = 0; prefix < length; prefix++)
	{
		PgLocalCacheRespArg args[PGLC_RESP_MAX_ARGS];
		const char *error = "not reset";
		Size		consumed = SIZE_MAX;
		int			argc = -1;
		int			status;

		status = parse(request, prefix, args, &argc, &consumed, &error);
		CHECK(status == 0);
		CHECK(error == NULL);
		CHECK(argc == 0);
		CHECK(consumed == 0);
	}
}

typedef struct MalformedCase
{
	const char *input;
	const char *expected_error;
} MalformedCase;

static void
test_malformed_requests(void)
{
	static const MalformedCase cases[] = {
		{"PING\r\n", "only RESP2 arrays are accepted"},
		{"*0\r\n", "invalid argument count"},
		{"*-1\r\n", "invalid argument count"},
		{"*999999\r\n", "invalid argument count"},
		{"*x\r\n", "invalid decimal length"},
		{"*\r\n", "empty decimal length"},
		{"*1\n", "invalid decimal length"},
		{"*9223372036854775808\r\n", "decimal length overflow"},
		{"*1\r\n+4\r\nMGET\r\n", "command arguments must be bulk strings"},
		{"*1\r\n$-1\r\n", "invalid bulk string length"},
		{"*1\r\n$65537\r\n", "invalid bulk string length"},
		{"*1\r\n$x\r\n", "invalid decimal length"},
		{"*1\r\n$\r\n", "empty decimal length"},
		{"*1\r\n$4\r\nMGET\n\n", "bulk string is not terminated by CRLF"},
	};
	Size		i;

	for (i = 0; i < sizeof(cases) / sizeof(cases[0]); i++)
	{
		PgLocalCacheRespArg args[PGLC_RESP_MAX_ARGS];
		const char *error = NULL;
		Size		consumed = SIZE_MAX;
		int			argc = -1;

		CHECK(parse(cases[i].input, strlen(cases[i].input), args,
					&argc, &consumed, &error) == -1);
		CHECK(error != NULL);
		CHECK(strcmp(error, cases[i].expected_error) == 0);
		CHECK(argc == 0);
		CHECK(consumed == 0);
	}
}

static void
test_argument_boundaries(void)
{
	char	   *request;
	char	   *cursor;
	char		header[64];
	Size		request_length;
	PgLocalCacheRespArg args[PGLC_RESP_MAX_ARGS];
	const char *error;
	Size		consumed;
	int			argc;
	int			i;

	snprintf(header, sizeof(header), "*%d\r\n", PGLC_RESP_MAX_ARGS);
	request_length = strlen(header) + (Size) PGLC_RESP_MAX_ARGS * 7;
	request = malloc(request_length + 1);
	CHECK(request != NULL);
	cursor = request;
	memcpy(cursor, header, strlen(header));
	cursor += strlen(header);
	for (i = 0; i < PGLC_RESP_MAX_ARGS; i++)
	{
		memcpy(cursor, "$1\r\nx\r\n", strlen("$1\r\nx\r\n"));
		cursor += strlen("$1\r\nx\r\n");
	}
	CHECK((Size) (cursor - request) == request_length);

	CHECK(parse(request, request_length, args, &argc, &consumed,
				&error) == 1);
	CHECK(argc == PGLC_RESP_MAX_ARGS);
	CHECK(consumed == request_length);
	for (i = 0; i < argc; i++)
	{
		CHECK(args[i].len == 1);
		CHECK(args[i].data[0] == 'x');
	}
	free(request);

	snprintf(header, sizeof(header), "*%d\r\n", PGLC_RESP_MAX_ARGS + 1);
	CHECK(parse(header, strlen(header), args, &argc, &consumed,
				&error) == -1);
	CHECK(strcmp(error, "invalid argument count") == 0);

	request_length = strlen("*1\r\n$65536\r\n") + PGLC_REQUEST_MAX + 2;
	request = malloc(request_length);
	CHECK(request != NULL);
	cursor = request;
	memcpy(cursor, "*1\r\n$65536\r\n", strlen("*1\r\n$65536\r\n"));
	cursor += strlen("*1\r\n$65536\r\n");
	memset(cursor, 'v', PGLC_REQUEST_MAX);
	cursor += PGLC_REQUEST_MAX;
	memcpy(cursor, "\r\n", 2);

	CHECK(parse(request, request_length, args, &argc, &consumed,
				&error) == 1);
	CHECK(argc == 1);
	CHECK(args[0].len == PGLC_REQUEST_MAX);
	CHECK(consumed == request_length);
	free(request);
}

static void
test_responses(void)
{
	char	   *response;
	Size		length;
	static const char binary[] = {'a', '\0', '\r', '\n', 'z'};

	response = pglc_resp_simple("OK", &length);
	check_bytes(response, length, "+OK\r\n", 5);
	free(response);

	response = pglc_resp_error("bad\r\nrequest", &length);
	check_bytes(response, length, "-bad  request\r\n", 15);
	free(response);

	response = pglc_resp_integer(0, &length);
	check_bytes(response, length, ":0\r\n", 4);
	free(response);

	response = pglc_resp_integer(INT64_MIN, &length);
	check_bytes(response, length, ":-9223372036854775808\r\n", 23);
	free(response);

	response = pglc_resp_integer(INT64_MAX, &length);
	check_bytes(response, length, ":9223372036854775807\r\n", 22);
	free(response);

	response = pglc_resp_bulk(binary, sizeof(binary), &length);
	CHECK(length == strlen("$5\r\n") + sizeof(binary) + 2);
	CHECK(memcmp(response, "$5\r\n", 4) == 0);
	CHECK(memcmp(response + 4, binary, sizeof(binary)) == 0);
	CHECK(memcmp(response + 4 + sizeof(binary), "\r\n", 2) == 0);
	CHECK(response[length] == '\0');
	free(response);

	response = pglc_resp_bulk("", 0, &length);
	check_bytes(response, length, "$0\r\n\r\n", 6);
	free(response);

	response = pglc_resp_null(&length);
	check_bytes(response, length, "$-1\r\n", 5);
	free(response);
}

static uint32_t
next_random(uint32_t *state)
{
	uint32_t	value = *state;

	value ^= value << 13;
	value ^= value >> 17;
	value ^= value << 5;
	*state = value;
	return value;
}

static void
check_parse_invariants(const char *input, Size input_length)
{
	PgLocalCacheRespArg args[PGLC_RESP_MAX_ARGS];
	const char *error;
	Size		consumed;
	int			argc;
	int			status;
	int			i;
	uintptr_t	start = (uintptr_t) input;
	uintptr_t	end = start + input_length;

	memset(args, 0, sizeof(args));
	status = parse(input, input_length, args, &argc, &consumed, &error);
	CHECK(status >= -1 && status <= 1);
	if (status == 1)
	{
		CHECK(error == NULL);
		CHECK(argc > 0 && argc <= PGLC_RESP_MAX_ARGS);
		CHECK(consumed > 0 && consumed <= input_length);
		for (i = 0; i < argc; i++)
		{
			uintptr_t	arg = (uintptr_t) args[i].data;

			CHECK(args[i].len <= PGLC_REQUEST_MAX);
			CHECK(arg >= start);
			CHECK(arg <= end);
			if (arg >= start && arg <= end)
				CHECK(args[i].len <= (Size) (end - arg));
		}
	}
	else
	{
		CHECK(argc == 0);
		CHECK(consumed == 0);
		CHECK((status == 0 && error == NULL) ||
			  (status == -1 && error != NULL));
	}
}

static void
test_deterministic_fuzz_inputs(void)
{
	uint32_t	state = UINT32_C(0x91e10da5);
	unsigned char buffer[512];
	unsigned int iteration;

	for (iteration = 0; iteration < 20000; iteration++)
	{
		Size		length = next_random(&state) % sizeof(buffer);
		Size		i;

		for (i = 0; i < length; i++)
			buffer[i] = (unsigned char) next_random(&state);
		if (iteration % 3 == 0 && length > 0)
			buffer[0] = '*';
		check_parse_invariants((const char *) buffer, length);
	}

	for (iteration = 0; iteration < 10000; iteration++)
	{
		char		frame[512];
		char	   *cursor = frame;
		int			nargs = 1 + (int) (next_random(&state) % 4);
		int			arg;
		int			written;
		Size		frame_length;
		Size		remaining;

		remaining = (Size) (frame + sizeof(frame) - cursor);
		written = snprintf(cursor, remaining, "*%d\r\n", nargs);
		CHECK(written > 0 && (Size) written < remaining);
		cursor += written;
		for (arg = 0; arg < nargs; arg++)
		{
			Size	payload_length = next_random(&state) % 48;
			Size	i;

			remaining = (Size) (frame + sizeof(frame) - cursor);
			written = snprintf(cursor, remaining, "$%zu\r\n",
							   payload_length);
			CHECK(written > 0 && (Size) written < remaining);
			cursor += written;
			for (i = 0; i < payload_length; i++)
				*cursor++ = (char) next_random(&state);
			*cursor++ = '\r';
			*cursor++ = '\n';
		}
		frame_length = (Size) (cursor - frame);
		check_parse_invariants(frame, frame_length);

		if (iteration % 2 == 0 && frame_length > 0)
		{
			Size	mutation = next_random(&state) % frame_length;

			frame[mutation] ^= (char) (1U << (next_random(&state) % 8));
			check_parse_invariants(frame, frame_length);
		}
		else
		{
			Size	truncated = next_random(&state) % frame_length;

			check_parse_invariants(frame, truncated);
		}
	}
}

int
main(void)
{
	test_complete_requests();
	test_binary_and_empty_bulk_strings();
	test_all_valid_prefixes_are_incomplete();
	test_malformed_requests();
	test_argument_boundaries();
	test_responses();
	test_deterministic_fuzz_inputs();

	printf("resp source tests: %u assertions passed\n", assertions);
	return EXIT_SUCCESS;
}
