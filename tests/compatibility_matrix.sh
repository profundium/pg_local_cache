#!/usr/bin/env bash
set -Eeuo pipefail

repository_directory="$(
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
    pwd
)"

parse_matrix_values() {
    local name="$1"
    local allowed="$2"
    local raw="$3"
    local value
    local -a values=()

    read -r -a values <<<"${raw//,/ }"
    (( ${#values[@]} > 0 )) || {
        printf '%s must select at least one value\n' "$name" >&2
        return 1
    }
    for value in "${values[@]}"; do
        [[ " $allowed " == *" $value "* ]] || {
            printf 'unsupported %s value: %s\n' "$name" "$value" >&2
            return 1
        }
        printf '%s\n' "$value"
    done
}

majors_output="$(
    parse_matrix_values \
        PGLC_MATRIX_MAJORS \
        "14 15 16 17 18" \
        "${PGLC_MATRIX_MAJORS:-14,15,16,17,18}"
)"
variants_output="$(
    parse_matrix_values \
        PGLC_MATRIX_VARIANTS \
        "bookworm alpine3.23" \
        "${PGLC_MATRIX_VARIANTS:-bookworm,alpine3.23}"
)"
mapfile -t majors <<<"$majors_output"
mapfile -t variants <<<"$variants_output"

version="$(sed -n "s/^default_version = '\([^']*\)'$/\1/p" "$repository_directory/pg_local_cache.control")"
build_id="$(git -C "$repository_directory" rev-parse --verify HEAD)"
epoch="$(git -C "$repository_directory" show -s --format=%ct "$build_id")"
results_directory="${PGLC_MATRIX_RESULTS_DIR:-${repository_directory}/.supergoal/improve-extension-user-devex-download-he-4khgHD/evidence/release-archive-smoke-results}"
temporary_directory="$(mktemp -d "${TMPDIR:-/tmp}/pg_local_cache_matrix.XXXXXX")"
install -d -m 0700 "$results_directory"
pgxn_build_container=""
pgxn_runtime_container=""
artifact_smoke_container=""

cleanup() {
    [[ -z "$pgxn_build_container" ]] || docker rm -fv "$pgxn_build_container" >/dev/null 2>&1 || true
    [[ -z "$pgxn_runtime_container" ]] || docker rm -fv "$pgxn_runtime_container" >/dev/null 2>&1 || true
    [[ -z "$artifact_smoke_container" ]] || docker rm -fv "$artifact_smoke_container" >/dev/null 2>&1 || true
    rm -rf -- "$temporary_directory"
}
trap cleanup EXIT

