from __future__ import annotations

import multiprocessing
import subprocess
from multiprocessing.synchronize import Event
from pathlib import Path
from unittest.mock import patch

import pytest

from cache22.archive_layout import archive_paths_for_repository
from cache22.archive_storage import RepositoryBusyError, repository_operation
from cache22.import_service import import_repository
from cache22.import_state import clean_all_import_state, clean_repository_import_state
from cache22.repository_ref import parse_repository_url

URL = "https://host/team/project"


def _paused_import(root: Path, entered: Event, release: Event) -> None:
    def clone(args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        destination = Path(args[-1])
        destination.mkdir()
        (destination / "HEAD").write_text("winner")
        entered.set()
        if not release.wait(15):
            raise RuntimeError("Timed out waiting for test to release import")
        return subprocess.CompletedProcess(args, 0)

    with (
        patch("cache22.import_service.find_git_executable", return_value=Path("/usr/bin/git")),
        patch("cache22.git_mirror.subprocess.run", side_effect=clone),
    ):
        import_repository(URL, root, "git")


def _hold_lock(root: Path, entered: Event, release: Event) -> None:
    paths = archive_paths_for_repository(root, parse_repository_url(URL))
    with repository_operation(root, paths, create=True):
        entered.set()
        release.wait(15)


def test_competing_import_and_cleanup_preserve_winning_clone(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    entered, release = context.Event(), context.Event()
    process = context.Process(target=_paused_import, args=(tmp_path, entered, release))
    process.start()
    paths = archive_paths_for_repository(tmp_path, parse_repository_url(URL))
    try:
        assert entered.wait(10)
        with (
            patch("cache22.import_service.find_git_executable", side_effect=AssertionError),
            pytest.raises(RepositoryBusyError, match="Repository is busy"),
        ):
            import_repository(URL, tmp_path, "git")
        with pytest.raises(RepositoryBusyError, match="Repository is busy"):
            clean_repository_import_state(URL, (tmp_path,))
        with pytest.raises(RepositoryBusyError, match="Repository is busy"):
            clean_all_import_state((tmp_path,))
        assert (paths.mirror_repository / "HEAD").read_text() == "winner"
        assert not paths.clone_complete_marker.exists()
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)

    assert process.exitcode == 0
    assert paths.clone_complete_marker.is_file()
    lock_inode = paths.lock_file.stat().st_ino
    assert import_repository(URL, tmp_path, "git").archive_path == paths.mirror_repository
    assert clean_repository_import_state(URL, (tmp_path,)) == ()
    assert paths.lock_file.stat().st_ino == lock_inode
    assert (paths.mirror_repository / "HEAD").read_text() == "winner"


def test_process_exit_releases_lock_without_deleting_lock_file(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    entered, release = context.Event(), context.Event()
    process = context.Process(target=_hold_lock, args=(tmp_path, entered, release))
    process.start()
    try:
        assert entered.wait(10)
        paths = archive_paths_for_repository(tmp_path, parse_repository_url(URL))
        inode = paths.lock_file.stat().st_ino
        other = archive_paths_for_repository(tmp_path, parse_repository_url(URL + "-other"))
        with repository_operation(tmp_path, other, create=True) as storage:
            assert storage is not None
    finally:
        process.terminate()
        process.join(5)

    assert not process.is_alive()
    assert clean_repository_import_state(URL, (tmp_path,)) == ()
    assert paths.lock_file.stat().st_ino == inode
