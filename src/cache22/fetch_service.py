from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .errors import StructuralError
from .index import Index
from .repo_service import FetchResult, execute_job, registration_target
from .scheduler import Scheduler


def fetch_repository(
    url: str,
    archive_dir: Path | None = None,
    *,
    case_sensitive: bool = False,
    adopt: bool = False,
    index: Index,
    timeout: float = 7200,
) -> FetchResult:
    if timeout <= 0:
        raise StructuralError("Operation timeout must be positive")
    repository, root = registration_target(index, url, archive_dir, case_sensitive)
    record = index.add(repository, root, importing=True)
    job = Scheduler(index).immediate(record["id"], "fetch")
    result = execute_job(
        index,
        job,
        fetch_timeout=timeout,
        adopt=adopt,
        source_url=repository.clone_url,
    )

    assert isinstance(result, FetchResult)
    return replace(result, repository=index.get(record["id"]))
