from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from cache22.cli import app
from cache22.index import Index
from cache22.job_queue import Queue
from cache22.repo_service import add_repository


def test_register_batch_is_register_only_and_reports_each_input(tmp_path: Path) -> None:
    root = tmp_path / "archives"
    root.mkdir()
    first, second = "https://host/team/one", "https://host/team/two"
    result = CliRunner().invoke(
        app, ["add", first, "invalid", second, first, "--root", str(root), "--json"]
    )
    assert result.exit_code == 1, result.output
    rows = json.loads(result.stdout)
    assert [row["selector"] for row in rows] == [first, "invalid", second, first]
    assert [row["status"] for row in rows] == ["registered", "error", "registered", "duplicate"]
    assert rows[0]["repository_id"] == rows[3]["repository_id"]
    assert len(Index().list()) == 2
    assert Queue(Index()).list() == []
    assert list(root.iterdir()) == []


def test_batch_mutations_continue_and_deduplicate_resolved_selectors(tmp_path: Path) -> None:
    index = Index.initialize()
    records = [
        add_repository(f"https://host/team/{name}", tmp_path, index=index)
        for name in ("one", "two")
    ]
    selectors = [
        records[0]["repo_key"],
        "host/missing/repo",
        records[0]["source_url"],
        records[1]["source_url"],
    ]
    runner = CliRunner()
    for command, options in (
        ("queue", []),
        ("schedule", ["--every", "6h"]),
        ("unqueue", []),
        ("schedule", ["--off"]),
    ):
        result = runner.invoke(app, [command, *selectors, *options, "--json"])
        assert result.exit_code == 1, result.output
        rows = json.loads(result.stdout)
        assert [row["status"] for row in rows] == ["accepted", "error", "accepted"]
        assert [row["selector"] for row in rows] == [selectors[0], selectors[1], selectors[3]]
        if command == "queue":
            assert len(Queue(index).list()) == 2
            assert all(job["kind"] == "fetch" for job in Queue(index).list())
        if command == "schedule":
            assert all(
                index.get(record["id"])["scheduled"] == (options[0] == "--every")
                for record in records
            )
    assert all(job["state"] == "cancelled" for job in Queue(index).list())


def test_command_wide_validation_precedes_mutations(tmp_path: Path) -> None:
    index = Index.initialize()
    record = add_repository("https://host/team/one", tmp_path, index=index)
    runner = CliRunner()
    for args in (
        ["schedule", record["repo_key"]],
        ["schedule", record["repo_key"], "--every", "0h"],
        ["schedule", record["repo_key"], "--every", "6h", "--off"],
        ["queue", record["repo_key"], "--kind", "invalid"],
        ["worker", "--timeout-check", "0"],
        ["web", "--port", "65536"],
    ):
        assert runner.invoke(app, args).exit_code == 2
    assert Queue(index).list() == []
    assert not index.get(record["id"])["scheduled"]


def test_worker_defaults_to_continuous_and_once_emits_result_array() -> None:
    runner = CliRunner()

    def report_result(**kwargs: object) -> None:
        from typing import Any, cast

        cast(Any, kwargs["report"])({"outcome": "succeeded", "finished_at": 1000})

    with patch("cache22.repo_cli.run_continuous", side_effect=report_result) as continuous:
        result = runner.invoke(app, ["worker", "--timeout-check", "7", "--json"])
        assert result.exit_code == 0, result.output
        assert continuous.call_args.kwargs["check_timeout"] == 7
        assert json.loads(result.stdout) == {
            "outcome": "succeeded",
            "finished_at": "1970-01-01T00:16:40Z",
        }
    with patch("cache22.repo_cli.run_worker", return_value=[{"outcome": "failed"}]) as once:
        result = runner.invoke(app, ["worker", "--once", "--json"])
        assert result.exit_code == 1, result.output
        assert json.loads(result.stdout) == [{"outcome": "failed"}]
        once.assert_called_once()
