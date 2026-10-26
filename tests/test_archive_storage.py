from __future__ import annotations

import os
from pathlib import Path

import pytest

from cache22.archive_layout import archive_paths_for_repository
from cache22.archive_storage import repository_operation
from cache22.repository_ref import parse_repository_url


@pytest.mark.parametrize("stale_scan", [False, True])
def test_bundle_discovery_tracks_changes_in_one_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale_scan: bool
) -> None:
    paths = archive_paths_for_repository(
        tmp_path, parse_repository_url("https://example.test/team/project.v1")
    )
    with repository_operation(tmp_path, paths, create=True) as storage:
        assert storage is not None
        if stale_scan:
            # Model Btrfs retaining an earlier listing on the long-lived handle,
            # so this regression also fails on CI's tmpfs/ext4 filesystems.
            listdir = os.listdir
            initial = listdir(storage.directory_fd)

            def cached_listdir(fd: int) -> list[str]:
                return initial.copy() if fd == storage.directory_fd else listdir(fd)

            monkeypatch.setattr(os, "listdir", cached_listdir)

        assert storage.bundle_generations() == []
        first = paths.repository_dir / f"project.v1.{'a' * 32}.bundle"
        second = paths.repository_dir / f"project.v1.{'b' * 32}.bundle"
        first.touch()
        (paths.repository_dir / f"projectXv1.{'c' * 32}.bundle").touch()
        (paths.repository_dir / "project.v1.invalid.bundle").touch()
        assert storage.bundle_generations() == [first.name]

        first.rename(second)
        assert storage.bundle_generations() == [second.name]
        first.touch()
        assert storage.bundle_generations() == [first.name, second.name]
        first.unlink()
        second.unlink()
        assert storage.bundle_generations() == []


def test_bundle_discovery_stays_with_reserved_directory(tmp_path: Path) -> None:
    paths = archive_paths_for_repository(
        tmp_path, parse_repository_url("https://example.test/team/project")
    )
    with repository_operation(tmp_path, paths, create=True) as storage:
        assert storage is not None
        assert storage.bundle_generations() == []
        moved = paths.repository_dir.with_name("moved")
        paths.repository_dir.rename(moved)
        paths.repository_dir.mkdir()
        expected = f"project.{'a' * 32}.bundle"
        (moved / expected).touch()
        (paths.repository_dir / f"project.{'b' * 32}.bundle").touch()
        assert storage.bundle_generations() == [expected]
