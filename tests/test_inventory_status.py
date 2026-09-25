"""Attempt-derived status and the history needed to keep it meaningful."""

import sqlite3
from functools import partial
from pathlib import Path

import pytest

from cache22.index import Index
from cache22.job_queue import Kind, Queue
from cache22.manager_service import detail, jobs_snapshot
from cache22.repo_service import add_repository
from cache22.scheduler import Scheduler


def test_schema_v3_stores_attempts_instead_of_repository_outcomes(tmp_path: Path) -> None:
    index = Index.initialize(tmp_path / "index.db")
    record = add_repository("https://host/team/repo", tmp_path, index=index)
    removed = {
        name
        for kind, past in (("check", "checked"), ("fetch", "fetched"), ("convert", "converted"))
        for name in (
            f"last_{kind}_attempt_at",
            f"last_{past}_at",
            f"{kind}_outcome",
            f"{kind}_error_category",
            f"{kind}_error",
            f"{kind}_error_at",
        )
    }
    with index.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert removed.isdisjoint(row[1] for row in db.execute("PRAGMA table_info(repositories)"))
    assert all(record[f"last_{past}_at"] is None for past in ("checked", "fetched", "converted"))
    assert all(
        record[field] is None
        for field in ("last_error", "last_error_kind", "last_error_category", "last_error_at")
    )
    assert not record["has_error"]
    assert Index(index.path, read_only=True).get(record["id"]) == record
    with pytest.raises(ValueError, match="Unknown or immutable"):
        index.update(record["id"], last_fetched_at=1)


@pytest.mark.parametrize("version", [2, 99])
@pytest.mark.parametrize("access", ["read_only", "read_write", "initialize"])
def test_unsupported_index_is_rejected_without_reset(
    tmp_path: Path, version: int, access: str
) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE preserved(value TEXT)")
        db.execute("INSERT INTO preserved VALUES('old inventory')")
        db.execute(f"PRAGMA user_version={version}")
        before = list(db.iterdump())
    open_index = (
        Index.initialize
        if access == "initialize"
        else partial(Index, read_only=access == "read_only")
    )
    with pytest.raises(ValueError, match=f"version: {version}; expected 3"):
        open_index(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert db.execute("PRAGMA user_version").fetchone()[0] == version
        assert list(db.iterdump()) == before


@pytest.mark.parametrize(
    "kind,past", [("check", "checked"), ("fetch", "fetched"), ("convert", "converted")]
)
def test_success_timestamps_require_completed_attempts(
    tmp_path: Path, kind: Kind, past: str
) -> None:
    now = 1000
    index = Index.initialize(tmp_path / "index.db", clock=lambda: now)
    repo_id = add_repository("https://host/team/repo", tmp_path, index=index)["id"]
    scheduler = Scheduler(index)
    column = f"last_{past}_at"
    first = scheduler.immediate(repo_id, kind)
    assert index.get(repo_id)[column] is None
    now += 1
    scheduler.finish(first)
    success_at = now
    assert index.get(repo_id)[column] == success_at
    now += 1
    failed = scheduler.immediate(repo_id, kind)
    scheduler.finish(failed, category="structural", error="Failure after prior success")
    assert index.get(repo_id)[column] == success_at
    now += 1
    interrupted = scheduler.immediate(repo_id, kind)
    scheduler.interrupt(interrupted)
    assert index.get(repo_id)[column] == success_at
    retry = scheduler.claim()
    assert retry is not None
    assert index.get(repo_id)[column] == success_at
    now += 1
    scheduler.finish(retry)
    record = index.get(repo_id)
    assert record[column] == now
    assert all(
        record[f"last_{other}_at"] is None for other in {"checked", "fetched", "converted"} - {past}
    )
    other_id = add_repository("https://host/team/other", tmp_path, index=index)["id"]
    now += 1
    scheduler.finish(scheduler.immediate(other_id, kind))
    assert [r["id"] for r in index.list(sort=column, descending=True)] == [other_id, repo_id]


def test_promoting_retry_preserves_original_attempt_kind(tmp_path: Path) -> None:
    index = Index.initialize(tmp_path / "index.db", clock=lambda: 1000)
    repo_id = add_repository("https://host/team/repo", tmp_path, index=index)["id"]
    queue = Queue(index)
    scheduler = Scheduler(index)
    check = scheduler.immediate(repo_id, "check")
    scheduler.finish(check, category="transport", error="Check unavailable")
    assert queue.enqueue(repo_id, "fetch") == check["id"]
    assert queue.list(repo_id)[0]["kind"] == "fetch"
    assert detail(index, repo_id)["attempts"][0]["kind"] == "check"
    retry = scheduler.claim()
    assert retry is not None and retry["kind"] == "fetch"
    assert jobs_snapshot(index, state="failed")["jobs"][0]["diagnostic"]["kind"] == "check"
    record = index.get(repo_id)
    assert record["last_error_kind"] == "check"
    assert record["last_error"] == "Check unavailable"
    scheduler.finish(retry)
    record = index.get(repo_id)
    assert record["last_checked_at"] is None
    assert record["last_fetched_at"] == 1000
    assert not record["has_error"]
    assert [a["kind"] for a in queue.list(repo_id)[0]["attempts"]] == ["check", "fetch"]


def test_retention_keeps_latest_success_per_repository_and_kind(tmp_path: Path) -> None:
    now = 1000
    index = Index.initialize(tmp_path / "index.db", clock=lambda: now)
    first_id = add_repository("https://host/team/first", tmp_path, index=index)["id"]
    second_id = add_repository("https://host/team/second", tmp_path, index=index)["id"]
    queue = Queue(index)
    scheduler = Scheduler(index)
    retained: set[int] = set()
    kinds: tuple[Kind, ...] = ("check", "fetch", "convert")
    for repo_id in (first_id, second_id):
        for kind in kinds:
            # Same-time successes select the newer attempt deterministically.
            scheduler.finish(scheduler.immediate(repo_id, kind))
            job = scheduler.immediate(repo_id, kind)
            scheduler.finish(job)
            retained.add(job["id"])
    failed = scheduler.immediate(first_id, "fetch")
    scheduler.finish(failed, category="structural", error="Old failure")
    cancelled = queue.enqueue(first_id, "fetch")
    queue.unqueue(first_id)
    assert index.get(first_id)["has_error"]
    with index.transaction() as db:
        db.execute(
            "INSERT INTO attempt_progress(attempt_id,phase,observed_at) SELECT id,'done',? FROM job_attempts",
            (now,),
        )
    before = index.get(first_id)
    now += 31 * 86400
    active = scheduler.immediate(first_id, "fetch")
    pending = queue.enqueue(first_id, "check")
    scheduler.tick()
    assert {job["id"] for job in queue.list()} == retained | {active["id"], pending}
    assert cancelled not in {job["id"] for job in queue.list()}
    after = index.get(first_id)
    assert not after["has_error"]
    for past in ("checked", "fetched", "converted"):
        assert after[f"last_{past}_at"] == before[f"last_{past}_at"]
    with index.connect() as db:
        assert db.execute("SELECT count(*) FROM attempt_progress").fetchone()[0] == 6
        assert db.execute("SELECT count(*) FROM job_attempts").fetchone()[0] == 7
    scheduler.finish(active)
    retained_fetch = next(
        j["id"] for j in queue.list(first_id) if j["kind"] == "fetch" and j["id"] in retained
    )
    scheduler.tick()
    assert retained_fetch not in {j["id"] for j in queue.list()}
    assert index.get(first_id)["last_fetched_at"] == now
