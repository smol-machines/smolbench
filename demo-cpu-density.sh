#!/usr/bin/env bash
set -euo pipefail

exec uv run python bench/cpu_density.py "$@"
