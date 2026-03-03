from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .repository_ref import RepositoryRef

TEMP_IMPORT_DIR_NAME = ".cache22-import"
CLONE_COMPLETE_MARKER_NAME = ".clone-complete"
GIT_MARKS_FILE_NAME = "git.marks"
FOSSIL_MARKS_FILE_NAME = "fossil.marks"


@dataclass(frozen=True, slots=True)
class ArchivePaths:
    repository_dir: Path
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
    repository_dir = archive_dir.joinpath(repository.host, *repository.namespace, repository.name)
    temp_dir = repository_dir / TEMP_IMPORT_DIR_NAME

    return ArchivePaths(
        repository_dir=repository_dir,
        mirror_repository=repository_dir / f"{repository.name}.git",
        fossil_repository=repository_dir / f"{repository.name}.fossil",
        git_marks=repository_dir / GIT_MARKS_FILE_NAME,
        fossil_marks=repository_dir / FOSSIL_MARKS_FILE_NAME,
        temp_dir=temp_dir,
        temp_fossil_repository=temp_dir / f"{repository.name}.fossil",
        temp_git_marks=temp_dir / GIT_MARKS_FILE_NAME,
        temp_fossil_marks=temp_dir / FOSSIL_MARKS_FILE_NAME,
        clone_complete_marker=repository_dir / CLONE_COMPLETE_MARKER_NAME,
    )


def looks_like_repository_dir(path: Path) -> bool:
    mirror_repository = path / f"{path.name}.git"
    fossil_repository = path / f"{path.name}.fossil"

    return (
        (path / TEMP_IMPORT_DIR_NAME).exists()
        or (path / CLONE_COMPLETE_MARKER_NAME).exists()
        or mirror_repository.exists()
        or fossil_repository.exists()
        or (path / GIT_MARKS_FILE_NAME).exists()
        or (path / FOSSIL_MARKS_FILE_NAME).exists()
    )
