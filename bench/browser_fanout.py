#!/usr/bin/env python3
"""Compare branching a live Chromium process with starting browser containers."""

from __future__ import annotations

import argparse
import base64
import binascii
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


PLAYWRIGHT_VERSION = "1.60.0"
IMAGE = (
    "mcr.microsoft.com/playwright/python:v1.60.0-noble@"
    "sha256:8ff591d613b01c884cc488339ed4318b4513eaf0c57a164a878ba49e70e3f384"
)
CPUS = 2
MEMORY_MB = 2048
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).resolve().parent / "workloads" / "browser_worker.py"
CLIENT = r"""
import json, os, time, urllib.parse, urllib.request
base = "http://127.0.0.1:8765"
started = time.perf_counter()
for attempt in range(100):
    try:
        with urllib.request.urlopen(base + "/health", timeout=1) as response:
            if json.load(response).get("ready"):
                break
    except Exception:
        if attempt == 99:
            raise
        time.sleep(0.05)
health_seconds = time.perf_counter() - started
url = base + "/action?" + urllib.parse.urlencode({"id": os.environ["BRANCH_ID"]})
started = time.perf_counter()
with urllib.request.urlopen(url, timeout=40) as response:
    payload = json.load(response)
payload["client_health_seconds"] = health_seconds
payload["client_action_seconds"] = time.perf_counter() - started
print(json.dumps(payload, separators=(",", ":")))
"""
HEALTH_CLIENT = r"""
import json, time, urllib.request
for attempt in range(100):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=1) as response:
            if json.load(response).get("ready"):
                break
    except Exception:
        if attempt == 99:
            raise
        time.sleep(0.05)
else:
    raise RuntimeError("browser worker did not become ready")
"""


@dataclass
class ActionResult:
    runtime: str
    repetition: int
    branch_id: str
    duration_seconds: float
    correct: bool
    action_count: int | None
    result: str | None
    screenshot_sha256: str | None
    screenshot_base64: str | None
    worker_action_seconds: float | None
    browser_launch_seconds: float | None
    client_health_seconds: float | None
    client_action_seconds: float | None
    error: str | None


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
    values = [result[key] for result in results if isinstance(result.get(key), float)]
    return statistics.median(values) if values else None


def without_screenshots(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: without_screenshots(item)
            for key, item in value.items()
            if key != "screenshot_base64"
        }
    if isinstance(value, list):
        return [without_screenshots(item) for item in value]
    return value


def parse_action(
    *,
    runtime: str,
    repetition: int,
    branch_id: str,
    duration: float,
    return_code: int,
    stdout: str,
    stderr: str,
) -> ActionResult:
    try:
        payload = json.loads(stdout.strip())
    except (json.JSONDecodeError, TypeError):
        payload = {}
    expected = f"{branch_id} · action 1"
    screenshot = payload.get("screenshot_base64")
    try:
        screenshot_bytes = base64.b64decode(screenshot, validate=True)
    except (binascii.Error, TypeError, ValueError):
        screenshot_bytes = b""
    screenshot_sha256 = payload.get("screenshot_sha256")
    screenshot_valid = (
        screenshot_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        and screenshot_sha256 == hashlib.sha256(screenshot_bytes).hexdigest()
    )
    correct = (
        return_code == 0
        and payload.get("branch_id") == branch_id
        and payload.get("action_count") == 1
        and payload.get("result") == expected
        and screenshot_valid
    )
    return ActionResult(
        runtime=runtime,
        repetition=repetition,
        branch_id=branch_id,
        duration_seconds=duration,
        correct=correct,
        action_count=payload.get("action_count"),
        result=payload.get("result"),
        screenshot_sha256=screenshot_sha256,
        screenshot_base64=screenshot,
        worker_action_seconds=payload.get("worker_action_seconds"),
        browser_launch_seconds=payload.get("browser_launch_seconds"),
        client_health_seconds=payload.get("client_health_seconds"),
        client_action_seconds=payload.get("client_action_seconds"),
        error=None if correct else (stderr[-2000:] or stdout[-2000:]),
    )


