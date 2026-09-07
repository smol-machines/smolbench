#!/usr/bin/env python3
"""Materialize and run one pinned-shape SWE-bench Verified task."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path


DATASET = "swe-bench/swe-bench-verified"
DEFAULT_TASK = "django__django-10999"
PINNED_IMAGES = {
    DEFAULT_TASK: (
        "swebench/sweb.eval.x86_64.django_1776_django-10999:latest",
        "sha256:22a35ae325dc5abdd397b1d474d66aad62da6d23c4e9c8763478e82bcf63af7b",
    )
}


def tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(str(item.relative_to(path)).encode())
        digest.update(b"\0")
        digest.update(item.read_bytes())
    return digest.hexdigest()


def dockerfile_base(path: Path) -> str:
    for line in path.read_text().splitlines():
        match = re.match(r"^\s*FROM\s+([^\s]+)", line, re.IGNORECASE)
        if match:
            return match.group(1)
    raise RuntimeError(f"no FROM image found in {path}")


def materialize_task(source: Path, destination_root: Path) -> tuple[Path, str, str]:
    source_digest = tree_digest(source)
    image = dockerfile_base(source / "environment" / "Dockerfile")
    if source.name in PINNED_IMAGES:
        expected_image, image_digest = PINNED_IMAGES[source.name]
        if image != expected_image:
            raise RuntimeError(
                f"{source.name} base image changed from {expected_image} to {image}"
            )
        image = f"{image}@{image_digest}"
    materialized_digest = hashlib.sha256(
        f"{source_digest}\0{image}".encode()
    ).hexdigest()
    destination = destination_root / f"{source.name}-{materialized_digest[:12]}"
    if not (destination / "task.toml").is_file():
        if destination.exists():
            raise RuntimeError(f"incomplete materialized task at {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}-{uuid.uuid4().hex[:6]}")
        shutil.copytree(source, temporary)
        definition = (temporary / "task.toml").read_text()
        definition, count = re.subn(
            r"(?m)^(\[environment\]\s*)$",
            lambda match: match.group(1) + "\n" + f"docker_image = {json.dumps(image)}",
            definition,
            count=1,
        )
        if count != 1:
            shutil.rmtree(temporary)
            raise RuntimeError("task.toml has no [environment] section")
        (temporary / "task.toml").write_text(definition)
        temporary.rename(destination)
    return destination, source_digest, image


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", choices=(DEFAULT_TASK,), default=DEFAULT_TASK)
    parser.add_argument("--fanout", type=int, default=4)
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/swebench"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.fanout, args.parallel, args.repetitions) < 1:
        parser.error("fanout, parallel, and repetitions must be positive")
    if args.parallel > args.fanout:
        parser.error("parallel cannot exceed fanout")

    dataset_dir = args.cache_dir.resolve() / "swe-bench-verified"
    source = dataset_dir / args.task
    if not (source / "task.toml").is_file():
        harbor = shutil.which("harbor")
        if harbor is None:
            raise RuntimeError("Harbor is not installed; run `uv sync --extra dev`")
        args.cache_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                harbor,
                "dataset",
                "download",
                DATASET,
                "--output-dir",
                str(args.cache_dir.resolve()),
            ],
            check=True,
        )
    if not (source / "task.toml").is_file():
        raise RuntimeError(f"{args.task!r} was not found in {DATASET}")

    task, digest, image = materialize_task(
        source, args.cache_dir.resolve() / "materialized"
    )
    print(
        f"Using {DATASET}/{args.task} sha256:{digest} on {image}",
        flush=True,
    )
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    output = args.output or Path("results") / f"{stamp}-swebench-verified.json"
    command = [
        sys.executable,
        str(Path(__file__).with_name("harbor_fanout.py")),
        "--dataset",
        f"{DATASET} (task sha256:{digest})",
        "--task-path",
        str(task),
        "--task-label",
        args.task,
        "--attempts",
        str(args.fanout),
        "--concurrency",
        str(args.parallel),
        "--repetitions",
        str(args.repetitions),
        "--providers",
        "smol-branch",
        "docker",
        "--prepare-script",
        str(Path(__file__).parent / "warmups" / "swebench_django_10999.sh"),
        "--output",
        str(output),
    ]
    subprocess.run(command, check=True)
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("render_results.py")),
            str(output),
            "--output",
            str(output.with_suffix(".html")),
        ],
        check=True,
    )
    print(f"Open {output.with_suffix('.html')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
