from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import IO

from .archive_layout import ArchivePaths


def prepare_staging_dir(paths: ArchivePaths) -> None:
    if paths.temp_dir.exists():
        shutil.rmtree(paths.temp_dir)

    paths.repository_dir.mkdir(parents=True, exist_ok=True)
    paths.temp_dir.mkdir(parents=True, exist_ok=True)


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


def promote_staged_archive(paths: ArchivePaths) -> None:
    staged_targets = (
        (paths.temp_git_marks, paths.git_marks),
        (paths.temp_fossil_marks, paths.fossil_marks),
        (paths.temp_fossil_repository, paths.fossil_repository),
    )
    for staged_path, _ in staged_targets:
        if not staged_path.exists():
            raise RuntimeError(f"Expected staged import output was not created: {staged_path}")

    paths.repository_dir.mkdir(parents=True, exist_ok=True)
    for staged_path, final_path in staged_targets:
        staged_path.replace(final_path)


def clear_staging_dir(paths: ArchivePaths) -> None:
    if not paths.temp_dir.exists():
        return

    shutil.rmtree(paths.temp_dir)


def clear_staging_dir_after_success(paths: ArchivePaths) -> None:
    try:
        clear_staging_dir(paths)
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
