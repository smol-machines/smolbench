#!/usr/bin/env python3
"""Render Harbor fan-out benchmark JSON as a standalone visual report."""

from __future__ import annotations

import argparse
import html
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


def median(rows: list[dict[str, Any]], path: tuple[str, ...]) -> float | None:
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


def fmt(value: float | None, suffix: str = "s") -> str:
    return "—" if value is None else f"{value:.2f}{suffix}"


def provider_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rewards = [
        float(reward)
        for row in rows
        for reward in row.get("rewards", [])
        if isinstance(reward, (int, float))
    ]
    memory_bytes = median(rows, ("approximate_peak_host_memory_bytes",))
    return {
        "wall": median(rows, ("wall_seconds",)),
        "harbor": median(rows, ("harbor_seconds",)),
        "setup_p50": median(rows, ("environment_setup_seconds", "p50")),
        "setup_p99": median(rows, ("environment_setup_seconds", "p99")),
        "handoff_p50": median(rows, ("artifact_handoff_seconds", "p50")),
        "verifier_p99": median(rows, ("verifier_seconds", "p99")),
        "memory_mib": memory_bytes / 2**20 if memory_bytes is not None else None,
        "completed": sum(int(row.get("completed", 0)) for row in rows),
        "errors": sum(int(row.get("errors", 0)) for row in rows),
        "reward": statistics.mean(rewards) if rewards else None,
    }


def ratio(baseline: float | None, candidate: float | None) -> str:
    if baseline is None or candidate is None or candidate <= 0:
        return "—"
    return f"{baseline / candidate:.2f}×"


def container_runtime_name(rows: list[dict[str, Any]]) -> str:
    for row in rows:
        runtime = (row.get("software") or {}).get("container_runtime")
        if isinstance(runtime, str) and runtime.lower().startswith("podman"):
            return "Podman"
    return "Docker"


def render_task(task: str, rows: list[dict[str, Any]]) -> str:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["provider"])].append(row)
    summaries = {name: provider_summary(items) for name, items in grouped.items()}
    baseline_name = "docker" if "docker" in summaries else "smol-cold"
    baseline_label = (
        container_runtime_name(grouped["docker"])
        if baseline_name == "docker"
        else baseline_name
    )
    baseline = summaries.get(baseline_name, {})
    branch = summaries.get("smol-branch", {})
    metadata = rows[0]
    cards = ""
    if branch and baseline:
        cards = f"""
        <div class="cards">
          <div class="card"><strong>{ratio(baseline.get("wall"), branch.get("wall"))}</strong><span>end-to-end speed vs {html.escape(baseline_label)}</span></div>
          <div class="card"><strong>{ratio(baseline.get("setup_p50"), branch.get("setup_p50"))}</strong><span>median readiness vs {html.escape(baseline_label)}</span></div>
          <div class="card"><strong>{ratio(baseline.get("setup_p99"), branch.get("setup_p99"))}</strong><span>p99 readiness vs {html.escape(baseline_label)}</span></div>
        </div>"""
    table_rows = []
    for name in sorted(summaries, key=lambda value: (value != "smol-branch", value)):
        item = summaries[name]
        reward = item["reward"]
        display_name = baseline_label if name == baseline_name else name
        table_rows.append(
            "<tr>"
            f"<th>{html.escape(display_name)}</th>"
            f"<td>{fmt(item['wall'])}</td>"
            f"<td>{fmt(item['harbor'])}</td>"
            f"<td>{fmt(item['setup_p50'])}</td>"
            f"<td>{fmt(item['setup_p99'])}</td>"
            f"<td>{fmt(item['handoff_p50'])}</td>"
            f"<td>{fmt(item['verifier_p99'])}</td>"
            f"<td>{fmt(item['memory_mib'], ' MiB')}</td>"
            f"<td>{item['completed']}/{item['completed'] + item['errors']}</td>"
            f"<td>{'—' if reward is None else f'{reward:.3f}'}</td>"
            "</tr>"
        )
    checkpoint = median(rows, ("checkpoint_prepare_seconds",))
    docker_rows = grouped.get("docker", [])
    docker_prepare = median(docker_rows, ("provider_prepare_seconds",))
    if any(row.get("provider_prepare_built") for row in docker_rows):
        docker_prepare_note = (
            f"the equivalently prepared {baseline_label} image took {fmt(docker_prepare)} "
            "to build"
        )
    elif docker_rows and docker_prepare is not None:
        docker_prepare_note = (
            f"the equivalently prepared {baseline_label} image was cached"
        )
    else:
        docker_prepare_note = "no separate Docker preparation was recorded"
    repetitions = len({row.get("repetition") for row in rows})
    return f"""
    <section>
      <h2>{html.escape(task)}</h2>
      <p class="meta">{html.escape(str(metadata.get("dataset", "")))} · {metadata.get("attempts")} trials × {repetitions} repetitions · concurrency {metadata.get("concurrency")} · agent {html.escape(str(metadata.get("agent", "")))}</p>
{cards}
      <div class="table-wrap"><table>
        <thead><tr><th>Provider</th><th>Wall</th><th>Harbor job</th><th>Setup p50</th><th>Setup p99</th><th>Handoff p50</th><th>Verifier p99</th><th>Approx. host memory</th><th>Completed</th><th>Mean reward</th></tr></thead>
        <tbody>{"".join(table_rows)}</tbody>
      </table></div>
      <p class="note">Values are medians across repetitions. The reusable checkpoint took {fmt(checkpoint)} to prepare once; {docker_prepare_note}. Both costs are separate from each fan-out wave. Handoff is Harbor's uninstrumented gap between agent completion and verifier start, which can include artifact collection, transfer and verifier setup. Memory is an approximate process-external MemAvailable delta, not per-VM RSS.</p>
    </section>"""


