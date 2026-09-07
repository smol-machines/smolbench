#!/usr/bin/env python3
"""Branch an initialized BrowserGym task and compare fresh prepared containers."""

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


BROWSERGYM_VERSION = "0.14.3"
PLAYWRIGHT_VERSION = "1.44.0"
MINIWOB_REVISION = "7fd85d71a4b60325c6585396ec4f48377d049838"
MINIWOB_REPOSITORY = "https://github.com/Farama-Foundation/miniwob-plusplus.git"
IMAGE = (
    "mcr.microsoft.com/playwright/python:v1.44.0-jammy@"
    "sha256:c257db7f706f31f852740b039dea4f4611418f1a6816c0ceae716dd20d4eddda"
)
CPUS = 2
MEMORY_MB = 2048
PORT = 8766
ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).resolve().parent / "workloads" / "browsergym_worker.py"
CLIENT = r"""
import json, os, time, urllib.parse, urllib.request
base = "http://127.0.0.1:8766"
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
    raise RuntimeError("BrowserGym worker did not become ready")
health_seconds = time.perf_counter() - started
query = urllib.parse.urlencode({"label": os.environ["LABEL"], "action": os.environ["ACTION"]})
started = time.perf_counter()
with urllib.request.urlopen(base + "/action?" + query, timeout=40) as response:
    payload = json.load(response)
payload["health"] = health
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
    raise RuntimeError("BrowserGym worker did not become ready")
"""


@dataclass(frozen=True)
class Candidate:
    label: str
    title: str
    action: str
    reward: float
    terminated: bool


@dataclass
class ActionResult:
    runtime: str
    repetition: int
    label: str
    title: str
    action: str
    duration_seconds: float
    correct: bool
    reward: float | None
    terminated: bool | None
    action_count_before: int | None
    action_count_after: int | None
    initial_screenshot_sha256: str | None
    screenshot_sha256: str | None
    screenshot_base64: str | None
    worker_action_seconds: float | None
    browsergym_launch_seconds: float | None
    client_health_seconds: float | None
    client_action_seconds: float | None
    error: str | None


def candidates() -> list[Candidate]:
    return [
        Candidate(
            "correct", "Click the target button", "click('{target_bid}')", 1.0, True
        ),
        Candidate("noop", "Do nothing", "noop()", 0.0, False),
        Candidate("scroll", "Scroll instead", "scroll(0, 200)", 0.0, False),
        Candidate("wrong", "Click the task panel", "click('10')", 0.0, False),
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
    values = [value for item in results if isinstance((value := item.get(key)), float)]
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


def checked_exec(machine: Machine, command: str, timeout: int = 900) -> str:
    result = machine.exec(
        ["/bin/bash", "-lc", command], ExecOptions(timeout=timeout, workdir="/")
    )
    if result.exit_code:
        raise RuntimeError(
            f"BrowserGym preparation failed ({result.exit_code})\n"
            f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"
        )
    return result.stdout


def read_health(machine: Machine, timeout: int = 120) -> dict[str, object]:
    output = checked_exec(machine, f"python - <<'PY'\n{HEALTH_CLIENT}\nPY", timeout)
    return json.loads(output.strip().splitlines()[-1])


def prepare_checkpoint(name: str) -> tuple[Machine, float, dict[str, object]]:
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
            "set -euo pipefail; "
            f"python -m pip install --disable-pip-version-check browsergym-miniwob=={BROWSERGYM_VERSION}; "
            "rm -rf /opt/miniwob-plusplus; "
            f"git clone --quiet {MINIWOB_REPOSITORY} /opt/miniwob-plusplus; "
            f"git -C /opt/miniwob-plusplus checkout --quiet {MINIWOB_REVISION}; "
            "mkdir -p /opt/smol-browsergym",
        )
        machine.write_file("/opt/smol-browsergym/worker.py", WORKER.read_bytes())
        checked_exec(
            machine,
            "export MINIWOB_URL=file:///opt/miniwob-plusplus/miniwob/html/miniwob/; "
            "nohup python /opt/smol-browsergym/worker.py "
            "</dev/null >/tmp/smol-browsergym.log 2>&1 &",
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
            "application_prepare_seconds": ready - created,
            "health": health,
        },
    )


def prepare_docker_image() -> tuple[str, float, bool]:
    if shutil.which("docker") is None:
        raise RuntimeError("Docker is required for the BrowserGym control")
    if subprocess.run(
        ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode:
        raise RuntimeError("`docker info` failed")
    worker_hash = hashlib.sha256(WORKER.read_bytes()).hexdigest()[:12]
    image = f"smol-bench/browsergym:{BROWSERGYM_VERSION}-{worker_hash}"
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
RUN python -m pip install --disable-pip-version-check browsergym-miniwob=={BROWSERGYM_VERSION} \\
 && git clone --quiet {MINIWOB_REPOSITORY} /opt/miniwob-plusplus \\
 && git -C /opt/miniwob-plusplus checkout --quiet {MINIWOB_REVISION}
ENV MINIWOB_URL=file:///opt/miniwob-plusplus/miniwob/html/miniwob/
COPY bench/workloads/browsergym_worker.py /opt/smol-browsergym/worker.py
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
            f"BrowserGym Docker image failed to build ({result.returncode})\n"
            f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"
        )
    return image, duration, True


