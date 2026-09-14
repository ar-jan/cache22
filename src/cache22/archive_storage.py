from __future__ import annotations

import errno
import fcntl
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from .archive_layout import ArchivePaths
from .repository_ref import validate_storage_component

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class RepositoryBusyError(RuntimeError):
    """Another Cache22 operation holds this repository's lock."""


@contextmanager
def open_archive_directory(
    root: Path, relative: Path, *, create: bool = False
) -> Iterator[int | None]:
    """Open directories beneath a resolved archive root without following symlinks."""
    if relative.is_absolute():
        raise ValueError(f"Archive path must be relative: {relative}")
    for part in relative.parts:
        validate_storage_component(part)

    directory_fd = os.open(root, _DIRECTORY_FLAGS)
    try:
        for part in relative.parts:
            try:
                child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                with suppress(FileExistsError):
                    os.mkdir(part, dir_fd=directory_fd)
                child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        yield directory_fd
    finally:
        os.close(directory_fd)


@dataclass(frozen=True)
class RepositoryStorage:
    paths: ArchivePaths
    directory_fd: int

    @contextmanager
    def open_directory(self, name: str) -> Iterator[int]:
        validate_storage_component(name)
        fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=self.directory_fd)
        try:
            yield fd
        finally:
            os.close(fd)

    def entry(self, name: str) -> os.stat_result | None:
        validate_storage_component(name)
        try:
            result = os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not (stat.S_ISREG(result.st_mode) or stat.S_ISDIR(result.st_mode)):
            raise ValueError(f"Unsafe archive entry: {self.paths.storage_dir / name}")
        return result

    def validate(self) -> None:
        for path, directory in (
            (self.paths.mirror_repository, True),
            (self.paths.temp_dir, True),
            (self.paths.clone_complete_marker, False),
            (self.paths.fossil_repository, False),
            (self.paths.git_marks, False),
            (self.paths.fossil_marks, False),
        ):
            result = self.entry(path.name)
            if result is not None and stat.S_ISDIR(result.st_mode) != directory:
                raise ValueError(f"Unexpected archive entry type: {path}")

    def remove(self, name: str) -> bool:
        result = self.entry(name)
        if result is None:
            return False
        if stat.S_ISDIR(result.st_mode):
            shutil.rmtree(name, dir_fd=self.directory_fd)
        else:
            os.unlink(name, dir_fd=self.directory_fd)
        return True

    def write_clone_marker(self) -> None:
        name = self.paths.clone_complete_marker.name
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.directory_fd,
        )
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write("complete\n")
        except OSError:
            with suppress(OSError):
                os.unlink(name, dir_fd=self.directory_fd)
            raise


@contextmanager
def repository_operation(
    root: Path, paths: ArchivePaths, *, create: bool = False
) -> Iterator[RepositoryStorage | None]:
    relative = paths.storage_dir.relative_to(root)
    with open_archive_directory(root, relative, create=create) as directory_fd:
        if directory_fd is None:
            yield None
            return
        lock_fd = os.open(
            paths.lock_file.name,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise ValueError(f"Unsafe archive lock: {paths.lock_file}")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EAGAIN, errno.EACCES}:
                    raise RepositoryBusyError(
                        f"Repository is busy: {paths.repository_dir}"
                    ) from exc
                raise
            storage = RepositoryStorage(paths, directory_fd)
            storage.validate()
            yield storage
        finally:
            os.close(lock_fd)
