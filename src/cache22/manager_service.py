"""Short-lived reads and commands shared by the browser and CLI adapters."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .index import Index, repository_key
from .inventory_service import public_record
from .job_queue import BLOCKING_JOB_SQL, Queue
from .operation import sanitize
from .repo_service import registration_target
from .scheduler import Scheduler

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
    if action not in {"check", "fetch", "convert", "unqueue", "schedule", "disable"}:
        raise ValueError("Unknown repository action")
    interval = duration(every or "") if action == "schedule" else None
    results = []
    queue = Queue(index)
    for repository_id in dict.fromkeys(ids):
        result: dict[str, Any] = {"repository_id": repository_id, "job_id": None}
        try:
            index.get(repository_id)
            if action in {"check", "fetch", "convert"}:
                result["job_id"] = queue.enqueue(
                    repository_id,
                    "convert" if action == "convert" else "check" if action == "check" else "fetch",
                )
            elif action == "unqueue":
                queue.unqueue(repository_id)
            else:
                Scheduler(index).schedule(repository_id, interval)
            result["status"] = "accepted"
        except (ValueError, OSError, sqlite3.Error) as exc:
            result.update(status="error", error=sanitize(str(exc)))
        results.append(result)
    return results


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
        repository = public_record(index._record(row))
        attempts = [
            dict(row)
            for row in db.execute(
                """SELECT a.*,j.origin,p.phase,p.observed_at,p.completed,p.total,p.unit,p.percentage,p.detail
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
    index: Index, *, section: str = "running", limit: int = 100, offset: int = 0
) -> dict[str, Any]:
    """Browser queue sections backed by the shared inspection query."""
    sections = ("running", "runnable", "deferred", "history")
    if section not in sections:
        raise ValueError("Unknown queue section")
    snapshot = jobs_snapshot(
        index, state=section, limit=limit, offset=offset, include_attempts=False
    )
    snapshot["section"] = snapshot.pop("state")
    snapshot["counts"] = {name: snapshot["counts"][name] for name in sections}
    return snapshot


def jobs_snapshot(
    index: Index,
    *,
    state: str = "all",
    repository_id: int | None = None,
    include_attempts: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    _page(limit, offset)
    now = index.now()
    predicates = {
        "all": "1",
        "pending": "j.state='pending'",
        "failed": "EXISTS (SELECT 1 FROM job_errors e WHERE e.job_id=j.id)",
        "running": "j.state='running'",
        "runnable": f"j.state='pending' AND j.due_at<=:now AND ({BLOCKING_JOB_SQL}) IS NULL",
        "deferred": f"j.state='pending' AND (j.due_at>:now OR ({BLOCKING_JOB_SQL}) IS NOT NULL)",
        "history": "j.state IN ('succeeded','failed','cancelled')",
    }
    if state not in predicates:
        raise ValueError("Unknown job state")
    scope = "(:repository_id IS NULL OR j.repository_id=:repository_id)"
    params = {"now": now, "limit": limit + 1, "offset": offset, "repository_id": repository_id}
    order = {
        "all": "j.id DESC",
        "history": "j.finished_at DESC,j.id DESC",
        "failed": "(SELECT e.error_at FROM job_errors e WHERE e.job_id=j.id) DESC,(SELECT e.attempt_id FROM job_errors e WHERE e.job_id=j.id) DESC",
    }.get(state, "j.origin='manual' DESC,j.due_at,j.id")
    with index.connect() as db:
        db.execute("BEGIN")
        counts = {
            name: db.execute(
                f"SELECT count(*) FROM jobs j WHERE ({where}) AND {scope}", params
            ).fetchone()[0]
            for name, where in predicates.items()
        }
        rows = [
            dict(row)
            for row in db.execute(
                f"""SELECT j.id,j.repository_id,r.repo_key,j.kind,j.origin,j.state,j.due_at,
            j.finished_at,j.retry_count,
            CASE WHEN j.state='pending' THEN ({BLOCKING_JOB_SQL}) END AS blocking_job_id,
            a.id AS attempt_id,a.started_at,
            a.finished_at AS attempt_finished_at,a.outcome,
            (SELECT count(*) FROM job_attempts n WHERE n.job_id=j.id) AS attempt_number,
            CASE WHEN j.state='running' AND j.lease_until>:now THEN 1 ELSE 0 END AS live,
            p.phase,p.observed_at,p.completed,p.total,p.unit,p.percentage,p.detail
            FROM jobs j JOIN repositories r ON r.id=j.repository_id
            LEFT JOIN job_attempts a ON a.id=(SELECT max(id) FROM job_attempts WHERE job_id=j.id)
            LEFT JOIN attempt_progress p ON p.attempt_id=a.id
            WHERE ({predicates[state]}) AND {scope}
            ORDER BY {order}
            LIMIT :limit OFFSET :offset""",
                params,
            )
        ]
        workers = [
            dict(row)
            for row in db.execute("SELECT * FROM workers ORDER BY heartbeat_at DESC LIMIT 100")
        ]
        for row in rows[:limit]:
            diagnostic = db.execute(
                """SELECT e.kind,e.attempt_id,e.error_at,e.outcome,e.error_category,e.error,
                (SELECT count(*) FROM job_attempts a WHERE a.job_id=e.job_id AND a.id<=e.attempt_id) AS attempt_number
                FROM job_errors e WHERE e.job_id=?""",
                (row["id"],),
            ).fetchone()
            row["diagnostic"] = dict(diagnostic) if diagnostic else None
            if include_attempts:
                row["attempts"] = [
                    dict(attempt)
                    for attempt in db.execute(
                        "SELECT * FROM job_attempts WHERE job_id=? ORDER BY id", (row["id"],)
                    )
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
        "state": state,
        "counts": counts,
        "jobs": rows[:limit],
        "next_offset": offset + limit if len(rows) > limit else None,
        "workers": workers,
    }
