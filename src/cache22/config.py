from __future__ import annotations

import fcntl
import os
import tempfile
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import tomli_w

ArchiveType = Literal["git", "fossil"]
DEFAULT_ARCHIVE_TYPE: ArchiveType = "git"
SUPPORTED_ARCHIVE_TYPES = frozenset({"git", "fossil"})


class ConfigError(ValueError):
    """Raised when the persisted config is invalid."""


def config_home() -> Path:
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home)

    return Path.home() / ".config"


def config_dir() -> Path:
    return config_home() / "cache22"


def config_file() -> Path:
    return config_dir() / "config.toml"


@dataclass(slots=True)
class Config:
    archive_dirs: list[Path]
    archive_type: ArchiveType = DEFAULT_ARCHIVE_TYPE


def load_config() -> Config:
    path = config_file()
    if not path.exists():
        return Config(archive_dirs=[], archive_type=DEFAULT_ARCHIVE_TYPE)

    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Config file is not valid TOML: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"Config file could not be read: {path}") from exc

    return Config(
        archive_dirs=_parse_archive_dirs(data, path),
        archive_type=_parse_archive_type(data, path),
    )


@contextmanager
def _config_transaction() -> Iterator[Config]:
    """Serialize command read-modify-write operations on the stable directory inode."""
    directory = config_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            config = load_config()
            yield config
        finally:
            os.close(fd)
    except OSError as exc:
        raise ConfigError(f"Config update failed: {directory}: {exc}") from exc


def _save_config(config: Config) -> None:
    """Publish settings while the caller holds the configuration transaction lock."""
    path = config_file()

    payload = {
        "archive_dirs": [str(archive_dir) for archive_dir in config.archive_dirs],
        "archive_type": config.archive_type,
    }
    encoded = tomli_w.dumps(payload).encode()
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".config-", delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except OSError as exc:
        raise ConfigError(f"Config file could not be written: {path}") from exc
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink()


def normalize_archive_dir(raw_path: str | Path) -> Path:
    archive_dir = Path(raw_path).expanduser().resolve()
    if not archive_dir.exists():
        raise ValueError(f"Archive directory does not exist: {archive_dir}")
    if not archive_dir.is_dir():
        raise ValueError(f"Archive path is not a directory: {archive_dir}")
    if not os.access(archive_dir, os.W_OK | os.X_OK):
        raise ValueError(f"Archive directory is not writable and searchable: {archive_dir}")

    return archive_dir


def add_archive_dir(raw_path: str | Path) -> tuple[Path, bool]:
    archive_dir = normalize_archive_dir(raw_path)
    with _config_transaction() as config:
        if archive_dir in config.archive_dirs:
            return archive_dir, False
        config.archive_dirs.append(archive_dir)
        _save_config(config)
    return archive_dir, True


def list_archive_dirs() -> list[Path]:
    return load_config().archive_dirs


def default_archive_type() -> ArchiveType:
    return load_config().archive_type


def set_archive_type(raw_archive_type: str) -> ArchiveType:
    archive_type = normalize_archive_type(raw_archive_type)
    with _config_transaction() as config:
        config.archive_type = archive_type
        _save_config(config)
    return archive_type


def default_archive_dir() -> Path:
    archive_dirs = list_archive_dirs()
    if not archive_dirs:
        raise ValueError(
            "No archive directories configured. Add one with 'cache22 config archive add PATH'"
        )

    return archive_dirs[0]


def normalize_archive_type(raw_archive_type: str) -> ArchiveType:
    archive_type = raw_archive_type.strip().casefold()
    if archive_type not in SUPPORTED_ARCHIVE_TYPES:
        supported_types = ", ".join(sorted(SUPPORTED_ARCHIVE_TYPES))
        raise ValueError(
            f"Unsupported archive type: {raw_archive_type}. Expected one of: {supported_types}"
        )

    return cast(ArchiveType, archive_type)


def _parse_archive_dirs(data: Any, path: Path) -> list[Path]:
    if not isinstance(data, dict):
        raise ConfigError(f"Config file must contain a TOML table: {path}")

    raw_archive_dirs = data.get("archive_dirs", [])
    if not isinstance(raw_archive_dirs, list):
        raise ConfigError(f"'archive_dirs' must be a list of strings in {path}")

    archive_dirs: list[Path] = []
    seen_archive_dirs: set[Path] = set()
    for index, value in enumerate(raw_archive_dirs):
        if not isinstance(value, str):
            raise ConfigError(f"'archive_dirs[{index}]' must be a string in {path}")
        archive_dir = _normalize_persisted_archive_dir(value, path, index)
        if archive_dir in seen_archive_dirs:
            continue
        seen_archive_dirs.add(archive_dir)
        archive_dirs.append(archive_dir)

    return archive_dirs


def _parse_archive_type(data: Any, path: Path) -> ArchiveType:
    raw_archive_type = data.get("archive_type", DEFAULT_ARCHIVE_TYPE)
    if not isinstance(raw_archive_type, str):
        raise ConfigError(f"'archive_type' must be a string in {path}")

    try:
        return normalize_archive_type(raw_archive_type)
    except ValueError as exc:
        raise ConfigError(f"{exc} in {path}") from exc


def _normalize_persisted_archive_dir(raw_path: str, config_path: Path, index: int) -> Path:
    archive_dir = Path(raw_path).expanduser()
    if not archive_dir.is_absolute():
        raise ConfigError(f"'archive_dirs[{index}]' must be an absolute path in {config_path}")

    try:
        return archive_dir.resolve()
    except (OSError, RuntimeError) as exc:
        raise ConfigError(
            f"'archive_dirs[{index}]' could not be resolved in {config_path}"
        ) from exc