build_release_archive() {
    local major="$1" variant="$2" libc="$3" lane="$4"
    local image="pg_local_cache-contracts:${lane}"
    local stage="$temporary_directory/${lane}-stage"
    local root_name="pg_local_cache-${version}-pg${major}-linux-${libc}-amd64"
    local root="$stage/$root_name"
    local archive="$results_directory/${root_name}.tar.gz"
    local extracted="$results_directory/${lane}-extracted"
    local identity="$results_directory/${lane}.identity.json"
    local container pkglibdir sharedir

    docker build --platform linux/amd64 --target extension \
        --build-arg "POSTGRES_MAJOR=$major" \
        --build-arg "POSTGRES_VARIANT=$variant" \
        --build-arg "PGLC_BUILD_ID=$build_id" \
        --tag "$image" "$repository_directory"
    pkglibdir="$(docker run --rm --platform linux/amd64 --entrypoint /bin/sh "$image" -ec 'pg_config --pkglibdir')"
    sharedir="$(docker run --rm --platform linux/amd64 --entrypoint /bin/sh "$image" -ec 'pg_config --sharedir')"
    install -d "$root/lib" "$root/share/extension" "$root/docs" "$temporary_directory/${lane}-sql"
    container="$(docker create --platform linux/amd64 "$image")"
    docker cp "$container:${pkglibdir}/pg_local_cache.so" "$root/lib/"
    docker cp "$container:${sharedir}/extension/." "$temporary_directory/${lane}-sql/"
    docker rm -fv "$container" >/dev/null
    install -m 0644 "$temporary_directory/${lane}-sql/pg_local_cache.control" "$root/share/extension/"
    find "$temporary_directory/${lane}-sql" -maxdepth 1 -type f -name 'pg_local_cache--*.sql' \
        -exec install -m 0644 {} "$root/share/extension/" \;
    install -m 0755 "$repository_directory/scripts/install-existing.sh" "$root/install.sh"
    install -m 0644 "$repository_directory/README.md" "$repository_directory/LICENSE" "$root/"
    install -m 0644 "$repository_directory/docs/INSTALL_EXISTING.md" \
        "$repository_directory/docs/BENCHMARKS.md" \
        "$repository_directory/docs/MONITORING.md" \
        "$repository_directory/docs/TECHNICAL.md" "$root/docs/"
    printf '%s\n' "$build_id" > "$root/BUILD-ID"
    {
        printf 'format=1\nversion=%s\npostgres_major=%s\nos=linux\n' "$version" "$major"
        printf 'libc=%s\narchitecture=amd64\ncommit=%s\n' "$libc" "$build_id"
        printf 'build_image=postgres:%s-%s\n' "$major" "$variant"
    } > "$root/RELEASE-METADATA"
    python3 -B "$repository_directory/scripts/release_archive.py" build \
        --stage "$stage" --root "$root_name" --output "$archive" --epoch "$epoch"
    python3 -B "$repository_directory/scripts/release_archive.py" inspect \
        --archive "$archive" --root "$root_name" --extract-dir "$extracted" \
        --identity-out "$identity" | tee "$results_directory/${lane}.members.txt"
    printf '%s\n' "$extracted/$root_name"
}

artifact_preflight_smoke() {
    local major="$1" variant="$2" lane="$3" release_root="$4"
    local image="pg_local_cache-contracts:${lane}"
    local before after

    docker run --detach --name "$artifact_smoke_container" --platform linux/amd64 \
        --network none --env POSTGRES_PASSWORD=artifact-test \
        --env POSTGRES_DB=app "$image" \
        postgres -c shared_preload_libraries=pg_local_cache \
        -c pg_local_cache.database=app -c pg_local_cache.port=0 >/dev/null
    for _attempt in {1..60}; do
        if [[ "$(docker logs "$artifact_smoke_container" 2>&1)" == \
            *'PostgreSQL init process complete; ready for start up.'* ]] \
            && docker exec "$artifact_smoke_container" pg_isready \
            --username postgres --dbname app >/dev/null 2>&1; then
            break
        fi
        sleep 0.2
    done
    docker exec "$artifact_smoke_container" pg_isready \
        --username postgres --dbname app >/dev/null
    docker cp "$release_root/." "$artifact_smoke_container:/artifact/"
    docker exec --user root "$artifact_smoke_container" chown -R 0:0 /artifact
    if [[ "$variant" == alpine3.23 ]]; then
        docker exec --user root "$artifact_smoke_container" sh -ec \
            '! command -v runuser >/dev/null 2>&1 && command -v su-exec >/dev/null'
    fi
    before="$(
        docker exec --user root "$artifact_smoke_container" sh -ec \
            'sha256sum "$PGDATA/postgresql.auto.conf" "$(pg_config --pkglibdir)/pg_local_cache.so" "$(pg_config --sharedir)/extension/pg_local_cache.control"'
    )"
    docker exec --user root "$artifact_smoke_container" /artifact/install.sh --help >/dev/null
    docker exec --user root "$artifact_smoke_container" \
        /artifact/install.sh preflight --database app --mode sql-only \
        --postgres-os-user postgres --pg-config pg_config --psql psql
    docker exec --user root "$artifact_smoke_container" \
        /artifact/install.sh install --database app --mode sql-only \
        --postgres-os-user postgres --pg-config pg_config --psql psql --dry-run
    after="$(
        docker exec --user root "$artifact_smoke_container" sh -ec \
            'sha256sum "$PGDATA/postgresql.auto.conf" "$(pg_config --pkglibdir)/pg_local_cache.so" "$(pg_config --sharedir)/extension/pg_local_cache.control"'
    )"
    [[ "$after" == "$before" ]]
    docker rm -fv "$artifact_smoke_container" >/dev/null
    artifact_smoke_container=""
    printf 'artifact preflight smoke passed (PostgreSQL %s-%s)\n' "$major" "$variant"
}