def checked_exec(machine: Machine, command: str, timeout: int = 600) -> None:
    result = machine.exec(
        ["/bin/bash", "-lc", command], ExecOptions(timeout=timeout, workdir="/")
    )
    if result.exit_code != 0:
        raise RuntimeError(
            f"browser checkpoint preparation failed ({result.exit_code})\n"
            f"stdout:\n{result.stdout[-4000:]}\n"
            f"stderr:\n{result.stderr[-4000:]}"
        )


def prepare_checkpoint(name: str) -> tuple[Machine, float, dict[str, float]]:
    started = time.perf_counter()
    machine = Machine.create(
        MachineConfig(
            name=name,
            image=IMAGE,
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
        checked_exec(
            machine,
            f"python -m pip install --disable-pip-version-check "
            f"playwright=={PLAYWRIGHT_VERSION} && mkdir -p /opt/smol-browser",
        )
        machine.write_file("/opt/smol-browser/worker.py", WORKER.read_bytes())
        checked_exec(
            machine,
            "nohup python /opt/smol-browser/worker.py "
            "</dev/null >/tmp/smol-browser.log 2>&1 &",
        )
        checked_exec(machine, f"python - <<'PY'\n{HEALTH_CLIENT}\nPY", timeout=120)
    except BaseException:
        machine.delete()
        raise
    ready = time.perf_counter()
    return (
        machine,
        ready - started,
        {
            "machine_create_seconds": created - started,
            "application_prepare_seconds": ready - created,
        },
    )


def prepare_docker_image() -> tuple[str, float, bool]:
    if shutil.which("docker") is None:
        raise RuntimeError("Docker is required for the browser baseline")
    if subprocess.run(
        ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode:
        raise RuntimeError("`docker info` failed")
    worker_hash = hashlib.sha256(WORKER.read_bytes()).hexdigest()[:12]
    image = f"smol-bench/live-browser:{PLAYWRIGHT_VERSION}-{worker_hash}"
    if (
        subprocess.run(
            ["docker", "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    ):
        return image, 0.0, False
    dockerfile = f"""FROM {IMAGE}
RUN python -m pip install --disable-pip-version-check playwright=={PLAYWRIGHT_VERSION}
COPY bench/workloads/browser_worker.py /opt/smol-browser/worker.py
"""
    started = time.perf_counter()
    result = subprocess.run(
        ["docker", "build", "--file", "-", "--tag", image, "."],
        input=dockerfile,
        text=True,
        capture_output=True,
        timeout=1800,
        cwd=REPOSITORY_ROOT,
    )
    duration = time.perf_counter() - started
    if result.returncode:
        raise RuntimeError(
            f"browser Docker image failed to build ({result.returncode})\n"
            f"stdout:\n{result.stdout[-4000:]}\n"
            f"stderr:\n{result.stderr[-4000:]}"
        )
    return image, duration, True


def failed_action(
    runtime: str,
    repetition: int,
    branch_id: str,
    duration: float,
    error: Exception,
) -> ActionResult:
    return ActionResult(
        runtime=runtime,
        repetition=repetition,
        branch_id=branch_id,
        duration_seconds=duration,
        correct=False,
        action_count=None,
        result=None,
        screenshot_sha256=None,
        screenshot_base64=None,
        worker_action_seconds=None,
        browser_launch_seconds=None,
        client_health_seconds=None,
        client_action_seconds=None,
        error=f"{type(error).__name__}: {error}",
    )


def run_smol_action(machine: Machine, branch_id: str, repetition: int) -> ActionResult:
    started = time.perf_counter()
    try:
        result = machine.exec(
            ["python", "-c", CLIENT],
            ExecOptions(env={"BRANCH_ID": branch_id}, timeout=60, workdir="/"),
        )
    except Exception as error:
        return failed_action(
            "smol-branch", repetition, branch_id, time.perf_counter() - started, error
        )
    return parse_action(
        runtime="smol-branch",
        repetition=repetition,
        branch_id=branch_id,
        duration=time.perf_counter() - started,
        return_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def run_docker_action(image: str, branch_id: str, repetition: int) -> ActionResult:
    started = time.perf_counter()
    container = f"smol-browser-bench-{uuid.uuid4().hex[:12]}"
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
                "/opt/smol-browser/worker.py",
                "--once",
                branch_id,
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
        return failed_action(
            "docker", repetition, branch_id, time.perf_counter() - started, error
        )
    return parse_action(
        runtime="docker",
        repetition=repetition,
        branch_id=branch_id,
        duration=time.perf_counter() - started,
        return_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def screenshots_are_unique(results: list[dict[str, object]]) -> bool:
    hashes = [result.get("screenshot_sha256") for result in results]
    return all(isinstance(item, str) for item in hashes) and len(hashes) == len(
        set(hashes)
    )


def render_report(payload: dict[str, object], output: Path) -> None:
    smol = payload["smol"]
    docker = payload["docker"]
    screenshots = [
        result
        for result in smol["results"]
        if result["repetition"] == payload["repetitions"]
        and result["correct"]
        and isinstance(result["screenshot_base64"], str)
    ]
    cards = "".join(
        f'<figure><img src="data:image/png;base64,{result["screenshot_base64"]}" '
        f'alt="{html.escape(result["branch_id"])}"><figcaption>'
        f"{html.escape(result['result'])}</figcaption></figure>"
        for result in screenshots
    )
    smol_total = smol["median_branch_seconds"] + smol["median_action_wall_seconds"]
    speedup = docker["median_start_and_action_seconds"] / smol_total
    comparison = (
        f"{speedup:.2f}× faster" if speedup >= 1 else f"{1 / speedup:.2f}× slower"
    )
    output.write_text(
        f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Live browser branching</title><style>
body{{font-family:system-ui,sans-serif;max-width:1200px;margin:50px auto;padding:0 24px;background:#0b1020;color:#f9fafb}}h1{{font-size:52px;margin-bottom:8px}}p{{color:#cbd5e1}}.metrics,.shots{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin:28px 0}}.metric,figure{{background:#172033;border:1px solid #334155;border-radius:16px;padding:18px;margin:0}}strong{{display:block;font-size:36px;color:#ff5c35}}img{{width:100%;border-radius:8px}}figcaption{{padding-top:10px;font-family:monospace}}</style></head><body>
<h1>Branch a running browser.</h1><p>One live Chromium process becomes {payload["fanout"]} isolated browsers. Each branch performs a different action from the same page state.</p>
<div class="metrics"><div class="metric"><strong>{smol["median_branch_seconds"] * 1000:.0f} ms</strong>median {payload["fanout"]}-way branch</div><div class="metric"><strong>{smol["median_action_wall_seconds"]:.2f} s</strong>all branched browser actions</div><div class="metric"><strong>{docker["median_start_and_action_seconds"]:.2f} s</strong>Docker start + browser actions</div><div class="metric"><strong>{comparison}</strong>Smol on this tiny page</div></div>
<div class="shots">{cards}</div><p>{smol["correct"]}/{len(smol["results"])} Smol and {docker["correct"]}/{len(docker["results"])} Docker actions passed exact state-isolation checks.</p></body></html>"""
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fanout", type=int, default=4)
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.fanout, args.parallel, args.repetitions) < 1:
        parser.error("fanout, parallel, and repetitions must be positive")
    if args.parallel > args.fanout:
        parser.error("parallel cannot exceed fanout")

    label = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_id = f"{label}-{uuid.uuid4().hex[:6]}"
    output = args.output or Path("results") / f"{label}-browser-fanout.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    docker_image, docker_prepare, docker_built = prepare_docker_image()
    golden, checkpoint_prepare, checkpoint_phases = prepare_checkpoint(
        f"browser-golden-{run_id}"
    )
    smol_runs = []
    docker_runs = []
    try:
        for repetition in range(1, args.repetitions + 1):
            providers = ["smol", "docker"]
            if repetition % 2 == 0:
                providers.reverse()
            for provider in providers:
                ids = [f"branch-{repetition}-{index}" for index in range(args.fanout)]
                if provider == "smol":
                    started = time.perf_counter()
                    machines = golden.branch_batch(
                        names=[
                            f"browser-{run_id}-r{repetition}-{i}"
                            for i in range(args.fanout)
                        ]
                    )
                    branch_seconds = time.perf_counter() - started
                    try:
                        started = time.perf_counter()
                        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
                            results = list(
                                pool.map(
                                    lambda pair: run_smol_action(
                                        pair[0], pair[1], repetition
                                    ),
                                    zip(machines, ids, strict=True),
                                )
                            )
                        action_wall = time.perf_counter() - started
                    finally:
                        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
                            list(pool.map(lambda machine: machine.delete(), machines))
                    print(
                        f"[Smol {repetition}] branch={branch_seconds:.3f}s "
                        f"actions={action_wall:.3f}s",
                        flush=True,
                    )
                    smol_runs.append(
                        {
                            "repetition": repetition,
                            "branch_seconds": branch_seconds,
                            "action_wall_seconds": action_wall,
                            "results": [asdict(result) for result in results],
                        }
                    )
                else:
                    started = time.perf_counter()
                    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
                        results = list(
                            pool.map(
                                lambda branch_id: run_docker_action(
                                    docker_image, branch_id, repetition
                                ),
                                ids,
                            )
                        )
                    wall = time.perf_counter() - started
                    print(
                        f"[Docker {repetition}] start+actions={wall:.3f}s", flush=True
                    )
                    docker_runs.append(
                        {
                            "repetition": repetition,
                            "start_and_action_seconds": wall,
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
        "workload": "stateful Playwright Chromium worker",
        "image": IMAGE,
        "fanout": args.fanout,
        "parallel": args.parallel,
        "repetitions": args.repetitions,
        "resources_per_environment": {"cpus": CPUS, "memory_mb": MEMORY_MB},
        "checkpoint_prepare_seconds": checkpoint_prepare,
        "checkpoint_prepare_phases": checkpoint_phases,
        "docker_image_prepare_seconds": docker_prepare,
        "docker_image_built": docker_built,
        "smol": {
            "median_branch_seconds": statistics.median(
                run["branch_seconds"] for run in smol_runs
            ),
            "median_action_wall_seconds": statistics.median(
                run["action_wall_seconds"] for run in smol_runs
            ),
            "action_latency_p50_seconds": statistics.median(
                result["duration_seconds"] for result in smol_results
            ),
            "action_latency_p99_seconds": percentile(
                [result["duration_seconds"] for result in smol_results], 0.99
            ),
            "worker_action_p50_seconds": numeric_median(
                smol_results, "worker_action_seconds"
            ),
            "correct": sum(result["correct"] for result in smol_results),
            "screenshots_unique": screenshots_are_unique(smol_results),
            "runs": smol_runs,
            "results": smol_results,
        },
        "docker": {
            "median_start_and_action_seconds": statistics.median(
                run["start_and_action_seconds"] for run in docker_runs
            ),
            "action_latency_p50_seconds": statistics.median(
                result["duration_seconds"] for result in docker_results
            ),
            "action_latency_p99_seconds": percentile(
                [result["duration_seconds"] for result in docker_results], 0.99
            ),
            "browser_launch_p50_seconds": numeric_median(
                docker_results, "browser_launch_seconds"
            ),
            "worker_action_p50_seconds": numeric_median(
                docker_results, "worker_action_seconds"
            ),
            "correct": sum(result["correct"] for result in docker_results),
            "screenshots_unique": screenshots_are_unique(docker_results),
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
            "docker": command_version(["docker", "--version"]),
            "playwright": PLAYWRIGHT_VERSION,
        },
    }
    render_report(payload, output.with_suffix(".html"))
    output.write_text(json.dumps(without_screenshots(payload), indent=2) + "\n")
    expected = args.fanout * args.repetitions
    print(f"Wrote {output} and {output.with_suffix('.html')}", flush=True)
    correct = payload["smol"]["correct"] == payload["docker"]["correct"] == expected
    unique = (
        payload["smol"]["screenshots_unique"]
        and payload["docker"]["screenshots_unique"]
    )
    return 0 if correct and unique else 1


if __name__ == "__main__":
    raise SystemExit(main())
