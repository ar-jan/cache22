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
from .repository_ref import RepositoryRef, is_repository_url, parse_repository_url

SCHEMA_VERSION = 4

WRITABLE_REPOSITORY_FIELDS = frozenset(
    {
        "display_path",
        "host",
        "source_url",
        "source_path",
        "archive_root",
        "storage_format",
        "archive_file",
        "created_at",
        "updated_at",
        "local_state",
        "local_observed_at",
        "reconciliation_required",
        "local_head_ref",
        "local_head_oid",
        "local_head_committed_at",
        "local_ref_digest",
        "remote_head_ref",
        "remote_head_oid",
        "remote_ref_digest",
    }
)

SCHEMA = """
CREATE TABLE repositories (
 id INTEGER PRIMARY KEY, repo_key TEXT NOT NULL UNIQUE,
 display_path TEXT NOT NULL, host TEXT NOT NULL,
 source_url TEXT NOT NULL, source_path TEXT NOT NULL, archive_root TEXT NOT NULL,
 storage_format TEXT NOT NULL DEFAULT 'git' CHECK(storage_format IN ('git','bundle')),
 archive_file TEXT,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 local_state TEXT NOT NULL DEFAULT 'unknown'
   CHECK(local_state IN ('unknown','absent','ready','missing','incomplete')),
 local_observed_at INTEGER, reconciliation_required INTEGER NOT NULL DEFAULT 0,
 local_head_ref TEXT, local_head_oid TEXT, local_head_committed_at INTEGER,
 local_ref_digest TEXT, remote_head_ref TEXT, remote_head_oid TEXT, remote_ref_digest TEXT
);
CREATE TABLE schedules (
 repository_id INTEGER PRIMARY KEY REFERENCES repositories(id) ON DELETE CASCADE,
 enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
 blocked INTEGER NOT NULL DEFAULT 0 CHECK(blocked IN (0,1)),
 interval_seconds INTEGER NOT NULL CHECK(interval_seconds > 0), next_due_at INTEGER NOT NULL
);
CREATE TABLE jobs (
 id INTEGER PRIMARY KEY, repository_id INTEGER NOT NULL REFERENCES repositories(id),
 kind TEXT NOT NULL CHECK(kind IN ('check','fetch','convert')),
 origin TEXT NOT NULL CHECK(origin IN ('manual','scheduled')),
 state TEXT NOT NULL DEFAULT 'pending'
   CHECK(state IN ('pending','running','succeeded','failed','cancelled')),
 due_at INTEGER NOT NULL, created_at INTEGER NOT NULL, finished_at INTEGER,
 retry_count INTEGER NOT NULL DEFAULT 0, claim_token TEXT, lease_until INTEGER
);
CREATE UNIQUE INDEX running_repository ON jobs(repository_id) WHERE state='running';
CREATE INDEX runnable_jobs ON jobs(state,due_at,origin,id);
CREATE TABLE job_attempts (
 id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('check','fetch','convert')),
 started_at INTEGER NOT NULL, finished_at INTEGER, outcome TEXT,
 error_category TEXT, error TEXT
);
CREATE INDEX attempts_job ON job_attempts(job_id,id);
CREATE INDEX attempts_success ON job_attempts(job_id,kind,finished_at) WHERE outcome='succeeded';
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
CREATE VIEW job_errors AS
 SELECT j.id AS job_id,j.repository_id,a.kind,j.origin,j.state,j.due_at,j.retry_count,
 a.id AS attempt_id,a.finished_at AS error_at,a.outcome,a.error_category,a.error
 FROM jobs j JOIN job_attempts a ON a.id=(
   SELECT max(id) FROM job_attempts WHERE job_id=j.id AND finished_at IS NOT NULL)
 WHERE j.state IN ('pending','running','failed') AND a.outcome IN ('failed','interrupted');
CREATE VIEW inventory AS SELECT r.*,
 -- Trim non-slash characters to locate the final display-path component.
 substr(r.display_path, length(rtrim(r.display_path, replace(r.display_path, '/', ''))) + 1)
   AS project_name,
 (SELECT max(a.finished_at) FROM jobs j JOIN job_attempts a ON a.job_id=j.id
  WHERE j.repository_id=r.id AND a.kind='check' AND a.outcome='succeeded') AS last_checked_at,
 (SELECT max(a.finished_at) FROM jobs j JOIN job_attempts a ON a.job_id=j.id
  WHERE j.repository_id=r.id AND a.kind='fetch' AND a.outcome='succeeded') AS last_fetched_at,
 (SELECT max(a.finished_at) FROM jobs j JOIN job_attempts a ON a.job_id=j.id
  WHERE j.repository_id=r.id AND a.kind='convert' AND a.outcome='succeeded') AS last_converted_at,
 e.error AS last_error,e.kind AS last_error_kind,e.error_category AS last_error_category,
 e.finished_at AS last_error_at,CAST(e.id IS NOT NULL AS INTEGER) AS has_error,
 archive_root || '/' || repo_key AS repository_dir,
 archive_root || '/' || repo_key || '/' || archive_file AS archive_path,
 CASE WHEN reconciliation_required OR remote_ref_digest IS NULL THEN 'unknown'
 WHEN local_state IN ('absent','missing') THEN 'not_fetched'
 WHEN local_state != 'ready' OR local_ref_digest IS NULL THEN 'unknown'
 WHEN local_ref_digest=remote_ref_digest THEN 'current' ELSE 'updates_available' END AS remote_status,
 CAST(EXISTS(SELECT 1 FROM jobs j WHERE j.repository_id=r.id AND j.state='pending') AS INTEGER) AS queued,
 CAST(EXISTS(SELECT 1 FROM jobs j WHERE j.repository_id=r.id AND j.state='running') AS INTEGER) AS running,
 CAST(COALESCE(s.enabled,0) AS INTEGER) AS scheduled,
 CAST(COALESCE(s.blocked,0) AS INTEGER) AS schedule_blocked, s.interval_seconds, s.next_due_at
 FROM repositories r LEFT JOIN schedules s ON s.repository_id=r.id
 LEFT JOIN job_attempts e ON e.id=(SELECT attempt_id FROM job_errors
   WHERE repository_id=r.id ORDER BY error_at DESC,attempt_id DESC LIMIT 1);
"""


