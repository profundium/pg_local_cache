#!/usr/bin/env bash
set -Eeuo pipefail

program_name="${0##*/}"
release_tag=""
pg_config_command="pg_config"
output_directory=""
dry_run=0
temporary_directory=""
created_output=0
success=0

usage() {
    cat <<EOF
Usage: $program_name --release-tag v<version> [options]

Select, verify, and extract one pg_local_cache Linux amd64 release.
It never executes the downloaded install.sh.

Options:
  --release-tag TAG       Required immutable tag, for example v1.3.0
  --pg-config PATH        pg_config command or path (default: pg_config)
  --output-directory DIR  New package directory
  --dry-run               Probe and print selection without network or writes
  -h, --help              Show this help
EOF
}

fail() {
    printf '%s: %s\n' "$program_name" "$*" >&2
    exit 1
}

unsupported() {
    printf '%s: %s\n' "$program_name" "$1" >&2
    printf 'Build from source with PGXS: https://profundium.github.io/pg_local_cache/docs/INSTALL_EXISTING.html#build-from-source\n' >&2
    exit 1
}

cleanup() {
    local status=$?

    if [[ "$success" != 1 && "$created_output" == 1 ]]; then
        rm -rf -- "$output_directory"
    fi
    if [[ -n "$temporary_directory" ]]; then
        rm -rf -- "$temporary_directory"
    fi
    return "$status"
}

trap cleanup EXIT
trap 'exit 130' HUP INT TERM

while (( $# > 0 )); do
    case "$1" in
        --release-tag)
            (( $# >= 2 )) || fail "--release-tag requires a value"
            release_tag="$2"
            shift 2
            ;;
        --pg-config)
            (( $# >= 2 )) || fail "--pg-config requires a value"
            pg_config_command="$2"
            shift 2
            ;;
        --output-directory)
            (( $# >= 2 )) || fail "--output-directory requires a value"
            output_directory="$2"
            shift 2
            ;;
        --dry-run)
            dry_run=1
            shift
            ;;
        -h|--help)
            usage
            success=1
            exit 0
            ;;
        *) fail "unknown option: $1" ;;
    esac
done

[[ "$release_tag" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] \
    || fail "--release-tag must match v<major>.<minor>.<patch>"
version="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.${BASH_REMATCH[3]}"

command -v "$pg_config_command" >/dev/null 2>&1 \
    || unsupported "pg_config was not found"
postgres_version="$($pg_config_command --version 2>/dev/null)" \
    || unsupported "pg_config --version failed"
[[ "$postgres_version" =~ ^PostgreSQL[[:space:]]+(14|15|16|17|18)(\.|$) ]] \
    || unsupported "unsupported PostgreSQL version: $postgres_version"
postgres_major="${BASH_REMATCH[1]}"

os_name="$(uname -s 2>/dev/null)" || unsupported "uname -s failed"
[[ "$os_name" == Linux ]] || unsupported "unsupported operating system: $os_name"
machine="$(uname -m 2>/dev/null)" || unsupported "uname -m failed"
case "$machine" in
    x86_64|amd64) architecture=amd64 ;;
    *) unsupported "unsupported architecture: $machine" ;;
esac

ldd_output="$(ldd --version 2>&1 || true)"
case "$ldd_output" in
    *musl*) libc=musl ;;
    *"GNU libc"*|*GLIBC*|*"GNU C Library"*|*"Free Software Foundation"*)
        libc=glibc
        ;;
    *) unsupported "could not identify glibc or musl from ldd --version" ;;
esac

command -v sha256sum >/dev/null 2>&1 \
    || unsupported "sha256sum was not found"
command -v curl >/dev/null 2>&1 || unsupported "curl was not found"
curl --version >/dev/null 2>&1 || unsupported "curl is unusable"
command -v tar >/dev/null 2>&1 || unsupported "tar was not found"
tar_version="$(tar --version 2>&1)" || unsupported "tar --version failed"
tar_help="$(tar --help 2>&1)" || unsupported "tar --help failed"
case "$tar_version" in
    *"GNU tar"*)
        [[ "$tar_help" == *"--list"* && "$tar_help" == *"--extract"* \
            && "$tar_help" == *"--gzip"* && "$tar_help" == *"--file"* ]] \
            || unsupported "GNU tar lacks required list/extract/gzip/file options"
        tar_implementation=gnu
        ;;
    *BusyBox*|*busybox*)
        [[ "$tar_help" == *"tar c|x|t"* && "$tar_help" == *"-f TARFILE"* \
            && "$tar_help" == *z* ]] \
            || unsupported "BusyBox tar lacks required list/extract/gzip/file options"
        tar_implementation=busybox
        ;;
    *) unsupported "unsupported tar implementation" ;;
