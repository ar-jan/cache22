from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from cache22.archive_layout import archive_paths_for_repository
from cache22.import_service import import_repository
from cache22.index import Index
from cache22.repository_ref import parse_repository_url

pytestmark = pytest.mark.usefixtures("mock_inventory_git")


def test_import_repository_clones_git_mirror(inventory_index: Index, tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    repository = parse_repository_url("https://gitlab.com/Group/Subgroup/Cache22.git")
    paths = archive_paths_for_repository(archive_dir, repository)
    clone_calls: list[list[str]] = []

    def fake_run(
        args: list[str], *, check: bool, observe_progress: bool = False
    ) -> subprocess.CompletedProcess[str]:
        assert check is True
        clone_calls.append(args)
        Path(args[-1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args=args, returncode=0)

    with (
        patch("cache22.storage.find_git_executable", return_value=Path("/usr/bin/git")),
        patch("cache22.git_mirror.run_git", side_effect=fake_run),
    ):
        result = import_repository(
            "https://gitlab.com/Group/Subgroup/Cache22.git",
            archive_dir=archive_dir,
            index=inventory_index,
        )

    assert result.archive_path == paths.mirror_repository
    assert result.info_messages == ()
    assert clone_calls == [
        [
            "/usr/bin/git",
            "clone",
            "--mirror",
            "--progress",
            "--",
            "https://gitlab.com/group/subgroup/cache22.git",
            str(paths.mirror_repository),
        ]
    ]
    assert paths.mirror_repository.exists()
    assert paths.clone_complete_marker.exists()


def test_import_repository_updates_existing_git_archive_with_info(
    inventory_index: Index, tmp_path: Path
) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    url = "https://github.com/ar-jan/cache22.git"
    repository = parse_repository_url(url)
    paths = archive_paths_for_repository(archive_dir, repository)
    paths.mirror_repository.mkdir(parents=True)
    paths.lock_file.write_text("cache22-storage-v1\n")
    paths.source_file.write_text(json.dumps({"source_path": repository.source_path}))
    paths.clone_complete_marker.write_text("complete\n")

    with (
        patch("cache22.storage.find_git_executable", return_value=Path("git")),
        patch("cache22.git_mirror._fetch_git_mirror") as fetch,
    ):
        result = import_repository(url, archive_dir=archive_dir, index=inventory_index)

    assert result.archive_path == paths.mirror_repository
    assert result.info_messages == (f"INFO: updated Git mirror: {paths.mirror_repository}",)
    fetch.assert_called_once_with(git_executable=Path("git"), url=url, paths=paths)


def test_import_repository_rejects_incomplete_final_clone(
    inventory_index: Index, tmp_path: Path
) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    url = "https://gitlab.com/group/subgroup/cache22.git"
    repository = parse_repository_url(url)
    paths = archive_paths_for_repository(archive_dir, repository)
    paths.mirror_repository.mkdir(parents=True)
    paths.lock_file.write_text("cache22-storage-v1\n")
    paths.source_file.write_text(json.dumps({"source_path": repository.source_path}))

    with (
        patch("cache22.storage.find_git_executable", return_value=Path("/usr/bin/git")),
        pytest.raises(ValueError, match="Expected a bare Git mirror"),
    ):
        import_repository(url, archive_dir=archive_dir, index=inventory_index)


def test_failed_clone_releases_lock_and_removes_only_incomplete_output(
    inventory_index: Index, tmp_path: Path
) -> None:
    url = "https://host/team/project"
    paths = archive_paths_for_repository(tmp_path, parse_repository_url(url))
    calls = 0

    def clone(
        args: list[str], *, check: bool, observe_progress: bool = False
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        Path(args[-1]).mkdir()
        if calls == 1:
            raise subprocess.CalledProcessError(128, args)
        return subprocess.CompletedProcess(args, 0)

    with (
        patch("cache22.storage.find_git_executable", return_value=Path("/usr/bin/git")),
        patch("cache22.git_mirror.run_git", side_effect=clone),
    ):
        with pytest.raises(RuntimeError, match="git clone --mirror failed"):
            import_repository(url, tmp_path, index=inventory_index)
        assert not paths.mirror_repository.exists()
        assert not paths.clone_complete_marker.exists()
        assert paths.lock_file.is_file()
        result = import_repository(url, tmp_path, index=inventory_index)

    assert result.archive_path == paths.mirror_repository
    assert paths.clone_complete_marker.is_file()
