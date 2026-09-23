"""Real-Git acceptance tests for bundle publication, updates, and recovery."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from cache22 import git_bundle, operation
from cache22.archive_layout import ArchivePaths, archive_paths_for_repository
from cache22.archive_storage import RepositoryBusyError, repository_operation
from cache22.cli import app
from cache22.config import add_archive_dir
from cache22.import_service import import_repository
from cache22.import_state import clean_repository_import_state
from cache22.index import Index
from cache22.job_queue import Queue
from cache22.manager_service import bulk_command, queue_snapshot
from cache22.repo_audit import audit
from cache22.repo_service import check_repository, execute_job
from cache22.repository_ref import parse_repository_url
from cache22.scheduler import Scheduler
from cache22.worker import run_worker

URL = "https://example.test/team/project"


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@dataclass
class Repo:
    source: Path
    root: Path
    index: Index
    id: int
    paths: ArchivePaths
    clock: list[float]

    def commit(self, message: str) -> str:
        (self.source / "file").write_text(message)
        git(self.source, "add", "file")
        git(self.source, "commit", "-m", message)
        return git(self.source, "rev-parse", "HEAD")

    def fetch(self) -> Any:
        return import_repository(URL, self.root, index=self.index)

    def convert(self) -> Path:
        queue = Queue(self.index)
        scheduler = Scheduler(self.index)
        job_id = queue.enqueue(self.id, "convert")
        job = scheduler.claim(job_id)
        assert job is not None
        return execute_job(self.index, job)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Repo:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Bundle Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.invalid")
    source = tmp_path / "source"
    source.mkdir()
    git(
        source,
        "init",
        "--initial-branch=main",
        f"--object-format={getattr(request, 'param', 'sha1')}",
    )
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{source}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", URL)
    root = tmp_path / "archive"
    root.mkdir()
    add_archive_dir(root)
    clock = [1000.0]
    index = Index(clock=lambda: clock[0])
    ref = parse_repository_url(URL)
    record = index.add(ref, root)
    return Repo(source, root, index, record["id"], archive_paths_for_repository(root, ref), clock)


@pytest.mark.parametrize("repo", ["sha1", "sha256"], indirect=True)
@pytest.mark.parametrize("detached", [False, True])
def test_offline_conversion_roundtrip_and_audit(
    repo: Repo, detached: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = repo.commit("first")
    git(repo.source, "branch", "other")
    git(repo.source, "tag", "-a", "v1", "-m", "annotation")
    git(repo.source, "update-ref", "refs/notes/example", first)
    if detached:
        git(repo.source, "checkout", "--detach")
        repo.commit("detached")
    repo.fetch()
    before = repo.index.get(repo.id)
    # Offline conversion must not touch origin, even with a detached HEAD.
    repo.source.rename(repo.source.with_name("offline"))
    bundle = repo.convert()
    assert bundle.is_file()
    assert not repo.paths.mirror_repository.exists()
    assert not repo.paths.clone_complete_marker.exists()
    assert not repo.paths.bundle_staging.exists()
    after = repo.index.get(repo.id)
    assert after["storage_format"] == "bundle"
    assert after["archive_path"] == str(bundle)
    for key in (
        "local_ref_digest",
        "local_head_ref",
        "local_head_oid",
        "local_head_committed_at",
        "last_fetched_at",
        "last_checked_at",
    ):
        assert after[key] == before[key]
    with repository_operation(repo.root, repo.paths) as storage:
        assert storage is not None
        state = git_bundle.read_bundle(storage, parse_repository_url(URL).source_path)
        git_bundle.restore(state, tmp_path / "restored.git")
    assert git(tmp_path / "restored.git", "rev-parse", "refs/notes/example") == first
    assert repo.convert() == bundle
    assert clean_repository_import_state(URL) == ()
    new_index = Index(tmp_path / "rebuilt.sqlite")
    assert any(item["fixed"] for item in audit(index=new_index, fix=True))
    rebuilt = new_index.get(URL)
    assert rebuilt["storage_format"] == "bundle"
    assert rebuilt["archive_path"] == str(bundle)
    assert rebuilt["local_ref_digest"] == before["local_ref_digest"]


def test_bundle_checks_and_fetches_follow_remote_refs(repo: Repo) -> None:
    first = repo.commit("first")
    git(repo.source, "branch", "deleted")
    git(repo.source, "tag", "old")
    repo.fetch()
    old_bundle = repo.convert()
    second = repo.commit("second")
    git(repo.source, "branch", "-D", "deleted")
    git(repo.source, "tag", "-d", "old")
    assert check_repository(repo.id, index=repo.index)["remote_status"] == "updates_available"
    repo.fetch()
    assert not old_bundle.exists()
    assert repo.index.get(repo.id)["local_head_oid"] == second
    git(repo.source, "reset", "--hard", first)
    rewritten = repo.commit("rewritten")
    repo.fetch()
    assert repo.index.get(repo.id)["local_head_oid"] == rewritten
    git(repo.source, "branch", "-m", "renamed")
    assert check_repository(repo.id, index=repo.index)["remote_status"] == "updates_available"
    repo.fetch()
    assert repo.index.get(repo.id)["local_head_ref"] == "refs/heads/renamed"
    assert not repo.paths.mirror_repository.exists()
    assert len(list(repo.paths.repository_dir.glob("*.bundle"))) == 1


def test_empty_conversion_and_empty_update_preserve_archive(repo: Repo) -> None:
    repo.fetch()
    with pytest.raises(ValueError, match="empty"):
        repo.convert()
    assert repo.paths.mirror_repository.is_dir()
    repo.commit("nonempty")
    repo.fetch()
    bundle = repo.convert()
    manifest = repo.paths.bundle_manifest.read_bytes()
    last_fetch = repo.index.get(repo.id)["last_fetched_at"]
    git(repo.source, "update-ref", "-d", "refs/heads/main")
    repo.clock[0] += 1
    with pytest.raises(ValueError, match="empty"):
        repo.fetch()
    assert bundle.is_file()
    assert repo.paths.bundle_manifest.read_bytes() == manifest
    assert repo.index.get(repo.id)["last_fetched_at"] == last_fetch


@pytest.mark.parametrize("after_publication", [False, True])
def test_publication_interruption_keeps_recoverable_source(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, after_publication: bool
) -> None:
    repo.commit("first")
    repo.fetch()
    if after_publication:

        def fail_retire(*args: Any) -> None:
            raise OSError("injected cleanup failure")

        monkeypatch.setattr(git_bundle, "retire", fail_retire)
    else:

        def fail_replace(*args: Any) -> None:
            raise OSError("injected publication failure")

        monkeypatch.setattr(git_bundle.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        repo.convert()
    assert repo.paths.mirror_repository.is_dir()
    assert repo.paths.bundle_manifest.exists() == after_publication
    monkeypatch.undo()
    # Cleanup uses the persisted binding and requires no network.
    with repository_operation(repo.root, repo.paths) as storage:
        assert storage is not None
        git_bundle.cleanup(storage, parse_repository_url(URL).source_path)
    assert repo.paths.mirror_repository.exists() != after_publication
    assert not repo.paths.bundle_staging.exists()


def test_corrupt_selected_bundle_prevents_source_retirement(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.commit("first")
    repo.fetch()
    original_retire = git_bundle.retire
    monkeypatch.setattr(git_bundle, "retire", lambda *args: [])
    bundle = repo.convert()
    monkeypatch.setattr(git_bundle, "retire", original_retire)
    contents = bytearray(bundle.read_bytes())
    contents[-1] ^= 1
    bundle.write_bytes(contents)
    with repository_operation(repo.root, repo.paths) as storage:
        assert storage is not None
        with pytest.raises(subprocess.CalledProcessError):
            git_bundle.cleanup(storage, parse_repository_url(URL).source_path)
    assert repo.paths.mirror_repository.is_dir()
    assert bundle.is_file()


def test_missing_manifest_never_recreates_mirror(repo: Repo) -> None:
    repo.commit("first")
    repo.fetch()
    bundle = repo.convert()
    repo.paths.bundle_manifest.unlink()
    with pytest.raises(ValueError, match="manifest is missing"):
        repo.fetch()
    assert bundle.exists()
    assert not repo.paths.mirror_repository.exists()


def test_ordered_conversion_jobs_and_manager_queue(repo: Repo) -> None:
    repo.commit("first")
    queue = Queue(repo.index)
    scheduler = Scheduler(repo.index)
    fetch = queue.enqueue(repo.id)
    conversion = bulk_command(repo.index, [repo.id], "convert")[0]["job_id"]
    assert queue.enqueue(repo.id, "convert") == conversion
    check = queue.enqueue(repo.id, "check")
    assert queue.enqueue(repo.id, "fetch") == check
    deferred = queue_snapshot(repo.index, section="deferred")
    assert {job["id"] for job in deferred["jobs"]} == {conversion, check}
    assert all(job["blocking_job_id"] == fetch for job in deferred["jobs"])
    assert scheduler.claim(conversion) is None
    with pytest.raises(RepositoryBusyError):
        repo.fetch()
    results = run_worker(index=repo.index)
    assert [r["job_id"] for r in results] == [fetch, conversion, check]
    assert all(r["outcome"] == "succeeded" for r in results)
    assert repo.index.get(repo.id)["storage_format"] == "bundle"


def test_retry_and_expired_claim_do_not_cross_conversion(repo: Repo) -> None:
    queue = Queue(repo.index)
    scheduler = Scheduler(repo.index)
    first = queue.enqueue(repo.id, "fetch")
    job = scheduler.claim(first)
    assert job is not None
    conversion = queue.enqueue(repo.id, "convert")
    last = queue.enqueue(repo.id, "fetch")
    repo.clock[0] += 121
    recovered = scheduler.claim()
    assert recovered is not None and recovered["id"] == first
    scheduler.finish(recovered, category="transport", error="offline")
    assert scheduler.claim(conversion) is None
    assert scheduler.claim(last) is None
    assert scheduler.claim() is None
    queue.unqueue(repo.id)
    assert all(j["state"] == "cancelled" for j in queue.list())


def test_cli_conversion_enqueues_without_reading_archive(repo: Repo) -> None:
    result = CliRunner().invoke(app, ["queue", URL, "--kind", "convert", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["status"] == "accepted"
    assert Queue(repo.index).list()[0]["kind"] == "convert"
    assert not repo.paths.repository_dir.exists()


def test_index_failure_after_publication_retains_source_and_audit_recovers(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.commit("first")
    repo.fetch()
    update = repo.index.update

    def fail_publication(repository_id: int, **fields: Any) -> None:
        if fields.get("storage_format") == "bundle":
            raise sqlite3.OperationalError("injected index failure")
        update(repository_id, **fields)

    monkeypatch.setattr(repo.index, "update", fail_publication)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        repo.convert()
    assert repo.paths.bundle_manifest.is_file()
    assert repo.paths.mirror_repository.is_dir()
    assert repo.index.get(repo.id)["reconciliation_required"]
    monkeypatch.setattr(repo.index, "update", update)
    audit(index=repo.index, fix=True)
    assert repo.index.get(repo.id)["storage_format"] == "bundle"
    clean_repository_import_state(URL)
    assert not repo.paths.mirror_repository.exists()


def test_bundle_fetch_index_failure_retains_generation_and_recovers_offline(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = repo.commit("first")
    repo.fetch()
    previous = repo.convert()
    latest = repo.commit("update")
    update = repo.index.update

    def fail_publication(repository_id: int, **fields: Any) -> None:
        if fields.get("storage_format") == "bundle":
            raise sqlite3.OperationalError("injected index failure")
        update(repository_id, **fields)

    with monkeypatch.context() as patch:
        patch.setattr(repo.index, "update", fail_publication)
        with pytest.raises(sqlite3.OperationalError, match="injected index failure"):
            repo.fetch()

    manifest = json.loads(repo.paths.bundle_manifest.read_text())
    published = repo.paths.repository_dir / manifest["bundle_file"]
    assert published != previous
    assert published.is_file()
    assert previous.is_file()
    record = repo.index.get(repo.id)
    assert record["local_head_oid"] == first
    assert record["reconciliation_required"]

    repo.source.rename(repo.source.with_name("offline"))
    assert any(issue["fixed"] for issue in audit(index=repo.index, fix=True))
    record = repo.index.get(repo.id)
    assert record["local_head_oid"] == latest
    assert record["archive_path"] == str(published)
    assert not record["reconciliation_required"]
    assert previous in clean_repository_import_state(URL)
    assert not previous.exists()
    assert published.is_file()
    assert audit(index=repo.index) == []


def test_expired_conversion_claim_cannot_publish(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.commit("first")
    repo.fetch()
    restore = git_bundle.restore

    def expire_after_verification(*args: Any) -> None:
        restore(*args)
        repo.clock[0] += 121

    monkeypatch.setattr(git_bundle, "restore", expire_after_verification)
    with pytest.raises(operation.ClaimLostError):
        repo.convert()
    assert not repo.paths.bundle_manifest.exists()
    assert repo.paths.mirror_repository.is_dir()
    monkeypatch.setattr(git_bundle, "restore", restore)
    outcomes = run_worker(index=repo.index)
    assert outcomes[0]["outcome"] == "succeeded"
    assert repo.index.get(repo.id)["storage_format"] == "bundle"


@pytest.mark.parametrize(
    "damage", ["truncated", "prerequisite", "filter", "head", "path", "symlink"]
)
def test_invalid_active_bundle_never_falls_back_to_retained_mirror(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    repo.commit("first")
    repo.fetch()
    monkeypatch.setattr(git_bundle, "retire", lambda *args: [])
    bundle = repo.convert()
    manifest = json.loads(repo.paths.bundle_manifest.read_text())
    if damage == "truncated":
        bundle.write_bytes(bundle.read_bytes()[:-8])
    elif damage in {"prerequisite", "filter"}:
        contents = bundle.read_bytes()
        extra = (
            f"-{manifest['head_oid']} prerequisite\n"
            if damage == "prerequisite"
            else "@filter=blob:none\n"
        ).encode()
        bundle.write_bytes(contents.replace(b"\n\n", b"\n" + extra + b"\n", 1))
        manifest["bundle_size"] = bundle.stat().st_size
    elif damage == "head":
        manifest["head_ref"] = "refs/heads/wrong"
    elif damage == "path":
        manifest["bundle_file"] = "../outside.bundle"
    else:
        outside = bundle.with_suffix(".saved")
        bundle.rename(outside)
        bundle.symlink_to(outside)
    repo.paths.bundle_manifest.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="[Bb]undle|Unsafe archive entry"):
        repo.fetch()
    assert repo.paths.mirror_repository.is_dir()
    with pytest.raises(ValueError, match="[Bb]undle|Unsafe archive entry"):
        clean_repository_import_state(URL)
    assert repo.paths.mirror_repository.is_dir()


def test_direct_import_updates_existing_bundle(repo: Repo) -> None:
    repo.commit("first")
    repo.fetch()
    repo.convert()
    second = repo.commit("second")
    result = import_repository(URL, index=repo.index)
    assert result.archive_path.suffix == ".bundle"
    assert repo.index.get(repo.id)["local_head_oid"] == second
    assert not repo.paths.mirror_repository.exists()


def test_scheduled_fetch_updates_bundle_and_conversion_does_not_change_schedule(repo: Repo) -> None:
    repo.commit("first")
    repo.fetch()
    scheduler = Scheduler(repo.index)
    scheduler.schedule(repo.id, 60)
    with repo.index.connect() as db:
        before = dict(db.execute("SELECT * FROM schedules").fetchone())
    repo.convert()
    with repo.index.connect() as db:
        assert dict(db.execute("SELECT * FROM schedules").fetchone()) == before
    second = repo.commit("second")
    results = run_worker(index=repo.index)
    assert len(results) == 2
    assert all(row["outcome"] == "succeeded" for row in results)
    assert repo.index.get(repo.id)["local_head_oid"] == second
    assert repo.index.get(repo.id)["storage_format"] == "bundle"


def test_claim_waits_for_running_immediate_check_even_with_older_fetch(repo: Repo) -> None:
    queue = Queue(repo.index)
    scheduler = Scheduler(repo.index)
    first = queue.enqueue(repo.id, "fetch")
    running = scheduler.immediate(repo.id, "check")
    other_worker = Scheduler(Index(repo.index.path, clock=lambda: repo.clock[0]))
    assert other_worker.claim() is None
    assert (
        queue_snapshot(repo.index, section="deferred")["jobs"][0]["blocking_job_id"]
        == running["id"]
    )
    scheduler.finish(running)
    claimed = other_worker.claim()
    assert claimed is not None and claimed["id"] == first


def test_remote_head_race_preserves_published_bundle(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cache22 import git_mirror

    repo.commit("first")
    repo.fetch()
    bundle = repo.convert()
    original_manifest = repo.paths.bundle_manifest.read_bytes()
    before = repo.index.get(repo.id)
    repo.commit("second")
    remote_head = git_mirror._remote_head
    calls = 0

    def racing_head(*args: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            repo.commit("raced")
        return remote_head(*args)

    monkeypatch.setattr(git_mirror, "_remote_head", racing_head)
    with pytest.raises(RuntimeError, match="HEAD synchronization failed"):
        repo.fetch()
    assert bundle.exists()
    assert repo.paths.bundle_manifest.read_bytes() == original_manifest
    after = repo.index.get(repo.id)
    assert after["local_ref_digest"] == before["local_ref_digest"]
    assert after["last_fetched_at"] == before["last_fetched_at"]
