import base64
import hashlib
import json
import subprocess

from bench.browsergym_fanout import (
    Candidate,
    candidates,
    parse_action,
    run_docker_action,
    without_screenshots,
)


INITIAL_SHA256 = "a" * 64


def payload(candidate: Candidate, action: str, **updates: object) -> str:
    screenshot = b"\x89PNG\r\n\x1a\nfixture"
    value = {
        "label": candidate.label,
        "action": action,
        "reward": candidate.reward,
        "terminated": candidate.terminated,
        "truncated": False,
        "last_action": action,
        "last_action_error": "",
        "action_count_before": 0,
        "action_count_after": 1,
        "initial_screenshot_sha256": INITIAL_SHA256,
        "screenshot_sha256": hashlib.sha256(screenshot).hexdigest(),
        "screenshot_base64": base64.b64encode(screenshot).decode(),
        "health": {"initial_screenshot_sha256": INITIAL_SHA256},
    }
    value.update(updates)
    return json.dumps(value)


def test_candidates_have_one_rewarded_action() -> None:
    options = candidates()

    assert len(options) == 4
    assert [item.label for item in options if item.reward] == ["correct"]
    assert len({item.action for item in options}) == 4


def test_result_requires_pristine_shared_start_and_expected_outcome() -> None:
    candidate = candidates()[0]
    action = "click('13')"
    valid = parse_action(
        runtime="smol-branch",
        repetition=1,
        candidate=candidate,
        action=action,
        duration=0.1,
        return_code=0,
        stdout=payload(candidate, action),
        stderr="",
        expected_initial_sha256=INITIAL_SHA256,
    )
    dirty = parse_action(
        runtime="smol-branch",
        repetition=1,
        candidate=candidate,
        action=action,
        duration=0.1,
        return_code=0,
        stdout=payload(candidate, action, action_count_before=1),
        stderr="",
        expected_initial_sha256=INITIAL_SHA256,
    )

    assert valid.correct
    assert not dirty.correct


def test_result_rejects_wrong_reward_or_screenshot() -> None:
    candidate = candidates()[1]
    action = candidate.action
    wrong_reward = payload(candidate, action, reward=1.0)
    wrong_digest = payload(candidate, action, screenshot_sha256="0" * 64)

    for value in (wrong_reward, wrong_digest):
        result = parse_action(
            runtime="docker",
            repetition=1,
            candidate=candidate,
            action=action,
            duration=0.1,
            return_code=0,
            stdout=value,
            stderr="",
            expected_initial_sha256=INITIAL_SHA256,
        )
        assert not result.correct


def test_docker_uses_pinned_worker_without_a_shell(monkeypatch) -> None:
    candidate = candidates()[0]
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        action = "click('13')"
        return subprocess.CompletedProcess(
            command, 0, stdout=payload(candidate, action), stderr=""
        )

    monkeypatch.setattr("bench.browsergym_fanout.subprocess.run", fake_run)

    result = run_docker_action("browsergym:test", candidate, 1, "13", INITIAL_SHA256)

    assert result.correct
    assert observed["command"][-4:] == [
        "/opt/smol-browsergym/worker.py",
        "--once",
        "correct",
        "click('{target_bid}')",
    ]
    assert "/bin/bash" not in observed["command"]


def test_public_json_removes_screenshot_payloads() -> None:
    assert without_screenshots(
        {"results": [{"label": "correct", "screenshot_base64": "large"}]}
    ) == {"results": [{"label": "correct"}]}
