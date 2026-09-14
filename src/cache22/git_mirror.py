from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import IO

from .archive_layout import ArchivePaths
from .archive_storage import RepositoryStorage


def git_repository_environment() -> dict[str, str]:
    """Keep transport/configuration settings, but select storage explicitly."""
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


def ensure_git_mirror(
    *,
    git_executable: Path,
    url: str,
    storage: RepositoryStorage,
    update: bool = False,
) -> str | None:
    paths = storage.paths
    if paths.clone_complete_marker.exists():
        if not paths.mirror_repository.exists():
            raise ValueError(
                f"Clone marker exists but mirror repository is missing: {paths.repository_dir}"
            )
        if update:
            _fetch_git_mirror(git_executable=git_executable, url=url, paths=paths)
            return f"INFO: updated Git mirror: {paths.mirror_repository}"
        return f"INFO: archive already exists: {paths.mirror_repository}"

    if paths.mirror_repository.exists():
        raise ValueError(
            f"Mirror repository exists without a completion marker: {paths.mirror_repository}. "
            f"Clear it with 'cache22 import clean repo {url}' to start over."
        )

    try:
        subprocess.run(
            [str(git_executable), "clone", "--mirror", "--", url, str(paths.mirror_repository)],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        _clear_incomplete_mirror(storage)
        raise RuntimeError(f"git clone --mirror failed with exit code {exc.returncode}") from exc

    try:
        storage.write_clone_marker()
    except OSError:
        _clear_incomplete_mirror(storage)
        raise

    return None


def _fetch_git_mirror(*, git_executable: Path, url: str, paths: ArchivePaths) -> None:
    try:
        subprocess.run(
            [
                str(git_executable),
                "-C",
                str(paths.mirror_repository),
                "fetch",
                "--atomic",
                "--prune",
                "--no-recurse-submodules",
                "--no-write-fetch-head",
                "--refmap=",
                "--",
                url,
                "+refs/*:refs/*",
            ],
            check=True,
            env=git_repository_environment(),
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"git fetch failed with exit code {exc.returncode}; "
            "the initialized mirror was kept. Retry the import to fetch updates."
        ) from exc


def open_fast_export(
    *,
    git_executable: Path,
    paths: ArchivePaths,
) -> tuple[subprocess.Popen[bytes], IO[bytes]]:
    process = subprocess.Popen(
        [
            str(git_executable),
            "-C",
            str(paths.mirror_repository),
            "fast-export",
            "--all",
            "--signed-tags=warn-strip",
            f"--export-marks={paths.temp_git_marks}",
        ],
        stdout=subprocess.PIPE,
    )
    if process.stdout is None:
        process.kill()
        process.wait()
        raise RuntimeError("git fast-export did not provide a stdout stream")

    return process, process.stdout


def _clear_incomplete_mirror(storage: RepositoryStorage) -> None:
    if storage.entry(storage.paths.clone_complete_marker.name) is not None:
        return
    storage.remove(storage.paths.mirror_repository.name)
