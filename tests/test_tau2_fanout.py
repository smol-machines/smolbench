import json
import subprocess
from pathlib import Path

from bench.tau2_fanout import (
    TAU2_REVISION,
    Candidate,
    candidates,
    install_tau2_command,
    parse_candidate,
    run_docker_candidate,
)


INITIAL_HASH = "a" * 64
ROOT = Path(__file__).parents[1]


def payload(candidate: Candidate, **updates: object) -> str:
    value = {
        "label": candidate.label,
        "title": candidate.title,
        "expected_reward": candidate.expected_reward,
        "reward": candidate.expected_reward,
        "db_match": candidate.expected_reward == 1.0,
        "reward_basis": ["DB"],
        "initial_db_hash": INITIAL_HASH,
        "starting_db_hash": INITIAL_HASH,
        "final_db_hash": "b" * 64,
        "action_count_before": 0,
        "action_count_after": 1,
        "tool_calls": ["get_order_details", "modify_user_address"],
        "tool_errors": [],
        "state": {
            "address": {"city": candidate.expected_city},
            "order_status": candidate.expected_order_status,
        },
        "worker_seconds": 0.1,
        "health": {
            "initial_db_hash": INITIAL_HASH,
            "action_count": 1,
            "source_revision": TAU2_REVISION,
        },
        "pre_action_health": {
            "initial_db_hash": INITIAL_HASH,
            "action_count": 0,
            "source_revision": TAU2_REVISION,
            "worker_initialize_seconds": 1.2,
        },
    }
    value.update(updates)
    return json.dumps(value)


def test_candidates_have_one_official_winner() -> None:
    options = candidates()

    assert len(options) == 4
    assert [item.label for item in options if item.expected_reward] == ["correct"]
    assert len({item.expected_city for item in options}) == 3


def test_prepared_runtime_includes_official_user_simulator_data() -> None:
    command = install_tau2_command()

    assert "data/tau2/domains/retail" in command
    assert "data/tau2/user_simulator" in command


def test_result_requires_pristine_common_state_and_expected_outcome() -> None:
    candidate = candidates()[0]
    valid = parse_candidate(
        runtime="smol-branch",
        repetition=1,
        candidate=candidate,
        duration=0.2,
        return_code=0,
        stdout=payload(candidate),
        stderr="",
        expected_initial_hash=INITIAL_HASH,
    )
    dirty = parse_candidate(
        runtime="smol-branch",
        repetition=1,
        candidate=candidate,
        duration=0.2,
        return_code=0,
        stdout=payload(candidate, starting_db_hash="c" * 64),
        stderr="",
        expected_initial_hash=INITIAL_HASH,
    )

    assert valid.correct
    assert not dirty.correct


def test_result_rejects_wrong_official_reward_or_final_state() -> None:
    candidate = candidates()[1]
    wrong_reward = payload(candidate, reward=1.0)
    wrong_city = payload(
        candidate,
        state={"address": {"city": "Seattle"}, "order_status": "pending"},
    )

    for value in (wrong_reward, wrong_city):
        result = parse_candidate(
            runtime="docker",
            repetition=1,
            candidate=candidate,
            duration=0.2,
            return_code=0,
            stdout=value,
            stderr="",
            expected_initial_hash=INITIAL_HASH,
        )
        assert not result.correct


def test_docker_uses_pinned_worker_without_network_or_shell(monkeypatch) -> None:
    candidate = candidates()[0]
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        return subprocess.CompletedProcess(
            command, 0, stdout=payload(candidate), stderr=""
        )

    monkeypatch.setattr("bench.tau2_fanout.subprocess.run", fake_run)

    result = run_docker_candidate("tau2:test", candidate, 1, INITIAL_HASH)

    assert result.correct
    command = observed["command"]
    assert command[command.index("--network") + 1] == "none"
    assert command[-3:] == [
        "/opt/tau2-worker/tau2_worker.py",
        "--once",
        "correct",
    ]
    assert "/bin/bash" not in command


def test_published_result_is_fully_qualified() -> None:
    result = json.loads((ROOT / "results" / "tau2-branch-search.json").read_text())

    assert result["revision"] == TAU2_REVISION
    assert result["repetitions"] == 3
    assert result["smol"]["correct"] == 12
    assert result["docker"]["correct"] == 12
    assert result["smol"]["source_unchanged"] is True
    assert {item["label"] for item in result["smol"]["results"]} == {
        "correct",
        "wrong-address",
        "cancel-order",
        "no-change",
    }
