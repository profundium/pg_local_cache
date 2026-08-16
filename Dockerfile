# syntax=docker/dockerfile:1.7

ARG POSTGRES_MAJOR=16
ARG POSTGRES_VARIANT=bookworm

FROM postgres:${POSTGRES_MAJOR}-${POSTGRES_VARIANT} AS builder

ARG POSTGRES_MAJOR
ARG POSTGRES_VARIANT
ARG PGLC_BUILD_ID

RUN case "$POSTGRES_MAJOR" in \
        14|15|16|17|18) ;; \
        *) printf 'unsupported PostgreSQL major: %s\n' "$POSTGRES_MAJOR" >&2; exit 1 ;; \
    esac \
    && case "$POSTGRES_VARIANT" in \
        bookworm|alpine3.23) ;; \
        *) printf 'unsupported PostgreSQL variant: %s\n' "$POSTGRES_VARIANT" >&2; exit 1 ;; \
    esac

RUN case "$POSTGRES_VARIANT" in \
        bookworm) \
            apt-get update \
            && apt-get install --yes --no-install-recommends \
                build-essential python3 \
                "postgresql-server-dev-${POSTGRES_MAJOR}" \
            && rm -rf /var/lib/apt/lists/* \
            && printf '%s\n' "/usr/lib/postgresql/${POSTGRES_MAJOR}/bin/pg_config" \
                >/tmp/pg_config ;; \
        alpine3.23) \
            apk add --no-cache bash build-base python3 \
            && printf '%s\n' /usr/local/bin/pg_config >/tmp/pg_config ;; \
    esac

WORKDIR /build

COPY Makefile pg_local_cache.control ./
COPY sql/ ./sql/
COPY src/ ./src/

RUN case "$PGLC_BUILD_ID" in \
        ''|*[!A-Za-z0-9._:-]*) printf 'invalid PGLC_BUILD_ID\n' >&2; exit 1 ;; \
    esac \
    && printf '%s\n' "$PGLC_BUILD_ID" > BUILD-ID \
    && pg_config="$(cat /tmp/pg_config)" \
    && installed_version="$("$pg_config" --version)" \
    && case "$installed_version" in \
        "PostgreSQL ${POSTGRES_MAJOR}."*) ;; \
        *) printf 'PostgreSQL major mismatch: expected %s, got: %s\n' \
            "$POSTGRES_MAJOR" "$installed_version" >&2; exit 1 ;; \
    esac \
    && make PG_CONFIG="$pg_config" PGLC_BUILD_ID="$PGLC_BUILD_ID" with_llvm=no clean \
    && make -j"$(nproc)" PG_CONFIG="$pg_config" PGLC_BUILD_ID="$PGLC_BUILD_ID" with_llvm=no \
    && make PG_CONFIG="$pg_config" PGLC_BUILD_ID="$PGLC_BUILD_ID" with_llvm=no DESTDIR=/stage install \
    && install -d /stage/extension/lib /stage/extension/share/extension \
    && install -m 0644 BUILD-ID /stage/extension/BUILD-ID \
    && install -m 0755 \
        "/stage$($pg_config --pkglibdir)/pg_local_cache.so" \
        /stage/extension/lib/pg_local_cache.so \
    && install -m 0644 \
        "/stage$($pg_config --sharedir)/extension/pg_local_cache.control" \
        /stage/extension/share/extension/pg_local_cache.control \
    && for sql_file in "/stage$($pg_config --sharedir)/extension/pg_local_cache--"*.sql; do \
        install -m 0644 "$sql_file" /stage/extension/share/extension/; \
    done

FROM postgres:${POSTGRES_MAJOR}-${POSTGRES_VARIANT} AS extension

ARG POSTGRES_MAJOR
ARG POSTGRES_VARIANT

COPY --from=builder /stage/extension/ /tmp/pg_local_cache_extension/

RUN case "$POSTGRES_VARIANT" in \
        bookworm) \
            pkglibdir="/usr/lib/postgresql/${POSTGRES_MAJOR}/lib"; \
            sharedir="/usr/share/postgresql/${POSTGRES_MAJOR}" ;; \
        alpine3.23) \
            apk add --no-cache bash su-exec; \
            pkglibdir=/usr/local/lib/postgresql; \
            sharedir=/usr/local/share/postgresql ;; \
    esac \
    && install -d "$pkglibdir" "$sharedir/extension" \
    && install -m 0755 /tmp/pg_local_cache_extension/lib/pg_local_cache.so \
        "$pkglibdir/pg_local_cache.so" \
    && install -m 0644 \
        /tmp/pg_local_cache_extension/share/extension/pg_local_cache.control \
        "$sharedir/extension/pg_local_cache.control" \
    && for sql_file in /tmp/pg_local_cache_extension/share/extension/pg_local_cache--*.sql; do \
        install -m 0644 "$sql_file" "$sharedir/extension/"; \
    done \
    && rm -rf /tmp/pg_local_cache_extension

# The `extension` stage preserves the upstream PostgreSQL entrypoint and adds
# only the extension files. The `runtime` stage adds this repository's
# new-cluster entrypoint, health check, and table-attachment helper.
FROM extension AS runtime

COPY --chmod=0755 docker/entrypoint.sh \
    /usr/local/bin/pg_local_cache_entrypoint
COPY --chmod=0755 docker/healthcheck.sh \
    /usr/local/bin/pg_local_cache_healthcheck
COPY --chmod=0755 docker/attach-table.sh \
    /usr/local/bin/pg_local_cache_attach
COPY --chmod=0755 docker/initdb/010_pg_local_cache.sh \
    /docker-entrypoint-initdb.d/010_pg_local_cache.sh

RUN install -d -o postgres -g postgres -m 0700 /run/pg_local_cache

ENV PG_LOCAL_CACHE_ROLE=local_cache_worker \
    PG_LOCAL_CACHE_BIND_ADDRESS=0.0.0.0 \
    PG_LOCAL_CACHE_PORT=6380 \
    PG_LOCAL_CACHE_WORKERS=8 \
    PG_LOCAL_CACHE_CACHE_ENTRIES=65536 \
    PG_LOCAL_CACHE_RELATION_STATES=1024 \
    PG_LOCAL_CACHE_MAX_CLIENTS=512 \
    PG_LOCAL_CACHE_MAX_CLIENTS_PER_WORKER=64 \
    PG_LOCAL_CACHE_MEMORY_BUDGET_MB=1024 \
    PG_LOCAL_CACHE_MAX_WORKER_PROCESSES=16 \
    PG_LOCAL_CACHE_IDLE_TIMEOUT_MS=300000 \
    PG_LOCAL_CACHE_STATEMENT_TIMEOUT_MS=2000 \
    PG_LOCAL_CACHE_LOCK_TIMEOUT_MS=250 \
    PG_LOCAL_CACHE_SINGLEFLIGHT_WAIT_MS=25 \
    PG_LOCAL_CACHE_MAX_PIPELINE_COMMANDS=256 \
    PG_LOCAL_CACHE_MAX_DIRTY_KEYS=4096 \
    PG_LOCAL_CACHE_AUTH_TOKEN_FILE=/run/secrets/pg_local_cache_auth_token

EXPOSE 5432 6380

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=6 \
    CMD ["/usr/local/bin/pg_local_cache_healthcheck"]

ENTRYPOINT ["/usr/local/bin/pg_local_cache_entrypoint"]
CMD ["postgres"]
