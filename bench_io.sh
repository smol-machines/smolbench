#!/bin/bash
#
# Benchmark: IO throughput inside microVMs.
#
# Measures sequential read/write and small random IO via dd.
# Host baselines show virtiofs overhead.
#
# Usage: ./smolbench/bench_io.sh [iterations]
# Output: JSON to stdout

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/common.sh"

ITERATIONS="${1:-${ITERATIONS:-3}}"
VM_NAME="bench-io-$$"
DD_COUNT=128  # MiB

init_smolvm

trap 'rm -f /tmp/smolbench_write /tmp/smolbench_rand_*; cleanup_bench' EXIT

log_header "IO Throughput Benchmarks"
log "Iterations: $ITERATIONS"
log "Block size: ${DD_COUNT}MiB"

# Parse dd stderr for MB/s. Extracts bytes and seconds, computes MB/s.
parse_dd_mbps() {
    echo "$1" | python3 -c "
import re, sys
text = sys.stdin.read()
m = re.search(r'(\d+)\s+bytes.*copied,\s*([\d.]+)\s*s', text)
if m:
    b, s = int(m.group(1)), float(m.group(2))
    print(f'{b / s / 1048576:.1f}' if s > 0 else '0')
else:
    print('0')
"
}

# -- Host: Sequential write

log ""
log "  [Host] Sequential write (dd, ${DD_COUNT}MiB)"

declare -a host_write_mbps=()

for i in $(seq 1 "$ITERATIONS"); do
    output=$(dd if=/dev/zero of=/tmp/smolbench_write bs=1M count=$DD_COUNT 2>&1) || true
    mbps=$(parse_dd_mbps "$output")
    host_write_mbps+=("$mbps")
    log "    Run $i: ${mbps} MB/s"
done

# -- Host: Sequential read

log ""
log "  [Host] Sequential read (dd, ${DD_COUNT}MiB)"

declare -a host_read_mbps=()

for i in $(seq 1 "$ITERATIONS"); do
    sh -c 'echo 3 > /proc/sys/vm/drop_caches' 2>/dev/null || true
    output=$(dd if=/tmp/smolbench_write of=/dev/null bs=1M 2>&1) || true
    mbps=$(parse_dd_mbps "$output")
    host_read_mbps+=("$mbps")
    log "    Run $i: ${mbps} MB/s"
done

# -- Host: Small random IO

log ""
log "  [Host] Small random IO (4K writes x 1000)"

declare -a host_random_times=()

for i in $(seq 1 "$ITERATIONS"); do
    duration=$(measure_ms sh -c 'j=0; while [ $j -lt 1000 ]; do dd if=/dev/zero of=/tmp/smolbench_rand_$j bs=4096 count=1 2>/dev/null; j=$((j+1)); done')
    host_random_times+=("$duration")
    log_result "$i" "$duration"
done

rm -f /tmp/smolbench_write /tmp/smolbench_rand_*

# -- CLI: Setup

log "Setting up microVM for CLI tests..."
$SMOLVM microvm stop "$VM_NAME" > /dev/null 2>&1 || true
$SMOLVM microvm delete "$VM_NAME" -f > /dev/null 2>&1 || true
$SMOLVM microvm create "$VM_NAME" > /dev/null 2>&1
_BENCH_VMS+=("$VM_NAME")
$SMOLVM microvm start "$VM_NAME" > /dev/null 2>&1

# -- CLI: Sequential write

log ""
log "  [CLI] Sequential write (dd, ${DD_COUNT}MiB)"

declare -a cli_write_mbps=()

for i in $(seq 1 "$ITERATIONS"); do
    output=$($SMOLVM microvm exec --name "$VM_NAME" -- sh -c "dd if=/dev/zero of=/tmp/bench_write bs=1M count=$DD_COUNT 2>&1" 2>/dev/null) || true
    mbps=$(parse_dd_mbps "$output")
    cli_write_mbps+=("$mbps")
    log "    Run $i: ${mbps} MB/s"
done

# -- CLI: Sequential read

log ""
log "  [CLI] Sequential read (dd, ${DD_COUNT}MiB)"

declare -a cli_read_mbps=()

for i in $(seq 1 "$ITERATIONS"); do
    $SMOLVM microvm exec --name "$VM_NAME" -- sh -c "echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true" > /dev/null 2>&1 || true
    output=$($SMOLVM microvm exec --name "$VM_NAME" -- sh -c "dd if=/tmp/bench_write of=/dev/null bs=1M 2>&1" 2>/dev/null) || true
    mbps=$(parse_dd_mbps "$output")
    cli_read_mbps+=("$mbps")
    log "    Run $i: ${mbps} MB/s"
done

# -- CLI: Small random IO

log ""
log "  [CLI] Small random IO (4K writes x 1000)"

declare -a cli_random_times=()

for i in $(seq 1 "$ITERATIONS"); do
    duration=$(measure_ms $SMOLVM microvm exec --name "$VM_NAME" -- sh -c \
        "j=0; while [ \$j -lt 1000 ]; do dd if=/dev/zero of=/tmp/rand_\$j bs=4096 count=1 2>/dev/null; j=\$((j+1)); done")
    cli_random_times+=("$duration")
    log_result "$i" "$duration"
