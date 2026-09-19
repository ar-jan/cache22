"""Short-lived reads and commands shared by the browser and CLI adapters."""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import default_archive_dir, normalize_archive_dir
from .index import Index, repository_key
from .job_queue import Queue
from .operation import sanitize
from .repository_ref import RepositoryRef, parse_repository_url

MAX_BATCH = 10_000


def duration(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)(s|m|h|d|w)", value)
    if not match:
        raise ValueError("Use a positive duration such as 30m, 6h, or 1d")
    seconds = int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match[2]]
    if seconds > 2**63 - 1 - 2**40:
        raise ValueError("Schedule interval is too large")
    return seconds


def json_value(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: json_value(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, int) and (key.endswith("_at") or key == "lease_until"):
        return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")
    return value


def registration_target(
    index: Index, url: str, root: Path | None, case_sensitive: bool
) -> tuple[RepositoryRef, Path]:
    ref = parse_repository_url(url, case_sensitive=case_sensitive)
    if root is None:
        with index.connect() as db:
            row = db.execute(
                "SELECT archive_root FROM repositories WHERE repo_key=?", (repository_key(ref),)
            ).fetchone()
        if row:
            return ref, Path(row["archive_root"])
    return ref, normalize_archive_dir(root if root is not None else default_archive_dir())


def register_batch(
    index: Index,
    urls: list[str],
    *,
    root: Path | None = None,
    case_sensitive: bool = False,
    fetch: bool = False,
    preview: bool = False,
) -> list[dict[str, Any]]:
    if len(urls) > MAX_BATCH:
        raise ValueError(f"At most {MAX_BATCH} URLs per submission")
    results: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    projected: dict[str, dict[str, Any]] = {}
    queue = Queue(index)
    for line, raw in enumerate(urls, 1):
        url = raw.strip()
        if not url:
            continue
        result: dict[str, Any] = {"line": line, "repository_id": None, "job_id": None}
        try:
            if url in seen:
                result.update(
                    seen[url], line=line, status="duplicate", duplicate_of=seen[url]["line"]
                )
                results.append(result)
                continue
            ref, target = registration_target(index, url, root, case_sensitive)
            key = repository_key(ref)
            result["repo_key"] = key
            result["archive_root"] = str(target)
            with index.connect() as db:
                existing = db.execute(
                    "SELECT * FROM repositories WHERE repo_key=?", (key,)
                ).fetchone()
            binding = existing if existing is not None else projected.get(key)
            if binding is not None:
                index.validate_binding(binding, ref, target)
            if preview:
                result.update(
                    status="existing" if binding is not None else "new",
                    repository_id=binding["id"] if binding is not None else None,
                )
                projected[key] = {
                    "id": result["repository_id"],
                    "source_path": ref.source_path,
                    "archive_root": str(target),
                }
            else:
                with index.transaction() as db:
                    repository_id = index.add_in(db, ref, target)
                    job_id = queue.enqueue_in(db, repository_id, "fetch") if fetch else None
                result.update(
                    status="existing" if existing is not None else "registered",
                    repository_id=repository_id,
                    job_id=job_id,
                )
            seen[url] = result.copy()
        except (ValueError, OSError, sqlite3.Error) as exc:
            result.update(status="error", error=sanitize(str(exc), url))
        results.append(result)
    return results


def bulk_command(
    index: Index,
    ids: list[int],
    action: str,
    *,
    every: str | None = None,
) -> list[dict[str, Any]]:
    if not ids or len(ids) > MAX_BATCH or any(type(i) is not int or i <= 0 for i in ids):
        raise ValueError(f"Provide between 1 and {MAX_BATCH} positive repository IDs")
    if action not in {"check", "fetch", "unqueue", "schedule", "disable"}:
        raise ValueError("Unknown repository action")
    interval = duration(every or "") if action == "schedule" else None
    results = []
    queue = Queue(index)
    for repository_id in dict.fromkeys(ids):
        result: dict[str, Any] = {"repository_id": repository_id, "job_id": None}
        try:
            index.get(repository_id)
            if action in {"check", "fetch"}:
                result["job_id"] = queue.enqueue(
                    repository_id, "check" if action == "check" else "fetch"
                )
            elif action == "unqueue":
                queue.unqueue(repository_id)
            else:
                queue.schedule(repository_id, interval)
            result["status"] = "accepted"
        except (ValueError, OSError, sqlite3.Error) as exc:
            result.update(status="error", error=sanitize(str(exc)))
        results.append(result)
    return results


def selected_ids(index: Index, where: list[str], params: dict[str, Any]) -> list[int]:
    """SQL fragments come from the Datasette filter adapter, never from a command body."""
    clause = " WHERE " + " AND ".join(f"({term})" for term in where) if where else ""
    with index.connect() as db:
        db.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 2
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        db.execute("BEGIN")
        rows = db.execute(
            f"SELECT id FROM inventory{clause} ORDER BY id LIMIT {MAX_BATCH + 1}", params
        ).fetchall()
    if len(rows) > MAX_BATCH:
        raise ValueError(f"Selection exceeds {MAX_BATCH} repositories; narrow the filters")
    return [row[0] for row in rows]


def _page(limit: int, offset: int) -> None:
    if not 1 <= limit <= 500 or offset < 0:
        raise ValueError("Limit must be 1–500 and offset must be nonnegative")


def detail(index: Index, repository_id: int, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    _page(limit, offset)
    with index.connect() as db:
        db.execute("BEGIN")
        row = db.execute("SELECT * FROM inventory WHERE id=?", (repository_id,)).fetchone()
        if row is None:
            raise ValueError("Repository not found")
        repository = index._record(row)
        # Details emphasize identity; the native Datasette record exposes the full source URL.
        repository.pop("source_url")
        attempts = [
            dict(row)
            for row in db.execute(
                """SELECT a.*,j.kind,j.origin,p.phase,p.observed_at,p.completed,p.total,p.unit,p.percentage,p.detail
            FROM job_attempts a JOIN jobs j ON j.id=a.job_id
            LEFT JOIN attempt_progress p ON p.attempt_id=a.id
            WHERE j.repository_id=? ORDER BY a.id DESC LIMIT ? OFFSET ?""",
                (repository_id, limit + 1, offset),
            )
        ]
    return {
        "repository": repository,
        "attempts": attempts[:limit],
        "next_offset": offset + limit if len(attempts) > limit else None,
    }


def queue_snapshot(
    index: Index,
    *,
    section: str = "running",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    _page(limit, offset)
    now = index.now()
    predicates = {
        "running": "j.state='running'",
        "runnable": "j.state='pending' AND j.due_at<=:now AND NOT EXISTS(SELECT 1 FROM jobs active WHERE active.repository_id=j.repository_id AND active.state='running')",
        "deferred": "j.state='pending' AND (j.due_at>:now OR EXISTS(SELECT 1 FROM jobs active WHERE active.repository_id=j.repository_id AND active.state='running'))",
        "history": "j.state IN ('succeeded','failed','cancelled')",
    }
    if section not in predicates:
        raise ValueError("Unknown queue section")
    with index.connect() as db:
        db.execute("BEGIN")
        counts = {
            name: db.execute(f"SELECT count(*) FROM jobs j WHERE {where}", {"now": now}).fetchone()[
                0
            ]
            for name, where in predicates.items()
        }
        rows = [
            dict(row)
            for row in db.execute(
                f"""SELECT j.id,j.repository_id,r.repo_key,j.kind,j.origin,j.state,j.due_at,
            j.finished_at,j.retry_count,j.error_category,j.error,a.id AS attempt_id,a.started_at,
            a.finished_at AS attempt_finished_at,a.outcome,
            (SELECT count(*) FROM job_attempts n WHERE n.job_id=j.id) AS attempt_number,
            CASE WHEN j.state='running' AND j.lease_until>:now THEN 1 ELSE 0 END AS live,
            p.phase,p.observed_at,p.completed,p.total,p.unit,p.percentage,p.detail
            FROM jobs j JOIN repositories r ON r.id=j.repository_id
            LEFT JOIN job_attempts a ON a.id=(SELECT max(id) FROM job_attempts WHERE job_id=j.id)
            LEFT JOIN attempt_progress p ON p.attempt_id=a.id
            WHERE {predicates[section]}
            ORDER BY {"j.finished_at DESC,j.id DESC" if section == "history" else "j.origin='manual' DESC,j.due_at,j.id"}
            LIMIT :limit OFFSET :offset""",
                {"now": now, "limit": limit + 1, "offset": offset},
            )
        ]
        workers = [
            dict(row)
            for row in db.execute("SELECT * FROM workers ORDER BY heartbeat_at DESC LIMIT 100")
        ]
    for row in rows:
        row["elapsed_seconds"] = (
            max(0, (row["attempt_finished_at"] or now) - row["started_at"])
            if row["started_at"]
            else None
        )
    for worker in workers:
        worker["heartbeat_age_seconds"] = max(0, now - worker["heartbeat_at"])
        worker["available"] = worker["stopped_at"] is None and worker["heartbeat_age_seconds"] <= 15
    return {
        "observed_at": now,
        "section": section,
        "counts": counts,
        "jobs": rows[:limit],
        "next_offset": offset + limit if len(rows) > limit else None,
        "workers": workers,
    }
