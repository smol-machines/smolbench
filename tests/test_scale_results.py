import json
from pathlib import Path

import pytest

from bench.scale_results import read_failure_rows, read_rows, render, summarize


def row(provider: str, fanout: int, wall: float) -> dict:
    return {
        "provider": provider,
        "dataset": "public@1",
        "task": "real-task",
        "attempts": fanout,
        "concurrency": fanout,
        "install_only": True,
        "return_code": 0,
        "completed": fanout,
        "errors": 0,
        "correctness_error": None,
        "wall_seconds": wall,
        "harbor_seconds": wall - 1,
        "environment_setup_seconds": {"p50": wall / 2, "p99": wall / 1.5},
        "approximate_peak_host_memory_bytes": fanout * 2**20,
        "host": {"platform": "test", "machine": "x86_64", "logical_cpus": 8},
        "software": {"harbor": "1", "smolmachines": "1"},
    }


def test_scale_summary_requires_and_reports_clean_waves(tmp_path: Path) -> None:
    source = tmp_path / "results.json"
    source.write_text(json.dumps([row("smol-branch", 16, 5), row("docker", 16, 9)]))

    report = summarize(read_rows([source]), "abcdef123456")

    assert report["smol_completed"] == 16
    assert report["comparisons"]["fanout_16"]["smol_full_lifecycle_speedup"] == 1.8
    assert "16-way soak" in render(report)


def test_scale_summary_rejects_partial_success(tmp_path: Path) -> None:
    failed = row("smol-branch", 16, 5)
    failed["completed"] = 15
    source = tmp_path / "failed.json"
    source.write_text(json.dumps([failed]))

    with pytest.raises(ValueError, match="failed correctness gate"):
        read_rows([source])


def test_failed_soak_is_retained_but_not_merged_into_clean_curve(
    tmp_path: Path,
) -> None:
    clean_source = tmp_path / "clean.json"
    clean_source.write_text(json.dumps([row("smol-branch", 64, 12)] * 3))
    failed = [row("smol-branch", 128, 20) for _ in range(3)]
    failed[-1]["completed"] = 96
    failed[-1]["errors"] = 32
    failed_source = tmp_path / "failed.json"
    failed_source.write_text(json.dumps(failed))

    report = summarize(
        read_rows([clean_source]),
        "abcdef123456",
        read_failure_rows([failed_source]),
    )

    assert report["failed_soaks"][0]["completed"] == 352
    assert report["points"][0]["fanout"] == 64
    assert "Failed qualification retained" in render(report)
