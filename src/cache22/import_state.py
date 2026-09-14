from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path

from .archive_layout import archive_paths_for_directory, archive_paths_for_repository
from .archive_storage import RepositoryStorage, open_archive_directory, repository_operation
from .config import list_archive_dirs, normalize_archive_dir
from .repository_ref import STORAGE_DIR_NAME, parse_repository_url, validate_storage_component


def clean_repository_import_state(
    url: str,
    archive_dirs: Sequence[Path] | None = None,
) -> tuple[Path, ...]:
    repository = parse_repository_url(url)
    removed_paths: list[Path] = []

    for archive_dir in _resolve_archive_dirs(archive_dirs):
        paths = archive_paths_for_repository(archive_dir, repository)
        with repository_operation(archive_dir, paths) as storage:
            if storage is not None:
                removed_paths.extend(_clean_repository_storage(storage))

    return tuple(sorted(removed_paths, key=str))


def clean_all_import_state(archive_dirs: Sequence[Path] | None = None) -> tuple[Path, ...]:
    removed_paths: list[Path] = []

    for archive_dir in _resolve_archive_dirs(archive_dirs):
        removed_paths.extend(_clean_partial_state_under(archive_dir))

    return tuple(sorted(removed_paths, key=str))


def _resolve_archive_dirs(archive_dirs: Sequence[Path] | None) -> tuple[Path, ...]:
    if archive_dirs is None:
        archive_dirs = list_archive_dirs()
        if not archive_dirs:
            raise ValueError(
                "No archive directories configured. Add one with 'cache22 config archive add PATH'"
            )
    return tuple(normalize_archive_dir(archive_dir) for archive_dir in archive_dirs)


def _clean_partial_state_under(root: Path) -> list[Path]:
    removed_paths: list[Path] = []
    directories_to_visit = [Path()]

    while directories_to_visit:
        relative = directories_to_visit.pop()
        with open_archive_directory(root, relative) as directory_fd:
            if directory_fd is None:
                continue
            with os.scandir(directory_fd) as entries:
                children = sorted(
                    (entry.name for entry in entries if entry.is_dir(follow_symlinks=False)),
                    reverse=True,
                )

            if STORAGE_DIR_NAME in children and len(relative.parts) >= 3:
                paths = archive_paths_for_directory(root / relative)
                marker = paths.lock_file
                try:
                    marker_is_symlink = stat.S_ISLNK(marker.lstat().st_mode)
                except FileNotFoundError:
                    marker_is_symlink = False
                with (
                    repository_operation(root, paths)
                    if not marker_is_symlink
                    else nullcontext(None) as storage
                ):
                    if storage is not None:
                        removed_paths.extend(_clean_repository_storage(storage))

            directories_to_visit.extend(
                relative / name
                for name in children
                if name != STORAGE_DIR_NAME and _valid_component(name)
            )

    return removed_paths


def _clean_repository_storage(storage: RepositoryStorage) -> list[Path]:
    paths = storage.paths
    removed_paths: list[Path] = []
    if storage.remove(paths.temp_dir.name):
        removed_paths.append(paths.temp_dir)

    mirror_exists = storage.entry(paths.mirror_repository.name) is not None
    marker_exists = storage.entry(paths.clone_complete_marker.name) is not None
    if mirror_exists and not marker_exists:
        storage.remove(paths.mirror_repository.name)
        removed_paths.append(paths.mirror_repository)
    if marker_exists and not mirror_exists:
        storage.remove(paths.clone_complete_marker.name)
        removed_paths.append(paths.clone_complete_marker)

    removed_paths.extend(storage.release_unused_source())
    return removed_paths


def _valid_component(name: str) -> bool:
    try:
        validate_storage_component(name)
    except ValueError:
        return False
    return True
