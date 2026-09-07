import json
from pathlib import Path

from bench.public_scorecard import build_scorecard, render_scorecard


ROOT = Path(__file__).resolve().parents[1]


def test_scorecard_covers_every_headline_control() -> None:
    payload = build_scorecard(ROOT / "results")
    assert payload["cloud_validation"]["checks_passed"] == 36
    assert payload["cloud_validation"]["checks_total"] == 36
    entries = payload["results"]
    labels = {(entry["workload"], entry["fanout"]) for entry in entries}
    assert labels == {
        ("Aider Polyglot", 4),
        ("Aider Polyglot", 16),
        ("Terminal-Bench regex-log", 4),
        ("SWE-bench Verified", 4),
        ("Harbor Index GSO", 4),
        ("tau2-bench retail", 4),
        ("BrowserGym MiniWoB", 4),
        ("Braintrust bash-agent-evals", 4),
        ("CPU and memory control", 16),
    }
    assert all(entry["correct"] == entry["total"] for entry in entries)
    density = next(
        entry for entry in entries if entry["workload"] == "CPU and memory control"
    )
    harbor = next(entry for entry in entries if entry["workload"] == "Harbor Index GSO")
    assert density["relative_speed"] >= 1
    assert density["physical_memory_ratio"] >= 6
    assert harbor["control"] == "Podman"
    assert 0.95 <= harbor["relative_speed"] <= 1.05


def test_committed_scorecard_is_generated_from_committed_evidence() -> None:
    payload = build_scorecard(ROOT / "results")
    assert json.loads((ROOT / "results" / "scorecard.json").read_text()) == payload
    assert (ROOT / "results" / "scorecard.html").read_text() == render_scorecard(
        payload
    )
