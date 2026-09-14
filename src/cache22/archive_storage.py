from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from .archive_layout import ArchivePaths
from .repository_ref import parse_repository_url, validate_storage_component

LOCK_SIGNATURE = b"cache22-storage-v1\n"

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
            (self.paths.source_file, False),
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

    def has_import_state(self) -> bool:
        return any(
            self.entry(path.name) is not None
            for path in (
                self.paths.mirror_repository,
                self.paths.fossil_repository,
                self.paths.temp_dir,
                self.paths.clone_complete_marker,
            )
        )

    def bind_source(self, source_path: str) -> None:
        name = self.paths.source_file.name
        stored = None
        if self.entry(name) is not None:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.directory_fd)
            with os.fdopen(fd) as handle:
                metadata = json.load(handle)
            if (
                not isinstance(metadata, dict)
                or set(metadata) != {"source_path"}
                or not isinstance(metadata["source_path"], str)
            ):
                raise ValueError(f"Malformed source metadata: {self.paths.source_file}")
            stored = metadata["source_path"]
            try:
                valid = parse_repository_url("https://" + stored + ".git", case_sensitive=True)
            except ValueError as exc:
                raise ValueError(f"Malformed source metadata: {self.paths.source_file}") from exc
            if valid.source_path != stored:
                raise ValueError(f"Malformed source metadata: {self.paths.source_file}")
        if self.has_import_state():
            if stored is None:
                raise ValueError(f"Archive data has no source binding: {self.paths.storage_dir}")
            if stored != source_path:
                raise ValueError(
                    f"Repository source conflict: stored {stored}; requested {source_path}"
                )
        if stored != source_path:
            if not self.has_import_state():
                self.release_unused_source()
            temporary = f".source-{uuid.uuid4().hex}"
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self.directory_fd
            )
            try:
                with os.fdopen(fd, "w") as handle:
                    json.dump({"source_path": source_path}, handle)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(
                    temporary, name, src_dir_fd=self.directory_fd, dst_dir_fd=self.directory_fd
                )
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=self.directory_fd)

    def release_unused_source(self) -> list[Path]:
        if self.has_import_state():
            return []
        removed = []
        for path in (self.paths.git_marks, self.paths.fossil_marks, self.paths.source_file):
            if self.remove(path.name):
                removed.append(path)
        return removed

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
        lock_fd = _open_owned_lock(directory_fd, paths, create=create)
        if lock_fd is None:
            yield None
            return
        try:
            _lock(lock_fd, paths)
            if os.pread(lock_fd, len(LOCK_SIGNATURE) + 1, 0) != LOCK_SIGNATURE:
                if create:
                    raise ValueError(f"Unrecognized archive storage: {paths.storage_dir}")
                yield None
                return
            storage = RepositoryStorage(paths, directory_fd)
            storage.validate()
            yield storage
        finally:
            os.close(lock_fd)


def _lock(fd: int, paths: ArchivePaths) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EAGAIN, errno.EACCES}:
            raise RepositoryBusyError(f"Repository is busy: {paths.repository_dir}") from exc
        raise


def _open_owned_lock(directory_fd: int, paths: ArchivePaths, *, create: bool) -> int | None:
    try:
        fd = os.open(
            paths.lock_file.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
        )
    except FileNotFoundError:
        if not create:
            return None
        entries = os.listdir(directory_fd)
        if entries:
            # A competing initializer may have just published its marker.
            if paths.lock_file.name in entries:
                return _open_owned_lock(directory_fd, paths, create=create)
            raise ValueError(
                f"Refusing to initialize nonempty archive storage: {paths.storage_dir}"
            )
        temporary = f"../.cache22-lock-init-{uuid.uuid4().hex}"
        fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
        try:
            os.write(fd, LOCK_SIGNATURE)
            os.fsync(fd)
            _lock(fd, paths)
            try:
                os.link(
                    temporary,
                    paths.lock_file.name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                os.close(fd)
                fd = -1
                return _open_owned_lock(directory_fd, paths, create=create)
            result = fd
            fd = -1
            return result
        finally:
            if fd != -1:
                os.close(fd)
            os.unlink(temporary, dir_fd=directory_fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        if create:
            raise ValueError(f"Unsafe archive lock: {paths.lock_file}")
        return None
    if os.pread(fd, len(LOCK_SIGNATURE) + 1, 0) != LOCK_SIGNATURE:
        os.close(fd)
        if create:
            raise ValueError(f"Unrecognized archive storage: {paths.storage_dir}")
        return None
    return fd
