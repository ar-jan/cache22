from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cache22.archive_layout import archive_paths_for_repository
from cache22.archive_storage import repository_operation
from cache22.import_service import import_repository
from cache22.import_state import clean_all_import_state, clean_repository_import_state
from cache22.repository_ref import parse_repository_url


@pytest.mark.parametrize(
    "url",
    [
        "https://../outside/victim",
        "git@/tmp:team/project",
        "git@..:team/project",
        "git@host\\other:team/project",
        "git@host\x00:team/project",
        "https://host/team/back\\slash",
        "https://host/te\nam/project",
        "ssh://git@host/te\tam/project",
        "git@host:team/project\r",
        "https://host/team/project\x7f",
    ],
)
def test_unsafe_repository_urls_fail_before_filesystem_changes(tmp_path: Path, url: str) -> None:
    with pytest.raises(ValueError, match="Unsafe|reserved"):
        import_repository(url, tmp_path, "git")
    with pytest.raises(ValueError, match="Unsafe|reserved"):
        clean_repository_import_state(url, (tmp_path,))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("prefix", ["https://host/", "ssh://git@host/", "git@host:"])
@pytest.mark.parametrize("suffix", ["?", "#", "?query=value", "#fragment"])
def test_query_and_fragment_delimiters_fail_before_storage_changes(
    tmp_path: Path, prefix: str, suffix: str
) -> None:
    url = f"{prefix}team/project{suffix}"
    with pytest.raises(ValueError, match="query or fragment"):
        import_repository(url, tmp_path, "git")
    with pytest.raises(ValueError, match="query or fragment"):
        clean_repository_import_state(url, (tmp_path,))
    assert list(tmp_path.iterdir()) == []


def test_cleanup_finds_subgroups_and_artifact_named_namespaces(
    tmp_path: Path,
) -> None:
    urls = [
        "https://host/team/.cache22-bundle",
        "https://host/other/.cache22-bundle/child",
        "https://host/team/.cache22/project",
        "https://host/team/.lock/project",
        "https://host/team/source.json/project",
        "https://host/team/team.git/project",
    ]
    archives = [archive_paths_for_repository(tmp_path, parse_repository_url(url)) for url in urls]
    for paths in archives:
        paths.mirror_repository.mkdir(parents=True)
        paths.lock_file.write_text("cache22-storage-v1\n")

    removed = clean_all_import_state((tmp_path,))

    assert set(removed) == {paths.mirror_repository for paths in archives}
    for paths in archives:
        assert not paths.mirror_repository.exists()
        assert paths.lock_file.is_file()


@pytest.mark.parametrize(
    "child", ["child", "project.git/child", "source.json/child", ".lock/child"]
)
@pytest.mark.parametrize("child_first", [False, True])
def test_import_rejects_repository_prefix_conflicts(
    tmp_path: Path, child: str, child_first: bool
) -> None:
    parent_url = "https://host/Team/Project"
    child_url = f"https://host/team/project/{child}"
    first, second = (child_url, parent_url) if child_first else (parent_url, child_url)
    paths = archive_paths_for_repository(tmp_path, parse_repository_url(first))
    with repository_operation(tmp_path, paths, create=True):
        paths.mirror_repository.mkdir()
        (paths.mirror_repository / "HEAD").write_text("keep")
        paths.clone_complete_marker.write_text("complete\n")
    before = sorted(tmp_path.rglob("*"))
    with (
        patch("cache22.import_service.find_git_executable", side_effect=AssertionError),
        pytest.raises(ValueError, match="Repository path conflict"),
    ):
        import_repository(second, tmp_path, "git", case_sensitive=True)
    assert sorted(tmp_path.rglob("*")) == before
    assert (paths.mirror_repository / "HEAD").read_text() == "keep"
    assert clean_all_import_state((tmp_path,)) == ()
    if not child_first:
        with pytest.raises(ValueError, match="Repository path conflict"):
            clean_repository_import_state(second, (tmp_path,))


