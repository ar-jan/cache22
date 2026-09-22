"""Noninteractive services shared by the CLI and Datasette manager."""

from __future__ import annotations

import sqlite3
import subprocess
import threading
from pathlib import Path
from typing import Any

from . import operation
from .archive_layout import archive_paths_for_repository
from .archive_storage import RepositoryBusyError, RepositoryStorage, repository_operation
from .git_observation import local_fields, remote_fields, remote_snapshot
from .index import Index
from .job_queue import Queue
from .repository_ref import parse_repository_url


def add_repository(
    url: str, root: Path | None = None, *, case_sensitive: bool = False, index: Index | None = None
) -> dict[str, Any]:
    index = index or Index()
    from .manager_service import registration_target

    ref, target = registration_target(index, url, root, case_sensitive)
    return index.add(ref, target)


def category_for(exc: BaseException) -> str:
    if isinstance(exc, RepositoryBusyError):
        return "busy"
    if isinstance(exc, (operation.ClaimLostError, operation.OperationInterrupted)):
        return "interrupted"
    if isinstance(exc, (FileNotFoundError, PermissionError)):
        return "unavailable"
    if isinstance(exc, (operation.TransportError, subprocess.TimeoutExpired)):
        return "transport"
    if exc.__cause__ is not None:
        return category_for(exc.__cause__)
    return "structural"


def error_text(exc: BaseException, url: str) -> str:
    # Do not copy credential-bearing clone URLs into persisted diagnostics.
    return operation.sanitize(str(exc), url)


def publish_local(index: Index, record: dict[str, Any], storage: RepositoryStorage | None) -> None:
    operation.progress("index publication")
    operation.guard()
    fields = local_fields(
        storage,
        record["source_path"],
        previously_ready=record["local_state"] in {"ready", "missing"},
        expected_format=record["storage_format"],
    )
    index.update(
        record["id"], **fields, local_observed_at=index.now(), reconciliation_required=False
    )


def publish_remote(index: Index, record: dict[str, Any]) -> None:
    operation.progress("remote observation")
    index.update(record["id"], last_check_attempt_at=index.now())
    try:
        snapshot = remote_snapshot(record["source_url"])
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        index.update(
            record["id"],
            check_outcome="failed",
            check_error_category=category_for(exc),
            check_error=error_text(exc, record["source_url"]),
            check_error_at=index.now(),
        )
        raise
    index.update(
        record["id"],
        **remote_fields(snapshot),
        last_checked_at=index.now(),
        check_outcome="succeeded",
        check_error=None,
        check_error_category=None,
        check_error_at=None,
    )


def check_repository(
    selector: str | int, *, index: Index | None = None, timeout: float = 120
) -> dict[str, Any]:
    if timeout <= 0:
        raise ValueError("Operation timeout must be positive")
    index = index or Index()
    record = index.get(selector)
    queue = Queue(index)
    job = queue.immediate(record["id"], "check")
    execute_job(index, job, check_timeout=timeout)
    return index.get(record["id"])


def check_locked(index: Index, record: dict[str, Any]) -> None:
    root = Path(record["archive_root"])
    if not root.is_dir():
        raise FileNotFoundError(f"Archive root unavailable: {root}")
    ref = parse_repository_url(record["source_url"], case_sensitive=True)
    paths = archive_paths_for_repository(root, ref)
    with repository_operation(root, paths) as storage:
        operation.guard()
        if (
            storage is None
            and paths.repository_dir.exists()
            and any(paths.repository_dir.iterdir())
        ):
            index.update(record["id"], local_state="incomplete", reconciliation_required=True)
            raise ValueError("Existing storage is unowned; explicit verified adoption is required")
        publish_local(index, record, storage)
        if index.get(record["id"])["local_state"] == "incomplete":
            raise ValueError(
                "Archive is incomplete; explicit cleanup or verified adoption is required"
            )
        publish_remote(index, record)


def execute_job(
    index: Index,
    job: dict[str, Any],
    *,
    check_timeout: float = 120,
    fetch_timeout: float = 7200,
    convert_timeout: float = 7200,
    adopt: bool = False,
    archive_type: str = "git",
    source_url: str | None = None,
    cancel: threading.Event | None = None,
) -> Any:
    queue = Queue(index)
    record = index.get(job["repository_id"])
    kind = job["kind"]
    result: Any = None
    try:
        timeout = {"check": check_timeout, "fetch": fetch_timeout, "convert": convert_timeout}[kind]
        with queue.running(job, timeout, cancel):
            operation.progress("validation")
            index.update(record["id"], **{f"last_{kind}_attempt_at": index.now()})
            if kind == "check":
                check_locked(index, record)
            elif kind == "convert":
                result = convert_locked(index, record)
                index.update(
                    record["id"],
                    last_converted_at=index.now(),
                    convert_outcome="succeeded",
                    convert_error=None,
                    convert_error_category=None,
                    convert_error_at=None,
                )
            else:
                from .config import normalize_archive_type
                from .import_service import _import_repository

                if not Path(record["archive_root"]).is_dir():
                    raise FileNotFoundError(f"Archive root unavailable: {record['archive_root']}")
                result = _import_repository(
                    source_url if source_url is not None else record["source_url"],
                    Path(record["archive_root"]),
                    normalize_archive_type(archive_type),
                    case_sensitive=True,
                    adopt=adopt,
                    index=index,
                    record=record,
                )
    except operation.OperationInterrupted, KeyboardInterrupt:
        queue.interrupt(job)
        raise
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, sqlite3.Error) as exc:
        category = category_for(exc)
        message = error_text(exc, record["source_url"])
        if not isinstance(exc, operation.ClaimLostError):
            # Publication must remain fenced even after the operation context exits.
            with index.transaction() as db:
                queue.validate(db, job)
                index.update_in(
                    db,
                    record["id"],
                    **{
                        f"{kind}_outcome": "failed",
                        f"{kind}_error_category": category,
                        f"{kind}_error": message,
                        f"{kind}_error_at": index.now(),
                    },
                )
            queue.finish(job, category=category, error=message)
        raise
    else:
        queue.finish(job)
    return result


def run_worker(
    *,
    index: Index | None = None,
    check_timeout: float = 120,
    fetch_timeout: float = 7200,
    convert_timeout: float = 7200,
    stop: threading.Event | None = None,
) -> list[dict[str, Any]]:
    from .worker import run_continuous

    outcomes: list[dict[str, Any]] = []
    run_continuous(
        index=index,
        stop=stop,
        check_timeout=check_timeout,
        fetch_timeout=fetch_timeout,
        convert_timeout=convert_timeout,
        report=outcomes.append,
        once=True,
    )
    return outcomes


def convert_locked(index: Index, record: dict[str, Any]) -> Path:
    from .git_bundle import materialize

    root = Path(record["archive_root"])
    ref = parse_repository_url(record["source_url"], case_sensitive=True)
    paths = archive_paths_for_repository(root, ref)
    with repository_operation(root, paths) as storage:
        if storage is None:
            raise ValueError("Conversion requires an existing Cache22-managed repository")
        if (
            record["storage_format"] == "bundle"
            and storage.entry(paths.bundle_manifest.name) is None
        ):
            raise ValueError("Selected bundle manifest is missing; restore it before conversion")
        index.update(record["id"], reconciliation_required=True)
        result = materialize(
            storage, record["source_path"], publish=lambda: publish_local(index, record, storage)
        )
        return result
