"""Operation context cleanup, heartbeat failure, and observational progress."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from cache22 import job_operation, operation
from cache22.index import Index
from cache22.job_operation import running_job
from cache22.repo_service import add_repository
from cache22.scheduler import Scheduler


@pytest.fixture
def scheduler(tmp_path: Path) -> Scheduler:
    index = Index.initialize(tmp_path / "index.db", clock=lambda: 1000)
    add_repository("https://host/team/repo", tmp_path, index=index)
    return Scheduler(index)


@pytest.mark.parametrize("heartbeat_fails", [False, True])
def test_heartbeat_and_context_cleanup_on_failure(
    scheduler: Scheduler, monkeypatch: pytest.MonkeyPatch, heartbeat_fails: bool
) -> None:
    repo_id = scheduler.index.list()[0]["id"]
    job = scheduler.immediate(repo_id, "fetch")
    queue = scheduler.queue
    original_thread = threading.Thread
    threads: list[threading.Thread] = []
    beat = threading.Event()
    renew = queue.heartbeat

    def tracked_thread(**kwargs: Any) -> threading.Thread:
        thread = original_thread(**kwargs)
        threads.append(thread)
        return thread

    def heartbeat(job: dict[str, Any]) -> None:
        if heartbeat_fails:
            beat.set()
            raise sqlite3.OperationalError("heartbeat unavailable")
        renew(job)
        beat.set()

    monkeypatch.setattr(job_operation, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(job_operation.threading, "Thread", tracked_thread)
    monkeypatch.setattr(queue, "heartbeat", heartbeat)
    previous = operation.Operation(lambda: None, float("inf"), lambda db: None)
    token = operation.current_operation.set(previous)
    try:
        expected = operation.ClaimLostError if heartbeat_fails else RuntimeError
        message = "heartbeat failed" if heartbeat_fails else "body failed"

        def execute() -> None:
            with running_job(queue, job, 10):
                assert operation.current_operation.get() is not previous
                assert beat.wait(2)
                if heartbeat_fails:
                    threads[0].join(2)
                    assert not threads[0].is_alive()
                    operation.guard()
                raise RuntimeError("body failed")

        with pytest.raises(expected, match=message):
            execute()
        assert operation.current_operation.get() is previous
        assert len(threads) == 1 and not threads[0].is_alive()
        # Context exit leaves finalization to the scheduler.
        assert queue.list()[0]["state"] == "running"
    finally:
        operation.current_operation.reset(token)


def test_thread_start_failure_restores_context(
    scheduler: Scheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = scheduler.immediate(scheduler.index.list()[0]["id"], "check")

    def fail_start(self: threading.Thread) -> None:
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(job_operation.threading.Thread, "start", fail_start)
    previous = operation.current_operation.get()
    with (
        pytest.raises(RuntimeError, match="thread unavailable"),
        running_job(scheduler.queue, job, 10),
    ):
        pytest.fail("operation must not start")
    assert operation.current_operation.get() is previous


def test_progress_throttles_successful_writes_and_retries_database_failures(
    scheduler: Scheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = scheduler.immediate(scheduler.index.list()[0]["id"], "fetch")
    queue = scheduler.queue
    now = 10.0
    monkeypatch.setattr(job_operation.time, "monotonic", lambda: now)
    publish = queue.publish_progress
    writes: list[dict[str, Any]] = []
    unavailable = True

    def write(job: dict[str, Any], snapshot: dict[str, Any]) -> None:
        writes.append(snapshot)
        if unavailable:
            raise sqlite3.OperationalError("progress unavailable")
        publish(job, snapshot)

    monkeypatch.setattr(queue, "publish_progress", write)
    with running_job(queue, job, 10):
        operation.progress("fetch", completed=1)
        unavailable = False
        operation.progress("fetch", completed=2)
        operation.progress("fetch", completed=3)  # Same phase is throttled.
        operation.progress("validation")  # A new phase publishes immediately.
        operation.progress("validation")
        now += 1
        operation.progress("validation", detail="https://user:secret@host/repo")
    assert len(writes) == 4
    with queue.index.connect() as db:
        row = db.execute("SELECT * FROM attempt_progress").fetchone()
        assert row["phase"] == "validation"
        assert "secret" not in row["detail"]


def test_cancelled_context_restores_fence(scheduler: Scheduler) -> None:
    job = scheduler.immediate(scheduler.index.list()[0]["id"], "check")
    cancel = threading.Event()
    previous = operation.current_operation.get()

    def execute() -> None:
        with running_job(scheduler.queue, job, 10, cancel):
            cancel.set()
            operation.guard()

    with pytest.raises(operation.OperationInterrupted):
        execute()
    assert operation.current_operation.get() is previous
