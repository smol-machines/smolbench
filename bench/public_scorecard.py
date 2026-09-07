#!/usr/bin/env python3
"""Build one public scorecard from the committed benchmark evidence."""

from __future__ import annotations

import argparse
import html
import json
import statistics
from pathlib import Path
from typing import Any


def _load(results: Path, name: str) -> Any:
    return json.loads((results / name).read_text())


def _relative_speed(control_seconds: float, smol_seconds: float) -> float:
    if smol_seconds <= 0 or control_seconds <= 0:
        raise ValueError("benchmark durations must be positive")
    return control_seconds / smol_seconds


def _container_runtime_name(rows: list[dict[str, Any]]) -> str:
    for row in rows:
        runtime = (row.get("software") or {}).get("container_runtime")
        if isinstance(runtime, str) and runtime.lower().startswith("podman"):
            return "Podman"
    return "Docker"


def _harbor_entry(
    results: Path,
    *,
    source: str,
    label: str,
    expected_reward: float,
    note: str,
) -> dict[str, Any]:
    rows = _load(results, source)
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{source} must contain benchmark rows")
    grouped = {
        provider: [row for row in rows if row.get("provider") == provider]
        for provider in ("smol-branch", "docker")
    }
    if any(not provider_rows for provider_rows in grouped.values()):
        raise ValueError(f"{source} must contain Smol and Docker rows")
    for row in rows:
        if row.get("return_code") != 0 or row.get("errors") != 0:
            raise ValueError(f"{source} contains a failed run")
        attempts = int(row["attempts"])
        rewards = row.get("rewards", [])
        if int(row.get("completed", 0)) != attempts or len(rewards) != attempts:
            raise ValueError(f"{source} contains a partial run")
        if any(float(reward) != expected_reward for reward in rewards):
            raise ValueError(f"{source} failed its expected reward gate")
    smol_seconds = statistics.median(
        float(row["wall_seconds"]) for row in grouped["smol-branch"]
    )
    control_seconds = statistics.median(
        float(row["wall_seconds"]) for row in grouped["docker"]
    )
    return {
        "workload": label,
        "fanout": int(rows[0]["attempts"]),
        "repetitions": len(grouped["smol-branch"]),
        "smol_seconds": smol_seconds,
        "control": _container_runtime_name(grouped["docker"]),
        "control_seconds": control_seconds,
        "relative_speed": _relative_speed(control_seconds, smol_seconds),
        "correct": sum(int(row["completed"]) for row in rows),
        "total": sum(int(row["attempts"]) for row in rows),
        "evidence": source,
        "note": note,
    }


def _paired_entry(
    *,
    source: str,
    label: str,
    fanout: int,
    repetitions: int,
    smol_seconds: float,
    control_seconds: float,
    correct: int,
    total: int,
    note: str,
    control: str = "Docker",
    memory_ratio: float | None = None,
) -> dict[str, Any]:
    if correct != total or total <= 0:
        raise ValueError(f"{source} failed its correctness gate")
    entry: dict[str, Any] = {
        "workload": label,
        "fanout": fanout,
        "repetitions": repetitions,
        "smol_seconds": smol_seconds,
        "control": control,
        "control_seconds": control_seconds,
        "relative_speed": _relative_speed(control_seconds, smol_seconds),
        "correct": correct,
        "total": total,
        "evidence": source,
        "note": note,
    }
    if memory_ratio is not None:
        entry["physical_memory_ratio"] = memory_ratio
    return entry


