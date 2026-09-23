"""Locked repository operations and mirror/bundle dispatch, without inventory policy."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import git_bundle
from .adoption import prepare_git_import
from .archive_layout import ArchivePaths, archive_paths_for_directory, archive_paths_for_repository
from .archive_storage import RepositoryStorage, repository_operation
from .git_config import validate_git_mirror_config
from .git_layout import validate_git_mirror_layout
from .git_mirror import ensure_git_mirror
from .git_observation import mirror_snapshot, snapshot_fields
from .repository_ref import RepositoryRef, parse_repository_url
from .system_tools import find_git_executable

Format = Literal["git", "bundle"]


@dataclass(frozen=True, slots=True)
class StorageFetchResult:
    archive_path: Path
    info_messages: tuple[str, ...] = ()


def _empty_fields() -> dict[str, Any]:
    return {
        "local_head_ref": None,
        "local_head_oid": None,
        "local_head_committed_at": None,
        "local_ref_digest": None,
    }


def absent_fields(previously_ready: bool = False) -> dict[str, Any]:
    return dict(_empty_fields(), local_state="missing" if previously_ready else "absent")


@dataclass(frozen=True)
class Repository:
    """A container reserved for one operation.

    Use only inside an opening context or its preparation callback. Preparation
    may run before ownership publication and final structural validation.
    """

    root: Path
    _storage: RepositoryStorage

    @property
    def paths(self) -> ArchivePaths:
        return self._storage.paths

    @staticmethod
    def paths_for(root: Path, ref: RepositoryRef) -> ArchivePaths:
        return archive_paths_for_repository(root, ref)

    @classmethod
    @contextmanager
    def open(
        cls,
        root: Path,
        ref: RepositoryRef,
        *,
        create: bool = False,
        prepare: Callable[[Repository], None] | None = None,
    ) -> Iterator[Repository | None]:
        with cls._open(root, cls.paths_for(root, ref), create=create, prepare=prepare) as repo:
            yield repo

    @classmethod
    @contextmanager
    def open_directory(
        cls,
        root: Path,
        directory: Path,
        *,
        prepare: Callable[[Repository], None] | None = None,
    ) -> Iterator[Repository | None]:
        with cls._open(root, archive_paths_for_directory(directory), prepare=prepare) as repo:
            yield repo

    @classmethod
    @contextmanager
    def _open(
        cls,
        root: Path,
        paths: ArchivePaths,
        *,
        create: bool = False,
        prepare: Callable[[Repository], None] | None = None,
    ) -> Iterator[Repository | None]:
        repo: Repository | None = None

        def prepare_storage(storage: RepositoryStorage) -> None:
            nonlocal repo
            repo = cls(root, storage)
            assert prepare is not None
            prepare(repo)

        with repository_operation(
            root, paths, create=create, prepare=prepare_storage if prepare is not None else None
        ) as storage:
            if storage is None:
                yield None
            else:
                yield repo if repo is not None else cls(root, storage)

    @property
    def format(self) -> Format:
        return (
            "bundle" if self._storage.entry(self.paths.bundle_manifest.name) is not None else "git"
        )

    @property
    def is_bundle(self) -> bool:
        return self.format == "bundle"

    def require_selected_format(self, expected: str) -> None:
        # A published bundle can precede its inventory update after interruption.
        if expected == "bundle" and not self.is_bundle:
            raise ValueError("Selected bundle manifest is missing; preserving all archive data")

    def read_source(self) -> str | None:
        return self._storage.read_source()

    def bind_source(self, source_path: str) -> None:
        self._storage.bind_source(source_path)

    def release_unused_source(self) -> list[Path]:
        return self._storage.release_unused_source()

    @property
    def has_completion_metadata(self) -> bool:
        return (
            self._storage.entry(self.paths.clone_complete_marker.name) is not None or self.is_bundle
        )

    def origin_url(self, source_path: str) -> str:
        if self.is_bundle:
            return git_bundle.read_bundle(self._storage, source_path).source_url
        self._storage.validate_clone_marker()
        validate_git_mirror_layout(self.paths.mirror_repository)
        return validate_git_mirror_config(
            find_git_executable(), self.paths.mirror_repository, source_path
        )

    def prepare_import(self, *, source_path: str, adopt: bool) -> bool:
        if self.is_bundle:
            if self._storage.entry(self.paths.lock_file.name) is None or adopt:
                raise ValueError("Bundle adoption is not supported")
            git_bundle.read_bundle(self._storage, source_path)
            return False
        return prepare_git_import(
            self._storage, archive_dir=self.root, source_path=source_path, adopt=adopt
        )

    def fetch(self, url: str, *, publish: Callable[[], None]) -> StorageFetchResult:
        if self.is_bundle:
            source_path = parse_repository_url(url, case_sensitive=True).source_path
            return StorageFetchResult(
                git_bundle.materialize(self._storage, source_path, update_url=url, publish=publish)
            )
        message = ensure_git_mirror(
            git_executable=find_git_executable(), url=url, storage=self._storage
        )
        return StorageFetchResult(
            self.paths.mirror_repository, (message,) if message is not None else ()
        )

    def convert_to_bundle(self, source_path: str, *, publish: Callable[[], None]) -> Path:
        return git_bundle.materialize(self._storage, source_path, publish=publish)

    def leftover_paths(self, observed: Mapping[str, Any]) -> list[Path]:
        paths = self.paths
        leftovers = [
            paths.repository_dir / name
            for name in self._storage.bundle_generations()
            if name != observed.get("archive_file")
        ]
        if self._storage.entry(paths.bundle_staging.name) is not None:
            leftovers.append(paths.bundle_staging)
        if (
            observed.get("storage_format") == "bundle"
            and self._storage.entry(paths.mirror_repository.name) is not None
        ):
            leftovers.append(paths.mirror_repository)
        return leftovers

    def observe_local(
        self, source_path: str, *, previously_ready: bool = False, expected_format: str = "git"
    ) -> dict[str, Any]:
        storage = self._storage
        empty = _empty_fields()
        paths = storage.paths
        if self.is_bundle:
            try:
                return git_bundle.bundle_fields(storage, source_path)
            except ValueError, OSError, subprocess.SubprocessError:
                return dict(empty, local_state="incomplete", storage_format="bundle")
        if expected_format == "bundle":
            return dict(empty, local_state="incomplete")
        mirror_exists = storage.entry(paths.mirror_repository.name) is not None
        marker_exists = storage.entry(paths.clone_complete_marker.name) is not None
        if not mirror_exists and not marker_exists:
            if storage.bundle_generations() or storage.entry(paths.bundle_staging.name) is not None:
                return dict(empty, local_state="incomplete")
            return dict(empty, local_state="missing" if previously_ready else "absent")
        if not mirror_exists or not marker_exists:
            return dict(empty, local_state="incomplete")
        try:
            storage.validate_clone_marker()
            storage.check_source(source_path)
            validate_git_mirror_layout(paths.mirror_repository)
            git = find_git_executable()
            validate_git_mirror_config(git, paths.mirror_repository, source_path)
        except ValueError:
            return dict(empty, local_state="incomplete")

        snapshot, date, _ = mirror_snapshot(paths.mirror_repository)
        return dict(
            snapshot_fields(snapshot, date),
            storage_format="git",
            archive_file=paths.mirror_repository.name,
        )

    def prepare_adoption(self, expected_source_path: str | None = None) -> bool:
        """Validate a discovered mirror before publishing ownership metadata."""
        storage = self._storage
        paths = storage.paths
        storage.validate()
        if self.is_bundle:
            return False
        storage.validate_clone_marker()
        if storage.entry(paths.mirror_repository.name) is None:
            return False
        source = storage.read_source()
        if (
            storage.entry(paths.lock_file.name) is not None
            and source is not None
            and storage.entry(paths.clone_complete_marker.name) is not None
        ):
            return False
        validate_git_mirror_layout(paths.mirror_repository)
        origin = validate_git_mirror_config(find_git_executable(), paths.mirror_repository)
        ref = parse_repository_url(origin, case_sensitive=True)
        if self.paths_for(self.root, ref) != paths:
            raise ValueError("Mirror is not at its canonical path")
        if expected_source_path is not None and expected_source_path != ref.source_path:
            raise ValueError("Repository source conflict with inventory")
        return self.prepare_import(source_path=ref.source_path, adopt=True)

    def clean(self) -> list[Path]:
        storage = self._storage
        paths = self.paths
        removed_paths: list[Path] = []
        bundled = self.is_bundle
        if (
            bundled
            or storage.bundle_generations()
            or storage.entry(paths.bundle_staging.name) is not None
        ):
            source = storage.read_source()
            if source is None:
                raise ValueError("Bundle storage has no source binding; preserving it")
            removed_paths.extend(git_bundle.cleanup(storage, source))

        mirror_exists = storage.entry(paths.mirror_repository.name) is not None
        marker_exists = storage.entry(paths.clone_complete_marker.name) is not None
        if mirror_exists and not marker_exists:
            storage.remove(paths.mirror_repository.name)
            removed_paths.append(paths.mirror_repository)
        if marker_exists and not mirror_exists and not bundled:
            storage.remove(paths.clone_complete_marker.name)
            removed_paths.append(paths.clone_complete_marker)

        removed_paths.extend(storage.release_unused_source())
        return removed_paths
