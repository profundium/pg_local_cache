PGLC_SRCDIR := $(dir $(firstword $(MAKEFILE_LIST)))

EXTENSION = pg_local_cache
MODULE_big = pg_local_cache

OBJS = src/pg_local_cache.o \
	src/pg_local_cache_worker.o src/resp.o src/key_codec.o \
	src/row_payload.o src/cache_arena.o src/cache_index.o

DATA := $(patsubst $(PGLC_SRCDIR)%,%,$(wildcard $(PGLC_SRCDIR)sql/pg_local_cache--*.sql))
REGRESS = admin
REGRESS_OPTS = --inputdir=$(PGLC_SRCDIR)test
PGFILEDESC = "pg_local_cache - transaction-aware primary-key row cache"
EXTRA_CLEAN = tests/unit/resp_test tests/unit/resp_test_sanitized \
	tests/unit/row_payload_test tests/unit/row_payload_test_sanitized \
	tests/unit/cache_arena_test tests/unit/cache_arena_test_sanitized \
	tests/unit/cache_index_test tests/unit/cache_index_test_sanitized \
	tests/unit/key_codec_test tests/unit/key_codec_test_sanitized \
	tests/unit/resp_parse_replay tests/unit/resp_parse_replay_sanitized

PG_CPPFLAGS = -I$(srcdir)/src
SHLIB_LINK =

STANDALONE_GOALS = verify-static source-test source-sanitize
ifneq ($(strip $(MAKECMDGOALS)),)
ifeq ($(strip $(filter-out $(STANDALONE_GOALS),$(MAKECMDGOALS))),)
SKIP_PGXS = 1
endif
endif

ifndef SKIP_PGXS
PGLC_DEFAULT_VERSION := $(strip $(shell sed -n "s/^default_version = '\([^']*\)'$$/\1/p" "$(PGLC_SRCDIR)pg_local_cache.control"))
# BUILD-ID holds the commit hash in git archives (export-subst), the raw
# placeholder in a checkout.
PGLC_BUILD_ID_FILE := $(strip $(shell sed -n '1{/^\$$Format/!p;}' "$(PGLC_SRCDIR)BUILD-ID" 2>/dev/null))
PGLC_BUILD_ID_RESOLVED := $(strip $(PGLC_BUILD_ID))
ifeq ($(PGLC_BUILD_ID_RESOLVED),)
ifneq ($(PGLC_BUILD_ID_FILE),)
PGLC_BUILD_ID_RESOLVED := $(PGLC_BUILD_ID_FILE)
else
PGLC_BUILD_ID_RESOLVED := $(strip $(shell git rev-parse --short=12 HEAD 2>/dev/null))
ifneq ($(PGLC_BUILD_ID_RESOLVED),)
ifneq ($(strip $(shell git status --porcelain 2>/dev/null)),)
PGLC_BUILD_ID_RESOLVED := $(PGLC_BUILD_ID_RESOLVED)-dirty
endif
endif
endif
endif
ifeq ($(PGLC_BUILD_ID_RESOLVED),)
PGLC_BUILD_ID_RESOLVED := $(PGLC_DEFAULT_VERSION)
endif
ifeq ($(shell printf '%s\n' '$(PGLC_BUILD_ID_RESOLVED)' | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$$' && printf yes),)
$(warning invalid PGLC_BUILD_ID; using default_version '$(PGLC_DEFAULT_VERSION)')
PGLC_BUILD_ID_RESOLVED := $(PGLC_DEFAULT_VERSION)
endif
PG_CPPFLAGS += -DPGLC_BUILD_ID='"$(PGLC_BUILD_ID_RESOLVED)"'
ifeq ($(PGLC_TEST_HOOKS),1)
PG_CPPFLAGS += -DPGLC_TEST_HOOKS
endif

PG_CONFIG ?= pg_config
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)
ifeq ($(with_ssl),openssl)
SHLIB_LINK += -lssl -lcrypto
endif
endif

.PHONY: verify-static source-test source-sanitize integration docker-smoke

verify-static:
	python3 -m compileall -q scripts tests
	bash -n docker/entrypoint.sh docker/healthcheck.sh docker/attach-table.sh \
		docker/initdb/010_pg_local_cache.sh tests/docker_smoke.sh \
		scripts/bump-version.sh
	scripts/bump-version.sh --check

source-test:
	$(MAKE) -C tests/unit check
	python3 -m unittest -v tests/bump_version_test.py

source-sanitize:
	$(MAKE) -C tests/unit sanitize

integration:
	python3 tests/whole_row_integration.py

docker-smoke:
	bash tests/docker_smoke.sh
