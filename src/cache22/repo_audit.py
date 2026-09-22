"""Offline inventory discovery, reconciliation, and explicitly requested adoption."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from .archive_storage import (
    has_repository_boundary,
    open_archive_directory,
)
from .config import list_archive_dirs
from .index import Index, repository_key
from .repository_ref import parse_repository_url, validate_storage_component
from .storage import Repository, absent_fields


def register_storage(index: Index, root: Path, repo: Repository) -> dict[str, Any]:
    source = repo.read_source()
    if source is None:
        raise ValueError("Managed mirror has no source binding")
    origin = repo.origin_url(source)
    ref = parse_repository_url(origin, case_sensitive=True)
    if Repository.paths_for(root, ref) != repo.paths:
        raise ValueError("Mirror is not at its canonical path")
    return index.add(ref, root)


def audit(
    *, index: Index | None = None, fix: bool = False, adopt: bool = False
) -> list[dict[str, Any]]:
    index = index or Index()
    fix = fix or adopt
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
                if unowned and not adopt:
                    issues.append(
                        {
                            "path": str(path),
                            "problem": "Unowned mirror; explicit adoption required (--adopt)",
                            "fixed": False,
                        }
                    )
                    continue
                if not boundary and not unowned:
                    for child in reversed(children):
                        try:
                            validate_storage_component(child)
                        except ValueError:
                            continue
                        pending.append(relative / child)
                    continue
                key = relative.as_posix()
                record = records.get(key)
                if record is not None and record["archive_root"] != str(root):
                    raise ValueError(
                        f"Duplicate managed copy; selected root is {record['archive_root']}"
                    )
                seen.add(key)
                adopted = False

                def prepare(
                    repo: Repository,
                    record: dict[str, Any] | None = record,
                ) -> None:
                    nonlocal adopted
                    adopted = repo.prepare_adoption(
                        record["source_path"] if record is not None else None
                    )

                with Repository.open_directory(
                    root, path, prepare=prepare if adopt else None
                ) as repo:
                    if repo is None:
                        raise ValueError("Unrecognized ownership marker")
                    registered = record is None
                    if record is None:
                        # Validate before adding; no index-only audit side effects.
                        source = repo.read_source()
                        if source is None:
                            raise ValueError("Managed storage has no source binding")
                        origin = repo.origin_url(source)
                        ref = parse_repository_url(origin, case_sensitive=True)
                        if repository_key(ref) != key:
                            raise ValueError("Mirror is not at its canonical path")
                        observed = repo.observe_local(source)
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
                    fields = repo.observe_local(
                        record["source_path"],
                        previously_ready=record["local_state"] in {"ready", "missing"},
                        expected_format=record["storage_format"],
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
                    elif changed and not fix:
                        issues.append(
                            {
                                "path": str(path),
                                "problem": "Local inventory differs from disk",
                                "fixed": fix,
                            }
                        )
                    for leftover in repo.leftover_paths(fields):
                        issues.append(
                            {
                                "path": str(leftover),
                                "problem": "Bundle staging or retired storage remains; run repo clean",
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
                        if (adopted or registered or changed) and fields[
                            "local_state"
                        ] != "incomplete":
                            problem = (
                                "Adopted Git mirror"
                                if adopted
                                else "Managed mirror was not indexed"
                                if registered
                                else "Local inventory differs from disk"
                            )
                            issues.append({"path": str(path), "problem": problem, "fixed": True})
            except (
                OSError,
                RuntimeError,
                ValueError,
                sqlite3.Error,
                subprocess.SubprocessError,
            ) as exc:
                issues.append({"path": str(path), "problem": str(exc), "fixed": False})
    for key, record in records.items():
        if key in seen or not Path(record["archive_root"]).is_dir():
            continue
        # Only observe expected paths here; an absent marker is not permission to adopt.
        try:
            root = Path(record["archive_root"])
            ref = parse_repository_url(record["source_url"], case_sensitive=True)
            paths = Repository.paths_for(root, ref)
            with Repository.open(root, ref) as repo:
                if (
                    repo is None
                    and paths.repository_dir.exists()
                    and any(paths.repository_dir.iterdir())
                ):
                    raise ValueError("Expected repository has unowned or invalid storage")
                previously_ready = record["local_state"] in {"ready", "missing"}
                fields = (
                    absent_fields(previously_ready)
                    if repo is None
                    else repo.observe_local(
                        record["source_path"],
                        previously_ready=previously_ready,
                        expected_format=record["storage_format"],
                    )
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
        except (
            OSError,
            RuntimeError,
            ValueError,
            sqlite3.Error,
            subprocess.SubprocessError,
        ) as exc:
            issues.append({"path": record["repository_dir"], "problem": str(exc), "fixed": False})
    return issues
