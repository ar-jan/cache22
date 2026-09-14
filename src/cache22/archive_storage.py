from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from .archive_layout import LOCK_FILE_NAME, ArchivePaths
from .repository_ref import parse_repository_url, validate_storage_component

LOCK_SIGNATURE = b"cache22-storage-v1\n"

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class RepositoryBusyError(RuntimeError):
    """Another Cache22 operation holds this repository's lock."""


def has_repository_boundary(directory_fd: int) -> bool:
    """A non-directory lock entry marks a terminal container, even if damaged."""
    try:
        marker = os.stat(LOCK_FILE_NAME, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return not stat.S_ISDIR(marker.st_mode)


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
        for depth, part in enumerate(relative.parts):
            if depth >= 3 and has_repository_boundary(directory_fd):
                ancestor = root.joinpath(*relative.parts[:depth])
                raise ValueError(
                    f"Repository path conflict: {root / relative} is inside repository {ancestor}"
                )
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
            raise ValueError(f"Unsafe archive entry: {self.paths.repository_dir / name}")
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

    def read_source(self) -> str | None:
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
            stored_host, _, stored_path = stored.partition("/")
            url_host = f"[{stored_host}]" if ":" in stored_host else stored_host
            try:
                valid = parse_repository_url(
                    f"https://{url_host}/{stored_path}.git", case_sensitive=True
                )
            except ValueError as exc:
                raise ValueError(f"Malformed source metadata: {self.paths.source_file}") from exc
            if valid.source_path != stored:
                raise ValueError(f"Malformed source metadata: {self.paths.source_file}")
        return stored

    def check_source(self, source_path: str, *, allow_unbound: bool = False) -> str | None:
        stored = self.read_source()
        if self.has_import_state():
            if stored is None and not allow_unbound:
                raise ValueError(f"Archive data has no source binding: {self.paths.repository_dir}")
            if stored is not None and stored != source_path:
                raise ValueError(
                    f"Repository source conflict: stored {stored}; requested {source_path}"
                )
        return stored

    def bind_source(self, source_path: str, *, allow_unbound: bool = False) -> None:
        stored = self.check_source(source_path, allow_unbound=allow_unbound)
        if stored != source_path:
            if not self.has_import_state():
                self.release_unused_source()
            # Keep incomplete publications outside the terminal container, so an
            # interrupted initialization can be verified and retried.
            temporary = f"../.cache22-source-init-{uuid.uuid4().hex}"
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
                    temporary,
                    self.paths.source_file.name,
                    src_dir_fd=self.directory_fd,
                    dst_dir_fd=self.directory_fd,
                )
                os.fsync(self.directory_fd)
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
        temporary = f"../.cache22-clone-init-{uuid.uuid4().hex}"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.directory_fd,
        )
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write("complete\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.link(
                temporary,
                name,
                src_dir_fd=self.directory_fd,
                dst_dir_fd=self.directory_fd,
                follow_symlinks=False,
            )
            os.fsync(self.directory_fd)
        finally:
            os.unlink(temporary, dir_fd=self.directory_fd)


@contextmanager
def repository_operation(
    root: Path,
    paths: ArchivePaths,
    *,
    create: bool = False,
    prepare: Callable[[RepositoryStorage], None] | None = None,
) -> Iterator[RepositoryStorage | None]:
    if prepare is not None and not create:
        raise ValueError("Storage preparation requires create=True")
    relative = paths.repository_dir.relative_to(root)
    with ExitStack() as stack:
        # Serialize namespace checks and marker publication, not the import itself.
        # Lock the existing root inode so no filename is reserved in the namespace.
        root_fd = os.open(root, _DIRECTORY_FLAGS)
        try:
            fcntl.flock(root_fd, fcntl.LOCK_EX)
            directory_fd = stack.enter_context(
                open_archive_directory(root, relative, create=create)
            )
            lock_fd = None
            if directory_fd is not None:
                if prepare is not None:
                    lock_fd = _prepare_owned_lock(directory_fd, paths, prepare)
                else:
                    lock_fd = _open_owned_lock(directory_fd, paths, create=create)
            if lock_fd is not None:
                stack.callback(os.close, lock_fd)
                _lock(lock_fd, paths)
        finally:
            os.close(root_fd)
        if directory_fd is None:
            yield None
            return
        if lock_fd is None:
            yield None
            return
        if os.pread(lock_fd, len(LOCK_SIGNATURE) + 1, 0) != LOCK_SIGNATURE:
            if create:
                raise ValueError(f"Unrecognized archive storage: {paths.repository_dir}")
            yield None
            return
        storage = RepositoryStorage(paths, directory_fd)
        storage.validate()
        yield storage


def _lock(fd: int, paths: ArchivePaths) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EAGAIN, errno.EACCES}:
            raise RepositoryBusyError(f"Repository is busy: {paths.repository_dir}") from exc
        raise


def _prepare_owned_lock(
    directory_fd: int, paths: ArchivePaths, prepare: Callable[[RepositoryStorage], None]
) -> int:
    storage = RepositoryStorage(paths, directory_fd)
    if storage.entry(paths.lock_file.name) is None and os.listdir(directory_fd):
        # No marker may be published until the candidate has been verified.
        # The root lock excludes competing registration and cleanup throughout.
        prepare(storage)
        return _publish_lock(directory_fd, paths)
    fd = _open_owned_lock(directory_fd, paths, create=True)
    assert fd is not None
    try:
        _lock(fd, paths)
        prepare(storage)
    except BaseException:
        os.close(fd)
        raise
    return fd


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
            raise ValueError(
                f"Repository path conflict: {paths.repository_dir} is a nonempty "
                "namespace or uninitialized directory"
            )
        return _publish_lock(directory_fd, paths)
    mode = os.fstat(fd).st_mode
    if not stat.S_ISREG(mode):
        os.close(fd)
        if create:
            if stat.S_ISDIR(mode):
                raise ValueError(
                    f"Repository path conflict: {paths.repository_dir} is a namespace directory"
                )
            raise ValueError(f"Unsafe archive lock: {paths.lock_file}")
        return None
    if os.pread(fd, len(LOCK_SIGNATURE) + 1, 0) != LOCK_SIGNATURE:
        os.close(fd)
        if create:
            raise ValueError(f"Unrecognized archive storage: {paths.repository_dir}")
        return None
    return fd


def _publish_lock(directory_fd: int, paths: ArchivePaths) -> int:
    temporary = f"../.cache22-lock-init-{uuid.uuid4().hex}"
    fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
    try:
        os.write(fd, LOCK_SIGNATURE)
        os.fsync(fd)
        _lock(fd, paths)
        os.link(
            temporary,
            paths.lock_file.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.fsync(directory_fd)
        result = fd
        fd = -1
        return result
    finally:
        if fd != -1:
            os.close(fd)
        os.unlink(temporary, dir_fd=directory_fd)
