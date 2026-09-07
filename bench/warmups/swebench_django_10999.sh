#!/usr/bin/env bash
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh
fi
mkdir -p /logs
cd /testbed
source /opt/miniconda3/bin/activate
conda activate testbed
python -m pip install -e .
