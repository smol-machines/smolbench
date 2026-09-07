from bench.render_results import provider_summary, ratio, render


def test_report_uses_medians_and_baseline_speedup() -> None:
    rows = [
        {
            "task": "real-task",
            "dataset": "public@1",
            "provider": provider,
            "attempts": 2,
            "concurrency": 2,
            "repetition": repetition,
            "agent": "oracle",
            "wall_seconds": wall,
            "harbor_seconds": wall - 1,
            "completed": 2,
            "errors": 0,
            "rewards": [1.0, 1.0],
            "environment_setup_seconds": {"p50": wall / 2, "p99": wall / 2},
            "verifier_seconds": {"p99": 1.0},
            "approximate_peak_host_memory_bytes": 2**20,
            "checkpoint_prepare_seconds": 3.0 if provider == "smol-branch" else None,
        }
        for provider, repetition, wall in (
            ("smol-branch", 1, 4.0),
            ("smol-branch", 2, 6.0),
            ("smol-cold", 1, 10.0),
            ("smol-cold", 2, 20.0),
        )
    ]

    summary = provider_summary(rows[:2])
    assert summary["wall"] == 5.0
    assert ratio(15.0, summary["wall"]) == "3.00×"
    report = render(rows)
    assert "real-task" in report
    assert "3.00×" in report
    assert "4/4" in report


def test_report_keeps_different_fanout_sizes_separate() -> None:
    rows = [
        {
            "task": "real-task",
            "dataset": "public@1",
            "provider": "smol-branch",
            "attempts": attempts,
            "concurrency": attempts,
            "repetition": 1,
            "agent": "oracle",
            "wall_seconds": float(attempts),
            "harbor_seconds": float(attempts),
            "completed": attempts,
            "errors": 0,
            "rewards": [1.0] * attempts,
        }
        for attempts in (4, 16)
    ]

    report = render(rows)
    assert report.count("<h2>real-task</h2>") == 2
    assert "4 trials × 1 repetitions · concurrency 4" in report
    assert "16 trials × 1 repetitions · concurrency 16" in report


def test_report_renders_braintrust_runtime_gate() -> None:
    payload = {
        "workload": {
            "repository": "https://example.com/repo",
            "revision": "abcdef1234567890",
            "dataset": "real data",
        },
        "mode": "smoke",
        "branch_count": 4,
        "branch_batch_seconds": 0.128,
        "branch_to_completed_wall_seconds": 0.117,
        "checkpoint_prepare_seconds": 231.5,
        "task_latency_seconds": {"p50": 0.1, "p99": 0.115},
        "correct": 4,
        "results": [{}, {}, {}, {}],
    }
    report = render([], [payload])
    assert "Braintrust bash-agent-evals" in report
    assert "128 ms" in report
    assert "4/4" in report


def test_report_renders_braintrust_docker_comparison() -> None:
    payload = {
        "workload": {
            "repository": "https://example.com/repo",
            "revision": "abcdef1234567890",
            "dataset": "real data",
        },
        "mode": "smoke",
        "branch_count": 4,
        "branch_batch_seconds": 0.125,
        "branch_to_completed_wall_seconds": 0.125,
        "checkpoint_prepare_seconds": 231.5,
        "task_latency_seconds": {"p50": 0.1, "p99": 0.115},
        "correct": 4,
        "results": [{}, {}, {}, {}],
        "docker": {
            "image_prepare_seconds": 12.0,
            "start_to_completed_wall_seconds": 1.0,
            "task_latency_seconds": {"p50": 0.9, "p99": 0.95},
            "correct": 4,
            "results": [{}, {}, {}, {}],
        },
    }
    report = render([], [payload])
    assert "Warm Docker" in report
    assert "4.00×" in report
    assert "12.00s" in report
