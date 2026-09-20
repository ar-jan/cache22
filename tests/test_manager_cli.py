from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cache22.cli import app
from cache22.index import Index, index_path
from cache22.job_queue import Queue
from cache22.manager_service import error_snapshot
from cache22.repo_service import add_repository


def test_errors_follow_completed_attempt_through_retry_and_interruption(tmp_path: Path) -> None:
    now = 1000
    index = Index(tmp_path / "index.db", clock=lambda: now)
    repository = add_repository("https://host/team/repo", tmp_path, index=index)
    queue = Queue(index)
    job = queue.immediate(repository["id"], "fetch")
    queue.finish(job, category="transport", error="Connection failed\nDiagnostic detail")
    error = error_snapshot(index)["errors"][0]
    assert (error["state"], error["retry_count"], error["due_at"]) == ("pending", 1, 1060)
    assert error["attempt_number"] == 1
    now = 1060
    retry = queue.claim()
    assert retry is not None
    error = error_snapshot(index)["errors"][0]
    assert error["state"] == "running"
    assert error["attempt_id"] == job["attempt_id"]
    queue.interrupt(retry, "Worker stopping")
    error = error_snapshot(index)["errors"][0]
    assert (error["outcome"], error["error"], error["attempt_number"]) == (
        "interrupted",
        "Worker stopping",
        2,
    )
    retry = queue.claim()
    assert retry is not None
    assert error_snapshot(index)["errors"][0]["error"] == "Worker stopping"
    queue.finish(retry)
    assert error_snapshot(index)["errors"] == []


def test_errors_pagination_and_job_scope(tmp_path: Path) -> None:
    now = 1000
    index = Index(tmp_path / "index.db", clock=lambda: now)
    repository = add_repository("https://host/team/repo", tmp_path, index=index)
    queue = Queue(index)
    first = queue.immediate(repository["id"], "fetch")
    queue.finish(first, category="structural", error="First failure")
    now += 1
    second = queue.immediate(repository["id"], "fetch")
    queue.finish(second, category="structural", error="Second failure")
    third = queue.immediate(repository["id"], "fetch")
    queue.finish(third, category="structural", error="Third failure at same time")
    cancelled = queue.immediate(repository["id"], "fetch")
    queue.finish(cancelled, category="transport", error="Cancelled retry")
    queue.unqueue(repository["id"])
    succeeded = queue.immediate(repository["id"], "fetch")
    queue.finish(succeeded)
    page = error_snapshot(index, limit=1)
    assert [row["id"] for row in page["errors"]] == [third["id"]]
    assert page["next_offset"] == 1
    page = error_snapshot(index, limit=2, offset=1)
    assert [row["id"] for row in page["errors"]] == [second["id"], first["id"]]
    assert page["next_offset"] is None
    assert error_snapshot(index, offset=3)["errors"] == []


def test_cli_default_override_text_json_and_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = Index()
    other = Index(tmp_path / "other ?# database.sqlite3")
    repository = add_repository("https://host/team/other", tmp_path, index=other)
    queue = Queue(other)
    job = queue.immediate(repository["id"], "fetch")
    message = "Fetch failed\nFull diagnostic " + "x" * 1000
    queue.finish(job, category="structural", error=message)
    with other.connect() as connection:
        before = list(connection.iterdump())
    # The override also accepts a relative path containing SQLite URI metacharacters.
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["manager", "errors", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["errors"] == []
    assert json.loads(result.stdout)["database"] == str(default.path.resolve())
    args = ["manager", "errors", "--db", other.path.name]
    result = runner.invoke(app, args + ["--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["database"] == str(other.path)
    assert data["observed_at"].endswith("Z")
    assert data["errors"][0]["error_at"].endswith("Z")
    assert data["errors"][0]["error"] == message
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "host/team/other" in result.stdout
    assert message.replace("\n", "\n    ") in result.stdout
    result = runner.invoke(
        app, ["manager", "queue", "--db", str(other.path), "--section", "history"]
    )
    assert result.exit_code == 0, result.output
    assert "history: 1" in result.stdout
    with other.connect() as connection:
        assert list(connection.iterdump()) == before
    with (
        Index(other.path, read_only=True).connect() as connection,
        pytest.raises(sqlite3.OperationalError, match="readonly"),
    ):
        connection.execute("DELETE FROM jobs")


def test_queue_cli_counts_progress_and_worker_freshness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = Index(clock=lambda: 1000)
    repository = add_repository("https://host/team/repo", tmp_path, index=index)
    queue = Queue(index)
    running = queue.immediate(repository["id"], "fetch")
    queue.enqueue(repository["id"])
    other = add_repository("https://host/team/other", tmp_path, index=index)
    queue.enqueue(other["id"])
    with index.transaction() as db:
        db.execute(
            "INSERT INTO attempt_progress(attempt_id,phase,observed_at,completed,total,unit,percentage) VALUES(?,'fetching',1000,1,2,'objects',50)",
            (running["attempt_id"],),
        )
        db.executemany(
            "INSERT INTO workers(id,pid,started_at,heartbeat_at,stopped_at,current_job_id) VALUES(?,?,900,?,?,?)",
            [
                ("fresh", 1, 985, None, running["id"]),
                ("stale", 2, 984, None, None),
                ("stopped", 3, 999, 1000, None),
            ],
        )
    monkeypatch.setattr(Index, "now", lambda self: 1000)
    runner = CliRunner()
    result = runner.invoke(app, ["manager", "queue", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["counts"] == {"running": 1, "runnable": 1, "deferred": 1, "history": 0}
    assert data["jobs"][0]["percentage"] == 50
    assert data["jobs"][0]["live"] == 1
    assert {w["id"]: w["available"] for w in data["workers"]} == {
        "fresh": True,
        "stale": False,
        "stopped": False,
    }
    result = runner.invoke(app, ["manager", "queue"])
    assert "percentage: 50" in result.stdout
    assert "Worker 2: stale" in result.stdout
    assert "Worker 3: stopped" in result.stdout
    monkeypatch.setattr(Index, "now", lambda self: 1120)
    result = runner.invoke(app, ["manager", "queue"])
    assert "No available worker." in result.stdout
    assert "Claim expired; awaiting recovery." in result.stdout
    result = runner.invoke(app, ["manager", "queue", "--section", "history"])
    assert "No jobs in this section" in result.stdout


@pytest.mark.parametrize("command", ["errors", "queue"])
def test_inspection_failures_and_arguments(tmp_path: Path, command: str) -> None:
    runner = CliRunner()
    missing = index_path()
    result = runner.invoke(app, ["manager", command])
    assert result.exit_code == 1
    assert str(missing) in result.stderr
    assert not missing.parent.exists()
    for path in [
        tmp_path / "missing" / "index.db",
        tmp_path / "invalid.db",
        tmp_path / "unsupported.db",
    ]:
        if path.name == "invalid.db":
            path.write_text("not sqlite")
        elif path.name == "unsupported.db":
            with sqlite3.connect(path) as db:
                db.execute("PRAGMA user_version=99")
        result = runner.invoke(app, ["manager", command, "--db", str(path)])
        assert result.exit_code == 1
        assert str(path) in result.stderr
        assert "Traceback" not in result.output
    assert not (tmp_path / "missing").exists()
    for options in [["--limit", "0"], ["--limit", "501"], ["--offset", "-1"]]:
        assert runner.invoke(app, ["manager", command, *options]).exit_code == 2
    assert runner.invoke(app, ["manager", "queue", "--section", "unknown"]).exit_code == 2