def failed_action(
    runtime: str, repetition: int, candidate: Candidate, duration: float, error: object
) -> ActionResult:
    return ActionResult(
        runtime,
        repetition,
        candidate.label,
        candidate.title,
        candidate.action,
        duration,
        False,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        str(error)[-2000:],
    )


def parse_action(
    *,
    runtime: str,
    repetition: int,
    candidate: Candidate,
    action: str,
    duration: float,
    return_code: int,
    stdout: str,
    stderr: str,
    expected_initial_sha256: str,
) -> ActionResult:
    try:
        payload = json.loads(stdout.strip())
    except (json.JSONDecodeError, TypeError):
        payload = {}
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
    initial_sha256 = payload.get("initial_screenshot_sha256")
    health = payload.get("health", {})
    health_initial = (
        health.get("initial_screenshot_sha256") if isinstance(health, dict) else None
    )
    correct = (
        return_code == 0
        and payload.get("label") == candidate.label
        and payload.get("action") == action
        and payload.get("reward") == candidate.reward
        and payload.get("terminated") is candidate.terminated
        and payload.get("truncated") is False
        and payload.get("last_action_error") == ""
        and payload.get("action_count_before") == 0
        and payload.get("action_count_after") == 1
        and initial_sha256 == expected_initial_sha256
        and (runtime == "docker" or health_initial == expected_initial_sha256)
        and screenshot_valid
    )
    return ActionResult(
        runtime=runtime,
        repetition=repetition,
        label=candidate.label,
        title=candidate.title,
        action=action,
        duration_seconds=duration,
        correct=correct,
        reward=payload.get("reward"),
        terminated=payload.get("terminated"),
        action_count_before=payload.get("action_count_before"),
        action_count_after=payload.get("action_count_after"),
        initial_screenshot_sha256=initial_sha256,
        screenshot_sha256=screenshot_sha256,
        screenshot_base64=screenshot,
        worker_action_seconds=payload.get("worker_action_seconds"),
        browsergym_launch_seconds=payload.get("browsergym_launch_seconds"),
        client_health_seconds=payload.get("client_health_seconds"),
        client_action_seconds=payload.get("client_action_seconds"),
        error=None if correct else (stderr[-2000:] or stdout[-2000:]),
    )


def run_smol_action(
    machine: Machine,
    candidate: Candidate,
    repetition: int,
    target_bid: str,
    initial_sha256: str,
) -> ActionResult:
    action = candidate.action.replace("{target_bid}", target_bid)
    started = time.perf_counter()
    try:
        result = machine.exec(
            ["python", "-c", CLIENT],
            ExecOptions(
                env={"LABEL": candidate.label, "ACTION": action},
                timeout=60,
                workdir="/",
            ),
        )
    except Exception as error:
        return failed_action(
            "smol-branch", repetition, candidate, time.perf_counter() - started, error
        )
    return parse_action(
        runtime="smol-branch",
        repetition=repetition,
        candidate=candidate,
        action=action,
        duration=time.perf_counter() - started,
        return_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        expected_initial_sha256=initial_sha256,
    )


