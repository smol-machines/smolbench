import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_terminal_wrapper_rejects_ambiguous_output_argument() -> None:
    result = subprocess.run(
        [str(ROOT / "demo-terminal-bench.sh"), "--output", "/tmp/ignored.json"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 2
    assert "Set OUTPUT=" in result.stderr


def test_browsergym_wrapper_rejects_arguments() -> None:
    result = subprocess.run(
        [str(ROOT / "demo-browsergym-branch.sh"), "--output", "/tmp/ignored.json"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 2
    assert "Usage:" in result.stderr


def test_tau2_wrapper_rejects_arguments() -> None:
    result = subprocess.run(
        [str(ROOT / "demo-tau2-branch.sh"), "--output", "/tmp/ignored.json"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 2
    assert "Usage:" in result.stderr


def test_branch_state_wrapper_validates_fanout_before_using_smolvm() -> None:
    result = subprocess.run(
        [str(ROOT / "demo-branch-state.sh")],
        cwd=ROOT,
        env={"FANOUT": "0", "PATH": "/usr/bin:/bin"},
        text=True,
        capture_output=True,
    )

    assert result.returncode == 2
    assert "positive integers" in result.stderr
