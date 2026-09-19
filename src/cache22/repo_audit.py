"""Explicit inventory discovery and reconciliation; never adopts or deletes archives."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from .archive_layout import archive_paths_for_directory, archive_paths_for_repository
from .archive_storage import (
    RepositoryStorage,
    has_repository_boundary,
    open_archive_directory,
    repository_operation,
)
from .config import list_archive_dirs
from .git_config import validate_git_mirror_config
from .git_layout import validate_git_mirror_layout
from .git_observation import local_fields
from .index import Index, repository_key
from .repository_ref import parse_repository_url, validate_storage_component
from .system_tools import find_git_executable


def register_storage(index: Index, root: Path, storage: RepositoryStorage) -> dict[str, Any]:
    source = storage.read_source()
    if source is None:
        raise ValueError("Managed mirror has no source binding")
    storage.validate_clone_marker()
    validate_git_mirror_layout(storage.paths.mirror_repository)
    origin = validate_git_mirror_config(
        find_git_executable(), storage.paths.mirror_repository, source
    )
    ref = parse_repository_url(origin, case_sensitive=True)
    if archive_paths_for_repository(root, ref) != storage.paths:
        raise ValueError("Mirror is not at its canonical path")
    return index.add(ref, root)


def audit(*, index: Index | None = None, fix: bool = False) -> list[dict[str, Any]]:
    index = index or Index()
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    with index.connect() as db:
        records = {r["repo_key"]: index._record(r) for r in db.execute("SELECT * FROM inventory")}
    roots = list(
        dict.fromkeys([*list_archive_dirs(), *(Path(r["archive_root"]) for r in records.values())])
    )
    for root in roots:
        if not root.is_dir():
            issues.append(
                {"path": str(root), "problem": "Archive root unavailable", "fixed": False}
            )
            continue
        pending = [Path()]
        while pending:
            relative = pending.pop()
            path = root / relative
            try:
                with open_archive_directory(root, relative) as fd:
                    if fd is None:
                        continue
                    with os.scandir(fd) as entries:
                        children = sorted(
                            e.name for e in entries if e.is_dir(follow_symlinks=False)
                        )
                    boundary = len(relative.parts) >= 3 and has_repository_boundary(fd)
                    unowned = (
                        len(relative.parts) >= 3
                        and f"{relative.name}.git" in children
                        and not boundary
                    )
                if unowned:
                    issues.append(
                        {
                            "path": str(path),
                            "problem": "Unowned mirror; explicit adoption required",
                            "fixed": False,
                        }
                    )
                    continue
                if not boundary:
                    for child in reversed(children):
                        try:
                            validate_storage_component(child)
                        except ValueError:
                            continue
                        pending.append(relative / child)
                    continue
                paths = archive_paths_for_directory(path)
                with repository_operation(root, paths) as storage:
                    if storage is None:
                        raise ValueError("Unrecognized ownership marker")
                    key = relative.as_posix()
                    record = records.get(key)
                    if record is not None and record["archive_root"] != str(root):
                        raise ValueError(
                            f"Duplicate managed copy; selected root is {record['archive_root']}"
                        )
                    if record is None:
                        # Validate before adding; no index-only audit side effects.
                        source = storage.read_source()
                        if source is None:
                            raise ValueError("Managed storage has no source binding")
                        storage.validate_clone_marker()
                        validate_git_mirror_layout(paths.mirror_repository)
                        origin = validate_git_mirror_config(
                            find_git_executable(), paths.mirror_repository, source
                        )
                        ref = parse_repository_url(origin, case_sensitive=True)
                        if repository_key(ref) != key:
                            raise ValueError("Mirror is not at its canonical path")
                        observed = local_fields(storage, source)
                        if observed["local_state"] != "ready":
                            raise ValueError("Mirror cannot be indexed as ready")
                        if not fix:
                            issues.append(
                                {
                                    "path": str(path),
                                    "problem": "Managed mirror is not indexed",
                                    "fixed": False,
                                }
                            )
                            continue
                        record = index.add(ref, root)
                        records[key] = record
                        issues.append(
                            {
                                "path": str(path),
                                "problem": "Managed mirror was not indexed",
                                "fixed": True,
                            }
                        )
                    seen.add(key)
                    fields = local_fields(
                        storage,
                        record["source_path"],
                        previously_ready=record["local_state"] in {"ready", "missing"},
                    )
                    changed = (
                        any(record[k] != v for k, v in fields.items())
                        or record["reconciliation_required"]
                    )
                    if fields["local_state"] == "incomplete":
                        issues.append(
                            {
                                "path": str(path),
                                "problem": "Incomplete or invalid mirror metadata",
                                "fixed": False,
                            }
                        )
                    elif changed:
                        issues.append(
                            {
                                "path": str(path),
                                "problem": "Local inventory differs from disk",
                                "fixed": fix,
                            }
                        )
                    if storage.entry(paths.temp_dir.name) is not None:
                        issues.append(
                            {
                                "path": str(paths.temp_dir),
                                "problem": "Interrupted import staging remains",
                                "fixed": False,
                            }
                        )
                    if fix:
                        index.update(
                            record["id"],
                            **fields,
                            local_observed_at=index.now(),
                            reconciliation_required=False,
                        )
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                issues.append({"path": str(path), "problem": str(exc), "fixed": False})
    for key, record in records.items():
        if key in seen or not Path(record["archive_root"]).is_dir():
            continue
        # Only observe expected paths here; an absent marker is not permission to adopt.
        try:
            root = Path(record["archive_root"])
            ref = parse_repository_url(record["source_url"], case_sensitive=True)
            paths = archive_paths_for_repository(root, ref)
            with repository_operation(root, paths) as storage:
                if (
                    storage is None
                    and paths.repository_dir.exists()
                    and any(paths.repository_dir.iterdir())
                ):
                    raise ValueError("Expected repository has unowned or invalid storage")
                fields = local_fields(
                    storage,
                    record["source_path"],
                    previously_ready=record["local_state"] in {"ready", "missing"},
                )
                if (
                    any(record[k] != v for k, v in fields.items())
                    or record["reconciliation_required"]
                ):
                    issues.append(
                        {
                            "path": str(paths.repository_dir),
                            "problem": "Local inventory differs from disk",
                            "fixed": fix,
                        }
                    )
                if fix:
                    index.update(
                        record["id"],
                        **fields,
                        local_observed_at=index.now(),
                        reconciliation_required=False,
                    )
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            issues.append({"path": record["repository_dir"], "problem": str(exc), "fixed": False})
    return issues
