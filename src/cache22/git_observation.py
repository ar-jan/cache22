"""Comparable local/remote ref snapshots. Remote checks never fetch objects."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import operation
from .git_config import (
    git_local_environment,
    git_repository_command,
    git_repository_environment,
)
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
        raise operation.TransportError(
            operation.failure_message(f"Remote check failed (Git exit {exc.returncode})", exc, url)
        ) from exc
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


def mirror_snapshot(mirror: Path) -> tuple[RefSnapshot, int | None, str]:
    git = find_git_executable()

    def read(*args: str, check: bool = True) -> subprocess.CompletedProcess[Any]:
        env = git_local_environment()
        return operation.run(
            git_repository_command(git, mirror, *args),
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
    return snapshot, date, read("rev-parse", "--show-object-format").stdout.strip()


def snapshot_fields(snapshot: RefSnapshot, date: int | None) -> dict[str, Any]:
    return {
        "local_state": "ready",
        "local_head_ref": snapshot.head_ref,
        "local_head_oid": snapshot.head_oid,
        "local_head_committed_at": date,
        "local_ref_digest": snapshot.digest,
    }


def remote_fields(snapshot: RefSnapshot) -> dict[str, Any]:
    return {
        "remote_head_ref": snapshot.head_ref,
        "remote_head_oid": snapshot.head_oid,
        "remote_ref_digest": snapshot.digest,
    }
