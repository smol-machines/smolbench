from __future__ import annotations

import pytest

from bench.tau2_agent import (
    compact_timeline,
    runtime_validated,
    summarize_simulation,
    task_passed,
    validate_api_base,
)


def test_api_base_must_be_guest_reachable() -> None:
    assert validate_api_base("https://model.example/v1/") == (
        "https://model.example/v1"
    )
    with pytest.raises(ValueError, match="guest"):
        validate_api_base("http://127.0.0.1:8000/v1")
    with pytest.raises(ValueError, match="http"):
        validate_api_base("model.example/v1")


def test_simulation_summary_keeps_evidence_without_raw_provider_payloads() -> None:
    summary = summarize_simulation(
        {
            "task_id": "33",
            "trial": 0,
            "seed": 123,
            "duration": 12.5,
            "termination_reason": "user_stop",
            "reward_info": {
                "reward": 1.0,
                "db_check": {"db_match": True},
                "action_checks": [
                    {"action_match": True},
                    {"action_match": False},
                ],
            },
            "messages": [
                {
                    "role": "assistant",
                    "content": "Checking the order.",
                    "tool_calls": [
                        {"name": "get_order_details", "arguments": {"id": "W1"}}
                    ],
                    "raw_data": {"large": "provider payload"},
                }
            ],
        }
    )

    assert summary["reward"] == 1.0
    assert summary["db_match"] is True
    assert summary["matched_actions"] == 1
    assert summary["expected_actions"] == 2
    assert "raw_data" not in summary["timeline"][0]
    assert task_passed(summary)
    assert runtime_validated(summary, True)
    assert not runtime_validated(summary, False)


def test_model_quality_is_separate_from_runtime_validation() -> None:
    summary = {
        "task_id": "33",
        "reward": 0.0,
        "db_match": False,
        "termination_reason": "user_stop",
        "timeline": [{"role": "assistant", "content": "I cannot solve this."}],
    }

    assert not task_passed(summary)
    assert runtime_validated(summary, True)


def test_timeline_caps_conversation_content() -> None:
    timeline = compact_timeline([{"role": "user", "content": "x" * 2000}])

    assert len(timeline[0]["content"]) == 1000