def render_braintrust(payload: dict[str, Any]) -> str:
    workload = payload["workload"]
    latency = payload["task_latency_seconds"]
    total = len(payload["results"])
    repetitions = payload.get("repetitions", 1)
    smol_wall = (
        payload["branch_batch_seconds"] + payload["branch_to_completed_wall_seconds"]
    )
    docker = payload.get("docker")
    if docker:
        docker_wall = docker["start_to_completed_wall_seconds"]
        docker_latency = docker["task_latency_seconds"]
        speedup_card = f"""
        <div class="card"><strong>{ratio(docker_wall, smol_wall)}</strong><span>Smol relative speed vs warm Docker</span></div>"""
        comparison_rows = f"""
          <tr><th>Smol branches</th><td>{fmt(smol_wall)}</td><td>{fmt(latency.get("p50"))}</td><td>{fmt(latency.get("p99"))}</td><td>{payload["correct"]}/{total}</td></tr>
          <tr><th>Warm Docker</th><td>{fmt(docker_wall)}</td><td>{fmt(docker_latency.get("p50"))}</td><td>{fmt(docker_latency.get("p99"))}</td><td>{docker["correct"]}/{len(docker["results"])}</td></tr>"""
        image_built = docker.get(
            "image_built", bool(docker.get("image_prepare_seconds"))
        )
        if image_built:
            docker_note = (
                " The equivalent Docker image build took "
                f"{fmt(docker.get('image_prepare_seconds'))}."
            )
        else:
            docker_note = " The equivalent Docker image was already cached."
    else:
        speedup_card = ""
        comparison_rows = f"""
          <tr><th>Smol branches</th><td>{fmt(smol_wall)}</td><td>{fmt(latency.get("p50"))}</td><td>{fmt(latency.get("p99"))}</td><td>{payload["correct"]}/{total}</td></tr>"""
        docker_note = ""
    return f"""
    <section>
      <h2>Braintrust bash-agent-evals</h2>
      <p class="meta">Pinned revision {html.escape(str(workload["revision"])[:12])} · {html.escape(str(workload["dataset"]))} · {payload["branch_count"]}-way fan-out × {repetitions} repetitions</p>
      <div class="cards">
        <div class="card"><strong>{payload["branch_batch_seconds"] * 1000:.0f} ms</strong><span>median checkpoint-to-{payload["branch_count"]}-branches</span></div>
        <div class="card"><strong>{payload["branch_to_completed_wall_seconds"] * 1000:.0f} ms</strong><span>median wall time for each query wave</span></div>
        <div class="card"><strong>{payload["correct"]}/{total}</strong><span>outputs matching published answers</span></div>
{speedup_card}
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>Runtime</th><th>Ready + queries</th><th>Task p50</th><th>Task p99</th><th>Correct</th></tr></thead>
        <tbody>{comparison_rows}</tbody>
      </table></div>
      <p class="note">Preparing the live Smol checkpoint took {fmt(payload.get("checkpoint_prepare_seconds"))} once.{docker_note} This model-free gate executes Braintrust's real transformed dataset and native SQLite dependency inside ordinary Node.js. It validates runtime compatibility and branch isolation, not model quality. <a href="{html.escape(str(workload["repository"]))}">View the public workload.</a></p>
    </section>"""


