import json
from pathlib import Path
from types import SimpleNamespace

from bench.cloud_branch_state import _delete_all, _exec


ROOT = Path(__file__).resolve().parents[1]


class FakeMachine:
    def __init__(self, name="fake", *, stdout="ok", exit_code=0, delete_error=None):
        self.name = name
        self.stdout = stdout
        self.exit_code = exit_code
        self.delete_error = delete_error
        self.commands = []
        self.deleted = False

    def exec(self, command, options):
        self.commands.append((command, options))
        return SimpleNamespace(
            stdout=self.stdout,
            assert_success=lambda script: (_ for _ in ()).throw(RuntimeError(script))
            if self.exit_code
            else None,
        )

    def delete(self):
        self.deleted = True
        if self.delete_error:
            raise RuntimeError(self.delete_error)


def test_exec_uses_a_bounded_text_command() -> None:
    machine = FakeMachine(stdout="value\n")
    assert _exec(machine, "printf value") == "value"
    command, options = machine.commands[0]
    assert command == ["/bin/sh", "-lc", "printf value"]
    assert options.timeout == 30
    assert options.output == "text"


def test_cleanup_is_reverse_order_and_reports_failures() -> None:
    events = []

    class OrderedMachine(FakeMachine):
        def delete(self):
            events.append(self.name)
            super().delete()

    root = OrderedMachine("root")
    child = OrderedMachine("child", delete_error="busy")
    leaf = OrderedMachine("leaf")
    errors = _delete_all([root, child, leaf])
    assert events == ["leaf", "child", "root"]
    assert errors == ["child: busy"]


def test_committed_cloud_run_passed_every_lifecycle_check() -> None:
    result = json.loads((ROOT / "results" / "cloud-branch-state.json").read_text())
    assert result["checks_passed"] == result["checks_total"] == 36
    assert result["source_continued"] is True
    assert result["nested_branch_worked"] is True
    assert result["fanout"] == 4
    assert result["repetitions"] == 3
