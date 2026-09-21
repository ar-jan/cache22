"""Durable, same-machine work queue and opt-in recurring checks."""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

from . import operation
from .archive_storage import RepositoryBusyError
from .index import Index

Kind = Literal["check", "fetch", "convert"]
RETRIES = (60, 300, 1800, 7200)
LEASE_SECONDS = 120
BLOCKING_JOB_SQL = """SELECT min(prior.id) FROM jobs prior
WHERE prior.repository_id=j.repository_id
AND (prior.state='running' OR (prior.id<j.id AND prior.state='pending'))"""


class Queue:
    def __init__(self, index: Index):
        self.index = index

    def enqueue_in(
        self, db: sqlite3.Connection, repository_id: int, kind: Kind, *, origin: str = "manual"
    ) -> int:
        if kind == "convert" and origin != "manual":
            raise ValueError("Conversion must be explicitly requested")
        pending = db.execute(
            "SELECT * FROM jobs WHERE repository_id=? AND state IN ('pending','running') ORDER BY id DESC LIMIT 1",
            (repository_id,),
        ).fetchone()
        if (
            pending
            and pending["state"] == "pending"
            and ((kind == "convert") == (pending["kind"] == "convert"))
        ):
            db.execute(
                "UPDATE jobs SET kind=?,origin=?,due_at=? WHERE id=?",
                (
                    "convert"
                    if kind == "convert"
                    else "fetch"
                    if "fetch" in (kind, pending["kind"])
                    else "check",
                    "manual" if "manual" in (origin, pending["origin"]) else "scheduled",
                    min(self.index.now(), pending["due_at"]),
                    pending["id"],
                ),
            )
            return pending["id"]
        cursor = db.execute(
            "INSERT INTO jobs(repository_id,kind,origin,due_at,created_at) VALUES(?,?,?,?,?)",
            (repository_id, kind, origin, self.index.now(), self.index.now()),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def enqueue(self, repository_id: int, kind: Kind = "fetch") -> int:
        with self.index.transaction() as db:
            return self.enqueue_in(db, repository_id, kind)

    def unqueue(self, repository_id: int) -> None:
        with self.index.transaction() as db:
            db.execute(
                "UPDATE jobs SET state='cancelled',finished_at=? WHERE repository_id=? AND state='pending'",
                (self.index.now(), repository_id),
            )

    def schedule(self, repository_id: int, interval: int | None) -> None:
        if interval is not None and interval <= 0:
            raise ValueError("Schedule interval must be positive")
        with self.index.transaction() as db:
            if interval is None:
                db.execute("UPDATE schedules SET enabled=0 WHERE repository_id=?", (repository_id,))
                db.execute(
                    "UPDATE jobs SET state='cancelled',finished_at=? WHERE repository_id=? AND state='pending' AND origin='scheduled'",
                    (self.index.now(), repository_id),
                )
            else:
                db.execute(
                    """INSERT INTO schedules(repository_id,enabled,interval_seconds,next_due_at) VALUES(?,1,?,?) ON CONFLICT(repository_id)
                    DO UPDATE SET enabled=1,blocked=0,interval_seconds=excluded.interval_seconds,next_due_at=excluded.next_due_at""",
                    (repository_id, interval, self.index.now()),
                )

    def materialize(self) -> None:
        with self.index.transaction() as db:
            for row in db.execute(
                """SELECT repository_id FROM schedules s WHERE enabled=1 AND blocked=0 AND next_due_at<=?
                AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.repository_id=s.repository_id AND j.state IN ('pending','running'))""",
                (self.index.now(),),
            ).fetchall():
                self.enqueue_in(db, row["repository_id"], "check", origin="scheduled")
            db.execute(
                "DELETE FROM workers WHERE COALESCE(stopped_at,heartbeat_at)<?",
                (self.index.now() - 30 * 86400,),
            )
            db.execute(
                "DELETE FROM jobs WHERE state IN ('succeeded','failed','cancelled') AND finished_at<?",
                (self.index.now() - 30 * 86400,),
            )

    def _recover(self, db: sqlite3.Connection) -> None:
        for row in db.execute(
            "SELECT * FROM jobs WHERE state='running' AND lease_until<=?", (self.index.now(),)
        ).fetchall():
            self._interrupt_in(db, row, "Worker claim expired")

    def interrupt(self, job: dict[str, Any], message: str = "Operation interrupted") -> None:
        with self.index.transaction() as db:
            self.validate(db, job)
            self._interrupt_in(db, job, message)

    def _interrupt_in(self, db: sqlite3.Connection, row: Any, message: str) -> None:
        db.execute(
            "UPDATE repositories SET reconciliation_required=1 WHERE id=?",
            (row["repository_id"],),
        )
        db.execute(
            "UPDATE job_attempts SET finished_at=?,outcome='interrupted',error_category='interrupted',error=? WHERE job_id=? AND finished_at IS NULL",
            (self.index.now(), message, row["id"]),
        )
        kind, origin = row["kind"], row["origin"]
        db.execute(
            "UPDATE jobs SET state='pending',kind=?,origin=?,claim_token=NULL,lease_until=NULL,due_at=? WHERE id=?",
            (kind, origin, self.index.now(), row["id"]),
        )
        if (
            origin == "scheduled"
            and not db.execute(
                "SELECT 1 FROM schedules WHERE repository_id=? AND enabled=1",
                (row["repository_id"],),
            ).fetchone()
        ):
            db.execute(
                "UPDATE jobs SET state='cancelled',finished_at=? WHERE id=?",
                (self.index.now(), row["id"]),
            )

    def claim(self, job_id: int | None = None) -> dict[str, Any] | None:
        with self.index.transaction() as db:
            self._recover(db)
            query = f"""SELECT * FROM jobs j WHERE state='pending' AND due_at<=?
                AND ({BLOCKING_JOB_SQL}) IS NULL"""
            parameters: list[Any] = [self.index.now()]
            if job_id is not None:
                query += " AND id=?"
                parameters.append(job_id)
            row = db.execute(
                query + " ORDER BY origin='manual' DESC,due_at,id LIMIT 1", parameters
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE jobs SET state='running',claim_token=?,lease_until=? WHERE id=?",
                (token, self.index.now() + LEASE_SECONDS, row["id"]),
            )
            attempt = db.execute(
                "INSERT INTO job_attempts(job_id,started_at) VALUES(?,?)",
                (row["id"], self.index.now()),
            )
            return dict(row, state="running", claim_token=token, attempt_id=attempt.lastrowid)

    def validate(self, db: sqlite3.Connection, job: dict[str, Any]) -> None:
        if not db.execute(
            "SELECT 1 FROM jobs WHERE id=? AND state='running' AND claim_token=? AND lease_until>?",
            (job["id"], job["claim_token"], self.index.now()),
        ).fetchone():
            raise operation.ClaimLostError(
                "Worker claim was lost; inventory requires reconciliation"
            )

    @contextmanager
    def running(
        self, job: dict[str, Any], timeout: float, cancel: threading.Event | None = None
    ) -> Iterator[None]:
        if timeout <= 0:
            raise ValueError("Operation timeout must be positive")
        stop = threading.Event()
        lost = threading.Event()

        def heartbeat() -> None:
            while not stop.wait(20):
                try:
                    with self.index.transaction() as db:
                        self.validate(db, job)
                        db.execute(
                            "UPDATE jobs SET lease_until=? WHERE id=?",
                            (self.index.now() + LEASE_SECONDS, job["id"]),
                        )
                except sqlite3.Error, operation.ClaimLostError:
                    lost.set()
                    return

        def validate() -> None:
            if cancel is not None and cancel.is_set():
                raise operation.OperationInterrupted("Worker stopping")
            if lost.is_set():
                raise operation.ClaimLostError("Worker heartbeat failed")
            with self.index.connect() as db:
                self.validate(db, job)

        last_write = 0.0
        last_phase = ""

        def publish(snapshot: dict[str, Any]) -> None:
            nonlocal last_write, last_phase
            now = time.monotonic()
            if snapshot["phase"] == last_phase and now - last_write < 1:
                return
            try:
                with self.index.transaction() as db:
                    self.validate(db, job)
                    db.execute(
                        """INSERT INTO attempt_progress
                        (attempt_id,phase,observed_at,completed,total,unit,percentage,detail)
                        VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(attempt_id) DO UPDATE SET
                        phase=excluded.phase,observed_at=excluded.observed_at,
                        completed=excluded.completed,total=excluded.total,unit=excluded.unit,
                        percentage=excluded.percentage,detail=excluded.detail""",
                        (
                            job["attempt_id"],
                            snapshot["phase"],
                            self.index.now(),
                            snapshot.get("completed"),
                            snapshot.get("total"),
                            snapshot.get("unit"),
                            snapshot.get("percentage"),
                            snapshot.get("detail"),
                        ),
                    )
            except sqlite3.Error:
                # Progress is observational; a busy database must not fail Git.
                return
            last_write, last_phase = now, snapshot["phase"]

        token = operation.current_operation.set(
            operation.Operation(
                validate, time.monotonic() + timeout, lambda db: self.validate(db, job), publish
            )
        )
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            operation.guard()
            yield
        finally:
            operation.current_operation.reset(token)
            stop.set()
            thread.join()

    def finish(
        self, job: dict[str, Any], *, category: str | None = None, error: str | None = None
    ) -> None:
        with self.index.transaction() as db:
            self.validate(db, job)
            now = self.index.now()
            state = "succeeded" if error is None else "failed"
            retries = job["retry_count"]
            due = now
            if category in {"busy", "unavailable"}:
                state, due = "pending", now + 60
            elif category == "transport" and retries < len(RETRIES):
                state, due = "pending", now + RETRIES[retries]
                retries += 1
            scheduled = db.execute(
                "SELECT * FROM schedules WHERE repository_id=? AND enabled=1",
                (job["repository_id"],),
            ).fetchone()
            if state == "pending" and job["origin"] == "scheduled" and not scheduled:
                state = "cancelled"
            db.execute(
                """UPDATE jobs SET state=?,due_at=?,retry_count=?,finished_at=?,claim_token=NULL,
                lease_until=NULL,error_category=?,error=? WHERE id=?""",
                (
                    state,
                    due,
                    retries,
                    None if state == "pending" else now,
                    category,
                    error,
                    job["id"],
                ),
            )
            db.execute(
                "UPDATE job_attempts SET finished_at=?,outcome=?,error_category=?,error=? WHERE job_id=? AND finished_at IS NULL",
                (now, "succeeded" if error is None else "failed", category, error, job["id"]),
            )
            if job["kind"] == "convert":
                return
            if category == "structural":
                db.execute(
                    "UPDATE schedules SET blocked=1 WHERE repository_id=?", (job["repository_id"],)
                )
            elif error is None:
                db.execute(
                    "UPDATE schedules SET blocked=0 WHERE repository_id=?", (job["repository_id"],)
                )
            if scheduled and state != "pending":
                db.execute(
                    "UPDATE schedules SET next_due_at=? WHERE repository_id=?",
                    (now + scheduled["interval_seconds"], job["repository_id"]),
                )
                row = db.execute(
                    "SELECT remote_status FROM inventory WHERE id=?", (job["repository_id"],)
                ).fetchone()
                if (
                    error is None
                    and job["kind"] == "check"
                    and job["origin"] == "scheduled"
                    and row["remote_status"] in {"updates_available", "not_fetched"}
                ):
                    self.enqueue_in(db, job["repository_id"], "fetch", origin="scheduled")

    def immediate(self, repository_id: int, kind: Kind) -> dict[str, Any]:
        # Refuse contention rather than accidentally execute a pending job of a different kind.
        with self.index.transaction() as db:
            self._recover(db)
            if db.execute(
                "SELECT 1 FROM jobs WHERE repository_id=? AND (state='running' OR (kind='convert' AND state='pending'))",
                (repository_id,),
            ).fetchone():
                raise RepositoryBusyError("Repository is busy")
            db.execute(
                "UPDATE jobs SET state='cancelled',finished_at=? WHERE repository_id=? AND state='pending' AND (kind=? OR ?='fetch')",
                (self.index.now(), repository_id, kind, kind),
            )
            token = uuid.uuid4().hex
            cursor = db.execute(
                "INSERT INTO jobs(repository_id,kind,origin,state,due_at,created_at,claim_token,lease_until) VALUES(?,?,'manual','running',?,?,?,?)",
                (
                    repository_id,
                    kind,
                    self.index.now(),
                    self.index.now(),
                    token,
                    self.index.now() + LEASE_SECONDS,
                ),
            )
            job = dict(db.execute("SELECT * FROM jobs WHERE id=?", (cursor.lastrowid,)).fetchone())
            attempt = db.execute(
                "INSERT INTO job_attempts(job_id,started_at) VALUES(?,?)",
                (job["id"], self.index.now()),
            )
            job["attempt_id"] = attempt.lastrowid
            return job

    def list(self, repository_id: int | None = None) -> list[dict[str, Any]]:
        with self.index.connect() as db:
            where = "" if repository_id is None else " WHERE repository_id=?"
            rows = db.execute(
                "SELECT * FROM jobs" + where + " ORDER BY id DESC",
                () if repository_id is None else (repository_id,),
            ).fetchall()
            return [
                dict(
                    row,
                    attempts=[
                        dict(a)
                        for a in db.execute(
                            "SELECT * FROM job_attempts WHERE job_id=? ORDER BY id", (row["id"],)
                        )
                    ],
                )
                for row in rows
            ]
