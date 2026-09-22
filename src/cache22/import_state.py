from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path

from .archive_layout import archive_paths_for_directory
from .archive_storage import (
    has_repository_boundary,
    open_archive_directory,
)
from .config import list_archive_dirs, normalize_archive_dir
from .index import Index
from .repo_audit import register_storage
from .repo_service import publish_local
from .repository_ref import parse_repository_url, validate_storage_component
from .storage import Repository


def clean_repository_import_state(
    url: str,
    archive_dirs: Sequence[Path] | None = None,
) -> tuple[Path, ...]:
    repository = parse_repository_url(url)
    removed_paths: list[Path] = []

    for archive_dir in _resolve_archive_dirs(archive_dirs):
        with Repository.open(archive_dir, repository) as repo:
            if repo is not None:
                removed_paths.extend(_clean_repository_storage(repo))

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
        # repository reservation. Repository.open_directory rechecks the marker.
        if paths is not None:
            with (
                Repository.open_directory(root, paths.repository_dir)
                if not marker_is_symlink
                else nullcontext(None) as repo
            ):
                if repo is not None:
                    removed_paths.extend(_clean_repository_storage(repo))
            continue
        directories_to_visit.extend(relative / name for name in children if _valid_component(name))

    return removed_paths


def _clean_repository_storage(repo: Repository) -> list[Path]:
    paths = repo.paths
    index = Index()
    with index.connect() as db:
        row = db.execute(
            "SELECT id FROM inventory WHERE repository_dir=?", (str(paths.repository_dir),)
        ).fetchone()
    record = index.get(row["id"]) if row else None
    if record is not None:
        repo.require_selected_format(record["storage_format"])
    if record is None and repo.has_completion_metadata and repo.read_source() is not None:
        try:
            record = register_storage(index, repo.root, repo)
        except ValueError:
            pass
    if record is not None:
        index.update(record["id"], reconciliation_required=True)
    removed_paths = repo.clean()
    if record is not None:
        publish_local(index, record, repo)
    return removed_paths


def _valid_component(name: str) -> bool:
    try:
        validate_storage_component(name)
    except ValueError:
        return False
    return True
