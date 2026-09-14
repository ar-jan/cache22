from __future__ import annotations

import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from cache22.archive_layout import ArchivePaths, archive_paths_for_repository
from cache22.archive_storage import RepositoryStorage
from cache22.fossil_archive import promote_staged_archive
from cache22.import_service import import_repository
from cache22.repository_ref import parse_repository_url

URL = "https://host/team/project"


@pytest.fixture
def local_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[bool], tuple[Path, ArchivePaths]]:
    if shutil.which("git") is None or shutil.which("fossil") is None:
        pytest.skip("Git and Fossil are required for archive integration tests")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("FOSSIL_HOME", str(tmp_path))

    def create(populated: bool) -> tuple[Path, ArchivePaths]:
        source = tmp_path / "source"
        root = tmp_path / "archive"
        root.mkdir()
        subprocess.run(
            ["git", "init", "--initial-branch=main", str(source)], check=True, capture_output=True
        )
        if populated:
            (source / "file.txt").write_text("archive fixture\n")
            subprocess.run(["git", "-C", str(source), "add", "file.txt"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source),
                    "-c",
                    "user.name=Cache22 Test",
                    "-c",
                    "user.email=test@example.org",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-m",
                    "Archive fixture",
                ],
                check=True,
                capture_output=True,
            )
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{source}.insteadOf")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", URL)
        return root, archive_paths_for_repository(root, parse_repository_url(URL))

    return create


@pytest.mark.parametrize("populated", [False, True])
def test_real_git_and_fossil_conversion(
    local_repository: Callable[[bool], tuple[Path, ArchivePaths]], populated: bool
) -> None:
    root, paths = local_repository(populated)

    result = import_repository(URL, root, "fossil")

    assert result.archive_path == paths.fossil_repository
    assert paths.clone_complete_marker.is_file()
    assert paths.git_marks.is_file()
    assert paths.fossil_marks.is_file()
    assert not paths.temp_dir.exists()
    connection = sqlite3.connect(paths.fossil_repository)
    try:
        count = connection.execute("SELECT count(*) FROM event WHERE type = 'ci'").fetchone()[0]
    finally:
        connection.close()
    assert count == int(populated)
    if not populated:
        assert paths.git_marks.read_bytes() == b""
    assert import_repository(URL, root, "fossil").archive_path == result.archive_path


def test_missing_marks_from_nonempty_export_are_not_synthesized(
    local_repository: Callable[[bool], tuple[Path, ArchivePaths]],
) -> None:
    root, paths = local_repository(True)
    real_popen = subprocess.Popen

    def omit_marks(args, **kwargs):
        if "fast-export" in args:
            args = [arg for arg in args if not arg.startswith("--export-marks=")]
        return real_popen(args, **kwargs)

    with (
        patch("cache22.git_mirror.subprocess.Popen", side_effect=omit_marks),
        pytest.raises(RuntimeError, match="Expected staged import output was not created"),
    ):
        import_repository(URL, root, "fossil")
    assert not paths.fossil_repository.exists()
    assert not paths.temp_git_marks.exists()
    assert paths.temp_dir.is_dir()
    assert paths.clone_complete_marker.is_file()


def test_missing_fossil_output_still_fails_for_empty_repository(
    local_repository: Callable[[bool], tuple[Path, ArchivePaths]],
) -> None:
    root, paths = local_repository(False)

    def discard_fossil(storage: RepositoryStorage) -> None:
        paths.temp_fossil_repository.unlink()
        promote_staged_archive(storage)

    with (
        patch("cache22.import_service.promote_staged_archive", side_effect=discard_fossil),
        pytest.raises(RuntimeError, match="Expected staged import output was not created"),
    ):
        import_repository(URL, root, "fossil")
    assert not paths.fossil_repository.exists()
    assert paths.temp_git_marks.is_file()
    assert paths.clone_complete_marker.is_file()
