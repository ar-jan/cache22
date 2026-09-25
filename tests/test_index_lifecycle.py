"""Index creation belongs to entry points; access must not bootstrap a database."""

import multiprocessing
import sqlite3
import stat
from multiprocessing.synchronize import Barrier
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cache22 import index as index_module
from cache22.cli import app
from cache22.index import SCHEMA_VERSION, Index, index_path
from cache22.repository_ref import parse_repository_url
from cache22.scheduler import Scheduler


def _initialize_together(path: Path, barrier: Barrier, number: int) -> None:
    barrier.wait(timeout=10)
    index = Index.initialize(path)
    index.add(parse_repository_url(f"https://host/team/repo{number}"), path.parent)


def test_concurrent_initialization(tmp_path: Path) -> None:
    path = tmp_path / "private" / "inventory.db"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    processes = [
        context.Process(target=_initialize_together, args=(path, barrier, number))
        for number in range(4)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
    index = Index(path)
    assert len(index.list()) == 4
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with index.connect() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_existing_opens_do_not_wait_for_writer(tmp_path: Path) -> None:
    index = Index.initialize(tmp_path / "inventory.db")
    with index.transaction():
        assert Index(index.path).list() == []
        assert Index(index.path, read_only=True).list() == []
        assert Index.initialize(index.path).list() == []


def test_initialization_rolls_back_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "inventory.db"
    with monkeypatch.context() as patch:
        patch.setattr(index_module, "SCHEMA", index_module.SCHEMA + "INVALID SQL;")
        with pytest.raises(sqlite3.OperationalError, match="syntax error"):
            Index.initialize(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("SELECT name FROM sqlite_schema").fetchall() == []
    for read_only in (False, True):
        with pytest.raises(ValueError, match="version: 0"):
            Index(path, read_only=read_only)
    assert Index.initialize(path).list() == []


@pytest.mark.parametrize("corrupt", [False, True])
def test_initialization_rejects_unknown_content(tmp_path: Path, corrupt: bool) -> None:
    path = tmp_path / "inventory.db"
    if corrupt:
        path.write_bytes(b"not a SQLite database")
    else:
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE preserved(value TEXT)")
            db.execute("INSERT INTO preserved VALUES ('keep')")
    before = path.read_bytes()
    with pytest.raises(
        (ValueError, sqlite3.DatabaseError), match="nonempty version-zero|not a database"
    ):
        Index.initialize(path)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("read_only", [False, True])
def test_access_never_creates_or_recreates_database(tmp_path: Path, read_only: bool) -> None:
    path = tmp_path / "missing" / "inventory.db"
    with pytest.raises(FileNotFoundError, match="audit --fix"):
        Index(path, read_only=read_only)
    assert not path.parent.exists()
    Index.initialize(path)
    index = Index(path, read_only=read_only)
    path.unlink()
    with pytest.raises(FileNotFoundError, match="does not exist"):
        index.list()
    assert not path.exists()


@pytest.mark.parametrize("args", [["list"], ["show", "host/team/repo"], ["jobs"], ["audit"]])
def test_inspection_does_not_initialize(args: list[str]) -> None:
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert str(index_path()) in result.stderr
    assert "audit --fix" in result.stderr
    assert "Traceback" not in result.output
    assert not index_path().parent.exists()


def test_inspection_does_not_recover_expired_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    index = Index.initialize(clock=lambda: 100)
    record = index.add(parse_repository_url("https://host/team/repo"), tmp_path)
    index.update(record["id"], local_state="absent")
    Scheduler(index).immediate(record["id"], "fetch")
    with index.connect() as db:
        before = list(db.iterdump())
    for args in (["list"], ["show", record["repo_key"]], ["jobs"], ["audit"]):
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 0, result.output
    with index.connect() as db:
        assert list(db.iterdump()) == before


def test_updates_validate_fields_without_schema_introspection(tmp_path: Path) -> None:
    index = Index.initialize(tmp_path / "inventory.db", clock=lambda: 100)
    record = index.add(parse_repository_url("https://host/team/repo"), tmp_path)
    with index.transaction() as db:
        # Deny introspection while allowing updates and their normal constraints.
        db.set_authorizer(
            lambda action, arg1, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_PRAGMA and arg1 == "table_info"
                else sqlite3.SQLITE_OK
            )
        )
        index.update_in(db, record["id"], local_state="missing")
        for field in ("id", "repo_key", "last_fetched_at", "unexpected"):
            with pytest.raises(ValueError, match="Unknown or immutable"):
                index.update_in(db, record["id"], **{field: 1})
    record = index.get(record["id"])
    assert record["local_state"] == "missing"
    assert record["updated_at"] == 100
