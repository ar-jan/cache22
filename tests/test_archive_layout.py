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
    assert paths.lock_file == paths.repository_dir / ".lock"
    assert paths.mirror_repository == paths.repository_dir / "cache22.git"
    assert paths.clone_complete_marker == paths.repository_dir / ".clone-complete"


def test_archive_paths_for_repository_include_gitlab_subgroups(tmp_path: Path) -> None:
    repository = parse_repository_url("https://gitlab.com/group/subgroup/cache22")

    paths = archive_paths_for_repository(tmp_path, repository)

    assert paths.repository_dir == tmp_path / "gitlab.com" / "group" / "subgroup" / "cache22"
    assert paths.mirror_repository == paths.repository_dir / "cache22.git"


def test_archive_paths_for_repository_include_alternative_git_host(
    tmp_path: Path,
) -> None:
    repository = parse_repository_url("https://git.example.org/Team/Subgroup/Cache22")
    lowercase = parse_repository_url("git@git.example.org:team/subgroup/cache22.git")

    paths = archive_paths_for_repository(tmp_path, repository)

    assert paths == archive_paths_for_repository(tmp_path, lowercase)
    assert paths.repository_dir == tmp_path / "git.example.org" / "team" / "subgroup" / "cache22"
    assert paths.mirror_repository == paths.repository_dir / "cache22.git"
