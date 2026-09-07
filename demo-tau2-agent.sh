#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 0 ]]; then
  printf 'Usage: OPENAI_API_BASE=... OPENAI_API_KEY=... AGENT_MODEL=... %s\n' "$0" >&2
  exit 2
fi

: "${OPENAI_API_BASE:?Set an OpenAI-compatible endpoint reachable from the guest}"
: "${OPENAI_API_KEY:?Set the API key for the endpoint}"
: "${AGENT_MODEL:?Set the LiteLLM model name, such as openai/gpt-4.1}"

args=(--agent-model "$AGENT_MODEL")
if [[ -n "${USER_MODEL:-}" ]]; then
  args+=(--user-model "$USER_MODEL")
fi
if [[ -n "${MODEL_SOURCE:-}" ]]; then
  args+=(--model-source "$MODEL_SOURCE")
fi
if [[ -n "${AGENT_IMPLEMENTATION:-}" ]]; then
  args+=(--agent-implementation "$AGENT_IMPLEMENTATION")
fi
if [[ "${REQUIRE_REWARD:-0}" == "1" ]]; then
  args+=(--require-reward)
fi
if [[ -n "${OUTPUT:-}" ]]; then
  args+=(--output "$OUTPUT")
fi

uv run python -m bench.tau2_agent "${args[@]}"
