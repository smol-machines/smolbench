#!/bin/bash
#
# Run all benchmark suites and produce a combined JSON report.
#
# Usage:
#   ./smolbench/run_all.sh
#   ./smolbench/run_all.sh --suites cold_start,cpu
#   ./smolbench/run_all.sh --iterations 10
#   ./smolbench/run_all.sh --output /path/to/report.json

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/common.sh"

SUITES="cold_start,cpu,io,network"
OUTPUT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --suites)     SUITES="$2"; shift 2 ;;
        --iterations) ITERATIONS="$2"; shift 2 ;;
        --output)     OUTPUT="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 [--suites LIST] [--iterations N] [--output PATH]"
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$OUTPUT" ]]; then
    mkdir -p "$RESULTS_DIR"
    OUTPUT="$RESULTS_DIR/bench-$(date +%Y%m%d-%H%M%S).json"
fi

export ITERATIONS BENCH_API_PORT

init_smolvm

log_header "smolvm Performance Benchmark Suite"
log "Suites:     $SUITES"
log "Iterations: $ITERATIONS"
log "Binary:     $SMOLVM"
log "Output:     $OUTPUT"

SYSTEM_INFO=$(collect_system_info)
TMPDIR_BENCH=$(mktemp -d)

trap 'rm -rf "$TMPDIR_BENCH"' EXIT

IFS=',' read -ra SUITE_LIST <<< "$SUITES"
for suite in "${SUITE_LIST[@]}"; do
    suite="${suite// /}"
    script="$SCRIPT_DIR/bench_${suite}.sh"

    if [[ ! -x "$script" ]]; then
        log "Warning: $script not found or not executable"
        continue
    fi

    log ""
    log "  Running suite: $suite"

    if "$script" "$ITERATIONS" > "$TMPDIR_BENCH/${suite}.json"; then
        log "  $suite: ok"
    else
        log "  $suite: FAILED"
        echo "{}" > "$TMPDIR_BENCH/${suite}.json"
    fi
done

# Merge results into final report
python3 << PYEOF
import json, glob, os, sys

system = json.loads('''$SYSTEM_INFO''')
results = {}
for f in sorted(glob.glob("$TMPDIR_BENCH/*.json")):
    try:
        with open(f) as fh:
            results.update(json.load(fh))
    except Exception as e:
        results[os.path.basename(f).replace(".json", "")] = {"error": str(e)}

report = {
    "system": system,
    "config": {"iterations": $ITERATIONS, "suites": "$SUITES".split(",")},
    "results": results,
}

with open("$OUTPUT", "w") as f:
    json.dump(report, f, indent=2)

# Summary
print("", file=sys.stderr)
print(f"  System: {system.get('cpu', '?')} / {system.get('arch', '?')}", file=sys.stderr)
print(f"  smolvm: {system.get('smolvmVersion', '?')}", file=sys.stderr)
print("", file=sys.stderr)
for name, data in results.items():
    if not isinstance(data, dict):
        continue
    if data.get("skipped"):
        print(f"  {name}: SKIPPED", file=sys.stderr)
        continue
    if "error" in data:
        print(f"  {name}: ERROR ({data['error']})", file=sys.stderr)
        continue
    for path in ["host", "cli", "api"]:
        for test, stats in data.get(path, {}).items():
            if isinstance(stats, dict) and "avg" in stats:
                unit = "MB/s" if "MBps" in test else "ms"
                print(f"  [{path}] {name}/{test}: {stats['avg']}{unit} (+/-{stats.get('stdDev', 0)})", file=sys.stderr)
print("", file=sys.stderr)
PYEOF

log "Report: $OUTPUT"