for major in "${majors[@]}"; do
    for variant in "${variants[@]}"; do
        case "$variant" in bookworm) libc=glibc ;; alpine3.23) libc=musl ;; esac
        lane="pg${major}-${libc}"
        if grep -Fqx "lane=${lane} status=PASS" \
            <(cut -d' ' -f1-2 "$results_directory/${lane}.result.txt" 2>/dev/null); then
            printf 'compatibility smoke: %s already PASS\n' "$lane"
            continue
        fi
        rm -rf -- "$results_directory/${lane}-extracted"
        rm -f -- "$results_directory/${lane}."* \
            "$results_directory/pg_local_cache-${version}-pg${major}-linux-${libc}-amd64.tar.gz"
        printf 'compatibility smoke: PostgreSQL %s-%s\n' "$major" "$variant"
        extracted_root="$(build_release_archive "$major" "$variant" "$libc" "$lane" | tee "$results_directory/${lane}.archive.txt" | tail -n 1)"
        if [[ "$major" == 16 ]]; then
            DOCKER_DEFAULT_PLATFORM=linux/amd64 \
            PGLC_RELEASE_ROOT="$extracted_root" \
            POSTGRES_MAJOR="$major" POSTGRES_VARIANT="$variant" \
                bash "${repository_directory}/tests/docker_smoke.sh" \
                | tee "$results_directory/${lane}.smoke.txt"
        else
            artifact_smoke_container="pg_local_cache_artifact_${lane}_$$"
            artifact_preflight_smoke "$major" "$variant" "$lane" "$extracted_root" \
                | tee "$results_directory/${lane}.smoke.txt"
            artifact_smoke_container=""
        fi
        archive="$results_directory/pg_local_cache-${version}-pg${major}-linux-${libc}-amd64.tar.gz"
        python3 -B "$repository_directory/scripts/release_archive.py" verify-identity \
            --archive "$archive" --identity "$results_directory/${lane}.identity.json" \
            | tee "$results_directory/${lane}.identity-verified.txt"
        sha256="$(sha256sum "$archive" | awk '{print $1}')"
        printf 'lane=%s status=PASS archive=%s sha256=%s extracted_root=%s\n' \
            "$lane" "$archive" "$sha256" "$extracted_root" \
            | tee "$results_directory/${lane}.result.txt"
        docker image rm "pg_local_cache-contracts:${lane}" \
            "pg_local_cache:${version}" >/dev/null 2>&1 || true
    done
done

source_archive="$results_directory/pg_local_cache-source.tar.gz"
rm -rf -- "$results_directory/source-extracted"
rm -f -- "$source_archive" "$results_directory/source.identity.json" \
    "$results_directory/source.members.txt"
source_stage="$temporary_directory/source-stage"
source_root="pg_local_cache-${version}-source"
install -d "$source_stage/$source_root"
git -C "$repository_directory" ls-files -z \
    | tar -C "$repository_directory" --null -T - -cf - \
    | tar -xf - -C "$source_stage/$source_root"
install -m 0755 "$repository_directory/scripts/release_archive.py" \
    "$source_stage/$source_root/scripts/release_archive.py"
