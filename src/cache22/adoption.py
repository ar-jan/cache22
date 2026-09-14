from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from .archive_layout import LOCK_FILE_NAME
from .archive_storage import RepositoryStorage
from .git_config import (
    git_repository_command,
    git_repository_environment,
    validate_git_mirror_config,
)
from .system_tools import find_git_executable


class AdoptionRequiredError(ValueError):
    """A candidate needs explicit adoption; all storage locks are released on exit."""

    def __init__(self, archive_dir: Path, repository_dir: Path) -> None:
        self.archive_dir = archive_dir
        self.repository_dir = repository_dir
        self.conflict_message = _conflict_message(repository_dir)
        super().__init__(
            f"{self.conflict_message}. Rerun with --adopt to verify and initialize "
            "the existing Git mirror."
        )


def prepare_git_import(
    storage: RepositoryStorage, *, archive_dir: Path, source_path: str, adopt: bool
) -> bool:
    """Verify and initialize under directory reservations and any existing repository lock."""
    paths = storage.paths
    owned = storage.entry(paths.lock_file.name) is not None
    try:
        storage.validate()
    except ValueError as exc:
        if not owned:
            raise ValueError(f"Repository path conflict: {paths.repository_dir}: {exc}") from exc
        raise
    if storage.entry(paths.mirror_repository.name) is None:
        if not owned and os.listdir(storage.directory_fd):
            raise ValueError(_conflict_message(paths.repository_dir))
        if adopt and storage.has_import_state():
            raise ValueError(f"Adoption requires an existing Git mirror: {paths.mirror_repository}")
        return False

    initialized = (
        owned
        and storage.entry(paths.clone_complete_marker.name) is not None
        and storage.entry(paths.source_file.name) is not None
    )
    if initialized and not adopt:
        return False

    try:
        _validate_layout(storage, source_path)
    except ValueError as exc:
        if not owned:
            raise ValueError(f"Repository path conflict: {paths.repository_dir}: {exc}") from exc
        raise
    if not adopt:
        raise AdoptionRequiredError(archive_dir, paths.repository_dir)

    _verify_mirror(paths.mirror_repository, source_path)
    # Completion comes first so cleanup cannot delete the verified mirror if
    # publication is interrupted. Missing source/ownership metadata is retryable.
    if storage.entry(paths.clone_complete_marker.name) is None:
        storage.write_clone_marker()
    storage.bind_source(source_path, allow_unbound=True)
    return not initialized


def _conflict_message(repository_dir: Path) -> str:
    return (
        f"Repository path conflict: {repository_dir} is a nonempty "
        "namespace or uninitialized directory"
    )


def _validate_layout(storage: RepositoryStorage, source_path: str) -> None:
    paths = storage.paths
    bound = storage.check_source(source_path, allow_unbound=True) is not None
    managed = storage.entry(paths.lock_file.name) is not None and bound
    if not managed:
        for path in (paths.fossil_repository, paths.git_marks, paths.fossil_marks):
            if storage.entry(path.name) is not None:
                raise ValueError(f"Git adoption cannot verify Fossil artifacts: {path}")
    allowed = {
        paths.mirror_repository.name,
        paths.lock_file.name,
        paths.source_file.name,
        paths.clone_complete_marker.name,
        paths.fossil_repository.name,
        paths.git_marks.name,
        paths.fossil_marks.name,
    }
    unexpected = set(os.listdir(storage.directory_fd)) - allowed
    if unexpected:
        raise ValueError(
            f"Cannot adopt {paths.repository_dir}: unexpected entries: "
            f"{', '.join(sorted(unexpected))}"
        )
    if (
        storage.entry(paths.clone_complete_marker.name) is not None
        and paths.clone_complete_marker.read_bytes() != b"complete\n"
    ):
        raise ValueError(f"Malformed clone completion marker: {paths.clone_complete_marker}")

    mirror = paths.mirror_repository
    for name, directory in (("HEAD", False), ("config", False), ("objects", True), ("refs", True)):
        entry = mirror / name
        try:
            mode = entry.lstat().st_mode
        except FileNotFoundError as exc:
            raise ValueError(f"Adoption requires a bare Git mirror: missing {entry}") from exc
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
            raise ValueError(
                f"Adoption requires a complete, self-contained mirror: {mirror / name}"
            )
    if any((mirror / "objects" / "pack").glob("*.promisor")):
        raise ValueError(f"Cannot adopt a partial Git clone: {mirror}")


def _verify_mirror(mirror: Path, source_path: str) -> None:
    git = find_git_executable()
    validate_git_mirror_config(git, mirror, source_path)
    env = git_repository_environment()
    env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1")

    def run(*args: str) -> str:
        result = subprocess.run(
            git_repository_command(git, mirror, *args),
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            raise ValueError(
                f"Git mirror verification failed at {mirror} ({args[0]}): "
                f"{result.stderr.strip() or result.stdout.strip() or f'exit code {result.returncode}'}"
            )
        return result.stdout.strip()

    if run("rev-parse", "--is-bare-repository") != "true":
        raise ValueError(f"Adoption requires a bare Git mirror: {mirror}")
    run("fsck", "--full")
