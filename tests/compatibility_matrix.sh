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

for major in "${majors[@]}"; do
    for variant in "${variants[@]}"; do
        printf 'compatibility smoke: PostgreSQL %s-%s\n' "$major" "$variant"
        POSTGRES_MAJOR="$major" POSTGRES_VARIANT="$variant" \
            bash "${repository_directory}/tests/docker_smoke.sh"
    done
done
