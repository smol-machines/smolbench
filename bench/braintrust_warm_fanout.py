#!/usr/bin/env python3
"""Benchmark Braintrust's SQLite workload at an inherited live-worker boundary."""

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
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "bench" / "workloads" / "braintrust_warm_worker.cjs"
WORKDIR = "/opt/bash-agent-evals"
REPOSITORY = "https://github.com/braintrustdata/bash-agent-evals.git"
REVISION = "a13ca02330fdd4f000ca7ad5e8a3b6958afd27b8"
IMAGE = (
    "node:22-bookworm@"
    "sha256:8a34c4ab3ea2c5cd194f07e317b2a8f09461d3c8b05c4e34c8ccd56d56024c4d"
)
RESOURCE_CPUS = 2
RESOURCE_MEMORY_MB = 4096

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

PREPARE_STAGES = (
    (
        "system-dependencies",
        """set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates git python3 make g++
rm -rf /var/lib/apt/lists/*
corepack enable
corepack prepare pnpm@8.15.9 --activate
""",
        600,
    ),
    (
        "checkout",
        f"""set -euo pipefail
rm -rf {WORKDIR}
git clone {REPOSITORY} {WORKDIR}
cd {WORKDIR}
git checkout --detach {REVISION}
""",
        300,
    ),
    (
        "dependencies",
        f"set -euo pipefail\ncd {WORKDIR}\npnpm install --frozen-lockfile\n",
        900,
    ),
    (
        "dataset-download",
        f"set -euo pipefail\ncd {WORKDIR}\npnpm download\n",
        900,
    ),
    (
        "dataset-transform",
        f"""set -euo pipefail
cd {WORKDIR}
pnpm transform
rm -rf data/raw
test -s data/database.sqlite
""",
        1200,
    ),
)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def host_cpu_model() -> str:
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        for line in cpuinfo.read_text(errors="replace").splitlines():
            if line.startswith("model name") and ":" in line:
                return line.split(":", 1)[1].strip()
    return platform.processor()


def command(
    arguments: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 300,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(arguments)}\n"
            f"stdout:\n{result.stdout[-4000:]}\n"
            f"stderr:\n{result.stderr[-4000:]}"
        )
    return result


def command_to_file(
    arguments: list[str],
    output_path: Path,
    *,
    env: dict[str, str] | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    # The detached VMM currently inherits the launcher stdio. A short-lived
    # PIPE therefore tears it down when subprocess.run closes the pipe. A
    # regular file has no reader endpoint and remains safe after this returns.
    with output_path.open("w+") as output:
        result = subprocess.run(
            arguments,
            env=env,
            text=True,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        output.seek(0)
        captured = output.read()
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(arguments)}\n"
            f"output:\n{captured[-4000:]}"
        )
    return subprocess.CompletedProcess(arguments, result.returncode, captured, "")


def start_log_path(source: str) -> Path:
    return Path(tempfile.gettempdir()) / f"braintrust-warm-{source}.start.log"


def runtime_environment(smolvm: str) -> dict[str, str]:
    # All host CLI invocations use the absolute binary path. Do not prepend its
    # build directory to PATH: the boot worker has its own executable-resolution
    # contract, and changing PATH can make a successfully started VMM exit.
    return os.environ.copy()


def wait_for_files(
    paths: list[Path], timeout: float
) -> tuple[list[dict[str, Any]], float]:
    started = time.perf_counter()
    deadline = started + timeout
    while time.perf_counter() < deadline:
        if all(path.is_file() for path in paths):
            return [
                json.loads(path.read_text()) for path in paths
            ], time.perf_counter() - started
        time.sleep(0.005)
    missing = [str(path) for path in paths if not path.is_file()]
    raise TimeoutError(f"timed out waiting for result files: {missing}")


def result_paths(
    directory: Path, provider: str, run_id: str, fanout: int
) -> list[Path]:
    return [
        directory / f"result-{provider}-{run_id}-{index}.json"
        for index in range(fanout)
    ]


def ready_paths(directory: Path, provider: str, run_id: str, fanout: int) -> list[Path]:
    return [
        directory / f"ready-{provider}-{run_id}-{index}.json" for index in range(fanout)
    ]


