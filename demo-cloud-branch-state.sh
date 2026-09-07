#!/usr/bin/env bash
set -euo pipefail

uv run python bench/cloud_branch_state.py "$@"
