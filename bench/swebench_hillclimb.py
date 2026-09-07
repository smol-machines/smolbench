#!/usr/bin/env python3
"""Evaluate competing fixes for one real SWE-bench issue from a shared state."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from smol import ExecOptions, Machine, MachineConfig, ResourceSpec

try:
    from bench.swebench_verified import (
        DATASET,
        DEFAULT_TASK,
        PINNED_IMAGES,
        dockerfile_base,
        tree_digest,
    )
except ModuleNotFoundError:
    from swebench_verified import (  # type: ignore[no-redef]
        DATASET,
        DEFAULT_TASK,
        PINNED_IMAGES,
        dockerfile_base,
        tree_digest,
    )


CPUS = 1
MEMORY_MB = 4096
STORAGE_GB = 12
REWARD_MARKER = "__SMOL_REWARD__="


@dataclass(frozen=True)
class Candidate:
    name: str
    title: str
    expected_reward: int
    script: str


@dataclass
class CandidateResult:
    runtime: str
    repetition: int
    candidate: str
    title: str
    expected_reward: int
    observed_reward: int | None
    candidate_exit_code: int | None
    verifier_exit_code: int | None
    duration_seconds: float
    correct: bool
    output: str
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


def patch_script(patch: str) -> str:
    return f"""#!/bin/bash
set -euo pipefail
cat > /tmp/candidate.patch <<'PATCH'
{patch.rstrip()}
PATCH
cd /testbed
git apply --check /tmp/candidate.patch
git apply /tmp/candidate.patch
"""


def candidates(source: Path) -> list[Candidate]:
    oracle = (source / "solution" / "solve.sh").read_text()
    suggested = r"""diff --git a/django/utils/dateparse.py b/django/utils/dateparse.py
