"""Standalone bundle generations, offline verification, and durable publication."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from . import operation
from .archive_storage import RepositoryStorage
from .git_config import (
    git_local_environment,
    git_repository_command,
    validate_git_mirror_config,
)
from .git_layout import validate_git_mirror_layout
from .git_mirror import fetch_git_repository
from .git_observation import RefSnapshot, mirror_snapshot, snapshot_fields
from .repository_ref import parse_repository_url
from .system_tools import find_git_executable


@dataclass(frozen=True)
class BundleState:
    path: Path
    snapshot: RefSnapshot
    committed_at: int | None
    object_format: str
    source_url: str


def _regular(storage: RepositoryStorage, name: str) -> int:
    entry = storage.entry(name)
    if entry is None or not stat.S_ISREG(entry.st_mode):
        raise ValueError(f"Missing or unsafe bundle file: {name}")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=storage.directory_fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"Unsafe bundle file: {name}")
    return fd


def _ref(name: str) -> bool:
    return (
        name.startswith("refs/")
        and not any(ord(c) < 33 or ord(c) == 127 or c in "~^:?*[\\" for c in name)
        and ".." not in name
        and "@{" not in name
        and not name.endswith(".")
        and all(
            part and not part.startswith(".") and not part.endswith(".lock")
            for part in name.split("/")
        )
    )


def _header(handle: BinaryIO, object_format: str) -> dict[str, str]:
    oid_pattern = re.compile(r"[0-9a-f]{" + ("40" if object_format == "sha1" else "64") + r"}\Z")
    refs: dict[str, str] = {}
    if handle.readline(65537) != b"# v3 git bundle\n":
        raise ValueError("Expected a version-3 bundle")
    if handle.readline(65537) != f"@object-format={object_format}\n".encode():
        raise ValueError("Bundle object format does not match its manifest")
    while True:
        if len(refs) % 256 == 0:
            operation.guard()
        line = handle.readline(65537)
        if line == b"\n":
            break
        if not line or len(line) > 65536:
            raise ValueError("Malformed bundle header")
        oid, separator, ref = line.decode("utf-8").rstrip("\n").partition(" ")
        if (
            not separator
            or not oid_pattern.fullmatch(oid)
            or (ref != "HEAD" and not _ref(ref))
            or ref in refs
        ):
            raise ValueError("Invalid bundle refs, prerequisites, or capabilities")
        refs[ref] = oid
    if handle.read(4) != b"PACK":
        raise ValueError("Bundle object pack is missing")
    return refs


def read_bundle(storage: RepositoryStorage, source_path: str) -> BundleState:
    storage.check_source(source_path)
    with os.fdopen(_regular(storage, storage.paths.bundle_manifest.name), "r") as handle:
        raw = handle.read(65537)
    if len(raw) > 65536:
        raise ValueError("Bundle manifest is too large")
    data = json.loads(raw)
    fields = {
        "version",
        "storage_format",
        "bundle_file",
        "bundle_size",
        "source_url",
        "object_format",
        "head_ref",
        "head_oid",
        "head_committed_at",
        "ref_digest",
    }
    if not isinstance(data, dict) or set(data) != fields:
        raise ValueError("Malformed bundle manifest")
    if (
        type(data["version"]) is not int
        or data["version"] != 1
        or data["storage_format"] != "bundle"
    ):
        raise ValueError("Unsupported bundle manifest version or storage format")
    name = data["bundle_file"]
    if not isinstance(name, str) or name not in storage.bundle_generations():
        raise ValueError("Invalid or missing bundle generation")
    if data["object_format"] not in ("sha1", "sha256"):
        raise ValueError("Unsupported bundle object format")
    if not isinstance(data["source_url"], str) or not data["source_url"]:
        raise ValueError("Invalid bundle origin")
    if parse_repository_url(data["source_url"], case_sensitive=True).source_path != source_path:
        raise ValueError("Bundle source conflicts with repository binding")
    oid_pattern = re.compile(
        r"[0-9a-f]{" + ("40" if data["object_format"] == "sha1" else "64") + r"}\Z"
    )
    head_ref, head_oid, date = data["head_ref"], data["head_oid"], data["head_committed_at"]
    if head_ref is not None and (not isinstance(head_ref, str) or not _ref(head_ref)):
        raise ValueError("Invalid bundle HEAD target")
    if head_oid is not None and (
        not isinstance(head_oid, str) or not oid_pattern.fullmatch(head_oid)
    ):
        raise ValueError("Invalid bundle HEAD object ID")
    if (head_oid is None and date is not None) or (head_oid is not None and type(date) is not int):
        raise ValueError("Invalid bundle HEAD commit date")
    refs: dict[str, str] = {}
    with os.fdopen(_regular(storage, name), "rb") as handle:
        if (
            type(data["bundle_size"]) is not int
            or os.fstat(handle.fileno()).st_size != data["bundle_size"]
        ):
            raise ValueError("Bundle file size does not match its manifest")
        refs = _header(handle, data["object_format"])
    if refs.pop("HEAD", None) != head_oid:
        raise ValueError("Bundle HEAD does not match its manifest")
    if head_ref is not None and refs.get(head_ref) != head_oid:
        raise ValueError("Bundle symbolic HEAD does not match its target")
    if not refs and head_oid is None:
        raise ValueError("Empty bundles are not supported")
    snapshot = RefSnapshot(refs, head_ref, head_oid)
    if snapshot.digest != data["ref_digest"]:
        raise ValueError("Bundle ref snapshot does not match its manifest")
    return BundleState(
        storage.paths.repository_dir / name,
        snapshot,
        date,
        data["object_format"],
        data["source_url"],
    )


def bundle_fields(storage: RepositoryStorage, source_path: str) -> dict[str, Any]:
    state = read_bundle(storage, source_path)
    return dict(
        snapshot_fields(state.snapshot, state.committed_at),
        storage_format="bundle",
        archive_file=state.path.name,
    )


def _git(mirror: Path, *args: str, input: str | None = None) -> str:
    return operation.run(
        git_repository_command(find_git_executable(), mirror, *args),
        input=input,
        check=True,
        capture_output=True,
        text=True,
        env=git_local_environment(),
    ).stdout.strip()


def restore(state: BundleState, destination: Path) -> None:
    # Reject prerequisites and filters before any candidate can become active.
    fd = os.open(state.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("Unsafe bundle file")
        refs = _header(handle, state.object_format)
    if refs.pop("HEAD", None) != state.snapshot.head_oid or refs != state.snapshot.refs:
        raise ValueError("Bundle header does not match the expected snapshot")
    operation.progress("bundle restoration")
    operation.run(
        [
            str(find_git_executable()),
            "-c",
            "core.hooksPath=/dev/null",
            "init",
            "--bare",
            "--template=",
            f"--object-format={state.object_format}",
            str(destination),
        ],
        check=True,
        capture_output=True,
        env=git_local_environment(),
    )
    _git(destination, "bundle", "unbundle", str(state.path))
    commands = (
        "start\n"
        + "".join(f"create {ref} {oid}\n" for ref, oid in state.snapshot.refs.items())
        + "prepare\ncommit\n"
    )
    _git(destination, "update-ref", "--stdin", input=commands)
    if state.snapshot.head_ref is not None:
        _git(destination, "symbolic-ref", "HEAD", state.snapshot.head_ref)
    elif state.snapshot.head_oid is not None:
        _git(destination, "update-ref", "--no-deref", "HEAD", state.snapshot.head_oid)
    _git(destination, "config", "remote.origin.url", state.source_url)
    _git(destination, "config", "remote.origin.fetch", "+refs/*:refs/*")
    _git(destination, "config", "remote.origin.mirror", "true")
    operation.progress("bundle verification")
    _git(destination, "fsck", "--full")
    if mirror_snapshot(destination) != (state.snapshot, state.committed_at, state.object_format):
        raise ValueError("Restored bundle does not match the expected repository snapshot")


def _safe_staging(storage: RepositoryStorage) -> None:
    path = storage.paths.bundle_staging
    if storage.entry(path.name) is None:
        return
    for directory, children, files in os.walk(path, followlinks=False):
        for name in (*children, *files):
            if not (
                stat.S_ISREG((Path(directory) / name).lstat().st_mode)
                or stat.S_ISDIR((Path(directory) / name).lstat().st_mode)
            ):
                raise ValueError("Unsafe bundle staging entry")


def clear_staging(storage: RepositoryStorage) -> None:
    _safe_staging(storage)
    storage.remove(storage.paths.bundle_staging.name)


def retire(storage: RepositoryStorage, state: BundleState) -> list[Path]:
    """Caller must have independently restored and verified the selected generation."""
    removed: list[Path] = []
    names = [name for name in storage.bundle_generations() if name != state.path.name]
    if storage.entry(storage.paths.mirror_repository.name) is not None:
        validate_git_mirror_layout(storage.paths.mirror_repository)
        names.append(storage.paths.mirror_repository.name)
    names.append(storage.paths.clone_complete_marker.name)
    for name in names:
        operation.guard()
        entry = storage.entry(name)
        if name.endswith(".bundle") and entry is not None and not stat.S_ISREG(entry.st_mode):
            raise ValueError("Unsafe retired bundle")
        if storage.remove(name):
            removed.append(storage.paths.repository_dir / name)
    os.fsync(storage.directory_fd)
    return removed


def cleanup(storage: RepositoryStorage, source_path: str) -> list[Path]:
    """Remove only unpublished or independently verified retired bundle storage."""
    removed: list[Path] = []
    if storage.entry(storage.paths.bundle_manifest.name) is not None:
        state = read_bundle(storage, source_path)
        retired = (
            len(storage.bundle_generations()) > 1
            or storage.entry(storage.paths.mirror_repository.name) is not None
            or storage.entry(storage.paths.clone_complete_marker.name) is not None
        )
        if retired:
            clear_staging(storage)
            storage.paths.bundle_staging.mkdir(mode=0o700)
            restore(state, storage.paths.bundle_staging / "verify.git")
            removed.extend(retire(storage, state))
    elif storage.bundle_generations():
        storage.validate_clone_marker()
        if storage.entry(storage.paths.clone_complete_marker.name) is None:
            raise ValueError(
                "Unpublished bundles have no complete source mirror; preserving storage"
            )
        validate_git_mirror_layout(storage.paths.mirror_repository)
        for name in storage.bundle_generations():
            with os.fdopen(_regular(storage, name), "rb"):
                pass
            storage.remove(name)
            removed.append(storage.paths.repository_dir / name)
    if storage.entry(storage.paths.bundle_staging.name) is not None:
        clear_staging(storage)
        removed.append(storage.paths.bundle_staging)
    return removed


def materialize(
    storage: RepositoryStorage,
    source_path: str,
    *,
    update_url: str | None = None,
    publish: Callable[[], None] = lambda: None,
) -> Path:
    """Convert a mirror, or fetch and replace an existing standalone bundle."""
    paths = storage.paths
    bundled = storage.entry(paths.bundle_manifest.name) is not None
    old = read_bundle(storage, source_path) if bundled else None
    if old is not None and update_url is None:
        # Validate even an idempotent retry before deleting any source copy.
        clear_staging(storage)
        paths.bundle_staging.mkdir(mode=0o700)
        restore(old, paths.bundle_staging / "verify.git")
        publish()
        retire(storage, old)
        clear_staging(storage)
        return old.path
    if old is None:
        storage.validate_clone_marker()
        if storage.entry(paths.clone_complete_marker.name) is None:
            raise ValueError("Conversion requires a complete managed mirror")
        storage.check_source(source_path)
        validate_git_mirror_layout(paths.mirror_repository)
        source_url = validate_git_mirror_config(
            find_git_executable(), paths.mirror_repository, source_path
        )
    else:
        source_url = update_url or old.source_url
    clear_staging(storage)
    paths.bundle_staging.mkdir(mode=0o700)
    mirror = paths.mirror_repository
    if old is not None:
        mirror = paths.bundle_staging / "work.git"
        restore(old, mirror)
        assert update_url is not None
        fetch_git_repository(git_executable=find_git_executable(), url=update_url, mirror=mirror)
    snapshot, date, object_format = mirror_snapshot(mirror)
    if not snapshot.refs and snapshot.head_oid is None:
        raise ValueError(
            "Cannot publish an empty repository as a bundle; previous archive retained"
        )
    operation.progress("bundle creation")
    staged = paths.bundle_staging / "next.bundle"
    _git(mirror, "bundle", "create", "--version=3", str(staged), "--all")
    expected = BundleState(staged, snapshot, date, object_format, source_url)
    restore(expected, paths.bundle_staging / "verify.git")
    operation.progress("bundle publication")
    name = f"{paths.repository_dir.name}.{uuid.uuid4().hex}.bundle"
    with staged.open("rb") as handle:
        os.fsync(handle.fileno())
        size = os.fstat(handle.fileno()).st_size
    operation.guard()
    published = paths.repository_dir / name
    os.rename(staged, published)
    os.fsync(storage.directory_fd)
    manifest = {
        "version": 1,
        "storage_format": "bundle",
        "bundle_file": name,
        "bundle_size": size,
        "source_url": source_url,
        "object_format": object_format,
        "head_ref": snapshot.head_ref,
        "head_oid": snapshot.head_oid,
        "head_committed_at": date,
        "ref_digest": snapshot.digest,
    }
    staged_manifest = paths.bundle_staging / "manifest.json"
    fd = os.open(staged_manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(manifest, handle)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    operation.guard()
    os.replace(staged_manifest, paths.bundle_manifest)
    os.fsync(storage.directory_fd)
    state = read_bundle(storage, source_path)
    publish()
    operation.progress("bundle cleanup")
    retire(storage, state)
    clear_staging(storage)
    return published
