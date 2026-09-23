"""Scheduling and execution policy, applied atomically with queue transitions."""

from __future__ import annotations

import sqlite3
from typing import Any

from .archive_storage import RepositoryBusyError
from .index import Index
from .job_queue import Kind, Queue

RETRIES = (60, 300, 1800, 7200)


class Scheduler:
    def __init__(self, index: Index):
        self.index = index
        self.queue = Queue(index)

    def schedule(self, repository_id: int, interval: int | None) -> None:
        if interval is not None and interval <= 0:
            raise ValueError("Schedule interval must be positive")
        with self.index.transaction() as db:
            if interval is None:
                db.execute("UPDATE schedules SET enabled=0 WHERE repository_id=?", (repository_id,))
                self.queue.cancel_pending_in(db, repository_id, origin="scheduled")
            else:
                db.execute(
                    """INSERT INTO schedules(repository_id,enabled,interval_seconds,next_due_at) VALUES(?,1,?,?) ON CONFLICT(repository_id)
                    DO UPDATE SET enabled=1,blocked=0,interval_seconds=excluded.interval_seconds,next_due_at=excluded.next_due_at""",
                    (repository_id, interval, self.index.now()),
                )

    def tick(self) -> None:
        with self.index.transaction() as db:
            for row in db.execute(
                """SELECT repository_id FROM schedules s WHERE enabled=1 AND blocked=0 AND next_due_at<=?
                AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.repository_id=s.repository_id AND j.state IN ('pending','running'))""",
                (self.index.now(),),
            ).fetchall():
                self.queue.enqueue_in(db, row["repository_id"], "check", origin="scheduled")
            db.execute(
                "DELETE FROM workers WHERE COALESCE(stopped_at,heartbeat_at)<?",
                (self.index.now() - 30 * 86400,),
            )
            self.queue.prune_history_in(db, self.index.now() - 30 * 86400)

    def _recover(self, db: sqlite3.Connection) -> None:
        for row in db.execute(
            "SELECT * FROM jobs WHERE state='running' AND lease_until<=?", (self.index.now(),)
        ).fetchall():
            self._interrupt_in(db, dict(row), "Worker claim expired")

    def interrupt(self, job: dict[str, Any], message: str = "Operation interrupted") -> None:
        with self.index.transaction() as db:
            self.queue.validate(db, job)
            self._interrupt_in(db, job, message)

    def _interrupt_in(self, db: sqlite3.Connection, job: dict[str, Any], message: str) -> None:
        cancelled = (
            job["origin"] == "scheduled"
            and not db.execute(
                "SELECT 1 FROM schedules WHERE repository_id=? AND enabled=1",
                (job["repository_id"],),
            ).fetchone()
        )
        self.queue.interrupt_in(db, job, message, cancelled=bool(cancelled))
        db.execute(
            "UPDATE repositories SET reconciliation_required=1 WHERE id=?", (job["repository_id"],)
        )

    def claim(self, job_id: int | None = None) -> dict[str, Any] | None:
        with self.index.transaction() as db:
            self._recover(db)
            return self.queue.claim_in(db, job_id)

    def finish(
        self, job: dict[str, Any], *, category: str | None = None, error: str | None = None
    ) -> None:
        with self.index.transaction() as db:
            self.queue.validate(db, job)
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
            self.queue.finish_in(
                db,
                job,
                outcome="succeeded" if error is None else "failed",
                state=state,
                due_at=due,
                retry_count=retries,
                category=category,
                error=error,
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
                    self.queue.enqueue_in(db, job["repository_id"], "fetch", origin="scheduled")

    def immediate(self, repository_id: int, kind: Kind) -> dict[str, Any]:
        # Immediate checks may overtake a pending fetch, but never a conversion.
        with self.index.transaction() as db:
            self._recover(db)
            if db.execute(
                "SELECT 1 FROM jobs WHERE repository_id=? AND (state='running' OR (kind='convert' AND state='pending'))",
                (repository_id,),
            ).fetchone():
                raise RepositoryBusyError("Repository is busy")
            self.queue.cancel_pending_in(db, repository_id, kind=None if kind == "fetch" else kind)
            job_id = self.queue.insert_in(db, repository_id, kind)
            job = self.queue.claim_in(db, job_id, overtake_pending=True)
            assert job is not None
            return job
