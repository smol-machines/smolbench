#!/bin/bash
#
# Benchmark: CPU performance inside microVMs.
#
# Measures single-core, multi-core workloads, and exec round-trip overhead
# via both CLI and HTTP API paths. Host exec baseline for comparison.
#
# Usage: ./smolbench/bench_cpu.sh [iterations]
# Output: JSON to stdout

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/common.sh"

ITERATIONS="${1:-${ITERATIONS:-5}}"
EXEC_ITERATIONS=50
VM_NAME="bench-cpu-$$"

init_smolvm

trap cleanup_bench EXIT

log_header "CPU Performance Benchmarks"
log "Iterations:      $ITERATIONS"
log "Exec iterations: $EXEC_ITERATIONS"

# Shell-based CPU workloads — no sysbench dependency.
CPU_WORKLOAD_SINGLE='i=0; while [ $i -lt 50000 ]; do i=$((i+1)); done; echo done'
CPU_WORKLOAD_MULTI='for p in 1 2 3 4; do (i=0; while [ $i -lt 50000 ]; do i=$((i+1)); done) & done; wait; echo done'

# -- Host: Exec round-trip baseline

log ""
log "  [Host] Exec round-trip baseline ($EXEC_ITERATIONS iterations)"

declare -a host_exec_times=()

for i in $(seq 1 "$EXEC_ITERATIONS"); do
    duration=$(measure_ms echo x)
    host_exec_times+=("$duration")
done

host_exec_stats_csv=$(IFS=,; echo "${host_exec_times[*]}")
log "    Completed $EXEC_ITERATIONS iterations"

# -- CLI: Setup

log "Setting up microVM for CLI tests..."
$SMOLVM microvm stop "$VM_NAME" > /dev/null 2>&1 || true
$SMOLVM microvm delete "$VM_NAME" -f > /dev/null 2>&1 || true
$SMOLVM microvm create "$VM_NAME" --cpus 4 --mem 1024 > /dev/null 2>&1
_BENCH_VMS+=("$VM_NAME")
$SMOLVM microvm start "$VM_NAME" > /dev/null 2>&1

# -- CLI: Single-core CPU workload

log ""
log "  [CLI] Single-core CPU workload"

declare -a cli_single_times=()

for i in $(seq 1 "$ITERATIONS"); do
    duration=$(measure_ms $SMOLVM microvm exec --name "$VM_NAME" -- sh -c "$CPU_WORKLOAD_SINGLE")
    cli_single_times+=("$duration")
    log_result "$i" "$duration"
done

# -- CLI: Multi-core CPU workload

log ""
log "  [CLI] Multi-core CPU workload (4 parallel)"

declare -a cli_multi_times=()

for i in $(seq 1 "$ITERATIONS"); do
    duration=$(measure_ms $SMOLVM microvm exec --name "$VM_NAME" -- sh -c "$CPU_WORKLOAD_MULTI")
    cli_multi_times+=("$duration")
    log_result "$i" "$duration"
done

# -- CLI: Exec round-trip overhead

log ""
log "  [CLI] Exec round-trip overhead ($EXEC_ITERATIONS iterations)"

declare -a cli_exec_times=()

for i in $(seq 1 "$EXEC_ITERATIONS"); do
    duration=$(measure_ms $SMOLVM microvm exec --name "$VM_NAME" echo x)
    cli_exec_times+=("$duration")
done

cli_exec_stats_csv=$(IFS=,; echo "${cli_exec_times[*]}")
log "    Completed $EXEC_ITERATIONS iterations"

cli_delete_vm "$VM_NAME"

# -- API: Setup

ensure_server_running

API_VM_NAME="bench-cpuapi-$$"

log ""
log "Setting up microVM for API tests..."
api_post "/api/v1/microvms/$API_VM_NAME/stop" > /dev/null 2>&1 || true
api_delete "/api/v1/microvms/$API_VM_NAME" > /dev/null 2>&1 || true
api_post "/api/v1/microvms" "{\"name\":\"$API_VM_NAME\",\"cpus\":4,\"memoryMb\":1024}" > /dev/null
_BENCH_VMS+=("$API_VM_NAME")
api_post "/api/v1/microvms/$API_VM_NAME/start" > /dev/null

# -- API: Single-core CPU workload

log ""
log "  [API] Single-core CPU workload"

declare -a api_single_times=()

for i in $(seq 1 "$ITERATIONS"); do
    duration=$(measure_ms curl -sf -X POST "$BENCH_API_URL/api/v1/microvms/$API_VM_NAME/exec" \
        -H "Content-Type: application/json" \
        -d "{\"command\":[\"sh\",\"-c\",\"$CPU_WORKLOAD_SINGLE\"]}")
    api_single_times+=("$duration")
    log_result "$i" "$duration"
done

# -- API: Multi-core CPU workload

log ""
log "  [API] Multi-core CPU workload (4 parallel)"

declare -a api_multi_times=()

for i in $(seq 1 "$ITERATIONS"); do
    duration=$(measure_ms curl -sf -X POST "$BENCH_API_URL/api/v1/microvms/$API_VM_NAME/exec" \
        -H "Content-Type: application/json" \
        -d "{\"command\":[\"sh\",\"-c\",\"$CPU_WORKLOAD_MULTI\"]}")
    api_multi_times+=("$duration")
    log_result "$i" "$duration"
done

# -- API: Exec round-trip overhead

log ""
log "  [API] Exec round-trip overhead ($EXEC_ITERATIONS iterations)"

declare -a api_exec_times=()

for i in $(seq 1 "$EXEC_ITERATIONS"); do
    duration=$(measure_ms curl -sf -X POST "$BENCH_API_URL/api/v1/microvms/$API_VM_NAME/exec" \
        -H "Content-Type: application/json" \
        -d '{"command":["echo","x"]}')
    api_exec_times+=("$duration")
done

api_exec_stats_csv=$(IFS=,; echo "${api_exec_times[*]}")
log "    Completed $EXEC_ITERATIONS iterations"

api_delete_vm "$API_VM_NAME"

# -- JSON output

host_exec_stats=$(calc_stats "$host_exec_stats_csv")
cli_single_stats=$(calc_stats "$(IFS=,; echo "${cli_single_times[*]}")")
cli_multi_stats=$(calc_stats "$(IFS=,; echo "${cli_multi_times[*]}")")
cli_exec_stats=$(calc_stats "$cli_exec_stats_csv")
api_single_stats=$(calc_stats "$(IFS=,; echo "${api_single_times[*]}")")
api_multi_stats=$(calc_stats "$(IFS=,; echo "${api_multi_times[*]}")")
api_exec_stats=$(calc_stats "$api_exec_stats_csv")

python3 -c "
import json
result = {
    'cpu': {
        'host': {
            'execRoundTrip': $host_exec_stats
        },
        'cli': {
            'singleCore': $cli_single_stats,
            'multiCore': $cli_multi_stats,
            'execRoundTrip': $cli_exec_stats
        },
        'api': {
            'singleCore': $api_single_stats,
            'multiCore': $api_multi_stats,
            'execRoundTrip': $api_exec_stats
        }
    }
}
print(json.dumps(result))
"

log ""
log "CPU benchmarks complete."
