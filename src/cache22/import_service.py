from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import IO

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
from .repository_ref import parse_repository_url
from .system_tools import find_fossil_executable, find_git_executable


@dataclass(frozen=True, slots=True)
class ImportResult:
    archive_path: Path
    info_messages: tuple[str, ...] = ()


def import_repository(
    url: str,
    archive_dir: Path | None = None,
    archive_type: ArchiveType | None = None,
    *,
    case_sensitive: bool = False,
    adopt: bool = False,
) -> ImportResult:
    repository = parse_repository_url(url, case_sensitive=case_sensitive)
    resolved_archive_dir = _resolve_archive_dir(archive_dir)
    resolved_archive_type = _resolve_archive_type(archive_type)
    if adopt and resolved_archive_type != "git":
        raise ValueError("--adopt is only supported in Git archive mode")
    paths = archive_paths_for_repository(resolved_archive_dir, repository)
    adopted = False

    def prepare(storage: RepositoryStorage) -> None:
        nonlocal adopted
        adopted = prepare_git_import(
            storage,
            archive_dir=resolved_archive_dir,
            source_path=repository.source_path,
            adopt=adopt,
        )

    with repository_operation(
        resolved_archive_dir,
        paths,
        create=True,
        prepare=prepare if resolved_archive_type == "git" else None,
    ) as storage:
        assert storage is not None
        storage.bind_source(repository.source_path)
        try:
            result = _import_locked_repository(
                repository.clone_url, paths, resolved_archive_type, storage
            )
            if adopted:
                return ImportResult(
                    result.archive_path,
                    (f"INFO: adopted Git mirror: {paths.mirror_repository}", *result.info_messages),
                )
            return result
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