install -m 0755 "$repository_directory/scripts/fetch-release.sh" \
    "$source_stage/$source_root/scripts/fetch-release.sh"
install -m 0644 "$repository_directory/compose.demo.yaml" \
    "$source_stage/$source_root/compose.demo.yaml"
install -m 0644 "$repository_directory/docker/initdb/020_demo.sql" \
    "$source_stage/$source_root/docker/initdb/020_demo.sql"
printf '%s\n' "$build_id" > "$source_stage/$source_root/BUILD-ID"
python3 -B "$repository_directory/scripts/release_archive.py" build \
    --stage "$source_stage" --root "$source_root" \
    --output "$source_archive" --epoch "$epoch"
python3 -B "$repository_directory/scripts/release_archive.py" inspect \
    --archive "$source_archive" --root "$source_root" \
    --extract-dir "$results_directory/source-extracted" \
    --identity-out "$results_directory/source.identity.json" \
    | tee "$results_directory/source.members.txt"
(
    cd "$results_directory/source-extracted/$source_root"
    bash -n scripts/install-existing.sh
    make verify-static source-test pgxn-check pgxn-dist
)
python3 -B "$repository_directory/scripts/release_archive.py" verify-identity \
    --archive "$source_archive" --identity "$results_directory/source.identity.json"

