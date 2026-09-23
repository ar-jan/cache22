from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .config import default_archive_dir, normalize_archive_dir
from .index import Index, repository_key
from .repo_service import ImportResult, execute_job
from .repository_ref import parse_repository_url
from .scheduler import Scheduler


def import_repository(
    url: str,
    archive_dir: Path | None = None,
    *,
    case_sensitive: bool = False,
    adopt: bool = False,
    index: Index | None = None,
    timeout: float = 7200,
) -> ImportResult:
    if timeout <= 0:
        raise ValueError("Operation timeout must be positive")
    repository = parse_repository_url(url, case_sensitive=case_sensitive)
    index = index or Index()
    with index.connect() as db:
        existing = db.execute(
            "SELECT archive_root FROM repositories WHERE repo_key=?", (repository_key(repository),)
        ).fetchone()
    root = (
        Path(existing["archive_root"])
        if archive_dir is None and existing
        else _resolve_archive_dir(archive_dir)
    )
    record = index.add(repository, root, importing=True)
    job = Scheduler(index).immediate(record["id"], "fetch")
    result = execute_job(
        index,
        job,
        fetch_timeout=timeout,
        adopt=adopt,
        source_url=repository.clone_url,
    )

    return replace(result, repository=index.get(record["id"]))


def _resolve_archive_dir(archive_dir: Path | None) -> Path:
    return normalize_archive_dir(default_archive_dir() if archive_dir is None else archive_dir)
