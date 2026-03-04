#!/bin/bash
#
# Shared utilities for smolbench.
#
# Source this in bench scripts:
#   source "$(dirname "$0")/common.sh"

set -euo pipefail

BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$BENCH_DIR/.." && pwd)"
RESULTS_DIR="$BENCH_DIR/results"
BENCH_API_PORT="${BENCH_API_PORT:-18099}"
BENCH_API_URL="http://127.0.0.1:$BENCH_API_PORT"
ITERATIONS="${ITERATIONS:-5}"

_BENCH_SERVER_PID=""
_BENCH_VMS=()
_BENCH_SANDBOXES=()

# --- Binary resolution ---

init_smolvm() {
    SMOLVM="${SMOLVM:-}"

    if [[ -z "$SMOLVM" ]] || [[ ! -x "$SMOLVM" ]]; then
        for candidate in "$PROJECT_ROOT/target/release/smolvm" "$PROJECT_ROOT/target/debug/smolvm"; do
            if [[ -x "$candidate" ]]; then
                SMOLVM="$candidate"
                break
            fi
        done
    fi

    if [[ -z "$SMOLVM" ]]; then
        echo "Error: Could not find smolvm binary. Build with: cargo build --release" >&2
        exit 1
    fi

    [[ "$SMOLVM" != /* ]] && SMOLVM="$(cd "$(dirname "$SMOLVM")" && pwd)/$(basename "$SMOLVM")"

    if [[ "$(uname -s)" == "Darwin" ]]; then
        local lib_dir="$PROJECT_ROOT/lib"
        [[ -d "$lib_dir" ]] && export DYLD_LIBRARY_PATH="${lib_dir}${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
    else
        local lib_dir="$PROJECT_ROOT/lib/linux-$(uname -m)"
        [[ -d "$lib_dir" ]] && export LD_LIBRARY_PATH="${lib_dir}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    fi
}

# --- Timing ---

now_ms() {
    if [[ "$(uname -s)" == "Linux" ]]; then
        echo $(( $(date +%s%N) / 1000000 ))
    else
        python3 -c "import time; print(int(time.time() * 1000))"
    fi
}

measure_ms() {
    local start end
    start=$(now_ms)
    "$@" > /dev/null 2>&1 || true
    end=$(now_ms)
    echo $(( end - start ))
}

# --- Stats ---

# Input: comma-separated numbers. Output: JSON object.
calc_stats() {
    python3 -c "
import json, statistics
samples = [$1]
n = len(samples)
if n == 0:
    r = {'min': 0, 'max': 0, 'avg': 0, 'stdDev': 0, 'median': 0, 'samples': []}
elif n == 1:
    r = {'min': samples[0], 'max': samples[0], 'avg': samples[0], 'stdDev': 0, 'median': samples[0], 'samples': samples}
else:
    r = {'min': min(samples), 'max': max(samples), 'avg': round(statistics.mean(samples), 1),
         'stdDev': round(statistics.stdev(samples), 1), 'median': round(statistics.median(samples), 1), 'samples': samples}
print(json.dumps(r))
"
}

# --- System info ---

collect_system_info() {
    python3 -c "
import json, subprocess, platform
from datetime import datetime, timezone

def cmd(args):
    try: return subprocess.check_output(args, stderr=subprocess.DEVNULL).decode().strip()
    except: return 'unknown'

os_name = platform.system()
info = {
    'hostname': platform.node(),
    'os': os_name,
    'osVersion': platform.release(),
    'arch': platform.machine(),
    'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
}

if os_name == 'Linux':
    for line in cmd(['lscpu']).splitlines():
        if 'Model name' in line:
            info['cpu'] = line.split(':', 1)[1].strip()
            break
    else:
        info['cpu'] = 'unknown'
    info['cpuCores'] = int(cmd(['nproc']) or 0)
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    info['memoryMb'] = int(line.split()[1]) // 1024
                    break
    except:
        info['memoryMb'] = 0
elif os_name == 'Darwin':
    info['cpu'] = cmd(['sysctl', '-n', 'machdep.cpu.brand_string'])
    info['cpuCores'] = int(cmd(['sysctl', '-n', 'hw.ncpu']) or 0)
    info['memoryMb'] = int(cmd(['sysctl', '-n', 'hw.memsize']) or 0) // 1048576

info['smolvmVersion'] = cmd(['$SMOLVM', '--version']).split()[-1] if '$SMOLVM' else 'unknown'
print(json.dumps(info))
"
}

# --- API server ---

ensure_server_running() {
    curl -sf "$BENCH_API_URL/health" > /dev/null 2>&1 && return 0

    log "Starting smolvm serve on port $BENCH_API_PORT..."
    $SMOLVM serve start -l "127.0.0.1:$BENCH_API_PORT" > /dev/null 2>&1 &
    _BENCH_SERVER_PID=$!

    local i=0
    while ! curl -sf "$BENCH_API_URL/health" > /dev/null 2>&1; do
        i=$((i + 1))
        if [[ $i -ge 30 ]]; then
            echo "Error: Server failed to start after 30s" >&2
            exit 1
        fi
        sleep 1
    done
}

stop_server() {
    if [[ -n "$_BENCH_SERVER_PID" ]]; then
        kill "$_BENCH_SERVER_PID" 2>/dev/null || true
        wait "$_BENCH_SERVER_PID" 2>/dev/null || true
        _BENCH_SERVER_PID=""
    fi
}

# --- API helpers ---

api_post() {
    curl -sf -X POST "$BENCH_API_URL$1" -H "Content-Type: application/json" -d "${2:-{}}" 2>/dev/null
}

api_delete() {
    curl -sf -X DELETE "$BENCH_API_URL$1" 2>/dev/null || true
}

# --- VM lifecycle ---

cli_delete_vm() {
    $SMOLVM microvm stop "$1" > /dev/null 2>&1 || true
    $SMOLVM microvm delete "$1" -f > /dev/null 2>&1 || true
}

cli_delete_sandbox() {
    $SMOLVM sandbox stop "$1" > /dev/null 2>&1 || true
    $SMOLVM sandbox delete "$1" -f > /dev/null 2>&1 || true
}

api_delete_vm() {
    api_post "/api/v1/microvms/$1/stop" > /dev/null 2>&1 || true
    api_delete "/api/v1/microvms/$1" > /dev/null 2>&1 || true
}

api_delete_sandbox() {
    api_post "/api/v1/sandboxes/$1/stop" > /dev/null 2>&1 || true
    api_delete "/api/v1/sandboxes/$1?force=true" > /dev/null 2>&1 || true
}

# --- Cleanup ---

cleanup_bench() {
    for vm in "${_BENCH_VMS[@]:-}"; do
        [[ -z "$vm" ]] && continue
        cli_delete_vm "$vm"
        api_delete_vm "$vm"
    done
    for sb in "${_BENCH_SANDBOXES[@]:-}"; do
        [[ -z "$sb" ]] && continue
        cli_delete_sandbox "$sb"
        api_delete_sandbox "$sb"
    done
    stop_server
}

# --- Output (all to stderr, stdout reserved for JSON) ---

log()        { echo "$@" >&2; }
log_header() { log ""; log "== $1 =="; log ""; }
log_result() { log "    Run $1: ${2}ms"; }
