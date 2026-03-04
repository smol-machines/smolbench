#!/bin/bash
#
# Benchmark: Network performance inside microVMs (requires --net).
#
# Measures DNS resolution and HTTP round-trip latency via CLI and API.
# Host baselines show virtual network stack overhead.
# Gracefully skips VM tests if network is unavailable inside the guest.
#
# Usage: ./smolbench/bench_network.sh [iterations]
# Output: JSON to stdout

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/common.sh"

ITERATIONS="${1:-${ITERATIONS:-5}}"
VM_NAME="bench-net-$$"

DNS_TARGET="example.com"
HTTP_TARGET="http://example.com"

init_smolvm

trap cleanup_bench EXIT

log_header "Network Benchmarks"
log "Iterations: $ITERATIONS"

# -- Host: DNS resolution (baseline)

log ""
log "  [Host] DNS resolution (nslookup $DNS_TARGET)"

declare -a host_dns_times=()

for i in $(seq 1 "$ITERATIONS"); do
    duration=$(measure_ms sh -c "nslookup $DNS_TARGET > /dev/null 2>&1 || getent hosts $DNS_TARGET > /dev/null 2>&1")
    host_dns_times+=("$duration")
    log_result "$i" "$duration"
done

# -- Host: HTTP round-trip (baseline)

log ""
log "  [Host] HTTP round-trip (wget $HTTP_TARGET)"

declare -a host_http_times=()

for i in $(seq 1 "$ITERATIONS"); do
    duration=$(measure_ms wget -q -O /dev/null --timeout=10 "$HTTP_TARGET")
    host_http_times+=("$duration")
    log_result "$i" "$duration"
done

# -- Setup: Create VM with networking

log "Setting up microVM with networking..."
$SMOLVM microvm stop "$VM_NAME" > /dev/null 2>&1 || true
$SMOLVM microvm delete "$VM_NAME" -f > /dev/null 2>&1 || true
$SMOLVM microvm create "$VM_NAME" --net > /dev/null 2>&1
_BENCH_VMS+=("$VM_NAME")
$SMOLVM microvm start "$VM_NAME" > /dev/null 2>&1

# -- Check VM network connectivity

log "Checking network connectivity..."
net_check=$($SMOLVM microvm exec --name "$VM_NAME" -- sh -c "wget -q -O /dev/null --timeout=5 $HTTP_TARGET 2>&1 && echo ok || echo fail" 2>/dev/null) || true

if [[ "$net_check" != *"ok"* ]]; then
    log "  Network unavailable inside VM - skipping VM benchmarks"

    host_dns_stats=$(calc_stats "$(IFS=,; echo "${host_dns_times[*]}")")
    host_http_stats=$(calc_stats "$(IFS=,; echo "${host_http_times[*]}")")

    python3 -c "
import json
result = {
    'network': {
        'skipped': False,
        'host': {
            'dnsResolutionMs': $host_dns_stats,
            'httpRoundTripMs': $host_http_stats
        },
        'cli': {},
        'api': {},
        'config': {
            'dnsTarget': '$DNS_TARGET',
            'httpTarget': '$HTTP_TARGET',
            'vmNetworkUnavailable': True
        }
    }
}
print(json.dumps(result))
"
    exit 0
fi

log "  Network available"

# -- CLI: DNS resolution

log ""
log "  [CLI] DNS resolution (nslookup $DNS_TARGET)"

declare -a cli_dns_times=()

for i in $(seq 1 "$ITERATIONS"); do
    duration=$(measure_ms $SMOLVM microvm exec --name "$VM_NAME" -- sh -c \
        "nslookup $DNS_TARGET > /dev/null 2>&1 || getent hosts $DNS_TARGET > /dev/null 2>&1")
    cli_dns_times+=("$duration")
    log_result "$i" "$duration"
done

# -- CLI: HTTP round-trip

log ""
log "  [CLI] HTTP round-trip (wget $HTTP_TARGET)"

declare -a cli_http_times=()

for i in $(seq 1 "$ITERATIONS"); do
    duration=$(measure_ms $SMOLVM microvm exec --name "$VM_NAME" -- \
        wget -q -O /dev/null --timeout=10 "$HTTP_TARGET")
    cli_http_times+=("$duration")
    log_result "$i" "$duration"
done

cli_delete_vm "$VM_NAME"

# -- API: Setup

ensure_server_running

API_VM_NAME="bench-netapi-$$"

log ""
log "Setting up microVM for API tests..."
api_post "/api/v1/microvms/$API_VM_NAME/stop" > /dev/null 2>&1 || true
api_delete "/api/v1/microvms/$API_VM_NAME" > /dev/null 2>&1 || true
api_post "/api/v1/microvms" "{\"name\":\"$API_VM_NAME\",\"network\":true}" > /dev/null
_BENCH_VMS+=("$API_VM_NAME")
api_post "/api/v1/microvms/$API_VM_NAME/start" > /dev/null

# -- API: DNS resolution

log ""
log "  [API] DNS resolution"

declare -a api_dns_times=()

for i in $(seq 1 "$ITERATIONS"); do
    duration=$(measure_ms curl -sf -X POST "$BENCH_API_URL/api/v1/microvms/$API_VM_NAME/exec" \
        -H "Content-Type: application/json" \
        -d "{\"command\":[\"sh\",\"-c\",\"nslookup $DNS_TARGET > /dev/null 2>&1 || getent hosts $DNS_TARGET > /dev/null 2>&1\"]}")
    api_dns_times+=("$duration")
    log_result "$i" "$duration"
done

# -- API: HTTP round-trip

log ""
log "  [API] HTTP round-trip"

declare -a api_http_times=()

for i in $(seq 1 "$ITERATIONS"); do
    duration=$(measure_ms curl -sf -X POST "$BENCH_API_URL/api/v1/microvms/$API_VM_NAME/exec" \
        -H "Content-Type: application/json" \
        -d "{\"command\":[\"wget\",\"-q\",\"-O\",\"/dev/null\",\"--timeout=10\",\"$HTTP_TARGET\"]}")
    api_http_times+=("$duration")
    log_result "$i" "$duration"
done

api_delete_vm "$API_VM_NAME"

# -- JSON output

host_dns_stats=$(calc_stats "$(IFS=,; echo "${host_dns_times[*]}")")
host_http_stats=$(calc_stats "$(IFS=,; echo "${host_http_times[*]}")")
cli_dns_stats=$(calc_stats "$(IFS=,; echo "${cli_dns_times[*]}")")
cli_http_stats=$(calc_stats "$(IFS=,; echo "${cli_http_times[*]}")")
api_dns_stats=$(calc_stats "$(IFS=,; echo "${api_dns_times[*]}")")
api_http_stats=$(calc_stats "$(IFS=,; echo "${api_http_times[*]}")")

python3 -c "
import json
result = {
    'network': {
        'skipped': False,
        'host': {
            'dnsResolutionMs': $host_dns_stats,
            'httpRoundTripMs': $host_http_stats
        },
        'cli': {
            'dnsResolutionMs': $cli_dns_stats,
            'httpRoundTripMs': $cli_http_stats
        },
        'api': {
            'dnsResolutionMs': $api_dns_stats,
            'httpRoundTripMs': $api_http_stats
        },
        'config': {
            'dnsTarget': '$DNS_TARGET',
            'httpTarget': '$HTTP_TARGET'
        }
    }
}
print(json.dumps(result))
"

log ""
log "Network benchmarks complete."
