from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path

import pytest

from bench import deepswe_daytona as benchmark


def _task_tree(root: Path) -> Path:
    task = root / "tasks" / "example-task"
    (task / "solution").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "solution" / "solve.sh").write_text("#!/bin/sh\ntrue\n")
    (task / "tests" / "test.sh").write_text("#!/bin/sh\ntrue\n")
    (task / "task.toml").write_text(
        """
[metadata]
display_title = "Example task"
base_commit_hash = "1111111111111111111111111111111111111111"

[environment]
docker_image = "example.invalid/task:sha256"
cpus = 2
memory_mb = 8192
storage_mb = 20480
""".strip()
        + "\n"
    )
    return root


class _Machine:
    def __init__(self, name: str):
        self.name = name
        self.deleted = False
        self.uploads: list[tuple[bytes, str]] = []
        self.commands: list[tuple[str, int]] = []

    def exec(self, command: str, timeout: int) -> benchmark.ExecResult:
        self.commands.append((command, timeout))
        return benchmark.ExecResult(0, "", "")

    def upload(self, data: bytes, path: str) -> None:
        self.uploads.append((data, path))

    def download(self, path: str) -> bytes:
        return b""

    def delete(self) -> None:
        self.deleted = True


class _Provider:
    name = "fake"

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.children: list[_Machine] = []

    def branch_many(self, source, task, names):
        if self.fail:
            raise RuntimeError("branch unavailable")
        children = [_Machine(name) for name in names]
        self.children.extend(children)
        return children, 0.25

    def accounting(self):
        return {}


class _DaytonaInner:
    def __init__(self, name: str, parent: "_DaytonaInner | None" = None):
        self.name = self.id = name
        self.parent = parent
        self.children: list[_DaytonaInner] = []
        self.deleted = False

    def fork(self, *, name: str, timeout: int):
        child = _DaytonaInner(name, self)
        self.children.append(child)
        return child

    def delete(self, *, timeout: int, wait: bool):
        if any(not child.deleted for child in self.children):
            raise RuntimeError("child still active")
        self.deleted = True


def test_load_task_builds_reproducible_payload(tmp_path: Path):
    checkout = _task_tree(tmp_path)
    first = benchmark.load_task(checkout, "example-task")
    second = benchmark.load_task(checkout, "example-task")

    assert first.image == "example.invalid/task:sha256"
    assert first.cpus == 2
    assert first.solution_archive == second.solution_archive
    assert first.tests_archive == second.tests_archive
    assert first.source_digest == second.source_digest


def test_load_task_rejects_unsafe_id(tmp_path: Path):
    with pytest.raises(ValueError, match="invalid DeepSWE task id"):
        benchmark.load_task(tmp_path, "../outside")


def test_archive_rejects_escaping_symlink(tmp_path: Path):
    root = tmp_path / "payload"
    root.mkdir()
    (root / "escape").symlink_to("../outside")
    with pytest.raises(RuntimeError, match="unsafe symlink"):
        benchmark._archive_tree(root)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [(b'{"reward": 1}', 1), (b"0\n", 0), (b'{"reward": -1}', None), (b"bad", None)],
)
def test_reward_parsing(payload: bytes, expected: int | None):
    assert benchmark._reward(payload) == expected


def test_install_archive_uses_persistent_workspace_path():
    machine = _Machine("archive")

    benchmark._install_archive(machine, b"payload", "/solution", "solution")

    assert machine.uploads == [(b"payload", "/workspace/.smolbench-solution.tar.gz")]


def test_verify_uploads_patch_to_persistent_workspace(monkeypatch, tmp_path: Path):
    task = benchmark.load_task(_task_tree(tmp_path), "example-task")
    machine = _Machine("verifier")
    monkeypatch.setattr(benchmark, "_install_archive", lambda *args: None)
    monkeypatch.setattr(machine, "download", lambda path: b'{"reward": 1}')

    reward, _ = benchmark._verify(machine, task, b"patch")

    assert reward == 1
    assert machine.uploads == [(b"patch", "/workspace/.smolbench-model.patch")]
    assert "/workspace/.smolbench-model.patch" in machine.commands[0][0]
    assert "/workspace/.smolbench-reward" in machine.commands[0][0]


def test_candidate_downloads_patch_from_persistent_workspace(
    monkeypatch, tmp_path: Path
):
    task = benchmark.load_task(_task_tree(tmp_path), "example-task")
    machine = _Machine("candidate")
    downloaded: list[str] = []
    monkeypatch.setattr(
        machine,
        "download",
        lambda path: downloaded.append(path) or b"patch",
    )

    patch, _ = benchmark._candidate(machine, task, "no-op")

    assert patch == b"patch"
    assert downloaded == ["/workspace/.smolbench-model.patch"]


def test_wave_uses_independent_agent_and_verifier_branches(monkeypatch, tmp_path: Path):
    task = benchmark.load_task(_task_tree(tmp_path), "example-task")
    provider = _Provider()

    monkeypatch.setattr(
        benchmark,
        "_candidate",
        lambda machine, task, candidate: (candidate.encode(), 0.1),
    )
    monkeypatch.setattr(
        benchmark,
        "_verify",
        lambda machine, task, patch: (1 if patch == b"oracle" else 0, 0.2),
    )

    trials, wave = benchmark.run_wave(
        provider,
        _Machine("source"),
        task,
        fanout=4,
        repetition=1,
        run_id="test",
    )

    assert len(provider.children) == 8
    assert len(trials) == 4
    assert all(trial.correct for trial in trials)
    assert all(child.deleted for child in provider.children)
    assert wave["branch_ready_seconds"] == 0.5
    assert wave["agent_ready_seconds"] == 0.25
    assert wave["verifier_ready_seconds"] == 0.25
    assert wave["error"] is None
    assert wave["cleanup_errors"] == []


