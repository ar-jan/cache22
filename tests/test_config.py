from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from cache22 import config as config_module
from cache22.cli import app
from cache22.config import (
    Config,
    ConfigError,
    _save_config,
    add_archive_dir,
    default_archive_type,
    list_archive_dirs,
    load_config,
    normalize_archive_dir,
    set_archive_type,
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


def test_load_defaults_archive_type_to_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    write_config(tmp_path, f'archive_dirs = ["{archive_dir}"]\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    config = load_config()

    assert config.archive_dirs == [archive_dir.resolve()]
    assert config.archive_type == "git"


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


def test_rejects_non_string_archive_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, "archive_type = 123\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    with pytest.raises(ConfigError, match="'archive_type' must be a string"):
        load_config()


def test_rejects_invalid_archive_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, 'archive_type = "bundle"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    with pytest.raises(ConfigError, match="Unsupported archive type"):
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

    with (
        patch("cache22.config.os.access", return_value=False) as access,
        pytest.raises(ValueError, match="not writable and searchable"),
    ):
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


def test_set_archive_type_persists_normalized_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    archive_type = set_archive_type("FOSSIL")

    assert archive_type == "fossil"
    assert default_archive_type() == "fossil"


def test_load_reports_unreadable_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "cache22"
    config_dir.mkdir()
    (config_dir / "config.toml").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    with pytest.raises(ConfigError, match="Config file could not be read"):
        load_config()


def test_save_reports_unwritable_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    with (
        patch("cache22.config.tempfile.NamedTemporaryFile", side_effect=OSError("disk full")),
        pytest.raises(ConfigError, match="Config file could not be written"),
    ):
        _save_config(Config(archive_dirs=[archive_dir]))


@pytest.mark.parametrize("failure", ["write", "fsync", "replace"])
def test_failed_save_preserves_previous_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    original = Config([tmp_path / "archive"], "fossil")
    _save_config(original)
    path = tmp_path / "cache22" / "config.toml"
    previous_bytes = path.read_bytes()
    real_temporary_file = tempfile.NamedTemporaryFile

    @contextmanager
    def failing_writer(**kwargs) -> Iterator[object]:
        with (
            real_temporary_file(**kwargs) as handle,
            patch.object(handle, "write", side_effect=OSError("disk full")),
        ):
            yield handle

    target, effect = {
        "write": ("cache22.config.tempfile.NamedTemporaryFile", failing_writer),
        "fsync": ("cache22.config.os.fsync", OSError("disk full")),
        "replace": ("pathlib.Path.replace", OSError("replacement failed")),
    }[failure]
    with (
        patch(target, side_effect=effect),
        pytest.raises(ConfigError, match="Config file could not be written"),
    ):
        _save_config(Config(original.archive_dirs, "git"))

    assert path.read_bytes() == previous_bytes
    assert load_config() == original
    assert list(path.parent.iterdir()) == [path]


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

    with patch("cache22.config._save_config", side_effect=OSError("disk full")):
        result = runner.invoke(app, ["config", "archive", "add", str(archive_dir)])

    assert result.exit_code == 1
    assert "disk full" in result.output
    assert "Traceback" not in result.output


def test_archive_type_show_reports_configured_value(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, 'archive_type = "fossil"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    result = runner.invoke(app, ["config", "archive-type", "show"])

    assert result.exit_code == 0
    assert result.output == "fossil\n"


def test_archive_type_set_updates_config_without_traceback(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    result = runner.invoke(app, ["config", "archive-type", "set", "fossil"])

    assert result.exit_code == 0
    assert "Default archive type: fossil" in result.output
    assert default_archive_type() == "fossil"
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


@pytest.mark.parametrize("second_change", ["archive", "archive_type"])
def test_concurrent_config_commands_preserve_both_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, second_change: str
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    publishing, contending, release = Event(), Event(), Event()
    save = config_module._save_config
    flock = config_module.fcntl.flock

    def paused_save(config: Config) -> None:
        if not publishing.is_set():
            publishing.set()
            assert release.wait(5)
        save(config)

    def observed_lock(fd: int, operation: int) -> None:
        if publishing.is_set():
            contending.set()
        flock(fd, operation)

    with (
        patch("cache22.config._save_config", side_effect=paused_save),
        patch("cache22.config.fcntl.flock", side_effect=observed_lock),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first_result = pool.submit(add_archive_dir, first)
        try:
            assert publishing.wait(5)
            second_result = (
                pool.submit(add_archive_dir, second)
                if second_change == "archive"
                else pool.submit(set_archive_type, "fossil")
            )
            assert contending.wait(5)
        finally:
            release.set()
        assert first_result.result(timeout=5) == (first, True)
        second_result.result(timeout=5)

    config = load_config()
    assert config.archive_dirs == ([first, second] if second_change == "archive" else [first])
    assert config.archive_type == ("git" if second_change == "archive" else "fossil")


def test_failed_config_transaction_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with (
        patch("cache22.config._save_config", side_effect=OSError("disk full")),
        pytest.raises(ConfigError, match="disk full"),
    ):
        add_archive_dir(tmp_path)
    fd = os.open(tmp_path / "cache22", os.O_RDONLY | os.O_DIRECTORY)
    try:
        config_module.fcntl.flock(fd, config_module.fcntl.LOCK_EX | config_module.fcntl.LOCK_NB)
    finally:
        os.close(fd)
    assert add_archive_dir(tmp_path) == (tmp_path, True)
