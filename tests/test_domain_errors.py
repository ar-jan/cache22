"""Expected failures retain scheduling policy; unexpected defects remain visible."""

from pathlib import Path

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from cache22 import manager_service, repo_service
from cache22.cli import app
from cache22.errors import (
    DomainError,
    RepositoryBusyError,
    StorageUnavailableError,
    StructuralError,
    TransportError,
)
from cache22.git_layout import validate_git_mirror_layout
from cache22.index import Index
from cache22.job_queue import Queue
from cache22.manager.app import create_app
from cache22.repository_ref import parse_repository_url
from cache22.scheduler import Scheduler
from cache22.storage import Repository
from cache22.worker import run_worker


@pytest.mark.parametrize(
    "error,state,retries,delay,blocked",
    [
        (RepositoryBusyError, "pending", 0, 60, 0),
        (StorageUnavailableError, "pending", 0, 60, 0),
        (TransportError, "pending", 1, 60, 0),
        (StructuralError, "failed", 0, 0, 1),
    ],
)
def test_domain_failures_keep_scheduling_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: type[DomainError],
    state: str,
    retries: int,
    delay: int,
    blocked: int,
) -> None:
    index = Index.initialize(tmp_path / "index.db", clock=lambda: 1000)
    record = repo_service.add_repository("https://host/team/repo", tmp_path, index=index)
    scheduler = Scheduler(index)
    scheduler.schedule(record["id"], 3600)
    job = scheduler.immediate(record["id"], "check")

    def fail(*args: object) -> None:
        # An explicit category takes precedence over a misleading chained cause.
        raise error("failed https://user:secret@host/repo") from FileNotFoundError("cause")

    monkeypatch.setattr(repo_service, "check_locked", fail)
    with pytest.raises(error):
        repo_service.execute_job(index, job)
    current = Queue(index).list(record["id"])[0]
    assert current["state"] == state
    assert current["retry_count"] == retries
    assert current["due_at"] == 1000 + delay
    assert current["attempts"][0]["error_category"] == error.category
    assert "secret" not in current["attempts"][0]["error"]
    with index.connect() as db:
        assert db.execute("SELECT blocked FROM schedules").fetchone()[0] == blocked


@pytest.mark.parametrize("defect", [RuntimeError, ValueError, TypeError])
def test_unexpected_defect_is_recorded_and_stops_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: type[Exception]
) -> None:
    index = Index.initialize(tmp_path / "index.db")
    record = repo_service.add_repository("https://host/team/repo", tmp_path, index=index)
    scheduler = Scheduler(index)
    scheduler.schedule(record["id"], 3600)
    Queue(index).enqueue(record["id"], "check")

    def fail(*args: object) -> None:
        raise defect("bug https://user:secret@host/repo")

    monkeypatch.setattr(repo_service, "check_locked", fail)
    with pytest.raises(defect, match="bug"):
        run_worker(index=index)
    current = Queue(index).list(record["id"])[0]
    assert current["state"] == "failed" and current["retry_count"] == 0
    assert current["attempts"][0]["error_category"] == "unexpected"
    assert "secret" not in current["attempts"][0]["error"]
    with index.connect() as db:
        assert db.execute("SELECT blocked FROM schedules").fetchone()[0] == 0
        assert db.execute("SELECT stopped_at FROM workers").fetchone()[0] is not None


def test_storage_boundary_distinguishes_unavailable_from_invalid_layout(tmp_path: Path) -> None:
    ref = parse_repository_url("https://host/team/repo")
    with pytest.raises(StorageUnavailableError), Repository.open(tmp_path / "missing", ref):
        pass
    # Missing content inside an existing mirror is structural, even with an OS cause.
    mirror = tmp_path / "repo.git"
    mirror.mkdir()
    with pytest.raises(StructuralError, match="missing") as caught:
        validate_git_mirror_layout(mirror)
    assert isinstance(caught.value.__cause__, FileNotFoundError)


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_domain_error_output_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, as_json: bool
) -> None:
    index = Index.initialize()
    record = repo_service.add_repository("https://host/team/repo", tmp_path, index=index)

    def fail(*args: object) -> None:
        raise StructuralError("failure https://user:secret@host/repo")

    monkeypatch.setattr(repo_service, "check_locked", fail)
    result = CliRunner().invoke(
        app, ["check", record["repo_key"], *(["--json"] if as_json else [])]
    )
    assert result.exit_code == 1
    assert "failure" in result.output and "secret" not in result.output


@pytest.mark.parametrize("defect", [RuntimeError, ValueError, TypeError])
def test_cli_does_not_render_programming_errors_as_expected_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: type[Exception]
) -> None:
    index = Index.initialize()
    record = repo_service.add_repository("https://host/team/repo", tmp_path, index=index)

    def fail(*args: object) -> None:
        raise defect("unexpected defect")

    monkeypatch.setattr(repo_service, "check_locked", fail)
    result = CliRunner().invoke(app, ["check", record["repo_key"]])
    assert isinstance(result.exception, defect)
    assert "unexpected defect" not in result.output


def test_web_renders_domain_errors_but_propagates_defects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(create_app(tmp_path / "index.db")) as client:

        def expected(*args: object, **kwargs: object) -> None:
            raise RepositoryBusyError("busy https://user:secret@host/repo")

        monkeypatch.setattr(manager_service, "detail", expected)
        response = client.get("/api/detail?id=1", headers={"host": "localhost"})
        assert response.status_code == 400
        assert "busy" in response.json()["error"] and "secret" not in response.text

        def unexpected(*args: object, **kwargs: object) -> None:
            raise ValueError("unexpected defect")

        monkeypatch.setattr(manager_service, "detail", unexpected)
        with pytest.raises(ValueError, match="unexpected defect"):
            client.get("/api/detail?id=1", headers={"host": "localhost"})
