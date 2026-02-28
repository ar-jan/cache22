from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from cache22.cli import app
from cache22.import_git import archive_paths_for_repository, clear_git_import_stage, import_git_repository, parse_repository_url


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


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

    def __enter__(self) -> _FakeProcess:
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


def test_parse_repository_url_normalizes_https_and_ssh_for_github() -> None:
    https_repository = parse_repository_url("https://github.com/Ar-Jan/Cache22.git")
    ssh_repository = parse_repository_url("git@github.com:ar-jan/cache22.git")

    assert https_repository == ssh_repository
    assert https_repository.host == "github.com"
    assert https_repository.namespace == ("ar-jan",)
    assert https_repository.name == "cache22"


def test_parse_repository_url_normalizes_https_and_ssh_for_gitlab() -> None:
    https_repository = parse_repository_url("https://gitlab.com/Group/Subgroup/Cache22.git")
    ssh_repository = parse_repository_url("git@gitlab.com:group/subgroup/cache22.git")

    assert https_repository == ssh_repository
    assert https_repository.host == "gitlab.com"
    assert https_repository.namespace == ("group", "subgroup")
    assert https_repository.name == "cache22"


def test_parse_repository_url_supports_ssh_scheme_for_gitlab() -> None:
    repository = parse_repository_url("ssh://git@gitlab.com/Group/Subgroup/Cache22.git")

    assert repository.host == "gitlab.com"
    assert repository.namespace == ("group", "subgroup")
    assert repository.name == "cache22"


def test_parse_repository_url_accepts_alternative_git_host() -> None:
    repository = parse_repository_url("git@git.example.org:Team/Subgroup/Cache22.git")

    assert repository.host == "git.example.org"
    assert repository.namespace == ("Team", "Subgroup")
    assert repository.name == "Cache22"


def test_parse_repository_url_preserves_case_for_alternative_git_host() -> None:
    mixed_case = parse_repository_url("git@git.example.org:Team/Subgroup/Cache22.git")
    lowercase = parse_repository_url("git@git.example.org:team/subgroup/cache22.git")

    assert mixed_case != lowercase


def test_archive_paths_for_repository_are_deterministic_for_github(tmp_path: Path) -> None:
    repository = parse_repository_url("https://github.com/ar-jan/cache22")

    paths = archive_paths_for_repository(tmp_path, repository)

    assert paths.repository_dir == tmp_path / "github.com" / "ar-jan" / "cache22"
    assert paths.fossil_repository == paths.repository_dir / "cache22.fossil"
    assert paths.git_marks == paths.repository_dir / "git.marks"
    assert paths.fossil_marks == paths.repository_dir / "fossil.marks"
    assert paths.stage_root == tmp_path / ".cache22" / "git-import"
    assert paths.stage_dir == paths.stage_root / "github.com" / "ar-jan" / "cache22"
    assert paths.mirror_dir == paths.stage_dir / "repo.git"
    assert paths.staged_fossil_repository == paths.stage_dir / "cache22.fossil"
    assert paths.staged_git_marks == paths.stage_dir / "git.marks"
    assert paths.staged_fossil_marks == paths.stage_dir / "fossil.marks"
    assert paths.clone_complete_marker == paths.stage_dir / ".clone-complete"


def test_archive_paths_for_repository_include_gitlab_subgroups(tmp_path: Path) -> None:
    repository = parse_repository_url("https://gitlab.com/group/subgroup/cache22")

    paths = archive_paths_for_repository(tmp_path, repository)

    assert paths.repository_dir == tmp_path / "gitlab.com" / "group" / "subgroup" / "cache22"
    assert paths.fossil_repository == paths.repository_dir / "cache22.fossil"
    assert paths.git_marks == paths.repository_dir / "git.marks"
    assert paths.fossil_marks == paths.repository_dir / "fossil.marks"
    assert paths.stage_dir == tmp_path / ".cache22" / "git-import" / "gitlab.com" / "group" / "subgroup" / "cache22"


def test_archive_paths_for_repository_include_alternative_git_host(tmp_path: Path) -> None:
    repository = parse_repository_url("https://git.example.org/Team/Subgroup/Cache22")

    paths = archive_paths_for_repository(tmp_path, repository)

    assert paths.repository_dir == tmp_path / "git.example.org" / "Team" / "Subgroup" / "Cache22"
    assert paths.fossil_repository == paths.repository_dir / "Cache22.fossil"
    assert paths.git_marks == paths.repository_dir / "git.marks"
    assert paths.fossil_marks == paths.repository_dir / "fossil.marks"


