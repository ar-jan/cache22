"""Queue persistence can be exercised without scheduling or running operations."""

from pathlib import Path

import pytest

from cache22.index import Index
from cache22.job_queue import Queue
from cache22.operation import ClaimLostError
from cache22.repo_service import add_repository


def test_coalescing_and_claims_respect_conversion_barriers(tmp_path: Path) -> None:
    index = Index.initialize(tmp_path / "index.db", clock=lambda: 1000)
    repo_id = add_repository("https://host/team/repo", tmp_path, index=index)["id"]
    queue = Queue(index)
    with index.transaction() as db:
        first = queue.enqueue_in(db, repo_id, "check", origin="scheduled")
    assert queue.enqueue(repo_id, "fetch") == first
    conversion = queue.enqueue(repo_id, "convert")
    assert queue.enqueue(repo_id, "convert") == conversion
    last = queue.enqueue(repo_id, "check")
    assert queue.enqueue(repo_id, "fetch") == last
    assert last != first
    with index.transaction() as db:
        assert queue.claim_in(db, conversion) is None
        job = queue.claim_in(db)
        assert job is not None and job["id"] == first
        assert job["kind"] == "fetch" and job["origin"] == "manual"
        # The queue persists the supplied disposition without choosing retry policy.
        queue.finish_in(
            db,
            job,
            outcome="failed",
            state="pending",
            due_at=2000,
            retry_count=7,
            category="transport",
            error="offline",
        )
    with index.transaction() as db:
        assert queue.claim_in(db) is None
    assert queue.list()[-1]["retry_count"] == 7
    assert queue.list()[-1]["attempts"][0]["outcome"] == "failed"


def test_expired_owner_cannot_renew_publish_or_finalize(tmp_path: Path) -> None:
    now = 1000
    index = Index.initialize(tmp_path / "index.db", clock=lambda: now)
    repo_id = add_repository("https://host/team/repo", tmp_path, index=index)["id"]
    queue = Queue(index)
    queue.enqueue(repo_id)
    with index.transaction() as db:
        job = queue.claim_in(db)
    assert job is not None
    now = 1010
    queue.heartbeat(job)
    assert queue.list()[0]["lease_until"] == 1130
    queue.publish_progress(job, {"phase": "before expiry"})
    now = 1130
    before = queue.list()
    with pytest.raises(ClaimLostError):
        queue.heartbeat(job)
    with pytest.raises(ClaimLostError):
        queue.publish_progress(job, {"phase": "stale"})
    with index.transaction() as db, pytest.raises(ClaimLostError):
        queue.finish_in(
            db,
            job,
            outcome="succeeded",
            state="succeeded",
            due_at=now,
            retry_count=0,
            category=None,
            error=None,
        )
    assert queue.list() == before
    with index.connect() as db:
        assert db.execute("SELECT phase FROM attempt_progress").fetchone()[0] == "before expiry"
