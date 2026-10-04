CC ?= cc

CPPFLAGS += -DPGLC_RESP_STANDALONE -I../../src
COMMON_CFLAGS = -std=c11 -O2 -g -Wall -Wextra -Werror -Wpedantic
SOURCES = resp_test.c ../../src/resp.c
FUZZ_SOURCE = ../../fuzz/resp_parse_fuzzer.c
FUZZ_CORPUS = ../../fuzz/corpus/resp_parse
TARGET = resp_test
SANITIZED_TARGET = resp_test_sanitized
FUZZ_REPLAY_TARGET = resp_parse_replay
FUZZ_REPLAY_SANITIZED_TARGET = resp_parse_replay_sanitized

.PHONY: all check sanitize clean

all: $(TARGET)

$(TARGET): $(SOURCES) ../../src/resp.h ../../src/resp_limits.h
	$(CC) $(CPPFLAGS) $(COMMON_CFLAGS) $(CFLAGS) $(SOURCES) $(LDFLAGS) -o $@

$(SANITIZED_TARGET): $(SOURCES) ../../src/resp.h ../../src/resp_limits.h
	$(CC) $(CPPFLAGS) $(COMMON_CFLAGS) -O1 \
		-fsanitize=address,undefined -fno-omit-frame-pointer \
		$(CFLAGS) $(SOURCES) $(LDFLAGS) \
		-fsanitize=address,undefined -o $@

$(FUZZ_REPLAY_TARGET): $(FUZZ_SOURCE) ../../src/resp.c ../../src/resp.h ../../src/resp_limits.h
	$(CC) $(CPPFLAGS) $(COMMON_CFLAGS) -DPGLC_RESP_FUZZ_REPLAY \
		$(FUZZ_SOURCE) ../../src/resp.c $(LDFLAGS) -o $@

$(FUZZ_REPLAY_SANITIZED_TARGET): $(FUZZ_SOURCE) ../../src/resp.c ../../src/resp.h ../../src/resp_limits.h
	$(CC) $(CPPFLAGS) $(COMMON_CFLAGS) -O1 \
		-fsanitize=address,undefined -fno-omit-frame-pointer \
		-DPGLC_RESP_FUZZ_REPLAY $(FUZZ_SOURCE) ../../src/resp.c $(LDFLAGS) \
		-fsanitize=address,undefined -o $@

check: $(TARGET) $(FUZZ_REPLAY_TARGET)
	./$(TARGET)
	./$(FUZZ_REPLAY_TARGET) $(FUZZ_CORPUS)

sanitize: $(SANITIZED_TARGET) $(FUZZ_REPLAY_SANITIZED_TARGET)
	ASAN_OPTIONS=$${ASAN_OPTIONS:-detect_leaks=0:strict_string_checks=1} \
	UBSAN_OPTIONS=$${UBSAN_OPTIONS:-print_stacktrace=1:halt_on_error=1} \
	./$(SANITIZED_TARGET)
	ASAN_OPTIONS=$${ASAN_OPTIONS:-detect_leaks=0:strict_string_checks=1} \
	UBSAN_OPTIONS=$${UBSAN_OPTIONS:-print_stacktrace=1:halt_on_error=1} \
	./$(FUZZ_REPLAY_SANITIZED_TARGET) $(FUZZ_CORPUS)

clean:
	rm -f $(TARGET) $(SANITIZED_TARGET) $(FUZZ_REPLAY_TARGET) $(FUZZ_REPLAY_SANITIZED_TARGET)