esac

asset="pg_local_cache-pg${postgres_major}-linux-${libc}-${architecture}"
root_name="pg_local_cache-${version}-pg${postgres_major}-linux-${libc}-${architecture}"
base_url="https://github.com/profundium/pg_local_cache/releases/download/${release_tag}"
archive_url="${base_url}/${asset}.tar.gz"
checksum_url="${base_url}/SHA256SUMS"
if [[ -z "$output_directory" ]]; then
    output_directory="$PWD/${asset}-${release_tag}"
fi
output_directory="${output_directory%/}"
[[ -n "$output_directory" && "$output_directory" != / \
    && "$output_directory" != . && "$output_directory" != .. ]] \
    || fail "unsafe output directory"
[[ ! -e "$output_directory" && ! -L "$output_directory" ]] \
    || fail "output directory already exists: $output_directory"
output_parent="$(dirname -- "$output_directory")"
[[ -d "$output_parent" && ! -L "$output_parent" ]] \
    || fail "output parent must be an existing real directory: $output_parent"

printf 'release_tag=%s\nasset=%s\ntar=%s\narchive_url=%s\n' \
    "$release_tag" "$asset" "$tar_implementation" "$archive_url"
printf 'checksum_url=%s\noutput=%s\n' "$checksum_url" "$output_directory"
if [[ "$dry_run" == 1 ]]; then
    printf 'dry_run=PASS (no network or writes)\n'
    success=1
    exit 0
fi

umask 077
temporary_directory="$(mktemp -d "${output_parent}/.pg_local_cache-fetch.XXXXXX")" \
    || fail "could not create a private temporary directory"
chmod 0700 "$temporary_directory"
archive="$temporary_directory/${asset}.tar.gz"
manifest="$temporary_directory/SHA256SUMS"
curl --fail --location --proto '=https' --proto-redir '=https' \
    --output "$archive" "$archive_url"
curl --fail --location --proto '=https' --proto-redir '=https' \
    --output "$manifest" "$checksum_url"
for downloaded in "$archive" "$manifest"; do
    [[ -f "$downloaded" && ! -L "$downloaded" ]] \
        || fail "download is not a regular non-symlink file: ${downloaded##*/}"
done
chmod 0400 "$archive" "$manifest"

expected_hash=""
matching_checksums=0
while IFS= read -r checksum_line || [[ -n "$checksum_line" ]]; do
    [[ -z "$checksum_line" ]] && continue
    [[ "$checksum_line" =~ ^([0-9a-f]{64})[[:space:]]+\*?([^[:space:]]+)$ ]] \
        || fail "SHA256SUMS contains an invalid record"
    checksum_name="${BASH_REMATCH[2]}"
    if [[ "$checksum_name" == "${asset}.tar.gz" ]]; then
        expected_hash="${BASH_REMATCH[1]}"
        (( matching_checksums += 1 ))
    fi
done < "$manifest"
[[ "$matching_checksums" == 1 ]] \
    || fail "SHA256SUMS must contain exactly one ${asset}.tar.gz record"
hash_output="$(sha256sum "$archive")" || fail "could not hash downloaded archive"
actual_hash="${hash_output%%[[:space:]]*}"
[[ "$actual_hash" == "$expected_hash" ]] || fail "archive SHA256 mismatch"

