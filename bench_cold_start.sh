#!/bin/bash
#
# Benchmark: Cold start times for microVMs and sandboxes.
#
# Measures startup latency via both CLI and HTTP API paths.
#
# Usage: ./smolbench/bench_cold_start.sh [iterations]
# Output: JSON to stdout

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/common.sh"

ITERATIONS="${1:-${ITERATIONS:-5}}"
VM_NAME="bench-cs-$$"

init_smolvm

trap cleanup_bench EXIT

log_header "Cold Start Benchmarks"
log "Iterations: $ITERATIONS"
log "Binary:     $SMOLVM"

# -- CLI: MicroVM cold start (stop -> start)

log ""
log "  [CLI] MicroVM cold start (stop -> start)"
log "  Measures: fork -> kernel boot -> init -> agent ready"

declare -a cli_vm_start_times=()

$SMOLVM microvm stop "$VM_NAME" > /dev/null 2>&1 || true
$SMOLVM microvm delete "$VM_NAME" -f > /dev/null 2>&1 || true
$SMOLVM microvm create "$VM_NAME" > /dev/null 2>&1
_BENCH_VMS+=("$VM_NAME")

for i in $(seq 1 "$ITERATIONS"); do
    $SMOLVM microvm stop "$VM_NAME" > /dev/null 2>&1 || true
    sleep 0.5

    duration=$(measure_ms $SMOLVM microvm start "$VM_NAME")
    cli_vm_start_times+=("$duration")
    log_result "$i" "$duration"
done

# -- CLI: MicroVM start + first exec

log ""
log "  [CLI] MicroVM start + first exec"
log "  Measures: cold start + first vsock round-trip"

declare -a cli_vm_exec_times=()

for i in $(seq 1 "$ITERATIONS"); do
    $SMOLVM microvm stop "$VM_NAME" > /dev/null 2>&1 || true
    sleep 0.5

    start=$(now_ms)
    $SMOLVM microvm start "$VM_NAME" > /dev/null 2>&1
    for _attempt in $(seq 1 5); do
        if $SMOLVM microvm exec --name "$VM_NAME" echo hello > /dev/null 2>&1; then
            break
        fi
        sleep 0.3
    done
    end=$(now_ms)

    duration=$(( end - start ))
    cli_vm_exec_times+=("$duration")
    log_result "$i" "$duration"
done

cli_delete_vm "$VM_NAME"

# -- CLI: Sandbox create + start

log ""
log "  [CLI] Sandbox create + start"

declare -a cli_sb_times=()
SB_NAME="bench-sb-$$"

for i in $(seq 1 "$ITERATIONS"); do
    cli_delete_sandbox "$SB_NAME" 2>/dev/null || true
    sleep 0.5

    start=$(now_ms)
    $SMOLVM sandbox create "$SB_NAME" > /dev/null 2>&1
    $SMOLVM sandbox start "$SB_NAME" > /dev/null 2>&1
    end=$(now_ms)

    duration=$(( end - start ))
    cli_sb_times+=("$duration")
    log_result "$i" "$duration"
done

cli_delete_sandbox "$SB_NAME"

# -- API: MicroVM cold start (stop -> start)

ensure_server_running

API_VM_NAME="bench-csapi-$$"

log ""
log "  [API] MicroVM cold start (stop -> start)"

declare -a api_vm_start_times=()

api_post "/api/v1/microvms" "{\"name\":\"$API_VM_NAME\"}" > /dev/null
_BENCH_VMS+=("$API_VM_NAME")

for i in $(seq 1 "$ITERATIONS"); do
    api_post "/api/v1/microvms/$API_VM_NAME/stop" > /dev/null 2>&1 || true
    sleep 0.5

    duration=$(measure_ms curl -sf -X POST "$BENCH_API_URL/api/v1/microvms/$API_VM_NAME/start")
    api_vm_start_times+=("$duration")
    log_result "$i" "$duration"
done

# -- API: MicroVM start + first exec

log ""
log "  [API] MicroVM start + first exec"

declare -a api_vm_exec_times=()

for i in $(seq 1 "$ITERATIONS"); do
    api_post "/api/v1/microvms/$API_VM_NAME/stop" > /dev/null 2>&1 || true
    sleep 0.5

    start=$(now_ms)
    api_post "/api/v1/microvms/$API_VM_NAME/start" > /dev/null
    for _attempt in $(seq 1 5); do
        if api_post "/api/v1/microvms/$API_VM_NAME/exec" '{"command":["echo","hello"]}' > /dev/null 2>&1; then
            break
        fi
        sleep 0.3
    done
    end=$(now_ms)

    duration=$(( end - start ))
    api_vm_exec_times+=("$duration")
    log_result "$i" "$duration"
done

api_delete_vm "$API_VM_NAME"

# -- API: Sandbox create + start

log ""
log "  [API] Sandbox create + start"

declare -a api_sb_times=()
API_SB_NAME="bench-sbapi-$$"
_BENCH_SANDBOXES+=("$API_SB_NAME")

for i in $(seq 1 "$ITERATIONS"); do
    api_delete_sandbox "$API_SB_NAME" 2>/dev/null || true
    sleep 0.5

    start=$(now_ms)
    api_post "/api/v1/sandboxes" "{\"name\":\"$API_SB_NAME\"}" > /dev/null
    api_post "/api/v1/sandboxes/$API_SB_NAME/start" > /dev/null
    end=$(now_ms)

    duration=$(( end - start ))
    api_sb_times+=("$duration")
    log_result "$i" "$duration"
done

# -- JSON output

cli_vm_start_stats=$(calc_stats "$(IFS=,; echo "${cli_vm_start_times[*]}")")
cli_vm_exec_stats=$(calc_stats "$(IFS=,; echo "${cli_vm_exec_times[*]}")")
cli_sb_stats=$(calc_stats "$(IFS=,; echo "${cli_sb_times[*]}")")
api_vm_start_stats=$(calc_stats "$(IFS=,; echo "${api_vm_start_times[*]}")")
api_vm_exec_stats=$(calc_stats "$(IFS=,; echo "${api_vm_exec_times[*]}")")
api_sb_stats=$(calc_stats "$(IFS=,; echo "${api_sb_times[*]}")")

python3 -c "
import json
result = {
    'coldStart': {
        'cli': {
            'microvmStart': $cli_vm_start_stats,
            'microvmStartExec': $cli_vm_exec_stats,
            'sandboxCreateStart': $cli_sb_stats
        },
        'api': {
            'microvmStart': $api_vm_start_stats,
            'microvmStartExec': $api_vm_exec_stats,
            'sandboxCreateStart': $api_sb_stats
        }
    }
}
print(json.dumps(result))
"

log ""
log "Cold start benchmarks complete."
