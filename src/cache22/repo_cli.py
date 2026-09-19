"""Thin CLI adapters for inventory and queue services."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Annotated, Any

import typer

from .import_service import import_repository
from .index import Index
from .job_queue import Queue
from .manager_service import duration
from .repo_audit import audit
from .repo_service import add_repository, check_repository, run_worker
from .worker import notify_ready, run_continuous, shutdown_signals

repo_app = typer.Typer(help="Browse the repository index and manage updates.", no_args_is_help=True)
worker_app = typer.Typer(help="Execute persistent update jobs.", no_args_is_help=True)


def command[**P](function: Callable[P, None]) -> Callable[P, None]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            function(*args, **kwargs)
        except (
            ValueError,
            RuntimeError,
            OSError,
            sqlite3.Error,
            subprocess.SubprocessError,
        ) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc

    return wrapped


def _json_value(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: _json_value(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_value(v) for v in value]
    if isinstance(value, int) and (key.endswith("_at") or key == "lease_until"):
        return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")
    return value


def output(value: Any, as_json: bool) -> None:
    value = _json_value(value)
    if as_json:
        typer.echo(json.dumps(value, ensure_ascii=False, indent=2))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and "repo_key" in item:
                typer.echo(
                    "\t".join(
                        str(item[k] if item[k] is not None else "-")
                        for k in (
                            "repo_key",
                            "local_state",
                            "remote_status",
                            "local_head_committed_at",
                            "last_checked_at",
                            "mirror_path",
                        )
                    )
                )
            else:
                typer.echo(json.dumps(item, ensure_ascii=False))
    else:
        for key, item in value.items():
            typer.echo(f"{key}: {item if item is not None else '-'}")


@repo_app.command("add")
@command
def add(
    url: str,
    archive_dir: Path | None = None,
    case_sensitive: bool = False,
    fetch: bool = False,
    queue: bool = False,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    if fetch and queue:
        raise typer.BadParameter("--fetch and --queue are mutually exclusive")
    index = Index()
    record = add_repository(url, archive_dir, case_sensitive=case_sensitive, index=index)
    if fetch:
        import_repository(
            record["source_url"],
            Path(record["archive_root"]),
            "git",
            case_sensitive=True,
            index=index,
        )
    elif queue:
        Queue(index).enqueue(record["id"])
    output(index.get(record["id"]), as_json)


@repo_app.command("list")
@command
def list_repositories(
    host: str | None = None,
    local_state: str | None = None,
    remote_status: str | None = None,
    queued: bool = False,
    scheduled: bool = False,
    sort: str = "repo_key",
    descending: bool = False,
    limit: int = 100,
    offset: int = 0,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    output(
        Index().list(
            host=host,
            local_state=local_state,
            remote_status=remote_status,
            queued=queued,
            scheduled=scheduled,
            sort=sort,
            descending=descending,
            limit=limit,
            offset=offset,
        ),
        as_json,
    )


@repo_app.command("show")
@command
def show(selector: str, as_json: bool = typer.Option(False, "--json")) -> None:
    output(Index().get(selector), as_json)


def selected(index: Index, selectors: list[str], all_repositories: bool) -> list[dict[str, Any]]:
    if bool(selectors) == all_repositories:
        raise typer.BadParameter("Provide selectors or --all, exclusively")
    if all_repositories:
        with index.connect() as db:
            return [
                index.get(row["id"])
                for row in db.execute("SELECT id FROM repositories ORDER BY repo_key")
            ]
    return list({record["id"]: record for record in (index.get(s) for s in selectors)}.values())


def _batch(
    selectors: list[str],
    all_repositories: bool,
    fetch: bool,
    adopt: bool,
    timeout: float,
    as_json: bool,
) -> None:
    index = Index()
    results: list[dict[str, Any]] = []
    failed = False
    for record in selected(index, selectors, all_repositories):
        try:
            if fetch:
                import_repository(
                    record["source_url"],
                    Path(record["archive_root"]),
                    "git",
                    case_sensitive=True,
                    adopt=adopt,
                    index=index,
                    timeout=timeout,
                )
            else:
                check_repository(record["id"], index=index, timeout=timeout)
            results.append(index.get(record["id"]))
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            failed = True
            results.append(dict(index.get(record["id"]), operation_error=str(exc)))
    output(results, as_json)
    if failed:
        raise typer.Exit(1)


@repo_app.command("check")
@command
def check(
    selectors: Annotated[list[str] | None, typer.Argument()] = None,
    all_repositories: bool = typer.Option(False, "--all"),
    timeout: float = 120,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    _batch(selectors or [], all_repositories, False, False, timeout, as_json)


@repo_app.command("fetch")
@command
def fetch(
    selectors: Annotated[list[str] | None, typer.Argument()] = None,
    all_repositories: bool = typer.Option(False, "--all"),
    adopt: bool = False,
    timeout: float = 7200,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    _batch(selectors or [], all_repositories, True, adopt, timeout, as_json)


@repo_app.command("queue")
@command
def enqueue(selector: str, check: bool = False) -> None:
    index = Index()
    typer.echo(Queue(index).enqueue(index.get(selector)["id"], "check" if check else "fetch"))


@repo_app.command("unqueue")
@command
def unqueue(selector: str) -> None:
    index = Index()
    Queue(index).unqueue(index.get(selector)["id"])


@repo_app.command("schedule")
@command
def schedule(selector: str, every: str | None = None, disable: bool = False) -> None:
    if (every is not None) == disable:
        raise typer.BadParameter("Provide --every DURATION or --disable, exclusively")
    index = Index()
    try:
        interval = duration(every) if every else None
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    Queue(index).schedule(index.get(selector)["id"], interval)
    output(index.get(selector), False)


@repo_app.command("jobs")
@command
def jobs(selector: str | None = None, as_json: bool = typer.Option(False, "--json")) -> None:
    index = Index()
    output(Queue(index).list(index.get(selector)["id"] if selector else None), as_json)


@repo_app.command("audit")
@command
def audit_repositories(
    fix: bool = False,
    adopt: bool = typer.Option(
        False, "--adopt", help="Verify and adopt discovered Git mirrors offline; implies --fix."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    issues = audit(fix=fix, adopt=adopt)
    output(issues, as_json)
    if any(not issue["fixed"] for issue in issues):
        raise typer.Exit(1)


@worker_app.command("run")
@command
def worker(
    once: bool = False,
    continuous: bool = False,
    check_timeout: float = 120,
    fetch_timeout: float = 7200,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    if once == continuous:
        raise typer.BadParameter("Specify exactly one of --once or --continuous")
    if continuous:
        stop = threading.Event()

        def report(result: dict[str, Any]) -> None:
            if as_json:
                typer.echo(json.dumps(_json_value(result)))
            else:
                output(result, False)

        with shutdown_signals(stop):
            run_continuous(
                stop=stop,
                check_timeout=check_timeout,
                fetch_timeout=fetch_timeout,
                report=report,
                ready=notify_ready,
            )
        return
    stop = threading.Event()
    with shutdown_signals(stop):
        results = run_worker(check_timeout=check_timeout, fetch_timeout=fetch_timeout, stop=stop)
    output(results, as_json)
    if any(result["outcome"] == "failed" for result in results):
        raise typer.Exit(1)
