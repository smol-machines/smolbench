#!/usr/bin/env python3
"""Exercise live, batch, and nested branching through the Smol Cloud SDK."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import uuid
from importlib.metadata import version
from pathlib import Path
from typing import Any

from smol import Machine
from smol.types import ConnectOptions, ExecOptions, MachineConfig, ResourceSpec


def _exec(machine: Machine, script: str) -> str:
    result = machine.exec(
        ["/bin/sh", "-lc", script], ExecOptions(timeout=30, output="text")
    )
    result.assert_success(script)
    return result.stdout.strip()


def _delete_all(machines: list[Machine]) -> list[str]:
    errors = []
    for machine in reversed(machines):
        try:
            machine.delete()
        except Exception as error:  # noqa: BLE001 - cleanup reports every failure
            errors.append(f"{machine.name}: {error}")
    return errors


def run_wave(
    conn: ConnectOptions, *, image: str, fanout: int, repetition: int
) -> dict[str, Any]:
    stamp = f"{int(time.time())}-{uuid.uuid4().hex[:8]}-r{repetition}"
    root = Machine.create(
        MachineConfig(
            name=f"cloud-branch-root-{stamp}",
            image=image,
            command=["/bin/sh", "-lc", "while :; do sleep 3600; done"],
            resources=ResourceSpec(cpus=1, memory_mb=512, network=True),
            checkpoint=True,
            ready_timeout_seconds=180,
        ),
        conn,
    )
    machines = [root]
    checks = []
    cleanup_errors: list[str] = []
    try:
        _exec(
            root,
            "printf shared-disk >/root/branch-state-disk; "
            "printf shared-ram >/dev/shm/branch-state-ram",
        )

        started = time.perf_counter()
        branches = root.branch_batch(
            count=fanout, name_prefix=f"cloud-branch-child-{stamp}"
        )
        batch_seconds = time.perf_counter() - started
        if len(branches) != fanout:
            raise RuntimeError(
                f"cloud returned {len(branches)} branches, expected {fanout}"
            )
        machines.extend(branches)

        _exec(
            root,
            "printf '%s' -source >>/root/branch-state-disk; "
            "printf '%s' -source >>/dev/shm/branch-state-ram",
        )
        checks.append(
            _exec(
                root,
                "printf '%s/%s' \"$(cat /root/branch-state-disk)\" "
                '"$(cat /dev/shm/branch-state-ram)"',
            )
            == "shared-disk-source/shared-ram-source"
        )

        for index, branch in enumerate(branches):
            inherited = _exec(
                branch,
                "printf '%s/%s' \"$(cat /root/branch-state-disk)\" "
                '"$(cat /dev/shm/branch-state-ram)"',
            )
            checks.append(inherited == "shared-disk/shared-ram")
            _exec(branch, f"printf unique-{index} >/root/branch-state-unique")
        for index, branch in enumerate(branches):
            checks.append(
                _exec(branch, "cat /root/branch-state-unique") == f"unique-{index}"
            )

        nested_started = time.perf_counter()
        nested_parent = root.branch(f"cloud-branch-parent-{stamp}", checkpointable=True)
        machines.append(nested_parent)
        _exec(nested_parent, "printf '%s' -child >>/root/branch-state-disk")
        grandchild = nested_parent.branch(f"cloud-branch-grandchild-{stamp}")
        machines.append(grandchild)
        nested_seconds = time.perf_counter() - nested_started
        _exec(nested_parent, "printf '%s' -parent >>/root/branch-state-disk")
        checks.extend(
            [
                _exec(root, "cat /root/branch-state-disk") == "shared-disk-source",
                _exec(nested_parent, "cat /root/branch-state-disk")
                == "shared-disk-source-child-parent",
                _exec(grandchild, "cat /root/branch-state-disk")
                == "shared-disk-source-child",
            ]
        )

        if not all(checks):
            raise RuntimeError(
                f"state inheritance or isolation failed ({sum(checks)}/{len(checks)})"
            )
        return {
            "repetition": repetition,
            "fanout": fanout,
            "batch_branch_seconds": batch_seconds,
            "nested_parent_and_child_seconds": nested_seconds,
            "checks_passed": sum(checks),
            "checks_total": len(checks),
            "source_continued": True,
            "nested_branch_worked": True,
        }
    finally:
        cleanup_errors = _delete_all(machines)
        if cleanup_errors:
            raise RuntimeError("cleanup failed: " + "; ".join(cleanup_errors))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("SMOL_CLOUD_URL"))
    parser.add_argument("--token", default=os.environ.get("SMOL_CLOUD_TOKEN"))
    parser.add_argument("--image", default="alpine:3.20")
    parser.add_argument("--fanout", type=int, default=4)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument(
        "--smolcloud-revision", default=os.environ.get("SMOLCLOUD_REVISION")
    )
    parser.add_argument("--smolvm-revision", default=os.environ.get("SMOLVM_REVISION"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.fanout < 1 or args.repetitions < 1:
        parser.error("--fanout and --repetitions must be positive")
    if not args.token:
        parser.error("set SMOL_CLOUD_TOKEN or pass --token")

    conn = ConnectOptions(target="cloud", base_url=args.url, api_key=args.token)
    runs = [
        run_wave(conn, image=args.image, fanout=args.fanout, repetition=repetition)
        for repetition in range(1, args.repetitions + 1)
    ]
    payload = {
        "schema_version": 1,
        "target": "smol-cloud",
        "image": args.image,
        "fanout": args.fanout,
        "repetitions": args.repetitions,
        "median_batch_branch_seconds": statistics.median(
            run["batch_branch_seconds"] for run in runs
        ),
        "median_nested_parent_and_child_seconds": statistics.median(
            run["nested_parent_and_child_seconds"] for run in runs
        ),
        "source_continued": all(run["source_continued"] for run in runs),
        "nested_branch_worked": all(run["nested_branch_worked"] for run in runs),
        "checks_passed": sum(run["checks_passed"] for run in runs),
        "checks_total": sum(run["checks_total"] for run in runs),
        "software": {
            "smolmachines": version("smolmachines"),
            "smolcloud_revision": args.smolcloud_revision,
            "smolvm_revision": args.smolvm_revision,
        },
        "runs": runs,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
