from __future__ import annotations

import os
import stat
from pathlib import Path

from .archive_layout import LOCK_FILE_NAME


def validate_git_mirror_layout(mirror: Path) -> None:
    """Check self-contained storage before Git reads or writes an existing mirror."""
    if not stat.S_ISDIR(mirror.lstat().st_mode):
        raise ValueError(f"Unsafe Git mirror entry: {mirror}")
    for name, directory in (("HEAD", False), ("config", False), ("objects", True), ("refs", True)):
        entry = mirror / name
        try:
            mode = entry.lstat().st_mode
        except FileNotFoundError as exc:
            raise ValueError(f"Expected a bare Git mirror: missing {entry}") from exc
        if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
            raise ValueError(f"Unsafe Git mirror entry: {entry}")

    # Git must not traverse redirected storage or a nested Cache22 repository.
    def walk_error(exc: OSError) -> None:
        raise exc

    for directory, children, files in os.walk(mirror, onerror=walk_error, followlinks=False):
        for name in (*children, *files):
            entry = Path(directory) / name
            mode = entry.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError(f"Unsafe Git mirror entry: {entry}")
            if name == LOCK_FILE_NAME and not stat.S_ISDIR(mode):
                raise ValueError(f"Repository path conflict: nested repository boundary: {entry}")
    for name in ("commondir", "shallow", "objects/info/alternates", "objects/info/http-alternates"):
        if (mirror / name).exists():
            raise ValueError(f"Expected a complete, self-contained Git mirror: {mirror / name}")
    if any((mirror / "objects" / "pack").glob("*.promisor")):
        raise ValueError(f"Partial Git clones are not supported: {mirror}")
