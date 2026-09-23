"""Read-only job inspection."""

from __future__ import annotations

import sqlite3
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from .cli_support import JsonOption, command, output
from .index import Index, index_path
from .manager_service import jobs_snapshot, json_value


class JobState(StrEnum):
    all = "all"
    running = "running"
    pending = "pending"
    runnable = "runnable"
    deferred = "deferred"
    failed = "failed"
    history = "history"


def _fields(row: dict[str, Any], names: tuple[str, ...]) -> None:
    for name in names:
        value = row.get(name)
        if value is not None:
            text = str(value).replace("\n", "\n    ")
            typer.echo(f"  {name.replace('_', ' ')}: {text}")


def _report_snapshot(snapshot: dict[str, Any], as_json: bool) -> None:
    if as_json:
        output(snapshot, True)
        return
    typer.echo(f"Database: {snapshot['database']}\nObserved: {snapshot['observed_at']}")
    is_errors = snapshot["state"] == "failed"
    typer.echo("Queue: " + ", ".join(f"{k}: {v}" for k, v in snapshot["counts"].items()))
    if not any(worker["available"] for worker in snapshot["workers"]):
        typer.echo("No available worker.")
    for worker in snapshot["workers"]:
        state = (
            "stopped"
            if worker["stopped_at"] is not None
            else "stale"
            if not worker["available"]
            else f"running job {worker['current_job_id']}"
            if worker["current_job_id"] is not None
            else "idle"
        )
        typer.echo(
            f"Worker {worker['pid']}: {state}; heartbeat {worker['heartbeat_age_seconds']}s ago"
        )
    typer.echo(f"State: {snapshot['state']}")
    rows = snapshot["jobs"]
    if not rows:
        typer.echo(
            "No errors on this page." if is_errors else "No jobs in this section on this page."
        )
    for row in rows:
        typer.echo(f"\nJob {row['id']}: {row['repo_key']} ({row['kind']}, {row['state']})")
        _fields(row, ("origin", "attempt_id", "attempt_number", "retry_count"))
        if row["state"] == "pending":
            _fields(row, ("due_at",))
        if row["state"] == "running" and not row["live"]:
            typer.echo("  Claim expired; awaiting recovery.")
        _fields(
            row,
            (
                "blocking_job_id",
                "started_at",
                "finished_at",
                "elapsed_seconds",
                "phase",
                "observed_at",
                "completed",
                "total",
                "unit",
                "percentage",
                "detail",
            ),
        )
        if row.get("diagnostic"):
            typer.echo("  Latest completed problem:")
            if row["state"] == "running":
                typer.echo("  Retry underway; diagnostic is from the previous completed attempt.")
            _fields(
                row["diagnostic"],
                (
                    "kind",
                    "attempt_id",
                    "attempt_number",
                    "error_at",
                    "outcome",
                    "error_category",
                    "error",
                ),
            )
        for attempt in row.get("attempts", []):
            typer.echo(f"  Attempt {attempt['id']}: {attempt['outcome'] or 'running'}")
            _fields(attempt, ("started_at", "finished_at", "error_category", "error"))
    if snapshot["next_offset"] is not None:
        typer.echo(f"\nMore results: use --offset {snapshot['next_offset']}")


@command
def jobs(
    selector: Annotated[
        str | None, typer.Argument(help="Exact canonical repository key or supported Git URL.")
    ] = None,
    state: Annotated[
        JobState,
        typer.Option(
            help="View to inspect; failed includes retries with a completed problem attempt."
        ),
    ] = JobState.all,
    db: Annotated[Path | None, typer.Option(help="Read another existing index database.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 100,
    offset: Annotated[int, typer.Option(min=0)] = 0,
    as_json: JsonOption = False,
) -> None:
    """Inspect jobs, attempts, diagnostics, and workers without modifying the index."""
    path = (db if db is not None else index_path()).expanduser().resolve()
    try:
        index = Index(path, read_only=True)
        repository_id = index.get(selector)["id"] if selector is not None else None
        snapshot = jobs_snapshot(
            index, state=state.value, repository_id=repository_id, limit=limit, offset=offset
        )
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise RuntimeError(f"Cannot inspect database {path}: {exc}") from exc
    _report_snapshot(json_value(dict(snapshot, database=str(path))), as_json)
