from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cache22.cli import app
from cache22.index import Index, index_path
from cache22.job_queue import Queue
from cache22.manager_service import jobs_snapshot
from cache22.repo_service import add_repository
from cache22.scheduler import Scheduler


def test_errors_follow_completed_attempt_through_retry_and_interruption(tmp_path: Path) -> None:
    now = 1000
    index = Index(tmp_path / "index.db", clock=lambda: now)
    repository = add_repository("https://host/team/repo", tmp_path, index=index)
    queue = Queue(index)
    scheduler = Scheduler(index)
    job = scheduler.immediate(repository["id"], "fetch")
    scheduler.finish(job, category="transport", error="Connection failed\nDiagnostic detail")
    error = jobs_snapshot(index, state="failed")["jobs"][0]
    assert (error["state"], error["retry_count"], error["due_at"]) == ("pending", 1, 1060)
    assert error["diagnostic"]["attempt_number"] == 1
    assert index.get(repository["id"])["last_error"] == error["diagnostic"]["error"]
    now = 1060
    retry = scheduler.claim()
    assert retry is not None
    error = jobs_snapshot(index, state="failed")["jobs"][0]
    assert error["state"] == "running"
    assert error["diagnostic"]["attempt_id"] == job["attempt_id"]
    assert index.get(repository["id"])["last_error"] == error["diagnostic"]["error"]
    scheduler.interrupt(retry, "Worker stopping")
    error = jobs_snapshot(index, state="failed")["jobs"][0]
    assert (
        error["diagnostic"]["outcome"],
        error["diagnostic"]["error"],
        error["diagnostic"]["attempt_number"],
    ) == (
        "interrupted",
        "Worker stopping",
        2,
    )
    retry = scheduler.claim()
    assert retry is not None
    assert (
        jobs_snapshot(index, state="failed")["jobs"][0]["diagnostic"]["error"] == "Worker stopping"
    )
    record = index.get(repository["id"])
    assert record["last_error"] == "Worker stopping"
    assert record["last_error_category"] == "interrupted"
    assert record["last_error_at"] == now
    assert record["has_error"]
    scheduler.finish(retry)
    assert jobs_snapshot(index, state="failed")["jobs"] == []
    assert not index.get(repository["id"])["has_error"]
    cancelled = scheduler.immediate(repository["id"], "fetch")
    scheduler.finish(cancelled, category="transport", error="Cancelled retry")
    assert index.get(repository["id"])["has_error"]
    queue.unqueue(repository["id"])
    record = index.get(repository["id"])
    assert not record["has_error"]
    assert all(
        record[field] is None
        for field in ("last_error", "last_error_kind", "last_error_category", "last_error_at")
    )


def test_errors_pagination_and_job_scope(tmp_path: Path) -> None:
    now = 1000
    index = Index(tmp_path / "index.db", clock=lambda: now)
    repository = add_repository("https://host/team/repo", tmp_path, index=index)
    queue = Queue(index)
    scheduler = Scheduler(index)
    first = scheduler.immediate(repository["id"], "fetch")
    scheduler.finish(first, category="structural", error="First failure")
    now += 1
    second = scheduler.immediate(repository["id"], "fetch")
    scheduler.finish(second, category="structural", error="Second failure")
    third = scheduler.immediate(repository["id"], "convert")
    scheduler.finish(third, category="structural", error="Third failure at same time")
    cancelled = scheduler.immediate(repository["id"], "fetch")
    scheduler.finish(cancelled, category="transport", error="Cancelled retry")
    queue.unqueue(repository["id"])
    succeeded = scheduler.immediate(repository["id"], "fetch")
    scheduler.finish(succeeded)
    page = jobs_snapshot(index, state="failed", limit=1)
    assert [row["id"] for row in page["jobs"]] == [third["id"]]
    assert page["next_offset"] == 1
    record = index.get(repository["id"])
    assert record["has_error"]
    assert (record["last_error"], record["last_error_kind"], record["last_error_at"]) == (
        "Third failure at same time",
        "convert",
        now,
    )
    page = jobs_snapshot(index, state="failed", limit=2, offset=1)
    assert [row["id"] for row in page["jobs"]] == [second["id"], first["id"]]
    assert page["next_offset"] is None
    assert jobs_snapshot(index, state="failed", offset=3)["jobs"] == []


