from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import IO

from .archive_layout import ArchivePaths
from .archive_storage import RepositoryStorage


def prepare_staging_dir(storage: RepositoryStorage) -> None:
    name = storage.paths.temp_dir.name
    storage.remove(name)
    os.mkdir(name, dir_fd=storage.directory_fd)


def open_fossil_import(
    *,
    fossil_executable: Path,
    paths: ArchivePaths,
    fast_export_stream: IO[bytes],
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            str(fossil_executable),
            "import",
            "--git",
            "--export-marks",
            str(paths.temp_fossil_marks),
            str(paths.temp_fossil_repository),
        ],
        stdin=fast_export_stream,
    )


def promote_staged_archive(storage: RepositoryStorage) -> None:
    paths = storage.paths
    staged_targets = (
        (paths.temp_git_marks, paths.git_marks),
        (paths.temp_fossil_marks, paths.fossil_marks),
        (paths.temp_fossil_repository, paths.fossil_repository),
    )
    with storage.open_directory(paths.temp_dir.name) as staging_fd:
        for staged_path, _ in staged_targets:
            try:
                result = os.stat(staged_path.name, dir_fd=staging_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"Expected staged import output was not created: {staged_path}"
                ) from exc
            if not stat.S_ISREG(result.st_mode):
                raise ValueError(f"Unsafe staged archive entry: {staged_path}")
        for staged_path, final_path in staged_targets:
            os.replace(
                staged_path.name,
                final_path.name,
                src_dir_fd=staging_fd,
                dst_dir_fd=storage.directory_fd,
            )


def clear_staging_dir(storage: RepositoryStorage) -> None:
    storage.remove(storage.paths.temp_dir.name)


def clear_staging_dir_after_success(storage: RepositoryStorage) -> None:
    try:
        clear_staging_dir(storage)
    except OSError:
        # The archive is already promoted. A cleanup failure should not turn a successful import
        # into a retry trap where the final archive exists but the command reported failure.
        return


def import_failure_message(url: str, paths: ArchivePaths, message: str) -> str:
    if paths.temp_dir.exists():
        return (
            f"{message}. Temporary Fossil import state was kept at {paths.temp_dir}. "
            f"Clear it with 'cache22 import clean repo {url}' to start over."
        )

    return message
