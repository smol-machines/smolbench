import subprocess
from pathlib import Path

from bench.swebench_hillclimb import (
    Candidate,
    candidates,
    parse_exit_codes,
    parse_reward,
    result_from_process,
)


def test_candidate_set_has_one_expected_winner(tmp_path: Path) -> None:
    solution = tmp_path / "solution"
    solution.mkdir()
    (solution / "solve.sh").write_text("#!/bin/sh\nexit 0\n")

    options = candidates(tmp_path)

    assert len({item.name for item in options}) == 4
    assert [item.name for item in options if item.expected_reward] == ["oracle"]


def test_all_synthetic_candidate_patches_are_well_formed(tmp_path: Path) -> None:
    solution = tmp_path / "solution"
    solution.mkdir()
    (solution / "solve.sh").write_text("#!/bin/sh\nexit 0\n")

    for candidate in candidates(tmp_path):
        if "cat > /tmp/candidate.patch" not in candidate.script:
            continue
        patch = candidate.script.split("<<'PATCH'\n", 1)[1].split("\nPATCH\n", 1)[0]
        result = subprocess.run(
            ["git", "apply", "--numstat"],
            input=f"{patch}\n",
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr


def test_result_requires_completed_candidate_and_official_grader_markers() -> None:
    candidate = Candidate("loser", "Expected failure", 0, "")
    output = """SWEBench results starts here
FAILED
SWEBench results ends here
__SMOL_REWARD__=0
__SMOL_CANDIDATE_RC__=0 __SMOL_VERIFIER_RC__=1
"""

    result = result_from_process(
        runtime="smol-branch",
        repetition=1,
        candidate=candidate,
        duration=1.0,
        return_code=0,
        stdout=output,
        stderr="",
    )

    assert result.correct
    assert parse_reward(output) == 0
    assert parse_exit_codes(output) == (0, 1)

    failed_patch = output.replace("__SMOL_CANDIDATE_RC__=0", "__SMOL_CANDIDATE_RC__=1")
    assert not result_from_process(
        runtime="smol-branch",
        repetition=1,
        candidate=candidate,
        duration=1.0,
        return_code=0,
        stdout=failed_patch,
        stderr="",
    ).correct
