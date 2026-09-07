#!/usr/bin/env bash
set -euo pipefail

task="${TASK:-polyglot_python_simple-linked-list}"
agent="${AGENT:-oracle}"
model="${MODEL:-}"
attempts="${ATTEMPTS:-4}"
concurrency="${CONCURRENCY:-4}"
repetitions="${REPETITIONS:-3}"
stamp="$(date -u +%Y%m%d-%H%M%S)"
result="${OUTPUT:-results/${stamp}-aider-polyglot.json}"
report="${REPORT:-${result%.json}.html}"
minimum_reward="${MINIMUM_REWARD:-}"
if [[ -z "$minimum_reward" && "$agent" == "oracle" ]]; then
  minimum_reward=1
fi
reward_args=()
if [[ -n "$minimum_reward" ]]; then
  reward_args=(--minimum-reward "$minimum_reward")
fi
model_args=()
if [[ -n "$model" ]]; then
  model_args=(--model "$model")
fi

uv run --no-sync python - <<'PY'
import smol.harbor

if not hasattr(smol.harbor, "_prepare_local_dockerfile_image"):
    raise SystemExit(
        "This demo needs a smolmachines SDK release with Dockerfile-backed "
        "Harbor task support; update the environment with `uv sync`."
    )
PY

uv run --no-sync python bench/harbor_fanout.py \
  --dataset aider/aider-polyglot \
  --task "$task" \
  --attempts "$attempts" \
  --concurrency "$concurrency" \
  --repetitions "$repetitions" \
  --agent "$agent" \
  "${model_args[@]}" \
  --providers smol-branch docker \
  --checkpoint-mode prepared \
  "${reward_args[@]}" \
  --output "$result" \
  "$@"

uv run --no-sync python bench/render_results.py "$result" --output "$report"
printf 'Open %s\n' "$report"
