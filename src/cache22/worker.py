"""Continuous worker ownership and independent availability heartbeats."""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from .index import Index
from .operation import OperationInterrupted
from .repo_service import error_text, execute_job
from .scheduler import Scheduler


@contextmanager
def shutdown_signals(stop: threading.Event) -> Iterator[None]:
    previous = {
        sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def run_worker(
    *,
    index: Index | None = None,
    check_timeout: float = 120,
    fetch_timeout: float = 7200,
    convert_timeout: float = 7200,
    stop: threading.Event | None = None,
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    run_continuous(
        index=index,
        stop=stop,
        check_timeout=check_timeout,
        fetch_timeout=fetch_timeout,
        convert_timeout=convert_timeout,
        report=outcomes.append,
        once=True,
    )
    return outcomes


def run_continuous(
    *,
    index: Index | None = None,
    stop: threading.Event | None = None,
    check_timeout: float = 120,
    fetch_timeout: float = 7200,
    convert_timeout: float = 7200,
    report: Callable[[dict[str, Any]], None] = lambda result: None,
    once: bool = False,
) -> None:
    if min(check_timeout, fetch_timeout, convert_timeout) <= 0:
        raise ValueError("Timeouts must be positive")
    index = index or Index()
    stop = stop or threading.Event()
    heartbeat_stop = threading.Event()
    heartbeat_failed = threading.Event()
    worker_id = uuid.uuid4().hex
    with index.transaction() as db:
        db.execute(
            "INSERT INTO workers(id,pid,started_at,heartbeat_at) VALUES(?,?,?,?)",
            (worker_id, os.getpid(), index.now(), index.now()),
        )

    def heartbeat() -> None:
        while not heartbeat_stop.wait(5):
            try:
                with index.transaction() as db:
                    db.execute(
                        "UPDATE workers SET heartbeat_at=? WHERE id=?", (index.now(), worker_id)
                    )
            except sqlite3.Error:
                # Become visibly stale and stop claiming if availability cannot be published.
                heartbeat_failed.set()
                stop.set()
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    scheduler = Scheduler(index)
    try:
        while not stop.is_set():
            scheduler.tick()
            if stop.is_set():
                break
            job = scheduler.claim()
            if job is None:
                if once:
                    break
                stop.wait(1)
                continue
            with index.transaction() as db:
                db.execute("UPDATE workers SET current_job_id=? WHERE id=?", (job["id"], worker_id))
            try:
                execute_job(
                    index,
                    job,
                    check_timeout=check_timeout,
                    fetch_timeout=fetch_timeout,
                    convert_timeout=convert_timeout,
                    cancel=stop,
                )
                report({"job_id": job["id"], "outcome": "succeeded"})
            except OperationInterrupted:
                report({"job_id": job["id"], "outcome": "interrupted"})
            except (
                OSError,
                RuntimeError,
                ValueError,
                sqlite3.Error,
                subprocess.SubprocessError,
            ) as exc:
                report(
                    {
                        "job_id": job["id"],
                        "outcome": "failed",
                        "error": error_text(exc, index.get(job["repository_id"])["source_url"]),
                    }
                )
            finally:
                with index.transaction() as db:
                    db.execute("UPDATE workers SET current_job_id=NULL WHERE id=?", (worker_id,))
    finally:
        heartbeat_stop.set()
        thread.join()
        with index.transaction() as db:
            db.execute(
                "UPDATE workers SET stopped_at=?,current_job_id=NULL WHERE id=?",
                (index.now(), worker_id),
            )
    if heartbeat_failed.is_set():
        raise RuntimeError("Worker availability heartbeat failed")
