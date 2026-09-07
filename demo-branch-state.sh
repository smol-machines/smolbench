#!/usr/bin/env bash
set -euo pipefail

fanout="${FANOUT:-4}"
parallel="${PARALLEL:-$fanout}"
image="${IMAGE:-alpine:3.20}"
if [[ ! "$fanout" =~ ^[1-9][0-9]*$ || ! "$parallel" =~ ^[1-9][0-9]*$ ]]; then
  printf 'FANOUT and PARALLEL must be positive integers.\n' >&2
  exit 2
fi

stamp="$(date -u +%s)-$$"
golden="branch-state-$stamp"
prefix="branch-state-child-$stamp"

cleanup() {
  local index name
  for ((index = 0; index < fanout; index++)); do
    name="$prefix-$index"
    smolvm machine delete --name "$name" >/dev/null 2>&1 || true
  done
  smolvm machine delete --name "$golden" >/dev/null 2>&1 || true
}
trap cleanup EXIT

smolvm machine create \
  --name "$golden" --image "$image" --cpus 1 --mem 512 --overlay 1 --net \
  -- /bin/sh -lc 'while :; do sleep 3600; done' >/dev/null
smolvm machine start --name "$golden" --forkable >/dev/null
smolvm machine exec --name "$golden" /bin/sh -lc \
  'printf shared-disk > /root/branch-state-disk; printf shared-ram > /dev/shm/branch-state-ram; test -x /usr/local/bin/smolvm-fork-ready' \
  >/dev/null
smolvm machine exec --name "$golden" --detach \
  /usr/local/bin/smolvm-fork-ready >/dev/null
sleep 1

started="$(python3 -c 'import time; print(time.monotonic())')"
smolvm machine fork \
  --golden "$golden" --count "$fanout" --name-prefix "$prefix" \
  --parallel "$parallel" >/dev/null
finished="$(python3 -c 'import time; print(time.monotonic())')"

for ((index = 0; index < fanout; index++)); do
  name="$prefix-$index"
  inherited="$(
    smolvm machine exec --name "$name" /bin/sh -lc \
      'printf "%s/%s" "$(cat /root/branch-state-disk)" "$(cat /dev/shm/branch-state-ram)"'
  )"
  if [[ "$inherited" != "shared-disk/shared-ram" ]]; then
    printf '%s inherited invalid state: %s\n' "$name" "$inherited" >&2
    exit 1
  fi
  smolvm machine exec --name "$name" /bin/sh -lc \
    "printf unique-$index > /root/branch-state-unique" >/dev/null
done

for ((index = 0; index < fanout; index++)); do
  name="$prefix-$index"
  unique="$(smolvm machine exec --name "$name" cat /root/branch-state-unique)"
  if [[ "$unique" != "unique-$index" ]]; then
    printf '%s isolation failure: %s\n' "$name" "$unique" >&2
    exit 1
  fi
done

python3 - "$started" "$finished" "$fanout" <<'PY'
import sys

elapsed = float(sys.argv[2]) - float(sys.argv[1])
print(f"PASS: {sys.argv[3]} branches in {elapsed:.3f}s")
print("Inherited disk state: PASS")
print("Inherited RAM state:  PASS")
print("Child isolation:      PASS")
PY
