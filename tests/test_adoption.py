from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from cache22 import adoption, git_mirror
from cache22.adoption import AdoptionRequiredError
from cache22.archive_layout import ArchivePaths, archive_paths_for_repository
from cache22.archive_storage import LOCK_SIGNATURE, RepositoryBusyError, repository_operation
from cache22.cli import app
from cache22.import_service import import_repository
from cache22.import_state import clean_repository_import_state
from cache22.repository_ref import parse_repository_url

URL = "https://host/team/project"


def git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@dataclass
class Mirror:
    root: Path
    source: Path
    paths: ArchivePaths

    def commit(self, text: str) -> str:
        (self.source / "file.txt").write_text(text)
        git(self.source, "add", "file.txt")
        git(self.source, "commit", "-m", text)
        return git(self.source, "rev-parse", "HEAD")


@pytest.fixture
def mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Mirror:
    if shutil.which("git") is None:
        pytest.skip("Git is required for mirror adoption tests")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Cache22 Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.org")
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "--initial-branch=main")
    root = tmp_path / "archive"
    root.mkdir()
    paths = archive_paths_for_repository(root, parse_repository_url(URL))
    result = Mirror(root, source, paths)
    result.commit("first")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{source}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", URL)
    paths.repository_dir.mkdir(parents=True)
    git(source, "clone", "--mirror", URL, str(paths.mirror_repository))
    return result


def snapshot(directory: Path) -> dict[str, bytes | str | None]:
    return {
        str(path.relative_to(directory)): (
            str(path.readlink())
            if path.is_symlink()
            else path.read_bytes()
            if path.is_file()
            else None
        )
        for path in directory.rglob("*")
    }


@pytest.mark.parametrize("owned", [False, True])
def test_adoption_registers_without_recloning_then_import_updates(
    mirror: Mirror, owned: bool
) -> None:
    paths = mirror.paths
    if owned:
        paths.lock_file.write_bytes(LOCK_SIGNATURE)
    before = snapshot(paths.repository_dir)
    with pytest.raises(AdoptionRequiredError, match="--adopt") as error:
        import_repository(URL, mirror.root, "git")
    assert error.value.archive_dir == mirror.root
    assert snapshot(paths.repository_dir) == before
    inode = paths.mirror_repository.stat().st_ino
    new_commit = mirror.commit("second")
    result = import_repository(URL, mirror.root, "git", adopt=True)
    assert "adopted Git mirror" in result.info_messages[0]
    assert paths.mirror_repository.stat().st_ino == inode
    assert paths.lock_file.read_bytes() == LOCK_SIGNATURE
    assert paths.clone_complete_marker.read_text() == "complete\n"
    assert json.loads(paths.source_file.read_text()) == {"source_path": "host/team/project"}
    assert git(paths.mirror_repository, "rev-parse", "HEAD") == new_commit
    lock_inode = paths.lock_file.stat().st_ino

    git(mirror.source, "branch", "later")
    git(mirror.source, "tag", "v1")
    import_repository(URL, mirror.root, "git")
    assert git(paths.mirror_repository, "show-ref") == git(mirror.source, "show-ref")
    assert paths.lock_file.stat().st_ino == lock_inode

    # Force a tag and a branch backwards, remove a ref, and add a new branch.
    first = git(mirror.source, "rev-parse", "HEAD^")
    git(mirror.source, "update-ref", "refs/heads/main", first)
    git(mirror.source, "tag", "--force", "v1", first)
    git(mirror.source, "branch", "--delete", "--force", "later")
    git(mirror.source, "branch", "added")
    import_repository(URL, mirror.root, "git")
    assert git(paths.mirror_repository, "show-ref") == git(mirror.source, "show-ref")
    git(mirror.source, "tag", "--delete", "v1")
    import_repository(URL, mirror.root, "git")
    assert git(paths.mirror_repository, "show-ref") == git(mirror.source, "show-ref")


