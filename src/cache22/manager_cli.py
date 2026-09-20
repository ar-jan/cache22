"""Own one web process and, unless web-only, one independent worker."""

from __future__ import annotations

import json
import os
import selectors
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from .index import Index, index_path
from .manager_service import error_snapshot, json_value, queue_snapshot
from .repo_cli import command
from .worker import shutdown_signals

manager_app = typer.Typer(help="Run the manager and inspect its queue.", no_args_is_help=True)


class QueueSection(StrEnum):
    running = "running"
    runnable = "runnable"
    deferred = "deferred"
    history = "history"


def _inspect(
    path: Path | None, section: QueueSection | None, limit: int, offset: int
) -> dict[str, Any]:
    selected_path = (path if path is not None else index_path()).expanduser().resolve()
    try:
        index = Index(selected_path, read_only=True)
        snapshot = (
            error_snapshot(index, limit=limit, offset=offset)
            if section is None
            else queue_snapshot(index, section=section.value, limit=limit, offset=offset)
        )
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise RuntimeError(f"Cannot inspect database {selected_path}: {exc}") from exc
    return json_value(dict(snapshot, database=str(selected_path)))


def _fields(row: dict[str, Any], names: tuple[str, ...]) -> None:
    for name in names:
        value = row.get(name)
        if value is not None:
            text = str(value).replace("\n", "\n    ")
            typer.echo(f"  {name.replace('_', ' ')}: {text}")


def _report_snapshot(snapshot: dict[str, Any], as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return
    typer.echo(f"Database: {snapshot['database']}\nObserved: {snapshot['observed_at']}")
    is_errors = "errors" in snapshot
    if not is_errors:
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
        typer.echo(f"Section: {snapshot['section']}")
    rows = snapshot["errors" if is_errors else "jobs"]
    if not rows:
        typer.echo(
            "No errors on this page." if is_errors else "No jobs in this section on this page."
        )
    for row in rows:
        typer.echo(f"\nJob {row['id']}: {row['repo_key']} ({row['kind']}, {row['state']})")
        _fields(row, ("origin", "attempt_id", "attempt_number", "retry_count"))
        if row["state"] == "pending":
            _fields(row, ("due_at",))
        if is_errors:
            if row["state"] == "running":
                typer.echo("  Retry underway; diagnostic is from the previous completed attempt.")
            _fields(row, ("error_at", "outcome", "error_category", "error"))
        else:
            if row["state"] == "running" and not row["live"]:
                typer.echo("  Claim expired; awaiting recovery.")
            _fields(
                row,
                (
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
                    "error_category",
                    "error",
                ),
            )
    if snapshot["next_offset"] is not None:
        typer.echo(f"\nMore results: use --offset {snapshot['next_offset']}")


@manager_app.command(
    "errors", help="List the latest failed or interrupted attempt per problem job."
)
@command
def errors(
    db: Annotated[Path | None, typer.Option(help="Read another index database.")] = None,
    limit: int = typer.Option(100, min=1, max=500),
    offset: int = typer.Option(0, min=0),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    _report_snapshot(_inspect(db, None, limit, offset), as_json)


@manager_app.command("queue", help="Inspect queue counts, workers, and a page of jobs.")
@command
def queue(
    db: Annotated[Path | None, typer.Option(help="Read another index database.")] = None,
    section: QueueSection = QueueSection.running,
    limit: int = typer.Option(100, min=1, max=500),
    offset: int = typer.Option(0, min=0),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    _report_snapshot(_inspect(db, section, limit, offset), as_json)


@manager_app.command("run")
@command
def run(port: int = 8001, web_only: bool = False) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    Index()  # Initialize WAL/schema before children open the same new database.
    stop = threading.Event()
    children: list[subprocess.Popen[bytes]] = []
    with shutdown_signals(stop), selectors.DefaultSelector() as selector:
        try:
            commands = [[sys.executable, "-m", "cache22.manager.web", str(port)]]
            if not web_only:
                commands.append([sys.executable, "-m", "cache22", "worker", "run", "--continuous"])
            for args in commands:
                read_fd, write_fd = os.pipe()
                try:
                    child = subprocess.Popen(
                        args,
                        env={
                            **os.environ,
                            "CACHE22_READY_FD": str(write_fd),
                            "DATASETTE_LOAD_PLUGINS": "",
                        },
                        pass_fds=(write_fd,),
                        start_new_session=True,
                    )
                    children.append(child)
                    selector.register(read_fd, selectors.EVENT_READ)
                except BaseException:
                    os.close(read_fd)
                    raise
                finally:
                    os.close(write_fd)
            deadline = time.monotonic() + 20
            while selector.get_map() and not stop.is_set():
                if any(child.poll() is not None for child in children):
                    raise RuntimeError("Manager child exited during startup")
                if time.monotonic() >= deadline:
                    raise RuntimeError("Manager startup timed out")
                for key, _ in selector.select(0.2):
                    if os.read(key.fd, 1) != b"1":
                        raise RuntimeError("Manager child failed before readiness")
                    selector.unregister(key.fd)
                    os.close(key.fd)
            if not stop.is_set():
                typer.echo(f"Cache22 manager: http://127.0.0.1:{port}/")
            while not stop.wait(0.2):
                if any(child.poll() is not None for child in children):
                    raise RuntimeError(
                        "Manager child exited unexpectedly; stopping remaining children"
                    )
        finally:
            for key in list(selector.get_map().values()):
                os.close(key.fd)
            for child in children:
                if child.poll() is None:
                    child.send_signal(signal.SIGTERM)
            deadline = time.monotonic() + 10
            for child in children:
                try:
                    child.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    typer.echo(f"Child {child.pid} did not stop gracefully; terminating", err=True)
                    child.kill()
                    child.wait()