def render(
    rows: list[dict[str, Any]], braintrust: list[dict[str, Any]] | None = None
) -> str:
    by_task: dict[tuple[str, int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            str(row["task"]),
            int(row.get("attempts", 0)),
            int(row.get("concurrency", 0)),
        )
        by_task[key].append(row)
    sections = "".join(
        render_task(task, items) for (task, _, _), items in by_task.items()
    )
    sections += "".join(render_braintrust(payload) for payload in (braintrust or []))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smol branching eval benchmark</title>
<style>
:root {{ color-scheme: light dark; font-family: ui-sans-serif, system-ui, sans-serif; --accent:#ff5c35; --panel:color-mix(in srgb, Canvas 94%, CanvasText 6%); }}
body {{ margin:0 auto; max-width:1120px; padding:48px 24px 80px; background:Canvas; color:CanvasText; }}
h1 {{ font-size:clamp(2rem,6vw,4.5rem); letter-spacing:-.05em; margin:0 0 12px; }}
h2 {{ font-size:2rem; margin:56px 0 8px; }}
p {{ line-height:1.55; }} .lede,.meta,.note {{ color:color-mix(in srgb, CanvasText 68%, Canvas 32%); }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px; margin:24px 0; }}
.card {{ background:var(--panel); border:1px solid color-mix(in srgb, CanvasText 15%, Canvas 85%); border-radius:14px; padding:20px; }}
.card strong {{ color:var(--accent); display:block; font-size:2.2rem; }} .card span {{ font-size:.9rem; }}
.table-wrap {{ overflow-x:auto; }} table {{ border-collapse:collapse; width:100%; }} th,td {{ border-bottom:1px solid color-mix(in srgb, CanvasText 16%, Canvas 84%); padding:12px 10px; text-align:right; white-space:nowrap; }} th:first-child {{ text-align:left; }} thead th {{ font-size:.78rem; text-transform:uppercase; }}
.note {{ font-size:.86rem; }} code {{ background:var(--panel); padding:.15rem .35rem; border-radius:.3rem; }}
</style></head><body>
<h1>Branch once. Evaluate everywhere.</h1>
<p class="lede">A reproducible comparison of clean trial environments created from one running Smol checkpoint versus machine or container startup. Every scored run must pass the task verifier.</p>
{sections}
<p class="note">Generated from the raw Harbor result artifacts by <code>bench/render_results.py</code>.</p>
</body></html>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=Path("results/report.html"))
    args = parser.parse_args()
    rows: list[dict[str, Any]] = []
    braintrust: list[dict[str, Any]] = []
    for source in args.inputs:
        payload = json.loads(source.read_text())
        if isinstance(payload, list):
            rows.extend(payload)
        elif isinstance(payload, dict) and "workload" in payload:
            braintrust.append(payload)
        else:
            raise ValueError(f"unsupported result schema in {source}")
    if not rows and not braintrust:
        raise ValueError("no benchmark rows found")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(rows, braintrust))
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