done

$SMOLVM microvm exec --name "$VM_NAME" -- sh -c "rm -f /tmp/bench_write /tmp/rand_*" > /dev/null 2>&1 || true
cli_delete_vm "$VM_NAME"

# -- API: Setup

ensure_server_running

API_VM_NAME="bench-ioapi-$$"

log ""
log "Setting up microVM for API tests..."
api_post "/api/v1/microvms/$API_VM_NAME/stop" > /dev/null 2>&1 || true
api_delete "/api/v1/microvms/$API_VM_NAME" > /dev/null 2>&1 || true
api_post "/api/v1/microvms" "{\"name\":\"$API_VM_NAME\"}" > /dev/null
_BENCH_VMS+=("$API_VM_NAME")
api_post "/api/v1/microvms/$API_VM_NAME/start" > /dev/null

# -- API: Sequential write

log ""
log "  [API] Sequential write (dd, ${DD_COUNT}MiB)"

declare -a api_write_mbps=()

for i in $(seq 1 "$ITERATIONS"); do
    response=$(api_post "/api/v1/microvms/$API_VM_NAME/exec" \
        "{\"command\":[\"sh\",\"-c\",\"dd if=/dev/zero of=/tmp/bench_write bs=1M count=$DD_COUNT 2>&1\"]}") || true
    output=$(python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('stdout','') + d.get('stderr',''))" <<< "$response") || true
    mbps=$(parse_dd_mbps "$output")
    api_write_mbps+=("$mbps")
    log "    Run $i: ${mbps} MB/s"
done

# -- API: Sequential read

log ""
log "  [API] Sequential read (dd, ${DD_COUNT}MiB)"

declare -a api_read_mbps=()

for i in $(seq 1 "$ITERATIONS"); do
    api_post "/api/v1/microvms/$API_VM_NAME/exec" \
        '{"command":["sh","-c","echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true"]}' > /dev/null 2>&1 || true
    response=$(api_post "/api/v1/microvms/$API_VM_NAME/exec" \
        "{\"command\":[\"sh\",\"-c\",\"dd if=/tmp/bench_write of=/dev/null bs=1M 2>&1\"]}") || true
    output=$(python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('stdout','') + d.get('stderr',''))" <<< "$response") || true
    mbps=$(parse_dd_mbps "$output")
    api_read_mbps+=("$mbps")
    log "    Run $i: ${mbps} MB/s"
done

# -- API: Small random IO

log ""
log "  [API] Small random IO (4K writes x 1000)"

declare -a api_random_times=()

for i in $(seq 1 "$ITERATIONS"); do
    duration=$(measure_ms curl -sf -X POST "$BENCH_API_URL/api/v1/microvms/$API_VM_NAME/exec" \
        -H "Content-Type: application/json" \
        -d '{"command":["sh","-c","j=0; while [ $j -lt 1000 ]; do dd if=/dev/zero of=/tmp/rand_$j bs=4096 count=1 2>/dev/null; j=$((j+1)); done"]}')
    api_random_times+=("$duration")
    log_result "$i" "$duration"
done

api_delete_vm "$API_VM_NAME"

# -- JSON output

host_write_stats=$(calc_stats "$(IFS=,; echo "${host_write_mbps[*]}")")
host_read_stats=$(calc_stats "$(IFS=,; echo "${host_read_mbps[*]}")")
host_random_stats=$(calc_stats "$(IFS=,; echo "${host_random_times[*]}")")
cli_write_stats=$(calc_stats "$(IFS=,; echo "${cli_write_mbps[*]}")")
cli_read_stats=$(calc_stats "$(IFS=,; echo "${cli_read_mbps[*]}")")
cli_random_stats=$(calc_stats "$(IFS=,; echo "${cli_random_times[*]}")")
api_write_stats=$(calc_stats "$(IFS=,; echo "${api_write_mbps[*]}")")
api_read_stats=$(calc_stats "$(IFS=,; echo "${api_read_mbps[*]}")")
api_random_stats=$(calc_stats "$(IFS=,; echo "${api_random_times[*]}")")

python3 -c "
import json
result = {
    'io': {
        'host': {
            'sequentialWriteMBps': $host_write_stats,
            'sequentialReadMBps': $host_read_stats,
            'randomIo4kMs': $host_random_stats
        },
        'cli': {
            'sequentialWriteMBps': $cli_write_stats,
            'sequentialReadMBps': $cli_read_stats,
            'randomIo4kMs': $cli_random_stats
        },
        'api': {
            'sequentialWriteMBps': $api_write_stats,
            'sequentialReadMBps': $api_read_stats,
            'randomIo4kMs': $api_random_stats
        },
        'config': {
            'blockSizeMb': $DD_COUNT,
            'randomWriteCount': 1000,
            'randomBlockSize': 4096
        }
    }
}
print(json.dumps(result))
"

log ""
log "IO benchmarks complete."
