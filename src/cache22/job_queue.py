"""Job persistence, coalescing, claims, and fenced attempt writes."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Literal

from . import operation
from .index import Index

Kind = Literal["check", "fetch", "convert"]
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
        return self.insert_in(db, repository_id, kind, origin=origin)

    def insert_in(
        self, db: sqlite3.Connection, repository_id: int, kind: Kind, *, origin: str = "manual"
    ) -> int:
        """Insert a fresh pending job without coalescing."""
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
            self.cancel_pending_in(db, repository_id)

    def cancel_pending_in(
        self,
        db: sqlite3.Connection,
        repository_id: int,
        *,
        origin: str | None = None,
        kind: Kind | None = None,
    ) -> None:
        query = "UPDATE jobs SET state='cancelled',finished_at=? WHERE repository_id=? AND state='pending'"
        parameters: list[Any] = [self.index.now(), repository_id]
        if origin is not None:
            query += " AND origin=?"
            parameters.append(origin)
        if kind is not None:
            query += " AND kind=?"
            parameters.append(kind)
        db.execute(query, parameters)

    def claim_in(
        self,
        db: sqlite3.Connection,
        job_id: int | None = None,
        *,
        overtake_pending: bool = False,
    ) -> dict[str, Any] | None:
        """Claim due work; only the scheduler may authorize overtaking pending jobs."""
        blocking = (
            "SELECT id FROM jobs prior WHERE prior.repository_id=j.repository_id AND prior.state='running'"
            if overtake_pending
            else BLOCKING_JOB_SQL
        )
        query = f"SELECT * FROM jobs j WHERE state='pending' AND due_at<=? AND ({blocking}) IS NULL"
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
        lease_until = self.index.now() + LEASE_SECONDS
        db.execute(
            "UPDATE jobs SET state='running',claim_token=?,lease_until=? WHERE id=?",
            (token, lease_until, row["id"]),
        )
        attempt = db.execute(
            "INSERT INTO job_attempts(job_id,kind,started_at) VALUES(?,?,?)",
            (row["id"], row["kind"], self.index.now()),
        )
        return dict(
            row,
            state="running",
            claim_token=token,
            lease_until=lease_until,
            attempt_id=attempt.lastrowid,
        )

    def validate(self, db: sqlite3.Connection, job: dict[str, Any]) -> None:
        if not db.execute(
            "SELECT 1 FROM jobs WHERE id=? AND state='running' AND claim_token=? AND lease_until>?",
            (job["id"], job["claim_token"], self.index.now()),
        ).fetchone():
            raise operation.ClaimLostError(
                "Worker claim was lost; inventory requires reconciliation"
            )

    def heartbeat(self, job: dict[str, Any]) -> None:
        with self.index.transaction() as db:
            self.validate(db, job)
            db.execute(
                "UPDATE jobs SET lease_until=? WHERE id=?",
                (self.index.now() + LEASE_SECONDS, job["id"]),
            )

    def publish_progress(self, job: dict[str, Any], snapshot: dict[str, Any]) -> None:
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

    def finish_in(
        self,
        db: sqlite3.Connection,
        job: dict[str, Any],
        *,
        outcome: Literal["succeeded", "failed"],
        state: str,
        due_at: int,
        retry_count: int,
        category: str | None,
        error: str | None,
    ) -> None:
        """Finalize a live attempt with the scheduler's explicit disposition."""
        self.validate(db, job)
        now = self.index.now()
        db.execute(
            """UPDATE jobs SET state=?,due_at=?,retry_count=?,finished_at=?,claim_token=NULL,
            lease_until=NULL WHERE id=?""",
            (
                state,
                due_at,
                retry_count,
                None if state == "pending" else now,
                job["id"],
            ),
        )
        db.execute(
            "UPDATE job_attempts SET finished_at=?,outcome=?,error_category=?,error=? WHERE job_id=? AND finished_at IS NULL",
            (now, outcome, category, error, job["id"]),
        )

    def interrupt_in(
        self,
        db: sqlite3.Connection,
        job: dict[str, Any],
        message: str,
        *,
        cancelled: bool,
    ) -> None:
        """Release a claim already validated or found expired in the caller's transaction."""
        now = self.index.now()
        db.execute(
            "UPDATE job_attempts SET finished_at=?,outcome='interrupted',error_category='interrupted',error=? WHERE job_id=? AND finished_at IS NULL",
            (now, message, job["id"]),
        )
        db.execute(
            "UPDATE jobs SET state=?,claim_token=NULL,lease_until=NULL,due_at=?,finished_at=? WHERE id=?",
            ("cancelled" if cancelled else "pending", now, now if cancelled else None, job["id"]),
        )

    def prune_history_in(self, db: sqlite3.Connection, cutoff: int) -> None:
        # Keep the evidence for each last-success timestamp beyond history expiry.
        db.execute(
            """WITH latest_success AS (
                SELECT a.job_id,row_number() OVER (
                    PARTITION BY j.repository_id,a.kind
                    ORDER BY a.finished_at DESC,a.id DESC
                ) AS position
                FROM job_attempts a JOIN jobs j ON j.id=a.job_id
                WHERE a.outcome='succeeded'
            )
            DELETE FROM jobs WHERE state IN ('succeeded','failed','cancelled') AND finished_at<?
            AND id NOT IN (SELECT job_id FROM latest_success WHERE position=1)""",
            (cutoff,),
        )

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
