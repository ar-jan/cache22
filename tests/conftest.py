import pytest

from cache22.index import Index


@pytest.fixture(autouse=True)
def isolated_index(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path_factory.mktemp("index")))


@pytest.fixture
def inventory_index() -> Index:
    """An explicitly requested inventory for service tests."""
    return Index.initialize()


@pytest.fixture
def mock_inventory_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy import unit tests use empty directories as mocked Git mirrors.

    Real observations are exercised by the Git integration tests, not these
    subprocess contract tests.
    """
    from cache22 import git_mirror, repo_service
    from cache22.storage import Repository

    def fields(
        repo: Repository,
        source_path: str,
        *,
        previously_ready: bool = False,
        expected_format: str = "git",
    ) -> dict[str, str]:
        paths = repo.paths
        mirror = paths.mirror_repository.exists()
        marker = paths.clone_complete_marker.exists()
        return {
            "local_state": "ready"
            if mirror and marker
            else "incomplete"
            if mirror or marker
            else "absent"
        }

    monkeypatch.setattr(Repository, "observe_local", fields)
    monkeypatch.setattr(repo_service, "publish_remote", lambda *args: None)

    monkeypatch.setattr(
        git_mirror, "_remote_head", lambda *args: git_mirror._RemoteHead(None, None)
    )
    monkeypatch.setattr(git_mirror, "_synchronize_head", lambda *args: None)
