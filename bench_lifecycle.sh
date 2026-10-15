#!/bin/bash
#
# Benchmark: machine lifecycle operations.
#
# Times the operations most workflows are built from: start, stop, one-shot
# runs, exec, file copy, checkpoint and restore, branch, pack and pack run.
# Every timed operation must succeed and its result is checked, so a broken
# binary fails instead of looking fast.
#
# Usage: ./smolbench/bench_lifecycle.sh [iterations]
# Output: JSON to stdout

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/common.sh"

ITERATIONS="${1:-${ITERATIONS:-5}}"
IMAGE="${BENCH_IMAGE:-alpine:3.20}"
CP_MIB="${BENCH_CP_MIB:-256}"
PREFIX="bench-lc-$$"
WORK="$(mktemp -d)"

init_smolvm

_LC_MACHINES=()

cleanup_lifecycle() {
    for vm in "${_LC_MACHINES[@]:-}"; do
        [[ -z "$vm" ]] && continue
        $SMOLVM machine delete --name "$vm" --cascade > /dev/null 2>&1 || true
        $SMOLVM machine delete --name "$vm" -f > /dev/null 2>&1 || true
    done
    rm -rf "$WORK"
}
trap cleanup_lifecycle EXIT

# Run a command and print how long it took in ms, measured inside one python
# process so interpreter startup is not part of the sample. Its stdout goes
# to $WORK/out. A failing command fails the suite.
timed() {
    python3 - "$WORK/out" "$@" << 'PYEOF'
import subprocess, sys, time
out, cmd = sys.argv[1], sys.argv[2:]
start = time.perf_counter()
proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
ms = (time.perf_counter() - start) * 1000
open(out, "wb").write(proc.stdout)
if proc.returncode != 0:
    sys.stderr.write(f"{' '.join(cmd)} exited {proc.returncode}: {proc.stderr.decode()[-400:]}\n")
    sys.exit(1)
print(round(ms, 1))
PYEOF
}

# Fail the suite unless the last timed command printed exactly $1.
expect_out() {
    local got
    got="$(tr -d '\r' < "$WORK/out" | sed -e 's/[[:space:]]*$//')"
    if [[ "$got" != "$1" ]]; then
        log "    expected '$1', got '$got'"
        exit 1
    fi
}

machine() {
    $SMOLVM machine "$@" > /dev/null 2>&1
}

new_machine() {
    _LC_MACHINES+=("$1")
    machine create --name "$1" "${@:2}"
}

stats() {
    calc_stats "$(IFS=,; echo "$*")"
}

log_header "Lifecycle Benchmarks"
log "Iterations: $ITERATIONS"
log "Binary:     $SMOLVM"
log "Image:      $IMAGE"

# -- start / stop

log ""
log "  [CLI] machine start / stop (bare machine)"

declare -a start_times=() stop_times=()
VM="$PREFIX-ss"
new_machine "$VM"
machine start --name "$VM"
machine stop --name "$VM"
for i in $(seq 1 "$ITERATIONS"); do
    start_times+=("$(timed $SMOLVM machine start --name "$VM")")
    timed $SMOLVM machine exec --name "$VM" -- echo up > /dev/null
    expect_out up
    stop_times+=("$(timed $SMOLVM machine stop --name "$VM")")
    log "    Run $i: start ${start_times[$((i - 1))]}ms, stop ${stop_times[$((i - 1))]}ms"
done

# -- run, bare and image

log ""
log "  [CLI] machine run (bare, and $IMAGE with a warm seed)"

declare -a run_bare_times=() run_image_times=()
image_ok=1
$SMOLVM machine run --net --image "$IMAGE" -- true > /dev/null 2>&1 || image_ok=0
[[ $image_ok == 1 ]] || log "    $IMAGE could not be pulled; skipping image runs"
for i in $(seq 1 "$ITERATIONS"); do
    run_bare_times+=("$(timed $SMOLVM machine run -- echo bare)")
    expect_out bare
    if [[ $image_ok == 1 ]]; then
        run_image_times+=("$(timed $SMOLVM machine run --net --image "$IMAGE" -- echo image)")
        expect_out image
    fi
    log "    Run $i: bare ${run_bare_times[$((i - 1))]}ms${run_image_times[$((i - 1))]:+, image ${run_image_times[$((i - 1))]}ms}"
done

# -- exec and cp on a running machine

log ""
log "  [CLI] machine exec, and machine cp of ${CP_MIB} MiB each way"

declare -a exec_times=() up_rates=() down_rates=()
VM="$PREFIX-run"
new_machine "$VM"
machine start --name "$VM"
dd if=/dev/urandom of="$WORK/up.bin" bs=1048576 count="$CP_MIB" 2> /dev/null
for i in $(seq 1 "$ITERATIONS"); do
    exec_times+=("$(timed $SMOLVM machine exec --name "$VM" -- echo ok)")
    expect_out ok
    up_ms=$(timed $SMOLVM machine cp "$WORK/up.bin" "$VM:/workspace/up.bin")
    rm -f "$WORK/down.bin"
    down_ms=$(timed $SMOLVM machine cp "$VM:/workspace/up.bin" "$WORK/down.bin")
    cmp -s "$WORK/up.bin" "$WORK/down.bin" || { log "    copied file differs"; exit 1; }
    up_rates+=("$(python3 -c "print(round($CP_MIB * 1.048576 / ($up_ms / 1000), 1))")")
    down_rates+=("$(python3 -c "print(round($CP_MIB * 1.048576 / ($down_ms / 1000), 1))")")
    log "    Run $i: exec ${exec_times[$((i - 1))]}ms, up ${up_rates[$((i - 1))]} MB/s, down ${down_rates[$((i - 1))]} MB/s"
