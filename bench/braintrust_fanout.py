#!/usr/bin/env python3
"""Fan out Braintrust's public GH Archive agent workload from one checkpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from smol import ExecOptions, Machine, MachineConfig, ResourceSpec


REPOSITORY = "https://github.com/braintrustdata/bash-agent-evals.git"
REVISION = "a13ca02330fdd4f000ca7ad5e8a3b6958afd27b8"
IMAGE = (
    "node:22-bookworm@"
    "sha256:8a34c4ab3ea2c5cd194f07e317b2a8f09461d3c8b05c4e34c8ccd56d56024c4d"
)
WORKDIR = "/opt/bash-agent-evals"
RESOURCE_CPUS = 2
RESOURCE_MEMORY_MB = 4096
PREPARE = f"""set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates git python3 make g++
rm -rf /var/lib/apt/lists/*
corepack enable
corepack prepare pnpm@8.15.9 --activate
git clone {REPOSITORY} {WORKDIR}
cd {WORKDIR}
git checkout --detach {REVISION}
pnpm install --frozen-lockfile
pnpm download
pnpm transform
rm -rf data/raw
test -s data/database.sqlite
"""

DOCKERFILE = f"""FROM {IMAGE}
RUN set -eux; \\
    export DEBIAN_FRONTEND=noninteractive; \\
    apt-get update; \\
    apt-get install -y --no-install-recommends ca-certificates git python3 make g++; \\
    rm -rf /var/lib/apt/lists/*; \\
    corepack enable; \\
    corepack prepare pnpm@8.15.9 --activate; \\
    git clone {REPOSITORY} {WORKDIR}; \\
    cd {WORKDIR}; \\
    git checkout --detach {REVISION}; \\
    pnpm install --frozen-lockfile; \\
    pnpm download; \\
    pnpm transform; \\
    rm -rf data/raw; \\
    test -s data/database.sqlite
WORKDIR {WORKDIR}
"""

SMOKE_CASES = (
    (
        "most-issues",
        "SELECT r.full_name, COUNT(*) AS count FROM issues i JOIN repos r ON r.id=i.repo_id GROUP BY r.id ORDER BY count DESC LIMIT 1",
        [{"full_name": "microsoft/winget-pkgs", "count": 60}],
    ),
    (
        "merged-prs",
        "SELECT SUM(merged) AS merged, COUNT(*) AS total FROM pulls",
        [{"merged": 6894, "total": 16483}],
    ),
    (
        "vercel-repos",
        "SELECT name FROM repos WHERE owner='vercel' ORDER BY name",
        [
            {"name": name}
            for name in (
                "ai",
                "ai-chatbot",
                "commerce",
                "hyper",
                "next.js",
                "nft",
                "storage",
                "swr",
                "turbo",
                "vercel",
            )
        ],
    ),
    (
        "most-repositories",
        "SELECT owner, COUNT(*) AS count FROM repos GROUP BY owner ORDER BY count DESC LIMIT 1",
        [{"owner": "favstats", "count": 170}],
    ),
)

QUESTIONS = (
    "Which project has the most issues?",
    "What percentage of pull requests are merged?",
    "How many repos are there from vercel? List them.",
    "Which organization has the most repositories in the dataset?",
)

QUERY_PROGRAM = """
const Database = require('better-sqlite3');
const db = new Database('data/database.sqlite', { readonly: true });
console.log(JSON.stringify(db.prepare(process.env.SMOL_QUERY).all()));
"""


@dataclass
class TaskResult:
    branch: str
    case: str
    duration_seconds: float
    exit_code: int
    correct: bool
    output: str
    error: str


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def checked_exec(machine: Machine, command: str, timeout: int) -> None:
    result = machine.exec(
        ["/bin/bash", "-lc", command],
        ExecOptions(timeout=timeout, workdir="/"),
    )
    if result.exit_code != 0:
        raise RuntimeError(
            f"checkpoint preparation failed ({result.exit_code})\n"
            f"stdout:\n{result.stdout[-4000:]}\n"
            f"stderr:\n{result.stderr[-4000:]}"
        )


def prepare_checkpoint(name: str) -> tuple[Machine, float]:
    started = time.perf_counter()
    machine = Machine.create(
        MachineConfig(
            name=name,
            image=IMAGE,
            resources=ResourceSpec(
                cpus=RESOURCE_CPUS,
                memory_mb=RESOURCE_MEMORY_MB,
                storage_gb=20,
                network=True,
            ),
            persistent=True,
            checkpoint=True,
        )
    )
    try:
        checked_exec(machine, PREPARE, 1800)
    except BaseException:
        machine.delete()
        raise
    return machine, time.perf_counter() - started


def secret_env(model: str) -> dict[str, str]:
    names = (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "BASETEN_API_KEY",
        "MODAL_API_KEY",
        "BRAINTRUST_API_KEY",
    )
    values = {name: os.environ[name] for name in names if os.environ.get(name)}
    if model.startswith("claude-"):
        required = "ANTHROPIC_API_KEY"
    elif model == "glm-5":
        required = "MODAL_API_KEY"
    elif model in {"glm-4.7", "kimi-k2.5"}:
        required = "BASETEN_API_KEY"
    else:
        required = "OPENAI_API_KEY"
    if required not in values:
        raise RuntimeError(f"agent mode requires {required}")
    values["MODEL"] = model
    return values


def run_smoke(machine: Machine, index: int) -> TaskResult:
    case, query, expected = SMOKE_CASES[index % len(SMOKE_CASES)]
    started = time.perf_counter()
    result = machine.exec(
        ["node", "-e", QUERY_PROGRAM],
        ExecOptions(env={"SMOL_QUERY": query}, workdir=WORKDIR, timeout=120),
    )
    duration = time.perf_counter() - started
    output = result.stdout.strip()
    try:
        observed = json.loads(output)
    except json.JSONDecodeError:
        observed = None
    return TaskResult(
        branch=machine.name,
        case=case,
        duration_seconds=duration,
        exit_code=result.exit_code,
        correct=result.exit_code == 0 and observed == expected,
        output=output[-1000:],
        error=result.stderr[-1000:],
    )


def run_agent(
    machine: Machine, index: int, agent: str, env: dict[str, str]
) -> TaskResult:
    question = QUESTIONS[index % len(QUESTIONS)]
    started = time.perf_counter()
    result = machine.exec(
        ["pnpm", "debug", agent, question],
        ExecOptions(env=env, workdir=WORKDIR, timeout=600),
    )
    duration = time.perf_counter() - started
    output = result.stdout
    correct = (
        result.exit_code == 0
        and "═══ Final Result ═══" in output
        and "═══ Error ═══" not in output
    )
    return TaskResult(
        branch=machine.name,
        case=question,
        duration_seconds=duration,
        exit_code=result.exit_code,
        correct=correct,
        output=output[-4000:],
        error=result.stderr[-2000:],
    )


def prepare_docker_image() -> tuple[str, float, bool]:
    if shutil.which("docker") is None:
        raise RuntimeError("Docker baseline requested, but `docker` is not installed")
    info = subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if info.returncode != 0:
        raise RuntimeError("Docker baseline requested, but `docker info` failed")
    definition = hashlib.sha256(DOCKERFILE.encode()).hexdigest()[:12]
    image = f"smol-bench/braintrust-bash-evals:{REVISION[:12]}-{definition}"
    inspect = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if inspect.returncode == 0:
        return image, 0.0, False
    started = time.perf_counter()
    result = subprocess.run(
        ["docker", "build", "--tag", image, "-"],
        input=DOCKERFILE,
        text=True,
        capture_output=True,
        timeout=1800,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Docker baseline image failed to build ({result.returncode})\n"
            f"stdout:\n{result.stdout[-4000:]}\n"
            f"stderr:\n{result.stderr[-4000:]}"
        )
    return image, time.perf_counter() - started, True


def run_docker_smoke(image: str, index: int, label: str) -> TaskResult:
    case, query, expected = SMOKE_CASES[index % len(SMOKE_CASES)]
    name = f"bt-docker-{label}-{index:04d}"
    started = time.perf_counter()
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--cpus",
            str(RESOURCE_CPUS),
            "--memory",
            f"{RESOURCE_MEMORY_MB}m",
            "--name",
            name,
            "--env",
            f"SMOL_QUERY={query}",
            image,
            "node",
            "-e",
            QUERY_PROGRAM,
        ],
        text=True,
        capture_output=True,
        timeout=120,
    )
    duration = time.perf_counter() - started
    output = result.stdout.strip()
    try:
        observed = json.loads(output)
    except json.JSONDecodeError:
        observed = None
    return TaskResult(
        branch=name,
        case=case,
        duration_seconds=duration,
        exit_code=result.returncode,
        correct=result.returncode == 0 and observed == expected,
        output=output[-1000:],
        error=result.stderr[-1000:],
    )


def run_smol_wave(
    golden: Machine,
    *,
    fanout: int,
    parallel: int,
    mode: str,
    agent: str,
    env: dict[str, str],
    label: str,
    repetition: int,
) -> dict[str, object]:
    branches: list[Machine] = []
    results: list[TaskResult] = []
    names = [f"bt-{label}-r{repetition:02d}-{index:04d}" for index in range(fanout)]
    started = time.perf_counter()
    branches = golden.branch_batch(names=names)
    branch_seconds = time.perf_counter() - started
    print(
        f"[Smol {repetition}] Created {len(branches)} branches in "
        f"{branch_seconds:.3f}s",
        flush=True,
    )
    try:
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=min(parallel, len(branches))) as pool:
            if mode == "smoke":
                futures = [
                    pool.submit(run_smoke, machine, i)
                    for i, machine in enumerate(branches)
                ]
            else:
                futures = [
                    pool.submit(run_agent, machine, i, agent, env)
                    for i, machine in enumerate(branches)
                ]
            results = [future.result() for future in futures]
        task_seconds = time.perf_counter() - started
    finally:
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=min(parallel, len(branches))) as pool:
            list(pool.map(lambda machine: machine.delete(), branches))
        cleanup_seconds = time.perf_counter() - started
    return {
        "repetition": repetition,
        "branch_batch_seconds": branch_seconds,
        "branch_to_completed_wall_seconds": task_seconds,
        "cleanup_seconds": cleanup_seconds,
        "correct": sum(result.correct for result in results),
        "results": [asdict(result) for result in results],
    }


def run_docker_wave(
    image: str,
    *,
    fanout: int,
    parallel: int,
    label: str,
    repetition: int,
) -> dict[str, object]:
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(parallel, fanout)) as pool:
        results = list(
            pool.map(
                lambda index: run_docker_smoke(
                    image, index, f"{label}-r{repetition:02d}"
                ),
                range(fanout),
            )
        )
    wall_seconds = time.perf_counter() - started
    print(
        f"[Docker {repetition}] Started and completed {len(results)} containers "
        f"in {wall_seconds:.3f}s",
        flush=True,
    )
    return {
        "repetition": repetition,
        "start_to_completed_wall_seconds": wall_seconds,
        "correct": sum(result.correct for result in results),
        "results": [asdict(result) for result in results],
    }


def main() -> int:
    experiment_started = time.perf_counter()
    parser = argparse.ArgumentParser()
    parser.add_argument("--fanout", type=int, default=4)
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--mode", choices=("smoke", "agent"), default="smoke")
    parser.add_argument("--agent", choices=("bash", "fs", "sql"), default="sql")
    parser.add_argument("--model", default="claude-sonnet-4-5")
    parser.add_argument("--checkpoint", help="reuse an existing checkpoint by name")
    parser.add_argument("--keep-checkpoint", action="store_true")
    parser.add_argument(
        "--docker",
        action="store_true",
        help="also build and run an equivalently prepared Docker image",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.fanout < 1 or args.parallel < 1 or args.repetitions < 1:
        parser.error("fanout, parallel, and repetitions must be positive")
    if args.parallel > args.fanout:
        parser.error("parallel cannot exceed fanout")
    if args.docker and args.mode != "smoke":
        parser.error("--docker currently supports --mode=smoke")
    env = secret_env(args.model) if args.mode == "agent" else {}

    label = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    owned = args.checkpoint is None
    if args.checkpoint:
        golden = Machine.connect(args.checkpoint)
        prepare_seconds = None
    else:
        name = f"braintrust-eval-{label}-{uuid.uuid4().hex[:6]}"
        print("Preparing Braintrust's pinned GH Archive workload once...", flush=True)
        golden, prepare_seconds = prepare_checkpoint(name)
        print(f"Checkpoint {golden.name} ready in {prepare_seconds:.2f}s", flush=True)

    docker_image = None
    docker_prepare_seconds = None
    docker_image_built = False
    smol_runs: list[dict[str, object]] = []
    docker_runs: list[dict[str, object]] = []
    checkpoint_cleanup_seconds = 0.0
    try:
        if args.docker:
            docker_image, docker_prepare_seconds, docker_image_built = (
                prepare_docker_image()
            )
        for repetition in range(1, args.repetitions + 1):
            providers = ["smol", "docker"] if args.docker else ["smol"]
            if repetition % 2 == 0:
                providers.reverse()
            for provider in providers:
                if provider == "smol":
                    smol_runs.append(
                        run_smol_wave(
                            golden,
                            fanout=args.fanout,
                            parallel=args.parallel,
                            mode=args.mode,
                            agent=args.agent,
                            env=env,
                            label=label,
                            repetition=repetition,
                        )
                    )
                else:
                    assert docker_image is not None
                    docker_runs.append(
                        run_docker_wave(
                            docker_image,
                            fanout=args.fanout,
                            parallel=args.parallel,
                            label=label,
                            repetition=repetition,
                        )
                    )
    finally:
        if owned and not args.keep_checkpoint:
            started = time.perf_counter()
            golden.delete()
            checkpoint_cleanup_seconds = time.perf_counter() - started

    results = [result for run in smol_runs for result in run["results"]]
    durations = [result["duration_seconds"] for result in results]
    branch_seconds = statistics.median(run["branch_batch_seconds"] for run in smol_runs)
    task_seconds = statistics.median(
        run["branch_to_completed_wall_seconds"] for run in smol_runs
    )
    branch_cleanup_seconds = statistics.median(
        run["cleanup_seconds"] for run in smol_runs
    )
    docker_payload = None
    if args.docker:
        docker_results = [result for run in docker_runs for result in run["results"]]
        docker_durations = [result["duration_seconds"] for result in docker_results]
        docker_payload = {
            "image": docker_image,
            "image_built": docker_image_built,
            "image_prepare_seconds": docker_prepare_seconds,
            "start_to_completed_wall_seconds": statistics.median(
                run["start_to_completed_wall_seconds"] for run in docker_runs
            ),
            "task_latency_seconds": {
                "p50": statistics.median(docker_durations),
                "p99": percentile(docker_durations, 0.99),
                "max": max(docker_durations),
            },
            "correct": sum(result["correct"] for result in docker_results),
            "runs": docker_runs,
            "results": docker_results,
        }
    payload = {
        "schema_version": 1,
        "workload": {
            "repository": REPOSITORY,
            "revision": REVISION,
            "image": IMAGE,
            "dataset": "GH Archive 2024-01-15 hour 15",
        },
        "resources_per_environment": {
            "cpus": RESOURCE_CPUS,
            "memory_mb": RESOURCE_MEMORY_MB,
        },
        "mode": args.mode,
        "agent": args.agent if args.mode == "agent" else None,
        "model": args.model if args.mode == "agent" else None,
        "checkpoint": golden.name,
        "checkpoint_prepare_seconds": prepare_seconds,
        "branch_count": args.fanout,
        "repetitions": args.repetitions,
        "branch_batch_seconds": branch_seconds,
        "branch_to_completed_wall_seconds": task_seconds,
        "branch_cleanup_seconds": branch_cleanup_seconds,
        "checkpoint_cleanup_seconds": checkpoint_cleanup_seconds,
        "total_experiment_seconds": time.perf_counter() - experiment_started,
        "task_latency_seconds": {
            "p50": statistics.median(durations),
            "p99": percentile(durations, 0.99),
            "max": max(durations),
        },
        "correct": sum(result["correct"] for result in results),
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "logical_cpus": os.cpu_count(),
        },
        "docker": docker_payload,
        "runs": smol_runs,
        "results": results,
    }
    output = args.output or Path("results") / f"{label}-braintrust-fanout.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    print(
        f"{payload['correct']}/{len(results)} Smol branches completed correctly; "
        f"wrote {output}",
        flush=True,
    )
    if args.keep_checkpoint and owned:
        print(f"Checkpoint retained as {golden.name}", flush=True)
    expected = args.fanout * args.repetitions
    docker_correct = docker_payload is None or docker_payload["correct"] == expected
    return 0 if payload["correct"] == len(results) and docker_correct else 1


if __name__ == "__main__":
    raise SystemExit(main())
