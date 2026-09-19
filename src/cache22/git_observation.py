"""Comparable local/remote ref snapshots. Remote checks never fetch objects."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any

from . import operation
from .archive_storage import RepositoryStorage
from .git_config import (
    git_repository_command,
    git_repository_environment,
    validate_git_mirror_config,
)
from .git_layout import validate_git_mirror_layout
from .system_tools import find_git_executable

OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


@dataclass(frozen=True)
class RefSnapshot:
    refs: dict[str, str]
    head_ref: str | None
    head_oid: str | None

    @property
    def digest(self) -> str:
        # An empty remote need not advertise its unborn HEAD target.
        payload = [
            sorted(self.refs.items()),
            self.head_ref if self.head_oid else None,
            self.head_oid,
        ]
        return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def remote_snapshot(url: str) -> RefSnapshot:
    try:
        result = operation.run(
            [
                str(find_git_executable()),
                "-c",
                "core.hooksPath=/dev/null",
                "ls-remote",
                "--symref",
                "--",
                url,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=git_repository_environment(),
        )
    except subprocess.CalledProcessError as exc:
        raise operation.TransportError(f"Remote check failed (Git exit {exc.returncode})") from exc
    refs: dict[str, str] = {}
    target = head = None
    for line in result.stdout.splitlines():
        value, separator, name = line.partition("\t")
        if not separator:
            raise ValueError("Malformed remote ref advertisement")
        if value.startswith("ref: "):
            if name == "HEAD":
                if target is not None:
                    raise ValueError("Duplicate remote HEAD target")
                target = value[5:]
                if not target.startswith("refs/"):
                    raise ValueError("Invalid remote HEAD target")
            continue
        if not OID.fullmatch(value):
            raise ValueError("Invalid remote object ID")
        if name == "HEAD":
            if head is not None:
                raise ValueError("Duplicate remote HEAD")
            head = value
        elif name.startswith("refs/") and not name.endswith("^{}"):
            if name in refs:
                raise ValueError("Duplicate remote ref")
            refs[name] = value
    if refs and head is None:
        raise ValueError("Nonempty remote did not advertise HEAD")
    return RefSnapshot(refs, target, head)


def local_fields(
    storage: RepositoryStorage | None, source_path: str, *, previously_ready: bool = False
) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "local_head_ref": None,
        "local_head_oid": None,
        "local_head_committed_at": None,
        "local_ref_digest": None,
    }
    if storage is None:
        return dict(empty, local_state="missing" if previously_ready else "absent")
    paths = storage.paths
    mirror_exists = storage.entry(paths.mirror_repository.name) is not None
    marker_exists = storage.entry(paths.clone_complete_marker.name) is not None
    if not mirror_exists and not marker_exists:
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

    def read(*args: str, check: bool = True) -> subprocess.CompletedProcess[Any]:
        env = git_repository_environment()
        env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1")
        return operation.run(
            git_repository_command(git, paths.mirror_repository, *args),
            check=check,
            capture_output=True,
            text=True,
            env=env,
        )

    refs = dict(
        line.split("\t", 1)
        for line in read("for-each-ref", "--format=%(refname)%09%(objectname)").stdout.splitlines()
    )
    symbolic = read("symbolic-ref", "--quiet", "HEAD", check=False)
    if symbolic.returncode not in (0, 1):
        symbolic.check_returncode()
    target = symbolic.stdout.strip() if symbolic.returncode == 0 else None
    resolved = read("rev-parse", "--verify", "--quiet", "HEAD", check=False)
    if resolved.returncode not in (0, 1):
        resolved.check_returncode()
    oid = resolved.stdout.strip() if resolved.returncode == 0 else None
    date = int(read("show", "-s", "--format=%ct", oid, "--").stdout.strip()) if oid else None
    snapshot = RefSnapshot(refs, target, oid)
    return {
        "local_state": "ready",
        "local_head_ref": target,
        "local_head_oid": oid,
        "local_head_committed_at": date,
        "local_ref_digest": snapshot.digest,
    }


def remote_fields(snapshot: RefSnapshot) -> dict[str, Any]:
    return {
        "remote_head_ref": snapshot.head_ref,
        "remote_head_oid": snapshot.head_oid,
        "remote_ref_digest": snapshot.digest,
    }
