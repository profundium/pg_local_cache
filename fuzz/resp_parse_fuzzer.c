/* SPDX-License-Identifier: MIT */
#define _POSIX_C_SOURCE 200809L

#include "resp.h"

#include <dirent.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CUT_LIMIT 63

static void
require(int condition, const char *message)
{
	if (!condition)
	{
		fprintf(stderr, "RESP parser invariant failed: %s\n", message);
		abort();
	}
}

static void
validate_success(const uint8_t *data, size_t size,
				 PgLocalCacheRespArg *args, int argc, size_t consumed)
{
	uintptr_t	base = (uintptr_t) data;
	uintptr_t	end;
	int			i;

	require(consumed <= size, "consumed exceeds input size");
	require(consumed > 0 && base <= UINTPTR_MAX - consumed,
			"consumed range is invalid");
	end = base + consumed;
	require(argc >= 1 && argc <= PGLC_RESP_MAX_ARGS,
			"argument count is out of range");

	for (i = 0; i < argc; i++)
	{
		uintptr_t	arg = (uintptr_t) args[i].data;

		require(arg >= base && arg < end, "argument data is outside consumed input");
		require(args[i].len <= end - arg,
				"argument length extends past consumed input");
		require(args[i].len <= PGLC_REQUEST_MAX,
				"argument length exceeds request limit");
	}
}

static int
contains_cut(const size_t *cuts, size_t count, size_t cut)
{
	size_t		i;

	for (i = 0; i < count; i++)
		if (cuts[i] == cut)
			return 1;
	return 0;
}

static void
add_cut(size_t *cuts, size_t *count, size_t cut, size_t consumed)
{
	if (cut > 0 && cut < consumed && *count < CUT_LIMIT &&
		!contains_cut(cuts, *count, cut))
		cuts[(*count)++] = cut;
}

static uint32_t
next_random(uint32_t *state)
{
	uint32_t	x = *state;

	x ^= x << 13;
	x ^= x >> 17;
	x ^= x << 5;
	*state = x;
	return x;
}

static void
check_prefix(const uint8_t *data, size_t cut)
{
	PgLocalCacheRespArg args[PGLC_RESP_MAX_ARGS];
	int			argc = 0;
	size_t		consumed = 0;
	const char *error = NULL;
	int			result;

	result = pglc_resp_parse((const char *) data, cut, args, &argc, &consumed,
							 &error);
	require(result == 0, "strict prefix of complete request was not incomplete");
}

static void
check_input(const uint8_t *data, size_t size, int exhaustive_prefixes)
{
	PgLocalCacheRespArg args[PGLC_RESP_MAX_ARGS];
	int			argc = 0;
	size_t		consumed = 0;
	const char *error = NULL;
	int			result;

	result = pglc_resp_parse((const char *) data, size, args, &argc, &consumed,
							 &error);
	require(result == -1 || result == 0 || result == 1,
				"parser returned an invalid status");
	if (result == -1)
	{
		require(error != NULL, "protocol error has no error string");
		return;
	}
	if (result != 1)
		return;

	validate_success(data, size, args, argc, consumed);
	{
		PgLocalCacheRespArg exact_args[PGLC_RESP_MAX_ARGS];
		int			exact_argc = 0;
		size_t		exact_consumed = 0;
		const char *exact_error = NULL;

	result = pglc_resp_parse((const char *) data, consumed, exact_args,
								 &exact_argc, &exact_consumed, &exact_error);
		require(result == 1 && exact_argc == argc && exact_consumed == consumed,
				"exact-consumed parse differs in status, argc, or consumed length");
		for (int i = 0; i < argc; i++)
		{
			require(exact_args[i].len == args[i].len,
					"exact-consumed parse differs in argument length");
			require(memcmp(exact_args[i].data, args[i].data, args[i].len) == 0,
					"exact-consumed parse differs in argument content");
		}
	}

	check_prefix(data, 0);
	if (exhaustive_prefixes)
	{
		for (size_t cut = 1; cut < consumed; cut++)
			check_prefix(data, cut);
	}
	else
	{
		size_t		cuts[CUT_LIMIT];
		size_t		count = 0;
		size_t		boundaries = 0;
		size_t		boundary_index = 0;
		size_t		stride;
		uint32_t random_state = (uint32_t) (size ^ UINT32_C(0x9e3779b9));

		for (size_t i = 0; i + 1 < consumed; i++)
			if (data[i] == '\r' && data[i + 1] == '\n')
				for (size_t cut = i; cut <= i + 2; cut++)
					if (cut > 0 && cut < consumed)
						boundaries++;
		stride = boundaries > 48 ? (boundaries + 47) / 48 : 1;
		for (size_t i = 0; i + 1 < consumed; i++)
			if (data[i] == '\r' && data[i + 1] == '\n')
				for (size_t cut = i; cut <= i + 2; cut++)
					if (cut > 0 && cut < consumed)
					{
						if (boundary_index % stride == 0)
							add_cut(cuts, &count, cut, consumed);
						boundary_index++;
					}

		for (size_t attempts = 0; count < CUT_LIMIT && attempts < CUT_LIMIT * 8;
			 attempts++)
		{
			if (consumed <= 1)
				break;
			if (random_state == 0)
				random_state = UINT32_C(0x6d2b79f5);
			add_cut(cuts, &count,
					(size_t) (next_random(&random_state) % (consumed - 1)) + 1,
					consumed);
		}
		for (size_t i = 0; i < count; i++)
			check_prefix(data, cuts[i]);
	}
}

int
LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
	check_input(data, size, 0);
	return 0;
}

#ifdef PGLC_RESP_FUZZ_REPLAY
int
main(int argc, char **argv)
{
	const char *directory = argc > 1 ? argv[1] : "../../fuzz/corpus/resp_parse";
	DIR		   *corpus = opendir(directory);
	struct dirent *entry;
	size_t		files = 0;

	if (corpus == NULL)
	{
		perror(directory);
		return 1;
	}
	while ((entry = readdir(corpus)) != NULL)
	{
		char		path[4096];
		int			path_length;
		FILE	   *file;
		long		file_size;
		uint8_t    *input;
		size_t		read_size;

		if (entry->d_name[0] == '.')
			continue;
		path_length = snprintf(path, sizeof(path), "%s/%s", directory,
						  entry->d_name);
		if (path_length < 0 || (size_t) path_length >= sizeof(path))
		{
			fprintf(stderr, "corpus path is too long\n");
			closedir(corpus);
			return 1;
		}
		file = fopen(path, "rb");
		if (file == NULL || fseek(file, 0, SEEK_END) != 0 ||
			(file_size = ftell(file)) < 0 || fseek(file, 0, SEEK_SET) != 0)
		{
			perror(path);
			if (file != NULL)
				fclose(file);
			closedir(corpus);
			return 1;
		}
		input = malloc(file_size > 0 ? (size_t) file_size : 1);
		if (input == NULL)
		{
			perror("malloc");
			fclose(file);
			closedir(corpus);
			return 1;
		}
		read_size = fread(input, 1, (size_t) file_size, file);
		if (read_size != (size_t) file_size || ferror(file))
		{
			perror(path);
			free(input);
			fclose(file);
			closedir(corpus);
			return 1;
		}
		fclose(file);
		check_input(input, read_size, 1);
		free(input);
		files++;
	}
	closedir(corpus);
	if (files == 0)
	{
		fprintf(stderr, "seed corpus is empty: %s\n", directory);
		return 1;
	}
	printf("replayed %zu RESP parser seed files\n", files);
	return 0;
}
#endif