def build_scorecard(results: Path) -> dict[str, Any]:
    entries = [
        _harbor_entry(
            results,
            source="aider-polyglot-n4.json",
            label="Aider Polyglot",
            expected_reward=1.0,
            note="Ordinary Dockerfile-backed coding task and verifier.",
        ),
        _harbor_entry(
            results,
            source="aider-polyglot-n16.json",
            label="Aider Polyglot",
            expected_reward=1.0,
            note="The same coding task at 16-way concurrency.",
        ),
        _harbor_entry(
            results,
            source="terminal-bench-matched-instrumented.json",
            label="Terminal-Bench regex-log",
            expected_reward=1.0,
            note="Prepared shell environment and official verifier.",
        ),
        _harbor_entry(
            results,
            source="swebench-verified-pinned-final.json",
            label="SWE-bench Verified",
            expected_reward=1.0,
            note="Published Django issue, oracle patch, and official verifier.",
        ),
        _harbor_entry(
            results,
            source="harbor-index-gso-n4.json",
            label="Harbor Index GSO",
            expected_reward=0.0,
            note="Large NumPy build and artifact-to-verifier path on an eight-core bare-metal host; the nop control intentionally scores zero.",
        ),
    ]

    tau2 = _load(results, "tau2-branch-search.json")
    entries.append(
        _paired_entry(
            source="tau2-branch-search.json",
            label="tau2-bench retail",
            fanout=int(tau2["fanout"]),
            repetitions=int(tau2["repetitions"]),
            smol_seconds=float(tau2["smol"]["median_branch_seconds"])
            + float(tau2["smol"]["median_candidate_wall_seconds"]),
            control_seconds=float(tau2["docker"]["median_start_and_candidate_seconds"]),
            correct=int(tau2["smol"]["correct"]) + int(tau2["docker"]["correct"]),
            total=len(tau2["smol"]["runs"]) * int(tau2["fanout"])
            + len(tau2["docker"]["runs"]) * int(tau2["fanout"]),
            note="Branch/evaluate/select over initialized tool and database state.",
        )
    )

    browser = _load(results, "browsergym-branch-search.json")
    entries.append(
        _paired_entry(
            source="browsergym-branch-search.json",
            label="BrowserGym MiniWoB",
            fanout=int(browser["fanout"]),
            repetitions=int(browser["repetitions"]),
            smol_seconds=float(browser["smol"]["median_branch_seconds"])
            + float(browser["smol"]["median_action_wall_seconds"]),
            control_seconds=float(browser["docker"]["median_start_and_action_seconds"]),
            correct=int(browser["smol"]["correct"]) + int(browser["docker"]["correct"]),
            total=len(browser["smol"]["results"]) + len(browser["docker"]["results"]),
            note="Live Chromium state branches correctly; first-touch execution remains slower.",
        )
    )

    braintrust = _load(results, "braintrust-warm-baremetal-i9700-5x4.json")
    entries.append(
        _paired_entry(
            source="braintrust-warm-baremetal-i9700-5x4.json",
            label="Braintrust bash-agent-evals",
            fanout=int(braintrust["fanout"]),
            repetitions=int(braintrust["repetitions"]),
            smol_seconds=float(
                braintrust["smol_retained_pool"]["wall_seconds"]["median"]
            ),
            control_seconds=float(braintrust["docker_fresh"]["wall_seconds"]["median"]),
            correct=int(braintrust["smol_retained_pool"]["correct"])
            + int(braintrust["docker_fresh"]["correct"]),
            total=int(braintrust["fanout"]) * int(braintrust["repetitions"]) * 2,
            control="Podman",
            note="Representative lifecycle on bare metal: release a branch retained at the initialized hotspot versus create a fresh container; direct recapture and prewarmed-process controls remain in the artifact.",
        )
    )

    density = _load(results, "cpu-density.json")
    points = {point["provider"]: point for point in density["summary"]["points"]}
    smol = points["smol-branch"]
    podman = points["podman"]
    total = sum(len(row["results"]) for row in density["raw"])
    comparisons = density["summary"]["comparisons"]
    entries.append(
        _paired_entry(
            source="cpu-density.json",
            label="CPU and memory control",
            fanout=int(density["config"]["fanout"]),
            repetitions=int(density["config"]["repetitions"]),
            smol_seconds=float(smol["median_wave_seconds"]),
            control_seconds=float(podman["median_wave_seconds"]),
            correct=total,
            total=total,
            control="Podman",
            memory_ratio=float(comparisons["container_to_smol_physical_memory_ratio"]),
            note="Same content-addressed image, exact outputs, physical host-memory sampling, and initialized 256 MiB state.",
        )
    )

    cloud = _load(results, "cloud-branch-state.json")
    if (
        cloud["checks_passed"] != cloud["checks_total"]
        or not cloud["source_continued"]
        or not cloud["nested_branch_worked"]
    ):
        raise ValueError("cloud-branch-state.json failed its lifecycle gate")

    return {
        "schema_version": 1,
        "validated_at": "2026-09-06",
        "method": "Alternating repeated provider waves with pinned workload identities and mandatory correctness gates.",
        "cloud_validation": {
            "fanout": cloud["fanout"],
            "repetitions": cloud["repetitions"],
            "checks_passed": cloud["checks_passed"],
            "checks_total": cloud["checks_total"],
            "median_batch_branch_seconds": cloud["median_batch_branch_seconds"],
            "median_nested_parent_and_child_seconds": cloud[
                "median_nested_parent_and_child_seconds"
            ],
            "evidence": "cloud-branch-state.json",
        },
        "results": entries,
    }


def _speed_label(value: float) -> str:
    if value >= 1:
        return f"{value:.2f}x faster"
    return f"{1 / value:.2f}x slower"


