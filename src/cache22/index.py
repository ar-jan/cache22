"""Machine-local inventory. No inventory query touches an archive or invokes Git."""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .operation import current_operation
from .repository_ref import RepositoryRef, parse_repository_url

SCHEMA = """
CREATE TABLE repositories (
 id INTEGER PRIMARY KEY, repo_key TEXT NOT NULL UNIQUE,
 project_name TEXT NOT NULL, display_path TEXT NOT NULL, host TEXT NOT NULL,
 source_url TEXT NOT NULL, source_path TEXT NOT NULL, archive_root TEXT NOT NULL,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 local_state TEXT NOT NULL DEFAULT 'unknown'
   CHECK(local_state IN ('unknown','absent','ready','missing','incomplete')),
 local_observed_at INTEGER, reconciliation_required INTEGER NOT NULL DEFAULT 0,
 local_head_ref TEXT, local_head_oid TEXT, local_head_committed_at INTEGER,
 local_ref_digest TEXT, remote_head_ref TEXT, remote_head_oid TEXT, remote_ref_digest TEXT,
 last_check_attempt_at INTEGER, last_checked_at INTEGER,
 last_fetch_attempt_at INTEGER, last_fetched_at INTEGER,
 check_outcome TEXT, fetch_outcome TEXT,
 check_error_category TEXT, check_error TEXT, check_error_at INTEGER,
 fetch_error_category TEXT, fetch_error TEXT, fetch_error_at INTEGER
);
CREATE TABLE schedules (
 repository_id INTEGER PRIMARY KEY REFERENCES repositories(id) ON DELETE CASCADE,
 enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
 blocked INTEGER NOT NULL DEFAULT 0 CHECK(blocked IN (0,1)),
 interval_seconds INTEGER NOT NULL CHECK(interval_seconds > 0), next_due_at INTEGER NOT NULL
);
CREATE TABLE jobs (
 id INTEGER PRIMARY KEY, repository_id INTEGER NOT NULL REFERENCES repositories(id),
 kind TEXT NOT NULL CHECK(kind IN ('check','fetch')),
 origin TEXT NOT NULL CHECK(origin IN ('manual','scheduled')),
 state TEXT NOT NULL DEFAULT 'pending'
   CHECK(state IN ('pending','running','succeeded','failed','cancelled')),
 due_at INTEGER NOT NULL, created_at INTEGER NOT NULL, finished_at INTEGER,
 retry_count INTEGER NOT NULL DEFAULT 0, claim_token TEXT, lease_until INTEGER,
 error_category TEXT, error TEXT
);
CREATE UNIQUE INDEX pending_repository ON jobs(repository_id) WHERE state='pending';
CREATE UNIQUE INDEX running_repository ON jobs(repository_id) WHERE state='running';
CREATE INDEX runnable_jobs ON jobs(state,due_at,origin,id);
CREATE TABLE job_attempts (
 id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
 started_at INTEGER NOT NULL, finished_at INTEGER, outcome TEXT,
 error_category TEXT, error TEXT
);
CREATE INDEX attempts_job ON job_attempts(job_id,id);
CREATE INDEX jobs_history ON jobs(finished_at,id);
CREATE INDEX jobs_repository_history ON jobs(repository_id,id);
CREATE TABLE attempt_progress (
 attempt_id INTEGER PRIMARY KEY REFERENCES job_attempts(id) ON DELETE CASCADE,
 phase TEXT NOT NULL, observed_at INTEGER NOT NULL,
 completed INTEGER, total INTEGER, unit TEXT, percentage REAL, detail TEXT
);
CREATE TABLE workers (
 id TEXT PRIMARY KEY, pid INTEGER NOT NULL, started_at INTEGER NOT NULL,
 heartbeat_at INTEGER NOT NULL, stopped_at INTEGER, current_job_id INTEGER
);
CREATE INDEX workers_heartbeat ON workers(heartbeat_at);
CREATE INDEX schedules_due ON schedules(enabled,next_due_at);
CREATE INDEX repository_inventory ON repositories(local_state,host,repo_key);
CREATE VIEW inventory AS SELECT r.*,
 CAST((check_error IS NOT NULL OR fetch_error IS NOT NULL) AS INTEGER) AS has_error,
 archive_root || '/' || repo_key AS repository_dir,
 CASE WHEN reconciliation_required OR remote_ref_digest IS NULL THEN 'unknown'
 WHEN local_state IN ('absent','missing') THEN 'not_fetched'
 WHEN local_state != 'ready' OR local_ref_digest IS NULL THEN 'unknown'
 WHEN local_ref_digest=remote_ref_digest THEN 'current' ELSE 'updates_available' END AS remote_status,
 CAST(EXISTS(SELECT 1 FROM jobs j WHERE j.repository_id=r.id AND j.state='pending') AS INTEGER) AS queued,
 CAST(EXISTS(SELECT 1 FROM jobs j WHERE j.repository_id=r.id AND j.state='running') AS INTEGER) AS running,
 CAST(COALESCE(s.enabled,0) AS INTEGER) AS scheduled,
 CAST(COALESCE(s.blocked,0) AS INTEGER) AS schedule_blocked, s.interval_seconds, s.next_due_at
 FROM repositories r LEFT JOIN schedules s ON s.repository_id=r.id;
"""


