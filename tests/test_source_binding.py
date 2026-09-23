from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from cache22.archive_layout import archive_paths_for_repository
from cache22.archive_storage import repository_operation
from cache22.cli import app
from cache22.import_service import import_repository
from cache22.import_state import clean_all_import_state, clean_repository_import_state
from cache22.repo_service import ImportResult
from cache22.repository_ref import parse_repository_url

pytestmark = pytest.mark.usefixtures("mock_inventory_git")

URL = "https://HOST/Team/Repo.GIT"


@pytest.mark.parametrize(
    "url,default,override",
    [
        (URL, "https://host/team/repo.git", "https://host/Team/Repo.GIT"),
        (
            "SSH://User@HOST:0022/Team/Repo",
            "ssh://User@host:0022/team/repo",
            "ssh://User@host:0022/Team/Repo",
        ),
        (
            "https://User:Pass@HOST:443/Team/Repo.git",
            "https://User:Pass@host:443/team/repo.git",
            "https://User:Pass@host:443/Team/Repo.git",
        ),
        ("User@HOST:Team/Repo.Git", "User@host:team/repo.git", "User@host:Team/Repo.Git"),
        ("User@HOST:/Team/Repo", "User@host:/team/repo", "User@host:/Team/Repo"),
    ],
)
def test_fetch_spelling_and_identity(url: str, default: str, override: str) -> None:
    normalized = parse_repository_url(url)
    preserved = parse_repository_url(url, case_sensitive=True)
    assert normalized == preserved
    assert hash(normalized) == hash(preserved)
    assert normalized.clone_url == default
    assert preserved.clone_url == override
    assert normalized.source_path == "host/team/repo"
    assert preserved.source_path == preserved.display_path == "host/Team/Repo"


def clone(
    args: list[str], *, check: bool, observe_progress: bool = False
) -> subprocess.CompletedProcess[str]:
    Path(args[-1]).mkdir()
    return subprocess.CompletedProcess(args, 0)


@pytest.mark.parametrize(
    "scheme,credentials", [("https", ""), ("ssh", "User@"), ("https", "User:Pass@")]
)
@pytest.mark.parametrize("port", ["", ":0022"])
@pytest.mark.parametrize("case_sensitive", [False, True])
def test_ipv6_clone_url_and_archive_reuse(
    tmp_path: Path, scheme: str, credentials: str, port: str, case_sensitive: bool
) -> None:
    url = f"{scheme}://{credentials}[2001:DB8::1]{port}/Team/Repo.git"
    path = "Team/Repo" if case_sensitive else "team/repo"
    expected = f"{scheme}://{credentials}[2001:db8::1]{port}/{path}.git"
    repository = parse_repository_url(url, case_sensitive=case_sensitive)
    assert repository.host == "2001:db8::1"
    assert repository.source_path == f"2001:db8::1/{path}"
    assert repository.clone_url == expected
    with (
        patch("cache22.storage.find_git_executable", return_value=Path("git")),
        patch("cache22.git_mirror.run_git", side_effect=clone) as run,
    ):
        first = import_repository(url, tmp_path, case_sensitive=case_sensitive)
        assert run.call_args.args[0][-2] == expected
    with (
        patch("cache22.storage.find_git_executable", return_value=Path("git")),
        patch("cache22.git_mirror._fetch_git_mirror") as fetch,
    ):
        assert (
            import_repository(url, tmp_path, case_sensitive=case_sensitive).archive_path
            == first.archive_path
        )
        assert fetch.call_args.kwargs["url"] == expected


@pytest.mark.parametrize("first_override", [False, True])
def test_source_conflicts_before_archive_reuse(tmp_path: Path, first_override: bool) -> None:
    with (
        patch("cache22.storage.find_git_executable", return_value=Path("git")),
        patch("cache22.git_mirror.run_git", side_effect=clone) as run,
    ):
        result = import_repository(URL, tmp_path, case_sensitive=first_override)
        assert (
            run.call_args.args[0][-2]
            == parse_repository_url(URL, case_sensitive=first_override).clone_url
        )
    paths = archive_paths_for_repository(tmp_path, parse_repository_url(URL))
    before = paths.source_file.read_bytes()
    with (
        patch("cache22.storage.find_git_executable", side_effect=AssertionError),
        pytest.raises(ValueError, match="stored .*requested"),
    ):
        import_repository(URL, tmp_path, case_sensitive=not first_override)
    path = "Team/Repo" if first_override else "team/repo"
    with (
        patch("cache22.storage.find_git_executable", return_value=Path("git")),
        patch("cache22.git_mirror._fetch_git_mirror") as fetch,
    ):
        reused = import_repository(f"User@host:/{path}", tmp_path, case_sensitive=True)
    fetch.assert_called_once()
    assert reused.archive_path == result.archive_path
    assert paths.source_file.read_bytes() == before


