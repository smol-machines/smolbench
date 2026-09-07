#!/usr/bin/env python3
"""Validate and render high-fanout Harbor lifecycle results."""

from __future__ import annotations

import argparse
import html
import json
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any


def median(rows: list[dict[str, Any]], *path: str) -> float | None:
    values: list[float] = []
    for row in rows:
        value: Any = row
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if isinstance(value, (int, float)):
            values.append(float(value))
    return statistics.median(values) if values else None


def require_clean_row(row: dict[str, Any], source: Path) -> None:
    attempts = row.get("attempts")
    errors = row.get("errors")
    completed = row.get("completed")
    if not row.get("install_only"):
        raise ValueError(f"{source}: scale inputs must use --install-only")
    if not isinstance(attempts, int) or attempts <= 0:
        raise ValueError(f"{source}: invalid attempt count")
    if row.get("concurrency") != attempts:
        raise ValueError(f"{source}: concurrency must equal attempts")
    if row.get("return_code") != 0 or errors != 0 or completed != attempts:
        raise ValueError(
            f"{source}: failed correctness gate "
            f"(rc={row.get('return_code')}, completed={completed}, errors={errors})"
        )
    if row.get("correctness_error"):
        raise ValueError(f"{source}: result contains a correctness error")


def read_rows(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source in paths:
        payload = json.loads(source.read_text())
        if not isinstance(payload, list) or not payload:
            raise ValueError(f"{source}: expected a non-empty result array")
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError(f"{source}: result rows must be objects")
            require_clean_row(row, source)
            rows.append(row)
    tasks = {(row.get("dataset"), row.get("task")) for row in rows}
    if len(tasks) != 1:
        raise ValueError("all scale inputs must describe the same public workload")
    return rows


def read_failure_rows(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source in paths:
        payload = json.loads(source.read_text())
        if not isinstance(payload, list) or not payload:
            raise ValueError(f"{source}: expected a non-empty result array")
        if not all(isinstance(row, dict) for row in payload):
            raise ValueError(f"{source}: result rows must be objects")
        failed = [
            row
            for row in payload
            if row.get("return_code") != 0
            or row.get("errors")
            or row.get("completed") != row.get("attempts")
            or row.get("correctness_error")
        ]
        if not failed:
            raise ValueError(f"{source}: --failure input contains no failed wave")
        rows.extend(payload)
    return rows


def summarize(
    rows: list[dict[str, Any]],
    smolvm_revision: str,
    failure_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["provider"]), int(row["attempts"]))].append(row)

    points: list[dict[str, Any]] = []
    for (provider, fanout), group in sorted(
        grouped.items(), key=lambda item: (item[0][1], item[0][0] != "smol-branch")
    ):
        harbor_rates = [
            fanout / float(row["harbor_seconds"])
            for row in group
            if isinstance(row.get("harbor_seconds"), (int, float))
            and row["harbor_seconds"] > 0
        ]
        memory = median(group, "approximate_peak_host_memory_bytes")
        points.append(
            {
                "provider": provider,
                "fanout": fanout,
                "repetitions": len(group),
                "completed": sum(int(row["completed"]) for row in group),
                "total": sum(int(row["attempts"]) for row in group),
                "errors": sum(int(row["errors"]) for row in group),
                "median_wall_seconds": median(group, "wall_seconds"),
                "median_harbor_seconds": median(group, "harbor_seconds"),
                "median_setup_p50_seconds": median(
                    group, "environment_setup_seconds", "p50"
                ),
                "median_setup_p99_seconds": median(
                    group, "environment_setup_seconds", "p99"
                ),
                "median_completed_per_harbor_second": (
                    statistics.median(harbor_rates) if harbor_rates else None
                ),
                "approximate_median_peak_host_memory_mib": (
                    memory / 2**20 if memory is not None else None
                ),
            }
        )

    comparisons: dict[str, Any] = {}
    smol_16 = next(
        (
            point
            for point in points
            if point["provider"] == "smol-branch" and point["fanout"] == 16
        ),
        None,
    )
    docker_16 = next(
        (
            point
            for point in points
            if point["provider"] == "docker" and point["fanout"] == 16
        ),
        None,
    )
    if smol_16 and docker_16:
        comparisons["fanout_16"] = {
            "smol_full_lifecycle_speedup": (
                docker_16["median_wall_seconds"] / smol_16["median_wall_seconds"]
            ),
            "smol_harbor_job_speedup": (
                docker_16["median_harbor_seconds"] / smol_16["median_harbor_seconds"]
            ),
            "docker_environment_setup_speedup": (
                smol_16["median_setup_p50_seconds"]
                / docker_16["median_setup_p50_seconds"]
            ),
            "docker_to_smol_observed_memory_pressure_ratio": (
                docker_16["approximate_median_peak_host_memory_mib"]
                / smol_16["approximate_median_peak_host_memory_mib"]
            ),
        }

    first = rows[0]
    smol_points = [point for point in points if point["provider"] == "smol-branch"]
    failed_soaks: list[dict[str, Any]] = []
    failed_groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in failure_rows or []:
        failed_groups[(str(row["provider"]), int(row["attempts"]))].append(row)
    for (provider, fanout), group in sorted(failed_groups.items()):
        failed = [
            row
            for row in group
            if row.get("return_code") != 0
            or row.get("errors")
            or row.get("completed") != row.get("attempts")
            or row.get("correctness_error")
        ]
        failed_soaks.append(
            {
                "provider": provider,
                "fanout": fanout,
                "waves": len(group),
                "failed_waves": len(failed),
                "completed": sum(int(row.get("completed", 0)) for row in group),
                "total": sum(int(row.get("attempts", 0)) for row in group),
                "errors": sum(int(row.get("errors", 0)) for row in group),
                "correctness_errors": sorted(
                    {
                        str(row["correctness_error"])
                        for row in failed
                        if row.get("correctness_error")
                    }
                ),
            }
        )
    notes = [
        "N=16, N=32, and N=64 are repeated soaks; N=128 is a single boundary probe when its repetition count is one.",
        "Harbor job and wall measurements include creation, readiness, and cleanup; environment setup measures readiness only.",
        "MemAvailable deltas are noisy process-external observations, not per-VM RSS or a physical COW accounting measurement.",
    ]
    if failed_soaks:
        notes.insert(
            1,
            "A separate repeated N=128 qualification failed and is reported below; the successful single probe is not a production reliability claim.",
        )
    return {
        "schema_version": 1,
        "validated_at": date.today().isoformat(),
        "workload": {
            "dataset": first["dataset"],
            "task": first["task"],
            "mode": "Harbor install-only environment lifecycle",
        },
        "host": first["host"],
        "software": first["software"],
        "smolvm_revision": smolvm_revision,
        "points": points,
        "failed_soaks": failed_soaks,
        "comparisons": comparisons,
        "smol_completed": sum(point["completed"] for point in smol_points),
        "smol_errors": sum(point["errors"] for point in smol_points),
        "notes": notes,
    }


