from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import operation
from .archive_layout import ArchivePaths
from .archive_storage import RepositoryStorage
from .diagnostics import failure_message
from .errors import StructuralError, TransportError, storage_error
from .git_config import (
    git_repository_command,
    git_repository_environment,
    validate_git_mirror_config,
)
from .git_layout import validate_git_mirror_layout
from .operation import run as run_git
from .remote_transport import with_transport_fallback
from .repository_ref import parse_repository_url


def ensure_git_mirror(
    *,
    git_executable: Path,
    url: str,
    storage: RepositoryStorage,
) -> Literal["created", "updated"]:
    paths = storage.paths
    if paths.clone_complete_marker.exists():
        if not paths.mirror_repository.exists():
            raise StructuralError(
                f"Clone marker exists but mirror repository is missing: {paths.repository_dir}"
            )
        _fetch_git_mirror(git_executable=git_executable, url=url, paths=paths)
        return "updated"

    if paths.mirror_repository.exists():
        raise StructuralError(
            f"Mirror repository exists without a completion marker: {paths.mirror_repository}. "
            f"Clear it with 'cache22 clean {url}' to start over."
        )

    operation.progress("cloning")

    def clone(candidate: str) -> None:
        try:
            run_git(
                [
                    str(git_executable),
                    "clone",
                    "--mirror",
                    "--progress",
                    "--",
                    candidate,
                    str(paths.mirror_repository),
                ],
                check=True,
                observe_progress=True,
            )
        except subprocess.CalledProcessError:
            _clear_incomplete_mirror(storage)
            raise

    url, _ = with_transport_fallback(
        url, clone, lambda exc: f"git clone --mirror failed with exit code {exc.returncode}"
    )

    try:
        # Git clone can turn a detached remote HEAD into a matching local branch.
        # Publish the actual advertised HEAD before declaring the clone complete.
        head = _checked_remote_head(git_executable, paths.mirror_repository, url)
        operation.progress("HEAD synchronization")
        _synchronize_head(git_executable, paths.mirror_repository, head)
        if _checked_remote_head(git_executable, paths.mirror_repository, url) != head:
            raise StructuralError("Remote HEAD changed during cloning; retry the fetch")
        storage.write_clone_marker()
    except OSError, RuntimeError, ValueError, subprocess.SubprocessError:
        _clear_incomplete_mirror(storage)
        raise

    return "created"


def _fetch_git_mirror(*, git_executable: Path, url: str, paths: ArchivePaths) -> None:
    fetch_git_repository(git_executable=git_executable, url=url, mirror=paths.mirror_repository)


def fetch_git_repository(*, git_executable: Path, url: str, mirror: Path) -> None:
    validate_git_mirror_layout(mirror)
    validate_git_mirror_config(
        git_executable, mirror, parse_repository_url(url, case_sensitive=True).source_path
    )
    try:
        # The URL that answers discovery serves the fetch and its verification.
        url, head = with_transport_fallback(
            url,
            lambda candidate: _remote_head(git_executable, mirror, candidate),
            lambda exc: "Remote HEAD discovery failed",
        )
    except StructuralError as exc:
        raise StructuralError(
            "Remote HEAD discovery failed; the initialized mirror was kept. "
            "Retry the fetch to fetch updates."
        ) from exc
    operation.progress("fetching")
    refspecs = ["+refs/*:refs/*"]
    if head.target is None and head.oid is not None:
        # A detached HEAD can name a commit unreachable from every advertised ref.
        refspecs.append("HEAD")
    try:
        run_git(
            git_repository_command(
                git_executable,
                mirror,
                "fetch",
                "--progress",
                "--atomic",
                "--prune",
                "--no-recurse-submodules",
                "--no-write-fetch-head",
                "--no-auto-maintenance",
                "--refmap=",
                "--",
                url,
                *refspecs,
            ),
            check=True,
            env=git_repository_environment(),
            observe_progress=True,
        )
    except subprocess.CalledProcessError as exc:
        raise TransportError(
            failure_message(
                f"git fetch failed with exit code {exc.returncode}; "
                "the initialized mirror was kept. Retry the fetch to fetch updates.",
                exc,
                url,
            )
        ) from exc
    try:
        if _checked_remote_head(git_executable, mirror, url) != head:
            raise StructuralError("Remote HEAD changed during fetching")
        operation.progress("HEAD synchronization")
        _synchronize_head(git_executable, mirror, head)
    except (OSError, subprocess.CalledProcessError, StructuralError) as exc:
        message = (
            "Git refs were fetched, but HEAD synchronization failed; "
            "the mirror was kept. Retry the fetch to complete the update."
        )
        if isinstance(exc, OSError):
            raise storage_error(exc, message) from exc
        raise StructuralError(message) from exc