def index_path() -> Path:
    return (
        Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
        / "cache22/index.sqlite3"
    )


def repository_key(repository: RepositoryRef) -> str:
    return "/".join((repository.host, *repository.namespace, repository.name))


class Index:
    def __init__(
        self,
        path: Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
        read_only: bool = False,
    ):
        self.path = path if path is not None else index_path()
        self.clock = clock
        self.read_only = read_only
        if read_only:
            self.path = self.path.expanduser().resolve()
            with self.connect() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version != 1:
                    raise ValueError(f"Unsupported repository index version: {version}")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                # execute statements individually: executescript implicitly commits.
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("PRAGMA user_version=1")
            elif version != 1:
                raise ValueError(f"Unsupported repository index version: {version}")
            db.commit()

    def now(self) -> int:
        return int(self.clock())

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(
            self.path.as_uri() + "?mode=ro" if self.read_only else self.path,
            uri=self.read_only,
            timeout=5,
            isolation_level=None,
        )
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def add(
        self, repository: RepositoryRef, root: Path, *, importing: bool = False
    ) -> dict[str, Any]:
        with self.transaction() as db:
            self.add_in(db, repository, root, importing=importing)
        return self.get(repository_key(repository))

    def add_in(
        self,
        db: sqlite3.Connection,
        repository: RepositoryRef,
        root: Path,
        *,
        importing: bool = False,
    ) -> int:
        """Register using the caller's transaction, including source/root validation."""
        key = repository_key(repository)
        existing = db.execute("SELECT * FROM repositories WHERE repo_key=?", (key,)).fetchone()
        if existing:
            self.validate_binding(existing, repository, root, importing=importing)
            return existing["id"]
        cursor = db.execute(
            """INSERT INTO repositories
            (repo_key,project_name,display_path,host,source_url,source_path,archive_root,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                key,
                repository.display_path.rsplit("/", 1)[-1],
                repository.display_path,
                repository.host,
                repository.clone_url,
                repository.source_path,
                str(root),
                self.now(),
                self.now(),
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    @staticmethod
    def validate_binding(
        existing: Any, repository: RepositoryRef, root: Path, *, importing: bool = False
    ) -> None:
        if existing["archive_root"] != str(root):
            raise ValueError(
                f"Repository already assigned to {existing['archive_root']}: {repository_key(repository)}"
            )
        if existing["source_path"] != repository.source_path and not importing:
            raise ValueError(
                f"Repository source conflict: stored {existing['source_path']}; requested {repository.source_path}"
            )

    def get(self, selector: str | int) -> dict[str, Any]:
        if isinstance(selector, str) and (
            "://" in selector or "@" in selector.split("/", 1)[0] and ":" in selector
        ):
            selector = repository_key(parse_repository_url(selector))
        column = "id" if isinstance(selector, int) else "repo_key"
        with self.connect() as db:
            row = db.execute(f"SELECT * FROM inventory WHERE {column}=?", (selector,)).fetchone()
        if row is None:
            raise ValueError(f"Repository is not indexed: {selector}")
        return self._record(row)

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["mirror_path"] = (
            result["repository_dir"] + "/" + result["repo_key"].rsplit("/", 1)[-1] + ".git"
        )
        for field in (
            "queued",
            "running",
            "scheduled",
            "schedule_blocked",
            "reconciliation_required",
        ):
            result[field] = bool(result[field])
        return result

    def list(
        self,
        *,
        host: str | None = None,
        local_state: str | None = None,
        remote_status: str | None = None,
        queued: bool = False,
        scheduled: bool = False,
        sort: str = "repo_key",
        descending: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if sort not in {
            "repo_key",
            "project_name",
            "local_head_committed_at",
            "last_checked_at",
            "last_fetched_at",
            "remote_status",
        }:
            raise ValueError(f"Unsupported sort column: {sort}")
        if limit < 1 or offset < 0:
            raise ValueError("Limit must be positive and offset nonnegative")
        clauses: list[str] = []
        parameters: list[Any] = []
        for key, value in [
            ("host", host),
            ("local_state", local_state),
            ("remote_status", remote_status),
        ]:
            if value is not None:
                clauses.append(f"{key}=?")
                parameters.append(value)
        if queued:
            clauses.append("queued=1")
        if scheduled:
            clauses.append("scheduled=1")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM inventory{where} ORDER BY {sort} {'DESC' if descending else 'ASC'},id LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
        return [self._record(row) for row in rows]

    def update(self, repository_id: int, **fields: Any) -> None:
        with self.transaction() as db:
            self.update_in(db, repository_id, **fields)

    def update_in(self, db: sqlite3.Connection, repository_id: int, **fields: Any) -> None:
        operation = current_operation.get()
        if operation is not None:
            operation.validate_db(db)
        columns = {row[1] for row in db.execute("PRAGMA table_info(repositories)")}
        if not fields.keys() <= columns - {"id", "repo_key"}:
            raise ValueError("Unknown or immutable inventory field")
        fields["updated_at"] = self.now()
        db.execute(
            "UPDATE repositories SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
            (*fields.values(), repository_id),
        )
