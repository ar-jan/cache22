from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .repository_ref import RepositoryRef, validate_storage_component

CLONE_COMPLETE_MARKER_NAME = ".clone-complete"
LOCK_FILE_NAME = ".lock"


@dataclass(frozen=True, slots=True)
class ArchivePaths:
    repository_dir: Path
    source_file: Path
    lock_file: Path
    mirror_repository: Path
    clone_complete_marker: Path
    bundle_manifest: Path
    bundle_staging: Path


def archive_paths_for_repository(archive_dir: Path, repository: RepositoryRef) -> ArchivePaths:
    for component in (repository.host, *repository.namespace, repository.name):
        validate_storage_component(component)
    repository_dir = archive_dir.joinpath(repository.host, *repository.namespace, repository.name)
    return archive_paths_for_directory(repository_dir)


def archive_paths_for_directory(repository_dir: Path) -> ArchivePaths:
    name = repository_dir.name

    return ArchivePaths(
        repository_dir=repository_dir,
        source_file=repository_dir / "source.json",
        lock_file=repository_dir / LOCK_FILE_NAME,
        mirror_repository=repository_dir / f"{name}.git",
        clone_complete_marker=repository_dir / CLONE_COMPLETE_MARKER_NAME,
        bundle_manifest=repository_dir / "bundle.json",
        bundle_staging=repository_dir / ".cache22-bundle",
    )
