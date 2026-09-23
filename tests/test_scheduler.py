"""Atomic scheduling policy, including failures between queue and schedule writes."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cache22.index import Index
from cache22.operation import ClaimLostError
from cache22.repo_service import add_repository
from cache22.scheduler import Scheduler


@pytest.fixture
def scheduler(tmp_path: Path) -> Scheduler:
    index = Index(tmp_path / "index.db", clock=lambda: 1000)
    add_repository("https://host/team/repo", tmp_path, index=index)
    return Scheduler(index)


@pytest.fixture
def repo_id(scheduler: Scheduler) -> int:
    return scheduler.index.list()[0]["id"]


@pytest.mark.parametrize("stage", ["schedule", "followup"])
def test_completion_rolls_back_attempt_schedule_and_followup(
    scheduler: Scheduler, repo_id: int, stage: str
) -> None:
    scheduler.schedule(repo_id, 60)
    scheduler.tick()
    job = scheduler.claim()
    assert job is not None
    scheduler.index.update(repo_id, local_state="absent", remote_ref_digest="known")
    before = scheduler.queue.list()
    with scheduler.index.transaction() as db:
        target = (
            "UPDATE ON schedules" if stage == "schedule" else "INSERT ON jobs WHEN NEW.kind='fetch'"
        )
        db.execute(
            f"CREATE TRIGGER fail BEFORE {target} BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        scheduler.finish(job)
    assert scheduler.queue.list() == before
    assert scheduler.index.get(repo_id)["next_due_at"] == 1000
    assert scheduler.index.get(repo_id)["last_checked_at"] is None
    with scheduler.index.transaction() as db:
        db.execute("DROP TRIGGER fail")
    scheduler.finish(job)
    assert scheduler.index.get(repo_id)["next_due_at"] == 1060
    assert scheduler.index.get(repo_id)["last_checked_at"] == 1000
    jobs = scheduler.queue.list()
    assert [(j["kind"], j["state"]) for j in jobs] == [("fetch", "pending"), ("check", "succeeded")]
    with pytest.raises(ClaimLostError):
        scheduler.finish(job)
    assert scheduler.queue.list() == jobs


def test_recovery_and_new_claim_roll_back_together(scheduler: Scheduler, repo_id: int) -> None:
    old = scheduler.immediate(repo_id, "fetch")
    scheduler.index.clock = lambda: 1121
    before = scheduler.queue.list()
    with scheduler.index.transaction() as db:
        db.execute(
            "CREATE TRIGGER fail BEFORE INSERT ON job_attempts BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        scheduler.claim()
    assert scheduler.queue.list() == before
    assert not scheduler.index.get(repo_id)["reconciliation_required"]
    with scheduler.index.transaction() as db:
        db.execute("DROP TRIGGER fail")
    recovered = scheduler.claim()
    assert recovered is not None and recovered["id"] == old["id"]
    assert recovered["claim_token"] != old["claim_token"]
    assert scheduler.index.get(repo_id)["reconciliation_required"]
    assert [a["outcome"] for a in scheduler.queue.list()[0]["attempts"]] == ["interrupted", None]


def test_immediate_admission_rolls_back_superseded_jobs(scheduler: Scheduler, repo_id: int) -> None:
    scheduler.queue.enqueue(repo_id, "check")
    before = scheduler.queue.list()
    with scheduler.index.transaction() as db:
        db.execute(
            "CREATE TRIGGER fail BEFORE INSERT ON job_attempts BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        scheduler.immediate(repo_id, "fetch")
    assert scheduler.queue.list() == before


def test_concurrent_ticks_and_claims_create_one_attempt(scheduler: Scheduler, repo_id: int) -> None:
    scheduler.schedule(repo_id, 60)
    barrier = threading.Barrier(2)

    def claim(_: int):
        other = Scheduler(scheduler.index)
        barrier.wait(timeout=5)
        other.tick()
        barrier.wait(timeout=5)
        return other.claim()

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, range(2)))
    assert sum(job is not None for job in claims) == 1
    jobs = scheduler.queue.list()
    assert len(jobs) == 1 and len(jobs[0]["attempts"]) == 1


@pytest.mark.parametrize("category", [None, "transport", "busy", "unavailable", "structural"])
def test_disable_racing_completion_leaves_no_automatic_pending_work(
    scheduler: Scheduler, repo_id: int, category: str | None
) -> None:
    scheduler.schedule(repo_id, 60)
    scheduler.tick()
    job = scheduler.claim()
    assert job is not None
    scheduler.index.update(repo_id, local_state="absent", remote_ref_digest="known")
    barrier = threading.Barrier(2)

    def finish() -> None:
        barrier.wait(timeout=5)
        scheduler.finish(job, category=category, error="failure" if category else None)

    def disable() -> None:
        barrier.wait(timeout=5)
        Scheduler(scheduler.index).schedule(repo_id, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(finish), pool.submit(disable)]
        for future in futures:
            future.result(timeout=10)
    assert not scheduler.index.get(repo_id)["scheduled"]
    assert all(job["state"] not in {"pending", "running"} for job in scheduler.queue.list())
    scheduler.index.clock = lambda: 10000
    scheduler.tick()
    assert scheduler.claim() is None


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("disable_first", [False, True])
def test_disable_and_interruption_preserve_manual_successor(
    scheduler: Scheduler, repo_id: int, expired: bool, disable_first: bool
) -> None:
    scheduler.schedule(repo_id, 60)
    scheduler.tick()
    job = scheduler.claim()
    assert job is not None
    manual = scheduler.queue.enqueue(repo_id, "fetch")
    if disable_first:
        scheduler.schedule(repo_id, None)
    if expired:
        scheduler.index.clock = lambda: 1121
        # Recover without claiming either job, so both serial orderings can be checked.
        assert scheduler.claim(-1) is None
    else:
        scheduler.interrupt(job)
    if not disable_first:
        scheduler.schedule(repo_id, None)
    claimed = scheduler.claim()
    assert claimed is not None and claimed["id"] == manual and claimed["origin"] == "manual"
    interrupted = next(j for j in scheduler.queue.list() if j["id"] == job["id"])
    assert interrupted["state"] == "cancelled"
    assert interrupted["retry_count"] == 0
    assert interrupted["attempts"][0]["outcome"] == "interrupted"
    assert scheduler.index.get(repo_id)["reconciliation_required"]
