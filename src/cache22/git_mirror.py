from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from . import operation
from .archive_layout import ArchivePaths
from .archive_storage import RepositoryStorage
from .git_config import (
    git_repository_command,
    git_repository_environment,
    validate_git_mirror_config,
)
from .git_layout import validate_git_mirror_layout
from .operation import run as run_git
from .repository_ref import parse_repository_url


def ensure_git_mirror(
    *,
    git_executable: Path,
    url: str,
    storage: RepositoryStorage,
    update: bool = False,
) -> str | None:
    paths = storage.paths
    if paths.clone_complete_marker.exists():
        if not paths.mirror_repository.exists():
            raise ValueError(
                f"Clone marker exists but mirror repository is missing: {paths.repository_dir}"
            )
        if update:
            _fetch_git_mirror(git_executable=git_executable, url=url, paths=paths)
            return f"INFO: updated Git mirror: {paths.mirror_repository}"
        return f"INFO: archive already exists: {paths.mirror_repository}"

    if paths.mirror_repository.exists():
        raise ValueError(
            f"Mirror repository exists without a completion marker: {paths.mirror_repository}. "
            f"Clear it with 'cache22 import clean repo {url}' to start over."
        )

    try:
        run_git(
            [str(git_executable), "clone", "--mirror", "--", url, str(paths.mirror_repository)],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        _clear_incomplete_mirror(storage)
        raise operation.TransportError(
            f"git clone --mirror failed with exit code {exc.returncode}"
        ) from exc

    try:
        # Git clone can turn a detached remote HEAD into a matching local branch.
        # Publish the actual advertised HEAD before declaring the clone complete.
        head = _remote_head(git_executable, paths.mirror_repository, url)
        _synchronize_head(git_executable, paths.mirror_repository, head)
        if _remote_head(git_executable, paths.mirror_repository, url) != head:
            raise ValueError("Remote HEAD changed during cloning; retry the import")
        storage.write_clone_marker()
    except OSError, RuntimeError, ValueError, subprocess.SubprocessError:
        _clear_incomplete_mirror(storage)
        raise

    return None


def _fetch_git_mirror(*, git_executable: Path, url: str, paths: ArchivePaths) -> None:
    mirror = paths.mirror_repository
    validate_git_mirror_layout(mirror)
    validate_git_mirror_config(
        git_executable, mirror, parse_repository_url(url, case_sensitive=True).source_path
    )
    try:
        head = _remote_head(git_executable, mirror, url)
    except subprocess.CalledProcessError as exc:
        raise operation.TransportError("Remote HEAD discovery failed") from exc
    except ValueError as exc:
        raise RuntimeError(
            "Remote HEAD discovery failed; the initialized mirror was kept. "
            "Retry the import to fetch updates."
        ) from exc
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
        )
    except subprocess.CalledProcessError as exc:
        raise operation.TransportError(
            f"git fetch failed with exit code {exc.returncode}; "
            "the initialized mirror was kept. Retry the import to fetch updates."
        ) from exc
    try:
        if _remote_head(git_executable, mirror, url) != head:
            raise ValueError("Remote HEAD changed during fetching")
        _synchronize_head(git_executable, mirror, head)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        raise RuntimeError(
            "Git refs were fetched, but HEAD synchronization failed; "
            "the mirror was kept. Retry the import to complete the update."
        ) from exc


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


def _remote_head(git: Path, mirror: Path, url: str) -> _RemoteHead:
    try:
        advertisement = _read_git(git, mirror, "ls-remote", "--symref", "--", url, "HEAD").stdout
    except subprocess.CalledProcessError as exc:
        raise operation.TransportError("Remote HEAD discovery failed; retry the import") from exc
    target = oid = None
    for line in advertisement.splitlines():
        value, _, name = line.partition("\t")
        if name != "HEAD":
            continue
        if value.startswith("ref: "):
            if target is not None:
                raise ValueError("Duplicate remote HEAD symref")
            target = value.removeprefix("ref: ")
            if not target.startswith("refs/"):
                raise ValueError("Invalid remote HEAD target")
            _read_git(git, mirror, "check-ref-format", target)
        else:
            if oid is not None or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
                raise ValueError("Invalid remote HEAD object ID")
            oid = value
    return _RemoteHead(target, oid)


def _synchronize_head(git: Path, mirror: Path, head: _RemoteHead) -> None:
    if head.target is not None:
        target = _read_git(
            git, mirror, "rev-parse", "--verify", "--quiet", head.target, check=False
        )
        if target.returncode != 0 and (head.oid is not None or _has_refs(git, mirror)):
            raise ValueError("Advertised HEAD target disappeared during fetching")
        if target.returncode == 0 and target.stdout.strip() != head.oid:
            raise ValueError("Fetched HEAD target does not match the advertised object ID")
        _read_git(git, mirror, "symbolic-ref", "HEAD", head.target)
    elif head.oid is not None:
        _read_git(git, mirror, "cat-file", "-e", f"{head.oid}^{{commit}}")
        _read_git(git, mirror, "update-ref", "--no-deref", "HEAD", head.oid)
    else:
        if _has_refs(git, mirror):
            raise ValueError("Nonempty remote did not advertise HEAD")
        existing = _read_git(git, mirror, "symbolic-ref", "--quiet", "HEAD", check=False)
        if existing.returncode == 1:
            _read_git(git, mirror, "symbolic-ref", "HEAD", "refs/heads/main")
        elif existing.returncode != 0:
            raise ValueError("Could not read the empty mirror's HEAD")


def _has_refs(git: Path, mirror: Path) -> bool:
    return bool(
        _read_git(git, mirror, "for-each-ref", "--count=1", "--format=%(refname)").stdout.strip()
    )


def open_fast_export(
    *,
    git_executable: Path,
    paths: ArchivePaths,
) -> tuple[subprocess.Popen[bytes], IO[bytes]]:
    process = subprocess.Popen(
        [
            str(git_executable),
            "-C",
            str(paths.mirror_repository),
            "fast-export",
            "--all",
            "--signed-tags=warn-strip",
            f"--export-marks={paths.temp_git_marks}",
        ],
        stdout=subprocess.PIPE,
    )
    if process.stdout is None:
        process.kill()
        process.wait()
        raise RuntimeError("git fast-export did not provide a stdout stream")

    return process, process.stdout


def _clear_incomplete_mirror(storage: RepositoryStorage) -> None:
    if storage.entry(storage.paths.clone_complete_marker.name) is not None:
        return
    storage.remove(storage.paths.mirror_repository.name)
