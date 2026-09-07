#!/usr/bin/env python3
"""Keep one initialized tau2-bench retail task alive for branching."""

from __future__ import annotations

import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.metadata import version
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROCESS_STARTED = time.perf_counter()
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from loguru import logger  # noqa: E402
from tau2.data_model.message import AssistantMessage, ToolCall  # noqa: E402
from tau2.domains.retail.environment import get_environment, get_tasks  # noqa: E402
from tau2.evaluator.evaluator_env import EnvironmentEvaluator  # noqa: E402


CASES = json.loads((Path(__file__).parent / "tau2_cases.json").read_text())
TASK = next(task for task in get_tasks(None) if task.id == CASES["task_id"])
ENV = get_environment()
INITIAL_DB_HASH = ENV.get_db_hash()
ACTION_COUNT = 0
INITIALIZE_SECONDS = time.perf_counter() - PROCESS_STARTED

logger.remove()


def customer_state() -> dict[str, object]:
    user = ENV.make_tool_call("get_user_details", user_id=CASES["user_id"])
    order = ENV.make_tool_call("get_order_details", order_id=CASES["order_id"])
    return {
        "address": user.address.model_dump(),
        "order_status": order.status,
    }


INITIAL_STATE = customer_state()


def health() -> dict[str, object]:
    return {
        "ready": True,
        "benchmark": CASES["benchmark"],
        "domain": CASES["domain"],
        "task_id": CASES["task_id"],
        "source_revision": CASES["source_revision"],
        "tau2_version": version("tau2"),
        "worker_initialize_seconds": INITIALIZE_SECONDS,
        "initial_db_hash": INITIAL_DB_HASH,
        "initial_state": INITIAL_STATE,
        "action_count": ACTION_COUNT,
    }


def candidate_by_label(label: str) -> dict[str, object]:
    for candidate in CASES["candidates"]:
        if candidate["label"] == label:
            return candidate
    raise ValueError(f"unknown candidate: {label}")


def candidate_actions(candidate: dict[str, object]) -> list[dict[str, object]]:
    expected = TASK.evaluation_criteria.actions
    if expected is None:
        raise RuntimeError("task has no official actions")
    actions = [
        {
            "name": action.name,
            "arguments": action.arguments,
            "requestor": action.requestor,
        }
        for action in expected[:-1]
    ]
    decision = candidate["decision"]
    if decision == "official":
        action = expected[-1]
        actions.append(
            {
                "name": action.name,
                "arguments": action.arguments,
                "requestor": action.requestor,
            }
        )
    elif isinstance(decision, dict):
        actions.append(
            {
                "name": decision["name"],
                "arguments": decision["arguments"],
                "requestor": "assistant",
            }
        )
    return actions


def perform_candidate(label: str) -> dict[str, object]:
    global ACTION_COUNT
    if ACTION_COUNT:
        raise RuntimeError("this task state has already evaluated a candidate")
    candidate = candidate_by_label(label)
    started = time.perf_counter()
    pre_action_health = health()
    starting_hash = ENV.get_db_hash()
    trajectory = []
    calls = candidate_actions(candidate)
    errors = []
    for index, action in enumerate(calls):
        tool_call = ToolCall(
            id=f"{label}-{index}",
            name=action["name"],
            arguments=action["arguments"],
            requestor=action["requestor"],
        )
        tool_result = ENV.get_response(tool_call)
        trajectory.extend(
            [
                AssistantMessage(
                    role="assistant", content=None, tool_calls=[tool_call]
                ),
                tool_result,
            ]
        )
        if tool_result.error:
            errors.append(tool_result.content)

    reward = EnvironmentEvaluator.calculate_reward(
        environment_constructor=get_environment,
        task=TASK,
        full_trajectory=trajectory,
    )
    ACTION_COUNT += 1
    state = customer_state()
    return {
        "label": label,
        "title": candidate["title"],
        "expected_reward": candidate["expected_reward"],
        "reward": reward.reward,
        "db_match": reward.db_check.db_match if reward.db_check else None,
        "reward_basis": [item.value for item in reward.reward_basis or []],
        "initial_db_hash": INITIAL_DB_HASH,
        "starting_db_hash": starting_hash,
        "final_db_hash": ENV.get_db_hash(),
        "action_count_before": 0,
        "action_count_after": ACTION_COUNT,
        "tool_calls": [action["name"] for action in calls],
        "tool_errors": errors,
        "state": state,
        "worker_seconds": time.perf_counter() - started,
        "pre_action_health": pre_action_health,
        "health": health(),
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.reply(200, health())
            return
        if parsed.path != "/candidate":
            self.reply(404, {"error": "not found"})
            return
        label = parse_qs(parsed.query).get("label", [""])[0]
        if not label:
            self.reply(400, {"error": "label is required"})
            return
        try:
            self.reply(200, perform_candidate(label))
        except Exception as error:
            self.reply(500, {"error": f"{type(error).__name__}: {error}"})

    def reply(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", metavar="LABEL")
    args = parser.parse_args()
    if args.once:
        print(json.dumps(perform_candidate(args.once), separators=(",", ":")))
        return
    HTTPServer(("127.0.0.1", 8767), Handler).serve_forever(poll_interval=0.05)


if __name__ == "__main__":
    main()
