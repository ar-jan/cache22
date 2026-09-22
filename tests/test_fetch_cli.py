from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from cache22.cli import app
from cache22.index import Index
from cache22.repo_service import ImportResult
from cache22.repository_ref import parse_repository_url

URL = "https://github.com/ar-jan/cache22.git"
KEY = "github.com/ar-jan/cache22"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_fetch_url_reports_missing_archive_dir_without_traceback(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    result = runner.invoke(app, ["repo", "fetch", URL])

    assert result.exit_code == 1
    assert "No archive directories configured" in result.output
    assert "Traceback" not in result.output


def test_fetch_unknown_key_is_rejected_without_registration(runner: CliRunner) -> None:
    with patch("cache22.repo_cli.import_repository", side_effect=AssertionError) as call:
        result = runner.invoke(app, ["repo", "fetch", KEY])

    assert result.exit_code == 1
    assert f"Repository is not indexed: {KEY}" in result.output
    call.assert_not_called()


def test_fetch_new_url_registers_and_passes_options(runner: CliRunner, tmp_path: Path) -> None:
    with patch("cache22.repo_cli.import_repository", return_value=ImportResult(tmp_path)) as call:
        result = runner.invoke(app, ["repo", "fetch", URL, "--case-sensitive"])

    # The mocked import registers nothing, so the summary row falls back to the selector.
    assert result.exit_code == 1
    assert f"Repository is not indexed: {KEY}" in result.output
    call.assert_called_once()
    assert call.call_args.args == (URL,)
    assert call.call_args.kwargs["case_sensitive"] is True
    assert call.call_args.kwargs["adopt"] is False
    assert call.call_args.kwargs["archive_dir"] is None


def register(root: Path) -> dict[str, Any]:
    root.mkdir(exist_ok=True)
    return Index().add(parse_repository_url(URL), root)


def test_fetch_indexed_key_uses_stored_binding_and_reports_info(
    runner: CliRunner, tmp_path: Path
) -> None:
    record = register(tmp_path / "archive")
    archive_path = tmp_path / "archive" / "github.com" / "ar-jan" / "cache22" / "cache22.git"

    with patch(
        "cache22.repo_cli.import_repository",
        return_value=ImportResult(
            archive_path, info_messages=(f"INFO: updated Git mirror: {archive_path}",)
        ),
    ) as call:
        result = runner.invoke(app, ["repo", "fetch", KEY])

    assert result.exit_code == 0, result.output
    call.assert_called_once()
    assert call.call_args.args == (record["source_url"], Path(record["archive_root"]))
    assert call.call_args.kwargs["case_sensitive"] is True
    lines = result.output.splitlines()
    assert lines[0] == f"INFO: updated Git mirror: {archive_path}"
    assert lines[1].startswith(KEY)


def test_fetch_json_suppresses_info_messages(runner: CliRunner, tmp_path: Path) -> None:
    register(tmp_path / "archive")
    with patch(
        "cache22.repo_cli.import_repository",
        return_value=ImportResult(tmp_path, info_messages=("INFO: noise",)),
    ):
        result = runner.invoke(app, ["repo", "fetch", KEY, "--json"])

    assert result.exit_code == 0, result.output
    assert "INFO" not in result.output
    assert json.loads(result.output)[0]["repo_key"] == KEY


def test_fetch_requires_selectors_or_all(runner: CliRunner) -> None:
    assert runner.invoke(app, ["repo", "fetch"]).exit_code == 2
    assert runner.invoke(app, ["repo", "fetch", KEY, "--all"]).exit_code == 2


def test_clean_url_reports_removed_paths(runner: CliRunner, tmp_path: Path) -> None:
    removed_paths = (
        tmp_path / "github.com" / "ar-jan" / "cache22" / ".cache22-bundle",
        tmp_path / "github.com" / "ar-jan" / "cache22" / "cache22.git",
    )

    with patch(
        "cache22.repo_cli.clean_repository_import_state", return_value=removed_paths
    ) as call:
        result = runner.invoke(app, ["repo", "clean", URL])

    assert result.exit_code == 0
    call.assert_called_once_with(URL)
    assert result.output.splitlines() == [
        f"Removed partial import state: {removed_paths[0]}",
        f"Removed partial import state: {removed_paths[1]}",
    ]


def test_clean_indexed_key_uses_stored_source_url(runner: CliRunner, tmp_path: Path) -> None:
    register(tmp_path / "archive")
    with patch("cache22.repo_cli.clean_repository_import_state", return_value=()) as call:
        result = runner.invoke(app, ["repo", "clean", KEY])

    assert result.exit_code == 0
    call.assert_called_once_with(URL)
    assert result.output == "No partial import state found.\n"


def test_clean_unknown_key_is_rejected(runner: CliRunner) -> None:
    with patch("cache22.repo_cli.clean_repository_import_state") as call:
        result = runner.invoke(app, ["repo", "clean", KEY])

    assert result.exit_code == 1
    assert f"Repository is not indexed: {KEY}" in result.output
    call.assert_not_called()


def test_clean_all_reports_no_partial_state(runner: CliRunner) -> None:
    with patch("cache22.repo_cli.clean_all_import_state", return_value=()) as call:
        result = runner.invoke(app, ["repo", "clean", "--all"])

    assert result.exit_code == 0
    call.assert_called_once_with()
    assert result.output == "No partial import state found.\n"


def test_import_group_is_removed(runner: CliRunner) -> None:
    result = runner.invoke(app, ["import", "repo", URL])

    assert result.exit_code == 2
    assert "Traceback" not in result.output