if [[ " ${majors[*]} " == *" 16 "* && " ${variants[*]} " == *" bookworm "* ]]; then
    installer_log="$results_directory/pg16-bookworm-installer-tests.txt"
    docker build --platform linux/amd64 --target builder \
        --build-arg POSTGRES_MAJOR=16 --build-arg POSTGRES_VARIANT=bookworm \
        --build-arg "PGLC_BUILD_ID=$build_id" \
        --tag pg_local_cache-contracts:pg16-bookworm "$repository_directory"

    pgxn_extract="$temporary_directory/pgxn-extracted"
    install -d "$pgxn_extract"
    python3 -m zipfile -e \
        "$results_directory/source-extracted/$source_root/dist/pg_local_cache-${version}.zip" \
        "$pgxn_extract"
    pgxn_root="$pgxn_extract/pg_local_cache-${version}"
    [[ -d "$pgxn_root" && ! -e "$pgxn_root/.git" ]]
    pgxn_release_root="$temporary_directory/pgxn-release"
    install -d "$pgxn_release_root"
    pgxn_build_container="pg_local_cache_pgxn_build_$$"
    docker create --name "$pgxn_build_container" --platform linux/amd64 \
        --network none --env "PGLC_BUILD_ID=$build_id" \
        --volume "$pgxn_root:/src:ro" \
        --volume "$pgxn_release_root:/release" \
        pg_local_cache-contracts:pg16-bookworm sh -ec '
            cp -a /src /tmp/pgxn
            mv /usr/bin/python3 /usr/bin/python3.disabled
            ! command -v python3
            cd /tmp/pgxn
            test "$(cat BUILD-ID)" = "$PGLC_BUILD_ID"
            make clean
            make
            make DESTDIR=/tmp/install install
            install -d /release/lib /release/share/extension
            install -m 0755 "/tmp/install$(pg_config --pkglibdir)/pg_local_cache.so" /release/lib/
            install -m 0644 "/tmp/install$(pg_config --sharedir)/extension/pg_local_cache.control" /release/share/extension/
            for sql_file in "/tmp/install$(pg_config --sharedir)"/extension/pg_local_cache--*.sql; do
                install -m 0644 "$sql_file" /release/share/extension/
            done
            install -m 0755 scripts/install-existing.sh /release/install.sh
            install -m 0644 BUILD-ID /release/BUILD-ID
            {
                printf "format=1\nversion=%s\npostgres_major=16\nos=linux\n" '"$version"'
                printf "libc=glibc\narchitecture=amd64\ncommit=%s\n" "$PGLC_BUILD_ID"
                printf "build_image=pgxn-consumer\n"
            } > /release/RELEASE-METADATA
        '
    docker start "$pgxn_build_container" >/dev/null
    pgxn_build_exit="$(docker wait "$pgxn_build_container")"
    docker logs "$pgxn_build_container"
    [[ "$pgxn_build_exit" == 0 ]]
    docker rm -fv "$pgxn_build_container" >/dev/null
    pgxn_build_container=""

    pgxn_runtime_container="pg_local_cache_pgxn_runtime_$$"
    docker run --detach --name "$pgxn_runtime_container" --platform linux/amd64 \
        --network none --env POSTGRES_PASSWORD=pgxn-test \
        --env POSTGRES_DB=pgxn_cache \
        --volume "$pgxn_release_root:/artifact:ro" \
        postgres:16-bookworm >/dev/null
    for _attempt in {1..60}; do
        if [[ "$(docker logs "$pgxn_runtime_container" 2>&1)" == \
            *'PostgreSQL init process complete; ready for start up.'* ]] \
            && docker exec "$pgxn_runtime_container" pg_isready \
            --username postgres --dbname pgxn_cache >/dev/null 2>&1; then
            break
        fi
        sleep 0.2
    done
    docker exec "$pgxn_runtime_container" pg_isready \
        --username postgres --dbname pgxn_cache >/dev/null
    pgxn_install_output="$(
        docker exec --user root "$pgxn_runtime_container" \
            /artifact/install.sh install --database pgxn_cache --mode sql-only \
            --restart-method none --postgres-os-user postgres \
            --pg-config pg_config --psql psql \
            --state-root /var/lib/postgresql/data/pg_local_cache-install-state
    )"
    pgxn_state_directory="$(printf '%s\n' "$pgxn_install_output" | sed -n 's/^.*state directory: //p' | tail -n 1)"
    [[ "$pgxn_state_directory" == /var/lib/postgresql/data/pg_local_cache-install-state/* ]]
    docker restart "$pgxn_runtime_container" >/dev/null
    for _attempt in {1..60}; do
        if docker exec "$pgxn_runtime_container" pg_isready \
            --username postgres --dbname pgxn_cache >/dev/null 2>&1; then
            break
        fi
        sleep 0.2
    done
    docker exec --user postgres "$pgxn_runtime_container" \
        /artifact/install.sh verify --state-directory "$pgxn_state_directory" \
        --postgres-os-user postgres --pg-config pg_config --psql psql
    pgxn_identity="$(
        docker exec "$pgxn_runtime_container" psql --username postgres \
            --dbname pgxn_cache --no-psqlrc --tuples-only --no-align \
            --set ON_ERROR_STOP=1 --command \
            "SELECT current_setting('pg_local_cache.binary_version'), current_setting('pg_local_cache.binary_build_id'), current_setting('pg_local_cache.port')"
    )"
    [[ "$pgxn_identity" == "${version}|${build_id}|0" ]]
    docker exec -i "$pgxn_runtime_container" psql --username postgres \
        --dbname pgxn_cache --no-psqlrc --set ON_ERROR_STOP=1 <<'SQL'
CREATE TABLE public.pgxn_smoke (id bigint PRIMARY KEY, payload text NOT NULL);
INSERT INTO public.pgxn_smoke VALUES (1, 'fresh-pgxn-cache');
SELECT local_cache.attach_table('public.pgxn_smoke'::regclass);
SELECT local_cache.mget('public.pgxn_smoke'::regclass, ARRAY[1]::bigint[]);
SELECT local_cache.mget('public.pgxn_smoke'::regclass, ARRAY[1]::bigint[]);
SQL
    pgxn_cache_hit="$(
        docker exec "$pgxn_runtime_container" psql --username postgres \
            --dbname pgxn_cache --no-psqlrc --tuples-only --no-align \
            --set ON_ERROR_STOP=1 --command \
            "SELECT (local_cache.stats() ->> 'sql_cache_hits')::bigint >= 1"
    )"
    [[ "$pgxn_cache_hit" == t ]]
    printf 'pgxn lifecycle: PASS (no git, no python3, fresh SQL-only, build %s)\n' \
        "$build_id" | tee "$results_directory/pg16-bookworm-pgxn-lifecycle.txt"
    docker rm -fv "$pgxn_runtime_container" >/dev/null
    pgxn_runtime_container=""

    docker run --rm --platform linux/amd64 --network none \
        --volume "${repository_directory}:/workspace:ro" --workdir /workspace \
        pg_local_cache-contracts:pg16-bookworm \
        python3 -B -m unittest -v \
          tests.installer_release_contract_test.InstallerContracts.test_binary_archive_root_is_installed_without_source_tree \
          tests.installer_release_contract_test.InstallerContracts.test_binary_mismatch_fails_before_any_install_write \
          tests.installer_release_contract_test.InstallerContracts.test_partial_alter_system_failure_restores_exact_auto_conf \
          tests.installer_release_contract_test.InstallerContracts.test_pending_file_settings_are_preserved_in_written_configuration \
          tests.installer_release_contract_test.InstallerContracts.test_resp_token_accepts_only_owner_readable_0400_or_0600 \
          tests.installer_release_contract_test.InstallerContracts.test_sql_only_to_resp_adds_the_new_worker_slots \
          tests.installer_release_contract_test.InstallerContracts.test_staged_sql_only_to_resp_reserves_slots_before_first_restart \
          tests.installer_release_contract_test.InstallerContracts.test_upgrade_preserves_an_existing_extension_owner \
        2>&1 | tee "$installer_log"
    test "$(grep -Ec '^test_.* \.\.\. ok$' "$installer_log")" -eq 8
    grep -Eq '^Ran 8 tests in ' "$installer_log"
    grep -Fx 'OK' "$installer_log"
    ! grep -Eq 'skipped|FAILED|ERROR' "$installer_log"

    for fetch_variant in bookworm alpine3.23; do
        case "$fetch_variant" in
            bookworm)
                fetch_libc=glibc
                fetch_tar=gnu
                fetch_image=pg_local_cache-contracts:pg16-bookworm
                ;;
            alpine3.23)
                fetch_libc=musl
                fetch_tar=busybox
                fetch_image=pg_local_cache-contracts:pg16-fetch-musl
                docker build --platform linux/amd64 --target builder \
                    --build-arg POSTGRES_MAJOR=16 \
                    --build-arg POSTGRES_VARIANT=alpine3.23 \
                    --build-arg "PGLC_BUILD_ID=$build_id" \
                    --tag "$fetch_image" "$repository_directory"
                ;;
        esac
        fetch_log="$results_directory/pg16-${fetch_libc}-fetcher-tests.txt"
        docker run --rm --platform linux/amd64 --network none \
            --env "PGLC_FETCH_TEST_LIBC=$fetch_libc" \
            --env "PGLC_FETCH_TEST_TAR_IMPL=$fetch_tar" \
            --volume "${repository_directory}:/workspace:ro" --workdir /workspace \
            "$fetch_image" python3 -B -m unittest -v \
              tests.installer_release_contract_test.FetchReleaseContracts.test_checksum_and_archive_attacks_leave_no_output \
              tests.installer_release_contract_test.FetchReleaseContracts.test_verified_archive_is_extracted_but_never_executed \
            2>&1 | tee "$fetch_log"
        grep -Eq '^Ran 2 tests in ' "$fetch_log"
        grep -Fx 'OK' "$fetch_log"
        ! grep -Eq 'skipped|FAILED|ERROR' "$fetch_log"
        [[ "$fetch_variant" == bookworm ]] \
            || docker image rm "$fetch_image" >/dev/null 2>&1 || true
    done
    docker image rm pg_local_cache-contracts:pg16-bookworm >/dev/null 2>&1 || true
fi