def render_scorecard(payload: dict[str, Any]) -> str:
    entries = payload["results"]
    cloud = payload["cloud_validation"]
    faster = sum(float(entry["relative_speed"]) >= 1 for entry in entries)
    executions = sum(int(entry["total"]) for entry in entries)
    memory = next(
        float(entry["physical_memory_ratio"])
        for entry in entries
        if "physical_memory_ratio" in entry
    )
    rows = []
    for entry in entries:
        relative = float(entry["relative_speed"])
        class_name = "faster" if relative >= 1 else "slower"
        memory_text = (
            f"; {float(entry['physical_memory_ratio']):.2f}x lower physical-memory pressure"
            if "physical_memory_ratio" in entry
            else ""
        )
        rows.append(
            "<tr>"
            f"<th>{html.escape(str(entry['workload']))}<small>{html.escape(str(entry['note']))}</small></th>"
            f"<td>{entry['fanout']}</td>"
            f"<td>{float(entry['smol_seconds']):.3f}s</td>"
            f"<td>{html.escape(str(entry['control']))}<br>{float(entry['control_seconds']):.3f}s</td>"
            f"<td class='{class_name}'>{_speed_label(relative)}{memory_text}</td>"
            f"<td>{entry['correct']}/{entry['total']}</td>"
            f"<td><a href='{html.escape(str(entry['evidence']))}'>JSON</a></td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smol branching workload scorecard</title>
<style>
:root {{ color-scheme:light dark; font-family:ui-sans-serif,system-ui,sans-serif; --accent:#ff5c35; --panel:color-mix(in srgb,Canvas 94%,CanvasText 6%); }}
body {{ margin:0 auto; max-width:1180px; padding:48px 24px 80px; background:Canvas; color:CanvasText; }}
h1 {{ font-size:clamp(2.2rem,6vw,4.8rem); letter-spacing:-.055em; margin:0 0 12px; }}
p {{ line-height:1.55; }} .lede,.note,small {{ color:color-mix(in srgb,CanvasText 68%,Canvas 32%); }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:12px; margin:28px 0 36px; }}
.card {{ background:var(--panel); border:1px solid color-mix(in srgb,CanvasText 15%,Canvas 85%); border-radius:14px; padding:20px; }}
.card strong {{ color:var(--accent); display:block; font-size:2.15rem; }}
.table {{ overflow-x:auto; }} table {{ border-collapse:collapse; width:100%; }}
th,td {{ border-bottom:1px solid color-mix(in srgb,CanvasText 16%,Canvas 84%); padding:13px 10px; text-align:right; vertical-align:top; white-space:nowrap; }}
th:first-child {{ text-align:left; white-space:normal; min-width:250px; }} th small {{ display:block; font-weight:400; margin-top:5px; }}
thead th {{ font-size:.75rem; text-transform:uppercase; }} .faster {{ color:#38a169; }} .slower {{ color:#d97706; }}
</style></head><body>
<h1>Branch once. Test the real workload.</h1>
<p class="lede">A consolidated, correctness-gated comparison of Smol branches and equivalent Docker or Podman environments. Model inference is excluded because it is shared provider cost.</p>
<div class="cards">
  <div class="card"><strong>{len(entries)}</strong><span>public workload controls</span></div>
  <div class="card"><strong>{faster}/{len(entries)}</strong><span>faster full lifecycle</span></div>
  <div class="card"><strong>{executions}</strong><span>correct executions represented</span></div>
  <div class="card"><strong>{memory:.2f}x</strong><span>lower physical-memory pressure at N=16</span></div>
  <div class="card"><strong>{cloud["checks_passed"]}/{cloud["checks_total"]}</strong><span><a href="{cloud["evidence"]}">Smol Cloud live, batch, and nested branch checks</a></span></div>
</div>
<div class="table"><table><thead><tr><th>Workload</th><th>Fan-out</th><th>Smol</th><th>Control</th><th>Result</th><th>Correct</th><th>Evidence</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>
<p class="note">Times are median full-wave durations. Preparation is performed equivalently and reported separately in each linked artifact. Faster and slower controls are both retained so the workload boundary remains visible.</p>
</body></html>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", type=Path, default=Path("results"))
    parser.add_argument("--json", type=Path, default=Path("results/scorecard.json"))
    parser.add_argument("--html", type=Path, default=Path("results/scorecard.html"))
    args = parser.parse_args()
    payload = build_scorecard(args.results_dir)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.html.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    args.html.write_text(render_scorecard(payload))
    print(f"Wrote {args.json} and {args.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