def test_failed_clone_and_interrupted_cleanup_allow_rebinding(tmp_path: Path) -> None:
    paths = archive_paths_for_repository(tmp_path, parse_repository_url(URL))

    def failed(args: list[str], *, check: bool, observe_progress: bool = False) -> None:
        Path(args[-1]).mkdir()
        raise subprocess.CalledProcessError(1, args)

    with (
        patch("cache22.storage.find_git_executable", return_value=Path("git")),
        patch("cache22.git_mirror.run_git", side_effect=failed),
        pytest.raises(RuntimeError),
    ):
        import_repository(URL, tmp_path, case_sensitive=True)
    assert not paths.source_file.exists()
    inode = paths.lock_file.stat().st_ino
    with repository_operation(tmp_path, paths) as storage:
        assert storage is not None
        storage.bind_source("host/Team/Repo")
        paths.mirror_repository.mkdir()
    clean_repository_import_state(URL, (tmp_path,))
    assert not paths.source_file.exists()
    with (
        patch("cache22.storage.find_git_executable", return_value=Path("git")),
        patch("cache22.git_mirror.run_git", side_effect=clone),
    ):
        import_repository(URL, tmp_path)
    assert json.loads(paths.source_file.read_text()) == {"source_path": "host/team/repo"}
    assert paths.lock_file.stat().st_ino == inode


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ("{}", "Malformed source metadata"),
        ("{", "Expecting property name enclosed in double quotes"),
        ('{"source_path": 3}', "Malformed source metadata"),
        ('{"source_path":"HOST/team/repo"}', "Malformed source metadata"),
    ],
)
def test_malformed_binding_is_never_replaced(tmp_path: Path, metadata: str, message: str) -> None:
    paths = archive_paths_for_repository(tmp_path, parse_repository_url(URL))
    with repository_operation(tmp_path, paths, create=True):
        paths.source_file.write_text(metadata)
    with pytest.raises(ValueError, match=message):
        import_repository(URL, tmp_path)
    assert paths.source_file.read_text() == metadata


def test_unbound_archive_is_rejected_and_metadata_only_can_rebind(tmp_path: Path) -> None:
    paths = archive_paths_for_repository(tmp_path, parse_repository_url(URL))
    with repository_operation(tmp_path, paths, create=True) as storage:
        assert storage is not None
        paths.mirror_repository.mkdir()
        with pytest.raises(ValueError, match="no source binding"):
            storage.bind_source("host/team/repo")
        paths.mirror_repository.rmdir()
        storage.bind_source("host/Team/Repo")
        storage.bind_source("host/team/repo")
    assert json.loads(paths.source_file.read_text())["source_path"] == "host/team/repo"


@pytest.mark.parametrize("marker", [None, "", "unrelated\n", "symlink"])
def test_bulk_cleanup_skips_unowned_and_invalid_names(tmp_path: Path, marker: str | None) -> None:
    paths = archive_paths_for_repository(tmp_path, parse_repository_url(URL))
    paths.mirror_repository.mkdir(parents=True)
    if marker == "symlink":
        paths.lock_file.symlink_to(tmp_path / "missing")
    elif marker is not None:
        paths.lock_file.write_text(marker)
    for name in ["a\\bad", "a\nbad"]:
        (tmp_path / name).mkdir()
    valid = archive_paths_for_repository(tmp_path, parse_repository_url("https://host/team/zvalid"))
    with repository_operation(tmp_path, valid, create=True):
        valid.mirror_repository.mkdir()
    before = sorted(p.name for p in paths.repository_dir.iterdir())
    assert clean_all_import_state((tmp_path,)) == (valid.mirror_repository,)
    assert sorted(p.name for p in paths.repository_dir.iterdir()) == before
    assert paths.mirror_repository.is_dir()


def test_cli_passes_case_sensitive_option(tmp_path: Path) -> None:
    with patch("cache22.repo_cli.import_repository", return_value=ImportResult(tmp_path)) as call:
        CliRunner().invoke(app, ["fetch", URL, "--case-sensitive"])
    assert call.call_args.args == (URL,)
    assert call.call_args.kwargs["case_sensitive"] is True
    assert call.call_args.kwargs["adopt"] is False


def test_import_does_not_claim_nonempty_unowned_storage(tmp_path: Path) -> None:
    paths = archive_paths_for_repository(tmp_path, parse_repository_url(URL))
    unrelated = paths.repository_dir / "unrelated"
    unrelated.mkdir(parents=True)
    with pytest.raises(ValueError, match="nonempty"):
        import_repository(URL, tmp_path)
    assert list(paths.repository_dir.iterdir()) == [unrelated]


def test_repository_name_ending_in_git_can_be_reused(tmp_path: Path) -> None:
    url = "https://host/Team/Repo.git.git"
    with (
        patch("cache22.storage.find_git_executable", return_value=Path("git")),
        patch("cache22.git_mirror.run_git", side_effect=clone),
    ):
        first = import_repository(url, tmp_path, case_sensitive=True)
    with patch("cache22.git_mirror._fetch_git_mirror"):
        assert (
            import_repository(url, tmp_path, case_sensitive=True).archive_path == first.archive_path
        )
