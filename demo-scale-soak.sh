#!/usr/bin/env bash
set -euo pipefail

stamp="$(date -u +%Y%m%d-%H%M%S)"
directory="${OUTPUT_DIR:-results/raw/${stamp}-scale-soak}"
read -r -a sizes <<<"${SIZES:-16 32 64}"
repetitions="${REPETITIONS:-3}"
docker_size="${DOCKER_SIZE:-16}"
boundary="${BOUNDARY:-}"
revision="${SMOLVM_REVISION:-unknown}"
inputs=()
failure_args=()
status=0

mkdir -p "$directory"
for size in "${sizes[@]}"; do
  if [[ ! "$size" =~ ^[1-9][0-9]*$ ]]; then
    printf 'Invalid fan-out: %s\n' "$size" >&2
    exit 2
  fi
  output="$directory/smol-n${size}.json"
  PROVIDERS=smol-branch ATTEMPTS="$size" CONCURRENCY="$size" \
    REPETITIONS="$repetitions" OUTPUT="$output" \
    ./demo-terminal-bench.sh --install-only
  inputs+=("$output")
done

if [[ -n "$boundary" ]]; then
  if [[ ! "$boundary" =~ ^[1-9][0-9]*$ ]]; then
    printf 'Invalid boundary fan-out: %s\n' "$boundary" >&2
    exit 2
  fi
  output="$directory/smol-n${boundary}-probe.json"
  if PROVIDERS=smol-branch ATTEMPTS="$boundary" CONCURRENCY="$boundary" \
    REPETITIONS=1 OUTPUT="$output" \
    ./demo-terminal-bench.sh --install-only; then
    inputs+=("$output")
  else
    failure_args+=(--failure "$output")
    status=1
  fi
fi

if [[ "$docker_size" != "0" ]]; then
  output="$directory/docker-n${docker_size}.json"
  PROVIDERS=docker ATTEMPTS="$docker_size" CONCURRENCY="$docker_size" \
    REPETITIONS="$repetitions" OUTPUT="$output" \
    ./demo-terminal-bench.sh --install-only
  inputs+=("$output")
fi

uv run python bench/scale_results.py "${inputs[@]}" \
  --smolvm-revision "$revision" \
  "${failure_args[@]}" \
  --json results/scale-soak.json \
  --html results/scale-soak.html

printf 'Open results/scale-soak.html\n'
exit "$status"
