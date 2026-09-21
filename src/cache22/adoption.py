from __future__ import annotations

import os
from pathlib import Path

from .archive_storage import RepositoryStorage
from .git_config import (
    git_repository_command,
    git_repository_environment,
    validate_git_mirror_config,
)
from .git_layout import validate_git_mirror_layout
from .operation import run as run_git
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
    if storage.entry(paths.bundle_manifest.name) is not None:
        if not owned or adopt:
            raise ValueError("Bundle adoption is not supported")
        from .git_bundle import read_bundle

        read_bundle(storage, source_path)
        return False
    try:
        storage.validate()
        storage.validate_clone_marker()
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
    validate_git_mirror_layout(paths.mirror_repository)


def _verify_mirror(mirror: Path, source_path: str) -> None:
    git = find_git_executable()
    validate_git_mirror_config(git, mirror, source_path)
    env = git_repository_environment()
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_COUNT="0",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_NO_LAZY_FETCH="1",
    )

    def run(*args: str) -> str:
        result = run_git(
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
