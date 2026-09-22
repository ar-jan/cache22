"""Noninteractive services shared by the CLI and Datasette manager."""

from __future__ import annotations

import sqlite3
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import operation
from .archive_storage import RepositoryBusyError
from .config import default_archive_dir, normalize_archive_dir
from .git_observation import remote_fields, remote_snapshot
from .index import Index, repository_key
from .job_queue import Queue
from .repository_ref import RepositoryRef, parse_repository_url
from .storage import Repository, absent_fields


@dataclass(frozen=True, slots=True)
class ImportResult:
    archive_path: Path
    info_messages: tuple[str, ...] = ()
    repository: dict[str, Any] | None = None


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


def add_repository(
    url: str, root: Path | None = None, *, case_sensitive: bool = False, index: Index | None = None
) -> dict[str, Any]:
    index = index or Index()
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


def publish_local(index: Index, record: dict[str, Any], repo: Repository | None) -> None:
    operation.progress("index publication")
    operation.guard()
    previously_ready = record["local_state"] in {"ready", "missing"}
    fields = (
        absent_fields(previously_ready)
        if repo is None
        else repo.observe_local(
            record["source_path"],
            previously_ready=previously_ready,
            expected_format=record["storage_format"],
        )
    )
    index.update(
        record["id"], **fields, local_observed_at=index.now(), reconciliation_required=False
    )


def publish_remote(index: Index, record: dict[str, Any]) -> None:
    operation.progress("remote observation")
    snapshot = remote_snapshot(record["source_url"])
    index.update(record["id"], **remote_fields(snapshot))


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


def fetch_locked(
    index: Index, record: dict[str, Any], *, url: str, adopt: bool = False
) -> ImportResult:
    repository = parse_repository_url(url, case_sensitive=True)
    resolved_archive_dir = normalize_archive_dir(Path(record["archive_root"]))
    index.update(record["id"], reconciliation_required=True)
    adopted = False

    def prepare(repo: Repository) -> None:
        nonlocal adopted
        repo.require_selected_format(record["storage_format"])
        adopted = repo.prepare_import(source_path=repository.source_path, adopt=adopt)

    with Repository.open(
        resolved_archive_dir,
        repository,
        create=record["storage_format"] != "bundle",
        prepare=prepare,
    ) as repo:
        if repo is None:
            raise ValueError("Selected bundle storage is missing; restore it before fetching")
        operation.guard()
        repo.require_selected_format(record["storage_format"])
        repo.bind_source(repository.source_path)
        index.update(
            record["id"], source_path=repository.source_path, source_url=repository.clone_url
        )
        record = index.get(record["id"])
        try:
            fetched = repo.fetch(
                repository.clone_url, publish=lambda: publish_local(index, record, repo)
            )
            result = ImportResult(fetched.archive_path, fetched.info_messages)
            publish_local(index, record, repo)
            if index.get(record["id"])["local_state"] != "ready":
                raise ValueError("Imported mirror could not be validated for the inventory")
            # A transport failure in this optional probe does not fail the fetch job.
            try:
                publish_remote(index, record)
            except operation.TransportError:
                pass
            if adopted:
                return ImportResult(
                    result.archive_path,
                    (
                        f"INFO: adopted Git mirror: {repo.paths.mirror_repository}",
                        *result.info_messages,
                    ),
                )
            return result
        except OSError, RuntimeError, ValueError, subprocess.SubprocessError:
            # Observe refs even when a fetch updated them but HEAD publication failed.
            try:
                publish_local(index, record, repo)
            except OSError, RuntimeError, ValueError, subprocess.SubprocessError:
                pass
            raise
        finally:
            repo.release_unused_source()


def check_locked(index: Index, record: dict[str, Any]) -> None:
    root = Path(record["archive_root"])
    if not root.is_dir():
        raise FileNotFoundError(f"Archive root unavailable: {root}")
    ref = parse_repository_url(record["source_url"], case_sensitive=True)
    paths = Repository.paths_for(root, ref)
    with Repository.open(root, ref) as repo:
        operation.guard()
        if repo is None and paths.repository_dir.exists() and any(paths.repository_dir.iterdir()):
            index.update(record["id"], local_state="incomplete", reconciliation_required=True)
            raise ValueError("Existing storage is unowned; explicit verified adoption is required")
        publish_local(index, record, repo)
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
            if kind == "check":
                check_locked(index, record)
            elif kind == "convert":
                result = convert_locked(index, record)
            else:
                if not Path(record["archive_root"]).is_dir():
                    raise FileNotFoundError(f"Archive root unavailable: {record['archive_root']}")
                result = fetch_locked(
                    index,
                    record,
                    url=source_url if source_url is not None else record["source_url"],
                    adopt=adopt,
                )
    except operation.OperationInterrupted, KeyboardInterrupt:
        queue.interrupt(job)
        raise
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, sqlite3.Error) as exc:
        category = category_for(exc)
        message = error_text(exc, record["source_url"])
        if not isinstance(exc, operation.ClaimLostError):
            # Queue finalization validates the claim after the operation context exits.
            queue.finish(job, category=category, error=message)
        raise
    else:
        queue.finish(job)
    return result


def convert_locked(index: Index, record: dict[str, Any]) -> Path:
    root = Path(record["archive_root"])
    ref = parse_repository_url(record["source_url"], case_sensitive=True)
    with Repository.open(root, ref) as repo:
        if repo is None:
            raise ValueError("Conversion requires an existing Cache22-managed repository")
        repo.require_selected_format(record["storage_format"])
        index.update(record["id"], reconciliation_required=True)
        result = repo.convert_to_bundle(
            record["source_path"], publish=lambda: publish_local(index, record, repo)
        )
        return result
