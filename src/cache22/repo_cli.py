"""Thin CLI adapters for inventory and queue services."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from .adoption import AdoptionRequiredError
from .cli_support import (
    REPOSITORY_COLUMNS,
    RESULT_COLUMNS,
    JsonOption,
    OptionalSelectors,
    Selector,
    Selectors,
    command,
    output,
)
from .import_service import import_repository
from .import_state import clean_all_import_state, clean_repository_import_state
from .index import Index
from .inventory_service import InventoryQuery, list_inventory
from .manager_service import bulk_command, duration, json_value, register_batch
from .repo_audit import audit
from .repo_service import ImportResult, check_repository
from .repository_ref import is_repository_url
from .worker import run_continuous, run_worker, shutdown_signals

repo_app = typer.Typer(
    help="Archive Git repositories as Git mirrors or bundles.", no_args_is_help=True
)


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


@repo_app.command("add")
@command
def add(
    urls: Annotated[
        list[str], typer.Argument(help="Git URLs to register without fetching or queueing.")
    ],
    root: Path | None = None,
    case_sensitive: bool = False,
    as_json: JsonOption = False,
) -> None:
    """Register Git URLs without downloading or queueing work."""
    results = register_batch(Index.initialize(), urls, root=root, case_sensitive=case_sensitive)
    for result in results:
        result["selector"] = urls[result["line"] - 1]
    output(results, as_json, columns=RESULT_COLUMNS)
    if any(result["status"] == "error" for result in results):
        raise typer.Exit(1)


@repo_app.command("list")
@command
def list_repositories(
    q: str = "",
    host: list[str] | None = None,
    archive_root: list[str] | None = None,
    local_state: list[str] | None = None,
    remote_status: list[str] | None = None,
    storage_format: list[str] | None = None,
    queued: bool | None = None,
    running: bool | None = None,
    scheduled: bool | None = None,
    schedule_blocked: bool | None = None,
    has_error: bool | None = None,
    reconciliation_required: bool | None = None,
    sort: str = "repo_key",
    descending: bool = False,
    limit: int = 100,
    offset: int = 0,
    as_json: JsonOption = False,
) -> None:
    """List indexed repositories without scanning storage or contacting remotes."""
    query = InventoryQuery(
        q=q,
        host=tuple(host or ()),
        archive_root=tuple(archive_root or ()),
        local_state=tuple(local_state or ()),
        remote_status=tuple(remote_status or ()),
        storage_format=tuple(storage_format or ()),
        queued=queued,
        running=running,
        scheduled=scheduled,
        schedule_blocked=schedule_blocked,
        has_error=has_error,
        reconciliation_required=reconciliation_required,
        sort=sort,
        descending=descending,
    )
    output(
        list_inventory(Index(read_only=True), query, limit=limit, offset=offset),
        as_json,
        columns=REPOSITORY_COLUMNS,
    )


@repo_app.command("show")
@command
def show(selector: Selector, as_json: JsonOption = False) -> None:
    """Show indexed repository details by key or URL."""
    output(Index(read_only=True).get(selector), as_json)


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
    index = Index.initialize()
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
    output(results, as_json, columns=REPOSITORY_COLUMNS)
    if failed:
        raise typer.Exit(1)


@repo_app.command("check")
@command
def check(
    selectors: OptionalSelectors = None,
    all_repositories: bool = typer.Option(False, "--all"),
    timeout: float = 120,
    as_json: JsonOption = False,
) -> None:
    """Compare indexed repositories with their remotes without fetching."""
    _batch(selectors, all_repositories, fetch=False, timeout=timeout, as_json=as_json)


@repo_app.command("fetch")
@command
def fetch(
    selectors: OptionalSelectors = None,
    all_repositories: bool = typer.Option(False, "--all"),
    case_sensitive: bool = typer.Option(
        False, "--case-sensitive", help="Preserve remote path casing when registering a new URL."
    ),
    adopt: bool = typer.Option(
        False, "--adopt", help="Verify and initialize an existing Git mirror before updating it."
    ),
    timeout: float = 7200,
    as_json: JsonOption = False,
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
    selectors: OptionalSelectors = None,
    all_repositories: bool = typer.Option(False, "--all"),
) -> None:
    """Remove partial import state; completed archives and lock files are kept."""
    index = Index.initialize()
    removed: list[Path] = []
    if all_repositories and not selectors:
        removed.extend(clean_all_import_state(index=index))
    else:
        for selector in selected(index, selectors, all_repositories):
            record = index.find(selector)
            if record is None and not is_repository_url(selector):
                raise ValueError(f"Repository is not indexed: {selector}")
            removed.extend(
                clean_repository_import_state(
                    record["source_url"] if record else selector, index=index
                )
            )
    if not removed:
        typer.echo("No partial import state found.")
    for path in removed:
        typer.echo(f"Removed partial import state: {path}")


class JobKind(StrEnum):
    check = "check"
    fetch = "fetch"
    convert = "convert"


def _mutate(selectors: list[str], action: str, as_json: bool, *, every: str | None = None) -> None:
    index = Index.initialize()
    seen: set[int] = set()
    results: list[dict[str, Any]] = []
    for selector in dict.fromkeys(selectors):
        try:
            record = index.get(selector)
            if record["id"] in seen:
                continue
            seen.add(record["id"])
            result = bulk_command(index, [record["id"]], action, every=every)[0]
            result.update(selector=selector, repo_key=record["repo_key"])
        except (ValueError, OSError, sqlite3.Error) as exc:
            result = {"selector": selector, "status": "error", "error": str(exc)}
        results.append(result)
    output(results, as_json, columns=RESULT_COLUMNS)
    if any(result["status"] == "error" for result in results):
        raise typer.Exit(1)


@repo_app.command("queue")
@command
def enqueue(
    selectors: Selectors, kind: JobKind = JobKind.fetch, as_json: JsonOption = False
) -> None:
    """Queue checks, fetches, or offline conversion to bundles."""
    _mutate(selectors, kind.value, as_json)


@repo_app.command("unqueue")
@command
def unqueue(selectors: Selectors, as_json: JsonOption = False) -> None:
    """Cancel pending work without changing recurring schedules."""
    _mutate(selectors, "unqueue", as_json)


@repo_app.command("schedule")
@command
def schedule(
    selectors: Selectors, every: str | None = None, off: bool = False, as_json: JsonOption = False
) -> None:
    """Schedule recurring checks or turn recurring work off."""
    if (every is not None) == off:
        raise typer.BadParameter("Provide --every DURATION or --off, exclusively")
    if every is not None:
        try:
            duration(every)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    _mutate(selectors, "disable" if off else "schedule", as_json, every=every)


@repo_app.command("audit")
@command
def audit_repositories(
    fix: bool = False,
    adopt: bool = typer.Option(
        False, "--adopt", help="Verify and adopt discovered Git mirrors offline; implies --fix."
    ),
    as_json: JsonOption = False,
) -> None:
    """Inspect archive storage and optionally repair inventory or adopt mirrors."""
    index = Index.initialize() if fix or adopt else Index(read_only=True)
    issues = audit(index=index, fix=fix, adopt=adopt)
    output(issues, as_json)
    if any(not issue["fixed"] for issue in issues):
        raise typer.Exit(1)


@repo_app.command("worker")
@command
def worker(
    once: bool = False,
    check_timeout: Annotated[float, typer.Option("--timeout-check", min=0, clamp=False)] = 120,
    fetch_timeout: Annotated[float, typer.Option("--timeout-fetch", min=0, clamp=False)] = 7200,
    convert_timeout: Annotated[float, typer.Option("--timeout-convert", min=0, clamp=False)] = 7200,
    as_json: JsonOption = False,
) -> None:
    """Process jobs continuously, or drain currently runnable work with --once."""
    if min(check_timeout, fetch_timeout, convert_timeout) <= 0:
        raise typer.BadParameter("Timeouts must be positive")
    index = Index.initialize()
    if not once:
        stop = threading.Event()

        def report(result: dict[str, Any]) -> None:
            if as_json:
                typer.echo(json.dumps(json_value(result)))
            else:
                output(result, False)

        with shutdown_signals(stop):
            run_continuous(
                index=index,
                stop=stop,
                check_timeout=check_timeout,
                fetch_timeout=fetch_timeout,
                convert_timeout=convert_timeout,
                report=report,
            )
        return
    stop = threading.Event()
    with shutdown_signals(stop):
        results = run_worker(
            index=index,
            check_timeout=check_timeout,
            fetch_timeout=fetch_timeout,
            convert_timeout=convert_timeout,
            stop=stop,
        )
    output(results, as_json)
    if any(result["outcome"] == "failed" for result in results):
        raise typer.Exit(1)
