from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

from .archive_layout import (
    CLONE_COMPLETE_MARKER_NAME,
    TEMP_IMPORT_DIR_NAME,
    archive_paths_for_repository,
    looks_like_repository_dir,
)
from .config import list_archive_dirs, normalize_archive_dir
from .repository_ref import parse_repository_url


def clean_repository_import_state(
    url: str,
    archive_dirs: Sequence[Path] | None = None,
) -> tuple[Path, ...]:
    repository = parse_repository_url(url)
    removed_paths: list[Path] = []

    for archive_dir in _resolve_archive_dirs(archive_dirs):
        paths = archive_paths_for_repository(archive_dir, repository)
        removed_paths.extend(_clean_repository_directory(paths.repository_dir))

    return tuple(sorted(removed_paths, key=str))


def clean_all_import_state(archive_dirs: Sequence[Path] | None = None) -> tuple[Path, ...]:
    removed_paths: list[Path] = []

    for archive_dir in _resolve_archive_dirs(archive_dirs):
        removed_paths.extend(_clean_partial_state_under(archive_dir))

    return tuple(sorted(removed_paths, key=str))


def _resolve_archive_dirs(archive_dirs: Sequence[Path] | None) -> tuple[Path, ...]:
    if archive_dirs is not None:
        return tuple(normalize_archive_dir(archive_dir) for archive_dir in archive_dirs)

    configured_archive_dirs = tuple(list_archive_dirs())
    if not configured_archive_dirs:
        raise ValueError(
            "No archive directories configured. Add one with 'cache22 config archive add PATH'"
        )

    return configured_archive_dirs


def _clean_partial_state_under(root: Path) -> list[Path]:
    if not root.exists():
        return []

    removed_paths: list[Path] = []
    directories_to_visit = [root]

    while directories_to_visit:
        current_dir = directories_to_visit.pop()
        if not current_dir.is_dir():
            continue

        if looks_like_repository_dir(current_dir):
            removed_paths.extend(_clean_repository_directory(current_dir))
            continue

        child_directories = sorted(
            (child for child in current_dir.iterdir() if child.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        )
        directories_to_visit.extend(child_directories)

    return removed_paths


def _clean_repository_directory(repository_dir: Path) -> list[Path]:
    removed_paths: list[Path] = []
    temp_dir = repository_dir / TEMP_IMPORT_DIR_NAME
    clone_complete_marker = repository_dir / CLONE_COMPLETE_MARKER_NAME
    mirror_repository = repository_dir / f"{repository_dir.name}.git"

    if temp_dir.exists():
        _remove_path(temp_dir)
        removed_paths.append(temp_dir)

    if mirror_repository.exists() and not clone_complete_marker.exists():
        _remove_path(mirror_repository)
        removed_paths.append(mirror_repository)

    if clone_complete_marker.exists() and not mirror_repository.exists():
        _remove_path(clone_complete_marker)
        removed_paths.append(clone_complete_marker)

    return removed_paths


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return

    path.unlink()
