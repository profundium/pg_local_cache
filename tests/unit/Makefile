CC ?= cc

CPPFLAGS += -DPGLC_RESP_STANDALONE -I../../src
COMMON_CFLAGS = -std=c11 -O2 -g -Wall -Wextra -Werror -Wpedantic
PG_CONFIG ?= pg_config
PG_SERVER_INCLUDE = $(shell $(PG_CONFIG) --includedir-server)

# PostgreSQL server headers require the same feature-test macros as server code.
PG_SERVER_CPPFLAGS = $(shell $(PG_CONFIG) --cppflags)
ROW_PAYLOAD_CPPFLAGS = $(PG_SERVER_CPPFLAGS) -DPGLC_BUILD_ID='"unit-test"' \
	-I$(PG_SERVER_INCLUDE) -I../../src
ROW_PAYLOAD_CFLAGS = -std=c11 -O2 -g -Wall -Wextra -Werror \
	-Wno-unused-parameter
SOURCES = resp_test.c ../../src/resp.c
FUZZ_SOURCE = ../../fuzz/resp_parse_fuzzer.c
FUZZ_CORPUS = ../../fuzz/corpus/resp_parse
ROW_PAYLOAD_SOURCES = row_payload_test.c ../../src/row_payload.c
TARGET = resp_test
SANITIZED_TARGET = resp_test_sanitized
TARGETS = $(TARGET) row_payload_test
SANITIZED_TARGETS = $(SANITIZED_TARGET) row_payload_test_sanitized
FUZZ_REPLAY_TARGET = resp_parse_replay
FUZZ_REPLAY_SANITIZED_TARGET = resp_parse_replay_sanitized

.PHONY: all check sanitize clean

all: $(TARGETS)

$(TARGET): $(SOURCES) ../../src/resp.h ../../src/resp_limits.h
	$(CC) $(CPPFLAGS) $(COMMON_CFLAGS) $(CFLAGS) $(SOURCES) $(LDFLAGS) -o $@

row_payload_test: $(ROW_PAYLOAD_SOURCES) ../../src/row_payload.h ../../src/pg_local_cache.h
	$(CC) $(ROW_PAYLOAD_CPPFLAGS) $(ROW_PAYLOAD_CFLAGS) $(CFLAGS) \
		$(ROW_PAYLOAD_SOURCES) $(LDFLAGS) -o $@

$(SANITIZED_TARGET): $(SOURCES) ../../src/resp.h ../../src/resp_limits.h
	$(CC) $(CPPFLAGS) $(COMMON_CFLAGS) -O1 \
		-fsanitize=address,undefined -fno-omit-frame-pointer \
		$(CFLAGS) $(SOURCES) $(LDFLAGS) \
		-fsanitize=address,undefined -o $@

row_payload_test_sanitized: $(ROW_PAYLOAD_SOURCES) ../../src/row_payload.h ../../src/pg_local_cache.h
	$(CC) $(ROW_PAYLOAD_CPPFLAGS) $(ROW_PAYLOAD_CFLAGS) -O1 \
		-fsanitize=address,undefined -fno-omit-frame-pointer $(CFLAGS) \
		$(ROW_PAYLOAD_SOURCES) $(LDFLAGS) \
		-fsanitize=address,undefined -o $@

$(FUZZ_REPLAY_TARGET): $(FUZZ_SOURCE) ../../src/resp.c ../../src/resp.h ../../src/resp_limits.h
	$(CC) $(CPPFLAGS) $(COMMON_CFLAGS) -DPGLC_RESP_FUZZ_REPLAY \
		$(FUZZ_SOURCE) ../../src/resp.c $(LDFLAGS) -o $@

$(FUZZ_REPLAY_SANITIZED_TARGET): $(FUZZ_SOURCE) ../../src/resp.c ../../src/resp.h ../../src/resp_limits.h
	$(CC) $(CPPFLAGS) $(COMMON_CFLAGS) -O1 \
		-fsanitize=address,undefined -fno-omit-frame-pointer \
		-DPGLC_RESP_FUZZ_REPLAY $(FUZZ_SOURCE) ../../src/resp.c $(LDFLAGS) \
		-fsanitize=address,undefined -o $@

check: $(TARGETS) $(FUZZ_REPLAY_TARGET)
	./$(TARGET)
	./row_payload_test
	./$(FUZZ_REPLAY_TARGET) $(FUZZ_CORPUS)

sanitize: $(SANITIZED_TARGETS) $(FUZZ_REPLAY_SANITIZED_TARGET)
	ASAN_OPTIONS=$${ASAN_OPTIONS:-detect_leaks=0:strict_string_checks=1} \
	UBSAN_OPTIONS=$${UBSAN_OPTIONS:-print_stacktrace=1:halt_on_error=1} \
	./$(SANITIZED_TARGET)
	ASAN_OPTIONS=$${ASAN_OPTIONS:-detect_leaks=0:strict_string_checks=1} \
	UBSAN_OPTIONS=$${UBSAN_OPTIONS:-print_stacktrace=1:halt_on_error=1} \
	./row_payload_test_sanitized
	ASAN_OPTIONS=$${ASAN_OPTIONS:-detect_leaks=0:strict_string_checks=1} \
	UBSAN_OPTIONS=$${UBSAN_OPTIONS:-print_stacktrace=1:halt_on_error=1} \
	./$(FUZZ_REPLAY_SANITIZED_TARGET) $(FUZZ_CORPUS)

clean:
	rm -f $(TARGETS) $(SANITIZED_TARGETS) $(FUZZ_REPLAY_TARGET) $(FUZZ_REPLAY_SANITIZED_TARGET)
