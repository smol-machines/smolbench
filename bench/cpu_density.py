#!/usr/bin/env python3
"""Compare one initialized Smol branch source with matched native containers."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
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


ROOT = Path(__file__).parents[1]
WORKLOADS = ROOT / "bench" / "workloads"
CONTAINERFILE = WORKLOADS / "Containerfile.cpu-parity"
WORKER = WORKLOADS / "cpu_parity_worker.py"


def command(
    arguments: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 300,
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
        rendered = " ".join(arguments)
        raise RuntimeError(
            f"command failed ({result.returncode}): {rendered}\n"
            f"stdout:\n{result.stdout[-4000:]}\n"
            f"stderr:\n{result.stderr[-4000:]}"
        )
    return result


def available_memory_bytes() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (FileNotFoundError, OSError, ValueError):
        return None
    return None


def total_memory_bytes() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (FileNotFoundError, OSError, ValueError):
        return None
    return None


def cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith(("model name", "Hardware")):
                return line.partition(":")[2].strip()
    except (FileNotFoundError, OSError):
        pass
    return platform.processor() or "unknown"


def stable_available_memory_bytes(samples: int = 9) -> int | None:
    values = []
    for index in range(samples):
        value = available_memory_bytes()
        if value is not None:
            values.append(value)
        if index + 1 < samples:
            time.sleep(0.05)
    return int(statistics.median(values)) if values else None


def memory_delta(before: int | None, after: int | None) -> int | None:
    if before is None or after is None:
        return None
    return before - after


def ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def process_cpu_percent(pid: int, duration: float) -> float | None:
    stat = Path(f"/proc/{pid}/stat")
    try:
        ticks_per_second = os.sysconf("SC_CLK_TCK")
        before = stat.read_text().split()
        started = time.monotonic()
        time.sleep(duration)
        after = stat.read_text().split()
    except (FileNotFoundError, OSError, ValueError):
        return None
    elapsed = time.monotonic() - started
    ticks = (int(after[13]) + int(after[14])) - (int(before[13]) + int(before[14]))
    return 100 * ticks / ticks_per_second / elapsed


def wait_for_results(
    directory: Path, fanout: int, timeout: float
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        paths = sorted(directory.glob("cpu-parity-result-*.json"))
        if len(paths) == fanout:
            rows = [json.loads(path.read_text()) for path in paths]
            validate_results(rows, fanout)
            return rows
        time.sleep(0.05)
    raise TimeoutError(
        f"received {len(list(directory.glob('cpu-parity-result-*.json')))} "
        f"of {fanout} workload results"
    )


def validate_results(rows: list[dict[str, Any]], fanout: int) -> None:
    task_ids = sorted(row.get("task_id") for row in rows)
    if task_ids != list(range(fanout)):
        raise ValueError(f"unexpected task IDs: {task_ids}")
    for row in rows:
        digest = row.get("digest")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError(f"task {row.get('task_id')} has an invalid digest")
        try:
            int(digest, 16)
        except ValueError as error:
            raise ValueError(
                f"task {row.get('task_id')} has an invalid digest"
            ) from error
        if not isinstance(row.get("checksum"), int):
            raise ValueError(f"task {row.get('task_id')} has no checksum")
        if not isinstance(row.get("work_ms"), (int, float)):
            raise ValueError(f"task {row.get('task_id')} has no work duration")


def canonical_results(rows: list[dict[str, Any]]) -> dict[int, tuple[str, int]]:
    return {
        int(row["task_id"]): (str(row["digest"]), int(row["checksum"])) for row in rows
    }


def detect_container_runtime(requested: str) -> str:
    candidates = [requested] if requested != "auto" else ["docker", "podman"]
    for candidate in candidates:
        executable = shutil.which(candidate)
        if executable is None:
            continue
        probe = command([executable, "info"], check=False, timeout=30)
        if probe.returncode == 0:
            return executable
    raise RuntimeError(
        f"container runtime {requested!r} is unavailable; install Docker or Podman"
    )


def workload_identity() -> str:
    digest = hashlib.sha256()
    digest.update(CONTAINERFILE.read_bytes())
    digest.update(WORKER.read_bytes())
    return digest.hexdigest()


def prepare_image(runtime: str, directory: Path) -> tuple[str, Path, float]:
    identity = workload_identity()
    image = f"smol-bench/cpu-density:{identity[:12]}"
    started = time.perf_counter()
    command(
        [
            runtime,
            "build",
            "--file",
            str(CONTAINERFILE),
            "--tag",
            image,
            str(WORKLOADS),
        ],
        timeout=900,
    )
    archive = directory / "cpu-density-image.tar"
    command([runtime, "save", "--output", str(archive), image], timeout=300)
    return image, archive, time.perf_counter() - started


def smol_environment(smolvm: str) -> dict[str, str]:
    environment = os.environ.copy()
    executable = Path(smolvm).expanduser()
    if executable.parent != Path("."):
        environment["PATH"] = f"{executable.resolve().parent}:{environment['PATH']}"
    return environment


def smol_source_pid(smolvm: str, source: str, env: dict[str, str]) -> int | None:
    result = command([smolvm, "machine", "ls", "--json"], env=env)
    records = json.loads(result.stdout)
    for record in records:
        if record.get("name") == source and isinstance(record.get("pid"), int):
            return int(record["pid"])
    return None


def wait_for_branchpoint(smolvm: str, source: str, env: dict[str, str]) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
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
            check=False,
            timeout=5,
        )
        if result.returncode == 0:
            return
        time.sleep(0.1)
    raise TimeoutError(f"Smol source {source!r} did not reach its branchpoint")


def run_smol_wave(
    smolvm: str,
    archive: Path,
    result_dir: Path,
    fanout: int,
    parallel: int,
    state_mib: int,
    rounds: int,
    hold_seconds: int,
) -> dict[str, Any]:
    token = uuid.uuid4().hex[:10]
    source = f"cpu-density-{token}"
    child_prefix = f"cpu-density-child-{token}"
    env = smol_environment(smolvm)
    baseline = stable_available_memory_bytes()
    prepared = False
    try:
        started = time.perf_counter()
        branch = command(
            [
                smolvm,
                "machine",
                "create",
                "--name",
                source,
                "--image",
                str(archive),
                "--cpus",
                "1",
                "--mem",
                "1024",
                "--storage",
                "2",
                "--volume",
                f"{result_dir}:/results",
                "--env",
                "BRANCH_MODE=1",
                "--env",
                "RESULT_DIR=/results",
                "--env",
                f"STATE_MIB={state_mib}",
                "--env",
                f"ROUNDS={rounds}",
                "--env",
                f"HOLD_SECONDS={hold_seconds}",
            ],
            env=env,
            timeout=300,
        )
        prepared = True
        command(
            [smolvm, "machine", "start", "--name", source, "--branchable"],
            env=env,
            timeout=120,
        )
        wait_for_branchpoint(smolvm, source, env)
        prepare_seconds = time.perf_counter() - started
        source_memory = stable_available_memory_bytes()
        source_pid = smol_source_pid(smolvm, source, env)
        idle_cpu = (
            process_cpu_percent(source_pid, 2.0) if source_pid is not None else None
        )

        wave_started = time.perf_counter()
        branch_env = env.copy()
        branch_env["RUST_LOG"] = "info"
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
                child_prefix,
                "--parallel",
                str(parallel),
            ],
            env=branch_env,
            timeout=300,
        )
        branch_seconds = time.perf_counter() - wave_started
        capture_window_ms = None
        for line in branch.stderr.splitlines():
            if "fork: golden RAM checkpoint written" not in line:
                continue
            for field in line.split():
                if field.startswith("elapsed_ms="):
                    capture_window_ms = float(field.removeprefix("elapsed_ms="))
                    break
        rows = wait_for_results(result_dir, fanout, 180)
        wave_seconds = time.perf_counter() - wave_started
        active_memory = stable_available_memory_bytes()
        return {
            "provider": "smol-branch",
            "prepare_seconds": prepare_seconds,
            "launch_seconds": branch_seconds,
            "capture_window_ms": capture_window_ms,
            "wave_seconds": wave_seconds,
            "idle_source_cpu_percent": idle_cpu,
            "physical_memory_bytes": memory_delta(baseline, active_memory),
            "source_physical_memory_bytes": memory_delta(baseline, source_memory),
            "incremental_children_memory_bytes": memory_delta(
                source_memory, active_memory
            ),
            "work_ms": [float(row["work_ms"]) for row in rows],
            "results": rows,
        }
    finally:
        if prepared:
            command(
                [smolvm, "machine", "delete", "--name", source, "--cascade"],
                env=env,
                timeout=120,
                check=False,
            )


def run_container_wave(
    runtime: str,
    image: str,
    result_dir: Path,
    fanout: int,
    parallel: int,
    state_mib: int,
    rounds: int,
    hold_seconds: int,
) -> dict[str, Any]:
    token = uuid.uuid4().hex[:10]
    names = [f"cpu-density-{token}-{index}" for index in range(fanout)]
    baseline = stable_available_memory_bytes()

    def launch(index: int) -> None:
        command(
            [
                runtime,
                "run",
                "--detach",
                "--name",
                names[index],
                "--network",
                "none",
                "--cpus",
                "1",
                "--memory",
                "1024m",
                "--volume",
                f"{result_dir}:/results:z",
                "--env",
                "BRANCH_MODE=0",
                "--env",
                "RESULT_DIR=/results",
                "--env",
                f"STATE_MIB={state_mib}",
                "--env",
                f"ROUNDS={rounds}",
                "--env",
                f"HOLD_SECONDS={hold_seconds}",
                "--env",
                f"TASK_ID={index}",
                image,
            ],
            timeout=120,
        )

    try:
        wave_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            list(executor.map(launch, range(fanout)))
        launch_seconds = time.perf_counter() - wave_started
        rows = wait_for_results(result_dir, fanout, 180)
        wave_seconds = time.perf_counter() - wave_started
        active_memory = stable_available_memory_bytes()
        return {
            "provider": Path(runtime).name,
            "prepare_seconds": 0.0,
            "launch_seconds": launch_seconds,
            "capture_window_ms": None,
            "wave_seconds": wave_seconds,
            "idle_source_cpu_percent": None,
            "physical_memory_bytes": memory_delta(baseline, active_memory),
            "source_physical_memory_bytes": None,
            "incremental_children_memory_bytes": memory_delta(baseline, active_memory),
            "work_ms": [float(row["work_ms"]) for row in rows],
            "results": rows,
        }
    finally:
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            list(
                executor.map(
                    lambda name: command(
                        [runtime, "rm", "--force", name],
                        timeout=60,
                        check=False,
                    ),
                    names,
                )
            )


def median(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [float(row[key]) for row in rows if isinstance(row.get(key), (int, float))]
    return statistics.median(values) if values else None


def summarize(raw: list[dict[str, Any]], fanout: int) -> dict[str, Any]:
    providers = sorted({str(row["provider"]) for row in raw})
    points: list[dict[str, Any]] = []
    for provider in providers:
        rows = [row for row in raw if row["provider"] == provider]
        work = [statistics.median(row["work_ms"]) for row in rows]
        physical = median(rows, "physical_memory_bytes")
        incremental = median(rows, "incremental_children_memory_bytes")
        points.append(
            {
                "provider": provider,
                "repetitions": len(rows),
                "median_prepare_seconds": median(rows, "prepare_seconds"),
                "median_launch_seconds": median(rows, "launch_seconds"),
                "median_capture_window_ms": median(rows, "capture_window_ms"),
                "median_wave_seconds": median(rows, "wave_seconds"),
                "median_work_ms": statistics.median(work),
                "median_physical_memory_bytes": physical,
                "median_incremental_memory_per_worker_bytes": (
                    incremental / fanout if incremental is not None else None
                ),
                "median_idle_source_cpu_percent": median(
                    rows, "idle_source_cpu_percent"
                ),
            }
        )
    smol = next(point for point in points if point["provider"] == "smol-branch")
    container = next(point for point in points if point["provider"] != "smol-branch")
    return {
        "points": points,
        "comparisons": {
            "container_to_smol_physical_memory_ratio": ratio(
                container["median_physical_memory_bytes"],
                smol["median_physical_memory_bytes"],
            ),
            "container_to_smol_incremental_worker_memory_ratio": ratio(
                container["median_incremental_memory_per_worker_bytes"],
                smol["median_incremental_memory_per_worker_bytes"],
            ),
            "container_to_smol_wave_speed_ratio": ratio(
                container["median_wave_seconds"], smol["median_wave_seconds"]
            ),
        },
    }


def format_number(value: float | None, suffix: str = "", digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}{suffix}"


def render(report: dict[str, Any]) -> str:
    rows = []
    for point in report["summary"]["points"]:
        memory = point["median_physical_memory_bytes"]
        per_worker = point["median_incremental_memory_per_worker_bytes"]
        rows.append(
            "<tr>"
            f"<th>{html.escape(point['provider'])}</th>"
            f"<td>{format_number(point['median_wave_seconds'], ' s')}</td>"
            f"<td>{format_number(point['median_work_ms'], ' ms', 1)}</td>"
            f"<td>{format_number(memory / 2**20 if memory is not None else None, ' MiB', 1)}</td>"
            f"<td>{format_number(per_worker / 2**20 if per_worker is not None else None, ' MiB', 1)}</td>"
            "</tr>"
        )
    comparison = report["summary"]["comparisons"]
    memory_ratio = format_number(
        comparison["container_to_smol_physical_memory_ratio"], "×"
    )
    incremental_ratio = format_number(
        comparison["container_to_smol_incremental_worker_memory_ratio"], "×"
    )
    wave_ratio = format_number(comparison["container_to_smol_wave_speed_ratio"], "×")
    capture_window = next(
        point["median_capture_window_ms"]
        for point in report["summary"]["points"]
        if point["provider"] == "smol-branch"
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smol branch density control</title><style>
body{{font-family:system-ui,sans-serif;max-width:920px;margin:48px auto;padding:0 24px;color:#172033}}
h1{{font-size:42px}} .cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}
.card{{padding:18px;border:1px solid #d6dae2;border-radius:14px}} strong{{display:block;font-size:32px}}
table{{border-collapse:collapse;width:100%;margin-top:28px}} th,td{{padding:12px;border-bottom:1px solid #ddd;text-align:right}}
th:first-child{{text-align:left}} code{{font-size:0.9em}} @media(max-width:700px){{.cards{{grid-template-columns:1fr}}}}
</style></head><body>
<h1>One initialized process, {report["config"]["fanout"]} isolated branches.</h1>
<p>The exact same Python image runs through Smol branches and native containers. Host <code>MemAvailable</code> is sampled while every worker remains alive.</p>
<div class="cards"><div class="card"><strong>{memory_ratio}</strong>container / Smol host-memory pressure</div>
<div class="card"><strong>{incremental_ratio}</strong>container / Smol incremental worker memory</div>
<div class="card"><strong>{wave_ratio}</strong>container / Smol wave time</div></div>
<p>The source-visible capture-and-resume window was <strong>{format_number(capture_window, " ms", 1)}</strong>; child admission and workload completion are reported separately.</p>
<table><thead><tr><th>Runtime</th><th>Wave</th><th>Worker median</th><th>Host-memory pressure</th><th>Incremental/worker</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
<p>Smol preparation is reported separately in the JSON. Physical-memory deltas are repeated process-external observations and may contain host noise.</p>
</body></html>"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fanout", type=int, default=16)
    parser.add_argument("--parallel", type=int, default=16)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--state-mib", type=int, default=256)
    parser.add_argument("--rounds", type=int, default=128)
    parser.add_argument("--hold-seconds", type=int, default=15)
    parser.add_argument("--smolvm", default=os.environ.get("SMOLVM_BIN", "smolvm"))
    parser.add_argument(
        "--container-runtime",
        choices=("auto", "docker", "podman"),
        default="auto",
    )
    parser.add_argument("--json", type=Path, default=ROOT / "results/cpu-density.json")
    parser.add_argument("--html", type=Path, default=ROOT / "results/cpu-density.html")
    args = parser.parse_args()
    for name in ("fanout", "parallel", "repetitions", "state_mib", "rounds"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.hold_seconds < 5:
        parser.error("--hold-seconds must be at least 5 for memory sampling")
    return args


def main() -> None:
    args = parse_args()
    runtime = detect_container_runtime(args.container_runtime)
    smolvm = shutil.which(args.smolvm) or str(Path(args.smolvm).expanduser())
    if not Path(smolvm).is_file():
        raise RuntimeError(f"smolvm executable not found: {args.smolvm}")
    raw: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="smol-cpu-density-") as temporary:
        temporary_path = Path(temporary)
        image, archive, image_prepare_seconds = prepare_image(runtime, temporary_path)
        for repetition in range(args.repetitions):
            providers = ["smol", "container"]
            if repetition % 2:
                providers.reverse()
            observed: dict[str, dict[int, tuple[str, int]]] = {}
            for provider in providers:
                result_dir = temporary_path / f"results-{repetition}-{provider}"
                result_dir.mkdir(mode=0o777)
                result_dir.chmod(0o777)
                if provider == "smol":
                    row = run_smol_wave(
                        smolvm,
                        archive,
                        result_dir,
                        args.fanout,
                        min(args.parallel, args.fanout),
                        args.state_mib,
                        args.rounds,
                        args.hold_seconds,
                    )
                else:
                    row = run_container_wave(
                        runtime,
                        image,
                        result_dir,
                        args.fanout,
                        min(args.parallel, args.fanout),
                        args.state_mib,
                        args.rounds,
                        args.hold_seconds,
                    )
                row["repetition"] = repetition + 1
                raw.append(row)
                observed[provider] = canonical_results(row["results"])
            if observed["smol"] != observed["container"]:
                raise RuntimeError(
                    f"repetition {repetition + 1}: Smol and container outputs differ"
                )

        revision = command(
            ["git", "-C", str(Path(smolvm).resolve().parents[2]), "rev-parse", "HEAD"],
            check=False,
        )
        report = {
            "schema_version": 1,
            "validated_at": datetime.now(UTC).isoformat(),
            "config": {
                "fanout": args.fanout,
                "parallel": min(args.parallel, args.fanout),
                "repetitions": args.repetitions,
                "state_mib": args.state_mib,
                "rounds": args.rounds,
                "hold_seconds": args.hold_seconds,
            },
            "workload": {
                "identity_sha256": workload_identity(),
                "description": "Python hashing, compression, JSON, and regex over initialized immutable state",
            },
            "host": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "cpu_model": cpu_model(),
                "logical_cpus": os.cpu_count(),
                "memory_bytes": total_memory_bytes(),
            },
            "software": {
                "smolvm": command([smolvm, "--version"]).stdout.strip(),
                "smolvm_revision": (
                    revision.stdout.strip() if revision.returncode == 0 else "unknown"
                ),
                "container_runtime": command([runtime, "--version"]).stdout.strip(),
                "image_prepare_seconds": image_prepare_seconds,
            },
            "summary": summarize(raw, args.fanout),
            "raw": raw,
            "notes": [
                "Both providers run the exact same content-addressed image and output gate.",
                "The Smol source initializes the immutable byte state before the branchpoint; each native container initializes its own copy.",
                "Host MemAvailable deltas measure total physical pressure, including the retained Smol source, and can contain unrelated host noise.",
                "Each MemAvailable observation is the median of nine samples; provider order alternates between repetitions.",
                "Process RSS/PSS is intentionally excluded because it undercounts retained file-backed snapshot pages that are resident without a current process mapping.",
                "Preparation is excluded from wave time and reported separately.",
                "The capture window is SmolVM's checkpoint-and-resume span; child admission and workload completion remain separate timings.",
            ],
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        args.html.write_text(render(report))
        print(json.dumps(report["summary"], indent=2))
        print(f"Wrote {args.json} and {args.html}")


if __name__ == "__main__":
    main()
