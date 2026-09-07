#!/usr/bin/env python3
"""Branch an initialized tau2-bench task and compare prepared containers."""

from __future__ import annotations

import argparse
import hashlib
import html
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
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from smol import ExecOptions, Machine, MachineConfig, ResourceSpec


ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).resolve().parent / "workloads" / "tau2_worker.py"
CASES_PATH = Path(__file__).resolve().parent / "workloads" / "tau2_cases.json"
CASES = json.loads(CASES_PATH.read_text())
TAU2_REPOSITORY = CASES["source_repository"]
TAU2_REVISION = CASES["source_revision"]
PYTHON_IMAGE = (
    "python:3.12-slim-bookworm@"
    "sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254"
)
CPUS = 2
MEMORY_MB = 2048
PORT = 8767
IMAGE_LAYOUT_VERSION = "text-agent-v1"
CLIENT = r"""
import json, os, time, urllib.parse, urllib.request
base = "http://127.0.0.1:8767"
started = time.perf_counter()
for attempt in range(100):
    try:
        with urllib.request.urlopen(base + "/health", timeout=1) as response:
            health = json.load(response)
        if health.get("ready"):
            break
    except Exception:
        if attempt == 99:
            raise
        time.sleep(0.05)
else:
    raise RuntimeError("tau2 worker did not become ready")
health_seconds = time.perf_counter() - started
query = urllib.parse.urlencode({"label": os.environ["LABEL"]})
started = time.perf_counter()
with urllib.request.urlopen(base + "/candidate?" + query, timeout=30) as response:
    payload = json.load(response)
payload["pre_action_health"] = health
payload["client_health_seconds"] = health_seconds
payload["client_action_seconds"] = time.perf_counter() - started
print(json.dumps(payload, separators=(",", ":")))
"""
HEALTH_CLIENT = f"""
import json, time, urllib.request
for attempt in range(200):
    try:
        with urllib.request.urlopen("http://127.0.0.1:{PORT}/health", timeout=1) as response:
            payload = json.load(response)
        if payload.get("ready"):
            print(json.dumps(payload, separators=(",", ":")))
            break
    except Exception:
        if attempt == 199:
            raise
        time.sleep(0.05)
else:
    raise RuntimeError("tau2 worker did not become ready")
"""


@dataclass(frozen=True)
class Candidate:
    label: str
    title: str
    expected_reward: float
    expected_city: str
    expected_order_status: str


@dataclass
class CandidateResult:
    runtime: str
    repetition: int
    label: str
    title: str
    duration_seconds: float
    correct: bool
    reward: float | None
    db_match: bool | None
    starting_db_hash: str | None
    final_db_hash: str | None
    city: str | None
    order_status: str | None
    tool_calls: list[str] | None
    worker_seconds: float | None
    worker_initialize_seconds: float | None
    client_health_seconds: float | None
    client_action_seconds: float | None
    error: str | None


def candidates() -> list[Candidate]:
    return [
        Candidate(
            label=item["label"],
            title=item["title"],
            expected_reward=item["expected_reward"],
            expected_city=item["expected_city"],
            expected_order_status=item["expected_order_status"],
        )
        for item in CASES["candidates"]
    ]