def test_cleanup_never_discovers_repositories_inside_complete_mirrors(tmp_path: Path) -> None:
    paths = archive_paths_for_repository(
        tmp_path, parse_repository_url("https://host/team/project")
    )
    with repository_operation(tmp_path, paths, create=True):
        paths.mirror_repository.mkdir()
        paths.clone_complete_marker.write_text("complete\n")
    nested = paths.mirror_repository / "objects"
    nested.mkdir()
    (nested / ".lock").write_text("cache22-storage-v1\n")
    stage = nested / ".cache22-bundle"
    stage.mkdir()
    (stage / "keep").write_text("keep")
    assert clean_all_import_state((tmp_path,)) == ()
    assert (stage / "keep").read_text() == "keep"


@pytest.mark.parametrize("link_target", ["namespace", "storage", "mirror", "stage", "lock"])
def test_targeted_operations_reject_symlinks_without_changing_external_data(
    tmp_path: Path, link_target: str
) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("keep")
    url = "https://host/team/project"
    paths = archive_paths_for_repository(root, parse_repository_url(url))
    target = {
        "namespace": root / "host",
        "storage": paths.repository_dir,
        "mirror": paths.mirror_repository,
        "stage": paths.bundle_staging,
        "lock": paths.lock_file,
    }[link_target]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(external, target_is_directory=True)
    if link_target in {"mirror", "stage"}:
        paths.lock_file.write_text("cache22-storage-v1\n")

    for operation in (
        lambda: clean_repository_import_state(url, (root,)),
        lambda: import_repository(url, root, "git"),
    ):
        with pytest.raises((OSError, ValueError), match="directory|symbolic|Unsafe"):
            operation()
        assert target.is_symlink()
        assert sentinel.read_text() == "keep"
        assert list(external.iterdir()) == [sentinel]


def test_clean_all_skips_external_and_cyclic_directory_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    outside = tmp_path / "outside"
    paths = archive_paths_for_repository(outside, parse_repository_url("https://host/team/project"))
    paths.mirror_repository.mkdir(parents=True)
    paths.lock_file.write_text("cache22-storage-v1\n")
    (root / "external").symlink_to(outside, target_is_directory=True)
    (root / "cycle").symlink_to(root, target_is_directory=True)

    assert clean_all_import_state((root,)) == ()
    assert paths.mirror_repository.is_dir()


def test_cleanup_unlinks_internal_symlinks_without_following_them(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("keep")
    url = "https://host/team/project"
    paths = archive_paths_for_repository(root, parse_repository_url(url))
    paths.mirror_repository.mkdir(parents=True)
    paths.lock_file.write_text("cache22-storage-v1\n")
    (paths.mirror_repository / "link").symlink_to(external, target_is_directory=True)

    assert clean_repository_import_state(url, (root,)) == (paths.mirror_repository,)
    assert sentinel.read_text() == "keep"


def test_cleanup_of_absent_repository_does_not_create_storage(tmp_path: Path) -> None:
    assert clean_repository_import_state("https://host/team/project", (tmp_path,)) == ()
    assert list(tmp_path.iterdir()) == []


def test_missing_configured_root_fails_before_clone_or_cleanup(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with (
        patch("cache22.config.list_archive_dirs", return_value=[missing]),
        patch("cache22.import_state.list_archive_dirs", return_value=[missing]),
        patch("cache22.import_service.find_git_executable", side_effect=AssertionError),
    ):
        with pytest.raises(ValueError, match="Archive directory does not exist"):
            import_repository("https://host/team/project", archive_type="git")
        with pytest.raises(ValueError, match="Archive directory does not exist"):
            clean_all_import_state()
        with pytest.raises(ValueError, match="Archive directory does not exist"):
            clean_repository_import_state("https://host/team/project")
    assert not missing.exists()
