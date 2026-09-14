from __future__ import annotations

from pathlib import Path

from cache22.archive_layout import archive_paths_for_repository
from cache22.import_state import clean_all_import_state, clean_repository_import_state
from cache22.repository_ref import parse_repository_url


def test_clean_repository_import_state_removes_fossil_stage_and_incomplete_clone(
    tmp_path: Path,
) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    url = "https://gitlab.com/group/subgroup/cache22.git"
    repository = parse_repository_url(url)
    paths = archive_paths_for_repository(archive_dir, repository)
    paths.temp_dir.mkdir(parents=True)
    (paths.temp_dir / "partial").write_text("partial")
    paths.mirror_repository.mkdir(parents=True)

    removed_paths = clean_repository_import_state(url, archive_dirs=(archive_dir,))

    assert removed_paths == (paths.temp_dir, paths.mirror_repository)
    assert not paths.temp_dir.exists()
    assert not paths.mirror_repository.exists()


def test_clean_repository_import_state_removes_stray_clone_marker(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    url = "https://gitlab.com/group/subgroup/cache22.git"
    repository = parse_repository_url(url)
    paths = archive_paths_for_repository(archive_dir, repository)
    paths.storage_dir.mkdir(parents=True)
    paths.clone_complete_marker.write_text("complete\n")

    removed_paths = clean_repository_import_state(url, archive_dirs=(archive_dir,))

    assert removed_paths == (paths.clone_complete_marker,)
    assert not paths.clone_complete_marker.exists()


def test_clean_all_import_state_removes_partial_state_in_all_archive_dirs(tmp_path: Path) -> None:
    first_archive_dir = tmp_path / "archive-a"
    second_archive_dir = tmp_path / "archive-b"
    first_archive_dir.mkdir()
    second_archive_dir.mkdir()
    first_repository = parse_repository_url("https://github.com/ar-jan/cache22.git")
    first_paths = archive_paths_for_repository(first_archive_dir, first_repository)
    first_paths.temp_dir.mkdir(parents=True)
    second_repository = parse_repository_url("https://gitlab.com/group/subgroup/cache22.git")
    second_paths = archive_paths_for_repository(second_archive_dir, second_repository)
    second_paths.mirror_repository.mkdir(parents=True)

    removed_paths = clean_all_import_state(
        archive_dirs=(first_archive_dir, second_archive_dir),
    )

    assert removed_paths == (first_paths.temp_dir, second_paths.mirror_repository)
    assert not first_paths.temp_dir.exists()
    assert not second_paths.mirror_repository.exists()
