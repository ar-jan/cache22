from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from cache22 import operation
from cache22.index import Index
from cache22.job_operation import running_job
from cache22.job_queue import Queue
from cache22.manager.app import create_datasette
from cache22.manager_service import bulk_command, detail, queue_snapshot, register_batch
from cache22.repo_service import add_repository
from cache22.scheduler import Scheduler
from cache22.worker import run_continuous


def test_registration_batch_preview_duplicates_conflicts_and_atomic_enqueue(tmp_path: Path) -> None:
    index = Index.initialize(tmp_path / "index.db")
    urls = ["https://host/Team/Repo", "https://host/Team/Repo", "invalid", "https://host/team/repo"]
    preview = register_batch(index, urls, root=tmp_path, case_sensitive=True, preview=True)
    assert [r["status"] for r in preview] == ["new", "duplicate", "error", "error"]
    assert index.list() == []
    results = register_batch(index, urls, root=tmp_path, case_sensitive=True, fetch=True)
    assert [r["status"] for r in results] == ["registered", "duplicate", "error", "error"]
    assert results[0]["job_id"] == results[1]["job_id"]
    repeated = register_batch(index, urls[:1], root=tmp_path, case_sensitive=True, fetch=True)
    assert repeated[0]["job_id"] == results[0]["job_id"]
    with patch.object(
        Queue, "enqueue_in", side_effect=sqlite3.OperationalError("simulated failure")
    ):
        failed = register_batch(index, ["https://host/team/other"], root=tmp_path, fetch=True)
    assert failed[0]["status"] == "error"
    assert len(index.list()) == 1


def test_interrupt_merges_successor_and_fences_progress(tmp_path: Path) -> None:
    index = Index.initialize(tmp_path / "index.db")
    repository = add_repository("https://host/team/repo", tmp_path, index=index)
    queue = Queue(index)
    scheduler = Scheduler(index)
    scheduler.schedule(repository["id"], 60)
    scheduler.tick()
    job = scheduler.claim()
    assert job
    with running_job(queue, job, 60):
        operation.progress("fetching", completed=1, total=2)
    assert queue_snapshot(index)["jobs"][0]["live"] == 1
    queue.enqueue(repository["id"], "fetch")
    scheduler.schedule(repository["id"], None)
    scheduler.interrupt(job)
    record = detail(index, repository["id"])
    assert record["attempts"][0]["outcome"] == "interrupted"
    assert queue_snapshot(index)["counts"]["runnable"] == 1
    recovered = scheduler.claim()
    assert recovered and recovered["kind"] == "fetch" and recovered["origin"] == "manual"
    assert recovered["attempt_id"] != job["attempt_id"]
    assert queue_snapshot(index)["jobs"][0]["phase"] is None
    with pytest.raises(operation.ClaimLostError), running_job(queue, job, 60):
        operation.progress("stale owner")


def test_worker_idle_registry_and_shutdown(tmp_path: Path) -> None:
    index = Index.initialize(tmp_path / "index.db")
    stop = threading.Event()
    thread = threading.Thread(target=run_continuous, kwargs={"index": index, "stop": stop})
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while not queue_snapshot(index)["workers"] and time.monotonic() < deadline:
            time.sleep(0.01)
        worker = queue_snapshot(index)["workers"][0]
        assert worker["available"] and worker["current_job_id"] is None
        with index.transaction() as db:
            db.execute("UPDATE workers SET heartbeat_at=?", (index.now() - 16,))
        assert not queue_snapshot(index)["workers"][0]["available"]
    finally:
        stop.set()
        thread.join(3)
    assert not thread.is_alive()
    assert queue_snapshot(index)["workers"][0]["stopped_at"] is not None


def test_streaming_progress_drains_both_pipes_and_preserves_output(tmp_path: Path) -> None:
    index = Index.initialize(tmp_path / "index.db")
    repository = add_repository("https://host/team/repo", tmp_path, index=index)
    queue = Queue(index)
    scheduler = Scheduler(index)
    job = scheduler.immediate(repository["id"], "fetch")
    script = "import os; os.write(1,b'x'*200000); os.write(2,b'noise'*20000+b'\\rReceiving objects: 50% (5/10)\\r')"
    with running_job(queue, job, 10):
        result = operation.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            observe_progress=True,
        )
    assert len(result.stdout) == 200000
    assert len(result.stderr) <= 65536
    progress = queue_snapshot(index)["jobs"][0]
    assert progress["percentage"] == 50 and progress["completed"] == 5
    scheduler.finish(job)
    assert queue_snapshot(index, section="history")["jobs"][0]["live"] == 0


def test_datasette_browsing_selection_commands_and_boundaries(tmp_path: Path) -> None:
    async def exercise() -> None:
        ds = create_datasette(tmp_path / "index.db")
        index = ds.cache22_index
        ids = [
            add_repository(f"https://user:secret@host/team/repo{i}", tmp_path, index=index)["id"]
            for i in range(3)
        ]
        try:
            # All index fields remain inspectable, including source URLs.
            response = await ds.client.get("/index/repositories.json")
            assert response.status_code == 200 and "user:secret" in response.text
            assert (
                await ds.client.get("/index/-/query.json?sql=select+count(*)+from+jobs")
            ).status_code == 200
            response = await ds.client.get("/", follow_redirects=True)
            assert response.status_code == 200 and "c22-inventory" in response.text
            # source_url does not appear among the summary table cells.
            assert 'class="col-source_url type-' not in response.text
            for query in ("project_name__contains=repo1", "_q=repo1", "_where=id%3D2"):
                selected = await ds.client.post("/-/cache22/api/selection", json={"query": query})
                assert selected.status_code == 200, selected.text
                assert selected.json()["ids"] == [ids[1]]
            selected = (
                await ds.client.post("/-/cache22/api/selection", json={"query": "queued=0"})
            ).json()["ids"]
            bulk_command(index, [ids[0]], "fetch")
            response = await ds.client.post(
                "/-/cache22/api/command", json={"ids": selected, "action": "check"}
            )
            assert len(response.json()["results"]) == 3
            assert Queue(index).list(ids[0])[0]["kind"] == "fetch"
            for headers in (
                {"host": "evil.example"},
                {"origin": "https://evil.example"},
                {"sec-fetch-site": "cross-site"},
            ):
                response = await ds.client.post(
                    "/-/cache22/api/command", json={"ids": ids, "action": "fetch"}, headers=headers
                )
                assert response.status_code == 403
            assert (await ds.client.get("/-/cache22/api/command")).status_code == 405
            response = await ds.client.post(
                "/index/repositories/-/insert", json={"rows": [{"repo_key": "bypass"}]}
            )
            assert response.status_code == 403
            response = await ds.client.post(
                "/-/cache22/api/command", json={"ids": [True], "action": "fetch"}
            )
            assert response.status_code == 400
            assert len(index.list()) == 3
            scheduler = Scheduler(index)
            failed = scheduler.immediate(ids[1], "check")
            scheduler.finish(failed, category="structural", error="Invalid storage")
            response = await ds.client.post(
                "/-/cache22/api/selection", json={"query": "has_error=1"}
            )
            assert response.status_code == 200
            assert response.json()["ids"] == [ids[1]]
            response = await ds.client.get("/index/inventory.json?has_error=1")
            assert response.status_code == 200
            assert response.json()["rows"][0]["last_error"] == "Invalid storage"
        finally:
            await ds.invoke_shutdown()

    asyncio.run(exercise())
