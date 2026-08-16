#!/usr/bin/env bash
set -Eeuo pipefail

database="${1:-postgres}"
temporary_directory="$(mktemp -d "${TMPDIR:-/tmp}/pg_local_cache-install.XXXXXX")"
trap 'rm -rf -- "$temporary_directory"' EXIT

latest_url="$(curl -fsSLI --proto '=https' --proto-redir '=https'     --tlsv1.2 -o /dev/null -w '%{url_effective}'     https://github.com/profundium/pg_local_cache/releases/latest)"
[[ "$latest_url" =~ /tag/(v[0-9]+\.[0-9]+\.[0-9]+)$ ]]     || { echo "could not resolve the latest release" >&2; exit 1; }
tag="${BASH_REMATCH[1]}"
base="https://github.com/profundium/pg_local_cache/releases/download/$tag"

curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2     -o "$temporary_directory/SHA256SUMS" "$base/SHA256SUMS"
curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2     -o "$temporary_directory/fetch-release.sh" "$base/fetch-release.sh"
(
    cd "$temporary_directory"
    awk '$2 == "fetch-release.sh"' SHA256SUMS > fetch-release.sha256
    [[ "$(wc -l < fetch-release.sha256)" -eq 1 ]]
    sha256sum --check --strict fetch-release.sha256
)
bash "$temporary_directory/fetch-release.sh"     --release-tag "$tag"     --output-directory "$temporary_directory/package"

installer=("$temporary_directory/package/install.sh" install     --database "$database" --restart-method pg_ctl)
if (( EUID == 0 )); then
    "${installer[@]}"
else
    sudo "${installer[@]}"
fi

