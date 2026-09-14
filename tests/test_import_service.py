from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Self
from unittest.mock import patch

import pytest

from cache22.archive_layout import archive_paths_for_repository
from cache22.import_service import import_repository
from cache22.repository_ref import parse_repository_url


class _FakePipe:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(
        self,
        args: list[str],
        *,
        stdout: int | None = None,
        stdin: _FakePipe | None = None,
        returncode: int = 0,
    ) -> None:
        self.args = args
        self.stdin = stdin
        self.returncode = returncode
        self.stdout = _FakePipe() if stdout == subprocess.PIPE else None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    def wait(self) -> int:
        return self.returncode


def _export_marks_path(args: list[str]) -> Path:
    for arg in args:
        if arg.startswith("--export-marks="):
            return Path(arg.removeprefix("--export-marks="))
    raise AssertionError(f"Missing --export-marks argument: {args}")


def test_import_repository_clones_git_mirror_without_fossil(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    repository = parse_repository_url("https://gitlab.com/Group/Subgroup/Cache22.git")
    paths = archive_paths_for_repository(archive_dir, repository)
    clone_calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check is True
        clone_calls.append(args)
        Path(args[-1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args=args, returncode=0)

    with (
        patch("cache22.import_service.find_git_executable", return_value=Path("/usr/bin/git")),
        patch("cache22.git_mirror.subprocess.run", side_effect=fake_run),
    ):
        result = import_repository(
            "https://gitlab.com/Group/Subgroup/Cache22.git",
            archive_dir=archive_dir,
            archive_type="git",
        )

    assert result.archive_path == paths.mirror_repository
    assert result.info_messages == ()
    assert clone_calls == [
        [
            "/usr/bin/git",
            "clone",
            "--mirror",
            "--",
            "https://gitlab.com/Group/Subgroup/Cache22.git",
            str(paths.mirror_repository),
        ]
    ]
    assert paths.mirror_repository.exists()
    assert paths.clone_complete_marker.exists()
    assert not paths.temp_dir.exists()


def test_import_repository_runs_clone_and_pipeline_for_fossil(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    repository = parse_repository_url("https://gitlab.com/Group/Subgroup/Cache22.git")
    paths = archive_paths_for_repository(archive_dir, repository)
    popen_calls: list[_FakeProcess] = []
    clone_calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check is True
        clone_calls.append(args)
        Path(args[-1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args=args, returncode=0)

    def fake_popen(
        args: list[str],
        *,
        stdout: int | None = None,
        stdin: _FakePipe | None = None,
    ) -> _FakeProcess:
        process = _FakeProcess(args, stdout=stdout, stdin=stdin)
        if args[0] == "/usr/bin/git":
            export_marks = _export_marks_path(args)
            export_marks.parent.mkdir(parents=True, exist_ok=True)
            export_marks.write_text("git marks")
        elif args[0] == "/usr/bin/fossil":
            fossil_marks = Path(args[4])
            fossil_repository = Path(args[5])
            assert fossil_repository.parent == paths.temp_dir
            assert fossil_repository.parent.exists()
            fossil_marks.write_text("fossil marks")
            fossil_repository.write_text("fossil repo")
        popen_calls.append(process)
        return process

    with (
        patch("cache22.import_service.find_git_executable", return_value=Path("/usr/bin/git")),
        patch(
            "cache22.import_service.find_fossil_executable",
            return_value=Path("/usr/bin/fossil"),
        ),
        patch("cache22.git_mirror.subprocess.run", side_effect=fake_run),
        patch("cache22.git_mirror.subprocess.Popen", side_effect=fake_popen),
        patch("cache22.fossil_archive.subprocess.Popen", side_effect=fake_popen),
    ):
        result = import_repository(
            "https://gitlab.com/Group/Subgroup/Cache22.git",
            archive_dir=archive_dir,
            archive_type="fossil",
        )

    assert result.archive_path == paths.fossil_repository
    assert result.info_messages == ()
    assert clone_calls[0][:5] == [
        "/usr/bin/git",
        "clone",
        "--mirror",
        "--",
        "https://gitlab.com/Group/Subgroup/Cache22.git",
    ]
    assert clone_calls[0][5] == str(paths.mirror_repository)
    assert len(popen_calls) == 2
    assert popen_calls[0].args[:4] == [
        "/usr/bin/git",
        "-C",
        str(paths.mirror_repository),
        "fast-export",
    ]
    assert popen_calls[0].args[4:] == [
        "--all",
        "--signed-tags=warn-strip",
        f"--export-marks={paths.temp_git_marks}",
    ]
    assert popen_calls[1].args == [
        "/usr/bin/fossil",
        "import",
        "--git",
        "--export-marks",
        str(paths.temp_fossil_marks),
        str(paths.temp_fossil_repository),
    ]
    assert popen_calls[1].stdin is popen_calls[0].stdout
    assert popen_calls[0].stdout is not None
    assert popen_calls[0].stdout.closed is True
    assert paths.mirror_repository.exists()
    assert paths.clone_complete_marker.exists()
    assert paths.fossil_repository.exists()
    assert paths.git_marks.exists()
    assert paths.fossil_marks.exists()
    assert not paths.temp_dir.exists()


def test_import_repository_reuses_completed_final_git_mirror_for_fossil(
    tmp_path: Path,
) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    url = "https://gitlab.com/group/subgroup/cache22.git"
    repository = parse_repository_url(url)
    paths = archive_paths_for_repository(archive_dir, repository)
    paths.mirror_repository.mkdir(parents=True)
    paths.clone_complete_marker.write_text("complete\n")
    popen_calls: list[_FakeProcess] = []

    def fake_run(args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"clone should not run again: {args}")

    def fake_popen(
        args: list[str],
        *,
        stdout: int | None = None,
        stdin: _FakePipe | None = None,
    ) -> _FakeProcess:
        process = _FakeProcess(args, stdout=stdout, stdin=stdin)
        if args[0] == "/usr/bin/git":
            export_marks = _export_marks_path(args)
            export_marks.parent.mkdir(parents=True, exist_ok=True)
            export_marks.write_text("git marks")
        elif args[0] == "/usr/bin/fossil":
            Path(args[4]).write_text("fossil marks")
            Path(args[5]).write_text("fossil repo")
        popen_calls.append(process)
        return process

    with (
        patch("cache22.import_service.find_git_executable", return_value=Path("/usr/bin/git")),
        patch(
            "cache22.import_service.find_fossil_executable",
            return_value=Path("/usr/bin/fossil"),
        ),
        patch("cache22.git_mirror.subprocess.run", side_effect=fake_run),
        patch("cache22.git_mirror.subprocess.Popen", side_effect=fake_popen),
        patch("cache22.fossil_archive.subprocess.Popen", side_effect=fake_popen),
    ):
        result = import_repository(
            url,
            archive_dir=archive_dir,
            archive_type="fossil",
        )

    assert result.archive_path == paths.fossil_repository
    assert result.info_messages == (f"INFO: archive already exists: {paths.mirror_repository}",)
    assert len(popen_calls) == 2
    assert paths.fossil_repository.exists()
    assert not paths.temp_dir.exists()


def test_import_repository_returns_existing_git_archive_with_info(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    url = "https://github.com/ar-jan/cache22.git"
    repository = parse_repository_url(url)
    paths = archive_paths_for_repository(archive_dir, repository)
    paths.mirror_repository.mkdir(parents=True)
    paths.clone_complete_marker.write_text("complete\n")

    with patch("cache22.import_service.find_git_executable", side_effect=AssertionError):
        result = import_repository(url, archive_dir=archive_dir, archive_type="git")

    assert result.archive_path == paths.mirror_repository
    assert result.info_messages == (f"INFO: archive already exists: {paths.mirror_repository}",)


def test_import_repository_returns_existing_fossil_archive_with_info(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    url = "https://github.com/ar-jan/cache22.git"
    repository = parse_repository_url(url)
    paths = archive_paths_for_repository(archive_dir, repository)
    paths.fossil_repository.parent.mkdir(parents=True)
    paths.fossil_repository.write_text("existing")

    with patch("cache22.import_service.find_git_executable", side_effect=AssertionError):
        result = import_repository(url, archive_dir=archive_dir, archive_type="fossil")

    assert result.archive_path == paths.fossil_repository
    assert result.info_messages == (f"INFO: archive already exists: {paths.fossil_repository}",)


def test_import_repository_keeps_success_when_stage_cleanup_fails(
    tmp_path: Path,
) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    url = "https://gitlab.com/group/subgroup/cache22.git"
    repository = parse_repository_url(url)
    paths = archive_paths_for_repository(archive_dir, repository)

    def fake_run(args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check is True
        Path(args[-1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args=args, returncode=0)

    def fake_popen(
        args: list[str],
        *,
        stdout: int | None = None,
        stdin: _FakePipe | None = None,
    ) -> _FakeProcess:
        process = _FakeProcess(args, stdout=stdout, stdin=stdin)
        if args[0] == "/usr/bin/git":
            export_marks = _export_marks_path(args)
            export_marks.parent.mkdir(parents=True, exist_ok=True)
            export_marks.write_text("git marks")
        elif args[0] == "/usr/bin/fossil":
            Path(args[4]).write_text("fossil marks")
            Path(args[5]).write_text("fossil repo")
        return process

    with (
        patch("cache22.import_service.find_git_executable", return_value=Path("/usr/bin/git")),
        patch(
            "cache22.import_service.find_fossil_executable",
            return_value=Path("/usr/bin/fossil"),
        ),
        patch("cache22.git_mirror.subprocess.run", side_effect=fake_run),
        patch("cache22.git_mirror.subprocess.Popen", side_effect=fake_popen),
        patch("cache22.fossil_archive.subprocess.Popen", side_effect=fake_popen),
        patch(
            "cache22.fossil_archive.clear_staging_dir",
            side_effect=OSError("cleanup failed"),
        ),
    ):
        result = import_repository(
            url,
            archive_dir=archive_dir,
            archive_type="fossil",
        )

    assert result.archive_path == paths.fossil_repository
    assert paths.mirror_repository.exists()
    assert paths.fossil_repository.exists()
    assert paths.git_marks.exists()
    assert paths.fossil_marks.exists()
    assert paths.temp_dir.exists()


def test_import_repository_preserves_fossil_stage_after_pipeline_failure(
    tmp_path: Path,
) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    url = "https://gitlab.com/group/subgroup/cache22.git"
    repository = parse_repository_url(url)
    paths = archive_paths_for_repository(archive_dir, repository)

    def fake_run(args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        Path(args[-1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args=args, returncode=0)

    def fake_popen(
        args: list[str],
        *,
        stdout: int | None = None,
        stdin: _FakePipe | None = None,
    ) -> _FakeProcess:
        if args[0] == "/usr/bin/git":
            export_marks = _export_marks_path(args)
            export_marks.parent.mkdir(parents=True, exist_ok=True)
            export_marks.write_text("git marks")
            return _FakeProcess(args, stdout=stdout, stdin=stdin, returncode=128)

        return _FakeProcess(args, stdout=stdout, stdin=stdin)

    with (
        patch("cache22.import_service.find_git_executable", return_value=Path("/usr/bin/git")),
        patch(
            "cache22.import_service.find_fossil_executable",
            return_value=Path("/usr/bin/fossil"),
        ),
        patch("cache22.git_mirror.subprocess.run", side_effect=fake_run),
        patch("cache22.git_mirror.subprocess.Popen", side_effect=fake_popen),
        patch("cache22.fossil_archive.subprocess.Popen", side_effect=fake_popen),
        pytest.raises(RuntimeError, match="Temporary Fossil import state was kept"),
    ):
        import_repository(
            url,
            archive_dir=archive_dir,
            archive_type="fossil",
        )

    assert paths.temp_dir.exists()
    assert paths.mirror_repository.exists()
    assert paths.clone_complete_marker.exists()
    assert not paths.fossil_repository.exists()


def test_import_repository_rejects_incomplete_final_clone(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    url = "https://gitlab.com/group/subgroup/cache22.git"
    repository = parse_repository_url(url)
    paths = archive_paths_for_repository(archive_dir, repository)
    paths.mirror_repository.mkdir(parents=True)

    with (
        patch("cache22.import_service.find_git_executable", return_value=Path("/usr/bin/git")),
        pytest.raises(RuntimeError, match="cache22 import clean repo"),
    ):
        import_repository(url, archive_dir=archive_dir, archive_type="git")


def test_failed_clone_releases_lock_and_removes_only_incomplete_output(tmp_path: Path) -> None:
    url = "https://host/team/project"
    paths = archive_paths_for_repository(tmp_path, parse_repository_url(url))
    calls = 0

    def clone(args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        Path(args[-1]).mkdir()
        if calls == 1:
            raise subprocess.CalledProcessError(128, args)
        return subprocess.CompletedProcess(args, 0)

    with (
        patch("cache22.import_service.find_git_executable", return_value=Path("/usr/bin/git")),
        patch("cache22.git_mirror.subprocess.run", side_effect=clone),
    ):
        with pytest.raises(RuntimeError, match="git clone --mirror failed"):
            import_repository(url, tmp_path, "git")
        assert not paths.mirror_repository.exists()
        assert not paths.clone_complete_marker.exists()
        assert paths.lock_file.is_file()
        result = import_repository(url, tmp_path, "git")

    assert result.archive_path == paths.mirror_repository
    assert paths.clone_complete_marker.is_file()
