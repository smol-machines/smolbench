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


FAKE_SMOLVM = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$SMOLVM_LOG"

name=""
prev=""
for arg in "$@"; do
  [[ "$prev" == "--name" ]] && name="$arg"
  prev="$arg"
done

case "$*" in
  *'printf "%s/%s"'*) printf 'shared-disk/shared-ram' ;;
  *'cat /root/branch-state-unique') printf 'unique-%s' "${name##*-}" ;;
esac
exit 0
"""


def test_branch_state_cleanup_deletes_every_machine_without_a_prompt(
    tmp_path: Path,
) -> None:
    """`machine delete` refuses without --force when stdin is not a terminal.

    The cleanup trap discards that failure, so a delete missing a
    non-interactive flag leaves the source machine running.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "smolvm"
    stub.write_text(FAKE_SMOLVM)
    stub.chmod(0o755)
    log = tmp_path / "calls.log"

    result = subprocess.run(
        [str(ROOT / "demo-branch-state.sh")],
        cwd=ROOT,
        env={
            "FANOUT": "2",
            "PATH": f"{stub_dir}:/usr/bin:/bin",
            "SMOLVM_LOG": str(log),
        },
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert "PASS: 2 branches" in result.stdout

    deletes = [
        line
        for line in log.read_text().splitlines()
        if line.startswith("machine delete ")
    ]
    assert len(deletes) == 3
    for call in deletes:
        assert "--force" in call or "--cascade" in call, call

    source = [call for call in deletes if "--name branch-state-child-" not in call]
    assert len(source) == 1
    assert "--cascade" in source[0], source[0]
