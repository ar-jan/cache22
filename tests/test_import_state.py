from __future__ import annotations

from pathlib import Path

import pytest

from cache22.archive_layout import archive_paths_for_repository
from cache22.import_state import clean_all_import_state, clean_repository_import_state
from cache22.index import Index, index_path
from cache22.repository_ref import parse_repository_url


def test_clean_repository_import_state_removes_incomplete_clone(
    inventory_index: Index,
    tmp_path: Path,
) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    url = "https://gitlab.com/group/subgroup/cache22.git"
    repository = parse_repository_url(url)
    paths = archive_paths_for_repository(archive_dir, repository)
    paths.mirror_repository.mkdir(parents=True)
    paths.lock_file.write_text("cache22-storage-v1\n")
    (paths.mirror_repository / "partial").write_text("partial")

    removed_paths = clean_repository_import_state(
        url, archive_dirs=(archive_dir,), index=inventory_index
    )

    assert removed_paths == (paths.mirror_repository,)
    assert not paths.mirror_repository.exists()


@pytest.mark.parametrize("contents", ["complete\n", "unfinished\n"])
def test_clean_repository_import_state_removes_stray_clone_marker(
    inventory_index: Index, tmp_path: Path, contents: str
) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    url = "https://gitlab.com/group/subgroup/cache22.git"
    repository = parse_repository_url(url)
    paths = archive_paths_for_repository(archive_dir, repository)
    paths.repository_dir.mkdir(parents=True)
    paths.lock_file.write_text("cache22-storage-v1\n")
    paths.clone_complete_marker.write_text(contents)

    removed_paths = clean_repository_import_state(
        url, archive_dirs=(archive_dir,), index=inventory_index
    )

    assert removed_paths == (paths.clone_complete_marker,)
    assert not paths.clone_complete_marker.exists()


def test_clean_all_import_state_removes_partial_state_in_all_archive_dirs(
    inventory_index: Index, tmp_path: Path
) -> None:
    first_archive_dir = tmp_path / "archive-a"
    second_archive_dir = tmp_path / "archive-b"
    first_archive_dir.mkdir()
    second_archive_dir.mkdir()
    first_repository = parse_repository_url("https://github.com/ar-jan/cache22.git")
    first_paths = archive_paths_for_repository(first_archive_dir, first_repository)
    first_paths.mirror_repository.mkdir(parents=True)
    first_paths.lock_file.write_text("cache22-storage-v1\n")
    second_repository = parse_repository_url("https://gitlab.com/group/subgroup/cache22.git")
    second_paths = archive_paths_for_repository(second_archive_dir, second_repository)
    second_paths.mirror_repository.mkdir(parents=True)
    second_paths.lock_file.write_text("cache22-storage-v1\n")

    removed_paths = clean_all_import_state(
        archive_dirs=(first_archive_dir, second_archive_dir), index=inventory_index
    )

    assert removed_paths == (first_paths.mirror_repository, second_paths.mirror_repository)
    assert not first_paths.mirror_repository.exists()
    assert not second_paths.mirror_repository.exists()


@pytest.mark.parametrize("all_repositories", [False, True])
def test_cleanup_uses_supplied_index(tmp_path: Path, all_repositories: bool) -> None:
    index = Index.initialize(tmp_path / "custom.db", clock=lambda: 123)
    root = tmp_path / "archive"
    ref = parse_repository_url("https://host/team/project")
    paths = archive_paths_for_repository(root, ref)
    paths.mirror_repository.mkdir(parents=True)
    paths.lock_file.write_text("cache22-storage-v1\n")
    (paths.mirror_repository / "partial").write_text("partial")
    record = index.add(ref, root)
    index.update(record["id"], local_state="ready")

    if all_repositories:
        removed = clean_all_import_state((root,), index=index)
    else:
        removed = clean_repository_import_state(ref.clone_url, (root,), index=index)

    assert removed == (paths.mirror_repository,)
    record = index.get(record["id"])
    assert record["local_state"] == "missing"
    assert record["local_observed_at"] == 123
    assert not record["reconciliation_required"]
    assert not index_path().parent.exists()
