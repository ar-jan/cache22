from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path

from .archive_layout import archive_paths_for_directory, archive_paths_for_repository
from .archive_storage import (
    RepositoryStorage,
    has_repository_boundary,
    open_archive_directory,
    repository_operation,
)
from .config import list_archive_dirs, normalize_archive_dir
from .index import Index
from .repo_audit import register_storage
from .repo_service import publish_local
from .repository_ref import parse_repository_url, validate_storage_component


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
        paths = None
        marker_is_symlink = False
        with open_archive_directory(root, relative) as directory_fd:
            if directory_fd is None:
                continue
            with os.scandir(directory_fd) as entries:
                children = sorted(
                    (entry.name for entry in entries if entry.is_dir(follow_symlinks=False)),
                    reverse=True,
                )

            if len(relative.parts) >= 3 and has_repository_boundary(directory_fd):
                paths = archive_paths_for_directory(root / relative)
                marker = paths.lock_file
                try:
                    marker_is_symlink = stat.S_ISLNK(marker.lstat().st_mode)
                except FileNotFoundError:
                    marker_is_symlink = False
        # Release shared discovery reservations before requesting an exclusive
        # repository reservation. repository_operation rechecks the marker.
        if paths is not None:
            with (
                repository_operation(root, paths)
                if not marker_is_symlink
                else nullcontext(None) as storage
            ):
                if storage is not None:
                    removed_paths.extend(_clean_repository_storage(storage))
            continue
        directories_to_visit.extend(relative / name for name in children if _valid_component(name))

    return removed_paths


def _clean_repository_storage(storage: RepositoryStorage) -> list[Path]:
    paths = storage.paths
    index = Index()
    with index.connect() as db:
        row = db.execute(
            "SELECT id FROM inventory WHERE repository_dir=?", (str(paths.repository_dir),)
        ).fetchone()
    record = index.get(row["id"]) if row else None
    if (
        record is not None
        and record["storage_format"] == "bundle"
        and storage.entry(paths.bundle_manifest.name) is None
    ):
        raise ValueError("Selected bundle manifest is missing; preserving all archive data")
    if record is None and (
        storage.entry(paths.clone_complete_marker.name) is not None
        or storage.entry(paths.bundle_manifest.name) is not None
    ):
        source = storage.read_source()
        if source is not None:
            components = source.split("/")
            root = paths.repository_dir.parents[len(components) - 1]
            try:
                record = register_storage(index, root, storage)
            except ValueError:
                pass
    if record is not None:
        index.update(record["id"], reconciliation_required=True)
    removed_paths: list[Path] = []
    from .git_bundle import cleanup, generation_names

    bundled = storage.entry(paths.bundle_manifest.name) is not None
    if bundled or generation_names(storage) or storage.entry(paths.bundle_staging.name) is not None:
        source = storage.read_source()
        if source is None:
            raise ValueError("Bundle storage has no source binding; preserving it")
        removed_paths.extend(cleanup(storage, source))
    if storage.remove(paths.temp_dir.name):
        removed_paths.append(paths.temp_dir)

    mirror_exists = storage.entry(paths.mirror_repository.name) is not None
    marker_exists = storage.entry(paths.clone_complete_marker.name) is not None
    if mirror_exists and not marker_exists:
        storage.remove(paths.mirror_repository.name)
        removed_paths.append(paths.mirror_repository)
    if marker_exists and not mirror_exists and not bundled:
        storage.remove(paths.clone_complete_marker.name)
        removed_paths.append(paths.clone_complete_marker)

    removed_paths.extend(storage.release_unused_source())
    if record is not None:
        publish_local(index, record, storage)
    return removed_paths


def _valid_component(name: str) -> bool:
    try:
        validate_storage_component(name)
    except ValueError:
        return False
    return True