@pytest.mark.parametrize(
    "problem",
    [
        "origin",
        "multiple_origins",
        "refspec",
        "bare",
        "corruption",
        "partial",
        "included_partial",
        "shallow",
        "alternates",
        "symlink",
        "namespace",
        "metadata",
        "binding",
        "marker",
        "stage",
    ],
)
def test_invalid_adoption_never_changes_existing_data(mirror: Mirror, problem: str) -> None:
    paths = mirror.paths
    repo = paths.mirror_repository
    if problem == "origin":
        git(repo, "config", "remote.origin.url", "https://host/other/project")
    elif problem == "multiple_origins":
        git(repo, "config", "--add", "remote.origin.url", URL)
    elif problem == "refspec":
        git(repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*")
    elif problem == "bare":
        git(repo, "config", "core.bare", "false")
    elif problem == "corruption":
        blob = git(repo, "rev-parse", "HEAD:file.txt")
        obj = repo / "objects" / blob[:2] / blob[2:]
        obj.unlink()  # Local clones can hardlink read-only objects to the source.
        obj.write_bytes(b"corrupt object")
    elif problem == "partial":
        git(repo, "config", "remote.origin.promisor", "true")
    elif problem == "included_partial":
        config = mirror.root / "partial.config"
        config.write_text('[remote "origin"]\n    promisor = true\n')
        git(repo, "config", "include.path", str(config))
    elif problem == "shallow":
        (repo / "shallow").write_text(git(repo, "rev-parse", "HEAD") + "\n")
    elif problem == "alternates":
        (repo / "objects" / "info" / "alternates").write_text(str(mirror.source / ".git/objects"))
    elif problem == "symlink":
        (repo / "refs" / "external").symlink_to(mirror.source)
    elif problem == "namespace":
        (repo / "child").mkdir()
        (repo / "child" / ".lock").write_bytes(LOCK_SIGNATURE)
    elif problem == "metadata":
        paths.source_file.write_text("{}")
    elif problem == "binding":
        paths.source_file.write_text('{"source_path":"host/other/project"}')
    elif problem == "marker":
        paths.clone_complete_marker.write_text("unfinished")
    elif problem == "stage":
        paths.temp_dir.mkdir()
    before = snapshot(paths.repository_dir)
    with pytest.raises(ValueError):
        import_repository(URL, mirror.root, "git", adopt=True)
    assert snapshot(paths.repository_dir) == before
    assert not paths.lock_file.exists()


@pytest.mark.parametrize("case_sensitive", [False, True])
def test_adoption_matches_transport_but_preserves_source_casing(
    mirror: Mirror, monkeypatch: pytest.MonkeyPatch, case_sensitive: bool
) -> None:
    git(mirror.paths.mirror_repository, "config", "remote.origin.url", "git@HOST:Team/Project.git")
    with pytest.raises(ValueError, match="origin host/Team/Project; requested host/team/project"):
        import_repository(URL, mirror.root, "git", adopt=True)
    url = "https://host/Team/Project" if case_sensitive else URL
    if case_sensitive:
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", url)
    else:
        git(
            mirror.paths.mirror_repository,
            "config",
            "remote.origin.url",
            "git@HOST:team/project.git",
        )
    import_repository(url, mirror.root, "git", adopt=True, case_sensitive=case_sensitive)
    # A repeated explicit adoption verifies again without replacing the lock.
    inode = mirror.paths.lock_file.stat().st_ino
    import_repository(url, mirror.root, "git", adopt=True, case_sensitive=case_sensitive)
    assert mirror.paths.lock_file.stat().st_ino == inode


def test_empty_mirror_can_be_adopted(mirror: Mirror) -> None:
    for directory in (mirror.source, mirror.paths.mirror_repository):
        git(directory, "update-ref", "-d", "refs/heads/main")
    assert (
        import_repository(URL, mirror.root, "git", adopt=True).archive_path
        == mirror.paths.mirror_repository
    )


def test_git_environment_cannot_redirect_adoption_or_updates(
    mirror: Mirror, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest = mirror.commit("update the mirror only")
    source_before = snapshot(mirror.source)
    monkeypatch.setenv("GIT_DIR", str(mirror.source / ".git"))
    monkeypatch.setenv("GIT_COMMON_DIR", str(mirror.source / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(mirror.source))
    import_repository(URL, mirror.root, "git", adopt=True)
    assert snapshot(mirror.source) == source_before
    for key in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"):
        monkeypatch.delenv(key)
    assert git(mirror.paths.mirror_repository, "rev-parse", "HEAD") == latest


def test_adoption_fetch_failure_keeps_initialization_for_retry(
    mirror: Mirror, monkeypatch: pytest.MonkeyPatch
) -> None:
    refs = git(mirror.paths.mirror_repository, "show-ref")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{mirror.root / 'missing'}.insteadOf")
    with pytest.raises(RuntimeError, match="Remote HEAD discovery failed"):
        import_repository(URL, mirror.root, "git", adopt=True)
    assert mirror.paths.lock_file.exists()
    assert mirror.paths.source_file.exists()
    assert mirror.paths.clone_complete_marker.exists()
    assert clean_repository_import_state(URL, (mirror.root,)) == ()
    assert git(mirror.paths.mirror_repository, "show-ref") == refs
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{mirror.source}.insteadOf")
    latest = mirror.commit("retry")
    import_repository(URL, mirror.root, "git")
    assert git(mirror.paths.mirror_repository, "rev-parse", "HEAD") == latest


def test_failed_fetch_updates_no_refs(mirror: Mirror) -> None:
    import_repository(URL, mirror.root, "git", adopt=True)
    refs = git(mirror.paths.mirror_repository, "show-ref")
    mirror.commit("atomic update")
    git(mirror.source, "branch", "second")
    ref_lock = mirror.paths.mirror_repository / "refs/heads/second.lock"
    ref_lock.touch()
    with pytest.raises(RuntimeError, match="git fetch failed"):
        import_repository(URL, mirror.root, "git")
    assert git(mirror.paths.mirror_repository, "show-ref") == refs
    ref_lock.unlink()
    import_repository(URL, mirror.root, "git")
    assert git(mirror.paths.mirror_repository, "show-ref") == git(mirror.source, "show-ref")


@pytest.mark.parametrize("owned,failure", [(False, "source"), (True, "source"), (False, "lock")])
def test_interrupted_initialization_can_be_retried(
    mirror: Mirror, owned: bool, failure: str
) -> None:
    if owned:
        mirror.paths.lock_file.write_bytes(LOCK_SIGNATURE)
    target = (
        "cache22.archive_storage.RepositoryStorage.bind_source"
        if failure == "source"
        else "cache22.archive_storage._publish_lock"
    )
    with (
        patch(target, side_effect=OSError("publication interrupted")),
        pytest.raises(OSError, match="publication interrupted"),
    ):
        import_repository(URL, mirror.root, "git", adopt=True)
    assert mirror.paths.clone_complete_marker.exists()
    assert clean_repository_import_state(URL, (mirror.root,)) == ()
    assert (
        import_repository(URL, mirror.root, "git", adopt=True).archive_path
        == mirror.paths.mirror_repository
    )


def test_adoption_obeys_existing_lock_and_blocks_nested_imports(mirror: Mirror) -> None:
    mirror.paths.lock_file.write_bytes(LOCK_SIGNATURE)
    with repository_operation(mirror.root, mirror.paths):
        with pytest.raises(RepositoryBusyError):
            import_repository(URL, mirror.root, "git", adopt=True)
        with pytest.raises(RepositoryBusyError):
            clean_repository_import_state(URL, (mirror.root,))
    import_repository(URL, mirror.root, "git", adopt=True)
    with pytest.raises(ValueError, match="Repository path conflict"):
        import_repository(URL + "/child", mirror.root, "git", adopt=True)


def test_adoption_reservation_allows_sibling_work_and_rejects_conflicts(
    mirror: Mirror, monkeypatch: pytest.MonkeyPatch
) -> None:
    verified, release = Event(), Event()
    verify = adoption._verify_mirror
    sibling_url = "https://host/team/sibling"
    sibling_paths = archive_paths_for_repository(mirror.root, parse_repository_url(sibling_url))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", f"url.{mirror.source}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", sibling_url)

    def paused_verify(path: Path, source_path: str) -> None:
        verify(path, source_path)
        verified.set()
        if not release.wait(10):
            raise RuntimeError("Timed out waiting to publish adoption metadata")

    def sibling() -> None:
        import_repository(sibling_url, mirror.root, "git")
        sibling_paths.temp_dir.mkdir()
        assert clean_repository_import_state(sibling_url, (mirror.root,)) == (
            sibling_paths.temp_dir,
        )

    with (
        patch("cache22.adoption._verify_mirror", side_effect=paused_verify),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        adopting = pool.submit(import_repository, URL, mirror.root, "git", adopt=True)
        try:
            assert verified.wait(10)
            assert not mirror.paths.lock_file.exists()
            pool.submit(sibling).result(timeout=5)
            with pytest.raises(RepositoryBusyError):
                clean_repository_import_state(URL, (mirror.root,))
            for url in (URL, URL + "/child"):
                with pytest.raises(RepositoryBusyError):
                    import_repository(url, mirror.root, "git")
        finally:
            release.set()
        adopting.result(timeout=10)
    assert mirror.paths.clone_complete_marker.exists()
    assert not (mirror.paths.repository_dir / "child").exists()


@pytest.mark.parametrize(
    "answer,success", [("y\n", True), ("n\n", False), ("\n", False), ("", False)]
)
@pytest.mark.parametrize("owned", [False, True])
def test_cli_offers_adoption_and_releases_locks_before_prompt(
    mirror: Mirror, answer: str, success: bool, owned: bool
) -> None:
    if owned:
        mirror.paths.lock_file.write_bytes(LOCK_SIGNATURE)

    def terminal() -> bool:
        # isatty is consulted only after the failed service call unwinds.
        fd = os.open(mirror.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        if owned:
            with repository_operation(mirror.root, mirror.paths):
                pass
        return True

    before = snapshot(mirror.paths.repository_dir)
    with (
        patch("cache22.import_service.default_archive_dir", return_value=mirror.root),
        patch("cache22.import_service.default_archive_type", return_value="git"),
        patch("cache22.cli._is_interactive", side_effect=terminal),
    ):
        result = CliRunner().invoke(app, ["import", "repo", URL], input=answer)
    assert (result.exit_code == 0) == success, result.output
    assert "[y/N]" in result.stderr
    assert "Verify and adopt" not in result.stdout
    if success:
        assert mirror.paths.source_file.exists()
    else:
        assert snapshot(mirror.paths.repository_dir) == before


def test_cli_noninteractive_requires_flag_and_explicit_adoption_never_prompts(
    mirror: Mirror,
) -> None:
    with (
        patch("cache22.import_service.default_archive_dir", return_value=mirror.root),
        patch("cache22.import_service.default_archive_type", return_value="git"),
        patch("cache22.cli.typer.confirm", side_effect=AssertionError("must not prompt")),
    ):
        runner = CliRunner()
        result = runner.invoke(app, ["import", "repo", URL])
        assert result.exit_code == 1
        assert "--adopt" in result.stderr
        result = runner.invoke(app, ["import", "repo", URL, "--adopt"])
        assert result.exit_code == 0, result.output


@pytest.mark.parametrize("problem", ["namespace", "symlink", "metadata"])
def test_ineligible_conflicts_never_offer_adoption(mirror: Mirror, problem: str) -> None:
    if problem == "namespace":
        (mirror.paths.repository_dir / "child").mkdir()
    elif problem == "symlink":
        mirror.paths.source_file.symlink_to(mirror.source / "file.txt")
    else:
        mirror.paths.source_file.write_text("malformed")
    before = snapshot(mirror.paths.repository_dir)
    with (
        patch("cache22.import_service.default_archive_dir", return_value=mirror.root),
        patch("cache22.import_service.default_archive_type", return_value="git"),
        patch("cache22.cli._is_interactive", return_value=True),
        patch("cache22.cli.typer.confirm", side_effect=AssertionError("must not prompt")),
    ):
        result = CliRunner().invoke(app, ["import", "repo", URL])
    assert result.exit_code == 1
    assert "[y/N]" not in result.stderr
    assert not isinstance(result.exception, AssertionError)
    assert snapshot(mirror.paths.repository_dir) == before


def test_prompt_retry_pins_configuration_and_revalidates_state(mirror: Mirror) -> None:
    def confirm(*args: object, **kwargs: object) -> bool:
        mirror.paths.source_file.write_text('{"source_path":"host/other/project"}')
        return True

    with (
        patch(
            "cache22.import_service.default_archive_dir", side_effect=[mirror.root, AssertionError]
        ),
        patch("cache22.import_service.default_archive_type", side_effect=["git", AssertionError]),
        patch("cache22.cli._is_interactive", return_value=True),
        patch("cache22.cli.typer.confirm", side_effect=confirm) as prompt,
    ):
        result = CliRunner().invoke(app, ["import", "repo", URL])
    assert result.exit_code == 1
    assert "source conflict" in result.stderr
    assert prompt.call_count == 1
    assert not mirror.paths.lock_file.exists()


def test_fossil_rejects_adoption_without_changes_or_prompt(mirror: Mirror) -> None:
    before = snapshot(mirror.paths.repository_dir)
    with (
        patch("cache22.import_service.default_archive_dir", return_value=mirror.root),
        patch("cache22.import_service.default_archive_type", return_value="fossil"),
        patch("cache22.cli.typer.confirm", side_effect=AssertionError("must not prompt")),
    ):
        result = CliRunner().invoke(app, ["import", "repo", URL, "--adopt"])
    assert result.exit_code == 1
    assert "only supported in Git" in result.stderr
    assert snapshot(mirror.paths.repository_dir) == before


@pytest.mark.parametrize("initialized", [False, True])
@pytest.mark.parametrize(
    "key",
    [
        "core.sshcommand",
        "credential.helper",
        "core.hookspath",
        "include.path",
        "url.ssh://unexpected/.insteadof",
        "remote.origin.uploadpack",
    ],
)
def test_unsafe_local_configuration_is_rejected_without_execution_or_changes(
    mirror: Mirror, initialized: bool, key: str
) -> None:
    if initialized:
        import_repository(URL, mirror.root, "git", adopt=True)
    sentinel = mirror.root / "command-executed"
    command = f"touch {shlex.quote(str(sentinel))}; exit 1"
    git(mirror.paths.mirror_repository, "config", key, command)
    url = "git@host:team/project" if key == "core.sshcommand" else URL
    if url != URL:
        git(mirror.paths.mirror_repository, "config", "remote.origin.url", url)
    before = snapshot(mirror.paths.repository_dir)
    with pytest.raises(
        (ValueError, RuntimeError), match="Unsupported repository configuration key"
    ) as error:
        import_repository(url, mirror.root, "git", adopt=not initialized)
    assert command not in str(error.value)
    assert not sentinel.exists()
    assert snapshot(mirror.paths.repository_dir) == before


def test_worktree_configuration_is_rejected_before_adoption(mirror: Mirror) -> None:
    (mirror.paths.mirror_repository / "config.worktree").write_text("[core]\nsshCommand = false\n")
    before = snapshot(mirror.paths.repository_dir)
    with pytest.raises(ValueError, match="worktree configuration"):
        import_repository(URL, mirror.root, "git", adopt=True)
    assert snapshot(mirror.paths.repository_dir) == before


def test_repository_hooks_are_disabled_during_adoption_and_updates(mirror: Mirror) -> None:
    sentinel = mirror.root / "hook-executed"
    hook = mirror.paths.mirror_repository / "hooks/reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(sentinel))}\n")
    hook.chmod(0o755)
    mirror.commit("adoption update")
    import_repository(URL, mirror.root, "git", adopt=True)
    latest = mirror.commit("ordinary update")
    import_repository(URL, mirror.root, "git")
    assert git(mirror.paths.mirror_repository, "rev-parse", "HEAD") == latest
    assert not sentinel.exists()


def test_trusted_user_ssh_configuration_remains_available(
    mirror: Mirror, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = mirror.root / "trusted-ssh-used"
    ssh = mirror.root / "trusted-ssh"
    ssh.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(sentinel))}\n"
        f"exec git-upload-pack {shlex.quote(str(mirror.source))}\n"
    )
    ssh.chmod(0o755)
    user_config = mirror.root / "user.config"
    git(mirror.source, "config", "--file", str(user_config), "core.sshcommand", str(ssh))
    git(mirror.source, "config", "--file", str(user_config), "ssh.variant", "simple")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(user_config))
    url = "git@host:team/project"
    git(mirror.paths.mirror_repository, "config", "remote.origin.url", url)
    latest = mirror.commit("authenticated update")
    import_repository(url, mirror.root, "git", adopt=True)
    assert sentinel.exists()
    assert git(mirror.paths.mirror_repository, "rev-parse", "HEAD") == latest


@pytest.mark.parametrize("owned,bound", [(False, False), (True, False), (False, True)])
@pytest.mark.parametrize("sidecar", ["fossil_repository", "git_marks", "fossil_marks"])
def test_unverified_fossil_artifacts_are_not_claimed(
    mirror: Mirror, owned: bool, bound: bool, sidecar: str
) -> None:
    paths = mirror.paths
    if owned:
        paths.lock_file.write_bytes(LOCK_SIGNATURE)
    if bound:
        paths.source_file.write_text('{"source_path":"host/team/project"}')
    getattr(paths, sidecar).write_text("unverified sidecar")
    before = snapshot(paths.repository_dir)
    for adopt in (False, True):
        with pytest.raises(ValueError, match="cannot verify Fossil artifacts"):
            import_repository(URL, mirror.root, "git", adopt=adopt)
    assert snapshot(paths.repository_dir) == before


def test_managed_fossil_artifacts_are_preserved_during_readoption(mirror: Mirror) -> None:
    paths = mirror.paths
    paths.lock_file.write_bytes(LOCK_SIGNATURE)
    paths.source_file.write_text('{"source_path":"host/team/project"}')
    for path in (paths.fossil_repository, paths.git_marks, paths.fossil_marks):
        path.write_text("managed sidecar")
    import_repository(URL, mirror.root, "git", adopt=True)
    result = import_repository(URL, mirror.root, "fossil")
    assert result.archive_path == paths.fossil_repository
    for path in (paths.fossil_repository, paths.git_marks, paths.fossil_marks):
        assert path.read_text() == "managed sidecar"


def test_default_branch_rename_updates_head_and_keeps_mirror_cloneable(mirror: Mirror) -> None:
    import_repository(URL, mirror.root, "git", adopt=True)
    git(mirror.source, "branch", "-m", "renamed")
    latest = mirror.commit("new default branch")
    import_repository(URL, mirror.root, "git")
    assert git(mirror.paths.mirror_repository, "symbolic-ref", "HEAD") == "refs/heads/renamed"
    assert git(mirror.paths.mirror_repository, "rev-parse", "HEAD") == latest
    checkout = mirror.root / "checkout"
    git(mirror.root, "clone", str(mirror.paths.mirror_repository), str(checkout))
    assert (checkout / "file.txt").read_text() == "new default branch"


def test_detached_remote_head_is_fetched_even_without_a_ref(mirror: Mirror) -> None:
    import_repository(URL, mirror.root, "git", adopt=True)
    git(mirror.source, "checkout", "--detach")
    detached = mirror.commit("unreferenced HEAD commit")
    import_repository(URL, mirror.root, "git")
    assert (mirror.paths.mirror_repository / "HEAD").read_text().strip() == detached
    assert git(mirror.paths.mirror_repository, "rev-parse", "HEAD:file.txt") == git(
        mirror.source, "rev-parse", "HEAD:file.txt"
    )
    # When the source becomes empty, a previous detached HEAD becomes unborn.
    git(mirror.source, "update-ref", "-d", "refs/heads/main")
    git(mirror.source, "symbolic-ref", "HEAD", "refs/heads/unborn")
    import_repository(URL, mirror.root, "git")
    assert git(mirror.paths.mirror_repository, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert not git(mirror.paths.mirror_repository, "for-each-ref")


def test_nonempty_remote_without_advertised_head_is_an_error(mirror: Mirror) -> None:
    import_repository(URL, mirror.root, "git", adopt=True)
    git(mirror.source, "symbolic-ref", "HEAD", "refs/heads/missing")
    with pytest.raises(RuntimeError, match="HEAD synchronization failed"):
        import_repository(URL, mirror.root, "git")
    assert mirror.paths.clone_complete_marker.exists()


def test_head_publication_failure_retains_fetched_refs_for_retry(mirror: Mirror) -> None:
    import_repository(URL, mirror.root, "git", adopt=True)
    git(mirror.source, "branch", "-m", "renamed")
    head_lock = mirror.paths.mirror_repository / "HEAD.lock"
    synchronize = git_mirror._synchronize_head

    def locked_head(git: Path, path: Path, head: git_mirror._RemoteHead) -> None:
        head_lock.touch()  # Introduce contention after the ref transaction succeeds.
        synchronize(git, path, head)

    with (
        patch("cache22.git_mirror._synchronize_head", side_effect=locked_head),
        pytest.raises(RuntimeError, match="refs were fetched, but HEAD synchronization failed"),
    ):
        import_repository(URL, mirror.root, "git")
    assert git(mirror.paths.mirror_repository, "show-ref") == git(mirror.source, "show-ref")
    head_lock.unlink()
    import_repository(URL, mirror.root, "git")
    assert git(mirror.paths.mirror_repository, "symbolic-ref", "HEAD") == "refs/heads/renamed"


def test_default_branch_change_during_fetch_is_retryable(mirror: Mirror) -> None:
    import_repository(URL, mirror.root, "git", adopt=True)
    discover = git_mirror._remote_head

    def changing_remote(executable: Path, path: Path, url: str) -> git_mirror._RemoteHead:
        head = discover(executable, path, url)
        git(mirror.source, "branch", "-m", "renamed")
        return head

    with (
        patch("cache22.git_mirror._remote_head", side_effect=changing_remote),
        pytest.raises(RuntimeError, match="HEAD synchronization failed"),
    ):
        import_repository(URL, mirror.root, "git")
    assert mirror.paths.clone_complete_marker.exists()
    import_repository(URL, mirror.root, "git")
    assert git(mirror.paths.mirror_repository, "symbolic-ref", "HEAD") == "refs/heads/renamed"
