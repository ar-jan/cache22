from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import IO

from .archive_layout import ArchivePaths


def ensure_git_mirror(
    *,
    git_executable: Path,
    url: str,
    paths: ArchivePaths,
) -> str | None:
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

    paths.repository_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [str(git_executable), "clone", "--mirror", url, str(paths.mirror_repository)],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        _clear_incomplete_mirror(paths)
        raise RuntimeError(f"git clone --mirror failed with exit code {exc.returncode}") from exc

    try:
        paths.clone_complete_marker.write_text("complete\n")
    except OSError:
        _clear_incomplete_mirror(paths)
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


def _clear_incomplete_mirror(paths: ArchivePaths) -> None:
    if paths.clone_complete_marker.exists():
        try:
            paths.clone_complete_marker.unlink()
        except OSError:
            return

    if not paths.mirror_repository.exists():
        return

    try:
        shutil.rmtree(paths.mirror_repository)
    except OSError:
        return
