import base64
import hashlib
import json
import subprocess

from bench.browser_fanout import (
    parse_action,
    run_docker_action,
    screenshots_are_unique,
    without_screenshots,
)


def payload(branch_id: str, *, action_count: int = 1) -> str:
    screenshot = b"\x89PNG\r\n\x1a\nfixture"
    return json.dumps(
        {
            "branch_id": branch_id,
            "action_count": action_count,
            "result": f"{branch_id} · action {action_count}",
            "screenshot_sha256": hashlib.sha256(screenshot).hexdigest(),
            "screenshot_base64": base64.b64encode(screenshot).decode(),
        }
    )


def test_action_requires_pristine_independent_browser_state() -> None:
    valid = parse_action(
        runtime="smol-branch",
        repetition=1,
        branch_id="branch-1",
        duration=0.1,
        return_code=0,
        stdout=payload("branch-1"),
        stderr="",
    )
    mutated = parse_action(
        runtime="smol-branch",
        repetition=1,
        branch_id="branch-1",
        duration=0.1,
        return_code=0,
        stdout=payload("branch-1", action_count=2),
        stderr="",
    )

    assert valid.correct
    assert not mutated.correct


def test_action_rejects_a_screenshot_with_the_wrong_digest() -> None:
    value = json.loads(payload("branch-1"))
    value["screenshot_sha256"] = "0" * 64

    result = parse_action(
        runtime="smol-branch",
        repetition=1,
        branch_id="branch-1",
        duration=0.1,
        return_code=0,
        stdout=json.dumps(value),
        stderr="",
    )

    assert not result.correct


def test_docker_runs_the_same_worker_without_a_shell(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=payload("branch-1"),
            stderr="",
        )

    monkeypatch.setattr("bench.browser_fanout.subprocess.run", fake_run)

    result = run_docker_action("browser:test", "branch-1", 1)

    assert result.correct
    assert observed["command"][-3:] == [
        "/opt/smol-browser/worker.py",
        "--once",
        "branch-1",
    ]
    assert "/bin/bash" not in observed["command"]


def test_public_json_omits_embedded_screenshots() -> None:
    assert without_screenshots(
        {"results": [{"branch_id": "one", "screenshot_base64": "large"}]}
    ) == {"results": [{"branch_id": "one"}]}


def test_every_branch_must_produce_a_distinct_screenshot() -> None:
    assert screenshots_are_unique(
        [{"screenshot_sha256": "one"}, {"screenshot_sha256": "two"}]
    )
    assert not screenshots_are_unique(
        [{"screenshot_sha256": "same"}, {"screenshot_sha256": "same"}]
    )
    assert not screenshots_are_unique([{"screenshot_sha256": ["invalid"]}])
