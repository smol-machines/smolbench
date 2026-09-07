#!/usr/bin/env bash
set -euo pipefail

fanout="${FANOUT:-4}"
parallel="${PARALLEL:-4}"
repetitions="${REPETITIONS:-3}"
stamp="$(date -u +%Y%m%d-%H%M%S)"
terminal_result="results/${stamp}-terminal-bench.json"
braintrust_result="results/${stamp}-braintrust.json"
report="results/${stamp}-public-suite.html"

uv run python bench/harbor_fanout.py \
  --task regex-log \
  --attempts "$fanout" \
  --concurrency "$parallel" \
  --repetitions "$repetitions" \
  --providers smol-branch docker \
  --prepare-script bench/warmups/terminal_bench_verifier.sh \
  --output "$terminal_result"

uv run python bench/braintrust_fanout.py \
  --fanout "$fanout" \
  --parallel "$parallel" \
  --repetitions "$repetitions" \
  --mode smoke \
  --docker \
  --output "$braintrust_result"

uv run python bench/render_results.py \
  "$terminal_result" \
  "$braintrust_result" \
  --output "$report"

printf 'Open %s\n' "$report"
