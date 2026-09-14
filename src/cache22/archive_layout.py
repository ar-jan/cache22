from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .repository_ref import RepositoryRef, validate_storage_component

TEMP_IMPORT_DIR_NAME = ".cache22-import"
CLONE_COMPLETE_MARKER_NAME = ".clone-complete"
GIT_MARKS_FILE_NAME = "git.marks"
FOSSIL_MARKS_FILE_NAME = "fossil.marks"
LOCK_FILE_NAME = ".lock"


@dataclass(frozen=True, slots=True)
class ArchivePaths:
    repository_dir: Path
    source_file: Path
    lock_file: Path
    mirror_repository: Path
    fossil_repository: Path
    git_marks: Path
    fossil_marks: Path
    temp_dir: Path
    temp_fossil_repository: Path
    temp_git_marks: Path
    temp_fossil_marks: Path
    clone_complete_marker: Path


def archive_paths_for_repository(archive_dir: Path, repository: RepositoryRef) -> ArchivePaths:
    for component in (repository.host, *repository.namespace, repository.name):
        validate_storage_component(component)
    repository_dir = archive_dir.joinpath(repository.host, *repository.namespace, repository.name)
    return archive_paths_for_directory(repository_dir)


def archive_paths_for_directory(repository_dir: Path) -> ArchivePaths:
    temp_dir = repository_dir / TEMP_IMPORT_DIR_NAME
    name = repository_dir.name

    return ArchivePaths(
        repository_dir=repository_dir,
        source_file=repository_dir / "source.json",
        lock_file=repository_dir / LOCK_FILE_NAME,
        mirror_repository=repository_dir / f"{name}.git",
        fossil_repository=repository_dir / f"{name}.fossil",
        git_marks=repository_dir / GIT_MARKS_FILE_NAME,
        fossil_marks=repository_dir / FOSSIL_MARKS_FILE_NAME,
        temp_dir=temp_dir,
        temp_fossil_repository=temp_dir / f"{name}.fossil",
        temp_git_marks=temp_dir / GIT_MARKS_FILE_NAME,
        temp_fossil_marks=temp_dir / FOSSIL_MARKS_FILE_NAME,
        clone_complete_marker=repository_dir / CLONE_COMPLETE_MARKER_NAME,
    )
