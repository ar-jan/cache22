from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from cache22.cli import app
from cache22.import_service import ImportResult


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_import_repo_reports_missing_archive_dir_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    result = runner.invoke(app, ["import", "repo", "https://github.com/ar-jan/cache22.git"])

    assert result.exit_code == 1
    assert "No archive directories configured" in result.output
    assert "Traceback" not in result.output


def test_import_repo_reports_success_path(runner: CliRunner, tmp_path: Path) -> None:
    archive_path = (
        tmp_path / "archive" / "github.com" / "ar-jan" / "cache22" / ".cache22" / "cache22.git"
    )

    with patch(
        "cache22.cli.import_repository",
        return_value=ImportResult(archive_path=archive_path),
    ):
        result = runner.invoke(app, ["import", "repo", "https://github.com/ar-jan/cache22.git"])

    assert result.exit_code == 0
    assert f"Imported archive: {archive_path}" in result.output


def test_import_repo_reports_info_messages_before_success_path(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    archive_path = (
        tmp_path / "archive" / "github.com" / "ar-jan" / "cache22" / ".cache22" / "cache22.git"
    )

    with patch(
        "cache22.cli.import_repository",
        return_value=ImportResult(
            archive_path=archive_path,
            info_messages=(f"INFO: archive already exists: {archive_path}",),
        ),
    ):
        result = runner.invoke(app, ["import", "repo", "https://github.com/ar-jan/cache22.git"])

    assert result.exit_code == 0
    assert result.output.splitlines() == [
        f"INFO: archive already exists: {archive_path}",
        f"Imported archive: {archive_path}",
    ]


def test_import_repo_rejects_extra_arguments_without_traceback(runner: CliRunner) -> None:
    result = runner.invoke(
        app,
        ["import", "repo", "git", "https://github.com/ar-jan/cache22.git"],
    )

    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_import_clean_repo_reports_removed_paths(runner: CliRunner, tmp_path: Path) -> None:
    removed_paths = (
        tmp_path / "archive" / "github.com" / "ar-jan" / "cache22" / ".cache22" / ".cache22-import",
        tmp_path / "archive" / "github.com" / "ar-jan" / "cache22" / ".cache22" / "cache22.git",
    )

    with patch("cache22.cli.clean_repository_import_state", return_value=removed_paths):
        result = runner.invoke(
            app,
            ["import", "clean", "repo", "https://github.com/ar-jan/cache22.git"],
        )

    assert result.exit_code == 0
    assert result.output.splitlines() == [
        f"Removed partial import state: {removed_paths[0]}",
        f"Removed partial import state: {removed_paths[1]}",
    ]


def test_import_clean_all_reports_no_partial_state(runner: CliRunner) -> None:
    with patch("cache22.cli.clean_all_import_state", return_value=()):
        result = runner.invoke(app, ["import", "clean", "all"])

    assert result.exit_code == 0
    assert result.output == "No partial import state found in configured archive directories.\n"
