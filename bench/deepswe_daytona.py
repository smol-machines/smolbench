#!/usr/bin/env python3
"""Compare Smol Cloud and Daytona on pinned DeepSWE branch workloads.

The oracle/no-op matrix removes model latency and model quality from the
measurement. Each provider starts one prepared VM, creates independent agent
and verifier children, applies the official DeepSWE solution in selected
children, and grades every child with the task's official separate verifier.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import importlib.metadata
import io
import json
import math
import os
import platform
import re
import shlex
import statistics
import subprocess
import tarfile
import threading
import time
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol


DEEPSWE_REPOSITORY = "https://github.com/datacurve-ai/deep-swe.git"
DEEPSWE_REVISION = "0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea"
DEFAULT_TASKS = ("fastapi-implicit-head-options", "wasmi-trap-coredumps")
DEFAULT_FANOUTS = (1, 4)
SMOL_PRICING_URL = "https://smolmachines.com/pricing"
DAYTONA_BILLING_URL = "https://www.daytona.io/docs/en/billing/"
DAYTONA_PRICING_URL = "https://www.daytona.io/"

# Public list rates on 2026-09-12. Raw rates and source URLs are emitted into
# every artifact so old results remain interpretable if either price changes.
DAYTONA_CPU_HOUR = 0.0504
DAYTONA_MEMORY_GB_HOUR = 0.0162
DAYTONA_DISK_GB_HOUR = 0.000108

TASK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    display_title: str
    image: str
    base_commit: str
    cpus: int
    memory_mb: int
    storage_mb: int
    path: Path
    solution_archive: bytes
    tests_archive: bytes
    source_digest: str


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class TrialResult:
    provider: str
    task: str
    repetition: int
    fanout: int
    candidate: str
    expected_reward: int
    reward: int | None
    branch_ready_seconds: float
    candidate_seconds: float
    verifier_seconds: float
    patch_bytes: int
    patch_sha256: str
    correct: bool
    error: str | None


class Machine(Protocol):
    name: str

    def exec(self, command: str, timeout: int) -> ExecResult: ...

    def upload(self, data: bytes, path: str) -> None: ...

    def download(self, path: str) -> bytes: ...

    def delete(self) -> None: ...


class Provider(Protocol):
    name: str

    def prepare_source(self, task: TaskSpec, name: str) -> tuple[Machine, float]: ...

    def branch_many(
        self, source: Machine, task: TaskSpec, names: list[str]
    ) -> tuple[list[Machine], float]: ...

    def accounting(self) -> dict[str, Any]: ...


def _run(command: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {shlex.join(command)}\n"
            f"{result.stderr[-4000:]}"
        )
    return result.stdout.strip()


def ensure_deepswe(cache: Path) -> Path:
    """Return an immutable checkout of the pinned public DeepSWE revision."""
    checkout = cache / f"deep-swe-{DEEPSWE_REVISION[:12]}"
    if not checkout.exists():
        cache.mkdir(parents=True, exist_ok=True)
        _run(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                DEEPSWE_REPOSITORY,
                str(checkout),
            ]
        )
        _run(["git", "checkout", "--detach", DEEPSWE_REVISION], cwd=checkout)
    observed = _run(["git", "rev-parse", "HEAD"], cwd=checkout)
    if observed != DEEPSWE_REVISION:
        raise RuntimeError(
            f"cached DeepSWE checkout is {observed}, expected {DEEPSWE_REVISION}; "
            f"remove {checkout} and retry"
        )
    if _run(["git", "status", "--porcelain"], cwd=checkout):
        raise RuntimeError(f"cached DeepSWE checkout is modified: {checkout}")
    return checkout


def _archive_tree(root: Path) -> bytes:
    """Create a deterministic archive without accepting escaping symlinks."""
    if not root.is_dir():
        raise RuntimeError(f"missing task directory: {root}")
    output = io.BytesIO()
    with gzip.GzipFile(
        fileobj=output, mode="wb", compresslevel=6, mtime=0
    ) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root)
                if path.is_symlink():
                    target = os.readlink(path)
                    if Path(target).is_absolute() or ".." in Path(target).parts:
                        raise RuntimeError(
                            f"unsafe symlink in task payload: {relative}"
                        )
                    info = tarfile.TarInfo(relative.as_posix())
                    info.type = tarfile.SYMTYPE
                    info.linkname = target
                    info.mode = 0o777
                    info.mtime = 0
                    archive.addfile(info)
                    continue
                info = archive.gettarinfo(str(path), arcname=relative.as_posix())
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                if info.isfile():
                    with path.open("rb") as source:
                        archive.addfile(info, source)
                elif info.isdir():
                    archive.addfile(info)
                else:
                    raise RuntimeError(f"unsupported task payload entry: {relative}")
    return output.getvalue()


def load_task(checkout: Path, task_id: str) -> TaskSpec:
    if not TASK_ID.fullmatch(task_id):
        raise ValueError(f"invalid DeepSWE task id: {task_id!r}")
    path = checkout / "tasks" / task_id
    config_path = path / "task.toml"
    if not config_path.is_file():
        raise RuntimeError(f"DeepSWE task does not exist: {task_id}")
    config = tomllib.loads(config_path.read_text())
    metadata = config["metadata"]
    environment = config["environment"]
    base_commit = str(metadata["base_commit_hash"])
    if not SHA.fullmatch(base_commit):
        raise RuntimeError(f"task {task_id} has an invalid base commit")
    digest = hashlib.sha256()
    for subtree in ("task.toml", "solution", "tests"):
        candidate = path / subtree
        if candidate.is_file():
            digest.update(candidate.read_bytes())
        else:
            for entry in sorted(candidate.rglob("*")):
                if entry.is_file():
                    digest.update(entry.relative_to(path).as_posix().encode())
                    digest.update(entry.read_bytes())
    return TaskSpec(
        task_id=task_id,
        display_title=str(metadata["display_title"]),
        image=str(environment["docker_image"]),
        base_commit=base_commit,
        cpus=int(environment["cpus"]),
        memory_mb=int(environment["memory_mb"]),
        storage_mb=int(environment["storage_mb"]),
        path=path,
        solution_archive=_archive_tree(path / "solution"),
        tests_archive=_archive_tree(path / "tests"),
        source_digest=digest.hexdigest(),
    )


class _SmolMachine:
    def __init__(self, owner: "SmolProvider", inner: Any, started: float):
        self._owner = owner
        self._inner = inner
        self._started = started
        self.name = inner.name
        self._deleted = False

    def exec(self, command: str, timeout: int) -> ExecResult:
        from smol import ExecOptions

        result = self._inner.exec(
            ["/bin/bash", "-lc", command],
            ExecOptions(timeout=timeout, workdir="/"),
        )
        return ExecResult(result.exit_code, result.stdout, result.stderr)

    def upload(self, data: bytes, path: str) -> None:
        self._inner.write_file(path, data)

    def download(self, path: str) -> bytes:
        return self._inner.read_file(path)

    def delete(self) -> None:
        if self._deleted:
            return
        report = self._inner.delete(include_usage=True)
        self._deleted = True
        self._owner.record_delete(self.name, self._started, report)


class SmolProvider:
    name = "smol-cloud"

    def __init__(self, *, api_key: str, base_url: str | None):
        from smol import ConnectOptions

        self._connection = ConnectOptions(
            target="cloud", api_key=api_key, base_url=base_url
        )
        self._reports: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def prepare_source(self, task: TaskSpec, name: str) -> tuple[Machine, float]:
        from smol import Machine as SmolMachine
        from smol import MachineConfig, ResourceSpec

        started = time.perf_counter()
        machine = SmolMachine.create(
            MachineConfig(
                name=name,
                image=task.image,
                resources=ResourceSpec(
                    cpus=task.cpus,
                    memory_mb=task.memory_mb,
                    storage_gb=math.ceil(task.storage_mb / 1024),
                    network=False,
                ),
                persistent=True,
                forkable=True,
                ready_timeout_seconds=1800,
            ),
            self._connection,
        )
        wrapped = _SmolMachine(self, machine, started)
        try:
            _checked_exec(wrapped, "test -d /app/.git", 60, "validate DeepSWE image")
        except BaseException:
            wrapped.delete()
            raise
        return wrapped, time.perf_counter() - started

    def branch_many(
        self, source: Machine, task: TaskSpec, names: list[str]
    ) -> tuple[list[Machine], float]:
        assert isinstance(source, _SmolMachine)
        started = time.perf_counter()
        children = source._inner.branch_batch(names=names)
        ready = time.perf_counter()
        return (
            [_SmolMachine(self, child, started) for child in children],
            ready - started,
        )

    def record_delete(self, name: str, started: float, report: Any) -> None:
        item = {
            "machine": name,
            "observed_lifetime_seconds": time.perf_counter() - started,
            "usage": dict(report.usage) if report else {},
            "cost": dict(report.cost) if report else {},
        }
        with self._lock:
            self._reports.append(item)

    def accounting(self) -> dict[str, Any]:
        reports = list(self._reports)
        cost_keys = {key for report in reports for key in report["cost"]}
        usage_keys = {key for report in reports for key in report["usage"]}
        return {
            "basis": "metered by Smol Cloud and finalized at machine deletion",
            "source": SMOL_PRICING_URL,
            "machines": reports,
            "cost": {
                key: sum(float(row["cost"].get(key, 0)) for row in reports)
                for key in sorted(cost_keys)
            },
            "usage": {
                key: sum(float(row["usage"].get(key, 0)) for row in reports)
                for key in sorted(usage_keys)
            },
        }


class _DaytonaMachine:
    def __init__(
        self,
        owner: "DaytonaProvider",
        inner: Any,
        task: TaskSpec,
        started: float,
        fork_depth: int = 0,
    ):
        self._owner = owner
        self._inner = inner
        self._task = task
        self._started = started
        self._fork_depth = fork_depth
        self.name = getattr(inner, "name", None) or inner.id
        self._deleted = False

    def exec(self, command: str, timeout: int) -> ExecResult:
        result = self._inner.process.exec(command, cwd="/", timeout=timeout)
        return ExecResult(int(result.exit_code), str(result.result or ""), "")

    def upload(self, data: bytes, path: str) -> None:
        self._inner.fs.upload_file(data, path)

    def download(self, path: str) -> bytes:
        value = self._inner.fs.download_file(path)
        if not isinstance(value, bytes):
            raise RuntimeError(f"Daytona returned no bytes for {path}")
        return value

    def delete(self) -> None:
        if self._deleted:
            return
        self._inner.delete(timeout=300, wait=True)
        self._deleted = True
        self._owner.record_delete(self.name, self._task, self._started)


class DaytonaProvider:
    name = "daytona"

    def __init__(self, *, fanout_method: str):
        from daytona import Daytona

        self._client = Daytona()
        self._fanout_method = fanout_method
        self._reports: list[dict[str, Any]] = []
        self._snapshot_events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    @staticmethod
    def _snapshot_name(task: TaskSpec) -> str:
        identity = hashlib.sha256(
            f"{DEEPSWE_REVISION}:{task.image}:{task.cpus}:{task.memory_mb}:"
            f"{task.storage_mb}".encode()
        ).hexdigest()[:12]
        return f"smolbench-{task.task_id[:32]}-{identity}"

    @staticmethod
    def _not_found(error: BaseException) -> bool:
        text = str(error).lower()
        return "404" in text or "not found" in text or "does not exist" in text

    def _ensure_snapshot(self, task: TaskSpec) -> tuple[str, float, bool]:
        from daytona import CreateSnapshotParams, Resources, SandboxClass

        name = self._snapshot_name(task)
        started = time.perf_counter()
        try:
            self._client.snapshot.get(name)
            return name, time.perf_counter() - started, True
        except Exception as error:  # noqa: BLE001
            if not self._not_found(error):
                raise
        self._client.snapshot.create(
            CreateSnapshotParams(
                name=name,
                image=task.image,
                resources=Resources(
                    cpu=task.cpus,
                    memory=math.ceil(task.memory_mb / 1024),
                    disk=math.ceil(task.storage_mb / 1024),
                ),
                sandbox_class=SandboxClass.LINUX_VM,
            ),
            timeout=1800,
        )
        return name, time.perf_counter() - started, False

    def prepare_source(self, task: TaskSpec, name: str) -> tuple[Machine, float]:
        from daytona import CreateSandboxFromSnapshotParams

        started = time.perf_counter()
        snapshot, snapshot_seconds, cached = self._ensure_snapshot(task)
        inner_started = time.perf_counter()
        inner = self._client.create(
            CreateSandboxFromSnapshotParams(
                name=name,
                snapshot=snapshot,
                network_block_all=True,
            ),
            timeout=1800,
        )
        wrapped = _DaytonaMachine(self, inner, task, inner_started)
        try:
            _checked_exec(wrapped, "test -d /app/.git", 60, "validate DeepSWE image")
        except BaseException:
            wrapped.delete()
            raise
        self._snapshot_events.append(
            {
                "task": task.task_id,
                "snapshot": snapshot,
                "cached": cached,
                "seconds": snapshot_seconds,
            }
        )
        return wrapped, time.perf_counter() - started

    def branch_many(
        self, source: Machine, task: TaskSpec, names: list[str]
    ) -> tuple[list[Machine], float]:
        assert isinstance(source, _DaytonaMachine)
        started = time.perf_counter()
        children: list[Machine] = []
        try:
            if self._fanout_method == "serial":
                for name in names:
                    fork_started = time.perf_counter()
                    child = source._inner.fork(name=name, timeout=300)
                    children.append(
                        _DaytonaMachine(self, child, task, fork_started, fork_depth=1)
                    )
            else:
                # Daytona documents one in-flight fork per parent and recommends
                # a tree when concurrency matters. Each level gives every parent
                # at most one fork and preserves the pristine starting state.
                parents = [source]
                offset = 0
                while offset < len(names):
                    level_names = names[offset : offset + len(parents)]
                    level_parents = parents[: len(level_names)]

                    def fork_one(item: tuple[Machine, str]) -> Machine | BaseException:
                        parent, child_name = item
                        assert isinstance(parent, _DaytonaMachine)
                        try:
                            fork_started = time.perf_counter()
                            child = parent._inner.fork(name=child_name, timeout=300)
                            return _DaytonaMachine(
                                self,
                                child,
                                task,
                                fork_started,
                                fork_depth=parent._fork_depth + 1,
                            )
                        except BaseException as error:  # noqa: BLE001
                            return error

                    with ThreadPoolExecutor(max_workers=len(level_names)) as pool:
                        results = list(
                            pool.map(fork_one, zip(level_parents, level_names))
                        )
                    level = [
                        result
                        for result in results
                        if not isinstance(result, BaseException)
                    ]
                    children.extend(level)
                    failures = [
                        result
                        for result in results
                        if isinstance(result, BaseException)
                    ]
                    if failures:
                        raise RuntimeError(
                            "Daytona tree fan-out failed: "
                            + "; ".join(str(error) for error in failures)
                        )
                    parents.extend(level)
                    offset += len(level)
        except BaseException:
            errors = _delete_all(children)
            if errors:
                raise RuntimeError(
                    "Daytona fan-out failed and partial cleanup also failed: "
                    + "; ".join(errors)
                )
            raise
        return children, time.perf_counter() - started

    def record_delete(self, name: str, task: TaskSpec, started: float) -> None:
        lifetime = time.perf_counter() - started
        cpu_hours = task.cpus * lifetime / 3600
        memory_gb_hours = math.ceil(task.memory_mb / 1024) * lifetime / 3600
        disk_gb_hours = math.ceil(task.storage_mb / 1024) * lifetime / 3600
        item = {
            "machine": name,
            "observed_lifetime_seconds": lifetime,
            "reserved_cpu_hours": cpu_hours,
            "reserved_memory_gb_hours": memory_gb_hours,
            "reserved_disk_gb_hours": disk_gb_hours,
            "modeled_cost_usd": {
                "cpu": cpu_hours * DAYTONA_CPU_HOUR,
                "memory": memory_gb_hours * DAYTONA_MEMORY_GB_HOUR,
                "disk_before_free_allowance": disk_gb_hours * DAYTONA_DISK_GB_HOUR,
            },
        }
        with self._lock:
            self._reports.append(item)

    def accounting(self) -> dict[str, Any]:
        reports = list(self._reports)
        components = ("cpu", "memory", "disk_before_free_allowance")
        return {
            "basis": (
                "modeled from reserved resources and measured lifecycle time; "
                "excludes credits and egress, and shows disk before Daytona's "
                "first-5-GiB allowance"
            ),
            "sources": [DAYTONA_BILLING_URL, DAYTONA_PRICING_URL],
            "rates": {
                "cpu_per_vcpu_hour": DAYTONA_CPU_HOUR,
                "memory_per_gb_hour": DAYTONA_MEMORY_GB_HOUR,
                "disk_per_gb_hour": DAYTONA_DISK_GB_HOUR,
            },
            "fanout_method": self._fanout_method,
            "snapshots": self._snapshot_events,
            "machines": reports,
            "modeled_cost_usd": {
                key: sum(row["modeled_cost_usd"][key] for row in reports)
                for key in components
            },
        }


def _checked_exec(
    machine: Machine, command: str, timeout: int, label: str
) -> ExecResult:
    result = machine.exec(command, timeout)
    if result.exit_code != 0:
        raise RuntimeError(
            f"{label} failed in {machine.name} ({result.exit_code})\n"
            f"stdout:\n{result.stdout[-3000:]}\n"
            f"stderr:\n{result.stderr[-3000:]}"
        )
    return result


def _install_archive(machine: Machine, data: bytes, target: str, label: str) -> None:
    archive = f"/tmp/smolbench-{label}.tar.gz"
    machine.upload(data, archive)
    _checked_exec(
        machine,
        f"rm -rf {shlex.quote(target)} && mkdir -p {shlex.quote(target)} && "
        f"tar -xzf {shlex.quote(archive)} -C {shlex.quote(target)} && "
        f"rm -f {shlex.quote(archive)}",
        120,
        f"install {label}",
    )


def _candidate(machine: Machine, task: TaskSpec, candidate: str) -> tuple[bytes, float]:
    started = time.perf_counter()
    if candidate == "oracle":
        _install_archive(machine, task.solution_archive, "/solution", "solution")
        _checked_exec(
            machine,
            "cd /app && bash /solution/solve.sh",
            1800,
            "apply official solution",
        )
    elif candidate != "no-op":
        raise ValueError(f"unknown candidate: {candidate}")
    base = shlex.quote(task.base_commit)
    _checked_exec(
        machine,
        "mkdir -p /logs/artifacts && "
        "git config --global --add safe.directory /app && "
        f"cd /app && git diff --binary {base} HEAD > /logs/artifacts/model.patch",
        300,
        "collect candidate patch",
    )
    return machine.download(
        "/logs/artifacts/model.patch"
    ), time.perf_counter() - started


def _reward(payload: bytes) -> int | None:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    result = value.get("reward") if isinstance(value, dict) else value
    return int(result) if result in (0, 1, 0.0, 1.0) else None


def _verify(machine: Machine, task: TaskSpec, patch: bytes) -> tuple[int | None, float]:
    started = time.perf_counter()
    _install_archive(machine, task.tests_archive, "/tests", "tests")
    machine.upload(patch, "/tmp/model.patch")
    _checked_exec(
        machine,
        "mkdir -p /logs/artifacts /logs/verifier && "
        "cp /tmp/model.patch /logs/artifacts/model.patch && "
        "chmod +x /tests/test.sh && bash /tests/test.sh",
        1800,
        "run official verifier",
    )
    try:
        payload = machine.download("/logs/verifier/reward.json")
    except Exception:  # noqa: BLE001
        payload = machine.download("/logs/verifier/reward.txt")
    return _reward(payload), time.perf_counter() - started


def _delete_all(machines: list[Machine]) -> list[str]:
    errors: list[str] = []
    errors_lock = threading.Lock()

    def remove(machine: Machine) -> None:
        try:
            machine.delete()
        except Exception as error:  # noqa: BLE001
            with errors_lock:
                errors.append(f"{machine.name}: {error}")

    depths = sorted(
        {int(getattr(machine, "_fork_depth", 0)) for machine in machines},
        reverse=True,
    )
    for depth in depths:
        level = [
            machine
            for machine in machines
            if int(getattr(machine, "_fork_depth", 0)) == depth
        ]
        with ThreadPoolExecutor(max_workers=min(16, max(1, len(level)))) as pool:
            list(pool.map(remove, level))
    return errors


def run_wave(
    provider: Provider,
    source: Machine,
    task: TaskSpec,
    *,
    fanout: int,
    repetition: int,
    run_id: str,
) -> tuple[list[TrialResult], dict[str, Any]]:
    candidates = ["oracle" if index % 2 == 0 else "no-op" for index in range(fanout)]
    names = [
        f"ds-{run_id}-{task.task_id[:12]}-r{repetition}-a{index}"
        for index in range(fanout)
    ] + [
        f"ds-{run_id}-{task.task_id[:12]}-r{repetition}-v{index}"
        for index in range(fanout)
    ]
    children: list[Machine] = []
    started = time.perf_counter()
    cleanup_errors: list[str] = []
    branch_seconds: float | None = None
    trials: list[TrialResult] = []
    wave_error: str | None = None
    try:
        children, branch_seconds = provider.branch_many(source, task, names)
        if len(children) != fanout * 2:
            raise RuntimeError(
                f"{provider.name} returned {len(children)} of {fanout * 2} branches"
            )
        agent_children = children[:fanout]
        verifier_children = children[fanout:]
        candidate_outputs: list[tuple[bytes, float] | BaseException] = []

        def run_candidate(
            item: tuple[Machine, str],
        ) -> tuple[bytes, float] | BaseException:
            try:
                return _candidate(item[0], task, item[1])
            except BaseException as error:  # noqa: BLE001
                return error

        with ThreadPoolExecutor(max_workers=fanout) as pool:
            candidate_outputs = list(
                pool.map(run_candidate, zip(agent_children, candidates))
            )

        def run_verifier(index: int) -> TrialResult:
            output = candidate_outputs[index]
            expected = 1 if candidates[index] == "oracle" else 0
            if isinstance(output, BaseException):
                return TrialResult(
                    provider.name,
                    task.task_id,
                    repetition,
                    fanout,
                    candidates[index],
                    expected,
                    None,
                    branch_seconds,
                    0,
                    0,
                    0,
                    hashlib.sha256(b"").hexdigest(),
                    False,
                    str(output),
                )
            patch, candidate_seconds = output
            try:
                reward, verifier_seconds = _verify(
                    verifier_children[index], task, patch
                )
                return TrialResult(
                    provider.name,
                    task.task_id,
                    repetition,
                    fanout,
                    candidates[index],
                    expected,
                    reward,
                    branch_seconds,
                    candidate_seconds,
                    verifier_seconds,
                    len(patch),
                    hashlib.sha256(patch).hexdigest(),
                    reward == expected,
                    None
                    if reward == expected
                    else f"expected reward {expected}, got {reward}",
                )
            except BaseException as error:  # noqa: BLE001
                return TrialResult(
                    provider.name,
                    task.task_id,
                    repetition,
                    fanout,
                    candidates[index],
                    expected,
                    None,
                    branch_seconds,
                    candidate_seconds,
                    0,
                    len(patch),
                    hashlib.sha256(patch).hexdigest(),
                    False,
                    str(error),
                )

        with ThreadPoolExecutor(max_workers=fanout) as pool:
            trials = list(pool.map(run_verifier, range(fanout)))
    except BaseException as error:  # noqa: BLE001
        wave_error = str(error)
    finally:
        cleanup_errors = _delete_all(children)
    return trials, {
        "provider": provider.name,
        "task": task.task_id,
        "repetition": repetition,
        "fanout": fanout,
        "branch_ready_seconds": branch_seconds,
        "wall_seconds": time.perf_counter() - started,
        "error": wave_error,
        "cleanup_errors": cleanup_errors,
    }


def _provider(name: str, args: argparse.Namespace) -> Provider:
    if name == "smol-cloud":
        token = os.environ.get("SMOL_CLOUD_TOKEN")
        if not token:
            raise RuntimeError("SMOL_CLOUD_TOKEN is required for provider smol-cloud")
        return SmolProvider(api_key=token, base_url=args.smol_url)
    if name == "daytona":
        api_key = os.environ.get("DAYTONA_API_KEY")
        jwt = os.environ.get("DAYTONA_JWT_TOKEN")
        organization = os.environ.get("DAYTONA_ORGANIZATION_ID")
        if not api_key and not (jwt and organization):
            raise RuntimeError(
                "Daytona credentials are required: set DAYTONA_API_KEY, or both "
                "DAYTONA_JWT_TOKEN and DAYTONA_ORGANIZATION_ID"
            )
        return DaytonaProvider(fanout_method=args.daytona_fanout)
    raise ValueError(f"unknown provider: {name}")


def _median_wave_metric(
    waves: list[dict[str, Any]],
    provider: str,
    task: str,
    fanout: int,
    metric: str,
) -> float:
    values = [
        float(row[metric])
        for row in waves
        if row["provider"] == provider
        and row["task"] == task
        and int(row["fanout"]) == fanout
        and not row.get("error")
        and not row.get("cleanup_errors")
        and row.get(metric) is not None
    ]
    return statistics.median(values) if values else 0.0


def _billing_summary(
    payload: dict[str, Any], provider: str
) -> tuple[float | None, str]:
    accounting = payload.get("accounting", {}).get(provider, {})
    if provider == "smol-cloud":
        cost = accounting.get("cost", {})
        micros = cost.get("amountDueMicros", cost.get("totalMicros"))
        if micros is None:
            return None, "finalized utilization meter"
        return float(micros) / 1_000_000, "finalized utilization meter"
    if provider == "daytona":
        components = accounting.get("modeled_cost_usd", {})
        if not components:
            return None, "reserved-capacity model"
        return sum(
            float(value) for value in components.values()
        ), "reserved-capacity model"
    return None, "unknown"


def render_html(payload: dict[str, Any]) -> str:
    providers = payload["providers"]
    tasks = payload["tasks"]
    fanouts = payload["config"]["fanouts"]
    waves = payload["waves"]
    rows = []
    for task in tasks:
        for fanout in fanouts:
            cells = []
            for provider in providers:
                ready = _median_wave_metric(
                    waves, provider, task["task_id"], fanout, "branch_ready_seconds"
                )
                wall = _median_wave_metric(
                    waves, provider, task["task_id"], fanout, "wall_seconds"
                )
                cells.append(
                    f"<td>{ready:.2f}s / {wall:.2f}s</td>" if wall else "<td>—</td>"
                )
            rows.append(
                f"<tr><td>{html.escape(task['display_title'])}</td>"
                f"<td>{fanout}</td>{''.join(cells)}</tr>"
            )
    header = "".join(
        f"<th>{html.escape(name)}<br>branch / end-to-end</th>" for name in providers
    )
    correct = sum(1 for row in payload["trials"] if row["correct"])
    total = len(payload["trials"])
    status = payload.get("status", "unknown")
    billing_rows = []
    for provider in providers:
        total_cost, basis = _billing_summary(payload, provider)
        provider_correct = sum(
            1
            for row in payload["trials"]
            if row["provider"] == provider and row["correct"]
        )
        cost = f"${total_cost:.6f}" if total_cost is not None else "—"
        per_trial = (
            f"${total_cost / provider_correct:.6f}"
            if total_cost is not None and provider_correct
            else "—"
        )
        billing_rows.append(
            f"<tr><td>{html.escape(provider)}</td><td>{cost}</td>"
            f"<td>{per_trial}</td><td>{html.escape(basis)}</td></tr>"
        )
    embedded = html.escape(json.dumps(payload, sort_keys=True))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smol Cloud vs Daytona — DeepSWE</title>
<style>
body{{font:16px/1.45 system-ui,sans-serif;max-width:980px;margin:40px auto;padding:0 20px;color:#18181b}}
h1{{font-size:42px;line-height:1.05}} .lede{{font-size:21px;color:#3f3f46}} table{{border-collapse:collapse;width:100%;margin:24px 0}}
th,td{{padding:12px;border-bottom:1px solid #ddd;text-align:left}} .ok{{color:#08783e;font-weight:700}} .note{{background:#f4f4f5;padding:16px;border-radius:10px}}
code{{font-size:13px}} details{{margin-top:24px}}
</style></head><body>
<h1>Smol Cloud vs Daytona on real DeepSWE environments</h1>
<p class="lede">Same pinned images, official solutions, official separate verifiers, and independent VM branches. Model latency is removed so this measures infrastructure.</p>
<p class="ok">Run status: {html.escape(status)} · correctness gate: {correct}/{total} expected rewards.</p>
<table><thead><tr><th>Task</th><th>Fan-out</th>{header}</tr></thead><tbody>{"".join(rows)}</tbody></table>
<p>End-to-end covers branch creation, candidate work, independent verification, and cleanup; source preparation is recorded separately.</p>
<div class="note"><strong>What branching means here.</strong> A sequential agent still benefits from starting each attempt after setup. Mid-trajectory branching only applies to retries, candidate search, or subagents. Both products support live VM forks; this run compares their supported fan-out paths.</div>
<h2>Cost over the measured lifecycle</h2>
<table><thead><tr><th>Provider</th><th>Total</th><th>Per correct trial</th><th>Basis</th></tr></thead><tbody>{"".join(billing_rows)}</tbody></table>
<p>Smol records finalized active CPU, resident memory, used disk, and machine-base charges. Daytona is modeled from its published reserved CPU/RAM/disk rates and each measured sandbox lifetime. Daytona snapshot storage, credits, and egress are excluded.</p>
<p>Sources: <a href="{SMOL_PRICING_URL}">Smol pricing</a>, <a href="{DAYTONA_BILLING_URL}">Daytona billing</a>, <a href="{DAYTONA_PRICING_URL}">Daytona rates</a>.</p>
<details><summary>Raw artifact</summary><pre><code>{embedded}</code></pre></details>
</body></html>"""


