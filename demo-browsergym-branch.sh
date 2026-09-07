#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 0 ]]; then
  printf 'Usage: REPETITIONS=3 PARALLEL=4 OUTPUT=results/run.json %s\n' "$0" >&2
  exit 2
fi

repetitions="${REPETITIONS:-3}"
parallel="${PARALLEL:-4}"
output="${OUTPUT:-}"
if [[ ! "$repetitions" =~ ^[1-9][0-9]*$ || ! "$parallel" =~ ^[1-4]$ ]]; then
  printf 'REPETITIONS must be positive and PARALLEL must be between 1 and 4.\n' >&2
  exit 2
fi

args=(--repetitions "$repetitions" --parallel "$parallel")
if [[ -n "$output" ]]; then
  args+=(--output "$output")
fi
uv run python -m bench.browsergym_fanout "${args[@]}"