def mem_available_bytes() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def stable_mem_available_bytes() -> int | None:
    samples: list[int] = []
    for _ in range(5):
        sample = mem_available_bytes()
        if sample is None:
            return None
        samples.append(sample)
        time.sleep(0.04)
    return int(statistics.median(samples))


def memory_delta(before: int | None, after: int | None) -> int | None:
    if before is None or after is None:
        return None
    return max(0, before - after)


def process_pss_bytes(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def smol_pids(smolvm: str, names: list[str], env: dict[str, str]) -> list[int]:
    records = json.loads(command([smolvm, "machine", "ls", "--json"], env=env).stdout)
    wanted = set(names)
    return [
        int(record["pid"])
        for record in records
        if record.get("name") in wanted and isinstance(record.get("pid"), int)
    ]


def summed_pss_bytes(pids: list[int]) -> int | None:
    values = [process_pss_bytes(pid) for pid in pids]
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def prepare_docker_image() -> tuple[str, float, bool]:
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


def prepare_worker_image() -> tuple[str, dict[str, Any]]:
    base_image, base_seconds, base_built = prepare_docker_image()
    identity = hashlib.sha256(
        f"{base_image}\nworker-layout-v3\n".encode() + WORKER.read_bytes()
    ).hexdigest()[:12]
    image = f"smol-bench/braintrust-warm-worker:{identity}"
    inspect = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    worker_built = inspect.returncode != 0
    build_seconds = 0.0
    if worker_built:
        definition = f"""FROM {base_image}
COPY bench/workloads/braintrust_warm_worker.cjs {WORKDIR}/braintrust_warm_worker.cjs
WORKDIR {WORKDIR}
CMD [\"node\", \"{WORKDIR}/braintrust_warm_worker.cjs\"]
"""
        started = time.perf_counter()
        result = subprocess.run(
            ["docker", "build", "--file", "-", "--tag", image, str(ROOT)],
            input=definition,
            text=True,
            capture_output=True,
            timeout=1800,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"worker image build failed ({result.returncode})\n"
                f"stdout:\n{result.stdout[-4000:]}\n"
                f"stderr:\n{result.stderr[-4000:]}"
            )
        build_seconds = time.perf_counter() - started

    return (
        image,
        {
            "base_image_built": base_built,
            "base_image_prepare_seconds": base_seconds,
            "worker_image_built": worker_built,
            "worker_image_build_seconds": build_seconds,
        },
    )


def delete_smol_machines(
    smolvm: str, names: list[str], env: dict[str, str], parallel: int
) -> None:
    def delete(name: str) -> None:
        command(
            [smolvm, "machine", "delete", "--name", name, "--force"],
            env=env,
            check=False,
        )

    with ThreadPoolExecutor(max_workers=min(parallel, len(names))) as executor:
        list(executor.map(delete, names))


def wait_for_branchpoint(
    smolvm: str, source: str, env: dict[str, str], timeout: float = 180
) -> float:
    started = time.perf_counter()
    deadline = started + timeout
    while time.perf_counter() < deadline:
        result = command(
            [
                smolvm,
                "machine",
                "exec",
                "--name",
                source,
                "--timeout",
                "2s",
                "--",
                "test",
                "-f",
                "/run/smolvm/forkpoint/ready",
            ],
            env=env,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            return time.perf_counter() - started
        time.sleep(0.02)
    raise TimeoutError(f"Smol source {source!r} did not reach its branchpoint")


def create_smol_source(
    smolvm: str,
    source: str,
    env: dict[str, str],
) -> dict[str, Any]:
    before = stable_mem_available_bytes()
    started = time.perf_counter()
    command(
        [
            smolvm,
            "machine",
            "create",
            "--name",
            source,
            "--image",
            IMAGE,
            "--net",
            "--cpus",
            str(RESOURCE_CPUS),
            "--mem",
            str(RESOURCE_MEMORY_MB),
            "--storage",
            "20",
            "--overlay",
            "4",
            "--",
            "/usr/bin/tail",
            "-f",
            "/dev/null",
        ],
        env=env,
        timeout=900,
    )
    boot_attempts = 0
    for boot_attempts in range(1, 4):
        try:
            command_to_file(
                [smolvm, "machine", "start", "--name", source, "--branchable"],
                start_log_path(source),
                env=env,
                timeout=900,
            )
        except RuntimeError:
            if boot_attempts == 3:
                raise
            continue
        # `start` returns when the agent is ready, while the image's persistent
        # workload may still be settling. Avoid opening an exec channel during
        # its first container launch; this wait is outside provider comparisons.
        time.sleep(5)
        probe = command(
            [
                smolvm,
                "machine",
                "exec",
                "--name",
                source,
                "--timeout",
                "10s",
                "--",
                "/bin/sh",
                "-c",
                "true",
            ],
            env=env,
            timeout=15,
            check=False,
        )
        if probe.returncode == 0:
            break
        if boot_attempts == 3:
            raise RuntimeError(
                f"source {source!r} was not live after three boot attempts:\n"
                f"{probe.stderr[-4000:]}"
            )
    created_seconds = time.perf_counter() - started
    started = time.perf_counter()
    stage_seconds: dict[str, float] = {}
    for label, script, timeout in PREPARE_STAGES:
        stage_started = time.perf_counter()
        command(
            [
                smolvm,
                "machine",
                "exec",
                "--name",
                source,
                "--timeout",
                f"{timeout}s",
                "--workdir",
                "/",
                "--",
                "/bin/bash",
                "-lc",
                script,
            ],
            env=env,
            timeout=timeout + 30,
        )
        stage_seconds[label] = time.perf_counter() - stage_started
    command(
        [
            smolvm,
            "machine",
            "cp",
            str(WORKER),
            f"{source}:{WORKDIR}/braintrust_warm_worker.cjs",
        ],
        env=env,
        timeout=120,
    )
    prepared_seconds = time.perf_counter() - started
    started = time.perf_counter()
    command(
        [
            smolvm,
            "machine",
            "exec",
            "--name",
            source,
            "--detach",
            "--workdir",
            WORKDIR,
            "--env",
            "SMOL_BRANCH=1",
            "--env",
            "BENCH_PROVIDER=smol-source",
            "--env",
            "BENCH_RUN_ID=source",
            "--env",
            "BENCH_INDEX=0",
            "--",
            "/bin/bash",
            "-lc",
            f"exec node {WORKDIR}/braintrust_warm_worker.cjs "
            f">/tmp/worker-{source}.log 2>&1",
        ],
        env=env,
        timeout=120,
    )
    wait_seconds = wait_for_branchpoint(smolvm, source, env)
    start_to_ready = time.perf_counter() - started
    after = stable_mem_available_bytes()
    pids = smol_pids(smolvm, [source], env)
    return {
        "create_seconds": created_seconds,
        "boot_attempts": boot_attempts,
        "workload_prepare_seconds": prepared_seconds,
        "workload_prepare_stage_seconds": stage_seconds,
        "start_to_ready_seconds": start_to_ready,
        "ready_wait_seconds": wait_seconds,
        "memory_pressure_bytes": memory_delta(before, after),
        "vmm_pss_bytes": summed_pss_bytes(pids),
    }


def validate_results(rows: list[dict[str, Any]], fanout: int) -> None:
    indexes = sorted(int(row["index"]) for row in rows)
    if indexes != list(range(fanout)):
        raise RuntimeError(f"expected result indexes 0..{fanout - 1}, got {indexes}")
    failures = [row for row in rows if not row.get("correct")]
    if failures:
        raise RuntimeError(
            f"{len(failures)} workload results were incorrect: {failures}"
        )


def collect_smol_results(
    smolvm: str,
    names: list[str],
    env: dict[str, str],
    parallel: int,
) -> tuple[list[dict[str, Any]], float]:
    started = time.perf_counter()

    def collect(name: str) -> dict[str, Any]:
        result = command(
            [
                smolvm,
                "machine",
                "exec",
                "--name",
                name,
                "--timeout",
                "30s",
                "--",
                "cat",
                "/tmp/braintrust-result.json",
            ],
            env=env,
            timeout=45,
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    with ThreadPoolExecutor(max_workers=min(parallel, len(names))) as executor:
        rows = list(executor.map(collect, names))
    return rows, time.perf_counter() - started


def run_smol_direct(
    smolvm: str,
    source: str,
    results: Path,
    fanout: int,
    parallel: int,
    repetition: int,
    token: str,
    env: dict[str, str],
) -> dict[str, Any]:
    run_id = f"direct-r{repetition}-{token}"
    prefix = f"bt-direct-{token}-r{repetition}"
    names = [f"{prefix}-{index}" for index in range(fanout)]
    before = stable_mem_available_bytes()
    started = time.perf_counter()
    branch = command(
        [
            smolvm,
            "machine",
            "branch",
            "--from",
            source,
            "--count",
            str(fanout),
            "--name-prefix",
            prefix,
            "--parallel",
            str(parallel),
            "--env",
            f"BENCH_RUN_ID={run_id}",
            "--env",
            "BENCH_PROVIDER=smol-direct",
            "--env",
            "BENCH_INDEX={index}",
        ],
        env=env,
        timeout=600,
    )
    branch_seconds = time.perf_counter() - started
    # The release acknowledgment precedes the worker's query by only a few
    # instructions. Let every child finish before using an out-of-band exec to
    # collect its private result; collection is deliberately outside latency.
    time.sleep(1)
    after = stable_mem_available_bytes()
    pss = summed_pss_bytes(smol_pids(smolvm, [source, *names], env))
    rows, collection_seconds = collect_smol_results(smolvm, names, env, parallel)
    validate_results(rows, fanout)
    query_tail_seconds = max(float(row["query_ms"]) for row in rows) / 1000
    total_seconds = branch_seconds + query_tail_seconds
    delete_smol_machines(smolvm, names, env, parallel)
    return {
        "repetition": repetition,
        "branch_seconds": branch_seconds,
        "query_tail_seconds": query_tail_seconds,
        "checkpoint_to_result_seconds": total_seconds,
        "latency_measurement": "branch_ack_wall_plus_max_guest_query",
        "result_collection_seconds_excluded": collection_seconds,
        "memory_pressure_bytes": memory_delta(before, after),
        "source_and_children_vmm_pss_bytes": pss,
        "stdout": branch.stdout[-2000:],
        "stderr": branch.stderr[-2000:],
        "results": rows,
    }


def run_smol_pool(
    smolvm: str,
    source: str,
    results: Path,
    fanout: int,
    parallel: int,
    repetition: int,
    token: str,
    env: dict[str, str],
) -> dict[str, Any]:
    run_id = f"pool-r{repetition}-{token}"
    prefix = f"bt-pool-{token}-r{repetition}"
    names = [f"{prefix}-{index}" for index in range(fanout)]
    before = stable_mem_available_bytes()
    started = time.perf_counter()
    command(
        [
            smolvm,
            "machine",
            "branch",
            "--from",
            source,
            "--count",
            str(fanout),
            "--name-prefix",
            prefix,
            "--parallel",
            str(parallel),
            "--hold",
        ],
        env=env,
        timeout=600,
    )
    fill_seconds = time.perf_counter() - started
    after_fill = stable_mem_available_bytes()
    fill_pss = summed_pss_bytes(smol_pids(smolvm, [source, *names], env))

    activation_started = time.perf_counter()

    def release(pair: tuple[int, str]) -> dict[str, Any]:
        index, name = pair
        result = command(
            [
                smolvm,
                "machine",
                "branch-release",
                "--name",
                name,
                "--env",
                f"BENCH_RUN_ID={run_id}",
                "--env",
                "BENCH_PROVIDER=smol-pool",
                "--env",
                f"BENCH_INDEX={index}",
            ],
            env=env,
            timeout=120,
        )
        return {
            "index": index,
            "ack_elapsed_seconds": time.perf_counter() - activation_started,
            "stdout": result.stdout[-500:],
            "stderr": result.stderr[-500:],
        }

    with ThreadPoolExecutor(max_workers=min(parallel, fanout)) as executor:
        releases = list(executor.map(release, enumerate(names)))
    release_seconds = time.perf_counter() - activation_started
    # Release acknowledgment occurs immediately before the inherited worker
    # resumes. Collect its private result out of band after completion so the
    # machine-exec control path is never part of task latency.
    time.sleep(1)
    active_pss = summed_pss_bytes(smol_pids(smolvm, [source, *names], env))
    rows, collection_seconds = collect_smol_results(smolvm, names, env, parallel)
    validate_results(rows, fanout)
    query_ms_by_index = {int(row["index"]): float(row["query_ms"]) for row in rows}
    release_to_result = max(
        float(release["ack_elapsed_seconds"])
        + query_ms_by_index[int(release["index"])] / 1000
        for release in releases
    )
    delete_smol_machines(smolvm, names, env, parallel)
    return {
        "repetition": repetition,
        "pool_fill_seconds": fill_seconds,
        "release_commands_seconds": release_seconds,
        "release_to_result_seconds": release_to_result,
        "latency_measurement": "per_child_release_ack_wall_plus_guest_query",
        "result_collection_seconds_excluded": collection_seconds,
        "pool_memory_pressure_bytes": memory_delta(before, after_fill),
        "source_and_pool_vmm_pss_bytes": fill_pss,
        "source_and_active_children_vmm_pss_bytes": active_pss,
        "release_acks": releases,
        "results": rows,
    }


def docker_arguments(
    image: str,
    name: str,
    results: Path,
    provider: str,
    run_id: str,
    index: int,
    gate: bool,
) -> list[str]:
    return [
        "docker",
        "run",
        "--name",
        name,
        "--network",
        "none",
        "--pids-limit",
        "-1",
        "--cpus",
        str(RESOURCE_CPUS),
        "--memory",
        f"{RESOURCE_MEMORY_MB}m",
        "--volume",
        f"{results}:/results",
        "--env",
        f"BENCH_PROVIDER={provider}",
        "--env",
        f"BENCH_RUN_ID={run_id}",
        "--env",
        f"BENCH_INDEX={index}",
        "--env",
        f"DOCKER_GATE={int(gate)}",
        "--env",
        "RESULT_DIR=/results",
        image,
    ]


def remove_docker_containers(names: list[str], parallel: int) -> None:
    def remove(name: str) -> None:
        subprocess.run(
            ["docker", "rm", "--force", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    with ThreadPoolExecutor(max_workers=min(parallel, len(names))) as executor:
        list(executor.map(remove, names))


def docker_pids(names: list[str]) -> list[int]:
    pids: list[int] = []
    for name in names:
        result = command(
            ["docker", "inspect", "--format", "{{.State.Pid}}", name],
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip().isdigit():
            pids.append(int(result.stdout.strip()))
    return pids


def parse_binary_size(value: str) -> int:
    units = {
        "B": 1,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
    }
    for unit in ("GiB", "MiB", "KiB", "B"):
        if value.endswith(unit):
            return int(float(value[: -len(unit)]) * units[unit])
    raise ValueError(f"unsupported Docker memory value: {value!r}")


def docker_memory_bytes(names: list[str]) -> int | None:
    result = command(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{.MemUsage}}",
            *names,
        ],
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        return sum(
            parse_binary_size(line.split(" / ", 1)[0].strip())
            for line in result.stdout.splitlines()
            if line.strip()
        )
    except ValueError:
        return None


def run_docker_fresh(
    image: str,
    results: Path,
    fanout: int,
    parallel: int,
    repetition: int,
    token: str,
) -> dict[str, Any]:
    provider = "docker-fresh"
    run_id = f"fresh-r{repetition}-{token}"
    names = [f"bt-fresh-{token}-r{repetition}-{index}" for index in range(fanout)]
    before = stable_mem_available_bytes()
    started = time.perf_counter()
    processes = [
        subprocess.Popen(
            docker_arguments(
                image, names[index], results, provider, run_id, index, False
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for index in range(fanout)
    ]
    try:
        rows, _ = wait_for_files(result_paths(results, provider, run_id, fanout), 180)
        total_seconds = time.perf_counter() - started
        validate_results(rows, fanout)
        after = stable_mem_available_bytes()
        pss = summed_pss_bytes(docker_pids(names))
        container_memory = docker_memory_bytes(names)
    finally:
        remove_docker_containers(names, parallel)
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
    return {
        "repetition": repetition,
        "start_to_result_seconds": total_seconds,
        "memory_pressure_bytes": memory_delta(before, after),
        "container_process_pss_bytes": pss,
        "container_cgroup_memory_bytes": container_memory,
        "results": rows,
    }


def run_docker_pool(
    image: str,
    results: Path,
    fanout: int,
    parallel: int,
    repetition: int,
    token: str,
) -> dict[str, Any]:
    provider = "docker-pool"
    run_id = f"pool-r{repetition}-{token}"
    names = [f"bt-dpool-{token}-r{repetition}-{index}" for index in range(fanout)]
    before = stable_mem_available_bytes()
    started = time.perf_counter()
    processes = [
        subprocess.Popen(
            docker_arguments(
                image, names[index], results, provider, run_id, index, True
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for index in range(fanout)
    ]
    try:
        ready, _ = wait_for_files(ready_paths(results, provider, run_id, fanout), 180)
        fill_seconds = time.perf_counter() - started
        after_fill = stable_mem_available_bytes()
        pss = summed_pss_bytes(docker_pids(names))
        container_memory = docker_memory_bytes(names)
        started = time.perf_counter()
        (results / f"go-{provider}-{run_id}").touch()
        rows, _ = wait_for_files(result_paths(results, provider, run_id, fanout), 180)
        release_to_result = time.perf_counter() - started
        validate_results(rows, fanout)
    finally:
        remove_docker_containers(names, parallel)
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
    return {
        "repetition": repetition,
        "pool_fill_seconds": fill_seconds,
        "release_to_result_seconds": release_to_result,
        "pool_memory_pressure_bytes": memory_delta(before, after_fill),
        "container_process_pss_bytes": pss,
        "container_cgroup_memory_bytes": container_memory,
        "ready": ready,
        "results": rows,
    }


def latency_summary(runs: list[dict[str, Any]], wall_field: str) -> dict[str, Any]:
    walls = [float(run[wall_field]) for run in runs]
    queries = [
        float(result["query_ms"]) / 1000 for run in runs for result in run["results"]
    ]
    return {
        "wall_seconds": {
            "median": statistics.median(walls),
            "p99": percentile(walls, 0.99),
            "min": min(walls),
            "max": max(walls),
        },
        "query_seconds": {
            "p50": statistics.median(queries),
            "p99": percentile(queries, 0.99),
            "max": max(queries),
        },
        "correct": sum(result["correct"] for run in runs for result in run["results"]),
        "runs": runs,
    }


def median_optional(runs: list[dict[str, Any]], field: str) -> float | None:
    values = [float(run[field]) for run in runs if run.get(field) is not None]
    return statistics.median(values) if values else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fanout", type=int, default=4)
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--smolvm", default=os.environ.get("SMOLVM_BIN", "smolvm"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.fanout < 1 or args.parallel < 1 or args.repetitions < 1:
        parser.error("fanout, parallel and repetitions must be positive")
    if args.parallel > args.fanout:
        parser.error("parallel cannot exceed fanout")
    smolvm = str(Path(args.smolvm).expanduser().resolve())
    if not Path(smolvm).is_file():
        parser.error(f"smolvm binary does not exist: {smolvm}")
    if shutil.which("docker") is None:
        parser.error("docker is required for the matched controls")

    label = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    token = uuid.uuid4().hex[:8]
    output = args.output or ROOT / "results" / f"{label}-braintrust-warm-fanout.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    env = runtime_environment(smolvm)
    image, preparation = prepare_worker_image()
    source = f"bt-warm-source-{token}"
    direct_runs: list[dict[str, Any]] = []
    smol_pool_runs: list[dict[str, Any]] = []
    docker_fresh_runs: list[dict[str, Any]] = []
    docker_pool_runs: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="braintrust-warm-results-") as temporary:
        results = Path(temporary).resolve()
        source_info: dict[str, Any] | None = None
        try:
            source_info = create_smol_source(smolvm, source, env)
            for repetition in range(1, args.repetitions + 1):
                providers = ["smol-direct", "docker-fresh", "smol-pool", "docker-pool"]
                shift = (repetition - 1) % len(providers)
                providers = providers[shift:] + providers[:shift]
                for provider in providers:
                    if provider == "smol-direct":
                        run = run_smol_direct(
                            smolvm,
                            source,
                            results,
                            args.fanout,
                            args.parallel,
                            repetition,
                            token,
                            env,
                        )
                        direct_runs.append(run)
                        print(
                            f"[Smol direct {repetition}] checkpoint-to-result="
                            f"{run['checkpoint_to_result_seconds']:.3f}s",
                            flush=True,
                        )
                    elif provider == "smol-pool":
                        run = run_smol_pool(
                            smolvm,
                            source,
                            results,
                            args.fanout,
                            args.parallel,
                            repetition,
                            token,
                            env,
                        )
                        smol_pool_runs.append(run)
                        print(
                            f"[Smol pool {repetition}] fill={run['pool_fill_seconds']:.3f}s "
                            f"release-to-result={run['release_to_result_seconds']:.3f}s",
                            flush=True,
                        )
                    elif provider == "docker-fresh":
                        run = run_docker_fresh(
                            image,
                            results,
                            args.fanout,
                            args.parallel,
                            repetition,
                            token,
                        )
                        docker_fresh_runs.append(run)
                        print(
                            f"[Docker fresh {repetition}] start-to-result="
                            f"{run['start_to_result_seconds']:.3f}s",
                            flush=True,
                        )
                    else:
                        run = run_docker_pool(
                            image,
                            results,
                            args.fanout,
                            args.parallel,
                            repetition,
                            token,
                        )
                        docker_pool_runs.append(run)
                        print(
                            f"[Docker pool {repetition}] fill={run['pool_fill_seconds']:.3f}s "
                            f"release-to-result={run['release_to_result_seconds']:.3f}s",
                            flush=True,
                        )
        finally:
            command(
                [smolvm, "machine", "delete", "--name", source, "--cascade"],
                env=env,
                check=False,
                timeout=300,
            )
            start_log_path(source).unlink(missing_ok=True)

    direct = latency_summary(direct_runs, "checkpoint_to_result_seconds")
    smol_pool = latency_summary(smol_pool_runs, "release_to_result_seconds")
    docker_fresh = latency_summary(docker_fresh_runs, "start_to_result_seconds")
    docker_pool = latency_summary(docker_pool_runs, "release_to_result_seconds")
    direct["branch_seconds_median"] = median_optional(direct_runs, "branch_seconds")
    smol_pool["pool_fill_seconds_median"] = median_optional(
        smol_pool_runs, "pool_fill_seconds"
    )
    docker_pool["pool_fill_seconds_median"] = median_optional(
        docker_pool_runs, "pool_fill_seconds"
    )
    payload = {
        "schema_version": 2,
        "benchmark": "braintrust-warm-inherited-worker",
        "workload": {
            "repository": REPOSITORY,
            "revision": REVISION,
            "base_image": IMAGE,
            "worker_image": image,
            "dataset": "GH Archive 2024-01-15 hour 15",
            "execution_boundary": (
                "ordinary Node process with an open better-sqlite3 connection, "
                "prepared statements and warmed query pages"
            ),
        },
        "resources_per_environment": {
            "cpus": RESOURCE_CPUS,
            "memory_mb": RESOURCE_MEMORY_MB,
        },
        "fanout": args.fanout,
        "parallel": args.parallel,
        "repetitions": args.repetitions,
        "source": source_info,
        "preparation": preparation,
        "smol_direct": direct,
        "smol_retained_pool": smol_pool,
        "docker_fresh": docker_fresh,
        "docker_prewarmed_pool": docker_pool,
        "comparisons": {
            "smol_direct_vs_docker_fresh": (
                docker_fresh["wall_seconds"]["median"]
                / direct["wall_seconds"]["median"]
            ),
            "smol_retained_vs_docker_prewarmed": (
                docker_pool["wall_seconds"]["median"]
                / smol_pool["wall_seconds"]["median"]
            ),
        },
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_model": host_cpu_model(),
            "logical_cpus": os.cpu_count(),
            "virtualization": command(
                ["systemd-detect-virt"], check=False
            ).stdout.strip(),
        },
        "runtime": {
            "smolvm_path": smolvm,
            "smolvm_version": command([smolvm, "--version"], env=env).stdout.strip(),
            "docker_version": command(["docker", "--version"]).stdout.strip(),
            "container_engine": (
                "Podman"
                if "podman" in os.environ.get("DOCKER_HOST", "").lower()
                else "Docker"
            ),
            "container_server_version": command(
                ["docker", "info", "--format", "{{.ServerVersion}}"]
            ).stdout.strip(),
        },
    }
    output.write_text(json.dumps(payload, indent=2) + "\n")
    print(
        f"All {args.fanout * args.repetitions * 4} results correct; wrote {output}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
