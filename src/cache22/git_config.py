from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from .repository_ref import parse_repository_url

_BOOLEAN_KEYS = {
    "core.filemode",
    "core.bare",
    "core.ignorecase",
    "core.precomposeunicode",
    "core.symlinks",
    "remote.origin.mirror",
}
_ALLOWED_KEYS = _BOOLEAN_KEYS | {
    "core.repositoryformatversion",
    "core.logallrefupdates",
    "extensions.objectformat",
    "remote.origin.url",
    "remote.origin.fetch",
    "remote.origin.tagopt",
}


def git_repository_environment() -> dict[str, str]:
    """Keep trusted caller transport settings, but select repository storage explicitly."""
    return {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_INDEX_FILE",
            "GIT_NAMESPACE",
            "GIT_SHALLOW_FILE",
            "GIT_REPLACE_REF_BASE",
        }
    }


def git_repository_command(git: Path, mirror: Path, *args: str) -> list[str]:
    return [str(git), "-c", "core.hooksPath=/dev/null", "--git-dir", str(mirror), *args]


def git_local_environment() -> dict[str, str]:
    env = git_repository_environment()
    env.pop("GIT_CONFIG_PARAMETERS", None)
    env.pop("GIT_CONFIG", None)
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_COUNT="0",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_NO_LAZY_FETCH="1",
    )
    return env


def validate_git_mirror_config(git: Path, mirror: Path, source_path: str | None = None) -> str:
    """Read only the local config, without includes or repository-aware commands."""
    worktree_config = mirror / "config.worktree"
    if worktree_config.exists() or worktree_config.is_symlink():
        raise ValueError(f"Unsupported repository worktree configuration: {worktree_config}")
    config_path = mirror / "config"
    fd = os.open(config_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError(f"Unsafe Git configuration entry: {config_path}")
        contents = handle.read()
    result = subprocess.run(
        [str(git), "config", "--file", "-", "--no-includes", "--null", "--list"],
        input=contents,
        capture_output=True,
        check=False,
        env=git_repository_environment(),
    )
    if result.returncode != 0:
        raise ValueError(f"Could not parse repository configuration: {config_path}")
    config: dict[str, str | None] = {}
    for record in result.stdout.decode("utf-8").split("\0"):
        if not record:
            continue
        key, separator, value = record.partition("\n")
        if key not in _ALLOWED_KEYS:
            raise ValueError(f"Unsupported repository configuration key: {key}")
        if key in config:
            raise ValueError(f"Duplicate repository configuration key: {key}")
        config[key] = value if separator else None
    for key in _BOOLEAN_KEYS & config.keys():
        _boolean(config[key], key)
    # Git 2.55 mirror clones emit --no-tags; explicit refs/* still includes tags.
    if "remote.origin.tagopt" in config and config["remote.origin.tagopt"] not in {
        "--tags",
        "--no-tags",
    }:
        raise ValueError("Invalid repository configuration value: remote.origin.tagopt")
    if "core.logallrefupdates" in config and config["core.logallrefupdates"] != "always":
        _boolean(config["core.logallrefupdates"], "core.logallrefupdates")
    if "core.repositoryformatversion" in config and config["core.repositoryformatversion"] not in {
        "0",
        "1",
    }:
        raise ValueError("Invalid repository configuration value: core.repositoryformatversion")
    if "extensions.objectformat" in config and config["extensions.objectformat"] not in {
        "sha1",
        "sha256",
    }:
        raise ValueError("Invalid repository configuration value: extensions.objectformat")
    if "core.bare" not in config or not _boolean(config["core.bare"], "core.bare"):
        raise ValueError(f"Adoption and updates require a bare Git mirror: {mirror}")
    if (
        "remote.origin.mirror" not in config
        or not _boolean(config["remote.origin.mirror"], "remote.origin.mirror")
        or config.get("remote.origin.fetch") != "+refs/*:refs/*"
    ):
        raise ValueError(f"Origin must be configured as a full Git mirror: {mirror}")
    origin_url = config.get("remote.origin.url")
    if not origin_url:
        raise ValueError(f"Exactly one origin URL is required: {mirror}")
    try:
        origin = parse_repository_url(origin_url, case_sensitive=True)
    except ValueError as exc:
        raise ValueError("Invalid repository configuration value: remote.origin.url") from exc
    if source_path is not None and origin.source_path != source_path:
        raise ValueError(
            f"Repository source conflict: origin {origin.source_path}; requested {source_path}"
        )

    return origin_url


def _boolean(value: str | None, key: str) -> bool:
    if value is None or value.lower() in {"true", "yes", "on"}:
        return True
    if value.lower() in {"", "false", "no", "off"}:
        return False
    try:
        return int(value) != 0
    except ValueError as exc:
        raise ValueError(f"Invalid repository configuration value: {key}") from exc
