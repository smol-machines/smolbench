#!/usr/bin/env python3
"""Keep one ordinary BrowserGym MiniWoB task alive for branching."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import browsergym.miniwob  # noqa: F401 - registers the Gym environments
import gymnasium as gym


ENV_ID = "browsergym/miniwob.click-test"
SEED = 123
ENV = None
TARGET_BID = None
INITIAL_SCREENSHOT_SHA256 = None
ACTION_COUNT = 0


def screenshot_bytes(observation: dict[str, object]) -> bytes:
    """Encode BrowserGym's RGB observation without a browser-specific shortcut."""
    from PIL import Image

    image = Image.fromarray(observation["screenshot"])
    from io import BytesIO

    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def target_bid(observation: dict[str, object]) -> str:
    tree = observation["axtree_object"]
    for node in tree["nodes"]:
        if (
            node.get("role", {}).get("value") == "button"
            and node.get("name", {}).get("value") == "Click Me!"
        ):
            bid = node.get("browsergym_id")
            if isinstance(bid, str):
                return bid
    raise RuntimeError("BrowserGym did not expose the target button")


def initialize() -> tuple[object, dict[str, object], float]:
    started = time.perf_counter()
    environment = gym.make(ENV_ID, headless=True)
    observation, _ = environment.reset(seed=SEED)
    return environment, observation, time.perf_counter() - started


def health() -> dict[str, object]:
    return {
        "ready": ENV is not None,
        "environment": ENV_ID,
        "seed": SEED,
        "goal": "Click the button.",
        "target_bid": TARGET_BID,
        "initial_screenshot_sha256": INITIAL_SCREENSHOT_SHA256,
        "action_count": ACTION_COUNT,
    }


def perform_action(action: str, label: str) -> dict[str, object]:
    global ACTION_COUNT
    if ENV is None:
        raise RuntimeError("BrowserGym environment is not ready")
    started = time.perf_counter()
    before = ACTION_COUNT
    observation, reward, terminated, truncated, _ = ENV.step(action)
    ACTION_COUNT += 1
    screenshot = screenshot_bytes(observation)
    return {
        "label": label,
        "action": action,
        "reward": float(reward),
        "terminated": bool(terminated),
        "truncated": bool(truncated),
        "last_action": observation["last_action"],
        "last_action_error": observation["last_action_error"],
        "action_count_before": before,
        "action_count_after": ACTION_COUNT,
        "initial_screenshot_sha256": INITIAL_SCREENSHOT_SHA256,
        "screenshot_sha256": hashlib.sha256(screenshot).hexdigest(),
        "screenshot_base64": base64.b64encode(screenshot).decode(),
        "worker_action_seconds": time.perf_counter() - started,
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.reply(200, health())
            return
        if parsed.path != "/action":
            self.reply(404, {"error": "not found"})
            return
        query = parse_qs(parsed.query)
        action = query.get("action", [""])[0]
        label = query.get("label", [""])[0]
        if not action or not label:
            self.reply(400, {"error": "action and label are required"})
            return
        try:
            self.reply(200, perform_action(action, label))
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
    global ENV, TARGET_BID, INITIAL_SCREENSHOT_SHA256
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", nargs=2, metavar=("LABEL", "ACTION"))
    args = parser.parse_args()

    ENV, observation, launch_seconds = initialize()
    TARGET_BID = target_bid(observation)
    INITIAL_SCREENSHOT_SHA256 = hashlib.sha256(
        screenshot_bytes(observation)
    ).hexdigest()
    if args.once:
        label, action = args.once
        result = perform_action(action.replace("{target_bid}", TARGET_BID), label)
        result["browsergym_launch_seconds"] = launch_seconds
        print(json.dumps(result, separators=(",", ":")))
        ENV.close()
        return
    HTTPServer(("127.0.0.1", 8766), Handler).serve_forever(poll_interval=0.05)


if __name__ == "__main__":
    main()
