#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
  node|go|node-workload) mode=$1 ;;
  *) echo "Usage: $0 node|go|node-workload > benchmark.json" >&2; exit 2 ;;
esac

cd "$(dirname "$0")/.."
for tool in docker node npm; do command -v "$tool" >/dev/null; done
if [[ $mode == go ]]; then command -v go >/dev/null; fi
docker info >/dev/null
npm --prefix examples/node-postgres ci --ignore-scripts >&2

export COMPOSE_PROJECT_NAME="pglc-bench-$$"
export PGLC_DEMO_PORT=0 PGLC_DEMO_RESP_PORT=0 PGLC_DEMO_MAX_CONNECTIONS=300
export PGLC_EXTENSION_REF=local
if git diff --quiet HEAD -- src sql Makefile Dockerfile pg_local_cache.control; then
  PGLC_EXTENSION_REF=$(git rev-parse HEAD)
fi
compose=(docker compose -f examples/compose.yaml)
if [[ $mode == go ]]; then compose+=(-f examples/compose.resp.yaml); fi
client="$COMPOSE_PROJECT_NAME-client"
temp=$(mktemp -d)
cleanup() {
  status=$?
  trap - EXIT
  if [[ $mode == go ]]; then docker rm -f "$client" >/dev/null 2>&1 || true; fi
  "${compose[@]}" down >&2 || status=1
  rm -rf "$temp"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"${compose[@]}" up --build --wait >&2
address=$("${compose[@]}" port postgres 5432)
export PGLC_DEMO_PORT=${address##*:}
export BENCHMARK_CLIENT=$mode SERVER_RESOURCES=1 REPEATS=${REPEATS:-3}
export BATCHES=${BATCHES:-1,64}

if [[ $mode == go ]]; then
  server=$("${compose[@]}" ps -q postgres)
  image=$(docker inspect --format '{{.Image}}' "$server")
  arch=$(docker image inspect --format '{{.Architecture}}' "$image")
  GOOS=linux GOARCH="$arch" CGO_ENABLED=0 go -C examples/go-pgx build -o "$temp/pglc-go-pgx" .
  docker run -d --name "$client" --network "container:$server" \
    --mount "type=bind,src=$temp/pglc-go-pgx,dst=/tmp/pglc-go-pgx,readonly" \
    "$image" sleep infinity >&2
  export PGLC_PGX_CONTAINER=$client GOMAXPROCS=${GOMAXPROCS:-8}
  export CONNECTIONS=${CONNECTIONS:-64,256} DURATION_SECONDS=${DURATION_SECONDS:-5}
elif [[ $mode == node ]]; then
  export CONNECTIONS=${CONNECTIONS:-4,64,256} DURATION_SECONDS=${DURATION_SECONDS:-10}
fi

if [[ $mode == node-workload ]]; then
  export CLIENTS=${CLIENTS:-64} REQUESTS=${REQUESTS:-50000}
  node examples/node-postgres/benchmark.mjs
else
  node examples/node-postgres/client-comparison.mjs
fi
