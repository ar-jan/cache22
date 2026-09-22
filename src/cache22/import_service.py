from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import operation
from .adoption import prepare_git_import
from .archive_layout import ArchivePaths, archive_paths_for_repository
from .archive_storage import RepositoryStorage, repository_operation
from .config import (
    ArchiveType,
    default_archive_dir,
    default_archive_type,
    normalize_archive_dir,
    normalize_archive_type,
)
from .git_mirror import ensure_git_mirror
from .index import Index, repository_key
from .job_queue import Queue
from .repo_service import execute_job, publish_local, publish_remote
from .repository_ref import parse_repository_url
from .system_tools import find_git_executable


@dataclass(frozen=True, slots=True)
class ImportResult:
    archive_path: Path
    info_messages: tuple[str, ...] = ()
    repository: dict[str, Any] | None = None


def import_repository(
    url: str,
    archive_dir: Path | None = None,
    archive_type: ArchiveType | None = None,
    *,
    case_sensitive: bool = False,
    adopt: bool = False,
    index: Index | None = None,
    timeout: float = 7200,
) -> ImportResult:
    if timeout <= 0:
        raise ValueError("Operation timeout must be positive")
    repository = parse_repository_url(url, case_sensitive=case_sensitive)
    index = index or Index()
    with index.connect() as db:
        existing = db.execute(
            "SELECT archive_root FROM repositories WHERE repo_key=?", (repository_key(repository),)
        ).fetchone()
    root = (
        Path(existing["archive_root"])
        if archive_dir is None and existing
        else _resolve_archive_dir(archive_dir)
    )
    kind = _resolve_archive_type(archive_type)
    record = index.add(repository, root, importing=True)
    job = Queue(index).immediate(record["id"], "fetch")
    result = execute_job(
        index,
        job,
        fetch_timeout=timeout,
        adopt=adopt,
        archive_type=kind,
        source_url=repository.clone_url,
    )

    return replace(result, repository=index.get(record["id"]))


def _import_repository(
    url: str,
    archive_dir: Path | None = None,
    archive_type: ArchiveType | None = None,
    *,
    case_sensitive: bool = False,
    adopt: bool = False,
    index: Index,
    record: dict[str, Any],
) -> ImportResult:
    repository = parse_repository_url(url, case_sensitive=case_sensitive)
    resolved_archive_dir = _resolve_archive_dir(archive_dir)
    _resolve_archive_type(archive_type)
    paths = archive_paths_for_repository(resolved_archive_dir, repository)
    index.update(record["id"], reconciliation_required=True)
    adopted = False

    def prepare(storage: RepositoryStorage) -> None:
        nonlocal adopted
        if (
            record["storage_format"] == "bundle"
            and storage.entry(paths.bundle_manifest.name) is None
        ):
            raise ValueError("Selected bundle manifest is missing; refusing to create a mirror")
        adopted = prepare_git_import(
            storage,
            archive_dir=resolved_archive_dir,
            source_path=repository.source_path,
            adopt=adopt,
        )

    with repository_operation(
        resolved_archive_dir,
        paths,
        create=record["storage_format"] != "bundle",
        prepare=prepare,
    ) as storage:
        if storage is None:
            raise ValueError("Selected bundle storage is missing; restore it before fetching")
        operation.guard()
        bundled = storage.entry(paths.bundle_manifest.name) is not None
        if record["storage_format"] == "bundle" and not bundled:
            raise ValueError("Selected bundle manifest is missing; refusing to create a mirror")
        if bundled and adopt:
            raise ValueError("Bundle adoption is not supported")
        if not bundled:
            storage.validate_clone_marker()
        storage.bind_source(repository.source_path)
        index.update(
            record["id"], source_path=repository.source_path, source_url=repository.clone_url
        )
        record = index.get(record["id"])
        try:
            if bundled:
                from .git_bundle import materialize

                result = ImportResult(
                    materialize(
                        storage,
                        repository.source_path,
                        update_url=repository.clone_url,
                        publish=lambda: publish_local(index, record, storage),
                    )
                )
            else:
                result = _import_locked_repository(repository.clone_url, paths, storage)
            publish_local(index, record, storage)
            if index.get(record["id"])["local_state"] != "ready":
                raise ValueError("Imported mirror could not be validated for the inventory")
            # A remote observation is separate from successful local materialization.
            index.update(
                record["id"],
                last_fetched_at=index.now(),
                fetch_outcome="succeeded",
                fetch_error=None,
                fetch_error_category=None,
                fetch_error_at=None,
            )
            try:
                publish_remote(index, record)
            except operation.TransportError:
                pass
            if adopted:
                return ImportResult(
                    result.archive_path,
                    (f"INFO: adopted Git mirror: {paths.mirror_repository}", *result.info_messages),
                )
            return result
        except OSError, RuntimeError, ValueError, subprocess.SubprocessError:
            # Observe refs even when a fetch updated them but HEAD publication failed.
            try:
                publish_local(index, record, storage)
            except OSError, RuntimeError, ValueError, subprocess.SubprocessError:
                pass
            raise
        finally:
            storage.release_unused_source()


def _import_locked_repository(
    url: str, paths: ArchivePaths, storage: RepositoryStorage
) -> ImportResult:
    message = ensure_git_mirror(git_executable=find_git_executable(), url=url, storage=storage)
    return ImportResult(paths.mirror_repository, (message,) if message is not None else ())


def _resolve_archive_dir(archive_dir: Path | None) -> Path:
    return normalize_archive_dir(default_archive_dir() if archive_dir is None else archive_dir)


def _resolve_archive_type(archive_type: ArchiveType | None) -> ArchiveType:
    if archive_type is not None:
        return normalize_archive_type(archive_type)

    return default_archive_type()