--- a/django/utils/dateparse.py
+++ b/django/utils/dateparse.py
@@ -32,6 +32,6 @@ standard_duration_re = re.compile(
     r'^'
     r'(?:(?P<days>-?\d+) (days?, )?)?'
-    r'((?:(?P<hours>-?\d+):)(?=\d+:\d+))?'
+    r'((?:(?P<hours>-?\d+):)(?=-?\d+:-?\d+))?'
     r'(?:(?P<minutes>-?\d+):)?'
     r'(?P<seconds>-?\d+)'
     r'(?:\.(?P<microseconds>\d{1,6})\d{0,6})?'
"""
    seconds_only = r"""diff --git a/django/utils/dateparse.py b/django/utils/dateparse.py
--- a/django/utils/dateparse.py
+++ b/django/utils/dateparse.py
@@ -34,7 +34,7 @@ standard_duration_re = re.compile(
     r'(?:(?P<days>-?\d+) (days?, )?)?'
     r'((?:(?P<hours>-?\d+):)(?=\d+:\d+))?'
     r'(?:(?P<minutes>-?\d+):)?'
-    r'(?P<seconds>-?\d+)'
+    r'(?P<seconds>\d+)'
     r'(?:\.(?P<microseconds>\d{1,6})\d{0,6})?'
     r'$'
 )
"""
    return [
        Candidate("oracle", "Official resolved patch", 1, oracle),
        Candidate(
            "issue-suggestion",
            "Original issue suggestion",
            0,
            patch_script(suggested),
        ),
        Candidate(
            "seconds-only",
            "Make only seconds unsigned",
            0,
            patch_script(seconds_only),
        ),
        Candidate(
            "no-change", "Leave the base revision unchanged", 0, "#!/bin/sh\nexit 0\n"
        ),
    ]


def ensure_source(cache_dir: Path) -> Path:
    source = cache_dir.resolve() / "swe-bench-verified" / DEFAULT_TASK
    if (source / "task.toml").is_file():
        return source
    harbor = shutil.which("harbor")
    if harbor is None:
        raise RuntimeError("Harbor is not installed; run `uv sync --extra dev`")
    cache_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            harbor,
            "dataset",
            "download",
            DATASET,
            "--output-dir",
            str(cache_dir.resolve()),
        ],
        check=True,
    )
    if not (source / "task.toml").is_file():
        raise RuntimeError(f"{DEFAULT_TASK!r} was not found in {DATASET}")
    return source


def pinned_image(source: Path) -> str:
    observed = dockerfile_base(source / "environment" / "Dockerfile")
    expected, digest = PINNED_IMAGES[DEFAULT_TASK]
    if observed != expected:
        raise RuntimeError(f"base image changed from {expected} to {observed}")
    return f"{expected}@{digest}"


def checked_exec(machine: Machine, command: str, timeout: int = 1800) -> None:
    result = machine.exec(
        ["/bin/bash", "-lc", command], ExecOptions(timeout=timeout, workdir="/")
    )
    if result.exit_code:
        raise RuntimeError(
            f"SWE-bench preparation failed ({result.exit_code})\n"
            f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"
        )


def install_inputs(machine: Machine, source: Path, options: list[Candidate]) -> None:
    checked_exec(machine, "mkdir -p /tests /candidates /logs/verifier")
    machine.write_file(
        "/tests/test.sh", (source / "tests" / "test.sh").read_bytes(), mode=0o755
    )
    machine.write_file(
        "/tests/config.json", (source / "tests" / "config.json").read_bytes()
    )
    for candidate in options:
        machine.write_file(
            f"/candidates/{candidate.name}.sh", candidate.script, mode=0o755
        )


PREPARE = r"""
set -euo pipefail
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh
fi
export PATH="/root/.local/bin:$PATH"
sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen
locale-gen >/dev/null
cd /testbed
source /opt/miniconda3/bin/activate
conda activate testbed
python -m pip install -e . >/dev/null
set +e
/tests/test.sh >/tmp/smol-swebench-prewarm.log 2>&1
set -e
rm -rf /logs/verifier
mkdir -p /logs/verifier
git reset --hard 36300ef336e3f130a0dadc1143163ff3d23dc843 >/dev/null
git clean -fd >/dev/null
"""


RUN_CANDIDATE = r"""
set +e
rm -rf /logs/verifier
mkdir -p /logs/verifier
/candidates/$CANDIDATE.sh
candidate_rc=$?
/tests/test.sh >/tmp/candidate-verifier.log 2>&1
verifier_rc=$?
reward=$(cat /logs/verifier/reward.txt 2>/dev/null || printf missing)
printf '\n__SMOL_REWARD__=%s\n' "$reward"
tail -n 80 /tmp/candidate-verifier.log
printf '\n__SMOL_CANDIDATE_RC__=%s __SMOL_VERIFIER_RC__=%s\n' "$candidate_rc" "$verifier_rc"
exit 0
"""


def prepare_checkpoint(
    name: str, image: str, source: Path, options: list[Candidate]
) -> tuple[Machine, float]:
    started = time.perf_counter()
    machine = Machine.create(
        MachineConfig(
            name=name,
            image=image,
            resources=ResourceSpec(
                cpus=CPUS,
                memory_mb=MEMORY_MB,
                storage_gb=STORAGE_GB,
                network=True,
            ),
            persistent=True,
            checkpoint=True,
        )
    )
    try:
        install_inputs(machine, source, options)
        checked_exec(machine, PREPARE)
    except BaseException:
        machine.delete()
        raise
    return machine, time.perf_counter() - started


def docker_definition(image: str) -> str:
    return f"""FROM {image}
COPY tests /tests
COPY candidates /candidates
COPY prepare.sh /usr/local/bin/smol-prepare-swebench
RUN chmod +x /tests/test.sh /candidates/*.sh
RUN /bin/bash /usr/local/bin/smol-prepare-swebench
"""


def prepare_docker_image(
    image: str, source: Path, options: list[Candidate]
) -> tuple[str, float, bool]:
    if shutil.which("docker") is None:
        raise RuntimeError("Docker is required for the SWE-bench control")
    if subprocess.run(
        ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode:
        raise RuntimeError("`docker info` failed")
    definition = docker_definition(image)
    content = hashlib.sha256()
    content.update(definition.encode())
    content.update((source / "tests" / "test.sh").read_bytes())
    content.update((source / "tests" / "config.json").read_bytes())
    for candidate in options:
        content.update(candidate.name.encode())
        content.update(candidate.script.encode())
    target = f"smol-bench/swebench-hillclimb:{content.hexdigest()[:16]}"
    if (
        subprocess.run(
            ["docker", "image", "inspect", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    ):
        return target, 0.0, False

    with tempfile.TemporaryDirectory(prefix="swebench-hillclimb-") as temporary:
        context = Path(temporary)
        shutil.copytree(source / "tests", context / "tests")
        (context / "candidates").mkdir()
        for candidate in options:
            (context / "candidates" / f"{candidate.name}.sh").write_text(
                candidate.script
            )
        (context / "prepare.sh").write_text(PREPARE)
        started = time.perf_counter()
        result = subprocess.run(
            ["docker", "build", "--file", "-", "--tag", target, "."],
            cwd=context,
            input=definition,
            text=True,
            capture_output=True,
            timeout=3600,
        )
    duration = time.perf_counter() - started
    if result.returncode:
        raise RuntimeError(
            f"SWE-bench Docker image failed to build ({result.returncode})\n"
            f"stdout:\n{result.stdout[-6000:]}\nstderr:\n{result.stderr[-6000:]}"
        )
    return target, duration, True


def parse_reward(output: str) -> int | None:
    match = re.search(rf"(?m)^{re.escape(REWARD_MARKER)}([01])$", output)
    return int(match.group(1)) if match else None


def parse_exit_codes(output: str) -> tuple[int | None, int | None]:
    match = re.search(
        r"(?m)^__SMOL_CANDIDATE_RC__=(\d+) __SMOL_VERIFIER_RC__=(\d+)$", output
    )
    return (int(match.group(1)), int(match.group(2))) if match else (None, None)


def result_from_process(
    *,
    runtime: str,
    repetition: int,
    candidate: Candidate,
    duration: float,
    return_code: int,
    stdout: str,
    stderr: str,
) -> CandidateResult:
    reward = parse_reward(stdout)
    candidate_rc, verifier_rc = parse_exit_codes(stdout)
    expected_verifier_rc = 0 if candidate.expected_reward else 1
    correct = (
        return_code == 0
        and candidate_rc == 0
        and verifier_rc == expected_verifier_rc
        and reward == candidate.expected_reward
        and "SWEBench results starts here" in stdout
        and "SWEBench results ends here" in stdout
    )
    return CandidateResult(
        runtime=runtime,
        repetition=repetition,
        candidate=candidate.name,
        title=candidate.title,
        expected_reward=candidate.expected_reward,
        observed_reward=reward,
        candidate_exit_code=candidate_rc,
        verifier_exit_code=verifier_rc,
        duration_seconds=duration,
        correct=correct,
        output=stdout[-5000:],
        error=None if correct else (stderr[-3000:] or stdout[-3000:]),
    )


def run_smol_candidate(
    machine: Machine, candidate: Candidate, repetition: int
) -> CandidateResult:
    started = time.perf_counter()
    try:
        result = machine.exec(
            ["/bin/bash", "-lc", RUN_CANDIDATE],
            ExecOptions(env={"CANDIDATE": candidate.name}, timeout=600, workdir="/"),
        )
    except Exception as error:
        return failed_result("smol-branch", repetition, candidate, started, error)
    return result_from_process(
        runtime="smol-branch",
        repetition=repetition,
        candidate=candidate,
        duration=time.perf_counter() - started,
        return_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def run_docker_candidate(
    image: str, candidate: Candidate, repetition: int
) -> CandidateResult:
    started = time.perf_counter()
    container = f"smol-swebench-{uuid.uuid4().hex[:12]}"
    try:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                container,
                "--network",
                "bridge",
                "--cpus",
                str(CPUS),
                "--memory",
                f"{MEMORY_MB}m",
                "--env",
                f"CANDIDATE={candidate.name}",
                image,
                "/bin/bash",
                "-lc",
                RUN_CANDIDATE,
            ],
            text=True,
            capture_output=True,
            timeout=600,
        )
    except Exception as error:
        subprocess.run(
            ["docker", "rm", "--force", container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return failed_result("docker", repetition, candidate, started, error)
    return result_from_process(
        runtime="docker",
        repetition=repetition,
        candidate=candidate,
        duration=time.perf_counter() - started,
        return_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def failed_result(
    runtime: str,
    repetition: int,
    candidate: Candidate,
    started: float,
    error: Exception,
) -> CandidateResult:
    return CandidateResult(
        runtime=runtime,
        repetition=repetition,
        candidate=candidate.name,
        title=candidate.title,
        expected_reward=candidate.expected_reward,
        observed_reward=None,
        candidate_exit_code=None,
        verifier_exit_code=None,
        duration_seconds=time.perf_counter() - started,
        correct=False,
        output="",
        error=f"{type(error).__name__}: {error}",
    )


def render(payload: dict[str, object], output: Path) -> None:
    last = payload["repetitions"]
    results = [
        item for item in payload["smol"]["results"] if item["repetition"] == last
    ]
    cards = "".join(
        f'<article class="{("pass" if item["observed_reward"] else "fail")}">'
        f'<div class="status">{("PASS" if item["observed_reward"] else "FAIL")}</div>'
        f"<h2>{html.escape(item['title'])}</h2><code>{html.escape(item['candidate'])}</code>"
        f"<p>{item['duration_seconds']:.2f} seconds</p></article>"
        for item in results
    )
    smol = payload["smol"]
    docker = payload["docker"]
    output.write_text(
        f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Branch and hill-climb SWE-bench</title><style>
body{{font-family:system-ui,sans-serif;max-width:1200px;margin:50px auto;padding:0 24px;background:#0b1020;color:#f8fafc}}h1{{font-size:50px;margin-bottom:8px}}.sub{{color:#cbd5e1;font-size:20px}}.metrics,.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin:28px 0}}.metric,article{{padding:20px;background:#172033;border:1px solid #334155;border-radius:16px}}strong{{display:block;font-size:34px;color:#ff5c35}}.status{{font-weight:800;font-size:24px}}.pass .status{{color:#4ade80}}.fail .status{{color:#f87171}}code{{color:#fbbf24}}</style></head><body>
<h1>One repository. Four candidate fixes.</h1><p class="sub">Branch the prepared Django SWE-bench environment, evaluate every candidate independently, and retain the winner.</p>
<div class="metrics"><div class="metric"><strong>{smol["median_branch_seconds"] * 1000:.0f} ms</strong>four-way branch</div><div class="metric"><strong>{smol["median_score_wall_seconds"]:.2f} s</strong>Smol candidates scored</div><div class="metric"><strong>{docker["median_score_wall_seconds"]:.2f} s</strong>Docker candidates scored</div><div class="metric"><strong>{smol["correct"]}/{len(smol["results"])}</strong>Smol outcomes exact</div></div>
<div class="grid">{cards}</div><p>Task: {html.escape(payload["task"])} · official SWE-bench verifier · pinned task tree and image digest.</p></body></html>"""
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/swebench"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("repetitions must be positive")

    source = ensure_source(args.cache_dir)
    image = pinned_image(source)
    options = candidates(source)
    digest = tree_digest(source)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_id = f"{stamp}-{uuid.uuid4().hex[:6]}"
    output = args.output or Path("results") / f"{stamp}-swebench-hillclimb.json"
    output.parent.mkdir(parents=True, exist_ok=True)

    docker_image, docker_prepare, docker_built = prepare_docker_image(
        image, source, options
    )
    golden, smol_prepare = prepare_checkpoint(
        f"swe-hillclimb-golden-{run_id}", image, source, options
    )
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
                            f"swe-hillclimb-{run_id}-r{repetition}-{item.name}"
                            for item in options
                        ]
                    )
                    branch_seconds = time.perf_counter() - started
                    try:
                        started = time.perf_counter()
                        with ThreadPoolExecutor(max_workers=len(options)) as pool:
                            results = list(
                                pool.map(
                                    lambda pair: run_smol_candidate(
                                        pair[0], pair[1], repetition
                                    ),
                                    zip(machines, options, strict=True),
                                )
                            )
                        score_seconds = time.perf_counter() - started
                    finally:
                        with ThreadPoolExecutor(max_workers=len(machines)) as pool:
                            list(pool.map(lambda machine: machine.delete(), machines))
                    print(
                        f"[Smol {repetition}] branch={branch_seconds:.3f}s "
                        f"score={score_seconds:.3f}s",
                        flush=True,
                    )
                    smol_runs.append(
                        {
                            "repetition": repetition,
                            "branch_seconds": branch_seconds,
                            "score_wall_seconds": score_seconds,
                            "results": [asdict(item) for item in results],
                        }
                    )
                else:
                    started = time.perf_counter()
                    with ThreadPoolExecutor(max_workers=len(options)) as pool:
                        results = list(
                            pool.map(
                                lambda item: run_docker_candidate(
                                    docker_image, item, repetition
                                ),
                                options,
                            )
                        )
                    score_seconds = time.perf_counter() - started
                    print(
                        f"[Docker {repetition}] score={score_seconds:.3f}s", flush=True
                    )
                    docker_runs.append(
                        {
                            "repetition": repetition,
                            "score_wall_seconds": score_seconds,
                            "results": [asdict(item) for item in results],
                        }
                    )
    finally:
        golden.delete()

    smol_results = [item for run in smol_runs for item in run["results"]]
    docker_results = [item for run in docker_runs for item in run["results"]]
    payload = {
        "schema_version": 1,
        "validated_at": datetime.now(UTC).isoformat(),
        "dataset": DATASET,
        "task": DEFAULT_TASK,
        "task_tree_sha256": digest,
        "image": image,
        "repetitions": args.repetitions,
        "resources_per_environment": {"cpus": CPUS, "memory_mb": MEMORY_MB},
        "smol_prepare_seconds": smol_prepare,
        "docker_prepare_seconds": docker_prepare,
        "docker_image_built": docker_built,
        "candidates": [
            {
                "name": item.name,
                "title": item.title,
                "expected_reward": item.expected_reward,
                "script_sha256": hashlib.sha256(item.script.encode()).hexdigest(),
            }
            for item in options
        ],
        "smol": {
            "median_branch_seconds": statistics.median(
                run["branch_seconds"] for run in smol_runs
            ),
            "median_score_wall_seconds": statistics.median(
                run["score_wall_seconds"] for run in smol_runs
            ),
            "correct": sum(item["correct"] for item in smol_results),
            "runs": smol_runs,
            "results": smol_results,
        },
        "docker": {
            "median_score_wall_seconds": statistics.median(
                run["score_wall_seconds"] for run in docker_runs
            ),
            "correct": sum(item["correct"] for item in docker_results),
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
            "harbor": package_version("harbor"),
            "smolmachines": package_version("smolmachines"),
            "smolvm": command_version(["smolvm", "--version"]),
            "docker": command_version(["docker", "--version"]),
        },
    }
    output.write_text(json.dumps(payload, indent=2) + "\n")
    render(payload, output.with_suffix(".html"))
    expected = len(options) * args.repetitions
    print(f"Wrote {output} and {output.with_suffix('.html')}", flush=True)
    return (
        0
        if payload["smol"]["correct"] == payload["docker"]["correct"] == expected
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
