from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Any

from . import operation
from .adoption import prepare_git_import
from .archive_layout import ArchivePaths, archive_paths_for_repository
from .archive_storage import RepositoryStorage, repository_operation
from .config import (
    ArchiveType,
    default_archive_dir,
    default_archive_type,
    normalize_archive_dir,
)
from .fossil_archive import (
    clear_staging_dir_after_success,
    import_failure_message,
    open_fossil_import,
    prepare_staging_dir,
    promote_staged_archive,
)
from .git_mirror import ensure_git_mirror, open_fast_export
from .index import Index, repository_key
from .job_queue import Queue
from .repo_service import execute_job, publish_local, publish_remote
from .repository_ref import parse_repository_url
from .system_tools import find_fossil_executable, find_git_executable


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
    if adopt and kind != "git":
        raise ValueError("--adopt is only supported in Git archive mode")
    record = index.add(repository, root, importing=True)
    job = Queue(index).immediate(record["id"], "fetch")
    result = execute_job(
        index,
        job,
        fetch_timeout=timeout,
        adopt=adopt,
        archive_type=kind,
        archive_type_explicit=archive_type is not None,
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
    archive_type_explicit: bool = True,
) -> ImportResult:
    repository = parse_repository_url(url, case_sensitive=case_sensitive)
    resolved_archive_dir = _resolve_archive_dir(archive_dir)
    resolved_archive_type = _resolve_archive_type(archive_type)
    if adopt and resolved_archive_type != "git":
        raise ValueError("--adopt is only supported in Git archive mode")
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
        prepare=prepare if resolved_archive_type == "git" else None,
    ) as storage:
        if storage is None:
            raise ValueError("Selected bundle storage is missing; restore it before fetching")
        operation.guard()
        bundled = storage.entry(paths.bundle_manifest.name) is not None
        if record["storage_format"] == "bundle" and not bundled:
            raise ValueError("Selected bundle manifest is missing; refusing to create a mirror")
        if bundled:
            if adopt:
                raise ValueError("Bundle adoption is not supported")
            if archive_type_explicit and resolved_archive_type == "fossil":
                raise ValueError("Fossil operations are not supported on bundled repositories")
            resolved_archive_type = "git"
        if not bundled:
            storage.validate_clone_marker()
        storage.bind_source(repository.source_path)
        index.update(
            record["id"], source_path=repository.source_path, source_url=repository.clone_url
        )
        record = index.get(record["id"])
        fetched = resolved_archive_type == "git" or not paths.clone_complete_marker.exists()
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
                result = _import_locked_repository(
                    repository.clone_url, paths, resolved_archive_type, storage
                )
            publish_local(index, record, storage)
            if resolved_archive_type == "git" and index.get(record["id"])["local_state"] != "ready":
                raise ValueError("Imported mirror could not be validated for the inventory")
            if fetched and index.get(record["id"])["local_state"] == "ready":
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
    url: str, paths: ArchivePaths, resolved_archive_type: ArchiveType, storage: RepositoryStorage
) -> ImportResult:
    info_messages: list[str] = []

    if resolved_archive_type == "fossil" and paths.fossil_repository.exists():
        info_messages.append(f"INFO: archive already exists: {paths.fossil_repository}")
        return ImportResult(paths.fossil_repository, tuple(info_messages))

    git_executable = find_git_executable()

    try:
        existing_mirror_message = ensure_git_mirror(
            git_executable=git_executable,
            url=url,
            storage=storage,
            update=resolved_archive_type == "git",
        )
        if existing_mirror_message is not None:
            info_messages.append(existing_mirror_message)

        if resolved_archive_type == "git":
            return ImportResult(paths.mirror_repository, tuple(info_messages))

        fossil_executable = find_fossil_executable()
        prepare_staging_dir(storage)
        _run_fossil_import_pipeline(
            git_executable=git_executable,
            fossil_executable=fossil_executable,
            paths=paths,
        )
        promote_staged_archive(storage)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(_failure_message(url, paths, resolved_archive_type, str(exc))) from exc

    clear_staging_dir_after_success(storage)
    return ImportResult(paths.fossil_repository, tuple(info_messages))


def _resolve_archive_dir(archive_dir: Path | None) -> Path:
    return normalize_archive_dir(default_archive_dir() if archive_dir is None else archive_dir)


def _resolve_archive_type(archive_type: ArchiveType | None) -> ArchiveType:
    if archive_type is not None:
        return archive_type

    return default_archive_type()


def _run_fossil_import_pipeline(
    *,
    git_executable: Path,
    fossil_executable: Path,
    paths: ArchivePaths,
) -> None:
    fossil_returncode: int | None = None
    git_process, fast_export_stream = open_fast_export(
        git_executable=git_executable,
        paths=paths,
    )

    with git_process:
        try:
            with open_fossil_import(
                fossil_executable=fossil_executable,
                paths=paths,
                fast_export_stream=fast_export_stream,
            ) as fossil_process:
                fast_export_stream.close()
                fossil_returncode = fossil_process.wait()
        finally:
            if not _stream_is_closed(fast_export_stream):
                fast_export_stream.close()
            git_returncode = git_process.wait()

    if fossil_returncode != 0:
        raise RuntimeError(f"fossil import --git failed with exit code {fossil_returncode}")
    if git_returncode != 0:
        raise RuntimeError(f"git fast-export --all failed with exit code {git_returncode}")

    missing_marks = [
        path for path in (paths.temp_git_marks, paths.temp_fossil_marks) if not path.exists()
    ]
    if missing_marks:
        try:
            result = subprocess.run(
                [
                    str(git_executable),
                    "-C",
                    str(paths.mirror_repository),
                    "rev-list",
                    "--all",
                    "--count",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError("Could not check whether the Git mirror has commits") from exc
        if result.stdout.strip() == "0":
            for path in missing_marks:
                with path.open("xb"):
                    pass


def _stream_is_closed(stream: IO[bytes]) -> bool:
    return bool(getattr(stream, "closed", False))


def _failure_message(
    url: str,
    paths: ArchivePaths,
    archive_type: ArchiveType,
    message: str,
) -> str:
    if archive_type == "fossil":
        return import_failure_message(url, paths, message)

    return message