def test_import_git_repository_runs_clone_and_pipeline(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    repository = parse_repository_url("https://gitlab.com/Group/Subgroup/Cache22.git")
    paths = archive_paths_for_repository(archive_dir, repository)
    popen_calls: list[_FakeProcess] = []
    clone_calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check is True
        clone_calls.append(args)
        Path(args[4]).mkdir(parents=True, exist_ok=True)
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
            fossil_marks.parent.mkdir(parents=True, exist_ok=True)
            fossil_marks.write_text("fossil marks")
            fossil_repository.write_text("fossil repo")
        popen_calls.append(process)
        return process

    with patch("cache22.import_git.find_git_executable", return_value=Path("/usr/bin/git")):
        with patch("cache22.import_git.find_fossil_executable", return_value=Path("/usr/bin/fossil")):
            with patch("cache22.import_git.subprocess.run", side_effect=fake_run):
                with patch("cache22.import_git.subprocess.Popen", side_effect=fake_popen):
                    archive_path = import_git_repository(
                        "https://gitlab.com/Group/Subgroup/Cache22.git",
                        archive_dir=archive_dir,
                    )

    assert archive_path == archive_dir / "gitlab.com" / "group" / "subgroup" / "cache22" / "cache22.fossil"
    assert len(clone_calls) == 1
    assert clone_calls[0][:4] == [
        "/usr/bin/git",
        "clone",
        "--mirror",
        "https://gitlab.com/Group/Subgroup/Cache22.git",
    ]
    assert clone_calls[0][4] == str(paths.mirror_dir)

    assert len(popen_calls) == 2
    assert popen_calls[0].args[:4] == ["/usr/bin/git", "-C", clone_calls[0][4], "fast-export"]
    assert popen_calls[0].args[4:] == [
        "--all",
        "--signed-tags=warn-strip",
        f"--export-marks={paths.staged_git_marks}",
    ]
    assert popen_calls[1].args == [
        "/usr/bin/fossil",
        "import",
        "--git",
        "--export-marks",
        str(paths.staged_fossil_marks),
        str(paths.staged_fossil_repository),
    ]
    assert popen_calls[1].stdin is popen_calls[0].stdout
    assert popen_calls[0].stdout is not None
    assert popen_calls[0].stdout.closed is True
    assert paths.fossil_repository.exists()
    assert paths.git_marks.exists()
    assert paths.fossil_marks.exists()
    assert not paths.stage_dir.exists()


def test_import_git_repository_reuses_completed_staged_clone(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    url = "https://gitlab.com/group/subgroup/cache22.git"
    repository = parse_repository_url(url)
    paths = archive_paths_for_repository(archive_dir, repository)
    paths.mirror_dir.mkdir(parents=True)
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
            export_marks.write_text("git marks")
        elif args[0] == "/usr/bin/fossil":
            Path(args[4]).write_text("fossil marks")
            Path(args[5]).write_text("fossil repo")
        popen_calls.append(process)
        return process

    with patch("cache22.import_git.find_git_executable", return_value=Path("/usr/bin/git")):
        with patch("cache22.import_git.find_fossil_executable", return_value=Path("/usr/bin/fossil")):
            with patch("cache22.import_git.subprocess.run", side_effect=fake_run):
                with patch("cache22.import_git.subprocess.Popen", side_effect=fake_popen):
                    archive_path = import_git_repository(url, archive_dir=archive_dir)

    assert archive_path == paths.fossil_repository
    assert len(popen_calls) == 2
    assert paths.fossil_repository.exists()
    assert not paths.stage_dir.exists()


def test_import_git_repository_keeps_success_when_stage_cleanup_fails(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    url = "https://gitlab.com/group/subgroup/cache22.git"
    repository = parse_repository_url(url)
    paths = archive_paths_for_repository(archive_dir, repository)

    def fake_run(args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check is True
        Path(args[4]).mkdir(parents=True, exist_ok=True)
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

    with patch("cache22.import_git.find_git_executable", return_value=Path("/usr/bin/git")):
        with patch("cache22.import_git.find_fossil_executable", return_value=Path("/usr/bin/fossil")):
            with patch("cache22.import_git.subprocess.run", side_effect=fake_run):
                with patch("cache22.import_git.subprocess.Popen", side_effect=fake_popen):
                    with patch("cache22.import_git._clear_stage_dir", side_effect=OSError("cleanup failed")):
                        archive_path = import_git_repository(url, archive_dir=archive_dir)

    assert archive_path == paths.fossil_repository
    assert paths.fossil_repository.exists()
    assert paths.git_marks.exists()
    assert paths.fossil_marks.exists()
    assert paths.stage_dir.exists()


def test_import_git_repository_preserves_staged_clone_after_pipeline_failure(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    url = "https://gitlab.com/group/subgroup/cache22.git"
    repository = parse_repository_url(url)
    paths = archive_paths_for_repository(archive_dir, repository)

    def fake_run(args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        Path(args[4]).mkdir(parents=True, exist_ok=True)
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

    with patch("cache22.import_git.find_git_executable", return_value=Path("/usr/bin/git")):
        with patch("cache22.import_git.find_fossil_executable", return_value=Path("/usr/bin/fossil")):
            with patch("cache22.import_git.subprocess.run", side_effect=fake_run):
                with patch("cache22.import_git.subprocess.Popen", side_effect=fake_popen):
                    with pytest.raises(RuntimeError, match="Staged Git import state was kept"):
                        import_git_repository(url, archive_dir=archive_dir)

    assert paths.stage_dir.exists()
    assert paths.mirror_dir.exists()
    assert paths.clone_complete_marker.exists()
    assert not paths.fossil_repository.exists()


def test_import_git_repository_rejects_incomplete_staged_clone(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    url = "https://gitlab.com/group/subgroup/cache22.git"
    repository = parse_repository_url(url)
    paths = archive_paths_for_repository(archive_dir, repository)
    paths.mirror_dir.mkdir(parents=True)

    with patch("cache22.import_git.find_git_executable", return_value=Path("/usr/bin/git")):
        with patch("cache22.import_git.find_fossil_executable", return_value=Path("/usr/bin/fossil")):
            with pytest.raises(RuntimeError, match="cache22 import git-clear"):
                import_git_repository(url, archive_dir=archive_dir)


def test_clear_git_import_stage_removes_staged_clone(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    url = "https://gitlab.com/group/subgroup/cache22.git"
    repository = parse_repository_url(url)
    paths = archive_paths_for_repository(archive_dir, repository)
    paths.mirror_dir.mkdir(parents=True)
    paths.clone_complete_marker.write_text("complete\n")

    cleared_stage_dir, cleared = clear_git_import_stage(url, archive_dir=archive_dir)

    assert cleared_stage_dir == paths.stage_dir
    assert cleared is True
    assert not paths.stage_dir.exists()


def test_import_git_repository_rejects_existing_archive_state(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    archive_path = archive_dir / "github.com" / "ar-jan" / "cache22" / "cache22.fossil"
    archive_path.parent.mkdir(parents=True)
    archive_path.write_text("existing")

    with pytest.raises(ValueError, match="Archive state already exists"):
        import_git_repository("https://github.com/ar-jan/cache22.git", archive_dir=archive_dir)


def test_import_git_reports_missing_archive_dir_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    result = runner.invoke(app, ["import", "git", "https://github.com/ar-jan/cache22.git"])

    assert result.exit_code == 1
    assert "No archive directories configured" in result.output
    assert "Traceback" not in result.output


def test_import_git_reports_success_path(runner: CliRunner, tmp_path: Path) -> None:
    archive_path = tmp_path / "archive" / "github.com" / "ar-jan" / "cache22" / "cache22.fossil"

    with patch("cache22.cli.import_git_repository", return_value=archive_path):
        result = runner.invoke(app, ["import", "git", "https://github.com/ar-jan/cache22.git"])

    assert result.exit_code == 0
    assert f"Imported archive: {archive_path}" in result.output


def test_import_git_clear_reports_success_path(runner: CliRunner, tmp_path: Path) -> None:
    stage_dir = tmp_path / "archive" / ".cache22" / "git-import" / "github.com" / "ar-jan" / "cache22"

    with patch("cache22.cli.clear_git_import_stage", return_value=(stage_dir, True)):
        result = runner.invoke(app, ["import", "git-clear", "https://github.com/ar-jan/cache22.git"])

    assert result.exit_code == 0
    assert f"Cleared staged Git import: {stage_dir}" in result.output