def test_cli_default_override_text_json_and_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = Index()
    other = Index(tmp_path / "other ?# database.sqlite3")
    repository = add_repository("https://host/team/other", tmp_path, index=other)
    scheduler = Scheduler(other)
    job = scheduler.immediate(repository["id"], "fetch")
    message = "Fetch failed\nFull diagnostic " + "x" * 1000
    scheduler.finish(job, category="structural", error=message)
    with other.connect() as connection:
        before = list(connection.iterdump())
    # The override also accepts a relative path containing SQLite URI metacharacters.
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["jobs", "--state", "failed", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["jobs"] == []
    assert json.loads(result.stdout)["database"] == str(default.path.resolve())
    args = ["jobs", "--state", "failed", "--db", other.path.name]
    result = runner.invoke(app, args + ["--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["database"] == str(other.path)
    assert data["observed_at"].endswith("Z")
    assert data["jobs"][0]["diagnostic"]["error_at"].endswith("Z")
    assert data["jobs"][0]["diagnostic"]["error"] == message
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "host/team/other" in result.stdout
    assert message.replace("\n", "\n    ") in result.stdout
    result = runner.invoke(app, ["jobs", "--db", str(other.path), "--state", "history"])
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
    scheduler = Scheduler(index)
    running = scheduler.immediate(repository["id"], "fetch")
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
    result = runner.invoke(app, ["jobs", "--state", "running", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["counts"] == {
        "all": 3,
        "pending": 2,
        "failed": 0,
        "running": 1,
        "runnable": 1,
        "deferred": 1,
        "history": 0,
    }
    assert data["jobs"][0]["percentage"] == 50
    assert data["jobs"][0]["live"] == 1
    assert {w["id"]: w["available"] for w in data["workers"]} == {
        "fresh": True,
        "stale": False,
        "stopped": False,
    }
    result = runner.invoke(app, ["jobs", "--state", "running"])
    assert "percentage: 50" in result.stdout
    assert "Worker 2: stale" in result.stdout
    assert "Worker 3: stopped" in result.stdout
    monkeypatch.setattr(Index, "now", lambda self: 1120)
    result = runner.invoke(app, ["jobs", "--state", "running"])
    assert "No available worker." in result.stdout
    assert "Claim expired; awaiting recovery." in result.stdout
    result = runner.invoke(app, ["jobs", "--state", "history"])
    assert "No jobs in this section" in result.stdout


@pytest.mark.parametrize("state", ["failed", "all"])
def test_inspection_failures_and_arguments(tmp_path: Path, state: str) -> None:
    runner = CliRunner()
    missing = index_path()
    result = runner.invoke(app, ["jobs", "--state", state])
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
        result = runner.invoke(app, ["jobs", "--state", state, "--db", str(path)])
        assert result.exit_code == 1
        assert str(path) in result.stderr
        assert "Traceback" not in result.output
    assert not (tmp_path / "missing").exists()
    for options in [["--limit", "0"], ["--limit", "501"], ["--offset", "-1"]]:
        assert runner.invoke(app, ["jobs", "--state", state, *options]).exit_code == 2
    assert runner.invoke(app, ["jobs", "--state", "unknown"]).exit_code == 2


def test_jobs_views_scope_pagination_and_retry_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1000
    index = Index(clock=lambda: now)
    monkeypatch.setattr(Index, "now", lambda self: now)
    record = add_repository("https://host/team/repo", tmp_path, index=index)
    other = add_repository("https://host/team/other", tmp_path, index=index)
    queue = Queue(index)
    scheduler = Scheduler(index)
    done = scheduler.immediate(record["id"], "check")
    scheduler.finish(done)
    problem = scheduler.immediate(record["id"], "fetch")
    scheduler.finish(problem, category="transport", error="Retry me")
    waiting = queue.enqueue(record["id"], "convert")
    runnable = queue.enqueue(other["id"], "check")
    runner = CliRunner()
    expected = {
        "all": [runnable, waiting, problem["id"], done["id"]],
        "running": [],
        "pending": [waiting, runnable, problem["id"]],
        "runnable": [runnable],
        "deferred": [waiting, problem["id"]],
        "failed": [problem["id"]],
        "history": [done["id"]],
    }
    for state, ids in expected.items():
        result = runner.invoke(app, ["jobs", "--state", state, "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert [row["id"] for row in data["jobs"]] == ids
        assert data["counts"] == {name: len(items) for name, items in expected.items()}
    for selector in (record["repo_key"], record["source_url"]):
        result = runner.invoke(app, ["jobs", selector, "--limit", "1", "--offset", "1", "--json"])
        data = json.loads(result.stdout)
        assert data["state"] == "all"
        assert data["counts"]["all"] == 3
        assert data["counts"]["runnable"] == 0
        assert data["next_offset"] == 2
        assert data["jobs"][0]["id"] == problem["id"]
        assert data["jobs"][0]["attempts"][0]["error"] == "Retry me"
    now = 1060
    retry = scheduler.claim(problem["id"])
    assert retry is not None
    result = runner.invoke(app, ["jobs", record["repo_key"], "--state", "failed", "--json"])
    row = json.loads(result.stdout)["jobs"][0]
    assert row["state"] == "running"
    assert row["attempt_id"] == retry["attempt_id"]
    assert row["diagnostic"]["attempt_id"] == problem["attempt_id"]
    assert row["diagnostic"]["error"] == "Retry me"
    assert len(row["attempts"]) == 2
    assert row["attempts"][-1]["outcome"] is None
    assert runner.invoke(app, ["jobs", "host/missing/repo"]).exit_code == 1