names_file="$temporary_directory/names"
verbose_file="$temporary_directory/verbose"
tar -tzf "$archive" > "$names_file" || fail "archive listing failed"
tar -tvzf "$archive" > "$verbose_file" || fail "archive type listing failed"
seen_names=$'\n'
have_root=0
have_installer=0
have_build_id=0
have_metadata=0
member_count=0
exec 3< "$names_file"
exec 4< "$verbose_file"
while IFS= read -r name <&3; do
    IFS= read -r verbose_line <&4 || fail "tar listing output is ambiguous"
    (( member_count += 1 ))
    [[ "$name" =~ ^[A-Za-z0-9._/-]+$ ]] || fail "archive path uses unsupported bytes"
    normalized="${name%/}"
    [[ -n "$normalized" && "$normalized" != /* ]] || fail "unsafe archive path"
    IFS=/ read -r -a path_parts <<< "$normalized"
    for part in "${path_parts[@]}"; do
        [[ -n "$part" && "$part" != . && "$part" != .. ]] \
            || fail "unsafe archive path: $name"
    done
    [[ "${path_parts[0]}" == "$root_name" ]] \
        || fail "archive contains an unexpected root"
    [[ "$seen_names" != *$'\n'"$normalized"$'\n'* ]] \
        || fail "duplicate archive path: $name"
    seen_names+="$normalized"$'\n'
    type_character="${verbose_line:0:1}"
    [[ "$type_character" == - || "$type_character" == d ]] \
        || fail "archive contains a link, device, FIFO, or unsupported type"

    relative="${normalized#"$root_name"}"
    relative="${relative#/}"
    if [[ -z "$relative" ]]; then
        [[ "$type_character" == d ]] || fail "archive root is not a directory"
        have_root=1
        continue
    fi
    first_part="${relative%%/*}"
    case "$first_part" in
        lib|share|docs|install.sh|README.md|LICENSE|BUILD-ID|RELEASE-METADATA) ;;
        *) fail "archive contains checkout or unexpected content: $relative" ;;
    esac
    [[ "$relative" != install.sh ]] || have_installer=1
    [[ "$relative" != BUILD-ID ]] || have_build_id=1
    [[ "$relative" != RELEASE-METADATA ]] || have_metadata=1
done
(( member_count > 0 )) || fail "archive is empty"
if IFS= read -r _extra_verbose <&4; then
    fail "tar listing output is ambiguous"
fi
[[ "$have_root" == 1 && "$have_installer" == 1 \
    && "$have_build_id" == 1 && "$have_metadata" == 1 ]] \
    || fail "archive is missing required release members"

validation_directory="$temporary_directory/validated"
mkdir -m 0700 "$validation_directory"
tar -xzf "$archive" -C "$validation_directory" \
    || fail "validated archive extraction failed"
validated_root="$validation_directory/$root_name"
[[ -d "$validated_root" && ! -L "$validated_root" \
    && -x "$validated_root/install.sh" \
    && -f "$validated_root/BUILD-ID" \
    && -f "$validated_root/RELEASE-METADATA" ]] \
    || fail "extracted release layout is invalid"
build_id="$(<"$validated_root/BUILD-ID")"
[[ "$build_id" =~ ^[0-9a-f]{40}$ ]] || fail "invalid BUILD-ID"

metadata_format=""
metadata_version=""
metadata_postgres_major=""
metadata_os=""
metadata_libc=""
metadata_architecture=""
metadata_commit=""
metadata_build_image=""
while IFS='=' read -r key value || [[ -n "$key$value" ]]; do
    [[ "$key" =~ ^[a-z_]+$ && -n "$value" ]] \
        || fail "invalid RELEASE-METADATA record"
    case "$key" in
        format) target=metadata_format ;;
        version) target=metadata_version ;;
        postgres_major) target=metadata_postgres_major ;;
        os) target=metadata_os ;;
        libc) target=metadata_libc ;;
        architecture) target=metadata_architecture ;;
        commit) target=metadata_commit ;;
        build_image) target=metadata_build_image ;;
        *) fail "unknown RELEASE-METADATA key: $key" ;;
    esac
    [[ -z "${!target}" ]] || fail "duplicate RELEASE-METADATA key: $key"
    printf -v "$target" '%s' "$value"
done < "$validated_root/RELEASE-METADATA"
[[ "$metadata_format" == 1 \
    && "$metadata_version" == "$version" \
    && "$metadata_postgres_major" == "$postgres_major" \
    && "$metadata_os" == linux \
    && "$metadata_libc" == "$libc" \
    && "$metadata_architecture" == "$architecture" \
    && "$metadata_commit" == "$build_id" \
    && -n "$metadata_build_image" ]] \
    || fail "release metadata does not match the selected asset"

mkdir -m 0700 "$output_directory" \
    || fail "could not create package directory"
created_output=1
mv -- "$validated_root"/* "$output_directory"/ \
    || fail "could not publish validated release"
extracted_path="$output_directory"
[[ -d "$extracted_path" && -x "$extracted_path/install.sh" ]] \
    || fail "published release is incomplete"

printf 'archive_sha256=%s\nextracted_path=%s\n' "$actual_hash" "$extracted_path"
printf 'next_command=sudo '; printf '%q ' "$extracted_path/install.sh" install; printf '\n'
printf 'install_executed=no\n'
success=1
