from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from cache22 import git_mirror, operation, repo_service
from cache22.cli import app
from cache22.config import add_archive_dir
from cache22.import_service import import_repository
from cache22.index import Index
from cache22.job_queue import Queue
from cache22.repo_audit import audit
from cache22.repo_service import add_repository, check_repository, run_worker

URL = "https://example.test/team/project"


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@dataclass
class Repository:
    source: Path
    root: Path
    index: Index
    clock: list[float]
    id: int

    def commit(self, message: str) -> str:
        (self.source / "file").write_text(message)
        git(self.source, "add", "file")
        git(self.source, "commit", "-m", message)
        return git(self.source, "rev-parse", "HEAD")

    def fetch(self) -> dict[str, Any]:
        import_repository(URL, self.root, index=self.index)
        return self.index.get(self.id)


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repository:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Cache22 Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.org")
        monkeypatch.setenv(f"GIT_{role}_DATE", "2001-02-03T04:05:06Z")
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "--initial-branch=main")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{source}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", URL)
    root = tmp_path / "archive"
    root.mkdir()
    add_archive_dir(root)
    clock = [1000.0]
    index = Index(clock=lambda: clock[0])
    record = add_repository(URL, root, index=index)
    return Repository(source, root, index, clock, record["id"])


def test_inventory_dates_and_persistent_unfetched_changes(repository: Repository) -> None:
    r = repository
    first = r.commit("first")
    record = check_repository(r.id, index=r.index)
    assert record["remote_status"] == "not_fetched"
    assert record["remote_head_oid"] == first
    assert record["local_head_committed_at"] is None
    assert not Path(record["archive_path"]).exists()
    assert not any("remote" in key and ("date" in key or "committed" in key) for key in record)
    record = r.fetch()
    assert record["remote_status"] == "current"
    assert record["local_head_committed_at"] == 981173106
    second = r.commit("second")
    for now in (1100, 1200):
        r.clock[0] = now
        record = check_repository(r.id, index=r.index)
        assert record["remote_status"] == "updates_available"
        assert record["local_head_oid"] == first
        assert record["remote_head_oid"] == second
        assert record["last_checked_at"] == now
    assert r.fetch()["remote_status"] == "current"


