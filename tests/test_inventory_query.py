"""The query contract shared by CLI listing, web pages, exports, and selection."""

import csv
import io
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from cache22.cli import app as cli
from cache22.index import Index
from cache22.inventory_service import (
    InventoryQuery,
    InventoryRequest,
    export_inventory,
    inventory_page,
    list_inventory,
    parse_query,
    selected_ids,
)
from cache22.manager.app import create_app
from cache22.repo_service import add_repository
from cache22.scheduler import Scheduler


def test_query_agreement_facets_and_exports(tmp_path: Path) -> None:
    app = create_app()
    index = app.state.index
    archive = tmp_path / "disconnected"
    archive.mkdir()
    rows = [
        add_repository(
            f"https://user:secret@{host}/Team/{name}", tmp_path / "disconnected", index=index
        )
        for host, name in (("a", "Same"), ("b", "Same"), ("c", "other"), ("b", "literal_%"))
    ]
    archive.rmdir()
    for row in rows[:3]:
        index.update(
            row["id"], local_state="ready", local_ref_digest="same", remote_ref_digest="same"
        )
    Scheduler(index).schedule(rows[0]["id"], 3600)
    raw = "host=a&host=b&local_state=ready&remote_status=current&queued=false&sort=project_name&limit=1&offset=1"
    request = parse_query(raw)
    snapshot = inventory_page(index, request)
    ids = [row["id"] for row in rows[:2]]
    assert [row["id"] for row in snapshot.rows] == [ids[1]]
    assert snapshot.total == 2
    assert snapshot.facets["host"] == [{"value": host, "count": 1} for host in ("a", "b", "c")]
    assert selected_ids(index, request.query) == ids
    assert [row["id"] for row in export_inventory(index, request.query)] == ids
    assert list_inventory(index, InventoryQuery(q="_%"))[0]["id"] == rows[3]["id"]
    assert len(list_inventory(index, InventoryQuery(q="SAME"))) == 2
    assert [row["id"] for row in list_inventory(index, InventoryQuery(scheduled=False))] == [
        rows[3]["id"],
        rows[1]["id"],
        rows[2]["id"],
    ]
    result = CliRunner().invoke(
        cli,
        [
            "list",
            "--host",
            "a",
            "--host",
            "b",
            "--local-state",
            "ready",
            "--remote-status",
            "current",
            "--no-queued",
            "--sort",
            "project_name",
            "--limit",
            "1",
            "--offset",
            "1",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert [row["id"] for row in json.loads(result.stdout)] == [ids[1]]
    with TestClient(app, base_url="http://localhost") as client:
        response = client.get("/?" + raw)
        assert response.status_code == 200
        assert 'value="true"' in response.text
        assert "secret" not in response.text
        assert client.post("/api/selection", json={"query": raw}).json()["ids"] == ids
        exported = client.get("/inventory.json?" + raw).json()
        assert [row["id"] for row in exported] == ids
        assert isinstance(exported[0]["scheduled"], bool)
        assert exported[0]["created_at"].endswith("Z")
        csv_rows = list(csv.DictReader(io.StringIO(client.get("/inventory.csv?" + raw).text)))
        assert [int(row["id"]) for row in csv_rows] == ids
        assert set(csv_rows[0]) == set(exported[0])
        assert "source_url" not in csv_rows[0]
        assert csv_rows[0]["last_checked_at"] == ""
        assert "No repositories on this page" in client.get("/?offset=100").text
        assert "No matching repositories" in client.get("/?q=nonexistent").text
        # Repository-controlled markup is always escaped in initial and refreshed pages.
        index.update(ids[0], display_path="a/Team/<img src=x onerror=alert(1)>")
        assert "<img src=x onerror=alert(1)>" not in client.get("/inventory/fragment").text
        assert "&lt;img src=x onerror=alert(1)&gt;" in client.get("/inventory/fragment").text


@pytest.mark.parametrize(
    "raw",
    [
        "_where=1",
        "sort=id;DROP+TABLE+repositories",
        "local_state=invalid",
        "queued=0",
        "limit=501",
        "offset=-1",
        "offset=999999999999999999999",
        "q=a&q=b",
        "_q=old",
    ],
)
def test_invalid_queries(raw: str) -> None:
    with pytest.raises(ValueError, match="Unsupported|Invalid|must be|Limit|Offset|Provide"):
        parse_query(raw)


def test_selection_limit_and_unlimited_exports(tmp_path: Path) -> None:
    index = Index.initialize(tmp_path / "index.db")
    with index.transaction() as db:
        db.execute("""WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n<10001)
          INSERT INTO repositories(repo_key,display_path,host,source_url,source_path,archive_root,created_at,updated_at)
          SELECT printf('host/repo%05d',n),printf('host/repo%05d',n),CASE WHEN n=10001 THEN 'other' ELSE 'host' END,'https://host/repo','repo','/disconnected',1,1 FROM seq""")
    assert len(selected_ids(index, InventoryQuery(host=("host",)))) == 10000
    with pytest.raises(ValueError, match="exceeds 10000"):
        selected_ids(index, InventoryQuery())
    assert len(export_inventory(index, InventoryQuery())) == 10001


def test_page_uses_one_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    index = Index.initialize(tmp_path / "index.db")
    repository = add_repository("https://host/team/repo", tmp_path, index=index)
    connect = index.connect
    changed = False

    @contextmanager
    def concurrent_connect():
        with connect() as db:

            def trace(sql):
                nonlocal changed
                if sql.startswith("SELECT * FROM inventory") and not changed:
                    changed = True
                    with connect() as writer:
                        writer.execute(
                            "UPDATE repositories SET local_state='ready' WHERE id=?",
                            (repository["id"],),
                        )

            db.set_trace_callback(trace)
            yield db

    monkeypatch.setattr(index, "connect", concurrent_connect)
    snapshot = inventory_page(index, InventoryRequest(InventoryQuery(local_state=("unknown",))))
    assert changed and snapshot.total == len(snapshot.rows) == 1
    assert snapshot.facets["local_state"] == [{"value": "unknown", "count": 1}]
    assert index.get(repository["id"])["local_state"] == "ready"