@dataclass(frozen=True)
class _RemoteHead:
    target: str | None
    oid: str | None


def _read_git(
    git: Path, mirror: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return run_git(
        git_repository_command(git, mirror, *args),
        check=check,
        capture_output=True,
        text=True,
        env=git_repository_environment(),
    )


def _checked_remote_head(git: Path, mirror: Path, url: str) -> _RemoteHead:
    try:
        return _remote_head(git, mirror, url)
    except subprocess.CalledProcessError as exc:
        raise TransportError(
            failure_message("Remote HEAD discovery failed; retry the fetch", exc, url)
        ) from exc


def _remote_head(git: Path, mirror: Path, url: str) -> _RemoteHead:
    """Raise ``CalledProcessError`` only when the remote cannot be reached."""
    operation.progress("remote observation")
    advertisement = _read_git(git, mirror, "ls-remote", "--symref", "--", url, "HEAD").stdout
    target = oid = None
    for line in advertisement.splitlines():
        value, _, name = line.partition("\t")
        if name != "HEAD":
            continue
        if value.startswith("ref: "):
            if target is not None:
                raise StructuralError("Duplicate remote HEAD symref")
            target = value.removeprefix("ref: ")
            if not target.startswith("refs/"):
                raise StructuralError("Invalid remote HEAD target")
            if _read_git(git, mirror, "check-ref-format", target, check=False).returncode:
                raise StructuralError("Invalid remote HEAD target")
        else:
            if oid is not None or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
                raise StructuralError("Invalid remote HEAD object ID")
            oid = value
    return _RemoteHead(target, oid)


def _synchronize_head(git: Path, mirror: Path, head: _RemoteHead) -> None:
    if head.target is not None:
        target = _read_git(
            git, mirror, "rev-parse", "--verify", "--quiet", head.target, check=False
        )
        if target.returncode != 0 and (head.oid is not None or _has_refs(git, mirror)):
            raise StructuralError("Advertised HEAD target disappeared during fetching")
        if target.returncode == 0 and target.stdout.strip() != head.oid:
            raise StructuralError("Fetched HEAD target does not match the advertised object ID")
        _read_git(git, mirror, "symbolic-ref", "HEAD", head.target)
    elif head.oid is not None:
        _read_git(git, mirror, "cat-file", "-e", f"{head.oid}^{{commit}}")
        _read_git(git, mirror, "update-ref", "--no-deref", "HEAD", head.oid)
    else:
        if _has_refs(git, mirror):
            raise StructuralError("Nonempty remote did not advertise HEAD")
        existing = _read_git(git, mirror, "symbolic-ref", "--quiet", "HEAD", check=False)
        if existing.returncode == 1:
            _read_git(git, mirror, "symbolic-ref", "HEAD", "refs/heads/main")
        elif existing.returncode != 0:
            raise StructuralError("Could not read the empty mirror's HEAD")


def _has_refs(git: Path, mirror: Path) -> bool:
    return bool(
        _read_git(git, mirror, "for-each-ref", "--count=1", "--format=%(refname)").stdout.strip()
    )


def _clear_incomplete_mirror(storage: RepositoryStorage) -> None:
    if storage.entry(storage.paths.clone_complete_marker.name) is not None:
        return
    storage.remove(storage.paths.mirror_repository.name)