def index_path() -> Path:
    return (
        Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
        / "cache22/index.sqlite3"
    )


def repository_key(repository: RepositoryRef) -> str:
    return "/".join((repository.host, *repository.namespace, repository.name))


def _version_error(version: int) -> str:
    return (
        f"Unsupported repository index version: {version}; expected {SCHEMA_VERSION}. "
        "Stop Cache22 processes, back up and remove the old index and its WAL/SHM sidecars, "
        "then run 'cache22 audit --fix'. Schedules and job history are not migrated."
    )


@contextmanager
def _connect(
    path: Path, *, read_only: bool = False, timeout: float = 5
) -> Iterator[sqlite3.Connection]:
    if not path.exists():
        raise FileNotFoundError(
            f"Repository index does not exist: {path}. "
            "Run 'cache22 add URL' or 'cache22 fetch URL' to register a repository, "
            "or 'cache22 audit --fix' to rebuild from archives."
        )
    db = sqlite3.connect(
        path.as_uri() + ("?mode=ro" if read_only else "?mode=rw"),
        uri=True,
        timeout=timeout,
        isolation_level=None,
    )
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        yield db
    finally:
        db.close()


def _uninitialized(db: sqlite3.Connection) -> bool:
    """Inspect a single transaction snapshot before attempting schema creation."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return False
    if version != 0:
        raise ValueError(_version_error(version))
    if db.execute("SELECT 1 FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' LIMIT 1").fetchone():
        raise ValueError("Cannot initialize a nonempty version-zero repository index")
    return True


def _initialize_schema(db: sqlite3.Connection) -> None:
    # Check before changing journal mode; normal startup needs no write lock.
    db.execute("BEGIN")
    needed = _uninitialized(db)
    db.commit()
    if not needed:
        return
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("BEGIN IMMEDIATE")
    # Another initializer may have committed while we waited for the lock.
    if _uninitialized(db):
        # executescript implicitly commits, so execute statements individually.
        for statement in SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    db.commit()


class Index:
    def __init__(
        self,
        path: Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
        read_only: bool = False,
    ):
        """Open and validate an existing index without initializing or recovering it."""
        self.path = (path if path is not None else index_path()).expanduser().resolve()
        self.clock = clock
        self.read_only = read_only
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise ValueError(_version_error(version))

    @classmethod
    def initialize(
        cls, path: Path | None = None, *, clock: Callable[[], float] = time.time
    ) -> Index:
        """Create an index if needed, once at the owning application entry point."""
        path = (path if path is not None else index_path()).expanduser().resolve()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        deadline = time.monotonic() + 5
        while True:
            try:
                # One retry budget covers all initialization lock acquisitions.
                with _connect(path, timeout=0) as db:
                    _initialize_schema(db)
                break
            except sqlite3.OperationalError as exc:
                # Concurrent WAL setup can report BUSY without honoring busy_timeout.
                remaining = deadline - time.monotonic()
                if (
                    exc.sqlite_errorcode & 0xFF not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                    or remaining <= 0
                ):
                    raise
                time.sleep(min(0.01, remaining))
        return cls(path, clock=clock)

    def now(self) -> int:
        return int(self.clock())

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with _connect(self.path, read_only=self.read_only) as db:
            yield db

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
            (repo_key,display_path,host,source_url,source_path,archive_root,created_at,updated_at,archive_file)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                key,
                repository.display_path,
                repository.host,
                repository.clone_url,
                repository.source_path,
                str(root),
                self.now(),
                self.now(),
                repository.name + ".git",
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

    @staticmethod
    def _selector_key(selector: str | int) -> str | int:
        if isinstance(selector, str) and is_repository_url(selector):
            return repository_key(parse_repository_url(selector))
        return selector

    def find(self, selector: str | int) -> dict[str, Any] | None:
        """Look up by ID, key, or clone URL; return None when not indexed."""
        key = self._selector_key(selector)
        column = "id" if isinstance(key, int) else "repo_key"
        with self.connect() as db:
            row = db.execute(f"SELECT * FROM inventory WHERE {column}=?", (key,)).fetchone()
        return None if row is None else self._record(row)

    def get(self, selector: str | int) -> dict[str, Any]:
        record = self.find(selector)
        if record is None:
            raise ValueError(f"Repository is not indexed: {self._selector_key(selector)}")
        return record

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
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
            "last_converted_at",
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
        if not fields.keys() <= WRITABLE_REPOSITORY_FIELDS:
            raise ValueError("Unknown or immutable inventory field")
        fields["updated_at"] = self.now()
        db.execute(
            "UPDATE repositories SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
            (*fields.values(), repository_id),
        )