def package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def command_version(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    output = result.stdout.strip() or result.stderr.strip()
    return output.splitlines()[0] if result.returncode == 0 and output else None


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def numeric_median(results: list[dict[str, object]], key: str) -> float | None:
    values = [
        value for item in results if isinstance((value := item.get(key)), (float, int))
    ]
    return statistics.median(values) if values else None


def checked_exec(machine: Machine, command: str, timeout: int = 900) -> str:
    result = machine.exec(
        ["/bin/bash", "-lc", command], ExecOptions(timeout=timeout, workdir="/")
    )
    if result.exit_code:
        raise RuntimeError(
            f"tau2 preparation failed ({result.exit_code})\n"
            f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"
        )
    return result.stdout


def read_health(machine: Machine, timeout: int = 120) -> dict[str, object]:
    output = checked_exec(machine, f"python - <<'PY'\n{HEALTH_CLIENT}\nPY", timeout)
    return json.loads(output.strip().splitlines()[-1])


def install_tau2_command() -> str:
    return (
        "set -euo pipefail; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update -qq; "
        "apt-get install -y -qq --no-install-recommends ca-certificates git; "
        "rm -rf /var/lib/apt/lists/* /opt/tau2-bench; "
        f"git clone --quiet --filter=blob:none --sparse {TAU2_REPOSITORY} /opt/tau2-bench; "
        "git -C /opt/tau2-bench sparse-checkout set src/tau2 "
        "data/tau2/domains/retail data/tau2/user_simulator; "
        f"git -C /opt/tau2-bench checkout --quiet {TAU2_REVISION}; "
        "python -m pip install --disable-pip-version-check --no-cache-dir "
        "--editable /opt/tau2-bench"
    )


def prepare_checkpoint(name: str) -> tuple[Machine, float, dict[str, object]]:
    started = time.perf_counter()
    machine = Machine.create(
        MachineConfig(
            name=name,
            image=PYTHON_IMAGE,
            resources=ResourceSpec(
                cpus=CPUS,
                memory_mb=MEMORY_MB,
                storage_gb=10,
                network=True,
            ),
            persistent=True,
            checkpoint=True,
        )
    )
    created = time.perf_counter()
    try:
        checked_exec(machine, install_tau2_command(), timeout=1800)
        installed = time.perf_counter()
        machine.write_file("/opt/tau2-worker/tau2_worker.py", WORKER.read_bytes())
        machine.write_file("/opt/tau2-worker/tau2_cases.json", CASES_PATH.read_bytes())
        checked_exec(
            machine,
            "nohup python /opt/tau2-worker/tau2_worker.py "
            "</dev/null >/tmp/tau2-worker.log 2>&1 &",
        )
        health = read_health(machine)
        checked_exec(
            machine,
            "nohup /usr/local/bin/smolvm-branch-ready "
            "</dev/null >/tmp/smolvm-branch-ready.log 2>&1 &",
        )
    except BaseException:
        machine.delete()
        raise
    ready = time.perf_counter()
    return (
        machine,
        ready - started,
        {
            "machine_create_seconds": created - started,
            "tau2_install_seconds": installed - created,
            "worker_initialize_seconds": ready - installed,
            "health": health,
        },
    )


def prepare_docker_image() -> tuple[str, float, bool]:
    if shutil.which("docker") is None:
        raise RuntimeError("Docker is required for the tau2 control")
    if subprocess.run(
        ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode:
        raise RuntimeError("`docker info` failed")
    workload_hash = hashlib.sha256(
        WORKER.read_bytes() + CASES_PATH.read_bytes() + IMAGE_LAYOUT_VERSION.encode()
    ).hexdigest()[:12]
    image = f"smol-bench/tau2:{TAU2_REVISION[:12]}-{workload_hash}"
    if (
        subprocess.run(
            ["docker", "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    ):
        return image, 0.0, False
    dockerfile = f"""FROM {PYTHON_IMAGE}
RUN apt-get update -qq \\
 && apt-get install -y -qq --no-install-recommends ca-certificates git \\
 && rm -rf /var/lib/apt/lists/* \\
 && git clone --quiet --filter=blob:none --sparse {TAU2_REPOSITORY} /opt/tau2-bench \\
 && git -C /opt/tau2-bench sparse-checkout set src/tau2 data/tau2/domains/retail data/tau2/user_simulator \\
 && git -C /opt/tau2-bench checkout --quiet {TAU2_REVISION} \\
 && python -m pip install --disable-pip-version-check --no-cache-dir --editable /opt/tau2-bench
COPY bench/workloads/tau2_worker.py /opt/tau2-worker/tau2_worker.py
COPY bench/workloads/tau2_cases.json /opt/tau2-worker/tau2_cases.json
"""
    started = time.perf_counter()
    result = subprocess.run(
        ["docker", "build", "--file", "-", "--tag", image, "."],
        input=dockerfile,
        text=True,
        capture_output=True,
        timeout=1800,
        cwd=ROOT,
    )
    duration = time.perf_counter() - started
    if result.returncode:
        raise RuntimeError(
            f"tau2 Docker image failed to build ({result.returncode})\n"
            f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"
        )
    return image, duration, True


def failed_candidate(
    runtime: str,
    repetition: int,
    candidate: Candidate,
    duration: float,
    error: object,
) -> CandidateResult:
    return CandidateResult(
        runtime=runtime,
        repetition=repetition,
        label=candidate.label,
        title=candidate.title,
        duration_seconds=duration,
        correct=False,
        reward=None,
        db_match=None,
        starting_db_hash=None,
        final_db_hash=None,
        city=None,
        order_status=None,
        tool_calls=None,
        worker_seconds=None,
        worker_initialize_seconds=None,
        client_health_seconds=None,
        client_action_seconds=None,
        error=str(error)[-2000:],
    )


def parse_candidate(
    *,
    runtime: str,
    repetition: int,
    candidate: Candidate,
    duration: float,
    return_code: int,
    stdout: str,
    stderr: str,
    expected_initial_hash: str,
) -> CandidateResult:
    try:
        payload = json.loads(stdout.strip())
    except (json.JSONDecodeError, TypeError):
        payload = {}
    state = payload.get("state")
    state = state if isinstance(state, dict) else {}
    address = state.get("address")
    address = address if isinstance(address, dict) else {}
    pre_health = payload.get("pre_action_health")
    pre_health = (
        pre_health if isinstance(pre_health, dict) else payload.get("health", {})
    )
    expected_db_match = candidate.expected_reward == 1.0
    correct = (
        return_code == 0
        and payload.get("label") == candidate.label
        and payload.get("expected_reward") == candidate.expected_reward
        and payload.get("reward") == candidate.expected_reward
        and payload.get("db_match") is expected_db_match
        and payload.get("initial_db_hash") == expected_initial_hash
        and payload.get("starting_db_hash") == expected_initial_hash
        and payload.get("action_count_before") == 0
        and payload.get("action_count_after") == 1
        and address.get("city") == candidate.expected_city
        and state.get("order_status") == candidate.expected_order_status
        and payload.get("tool_errors") == []
        and isinstance(pre_health, dict)
        and pre_health.get("initial_db_hash") == expected_initial_hash
        and pre_health.get("action_count") == 0
        and pre_health.get("source_revision") == TAU2_REVISION
    )
    return CandidateResult(
        runtime=runtime,
        repetition=repetition,
        label=candidate.label,
        title=candidate.title,
        duration_seconds=duration,
        correct=correct,
        reward=payload.get("reward"),
        db_match=payload.get("db_match"),
        starting_db_hash=payload.get("starting_db_hash"),
        final_db_hash=payload.get("final_db_hash"),
        city=address.get("city"),
        order_status=state.get("order_status"),
        tool_calls=payload.get("tool_calls"),
        worker_seconds=payload.get("worker_seconds"),
        worker_initialize_seconds=(
            pre_health.get("worker_initialize_seconds")
            if isinstance(pre_health, dict)
            else None
        ),
        client_health_seconds=payload.get("client_health_seconds"),
        client_action_seconds=payload.get("client_action_seconds"),
        error=None if correct else (stderr[-2000:] or stdout[-2000:]),
    )


def run_smol_candidate(
    machine: Machine,
    candidate: Candidate,
    repetition: int,
    initial_hash: str,
) -> CandidateResult:
    started = time.perf_counter()
    try:
        result = machine.exec(
            ["python", "-c", CLIENT],
            ExecOptions(env={"LABEL": candidate.label}, timeout=60, workdir="/"),
        )
    except Exception as error:
        return failed_candidate(
            "smol-branch", repetition, candidate, time.perf_counter() - started, error
        )
    return parse_candidate(
        runtime="smol-branch",
        repetition=repetition,
        candidate=candidate,
        duration=time.perf_counter() - started,
        return_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        expected_initial_hash=initial_hash,
    )


def run_docker_candidate(
    image: str,
    candidate: Candidate,
    repetition: int,
    initial_hash: str,
) -> CandidateResult:
    started = time.perf_counter()
    container = f"smol-tau2-{uuid.uuid4().hex[:12]}"
    try:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                container,
                "--network",
                "none",
                "--cpus",
                str(CPUS),
                "--memory",
                f"{MEMORY_MB}m",
                "--entrypoint",
                "python",
                image,
                "/opt/tau2-worker/tau2_worker.py",
                "--once",
                candidate.label,
            ],
            text=True,
            capture_output=True,
            timeout=120,
        )
    except Exception as error:
        subprocess.run(
            ["docker", "rm", "--force", container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return failed_candidate(
            "docker", repetition, candidate, time.perf_counter() - started, error
        )
    return parse_candidate(
        runtime="docker",
        repetition=repetition,
        candidate=candidate,
        duration=time.perf_counter() - started,
        return_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        expected_initial_hash=initial_hash,
    )


def render_report(payload: dict[str, object], output: Path) -> None:
    smol = payload["smol"]
    docker = payload["docker"]
    smol_total = smol["median_branch_seconds"] + smol["median_candidate_wall_seconds"]
    ratio = docker["median_start_and_candidate_seconds"] / smol_total
    comparison = f"{ratio:.2f}× faster" if ratio >= 1 else f"{1 / ratio:.2f}× slower"
    latest = [
        result
        for result in smol["results"]
        if result["repetition"] == payload["repetitions"]
    ]
    cards = "".join(
        f'<article class="candidate {"winner" if result["reward"] == 1 else ""}">'
        f"<span>{'selected' if result['reward'] == 1 else 'rejected'}</span>"
        f"<h2>{html.escape(result['title'])}</h2>"
        f"<p>reward <b>{result['reward']}</b> · city <b>{html.escape(result['city'] or '')}</b> · "
        f"order <b>{html.escape(result['order_status'] or '')}</b></p></article>"
        for result in latest
    )
    output.write_text(
        f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>tau2-bench branch search</title><style>
body{{font-family:system-ui,sans-serif;max-width:1150px;margin:50px auto;padding:0 24px;background:#0b1020;color:#f9fafb}}h1{{font-size:52px;line-height:1.05;margin-bottom:8px}}p{{color:#cbd5e1}}.metrics,.candidates{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin:28px 0}}.metric,.candidate{{background:#172033;border:1px solid #334155;border-radius:16px;padding:18px}}.metric strong{{display:block;font-size:34px;color:#ff5c35}}.candidate span{{color:#94a3b8;text-transform:uppercase;letter-spacing:.12em;font-size:12px}}.candidate.winner{{border-color:#22c55e;box-shadow:0 0 0 1px #22c55e}}.candidate.winner span{{color:#4ade80}}code{{color:#fdba74}}</style></head><body>
<h1>Branch one agent state. Test four decisions.</h1><p>Sierra's pinned τ²-bench retail task is initialized once. Four isolated futures execute ordinary benchmark tool calls and the official database evaluator selects the valid state.</p>
<div class="metrics"><div class="metric"><strong>{smol["median_branch_seconds"] * 1000:.0f} ms</strong>median four-way branch</div><div class="metric"><strong>{smol["median_candidate_wall_seconds"]:.2f} s</strong>all Smol candidates</div><div class="metric"><strong>{docker["median_start_and_candidate_seconds"]:.2f} s</strong>fresh prepared containers</div><div class="metric"><strong>{comparison}</strong>Smol end to end</div></div>
<div class="candidates">{cards}</div><p><code>{CASES["domain"]}/{CASES["task_id"]}</code> at <code>{TAU2_REVISION[:12]}</code>. {smol["correct"]}/{len(smol["results"])} Smol and {docker["correct"]}/{len(docker["results"])} Docker outcomes passed exact-state validation; the source stayed unchanged and responsive after every wave.</p></body></html>"""
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    options = candidates()
    if args.repetitions < 1 or not 1 <= args.parallel <= len(options):
        parser.error(
            "repetitions must be positive and parallel must be between 1 and 4"
        )

    label = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_id = f"{label}-{uuid.uuid4().hex[:6]}"
    output = args.output or Path("results") / f"{label}-tau2-fanout.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    print("[Prepare] Building or reusing the pinned Docker control", flush=True)
    docker_image, docker_prepare, docker_built = prepare_docker_image()
    print("[Prepare] Loading the tau2 retail environment in the golden", flush=True)
    golden, checkpoint_prepare, checkpoint_phases = prepare_checkpoint(
        f"tau2-golden-{run_id}"
    )
    health = checkpoint_phases["health"]
    initial_hash = health["initial_db_hash"]
    smol_runs: list[dict[str, object]] = []
    docker_runs: list[dict[str, object]] = []
    try:
        for repetition in range(1, args.repetitions + 1):
            providers = ["smol", "docker"]
            if repetition % 2 == 0:
                providers.reverse()
            for provider in providers:
                if provider == "smol":
                    started = time.perf_counter()
                    machines = golden.branch_batch(
                        names=[
                            f"tau2-{run_id}-r{repetition}-{candidate.label}"
                            for candidate in options
                        ]
                    )
                    branch_seconds = time.perf_counter() - started
                    try:
                        started = time.perf_counter()
                        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
                            results = list(
                                pool.map(
                                    lambda pair: run_smol_candidate(
                                        pair[0], pair[1], repetition, initial_hash
                                    ),
                                    zip(machines, options, strict=True),
                                )
                            )
                        candidate_wall = time.perf_counter() - started
                    finally:
                        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
                            list(pool.map(lambda machine: machine.delete(), machines))
                    source_health = read_health(golden, 15)
                    source_unchanged = (
                        source_health["action_count"] == 0
                        and source_health["initial_db_hash"] == initial_hash
                    )
                    print(
                        f"[Smol {repetition}] branch={branch_seconds:.3f}s "
                        f"candidates={candidate_wall:.3f}s "
                        f"source_unchanged={source_unchanged}",
                        flush=True,
                    )
                    smol_runs.append(
                        {
                            "repetition": repetition,
                            "branch_seconds": branch_seconds,
                            "candidate_wall_seconds": candidate_wall,
                            "source_unchanged": source_unchanged,
                            "results": [asdict(result) for result in results],
                        }
                    )
                else:
                    started = time.perf_counter()
                    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
                        results = list(
                            pool.map(
                                lambda candidate: run_docker_candidate(
                                    docker_image, candidate, repetition, initial_hash
                                ),
                                options,
                            )
                        )
                    wall = time.perf_counter() - started
                    print(
                        f"[Docker {repetition}] start+candidates={wall:.3f}s",
                        flush=True,
                    )
                    docker_runs.append(
                        {
                            "repetition": repetition,
                            "start_and_candidate_seconds": wall,
                            "results": [asdict(result) for result in results],
                        }
                    )
    finally:
        golden.delete()

    smol_results = [result for run in smol_runs for result in run["results"]]
    docker_results = [result for run in docker_runs for result in run["results"]]
    payload = {
        "schema_version": 1,
        "validated_at": datetime.now(UTC).isoformat(),
        "workload": "tau2-bench retail task 33 decision search",
        "repository": TAU2_REPOSITORY,
        "revision": TAU2_REVISION,
        "tau2_version": "1.0.1",
        "domain": CASES["domain"],
        "task_id": CASES["task_id"],
        "task_reason_for_call": (
            "Resolve a pending-office-order request and update the customer's profile "
            "to the Seattle address already present on the order."
        ),
        "fanout": len(options),
        "parallel": args.parallel,
        "repetitions": args.repetitions,
        "resources_per_environment": {"cpus": CPUS, "memory_mb": MEMORY_MB},
        "image": PYTHON_IMAGE,
        "checkpoint_prepare_seconds": checkpoint_prepare,
        "checkpoint_prepare_phases": checkpoint_phases,
        "docker_image_prepare_seconds": docker_prepare,
        "docker_image_built": docker_built,
        "initial_db_hash": initial_hash,
        "smol": {
            "median_branch_seconds": statistics.median(
                run["branch_seconds"] for run in smol_runs
            ),
            "median_candidate_wall_seconds": statistics.median(
                run["candidate_wall_seconds"] for run in smol_runs
            ),
            "candidate_latency_p50_seconds": statistics.median(
                result["duration_seconds"] for result in smol_results
            ),
            "candidate_latency_p99_seconds": percentile(
                [result["duration_seconds"] for result in smol_results], 0.99
            ),
            "worker_p50_seconds": numeric_median(smol_results, "worker_seconds"),
            "inherited_worker_initialize_seconds": numeric_median(
                smol_results, "worker_initialize_seconds"
            ),
            "source_unchanged": all(run["source_unchanged"] for run in smol_runs),
            "correct": sum(result["correct"] for result in smol_results),
            "runs": smol_runs,
            "results": smol_results,
        },
        "docker": {
            "median_start_and_candidate_seconds": statistics.median(
                run["start_and_candidate_seconds"] for run in docker_runs
            ),
            "candidate_latency_p50_seconds": statistics.median(
                result["duration_seconds"] for result in docker_results
            ),
            "candidate_latency_p99_seconds": percentile(
                [result["duration_seconds"] for result in docker_results], 0.99
            ),
            "worker_p50_seconds": numeric_median(docker_results, "worker_seconds"),
            "worker_initialize_p50_seconds": numeric_median(
                docker_results, "worker_initialize_seconds"
            ),
            "correct": sum(result["correct"] for result in docker_results),
            "runs": docker_runs,
            "results": docker_results,
        },
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "logical_cpus": os.cpu_count(),
        },
        "software": {
            "python": platform.python_version(),
            "smolmachines": package_version("smolmachines"),
            "smolvm": command_version(["smolvm", "--version"]),
            "smolvm_source_revision": os.environ.get("SMOLVM_REVISION"),
            "docker": command_version(["docker", "--version"]),
        },
    }
    render_report(payload, output.with_suffix(".html"))
    output.write_text(json.dumps(payload, indent=2) + "\n")
    expected = len(options) * args.repetitions
    print(f"Wrote {output} and {output.with_suffix('.html')}", flush=True)
    correct = payload["smol"]["correct"] == payload["docker"]["correct"] == expected
    return 0 if correct and payload["smol"]["source_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
