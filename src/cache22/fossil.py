from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class FossilStatus:
    executable: Path
    version: str


def get_fossil_status() -> FossilStatus:
    executable = shutil.which("fossil")
    if executable is None:
        raise RuntimeError("fossil executable not found in PATH")

    result = subprocess.run(
        [executable, "version"],
        check=True,
        capture_output=True,
        text=True,
    )
    return FossilStatus(
        executable=Path(executable).resolve(),
        version=result.stdout.strip(),
    )
