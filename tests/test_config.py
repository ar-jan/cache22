from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from cache22.cli import app
from cache22.config import (
    Config,
    ConfigError,
    add_archive_dir,
    list_archive_dirs,
    load_config,
    normalize_archive_dir,
    save_config,
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def write_config(tmp_path: Path, contents: str) -> Path:
    config_dir = tmp_path / "cache22"
    config_dir.mkdir()
    config_path = config_dir / "config.toml"
    config_path.write_text(contents)
    return config_path


def test_rejects_non_list_archive_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, 'archive_dirs = "not-a-list"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    with pytest.raises(ConfigError, match="'archive_dirs' must be a list of strings"):
        load_config()


def test_rejects_non_string_archive_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, "archive_dirs = [123]\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    with pytest.raises(ConfigError, match=r"'archive_dirs\[0\]' must be a string"):
        load_config()


def test_rejects_invalid_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, 'archive_dirs = ["/tmp/archive"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    with pytest.raises(ConfigError, match="Config file is not valid TOML"):
        load_config()


def test_rejects_relative_archive_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, 'archive_dirs = ["relative-dir"]\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    with pytest.raises(ConfigError, match=r"'archive_dirs\[0\]' must be an absolute path"):
        load_config()


def test_requires_search_permission(tmp_path: Path) -> None:
    archive_dir = tmp_path

    with patch("cache22.config.os.access", return_value=False) as access:
        with pytest.raises(ValueError, match="not writable and searchable"):
            normalize_archive_dir(archive_dir)

    access.assert_called_once_with(archive_dir, os.W_OK | os.X_OK)


def test_load_canonicalizes_and_deduplicates_archive_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    alias_dir = tmp_path / "archive-alias"
    alias_dir.symlink_to(archive_dir, target_is_directory=True)
    write_config(tmp_path, f'archive_dirs = ["{alias_dir}", "{archive_dir}"]\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert list_archive_dirs() == [archive_dir.resolve()]


def test_add_archive_dir_detects_duplicate_loaded_canonical_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    alias_dir = tmp_path / "archive-alias"
    alias_dir.symlink_to(archive_dir, target_is_directory=True)
    write_config(tmp_path, f'archive_dirs = ["{alias_dir}"]\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    configured_dir, added = add_archive_dir(archive_dir)

    assert configured_dir == archive_dir.resolve()
    assert added is False
    assert list_archive_dirs() == [archive_dir.resolve()]


def test_load_reports_unreadable_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / "cache22"
    config_dir.mkdir()
    (config_dir / "config.toml").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    with pytest.raises(ConfigError, match="Config file could not be read"):
        load_config()


def test_save_reports_unwritable_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    with patch("pathlib.Path.open", side_effect=OSError("disk full")):
        with pytest.raises(ConfigError, match="Config file could not be written"):
            save_config(Config(archive_dirs=[archive_dir]))


def test_list_reports_invalid_config_without_traceback(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, 'archive_dirs = "not-a-list"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    result = runner.invoke(app, ["config", "archive", "list"])

    assert result.exit_code == 1
    assert "'archive_dirs' must be a list of strings" in result.output
    assert "Traceback" not in result.output


def test_add_reports_write_failure_without_traceback(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    with patch("cache22.config.save_config", side_effect=OSError("disk full")):
        result = runner.invoke(app, ["config", "archive", "add", str(archive_dir)])

    assert result.exit_code == 1
    assert "disk full" in result.output
    assert "Traceback" not in result.output


def test_list_reports_unreadable_config_without_traceback(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "cache22"
    config_dir.mkdir()
    (config_dir / "config.toml").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    result = runner.invoke(app, ["config", "archive", "list"])

    assert result.exit_code == 1
    assert "Config file could not be read" in result.output
    assert "Traceback" not in result.output
