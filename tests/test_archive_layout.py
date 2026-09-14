from __future__ import annotations

from pathlib import Path

from cache22.archive_layout import archive_paths_for_repository
from cache22.repository_ref import parse_repository_url


def test_archive_paths_for_repository_are_deterministic_for_github(
    tmp_path: Path,
) -> None:
    repository = parse_repository_url("https://github.com/ar-jan/cache22")

    paths = archive_paths_for_repository(tmp_path, repository)

    assert paths.repository_dir == tmp_path / "github.com" / "ar-jan" / "cache22"
    assert paths.storage_dir == paths.repository_dir / ".cache22"
    assert paths.lock_file == paths.storage_dir / ".lock"
    assert paths.mirror_repository == paths.storage_dir / "cache22.git"
    assert paths.fossil_repository == paths.storage_dir / "cache22.fossil"
    assert paths.git_marks == paths.storage_dir / "git.marks"
    assert paths.fossil_marks == paths.storage_dir / "fossil.marks"
    assert paths.temp_dir == paths.storage_dir / ".cache22-import"
    assert paths.temp_fossil_repository == paths.temp_dir / "cache22.fossil"
    assert paths.temp_git_marks == paths.temp_dir / "git.marks"
    assert paths.temp_fossil_marks == paths.temp_dir / "fossil.marks"
    assert paths.clone_complete_marker == paths.storage_dir / ".clone-complete"


def test_archive_paths_for_repository_include_gitlab_subgroups(tmp_path: Path) -> None:
    repository = parse_repository_url("https://gitlab.com/group/subgroup/cache22")

    paths = archive_paths_for_repository(tmp_path, repository)

    assert paths.repository_dir == tmp_path / "gitlab.com" / "group" / "subgroup" / "cache22"
    assert paths.mirror_repository == paths.storage_dir / "cache22.git"
    assert paths.fossil_repository == paths.storage_dir / "cache22.fossil"
    assert paths.git_marks == paths.storage_dir / "git.marks"
    assert paths.fossil_marks == paths.storage_dir / "fossil.marks"
    assert paths.temp_dir == paths.storage_dir / ".cache22-import"


def test_archive_paths_for_repository_include_alternative_git_host(
    tmp_path: Path,
) -> None:
    repository = parse_repository_url("https://git.example.org/Team/Subgroup/Cache22")
    lowercase = parse_repository_url("git@git.example.org:team/subgroup/cache22.git")

    paths = archive_paths_for_repository(tmp_path, repository)

    assert paths == archive_paths_for_repository(tmp_path, lowercase)
    assert paths.repository_dir == tmp_path / "git.example.org" / "team" / "subgroup" / "cache22"
    assert paths.mirror_repository == paths.storage_dir / "cache22.git"
    assert paths.fossil_repository == paths.storage_dir / "cache22.fossil"
    assert paths.git_marks == paths.storage_dir / "git.marks"
    assert paths.fossil_marks == paths.storage_dir / "fossil.marks"
