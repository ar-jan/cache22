from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class FossilStatus:
    executable: Path
    version: str


def find_git_executable() -> Path:
    return _find_required_executable("git")


def find_fossil_executable() -> Path:
    return _find_required_executable("fossil")


def get_fossil_status() -> FossilStatus:
    executable = find_fossil_executable()
    try:
        result = subprocess.run(
            [str(executable), "version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"fossil version command failed with exit code {exc.returncode}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"fossil version command could not be run: {exc}") from exc
    return FossilStatus(
        executable=executable,
        version=result.stdout.strip(),
    )


def _find_required_executable(name: str) -> Path:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"{name} executable not found in PATH")

    return Path(executable).resolve()
