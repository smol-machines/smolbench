#!/usr/bin/env python3
"""Deterministic process-shaped workload for native-container/SmolVM controls."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import zlib


def setting(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def branch_environment() -> dict[str, str]:
    values: dict[str, str] = {}
    path = Path("/etc/smolvm/branch-env")
    if path.is_file():
        for line in path.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
    return values


def prepare_shared_state(size_mib: int) -> bytes:
    seed = hashlib.sha256(b"smolvm-cpu-parity-v1").digest()
    byte_count = size_mib * 1024 * 1024
    return (seed * ((byte_count + len(seed) - 1) // len(seed)))[:byte_count]


def execute(shared: bytes, task_id: int, rounds: int) -> tuple[str, int]:
    request = json.dumps(
        {
            "task": task_id,
            "route": "/v1/evaluate",
            "claims": ["read", "transform", "score"],
            "payload": "agent-evaluation-" * 128,
        },
        separators=(",", ":"),
    ).encode()
    pattern = re.compile(rb'"(?:task|route|claims|payload)"')
    digest = hashlib.sha256()
    digest.update(request)
    checksum = 0
    for round_index in range(rounds):
        offset = ((task_id + round_index) * 4096) % max(4096, len(shared) - 1024 * 1024)
        block = shared[offset : offset + 1024 * 1024]
        digest.update(block)
        encoded = zlib.compress(request + block[:32768], level=6)
        decoded = json.loads(request)
        checksum = (
            checksum * 1_000_003
            + len(encoded)
            + len(pattern.findall(request))
            + decoded["task"]
            + round_index
        ) & ((1 << 64) - 1)
    digest.update(checksum.to_bytes(8, "little"))
    return digest.hexdigest(), checksum


def main() -> None:
    init_start = time.monotonic_ns()
    shared = prepare_shared_state(setting("STATE_MIB", 128))
    init_ms = (time.monotonic_ns() - init_start) / 1_000_000

    if os.environ.get("BRANCH_MODE") == "1":
        subprocess.run(["smolvm-branch-ready"], check=True)

    branch_env = branch_environment()
    task_id = int(
        branch_env.get(
            "TASK_ID",
            branch_env.get("SMOLVM_BRANCH_INDEX", os.environ.get("TASK_ID", "0")),
        )
    )
    work_start = time.monotonic_ns()
    digest, checksum = execute(shared, task_id, setting("ROUNDS", 256))
    work_ms = (time.monotonic_ns() - work_start) / 1_000_000
    result = {
        "checksum": checksum,
        "digest": digest,
        "init_ms": round(init_ms, 3),
        "pid": os.getpid(),
        "state_mib": len(shared) // (1024 * 1024),
        "task_id": task_id,
        "work_ms": round(work_ms, 3),
    }
    result_dir = Path(os.environ.get("RESULT_DIR", "/tmp"))
    result_dir.mkdir(parents=True, exist_ok=True)
    result_path = result_dir / f"cpu-parity-result-{task_id}.json"
    temporary_path = result_path.with_suffix(".json.tmp")
    temporary_path.write_text(json.dumps(result, sort_keys=True))
    temporary_path.replace(result_path)
    print(json.dumps(result, sort_keys=True), flush=True)
    time.sleep(setting("HOLD_SECONDS", 60))


if __name__ == "__main__":
    main()
