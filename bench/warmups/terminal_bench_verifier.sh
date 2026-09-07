#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y curl
curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh
source /root/.local/bin/env
tmpdir="$(mktemp -d)"
cd "$tmpdir"
uv init --name harbor-verifier-cache
uv add pytest==8.4.1
rm -rf "$tmpdir"