def test_single_fanout_alternates_oracle_and_noop_across_repetitions(
    monkeypatch, tmp_path: Path
):
    task = benchmark.load_task(_task_tree(tmp_path), "example-task")
    provider = _Provider()
    seen: list[str] = []

    def candidate(machine, task, name):
        seen.append(name)
        return name.encode(), 0.1

    monkeypatch.setattr(benchmark, "_candidate", candidate)
    monkeypatch.setattr(
        benchmark,
        "_verify",
        lambda machine, task, patch: (1 if patch == b"oracle" else 0, 0.2),
    )

    first, _ = benchmark.run_wave(
        provider, _Machine("source"), task, fanout=1, repetition=1, run_id="test"
    )
    second, _ = benchmark.run_wave(
        provider, _Machine("source"), task, fanout=1, repetition=2, run_id="test"
    )

    assert seen == ["oracle", "no-op"]
    assert first[0].reward == 1
    assert second[0].reward == 0
    assert first[0].correct and second[0].correct


def test_wave_records_branch_failure(tmp_path: Path):
    task = benchmark.load_task(_task_tree(tmp_path), "example-task")
    trials, wave = benchmark.run_wave(
        _Provider(fail=True),
        _Machine("source"),
        task,
        fanout=1,
        repetition=1,
        run_id="test",
    )
    assert trials == []
    assert wave["branch_ready_seconds"] is None
    assert wave["error"] == "branch unavailable"


def test_daytona_tree_fanout_and_cleanup_respect_lineage(tmp_path: Path):
    task = benchmark.load_task(_task_tree(tmp_path), "example-task")
    provider = object.__new__(benchmark.DaytonaProvider)
    provider._fanout_method = "tree"
    provider._reports = []
    provider._snapshot_events = []
    provider._lock = threading.Lock()
    source_inner = _DaytonaInner("source")
    source = benchmark._DaytonaMachine(provider, source_inner, task, 0)

    children, elapsed = provider.branch_many(
        source, task, [f"child-{index}" for index in range(8)]
    )

    assert elapsed >= 0
    assert [child.name for child in children] == [
        f"child-{index}" for index in range(8)
    ]
    assert max(child._fork_depth for child in children) > 1
    assert benchmark._delete_all(children) == []
    assert all(child._inner.deleted for child in children)
    source.delete()


def test_dry_run_counts_one_source_per_task(tmp_path: Path):
    task = benchmark.load_task(_task_tree(tmp_path), "example-task")
    args = argparse.Namespace(
        providers=["smol-cloud", "daytona"],
        fanouts=[1, 4],
        repetitions=3,
        daytona_fanout="tree",
    )
    payload = benchmark.dry_run_payload([task, task], args)
    assert payload["planned_sandboxes_per_provider"] == 62


def test_billing_summary_uses_finalized_and_modeled_costs():
    payload = {
        "accounting": {
            "smol-cloud": {"cost": {"amountDueMicros": 125_000}},
            "daytona": {
                "modeled_cost_usd": {
                    "cpu": 0.10,
                    "memory": 0.02,
                    "disk_before_free_allowance": 0.001,
                }
            },
        }
    }
    assert benchmark._billing_summary(payload, "smol-cloud")[0] == 0.125
    assert benchmark._billing_summary(payload, "daytona")[0] == pytest.approx(0.121)


def test_smol_accounting_aggregates_only_numeric_usage_fields():
    provider = object.__new__(benchmark.SmolProvider)
    provider._reports = [
        {
            "usage": {"from": "2026-09-01T00:00:00Z", "cpuHours": 0.25},
            "cost": {"amountDueMicros": 100},
        },
        {
            "usage": {"from": "2026-09-01T00:00:00Z", "cpuHours": 0.5},
            "cost": {"amountDueMicros": 200},
        },
    ]

    accounting = provider.accounting()

    assert accounting["usage"] == {"cpuHours": 0.75}
    assert accounting["cost"] == {"amountDueMicros": 300.0}


def test_failed_run_is_written_and_returns_nonzero(monkeypatch, tmp_path: Path):
    payload = {
        "status": "failed",
        "providers": [],
        "tasks": [],
        "config": {"fanouts": []},
        "waves": [],
        "trials": [],
    }
    monkeypatch.setattr(benchmark, "run", lambda args: payload)
    output = tmp_path / "result.json"

    assert benchmark.main(["--output", str(output)]) == 1
    assert json.loads(output.read_text())["status"] == "failed"
    assert output.with_suffix(".html").is_file()


def test_startup_failure_is_written_and_returns_nonzero(monkeypatch, tmp_path: Path):
    def fail(args):
        raise RuntimeError("missing provider credential")

    monkeypatch.setattr(benchmark, "run", fail)
    output = tmp_path / "result.json"

    assert benchmark.main(["--output", str(output)]) == 1
    payload = json.loads(output.read_text())
    assert payload["status"] == "failed"
    assert payload["run_errors"] == ["missing provider credential"]