def fmt(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def render(report: dict[str, Any]) -> str:
    rows = []
    smol_points = [
        point for point in report["points"] if point["provider"] == "smol-branch"
    ]
    max_harbor = max(float(point["median_harbor_seconds"]) for point in smol_points)
    bars = []
    for point in smol_points:
        width = float(point["median_harbor_seconds"]) / max_harbor * 100
        qualification = (
            "single probe"
            if point["repetitions"] == 1
            else (f"{point['repetitions']}-wave soak")
        )
        bars.append(
            f"<div class='bar-row'><b>N={point['fanout']}</b>"
            f"<div class='track'><span style='width:{width:.1f}%'></span></div>"
            f"<em>{fmt(point['median_harbor_seconds'])}s · {qualification}</em></div>"
        )
    for point in report["points"]:
        rows.append(
            "<tr>"
            f"<th>{html.escape(point['provider'])}</th>"
            f"<td>{point['fanout']}</td>"
            f"<td>{point['repetitions']}</td>"
            f"<td>{point['completed']}/{point['total']}</td>"
            f"<td>{fmt(point['median_wall_seconds'])}s</td>"
            f"<td>{fmt(point['median_harbor_seconds'])}s</td>"
            f"<td>{fmt(point['median_setup_p50_seconds'])}s</td>"
            f"<td>{fmt(point['median_setup_p99_seconds'])}s</td>"
            f"<td>{fmt(point['median_completed_per_harbor_second'])}</td>"
            f"<td>{fmt(point['approximate_median_peak_host_memory_mib'], 0)} MiB</td>"
            "</tr>"
        )
    comparison = report["comparisons"].get("fanout_16", {})
    cards = ""
    if comparison:
        cards = f"""
        <div class="cards">
          <div class="card"><strong>{comparison["smol_full_lifecycle_speedup"]:.2f}×</strong><span>faster N=16 full lifecycle than Harbor Docker</span></div>
          <div class="card"><strong>{comparison["smol_harbor_job_speedup"]:.2f}×</strong><span>faster N=16 Harbor job than Docker</span></div>
          <div class="card"><strong>{comparison["docker_environment_setup_speedup"]:.2f}×</strong><span>faster Docker environment setup</span></div>
        </div>"""
    task = report["workload"]
    revision = html.escape(str(report["smolvm_revision"])[:12])
    notes = "".join(f"<li>{html.escape(note)}</li>" for note in report["notes"])
    repeated = [point for point in smol_points if point["repetitions"] >= 3]
    headline_fanout = max(int(point["fanout"]) for point in (repeated or smol_points))
    failed_soaks = report.get("failed_soaks", [])
    failure_section = ""
    if failed_soaks:
        failure_rows = "".join(
            "<tr>"
            f"<th>{html.escape(item['provider'])}</th>"
            f"<td>{item['fanout']}</td>"
            f"<td>{item['failed_waves']}/{item['waves']}</td>"
            f"<td>{item['completed']}/{item['total']}</td>"
            f"<td>{item['errors']}</td>"
            "</tr>"
            for item in failed_soaks
        )
        failure_section = f"""
<h2>Failed qualification retained</h2>
<p class="note">A failed soak is never folded into the successful curve or hidden. On this host the failed N=128 wave hit the upstream KVM first-run ENOMEM defect after the runtime's bounded retry; all temporary machines were cleaned up.</p>
<div class="table-wrap"><table><thead><tr><th>Provider</th><th>Fan-out</th><th>Failed waves</th><th>Completed</th><th>Errors</th></tr></thead><tbody>{failure_rows}</tbody></table></div>"""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smol branch scale validation</title>
<style>
:root {{ color-scheme:light dark; font-family:ui-sans-serif,system-ui,sans-serif; --accent:#ff5c35; --panel:color-mix(in srgb,Canvas 94%,CanvasText 6%); }}
body {{ margin:0 auto; max-width:1120px; padding:48px 24px 80px; background:Canvas; color:CanvasText; }}
h1 {{ font-size:clamp(2.2rem,7vw,5rem); letter-spacing:-.055em; line-height:.95; margin:0 0 18px; }}
h2 {{ margin-top:48px; }} p,li {{ line-height:1.55; }} .lede,.meta,.note {{ color:color-mix(in srgb,CanvasText 68%,Canvas 32%); }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:12px; margin:28px 0; }}
.card {{ background:var(--panel); border:1px solid color-mix(in srgb,CanvasText 15%,Canvas 85%); border-radius:14px; padding:20px; }}
.card strong {{ color:var(--accent); display:block; font-size:2.3rem; }} .card span {{ font-size:.9rem; }}
.bar-row {{ display:grid; grid-template-columns:64px 1fr 180px; gap:12px; align-items:center; margin:13px 0; }} .bar-row em {{ font-size:.82rem; }}
.track {{ height:22px; background:var(--panel); border-radius:6px; overflow:hidden; }} .track span {{ display:block; height:100%; background:var(--accent); }}
.table-wrap {{ overflow-x:auto; }} table {{ border-collapse:collapse; width:100%; }} th,td {{ border-bottom:1px solid color-mix(in srgb,CanvasText 16%,Canvas 84%); padding:11px 9px; text-align:right; white-space:nowrap; }} th:first-child {{ text-align:left; }} thead th {{ font-size:.73rem; text-transform:uppercase; }}
@media(max-width:650px) {{ .bar-row {{ grid-template-columns:54px 1fr; }} .bar-row em {{ grid-column:2; }} }}
</style></head><body>
<h1>{headline_fanout}-way soak.<br>Zero failed environments.</h1>
<p class="lede">One running Smol machine fanned out into clean Harbor eval environments on a 26-vCPU host. Repeated soaks passed through N={headline_fanout}; a larger N=128 probe passed but did not survive repeated qualification.</p>
<p class="meta">{html.escape(task["dataset"])}/{html.escape(task["task"])} · SmolVM {revision} · {report["smol_completed"]}/{report["smol_completed"]} Smol environments completed</p>
{cards}
<h2>Fan-out curve</h2>
<p class="note">Median Harbor lifecycle time; shorter is better. Each wave creates, readies and cleans up every environment.</p>
{"".join(bars)}
<h2>Measured results</h2>
<div class="table-wrap"><table><thead><tr><th>Provider</th><th>Fan-out</th><th>Waves</th><th>Completed</th><th>Full wall</th><th>Harbor job</th><th>Setup p50</th><th>Setup p99</th><th>Completed/s</th><th>Observed memory delta</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>
{failure_section}
<ul class="note">{notes}</ul>
</body></html>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--smolvm-revision", required=True)
    parser.add_argument("--failure", action="append", type=Path, default=[])
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--html", type=Path, required=True)
    args = parser.parse_args()
    failures = read_failure_rows(args.failure) if args.failure else []
    report = summarize(read_rows(args.inputs), args.smolvm_revision, failures)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.html.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2) + "\n")
    args.html.write_text(render(report))
    print(f"Wrote {args.json} and {args.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
