EXTENSION = pg_local_cache
MODULE_big = pg_local_cache

OBJS = src/pg_local_cache.o src/pg_local_cache_sql.o \
	src/pg_local_cache_worker.o src/resp.o src/key_codec.o \
	src/row_payload.o

DATA = sql/pg_local_cache--1.0.0.sql \
	sql/pg_local_cache--1.1.0.sql \
	sql/pg_local_cache--1.2.0.sql \
	sql/pg_local_cache--1.2.1.sql \
	sql/pg_local_cache--1.3.0.sql \
	sql/pg_local_cache--1.0.0--1.1.0.sql \
	sql/pg_local_cache--1.1.0--1.2.0.sql \
	sql/pg_local_cache--1.2.0--1.2.1.sql \
	sql/pg_local_cache--1.2.1--1.3.0.sql
PGFILEDESC = "pg_local_cache - transaction-aware primary-key row cache"
EXTRA_CLEAN = tests/unit/resp_test tests/unit/resp_test_sanitized

PG_CPPFLAGS = -I$(srcdir)/src
SHLIB_LINK =

STANDALONE_GOALS = verify-static source-test source-sanitize benchmark-test \
	benchmark pgxn-check pgxn-dist dist version-next version-bump
ifneq ($(strip $(MAKECMDGOALS)),)
ifeq ($(strip $(filter-out $(STANDALONE_GOALS),$(MAKECMDGOALS))),)
SKIP_PGXS = 1
endif
endif

ifndef SKIP_PGXS
PGLC_BUILD_ID_RESOLVED := $(strip $(PGLC_BUILD_ID))
ifeq ($(PGLC_BUILD_ID_RESOLVED),)
ifneq ($(wildcard BUILD-ID),)
PGLC_BUILD_ID_RESOLVED := $(strip $(shell sed -n '1p' BUILD-ID))
else
PGLC_BUILD_ID_RESOLVED := $(strip $(shell test -z "$$(git status --porcelain --untracked-files=all 2>/dev/null)" && git rev-parse --verify HEAD 2>/dev/null))
endif
endif
ifeq ($(shell printf '%s\n' '$(PGLC_BUILD_ID_RESOLVED)' | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$$' && printf yes),)
$(error supply a trustworthy PGLC_BUILD_ID, BUILD-ID, or build from a clean Git checkout)
endif
PG_CPPFLAGS += -DPGLC_BUILD_ID='"$(PGLC_BUILD_ID_RESOLVED)"'

PG_CONFIG ?= pg_config
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)
endif

.PHONY: verify-static source-test source-sanitize benchmark-test \
	integration benchmark docker-smoke pgxn-check pgxn-dist dist \
	version-next version-bump

verify-static:
	python3 -m py_compile benchmarks/compare.py benchmarks/scenarios.py \
		benchmarks/whole_row.py benchmarks/sql_only.py \
		scripts/auto_version.py scripts/build_pgxn_dist.py \
		scripts/release_archive.py \
		scripts/validate_pgxn_meta.py \
		scripts/validate_benchmark_evidence.py \
		tests/cache_contract_test.py \
		tests/monitoring_contract_test.py \
		tests/pgxn_contract_test.py \
		tests/release_version_test.py \
		tests/sql_counter_contract_test.py \
		tests/row_payload_contract_test.py \
		tests/whole_row_benchmark_test.py \
		tests/sql_api_test.py tests/sql_only_benchmark_test.py \
		tests/release_evidence_test.py \
		tests/release_matrix_contract_test.py \
		tests/worker_kvik_contract_test.py \
		tests/whole_row_integration.py tests/pipeline_integration.py \
		tests/oom_monitoring_integration.py \
		tests/sql_mget_integration.py
	bash -n docker/entrypoint.sh docker/healthcheck.sh docker/attach-table.sh \
		docker/initdb/010_pg_local_cache.sh tests/docker_smoke.sh \
		tests/docker_sql_only_smoke.sh \
		tests/compatibility_matrix.sh \
		monitoring/postgres/provision-monitor.sh \
		benchmarks/run.sh scripts/install-existing.sh scripts/fetch-release.sh \
		scripts/install-latest.sh
	python3 -m json.tool \
		monitoring/grafana/dashboards/pg-local-cache.json >/dev/null
	python3 scripts/validate_pgxn_meta.py

source-test:
	$(MAKE) -C tests/unit check
	python3 -m unittest -v tests/cache_contract_test.py \
		tests/monitoring_contract_test.py tests/pgxn_contract_test.py \
		tests/release_version_test.py \
		tests/sql_counter_contract_test.py \
		tests/row_payload_contract_test.py \
		tests/sql_api_test.py tests/worker_kvik_contract_test.py \
		tests/installer_release_contract_test.py tests/pages_contract_test.py \
		tests/release_evidence_test.py \
		tests/release_matrix_contract_test.py

source-sanitize:
	$(MAKE) -C tests/unit sanitize

benchmark-test:
	python3 -m unittest -v tests/whole_row_benchmark_test.py \
		tests/sql_only_benchmark_test.py

integration:
	python3 tests/whole_row_integration.py

benchmark:
	bash benchmarks/run.sh

docker-smoke:
	bash tests/docker_smoke.sh

pgxn-check:
	python3 scripts/validate_pgxn_meta.py

pgxn-dist: pgxn-check
	python3 scripts/build_pgxn_dist.py --output-dir dist

dist: pgxn-dist

version-next:
	python3 scripts/auto_version.py --print-next-version

version-bump:
	python3 scripts/auto_version.py --write
