from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.cpu_density import canonical_results, ratio, summarize, validate_results


ROOT = Path(__file__).parents[1]


def workload_results(count: int, prefix: str = "a") -> list[dict[str, object]]:
    return [
        {
            "task_id": index,
            "digest": (prefix * 64)[:64],
            "checksum": index + 10,
            "work_ms": float(index + 1),
        }
        for index in range(count)
    ]


def test_validate_results_rejects_duplicate_task_ids() -> None:
    rows = workload_results(2)
    rows[1]["task_id"] = 0

    with pytest.raises(ValueError, match="unexpected task IDs"):
        validate_results(rows, 2)


def test_canonical_results_compares_provider_outputs() -> None:
    assert canonical_results(workload_results(2)) == {
        0: ("a" * 64, 10),
        1: ("a" * 64, 11),
    }


def test_summarize_separates_capture_from_wave_and_memory() -> None:
    raw = [
        {
            "provider": "smol-branch",
            "prepare_seconds": 2.0,
            "launch_seconds": 0.4,
            "capture_window_ms": 30.0,
            "wave_seconds": 1.0,
            "idle_source_cpu_percent": 0.0,
            "physical_memory_bytes": 100.0,
            "incremental_children_memory_bytes": 40.0,
            "work_ms": [4.0, 6.0],
        },
        {
            "provider": "podman",
            "prepare_seconds": 0.0,
            "launch_seconds": 0.8,
            "capture_window_ms": None,
            "wave_seconds": 2.0,
            "idle_source_cpu_percent": None,
            "physical_memory_bytes": 600.0,
            "incremental_children_memory_bytes": 600.0,
            "work_ms": [5.0, 7.0],
        },
    ]

    summary = summarize(raw, fanout=2)
    smol = next(
        point for point in summary["points"] if point["provider"] == "smol-branch"
    )

    assert smol["median_capture_window_ms"] == 30.0
    assert summary["comparisons"] == {
        "container_to_smol_physical_memory_ratio": 6.0,
        "container_to_smol_incremental_worker_memory_ratio": 15.0,
        "container_to_smol_wave_speed_ratio": 2.0,
    }


def test_ratio_rejects_missing_or_nonpositive_denominators() -> None:
    assert ratio(4.0, 2.0) == 2.0
    assert ratio(None, 2.0) is None
    assert ratio(4.0, None) is None
    assert ratio(4.0, 0.0) is None


def test_published_cpu_density_report_is_complete_and_matched() -> None:
    report = json.loads((ROOT / "results/cpu-density.json").read_text())

    assert report["config"] == {
        "fanout": 16,
        "hold_seconds": 15,
        "parallel": 16,
        "repetitions": 5,
        "rounds": 128,
        "state_mib": 256,
    }
    for repetition in range(1, 6):
        rows = [row for row in report["raw"] if row["repetition"] == repetition]
        assert {row["provider"] for row in rows} == {"podman", "smol-branch"}
        by_provider = {row["provider"]: row for row in rows}
        assert canonical_results(by_provider["podman"]["results"]) == canonical_results(
            by_provider["smol-branch"]["results"]
        )

    comparison = report["summary"]["comparisons"]
    assert comparison["container_to_smol_physical_memory_ratio"] >= 6.0
    assert comparison["container_to_smol_wave_speed_ratio"] >= 1.0
    smol = next(
        point
        for point in report["summary"]["points"]
        if point["provider"] == "smol-branch"
    )
    assert smol["median_capture_window_ms"] < 100
    assert smol["median_idle_source_cpu_percent"] < 1
