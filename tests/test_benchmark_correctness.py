import subprocess
import tarfile
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from bench.harbor_fanout import (
    display_path,
    interval_gap,
    prepare_digest_pinned_task,
    prepare_docker_task,
    prepare_dockerfile_task,
    prepare_published_docker_image,
    summarize_job,
    validate_harbor_agent,
    validate_result,
)


def result(*, rewards: list[float], errors: int = 0, return_code: int = 0):
    return SimpleNamespace(
        provider="smol-branch",
        rewards=rewards,
        errors=errors,
        return_code=return_code,
    )


def test_result_paths_are_relative_inside_the_benchmark(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result_path = tmp_path / "results" / "raw" / "job" / "result.json"
    assert display_path(result_path) == "results/raw/job/result.json"


def test_interval_gap_measures_uninstrumented_handoff() -> None:
    before = {
        "started_at": "2026-09-05T00:00:00Z",
        "finished_at": "2026-09-05T00:00:01.250000Z",
    }
    after = {
        "started_at": "2026-09-05T00:00:03.750000Z",
        "finished_at": "2026-09-05T00:00:04Z",
    }
    assert interval_gap(before, after) == 2.5


def test_oracle_reward_must_meet_minimum() -> None:
    with pytest.raises(RuntimeError, match=r"observed=0\.000\.\.1\.000"):
        validate_result(
            result(rewards=[1.0, 0.0]),
            attempts=2,
            install_only=False,
            minimum_reward=1.0,
        )


def test_install_only_does_not_require_rewards() -> None:
    validate_result(
        result(rewards=[]),
        attempts=2,
        install_only=True,
        minimum_reward=1.0,
    )


def test_unknown_agent_is_rejected_before_benchmark_preparation() -> None:
    validate_harbor_agent("nop")
    with pytest.raises(RuntimeError, match="unknown Harbor agent 'not-real'"):
        validate_harbor_agent("not-real")


def test_matched_docker_preparation_copies_task_and_rewrites_image(
    tmp_path, monkeypatch
) -> None:
    task = tmp_path / "real-task"
    task.mkdir()
    original = '[environment]\ndocker_image = "example/base:1"\n'
    (task / "task.toml").write_text(original)
    (task / "instruction.md").write_text("do the work\n")
    script = tmp_path / "prepare.sh"
    script.write_text("set -e\necho warmed > /warm\n")
    observed = {}

    monkeypatch.setattr("bench.harbor_fanout.docker_preflight", lambda: None)

    def fake_run(command, **kwargs):
        if command[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(command, 1)
        observed["dockerfile"] = kwargs["input"]
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("bench.harbor_fanout.subprocess.run", fake_run)

    prepared, _, built, image = prepare_docker_task(task, script, tmp_path / "cache")

    assert built
    assert image in (prepared / "task.toml").read_text()
    assert "example/base:1" in observed["dockerfile"]
    assert "echo warmed" in observed["dockerfile"]
    assert (task / "task.toml").read_text() == original


def test_published_image_is_pulled_and_content_identified(monkeypatch) -> None:
    commands = []

    monkeypatch.setattr("bench.harbor_fanout.docker_preflight", lambda: None)
    monkeypatch.setattr(
        "bench.harbor_fanout.command_version", lambda command: "sha256:before"
    )

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[:2] == ["docker", "pull"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    '[{"Id":"sha256:after","RepoDigests":'
                    '["example/task@sha256:digest"]}]'
                ),
                stderr="",
            )
        raise AssertionError(command)

    monkeypatch.setattr("bench.harbor_fanout.subprocess.run", fake_run)
    seconds, updated, identity = prepare_published_docker_image("example/task:1", 30)

    assert seconds >= 0
    assert updated is True
    assert identity == {
        "reference": "example/task:1",
        "id": "sha256:after",
        "repository_digest": "example/task@sha256:digest",
    }
    assert ["docker", "pull", "example/task:1"] in commands


def test_published_task_uses_same_digest_for_both_providers(tmp_path) -> None:
    task = tmp_path / "task"
    task.mkdir()
    original = (
        '[environment]\ndocker_image = "example/agent:latest"\n'
        '[verifier.environment]\ndocker_image = "example/verifier:latest"\n'
    )
    (task / "task.toml").write_text(original)
    identities = {
        "environment": {
            "reference": "example/agent:latest",
            "repository_digest": "example/agent@sha256:aaa",
        },
        "verifier": {
            "reference": "example/verifier:latest",
            "repository_digest": "example/verifier@sha256:bbb",
        },
    }

    prepared = prepare_digest_pinned_task(task, identities, tmp_path / "cache")

    assert prepared != task
    definition = (prepared / "task.toml").read_text()
    assert 'docker_image = "example/agent@sha256:aaa"' in definition
    assert 'docker_image = "example/verifier@sha256:bbb"' in definition
    assert (task / "task.toml").read_text() == original

    (prepared / "task.toml").unlink()
    (prepared / "incomplete").write_text("stale cache")
    with pytest.raises(RuntimeError, match="cached prepared task is incomplete"):
        prepare_digest_pinned_task(task, identities, tmp_path / "cache")


def test_dockerfile_task_builds_one_export_for_smol_and_docker(
    tmp_path, monkeypatch
) -> None:
    task = tmp_path / "polyglot_python_real"
    environment = task / "environment"
    environment.mkdir(parents=True)
    original = "[environment]\nbuild_timeout_sec = 30\ncpus = 1\n"
    (task / "task.toml").write_text(original)
    (task / "instruction.md").write_text("fix it\n")
    (environment / "Dockerfile").write_text("FROM alpine:3.20\n")
    image_exists = False
    builds = 0
    exports = 0

    monkeypatch.setattr("bench.harbor_fanout.docker_preflight", lambda: None)

    def fake_run(command, **kwargs):
        nonlocal image_exists, builds, exports
        if command[:2] == ["docker", "version"]:
            return subprocess.CompletedProcess(
                command, 0, stdout="linux/amd64\n", stderr=""
            )
        if command[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(command, 0 if image_exists else 1)
        if command[:2] == ["docker", "build"]:
            image_exists = True
            builds += 1
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:3] == ["docker", "image", "save"]:
            exports += 1
            output = next(
                arg.removeprefix("--output=")
                for arg in command
                if arg.startswith("--output=")
            )
            payload = tmp_path / "payload"
            payload.write_text("layer")
            with tarfile.open(output, "w") as archive:
                archive.add(payload, arcname="layer")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr("bench.harbor_fanout.subprocess.run", fake_run)
    first = prepare_dockerfile_task(task, tmp_path / "cache")
    second = prepare_dockerfile_task(task, tmp_path / "cache")

    prepared, archive, _, _, built, image = first
    assert built is True
    assert second[4] is False
    assert builds == 1
    assert exports == 1
    assert tarfile.is_tarfile(archive)
    assert image == second[5]
    assert f'docker_image = "{image}"' in (prepared / "task.toml").read_text()
    assert (task / "task.toml").read_text() == original


def test_summary_records_task_content_hash(tmp_path) -> None:
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.toml").write_text('[task]\nname = "real/task"\n')
    job = tmp_path / "job"
    trial = job / "trial"
    trial.mkdir(parents=True)
    now = datetime.now(UTC).isoformat()
    (job / "result.json").write_text(
        '{"started_at":"' + now + '","finished_at":"' + now + '","stats":{}}'
    )
    (trial / "result.json").write_text('{"verifier_result":{"rewards":{"reward":1.0}}}')

    result = summarize_job(
        provider="smol-branch",
        repetition=1,
        dataset="real/dataset",
        task="real-task",
        source_task_path=task,
        workload_image={"tag": "real/image:1", "id": "sha256:abc"},
        attempts=1,
        concurrency=1,
        agent="oracle",
        model=None,
        install_only=False,
        checkpoint_mode="prepared",
        checkpoint_prepare_seconds=1.0,
        return_code=0,
        wall_seconds=2.0,
        peak_memory=3,
        job_dir=job,
        provider_prepare_seconds=None,
        provider_prepare_built=None,
        checkpoint_prepare_phases=None,
    )

    assert len(result.task_tree_sha256) == 64
    assert result.workload_image == {"tag": "real/image:1", "id": "sha256:abc"}
    assert result.rewards == [1.0]
