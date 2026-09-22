from __future__ import annotations

import shutil
from pathlib import Path


def find_git_executable() -> Path:
    return _find_required_executable("git")


def _find_required_executable(name: str) -> Path:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"{name} executable not found in PATH")

    return Path(executable).resolve()
