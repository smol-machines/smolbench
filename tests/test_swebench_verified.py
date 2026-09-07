from pathlib import Path

from bench.swebench_verified import dockerfile_base, materialize_task, tree_digest


def make_task(root: Path) -> Path:
    task = root / "example-task"
    (task / "environment").mkdir(parents=True)
    (task / "environment" / "Dockerfile").write_text(
        "# ignored\nFROM example/swebench:fixed\nWORKDIR /testbed\n"
    )
    (task / "task.toml").write_text(
        'schema_version = "1.1"\n\n[environment]\ncpus = 1\n'
    )
    (task / "instruction.md").write_text("fix it\n")
    return task


def test_materialize_task_records_source_digest_and_base_image(tmp_path) -> None:
    source = make_task(tmp_path / "source")

    task, digest, image = materialize_task(source, tmp_path / "output")

    assert digest == tree_digest(source)
    assert image == "example/swebench:fixed"
    assert f'docker_image = "{image}"' in (task / "task.toml").read_text()
    assert "docker_image" not in (source / "task.toml").read_text()


def test_dockerfile_base_ignores_comments(tmp_path) -> None:
    source = make_task(tmp_path)
    assert dockerfile_base(source / "environment" / "Dockerfile") == (
        "example/swebench:fixed"
    )
