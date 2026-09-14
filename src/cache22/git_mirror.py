from __future__ import annotations

import subprocess
from pathlib import Path
from typing import IO

from .archive_layout import ArchivePaths
from .archive_storage import RepositoryStorage


def ensure_git_mirror(
    *,
    git_executable: Path,
    url: str,
    storage: RepositoryStorage,
) -> str | None:
    paths = storage.paths
    if paths.clone_complete_marker.exists():
        if not paths.mirror_repository.exists():
            raise ValueError(
                f"Clone marker exists but mirror repository is missing: {paths.repository_dir}"
            )
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
