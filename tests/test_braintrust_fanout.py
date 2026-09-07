import json
import subprocess
from types import SimpleNamespace

import pytest

from bench.braintrust_fanout import (
    SMOKE_CASES,
    run_docker_smoke,
    run_smoke,
    secret_env,
)


class FakeMachine:
    name = "branch-1"

    def exec(self, command, options):
        assert command[:2] == ["node", "-e"]
        assert options.env["SMOL_QUERY"] == SMOKE_CASES[0][1]
        return SimpleNamespace(
            exit_code=0,
            stdout=json.dumps(SMOKE_CASES[0][2]) + "\n",
            stderr="",
        )


def test_smoke_query_requires_exact_expected_result() -> None:
    result = run_smoke(FakeMachine(), 0)
    assert result.correct
    assert result.case == "most-issues"


def test_agent_mode_requires_matching_model_key(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        secret_env("claude-sonnet-4-5")


def test_docker_baseline_uses_same_query_and_requires_exact_output(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(SMOKE_CASES[0][2]) + "\n",
            stderr="",
        )

    monkeypatch.setattr("bench.braintrust_fanout.subprocess.run", fake_run)

    result = run_docker_smoke("braintrust:test", 0, "run")

    assert result.correct
    assert "--network" in observed["command"]
    assert "--cpus" in observed["command"]
    assert "--memory" in observed["command"]
    assert f"SMOL_QUERY={SMOKE_CASES[0][1]}" in observed["command"]
    assert observed["command"][-3:-1] == ["node", "-e"]