done

# -- checkpoint and restore

log ""
log "  [CLI] machine checkpoint, and restore to first exec"

# A machine of its own: the one above holds the copied file in its page cache,
# which would make every checkpoint image larger.
declare -a checkpoint_times=() pause_times=() restore_times=()
VM="$PREFIX-ckpt"
new_machine "$VM"
machine start --name "$VM"
$SMOLVM machine exec --name "$VM" -- sh -c "echo state > /workspace/s" > /dev/null 2>&1
for i in $(seq 1 "$ITERATIONS"); do
    rm -f "$WORK/c.checkpoint"
    checkpoint_times+=("$(timed $SMOLVM machine checkpoint --name "$VM" -o "$WORK/c.checkpoint")")
    pause=$(sed -n 's/.* \([0-9.]*\)s source pause.*/\1/p' "$WORK/out")
    [[ -n "$pause" ]] || { log "    checkpoint printed no source pause"; exit 1; }
    pause_times+=("$(python3 -c "print(round($pause * 1000, 1))")")

    restored="$PREFIX-restored"
    _LC_MACHINES+=("$restored")
    a=$(timed $SMOLVM machine create --name "$restored" --from "$WORK/c.checkpoint")
    b=$(timed $SMOLVM machine start --name "$restored")
    c=$(timed $SMOLVM machine exec --name "$restored" -- cat /workspace/s)
    expect_out state
    restore_times+=("$(python3 -c "print(round($a + $b + $c, 1))")")
    machine delete --name "$restored" -f
    log "    Run $i: checkpoint ${checkpoint_times[$((i - 1))]}ms (pause ${pause_times[$((i - 1))]}ms), restore ${restore_times[$((i - 1))]}ms"
done

# -- branch

log ""
log "  [CLI] machine branch (one named child of a --branchable source)"

declare -a branch_times=()
SRC="$PREFIX-src"
new_machine "$SRC"
$SMOLVM machine start --name "$SRC" --branchable > /dev/null 2>&1
for i in $(seq 1 "$ITERATIONS"); do
    child="$PREFIX-child"
    _LC_MACHINES+=("$child")
    branch_times+=("$(timed $SMOLVM machine branch --from "$SRC" -n "$child")")
    timed $SMOLVM machine exec --name "$child" -- echo child > /dev/null
    expect_out child
    machine delete --name "$child" -f
    log_result "$i" "${branch_times[$((i - 1))]}"
done

# -- pack create and pack run

log ""
log "  [CLI] pack create --from-vm, and a warm pack run"

declare -a pack_times=() pack_run_times=()
VM="$PREFIX-pack"
new_machine "$VM"
machine start --name "$VM"
machine stop --name "$VM"
for i in $(seq 1 "$ITERATIONS"); do
    rm -f "$WORK/p" "$WORK/p.smolmachine"
    pack_times+=("$(timed $SMOLVM pack create --from-vm "$VM" -o "$WORK/p")")
    [[ -f "$WORK/p.smolmachine" ]] || { log "    pack produced no .smolmachine"; exit 1; }
    [[ $i == 1 ]] && $SMOLVM pack run --sidecar "$WORK/p.smolmachine" -- true > /dev/null 2>&1
    pack_run_times+=("$(timed $SMOLVM pack run --sidecar "$WORK/p.smolmachine" -- echo packed)")
    expect_out packed
    log "    Run $i: pack ${pack_times[$((i - 1))]}ms, pack run ${pack_run_times[$((i - 1))]}ms"
done

# -- JSON output

if [[ $image_ok == 1 ]]; then
    run_image_stats=$(stats "${run_image_times[@]}")
else
    run_image_stats="{}"
fi

python3 -c "
import json
result = {
    'lifecycle': {
        'cli': {
            'startMs': $(stats "${start_times[@]}"),
            'stopMs': $(stats "${stop_times[@]}"),
            'runBareMs': $(stats "${run_bare_times[@]}"),
            'runImageMs': $run_image_stats,
            'execMs': $(stats "${exec_times[@]}"),
            'cpUploadMBps': $(stats "${up_rates[@]}"),
            'cpDownloadMBps': $(stats "${down_rates[@]}"),
            'checkpointMs': $(stats "${checkpoint_times[@]}"),
            'checkpointPauseMs': $(stats "${pause_times[@]}"),
            'restoreMs': $(stats "${restore_times[@]}"),
            'branchMs': $(stats "${branch_times[@]}"),
            'packCreateMs': $(stats "${pack_times[@]}"),
            'packRunMs': $(stats "${pack_run_times[@]}"),
        }
    }
}
print(json.dumps(result))
"

log ""
log "Lifecycle benchmarks complete."
