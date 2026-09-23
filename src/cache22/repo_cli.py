"""Thin CLI adapters for inventory and queue services."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Annotated, Any

import typer

from .adoption import AdoptionRequiredError
from .import_service import import_repository
from .import_state import clean_all_import_state, clean_repository_import_state
from .index import Index
from .job_queue import Queue
from .manager_service import duration
from .repo_audit import audit
from .repo_service import ImportResult, add_repository, check_repository
from .repository_ref import is_repository_url
from .scheduler import Scheduler
from .worker import notify_ready, run_continuous, run_worker, shutdown_signals

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


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


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
                            "storage_format",
                            "archive_path",
                        )
                    )
                )
                if item.get("operation_error"):
                    typer.echo(f"{item['repo_key']}: {item['operation_error']}", err=True)
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


def selected(index: Index, selectors: list[str] | None, all_repositories: bool) -> list[str]:
    """Deduplicated selectors, or every indexed key for --all."""
    if bool(selectors) == all_repositories:
        raise typer.BadParameter("Provide selectors or --all, exclusively")
    if not all_repositories:
        return list(dict.fromkeys(selectors or []))
    with index.connect() as db:
        return [
            row["repo_key"]
            for row in db.execute("SELECT repo_key FROM repositories ORDER BY repo_key")
        ]


def _fetch_one(
    index: Index, selector: str, *, case_sensitive: bool, adopt: bool, timeout: float
) -> ImportResult:
    """Fetch an indexed repository, or register and fetch a new clone URL."""
    record = index.find(selector)
    if record is None and not is_repository_url(selector):
        raise ValueError(f"Repository is not indexed: {selector}")

    def attempt(adopt: bool, archive_dir: Path | None = None) -> ImportResult:
        if record is not None:
            return import_repository(
                record["source_url"],
                Path(record["archive_root"]),
                case_sensitive=True,
                adopt=adopt,
                index=index,
                timeout=timeout,
            )
        return import_repository(
            selector,
            archive_dir=archive_dir,
            case_sensitive=case_sensitive,
            adopt=adopt,
            index=index,
            timeout=timeout,
        )

    try:
        return attempt(adopt)
    except AdoptionRequiredError as exc:
        if adopt or not _is_interactive():
            raise
        typer.echo(exc.conflict_message, err=True)
        if not typer.confirm(
            "Verify and adopt the existing Git mirror, then fetch updates?", default=False, err=True
        ):
            raise ValueError("Adoption declined; the existing mirror was left untouched") from exc
        # The first attempt has released its locks. Retry against the same root
        # even if configuration changed while awaiting input.
        return attempt(True, exc.archive_dir)


def _batch(
    selectors: list[str] | None,
    all_repositories: bool,
    *,
    fetch: bool,
    case_sensitive: bool = False,
    adopt: bool = False,
    timeout: float,
    as_json: bool,
) -> None:
    index = Index()
    results: list[dict[str, Any]] = []
    failed = False
    for selector in selected(index, selectors, all_repositories):
        try:
            if fetch:
                result = _fetch_one(
                    index, selector, case_sensitive=case_sensitive, adopt=adopt, timeout=timeout
                )
                if not as_json:
                    for message in result.info_messages:
                        typer.echo(message)
            else:
                check_repository(selector, index=index, timeout=timeout)
            results.append(index.get(selector))
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            failed = True
            record = index.find(selector)
            results.append(
                dict(record, operation_error=str(exc))
                if record is not None
                else {"selector": selector, "operation_error": str(exc)}
            )
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
    """Compare indexed repositories with their remotes without fetching."""
    _batch(selectors, all_repositories, fetch=False, timeout=timeout, as_json=as_json)


@repo_app.command("fetch")
@command
def fetch(
    selectors: Annotated[list[str] | None, typer.Argument()] = None,
    all_repositories: bool = typer.Option(False, "--all"),
    case_sensitive: bool = typer.Option(
        False, "--case-sensitive", help="Preserve remote path casing when registering a new URL."
    ),
    adopt: bool = typer.Option(
        False, "--adopt", help="Verify and initialize an existing Git mirror before updating it."
    ),
    timeout: float = 7200,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Fetch repositories by key or URL; unknown URLs are registered first."""
    _batch(
        selectors,
        all_repositories,
        fetch=True,
        case_sensitive=case_sensitive,
        adopt=adopt,
        timeout=timeout,
        as_json=as_json,
    )


@repo_app.command("clean")
@command
def clean(
    selectors: Annotated[list[str] | None, typer.Argument()] = None,
    all_repositories: bool = typer.Option(False, "--all"),
) -> None:
    """Remove partial import state; completed archives and lock files are kept."""
    index = Index()
    removed: list[Path] = []
    if all_repositories and not selectors:
        removed.extend(clean_all_import_state())
    else:
        for selector in selected(index, selectors, all_repositories):
            record = index.find(selector)
            if record is None and not is_repository_url(selector):
                raise ValueError(f"Repository is not indexed: {selector}")
            removed.extend(
                clean_repository_import_state(record["source_url"] if record else selector)
            )
    if not removed:
        typer.echo("No partial import state found.")
    for path in removed:
        typer.echo(f"Removed partial import state: {path}")


@repo_app.command("convert")
@command
def convert(
    selector: str,
    to: str = typer.Option(..., "--to"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Queue offline conversion of a managed repository to a standalone bundle."""
    if to != "bundle":
        raise typer.BadParameter("Only --to bundle is supported")
    index = Index()
    record = index.get(selector)
    job_id = Queue(index).enqueue(record["id"], "convert")
    output({"repository_id": record["id"], "job_id": job_id, "status": "queued"}, as_json)


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
    Scheduler(index).schedule(index.get(selector)["id"], interval)
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
    convert_timeout: float = 7200,
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
                convert_timeout=convert_timeout,
                report=report,
                ready=notify_ready,
            )
        return
    stop = threading.Event()
    with shutdown_signals(stop):
        results = run_worker(
            check_timeout=check_timeout,
            fetch_timeout=fetch_timeout,
            convert_timeout=convert_timeout,
            stop=stop,
        )
    output(results, as_json)
    if any(result["outcome"] == "failed" for result in results):
        raise typer.Exit(1)
