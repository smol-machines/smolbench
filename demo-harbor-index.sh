#!/usr/bin/env bash
set -euo pipefail

task="${TASK:-gso-speedup-numpy-strings}"
agent="${AGENT:-nop}"
model="${MODEL:-}"
attempts="${ATTEMPTS:-4}"
concurrency="${CONCURRENCY:-4}"
repetitions="${REPETITIONS:-3}"
stamp="$(date -u +%Y%m%d-%H%M%S)"
result="${OUTPUT:-results/${stamp}-harbor-index.json}"
report="${REPORT:-${result%.json}.html}"
minimum_reward="${MINIMUM_REWARD:-}"

model_args=()
if [[ -n "$model" ]]; then
  model_args=(--model "$model")
fi
reward_args=()
if [[ -n "$minimum_reward" ]]; then
  reward_args=(--minimum-reward "$minimum_reward")
fi

uv run --no-sync python bench/harbor_fanout.py \
  --dataset harbor-index/harbor-index-1.0 \
  --task "$task" \
  --attempts "$attempts" \
  --concurrency "$concurrency" \
  --repetitions "$repetitions" \
  --agent "$agent" \
  "${model_args[@]}" \
  --providers smol-branch docker \
  --checkpoint-mode prepared \
  --keep-going \
  "${reward_args[@]}" \
  --output "$result" \
  "$@"

uv run --no-sync python bench/render_results.py "$result" --output "$report"
printf 'Open %s\n' "$report"
