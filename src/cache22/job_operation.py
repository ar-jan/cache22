"""Bind a claimed job to operation fencing, lease renewal, and progress."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from . import operation
from .job_queue import Queue

HEARTBEAT_SECONDS = 20


@contextmanager
def running_job(
    queue: Queue, job: dict[str, Any], timeout: float, cancel: threading.Event | None = None
) -> Iterator[None]:
    if timeout <= 0:
        raise ValueError("Operation timeout must be positive")
    stop = threading.Event()
    lost = threading.Event()

    def heartbeat() -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                queue.heartbeat(job)
            except sqlite3.Error, operation.ClaimLostError:
                lost.set()
                return

    def validate() -> None:
        if cancel is not None and cancel.is_set():
            raise operation.OperationInterrupted("Worker stopping")
        if lost.is_set():
            raise operation.ClaimLostError("Worker heartbeat failed")
        with queue.index.connect() as db:
            queue.validate(db, job)

    last_write = 0.0
    last_phase = ""

    def publish(snapshot: dict[str, Any]) -> None:
        nonlocal last_write, last_phase
        now = time.monotonic()
        if snapshot["phase"] == last_phase and now - last_write < 1:
            return
        try:
            queue.publish_progress(job, snapshot)
        except sqlite3.Error:
            # Progress is observational; a busy database must not fail Git.
            return
        last_write, last_phase = now, snapshot["phase"]

    thread = threading.Thread(target=heartbeat, daemon=True)
    token = operation.current_operation.set(
        operation.Operation(
            validate, time.monotonic() + timeout, lambda db: queue.validate(db, job), publish
        )
    )
    started = False
    try:
        operation.guard()
        thread.start()
        started = True
        yield
    finally:
        operation.current_operation.reset(token)
        stop.set()
        if started:
            thread.join()
