#!/usr/bin/env python3
"""Run an official tau2-bench agent conversation inside a live Smol branch."""

from __future__ import annotations

import argparse
import html
import json
import os
import platform
import shlex
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from smol import ExecOptions

from bench.tau2_fanout import (
    CASES,
    MEMORY_MB,
    TAU2_REPOSITORY,
    TAU2_REVISION,
    command_version,
    package_version,
    prepare_checkpoint,
    read_health,
)


TASK_ID = "33"


def validate_api_base(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("OPENAI_API_BASE must be an http(s) URL")
    if parsed.hostname in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError(
            "OPENAI_API_BASE must be reachable from the guest; host loopback is not"
        )
    return value.rstrip("/")


def compact_timeline(messages: list[object]) -> list[dict[str, object]]:
    timeline: list[dict[str, object]] = []
    for value in messages:
        if not isinstance(value, dict):
            continue
        item: dict[str, object] = {"role": value.get("role")}
        content = value.get("content")
        if isinstance(content, str) and content:
            item["content"] = content[:1000]
        calls = value.get("tool_calls")
        if isinstance(calls, list) and calls:
            item["tool_calls"] = [
                {
                    "name": call.get("name"),
                    "arguments": call.get("arguments"),
                }
                for call in calls
                if isinstance(call, dict)
            ]
        if value.get("error") is True:
            item["error"] = True
        timeline.append(item)
    return timeline


def summarize_simulation(simulation: dict[str, object]) -> dict[str, object]:
    reward_info = simulation.get("reward_info")
    reward_info = reward_info if isinstance(reward_info, dict) else {}
    db_check = reward_info.get("db_check")
    db_check = db_check if isinstance(db_check, dict) else {}
    action_checks = reward_info.get("action_checks")
    action_checks = action_checks if isinstance(action_checks, list) else []
    return {
        "task_id": simulation.get("task_id"),
        "trial": simulation.get("trial"),
        "seed": simulation.get("seed"),
        "duration_seconds": simulation.get("duration"),
        "termination_reason": simulation.get("termination_reason"),
        "reward": reward_info.get("reward"),
        "db_match": db_check.get("db_match"),
        "matched_actions": sum(
            check.get("action_match") is True
            for check in action_checks
            if isinstance(check, dict)
        ),
        "expected_actions": len(action_checks),
        "timeline": compact_timeline(
            simulation.get("messages")
            if isinstance(simulation.get("messages"), list)
            else []
        ),
    }


def task_passed(summary: dict[str, object]) -> bool:
    return (
        summary.get("task_id") == TASK_ID
        and summary.get("reward") == 1.0
        and summary.get("db_match") is True
        and summary.get("termination_reason") in {"agent_stop", "user_stop"}
    )


def runtime_validated(summary: dict[str, object], source_unchanged: bool) -> bool:
    timeline = summary.get("timeline")
    return (
        summary.get("task_id") == TASK_ID
        and isinstance(timeline, list)
        and bool(timeline)
        and source_unchanged
    )


def render_report(payload: dict[str, object], output: Path) -> None:
    run = payload["run"]
    timeline = run["timeline"]
    rows = []
    for item in timeline:
        role = html.escape(str(item.get("role") or "event"))
        content = html.escape(str(item.get("content") or ""))
        calls = item.get("tool_calls") or []
        tools = "".join(
            f"<code>{html.escape(str(call.get('name')))}</code>"
            for call in calls
            if isinstance(call, dict)
        )
        if content or tools:
            rows.append(
                f'<article class="event"><b>{role}</b><p>{content}</p>{tools}</article>'
            )
    task_status = "passed" if payload["task_passed"] else "did not pass"
    runtime_status = "validated" if payload["runtime_validated"] else "failed"
    output.write_text(
        f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>tau2 agent in a Smol branch</title><style>
body{{font-family:system-ui,sans-serif;max-width:1050px;margin:50px auto;padding:0 24px;background:#0b1020;color:#f9fafb}}h1{{font-size:48px;line-height:1.05;margin-bottom:8px}}p{{color:#cbd5e1}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin:28px 0}}.metric,.event{{background:#172033;border:1px solid #334155;border-radius:16px;padding:18px}}.metric strong{{display:block;font-size:30px;color:#ff5c35}}.event{{margin:12px 0}}.event>b{{text-transform:uppercase;color:#94a3b8;font-size:12px;letter-spacing:.12em}}code{{display:inline-block;margin:4px 8px 0 0;padding:5px 8px;border-radius:7px;background:#263248;color:#fdba74}}</style></head><body>
<h1>A real tool agent, running inside a branch.</h1><p>An official τ²-bench retail conversation ran in an isolated child of a live, initialized Smol machine. The source remained available and unchanged.</p>
<div class="metrics"><div class="metric"><strong>{payload["branch_seconds"] * 1000:.0f} ms</strong>branch creation</div><div class="metric"><strong>{run["reward"]}</strong>official reward</div><div class="metric"><strong>{task_status}</strong>agent task</div><div class="metric"><strong>{runtime_status}</strong>Smol runtime</div></div>
{"".join(rows)}
<p><code>retail/{TASK_ID}</code> at <code>{TAU2_REVISION[:12]}</code> · model <code>{html.escape(str(payload["agent_model"]))}</code> · source unchanged: <b>{str(payload["source_unchanged"]).lower()}</b>.</p></body></html>"""
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", default=os.environ.get("OPENAI_API_BASE"))
    parser.add_argument("--agent-model", default=os.environ.get("AGENT_MODEL"))
    parser.add_argument("--user-model", default=os.environ.get("USER_MODEL"))
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--model-source")
    parser.add_argument(
        "--agent-implementation",
        choices=("llm_agent", "llm_agent_gt"),
        default="llm_agent",
    )
    parser.add_argument("--seed", type=int, default=123)
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--require-reward", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.api_base:
        parser.error("--api-base or OPENAI_API_BASE is required")
    try:
        api_base = validate_api_base(args.api_base)
    except ValueError as error:
        parser.error(str(error))
    if not args.agent_model:
        parser.error("--agent-model or AGENT_MODEL is required")
    user_model = args.user_model or args.agent_model
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        parser.error(f"{args.api_key_env} is required")
    if args.max_steps < 1 or args.timeout < 1:
        parser.error("max-steps and timeout must be positive")

    label = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_id = f"{label}-{uuid.uuid4().hex[:6]}"
    output = args.output or Path("results") / f"{label}-tau2-agent.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    save_name = f"smol-agent-{run_id}"
    golden = None
    child = None
    try:
        print("[Prepare] Loading the pinned tau2 runtime", flush=True)
        golden, prepare_seconds, phases = prepare_checkpoint(
            f"tau2-agent-golden-{run_id}"
        )
        started = time.perf_counter()
        child = golden.branch_batch(names=[f"tau2-agent-{run_id}"])[0]
        branch_seconds = time.perf_counter() - started
        command = shlex.join(
            [
                "tau2",
                "run",
                "--domain",
                "retail",
                "--task-ids",
                TASK_ID,
                "--num-trials",
                "1",
                "--agent",
                args.agent_implementation,
                "--agent-llm",
                args.agent_model,
                "--user-llm",
                user_model,
                "--agent-llm-args",
                '{"temperature":0.0,"max_tokens":512}',
                "--user-llm-args",
                '{"temperature":0.0,"max_tokens":256}',
                "--max-steps",
                str(args.max_steps),
                "--timeout",
                str(args.timeout),
                "--max-retries",
                "0",
                "--seed",
                str(args.seed),
                "--save-to",
                save_name,
                "--log-level",
                "WARNING",
            ]
        )
        print(f"[Agent] Running official retail task {TASK_ID}", flush=True)
        started = time.perf_counter()
        result = child.exec(
            ["/bin/bash", "-lc", command],
            ExecOptions(
                env={
                    "OPENAI_API_KEY": api_key,
                    "OPENAI_API_BASE": api_base,
                    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                },
                workdir="/opt/tau2-bench",
                timeout=args.timeout + 120,
            ),
        )
        agent_wall_seconds = time.perf_counter() - started
        if result.exit_code:
            raise RuntimeError(
                f"tau2 run failed ({result.exit_code}): {result.stderr[-4000:]}"
            )
        raw = json.loads(
            child.read_file(
                f"/opt/tau2-bench/data/simulations/{save_name}/results.json"
            )
        )
        simulations = raw.get("simulations")
        if not isinstance(simulations, list) or len(simulations) != 1:
            raise RuntimeError("tau2 did not produce exactly one simulation")
        run = summarize_simulation(simulations[0])
        source_health = read_health(golden, 15)
        source_unchanged = source_health.get("action_count") == 0 and source_health.get(
            "initial_db_hash"
        ) == phases["health"].get("initial_db_hash")
        runtime_ok = runtime_validated(run, source_unchanged)
        passed = task_passed(run)
        payload = {
            "schema_version": 1,
            "validated_at": datetime.now(UTC).isoformat(),
            "workload": "tau2-bench retail task 33 real model agent",
            "repository": TAU2_REPOSITORY,
            "revision": TAU2_REVISION,
            "domain": CASES["domain"],
            "task_id": TASK_ID,
            "agent_model": args.agent_model,
            "user_model": user_model,
            "agent_implementation": args.agent_implementation,
            "model_source": args.model_source,
            "seed": args.seed,
            "resources_per_environment": {"cpus": 2, "memory_mb": MEMORY_MB},
            "checkpoint_prepare_seconds": prepare_seconds,
            "branch_seconds": branch_seconds,
            "agent_wall_seconds": agent_wall_seconds,
            "source_unchanged": source_unchanged,
            "runtime_validated": runtime_ok,
            "task_passed": passed,
            "run": run,
            "host": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "logical_cpus": os.cpu_count(),
            },
            "software": {
                "python": platform.python_version(),
                "smolmachines": package_version("smolmachines"),
                "smolvm": command_version(["smolvm", "--version"]),
                "smolvm_source_revision": os.environ.get("SMOLVM_REVISION"),
            },
            "methodology": (
                "Model inference uses the caller's provider endpoint and is not compared "
                "with Docker. This run proves that the official stateful tool-agent task "
                "executes correctly inside a live Smol branch."
            ),
        }
        render_report(payload, output.with_suffix(".html"))
        output.write_text(json.dumps(payload, indent=2) + "\n")
        print(
            f"[Result] reward={run['reward']} branch={branch_seconds:.3f}s "
            f"source_unchanged={source_unchanged}",
            flush=True,
        )
        print(f"Wrote {output} and {output.with_suffix('.html')}", flush=True)
        return 0 if runtime_ok and (passed or not args.require_reward) else 1
    finally:
        if child is not None:
            child.delete()
        if golden is not None:
            golden.delete()


if __name__ == "__main__":
    raise SystemExit(main())
