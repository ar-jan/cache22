"""Audit and normal operations agree about history after an interruption."""

from pathlib import Path

import pytest

from cache22 import repo_service
from cache22.index import Index
from cache22.repo_audit import audit
from cache22.repo_service import add_repository, check_repository
from cache22.scheduler import Scheduler


@pytest.mark.parametrize(
    "prior,expected",
    [("absent", "absent"), ("ready", "missing"), ("missing", "missing"), ("incomplete", "absent")],
)
@pytest.mark.parametrize("interrupted", [False, True])
def test_audit_and_check_preserve_prior_readiness_independently_of_freshness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior: str, expected: str, interrupted: bool
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    index = Index.initialize(tmp_path / "index.db")
    records = [
        add_repository(f"https://host/team/{name}", tmp_path, index=index)
        for name in ("audit", "check")
    ]
    scheduler = Scheduler(index)
    for record in records:
        index.update(record["id"], local_state=prior, remote_ref_digest="known")
        if interrupted:
            job = scheduler.immediate(record["id"], "fetch")
            scheduler.interrupt(job)
            before = index.get(record["id"])
            assert before["local_state"] == prior
            assert before["reconciliation_required"]
            assert before["remote_status"] == "unknown"

    monkeypatch.setattr(repo_service, "publish_remote", lambda *args: None)
    checked = check_repository(records[1]["id"], index=index)
    audit(index=index, fix=True)
    audited = index.get(records[0]["id"])
    for record in (audited, checked):
        assert record["local_state"] == expected
        assert not record["reconciliation_required"]
        assert record["remote_status"] == "not_fetched"
    # Repeating reconciliation must not turn missing storage into never-fetched storage.
    audit(index=index, fix=True)
    assert index.get(records[0]["id"])["local_state"] == expected