@pytest.mark.parametrize("stage", ["clone", "fetch", "ls-remote"])
def test_git_failure_details_reach_cli_and_history(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    r = repository
    if stage != "clone":
        r.commit("initial")
        r.fetch()
    run = operation.run
    secret_url = "https://user:secret@example.test/team/project/"
    diagnostic = "SSL certificate problem: unable to get local issuer certificate"
    stderr = (
        b"Receiving objects: 50% (5/10)\r" * 3000
        + f"\x1b[31mfatal: unable to access '{secret_url}': {diagnostic}\x1b[0m\n".encode()
        + (b"transport detail: \xff\n" if stage != "ls-remote" else b"")
    )

    def fail_transport(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if stage in args:
            return run(
                [
                    sys.executable,
                    "-c",
                    f"import os; os.write(2, {stderr!r}); raise SystemExit(128)",
                ],
                check=True,
                observe_progress=kwargs.get("observe_progress", False),
                capture_output=True,
                text=stage != "clone",
            )
        return run(args, **kwargs)

    monkeypatch.setattr(git_mirror, "run_git", fail_transport)
    monkeypatch.setattr(operation, "run", fail_transport)
    command = "check" if stage == "ls-remote" else "fetch"
    result = CliRunner().invoke(app, ["repo", command, URL])
    assert result.exit_code == 1, result.output
    job = Queue(r.index).list(r.id)[0]
    assert job["state"] == "pending" and job["error_category"] == "transport"
    for message in (
        result.output,
        job["error"],
        job["attempts"][0]["error"],
        r.index.get(r.id)[f"{command}_error"],
    ):
        assert diagnostic in message
        assert "secret" not in message and "\x1b" not in message
    assert len(job["error"]) <= 4000


def test_ref_deletions_force_updates_and_peeled_tags(repository: Repository) -> None:
    r = repository
    first = r.commit("first")
    git(r.source, "tag", "-a", "v1", "-m", "release")
    r.fetch()
    # Peeled tag advertisements must not make identical mirrors look different.
    assert check_repository(r.id, index=r.index)["remote_status"] == "current"
    second = r.commit("second")
    git(r.source, "branch", "side", first)
    r.fetch()
    git(r.source, "update-ref", "refs/heads/side", second)
    assert check_repository(r.id, index=r.index)["remote_status"] == "updates_available"
    r.fetch()
    git(r.source, "update-ref", "refs/heads/main", first)
    git(r.source, "tag", "-d", "v1")
    assert check_repository(r.id, index=r.index)["remote_status"] == "updates_available"
    assert r.fetch()["remote_status"] == "current"


@pytest.mark.parametrize("detached", [False, True])
def test_empty_and_detached_head(repository: Repository, detached: bool) -> None:
    r = repository
    if detached:
        r.commit("first")
        git(r.source, "checkout", "--detach")
    record = r.fetch()
    assert record["remote_status"] == "current"
    assert (record["local_head_oid"] is not None) == detached
    assert (record["local_head_committed_at"] is not None) == detached
    if detached:
        assert record["local_head_ref"] is None


def test_listing_is_database_only_with_disconnected_root(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = repository
    r.commit("first")
    before = r.fetch()
    r.root.rename(r.root.with_name("disconnected"))

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Inventory listing must not inspect archives or run Git")

    monkeypatch.setattr(os, "scandir", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    assert r.index.list()[0] == before
    result = CliRunner().invoke(app, ["repo", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["last_checked_at"] == "1970-01-01T00:16:40Z"


def test_schedule_checks_then_fetches_and_coalesces(repository: Repository) -> None:
    r = repository
    r.commit("first")
    queue = Queue(r.index)
    assert run_worker(index=r.index) == []
    queue.schedule(r.id, 3600)
    queue.materialize()
    queue.materialize()
    assert len(queue.list()) == 1
    assert [result["outcome"] for result in run_worker(index=r.index)] == ["succeeded", "succeeded"]
    assert r.index.get(r.id)["remote_status"] == "current"
    assert r.index.get(r.id)["next_due_at"] == 4600
    assert run_worker(index=r.index) == []
    r.clock[0] += 10 * 3600
    assert len(run_worker(index=r.index)) == 1
    queue.schedule(r.id, None)
    r.clock[0] += 10 * 3600
    assert run_worker(index=r.index) == []


def test_disable_running_schedule_prevents_followup(repository: Repository) -> None:
    r = repository
    queue = Queue(r.index)
    queue.schedule(r.id, 3600)
    queue.materialize()
    job = queue.claim()
    assert job
    r.index.update(r.id, local_state="absent", remote_ref_digest="known")
    queue.schedule(r.id, None)
    queue.finish(job)
    assert queue.claim() is None


def test_queue_deduplicates_and_manual_work_survives_disable(repository: Repository) -> None:
    r = repository
    queue = Queue(r.index)
    queue.schedule(r.id, 3600)
    queue.materialize()
    job_id = queue.enqueue(r.id, "fetch")
    assert queue.enqueue(r.id, "check") == job_id
    queue.schedule(r.id, None)
    job = queue.claim()
    assert job and job["kind"] == "fetch" and job["origin"] == "manual"
    queue.finish(job)
    assert queue.claim() is None


def test_claims_are_exclusive_and_expired_owners_are_fenced(repository: Repository) -> None:
    r = repository
    queue = Queue(r.index)
    queue.enqueue(r.id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: Queue(r.index).claim(), range(2)))
    assert sum(job is not None for job in claims) == 1
    old = next(job for job in claims if job)
    with queue.running(old, 120):
        r.clock[0] += 121
        new = queue.claim()
        assert new and new["id"] == old["id"] and new["claim_token"] != old["claim_token"]
        with pytest.raises(operation.ClaimLostError):
            r.index.update(r.id, local_state="ready")
    assert r.index.get(r.id)["reconciliation_required"]
    with pytest.raises(operation.ClaimLostError):
        queue.finish(old)
    queue.finish(new)
    assert queue.list()[0]["attempts"][0]["outcome"] == "interrupted"


def test_remote_failure_preserves_snapshot_and_retries(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = repository
    r.commit("first")
    before = r.fetch()

    def fail(url: str) -> Any:
        raise operation.TransportError("offline")

    monkeypatch.setattr(repo_service, "remote_snapshot", fail)
    queue = Queue(r.index)
    queue.enqueue(r.id, "check")
    for delay in (60, 300, 1800, 7200):
        assert len(run_worker(index=r.index)) == 1
        job = queue.list()[0]
        assert job["state"] == "pending"
        assert job["due_at"] == r.index.now() + delay
        assert run_worker(index=r.index) == []
        r.clock[0] += delay
    run_worker(index=r.index)
    assert queue.list()[0]["state"] == "failed"
    after = r.index.get(r.id)
    assert after["last_checked_at"] == before["last_checked_at"]
    assert after["remote_ref_digest"] == before["remote_ref_digest"]
    assert after["check_error"] == "offline"
    assert after["check_outcome"] == "failed"


def test_unavailable_root_defers_fetch_without_losing_inventory(repository: Repository) -> None:
    r = repository
    r.commit("first")
    before = r.fetch()
    r.root.rename(r.root.with_name("offline"))
    queue = Queue(r.index)
    queue.enqueue(r.id)
    run_worker(index=r.index)
    job = queue.list()[0]
    assert job["state"] == "pending" and job["retry_count"] == 0
    assert job["error_category"] == "unavailable"
    after = r.index.get(r.id)
    assert after["local_head_oid"] == before["local_head_oid"]
    assert after["local_state"] == "ready"


def test_audit_bootstraps_owned_mirrors_without_network(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    r = repository
    r.commit("first")
    before = r.fetch()
    other = Index(tmp_path / "other.sqlite3", clock=lambda: 2000)
    monkeypatch.setattr(
        repo_service, "remote_snapshot", lambda url: pytest.fail("Audit must be offline")
    )
    assert any("not indexed" in issue["problem"] for issue in audit(index=other))
    assert other.list() == []
    assert all(issue["fixed"] for issue in audit(index=other, fix=True))
    record = other.get(before["repo_key"])
    assert record["local_head_oid"] == before["local_head_oid"]
    assert record["remote_status"] == "unknown"
    assert record["last_fetched_at"] is None and record["last_checked_at"] is None
    assert not record["scheduled"]
    assert audit(index=other) == []


def test_publication_failure_requires_reconciliation(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = repository
    r.commit("first")
    update = r.index.update

    def fail_publication(repository_id: int, **fields: Any) -> None:
        if "local_state" in fields:
            raise sqlite3.OperationalError("simulated disk full")
        update(repository_id, **fields)

    monkeypatch.setattr(r.index, "update", fail_publication)
    with pytest.raises(sqlite3.OperationalError):
        r.fetch()
    record = r.index.get(r.id)
    assert record["reconciliation_required"]
    assert record["last_fetched_at"] is None
    assert Path(record["archive_path"]).exists()
    monkeypatch.setattr(r.index, "update", update)
    assert all(issue["fixed"] for issue in audit(index=r.index, fix=True))
    assert not r.index.get(r.id)["reconciliation_required"]
    assert r.index.get(r.id)["last_fetched_at"] is None


def test_cli_selection_and_scheduling(repository: Repository) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["repo", "check"]).exit_code == 2
    assert runner.invoke(app, ["repo", "fetch", URL, "--all"]).exit_code == 2
    assert runner.invoke(app, ["worker", "run"]).exit_code == 2
    result = runner.invoke(app, ["repo", "schedule", URL, "--every", "6h"])
    assert result.exit_code == 0, result.output
    assert repository.index.get(repository.id)["interval_seconds"] == 21600
    result = runner.invoke(app, ["repo", "show", URL, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["scheduled"] is True


def test_structural_failure_blocks_schedule_until_intervention(repository: Repository) -> None:
    r = repository
    queue = Queue(r.index)
    queue.schedule(r.id, 60)
    queue.materialize()
    job = queue.claim()
    assert job
    queue.finish(job, category="structural", error="Explicit adoption needed")
    r.clock[0] += 3600
    queue.materialize()
    assert queue.claim() is None
    assert r.index.get(r.id)["schedule_blocked"]
    queue.schedule(r.id, 60)
    queue.materialize()
    assert queue.claim() is not None


def test_direct_fetch_consumes_pending_work_and_keeps_selected_root(
    repository: Repository, tmp_path: Path
) -> None:
    r = repository
    r.commit("first")
    queue = Queue(r.index)
    queue.enqueue(r.id)
    result = import_repository(URL, index=r.index)
    assert result.repository is not None
    assert result.repository["archive_root"] == str(r.root)
    assert queue.claim() is None
    other_root = tmp_path / "other-root"
    other_root.mkdir()
    # A new default does not move a repository already assigned to a root.
    from cache22 import config

    config.config_file().write_text(f'archive_dirs = ["{other_root}"]\n')
    result = import_repository(URL, index=r.index)
    assert result.archive_path == Path(r.index.get(r.id)["archive_path"])


def test_default_branch_rename_and_cleanup_refresh_inventory(repository: Repository) -> None:
    from cache22.import_state import clean_repository_import_state

    r = repository
    r.commit("first")
    before = r.fetch()
    git(r.source, "branch", "-m", "renamed")
    assert check_repository(r.id, index=r.index)["remote_status"] == "updates_available"
    after = r.fetch()
    assert after["local_head_ref"] == "refs/heads/renamed"
    assert after["remote_status"] == "current"
    marker = Path(after["repository_dir"]) / ".clone-complete"
    marker.unlink()
    clean_repository_import_state(URL, [r.root])
    cleaned = r.index.get(r.id)
    assert cleaned["local_state"] == "missing"
    assert cleaned["local_head_oid"] is None
    assert cleaned["last_fetched_at"] == before["last_fetched_at"]


def test_audit_reports_duplicate_and_unowned_mirrors(
    repository: Repository, tmp_path: Path
) -> None:
    import shutil

    r = repository
    r.commit("first")
    record = r.fetch()
    duplicate = tmp_path / "duplicate"
    shutil.copytree(r.root, duplicate)
    add_archive_dir(duplicate)
    issues = audit(index=r.index, fix=True)
    assert any("Duplicate managed copy" in i["problem"] and not i["fixed"] for i in issues)
    unowned = r.root / "example.test/team/unowned"
    unowned.mkdir()
    git(r.source, "clone", "--mirror", URL, str(unowned / "unowned.git"))
    issues = audit(index=r.index, fix=True)
    assert any("Unowned mirror" in i["problem"] and not i["fixed"] for i in issues)
    assert not (unowned / ".lock").exists()
    assert r.index.get(r.id)["archive_root"] == record["archive_root"]


def test_operation_timeout_terminates_child_and_releases_locks(
    repository: Repository, tmp_path: Path
) -> None:
    import sys

    from cache22.archive_layout import archive_paths_for_repository
    from cache22.archive_storage import repository_operation
    from cache22.repository_ref import parse_repository_url

    r = repository
    queue = Queue(r.index)
    job = queue.immediate(r.id, "check")
    paths = archive_paths_for_repository(r.root, parse_repository_url(URL))
    pidfile = tmp_path / "child-pid"
    with (
        pytest.raises(operation.TransportError, match="timed out"),
        queue.running(job, 0.3),
        repository_operation(r.root, paths, create=True),
    ):
        operation.run(
            [
                sys.executable,
                "-c",
                'import os,signal,sys; open(sys.argv[1],"w").write(str(os.getpid())); signal.pause()',
                str(pidfile),
            ]
        )
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    with repository_operation(r.root, paths):
        pass
    queue.finish(job, category="transport", error="Operation timed out")