def run_docker_action(
    image: str,
    candidate: Candidate,
    repetition: int,
    target_bid: str,
    initial_sha256: str,
) -> ActionResult:
    started = time.perf_counter()
    container = f"smol-browsergym-{uuid.uuid4().hex[:12]}"
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
                "/opt/smol-browsergym/worker.py",
                "--once",
                candidate.label,
                candidate.action,
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
            "docker", repetition, candidate, time.perf_counter() - started, error
        )
    action = candidate.action.replace("{target_bid}", target_bid)
    return parse_action(
        runtime="docker",
        repetition=repetition,
        candidate=candidate,
        action=action,
        duration=time.perf_counter() - started,
        return_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        expected_initial_sha256=initial_sha256,
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
        f'alt="{html.escape(result["title"])}"><figcaption><b>'
        f"{html.escape(result['title'])}</b><br><code>{html.escape(result['action'])}</code>"
        f"<br>reward={result['reward']}</figcaption></figure>"
        for result in screenshots
    )
    smol_total = smol["median_branch_seconds"] + smol["median_action_wall_seconds"]
    ratio = docker["median_start_and_action_seconds"] / smol_total
    comparison = f"{ratio:.2f}× faster" if ratio >= 1 else f"{1 / ratio:.2f}× slower"
    output.write_text(
        f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BrowserGym branch search</title><style>
body{{font-family:system-ui,sans-serif;max-width:1200px;margin:50px auto;padding:0 24px;background:#0b1020;color:#f9fafb}}h1{{font-size:50px;margin-bottom:8px}}p{{color:#cbd5e1}}.metrics,.shots{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin:28px 0}}.metric,figure{{background:#172033;border:1px solid #334155;border-radius:16px;padding:18px;margin:0}}strong{{display:block;font-size:34px;color:#ff5c35}}img{{width:100%;border-radius:8px}}figcaption{{padding-top:10px}}</style></head><body>
<h1>Branch a real browser benchmark.</h1><p>One initialized BrowserGym MiniWoB task becomes {payload["fanout"]} isolated candidate futures. Each branch starts from the exact same live Chromium state and evaluates a different ordinary BrowserGym action.</p>
<div class="metrics"><div class="metric"><strong>{smol["median_branch_seconds"] * 1000:.0f} ms</strong>median {payload["fanout"]}-way branch</div><div class="metric"><strong>{smol["median_action_wall_seconds"]:.2f} s</strong>all branch actions</div><div class="metric"><strong>{docker["median_start_and_action_seconds"]:.2f} s</strong>fresh prepared containers</div><div class="metric"><strong>{comparison}</strong>Smol end to end</div></div>
<div class="shots">{cards}</div><p>{smol["correct"]}/{len(smol["results"])} Smol and {docker["correct"]}/{len(docker["results"])} Docker candidates matched their expected outcomes. The successful action is selected by reward.</p></body></html>"""
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
    output = args.output or Path("results") / f"{label}-browsergym-fanout.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    print("[Prepare] Building or reusing the pinned Docker control", flush=True)
    docker_image, docker_prepare, docker_built = prepare_docker_image()
    print("[Prepare] Starting and initializing the BrowserGym golden", flush=True)
    golden, checkpoint_prepare, checkpoint_phases = prepare_checkpoint(
        f"browsergym-golden-{run_id}"
    )
    health = checkpoint_phases["health"]
    target = health["target_bid"]
    initial_sha256 = health["initial_screenshot_sha256"]
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
                            f"browsergym-{run_id}-r{repetition}-{candidate.label}"
                            for candidate in options
                        ]
                    )
                    branch_seconds = time.perf_counter() - started
                    try:
                        started = time.perf_counter()
                        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
                            results = list(
                                pool.map(
                                    lambda pair: run_smol_action(
                                        pair[0],
                                        pair[1],
                                        repetition,
                                        target,
                                        initial_sha256,
                                    ),
                                    zip(machines, options, strict=True),
                                )
                            )
                        action_wall = time.perf_counter() - started
                    finally:
                        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
                            list(pool.map(lambda machine: machine.delete(), machines))
                    source_health = read_health(golden, 15)
                    golden_unchanged = (
                        source_health["action_count"] == 0
                        and source_health["initial_screenshot_sha256"] == initial_sha256
                    )
                    print(
                        f"[Smol {repetition}] branch={branch_seconds:.3f}s "
                        f"actions={action_wall:.3f}s source_unchanged={golden_unchanged}",
                        flush=True,
                    )
                    smol_runs.append(
                        {
                            "repetition": repetition,
                            "branch_seconds": branch_seconds,
                            "action_wall_seconds": action_wall,
                            "golden_unchanged": golden_unchanged,
                            "results": [asdict(result) for result in results],
                        }
                    )
                else:
                    started = time.perf_counter()
                    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
                        results = list(
                            pool.map(
                                lambda candidate: run_docker_action(
                                    docker_image,
                                    candidate,
                                    repetition,
                                    target,
                                    initial_sha256,
                                ),
                                options,
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
        "workload": "BrowserGym MiniWoB click-test",
        "environment": "browsergym/miniwob.click-test",
        "seed": 123,
        "image": IMAGE,
        "browsergym_version": BROWSERGYM_VERSION,
        "miniwob_revision": MINIWOB_REVISION,
        "fanout": len(options),
        "parallel": args.parallel,
        "repetitions": args.repetitions,
        "resources_per_environment": {"cpus": CPUS, "memory_mb": MEMORY_MB},
        "checkpoint_prepare_seconds": checkpoint_prepare,
        "checkpoint_prepare_phases": checkpoint_phases,
        "docker_image_prepare_seconds": docker_prepare,
        "docker_image_built": docker_built,
        "initial_screenshot_sha256": initial_sha256,
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
            "golden_unchanged": all(run["golden_unchanged"] for run in smol_runs),
            "correct": sum(result["correct"] for result in smol_results),
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
            "browsergym_launch_p50_seconds": numeric_median(
                docker_results, "browsergym_launch_seconds"
            ),
            "worker_action_p50_seconds": numeric_median(
                docker_results, "worker_action_seconds"
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
            "playwright": PLAYWRIGHT_VERSION,
        },
    }
    render_report(payload, output.with_suffix(".html"))
    output.write_text(json.dumps(without_screenshots(payload), indent=2) + "\n")
    expected = len(options) * args.repetitions
    print(f"Wrote {output} and {output.with_suffix('.html')}", flush=True)
    correct = payload["smol"]["correct"] == payload["docker"]["correct"] == expected
    return 0 if correct and payload["smol"]["golden_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