def dry_run_payload(tasks: list[TaskSpec], args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": "dry-run",
        "deep_swe": {"repository": DEEPSWE_REPOSITORY, "revision": DEEPSWE_REVISION},
        "providers": args.providers,
        "tasks": [
            {
                "task_id": task.task_id,
                "display_title": task.display_title,
                "image": task.image,
                "resources": {
                    "cpus": task.cpus,
                    "memory_mb": task.memory_mb,
                    "storage_mb": task.storage_mb,
                },
                "source_digest": task.source_digest,
            }
            for task in tasks
        ],
        "fanouts": args.fanouts,
        "repetitions": args.repetitions,
        "daytona_fanout": args.daytona_fanout,
        "planned_sandboxes_per_provider": len(tasks)
        * (1 + sum(2 * fanout * args.repetitions for fanout in args.fanouts)),
    }


def _installed_version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def run(args: argparse.Namespace) -> dict[str, Any]:
    checkout = ensure_deepswe(args.cache)
    tasks = [load_task(checkout, task_id) for task_id in args.tasks]
    if args.dry_run:
        return dry_run_payload(tasks, args)
    providers = [_provider(name, args) for name in args.providers]
    run_id = uuid.uuid4().hex[:7]
    waves: list[dict[str, Any]] = []
    trials: list[TrialResult] = []
    setup: list[dict[str, Any]] = []
    sources: list[Machine] = []
    cleanup_errors: list[str] = []
    run_errors: list[str] = []
    try:
        for task in tasks:
            for provider in providers:
                try:
                    source, seconds = provider.prepare_source(
                        task, f"ds-{run_id}-{provider.name[:5]}-{task.task_id[:18]}"
                    )
                except BaseException as error:  # noqa: BLE001
                    run_errors.append(
                        f"{provider.name} {task.task_id} source setup: {error}"
                    )
                    continue
                sources.append(source)
                setup.append(
                    {
                        "provider": provider.name,
                        "task": task.task_id,
                        "seconds": seconds,
                    }
                )
                stop_task = False
                for fanout in args.fanouts:
                    for repetition in range(1, args.repetitions + 1):
                        wave_trials, wave = run_wave(
                            provider,
                            source,
                            task,
                            fanout=fanout,
                            repetition=repetition,
                            run_id=run_id,
                        )
                        trials.extend(wave_trials)
                        waves.append(wave)
                        if wave["error"]:
                            run_errors.append(
                                f"{provider.name} {task.task_id} fanout={fanout} "
                                f"repetition={repetition}: {wave['error']}"
                            )
                            stop_task = True
                            break
                        if not wave_trials or not all(
                            row.correct for row in wave_trials
                        ):
                            run_errors.append(
                                f"correctness gate failed for {provider.name} "
                                f"{task.task_id} fanout={fanout} repetition={repetition}"
                            )
                            stop_task = True
                            break
                    if stop_task:
                        break
    finally:
        cleanup_errors.extend(_delete_all(list(reversed(sources))))
    expected_trials = len(providers) * len(tasks) * args.repetitions * sum(args.fanouts)
    passed = (
        not run_errors
        and not cleanup_errors
        and len(trials) == expected_trials
        and all(row.correct for row in trials)
    )
    payload = {
        "schema_version": 1,
        "status": "passed" if passed else "failed",
        "created_at": datetime.now(UTC).isoformat(),
        "runner": {
            "host": platform.node(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "packages": {
                "smolmachines": _installed_version("smolmachines"),
                "daytona": _installed_version("daytona"),
            },
        },
        "deep_swe": {"repository": DEEPSWE_REPOSITORY, "revision": DEEPSWE_REVISION},
        "method": (
            "Pinned DeepSWE image; official oracle and no-op candidates; official "
            "separate verifier; provider source prepared once; fresh agent and verifier "
            "branches per trial; model inference excluded."
        ),
        "config": {
            "fanouts": args.fanouts,
            "repetitions": args.repetitions,
            "daytona_fanout": args.daytona_fanout,
        },
        "providers": [provider.name for provider in providers],
        "tasks": [
            {
                "task_id": task.task_id,
                "display_title": task.display_title,
                "image": task.image,
                "base_commit": task.base_commit,
                "resources": {
                    "cpus": task.cpus,
                    "memory_mb": task.memory_mb,
                    "storage_mb": task.storage_mb,
                },
                "source_digest": task.source_digest,
            }
            for task in tasks
        ],
        "setup": setup,
        "waves": waves,
        "trials": [asdict(row) for row in trials],
        "accounting": {provider.name: provider.accounting() for provider in providers},
        "run_errors": run_errors,
        "cleanup_errors": cleanup_errors,
    }
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--providers",
        default="smol-cloud,daytona",
        help="comma-separated: smol-cloud,daytona",
    )
    parser.add_argument(
        "--tasks",
        default=",".join(DEFAULT_TASKS),
        help="comma-separated DeepSWE task ids",
    )
    parser.add_argument(
        "--fanouts",
        default=",".join(map(str, DEFAULT_FANOUTS)),
        help="comma-separated positive branch counts",
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument(
        "--daytona-fanout",
        choices=("tree", "serial"),
        default="tree",
        help="Daytona fan-out strategy; tree is its documented scale path",
    )
    parser.add_argument("--cache", type=Path, default=Path(".cache/deepswe-daytona"))
    parser.add_argument(
        "--output", type=Path, default=Path("results/deepswe-daytona.json")
    )
    parser.add_argument("--smol-url", default=os.environ.get("SMOL_CLOUD_URL"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    args.providers = [
        value.strip() for value in args.providers.split(",") if value.strip()
    ]
    args.tasks = [value.strip() for value in args.tasks.split(",") if value.strip()]
    try:
        args.fanouts = [int(value) for value in args.fanouts.split(",")]
    except ValueError as error:
        parser.error(f"invalid --fanouts: {error}")
    if not args.providers or any(
        name not in {"smol-cloud", "daytona"} for name in args.providers
    ):
        parser.error("--providers must contain smol-cloud and/or daytona")
    if not args.tasks:
        parser.error("--tasks cannot be empty")
    if not args.fanouts or any(value < 1 or value > 32 for value in args.fanouts):
        parser.error("--fanouts values must be between 1 and 32")
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = run(args)
    except Exception as error:  # noqa: BLE001
        payload = {
            "schema_version": 1,
            "status": "failed",
            "created_at": datetime.now(UTC).isoformat(),
            "deep_swe": {
                "repository": DEEPSWE_REPOSITORY,
                "revision": DEEPSWE_REVISION,
            },
            "config": {
                "fanouts": args.fanouts,
                "repetitions": args.repetitions,
                "daytona_fanout": args.daytona_fanout,
            },
            "providers": args.providers,
            "tasks": [],
            "setup": [],
            "waves": [],
            "trials": [],
            "accounting": {},
            "run_errors": [str(error)],
            "cleanup_errors": [],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if payload.get("mode") != "dry-run":
        args.output.with_suffix(".html").write_text(render_html(payload))
    print(json.dumps(payload, indent=2, sort_keys=True))
    return (
        0
        if payload.get("mode") == "dry-run" or payload.get("status") == "passed"
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
