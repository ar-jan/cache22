from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import sqlite3
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
from cache22.config import add_archive_dir
from cache22.import_service import import_repository
from cache22.import_state import clean_repository_import_state
from cache22.index import Index
from cache22.repo_audit import audit
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
        import_repository(URL, mirror.root)
    assert error.value.archive_dir == mirror.root
    assert snapshot(paths.repository_dir) == before
    inode = paths.mirror_repository.stat().st_ino
    new_commit = mirror.commit("second")
    result = import_repository(URL, mirror.root, adopt=True)
    assert "adopted Git mirror" in result.info_messages[0]
    assert paths.mirror_repository.stat().st_ino == inode
    assert paths.lock_file.read_bytes() == LOCK_SIGNATURE
    assert paths.clone_complete_marker.read_text() == "complete\n"
    assert json.loads(paths.source_file.read_text()) == {"source_path": "host/team/project"}
    assert git(paths.mirror_repository, "rev-parse", "HEAD") == new_commit
    lock_inode = paths.lock_file.stat().st_ino

    git(mirror.source, "branch", "later")
    git(mirror.source, "tag", "v1")
    import_repository(URL, mirror.root)
    assert git(paths.mirror_repository, "show-ref") == git(mirror.source, "show-ref")
    assert paths.lock_file.stat().st_ino == lock_inode

    # Force a tag and a branch backwards, remove a ref, and add a new branch.
    first = git(mirror.source, "rev-parse", "HEAD^")
    git(mirror.source, "update-ref", "refs/heads/main", first)
    git(mirror.source, "tag", "--force", "v1", first)
    git(mirror.source, "branch", "--delete", "--force", "later")
    git(mirror.source, "branch", "added")
    import_repository(URL, mirror.root)
    assert git(paths.mirror_repository, "show-ref") == git(mirror.source, "show-ref")
    git(mirror.source, "tag", "--delete", "v1")
    import_repository(URL, mirror.root)
    assert git(paths.mirror_repository, "show-ref") == git(mirror.source, "show-ref")


@pytest.mark.parametrize(
    ("problem", "message"),
    [
        ("origin", "Repository source conflict: origin"),
        ("multiple_origins", "Duplicate repository configuration key: remote.origin.url"),
        ("refspec", "Origin must be configured as a full Git mirror"),
        ("bare", "Adoption and updates require a bare Git mirror"),
        ("corruption", "(fsck)"),
        ("partial", "Unsupported repository configuration key: remote.origin.promisor"),
        ("included_partial", "Unsupported repository configuration key: include.path"),
        ("shallow", "Expected a complete, self-contained Git mirror"),
        ("alternates", "Expected a complete, self-contained Git mirror"),
        ("symlink", "Unsafe Git mirror entry"),
        ("namespace", "nested repository boundary"),
        ("metadata", "Malformed source metadata"),
        ("binding", "Repository source conflict: stored"),
        ("marker", "Malformed clone completion marker"),
        ("stage", "unexpected entries: .cache22-bundle"),
    ],
)
def test_invalid_adoption_never_changes_existing_data(
    mirror: Mirror, problem: str, message: str
) -> None:
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
        paths.bundle_staging.mkdir()
    before = snapshot(paths.repository_dir)
    with pytest.raises(ValueError, match=re.escape(message)):
        import_repository(URL, mirror.root, adopt=True)
    assert snapshot(paths.repository_dir) == before
    assert not paths.lock_file.exists()


@pytest.mark.parametrize("case_sensitive", [False, True])
def test_adoption_matches_transport_but_preserves_source_casing(
    mirror: Mirror, monkeypatch: pytest.MonkeyPatch, case_sensitive: bool
) -> None:
    git(mirror.paths.mirror_repository, "config", "remote.origin.url", "git@HOST:Team/Project.git")
    with pytest.raises(ValueError, match="origin host/Team/Project; requested host/team/project"):
        import_repository(URL, mirror.root, adopt=True)
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
    import_repository(url, mirror.root, adopt=True, case_sensitive=case_sensitive)
    # A repeated explicit adoption verifies again without replacing the lock.
    inode = mirror.paths.lock_file.stat().st_ino
    import_repository(url, mirror.root, adopt=True, case_sensitive=case_sensitive)
    assert mirror.paths.lock_file.stat().st_ino == inode


def test_empty_mirror_can_be_adopted(mirror: Mirror) -> None:
    for directory in (mirror.source, mirror.paths.mirror_repository):
        git(directory, "update-ref", "-d", "refs/heads/main")
    assert (
        import_repository(URL, mirror.root, adopt=True).archive_path
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
    import_repository(URL, mirror.root, adopt=True)
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
        import_repository(URL, mirror.root, adopt=True)
    assert mirror.paths.lock_file.exists()
    assert mirror.paths.source_file.exists()
    assert mirror.paths.clone_complete_marker.exists()
    assert clean_repository_import_state(URL, (mirror.root,)) == ()
    assert git(mirror.paths.mirror_repository, "show-ref") == refs
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{mirror.source}.insteadOf")
    latest = mirror.commit("retry")
    import_repository(URL, mirror.root)
    assert git(mirror.paths.mirror_repository, "rev-parse", "HEAD") == latest


def test_failed_fetch_updates_no_refs(mirror: Mirror) -> None:
    import_repository(URL, mirror.root, adopt=True)
    refs = git(mirror.paths.mirror_repository, "show-ref")
    mirror.commit("atomic update")
    git(mirror.source, "branch", "second")
    ref_lock = mirror.paths.mirror_repository / "refs/heads/second.lock"
    ref_lock.touch()
    with pytest.raises(RuntimeError, match="git fetch failed"):
        import_repository(URL, mirror.root)
    assert git(mirror.paths.mirror_repository, "show-ref") == refs
    ref_lock.unlink()
    import_repository(URL, mirror.root)
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
        import_repository(URL, mirror.root, adopt=True)
    assert mirror.paths.clone_complete_marker.exists()
    assert clean_repository_import_state(URL, (mirror.root,)) == ()
    assert (
        import_repository(URL, mirror.root, adopt=True).archive_path
        == mirror.paths.mirror_repository
    )


def test_adoption_obeys_existing_lock_and_blocks_nested_imports(mirror: Mirror) -> None:
    mirror.paths.lock_file.write_bytes(LOCK_SIGNATURE)
    with repository_operation(mirror.root, mirror.paths):
        with pytest.raises(RepositoryBusyError):
            import_repository(URL, mirror.root, adopt=True)
        with pytest.raises(RepositoryBusyError):
            clean_repository_import_state(URL, (mirror.root,))
    import_repository(URL, mirror.root, adopt=True)
    with pytest.raises(ValueError, match="Repository path conflict"):
        import_repository(URL + "/child", mirror.root, adopt=True)


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
        import_repository(sibling_url, mirror.root)
        sibling_paths.bundle_staging.mkdir()
        assert clean_repository_import_state(sibling_url, (mirror.root,)) == (
            sibling_paths.bundle_staging,
        )

    with (
        patch("cache22.adoption._verify_mirror", side_effect=paused_verify),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        adopting = pool.submit(import_repository, URL, mirror.root, adopt=True)
        try:
            assert verified.wait(10)
            assert not mirror.paths.lock_file.exists()
            pool.submit(sibling).result(timeout=5)
            with pytest.raises(RepositoryBusyError):
                clean_repository_import_state(URL, (mirror.root,))
            for url in (URL, URL + "/child"):
                with pytest.raises(RepositoryBusyError):
                    import_repository(url, mirror.root)
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
        patch("cache22.repo_cli._is_interactive", side_effect=terminal),
    ):
        result = CliRunner().invoke(app, ["repo", "fetch", URL], input=answer)
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
        patch("cache22.repo_cli.typer.confirm", side_effect=AssertionError("must not prompt")),
    ):
        runner = CliRunner()
        result = runner.invoke(app, ["repo", "fetch", URL])
        assert result.exit_code == 1
        assert "--adopt" in result.stderr
        result = runner.invoke(app, ["repo", "fetch", URL, "--adopt"])
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
        patch("cache22.repo_cli._is_interactive", return_value=True),
        patch("cache22.repo_cli.typer.confirm", side_effect=AssertionError("must not prompt")),
    ):
        result = CliRunner().invoke(app, ["repo", "fetch", URL])
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
        patch("cache22.repo_cli._is_interactive", return_value=True),
        patch("cache22.repo_cli.typer.confirm", side_effect=confirm) as prompt,
    ):
        result = CliRunner().invoke(app, ["repo", "fetch", URL])
    assert result.exit_code == 1
    assert "source conflict" in result.stderr
    assert prompt.call_count == 1
    assert not mirror.paths.lock_file.exists()


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
        import_repository(URL, mirror.root, adopt=True)
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
        import_repository(url, mirror.root, adopt=not initialized)
    assert command not in str(error.value)
    assert not sentinel.exists()
    assert snapshot(mirror.paths.repository_dir) == before


def test_worktree_configuration_is_rejected_before_adoption(mirror: Mirror) -> None:
    (mirror.paths.mirror_repository / "config.worktree").write_text("[core]\nsshCommand = false\n")
    before = snapshot(mirror.paths.repository_dir)
    with pytest.raises(ValueError, match="worktree configuration"):
        import_repository(URL, mirror.root, adopt=True)
    assert snapshot(mirror.paths.repository_dir) == before


def test_repository_hooks_are_disabled_during_adoption_and_updates(mirror: Mirror) -> None:
    sentinel = mirror.root / "hook-executed"
    hook = mirror.paths.mirror_repository / "hooks/reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(sentinel))}\n")
    hook.chmod(0o755)
    mirror.commit("adoption update")
    import_repository(URL, mirror.root, adopt=True)
    latest = mirror.commit("ordinary update")
    import_repository(URL, mirror.root)
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
    import_repository(url, mirror.root, adopt=True)
    assert sentinel.exists()
    assert git(mirror.paths.mirror_repository, "rev-parse", "HEAD") == latest


def test_default_branch_rename_updates_head_and_keeps_mirror_cloneable(mirror: Mirror) -> None:
    import_repository(URL, mirror.root, adopt=True)
    git(mirror.source, "branch", "-m", "renamed")
    latest = mirror.commit("new default branch")
    import_repository(URL, mirror.root)
    assert git(mirror.paths.mirror_repository, "symbolic-ref", "HEAD") == "refs/heads/renamed"
    assert git(mirror.paths.mirror_repository, "rev-parse", "HEAD") == latest
    checkout = mirror.root / "checkout"
    git(mirror.root, "clone", str(mirror.paths.mirror_repository), str(checkout))
    assert (checkout / "file.txt").read_text() == "new default branch"


def test_detached_remote_head_is_fetched_even_without_a_ref(mirror: Mirror) -> None:
    import_repository(URL, mirror.root, adopt=True)
    git(mirror.source, "checkout", "--detach")
    detached = mirror.commit("unreferenced HEAD commit")
    import_repository(URL, mirror.root)
    assert (mirror.paths.mirror_repository / "HEAD").read_text().strip() == detached
    assert git(mirror.paths.mirror_repository, "rev-parse", "HEAD:file.txt") == git(
        mirror.source, "rev-parse", "HEAD:file.txt"
    )
    # When the source becomes empty, a previous detached HEAD becomes unborn.
    git(mirror.source, "update-ref", "-d", "refs/heads/main")
    git(mirror.source, "symbolic-ref", "HEAD", "refs/heads/unborn")
    import_repository(URL, mirror.root)
    assert git(mirror.paths.mirror_repository, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert not git(mirror.paths.mirror_repository, "for-each-ref")


def test_nonempty_remote_without_advertised_head_is_an_error(mirror: Mirror) -> None:
    import_repository(URL, mirror.root, adopt=True)
    git(mirror.source, "symbolic-ref", "HEAD", "refs/heads/missing")
    with pytest.raises(RuntimeError, match="HEAD synchronization failed"):
        import_repository(URL, mirror.root)
    assert mirror.paths.clone_complete_marker.exists()


def test_head_publication_failure_retains_fetched_refs_for_retry(mirror: Mirror) -> None:
    import_repository(URL, mirror.root, adopt=True)
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
        import_repository(URL, mirror.root)
    assert git(mirror.paths.mirror_repository, "show-ref") == git(mirror.source, "show-ref")
    head_lock.unlink()
    import_repository(URL, mirror.root)
    assert git(mirror.paths.mirror_repository, "symbolic-ref", "HEAD") == "refs/heads/renamed"


@pytest.mark.parametrize("change", ["rename", "existing_branch", "symbolic_oid", "detached_oid"])
def test_remote_head_change_during_fetch_is_retryable(mirror: Mirror, change: str) -> None:
    if change == "existing_branch":
        git(mirror.source, "branch", "release")
    elif change == "detached_oid":
        git(mirror.source, "checkout", "--detach")
    import_repository(URL, mirror.root, adopt=True)
    previous_head = (mirror.paths.mirror_repository / "HEAD").read_bytes()
    discover = git_mirror._remote_head
    discoveries = 0

    def changing_remote(executable: Path, path: Path, url: str) -> git_mirror._RemoteHead:
        nonlocal discoveries
        head = discover(executable, path, url)
        discoveries += 1
        if discoveries == 1:
            if change == "rename":
                git(mirror.source, "branch", "-m", "renamed")
            elif change == "existing_branch":
                git(mirror.source, "symbolic-ref", "HEAD", "refs/heads/release")
            else:
                mirror.commit("changed during fetching")
        return head

    with (
        patch("cache22.git_mirror._remote_head", side_effect=changing_remote),
        pytest.raises(RuntimeError, match="HEAD synchronization failed"),
    ):
        import_repository(URL, mirror.root)
    assert discoveries == 2  # No automatic retry.
    assert (mirror.paths.mirror_repository / "HEAD").read_bytes() == previous_head
    assert git(mirror.paths.mirror_repository, "show-ref") == git(mirror.source, "show-ref")
    assert mirror.paths.clone_complete_marker.exists()
    import_repository(URL, mirror.root)
    assert (mirror.paths.mirror_repository / "HEAD").read_bytes() == (
        mirror.source / ".git/HEAD"
    ).read_bytes()


def test_fetched_head_must_match_even_when_advertisements_agree(mirror: Mirror) -> None:
    import_repository(URL, mirror.root, adopt=True)
    previous_head = (mirror.paths.mirror_repository / "HEAD").read_bytes()
    original = git(mirror.source, "rev-parse", "HEAD")
    discover = git_mirror._remote_head
    discoveries = 0
    fetched = ""

    def changing_remote(executable: Path, path: Path, url: str) -> git_mirror._RemoteHead:
        nonlocal discoveries, fetched
        discoveries += 1
        if discoveries == 2:
            # Restore the advertised OID after fetch has already received a different one.
            git(mirror.source, "update-ref", "refs/heads/main", original)
        head = discover(executable, path, url)
        if discoveries == 1:
            fetched = mirror.commit("temporary remote tip")
        return head

    with (
        patch("cache22.git_mirror._remote_head", side_effect=changing_remote),
        pytest.raises(RuntimeError, match="HEAD synchronization failed"),
    ):
        import_repository(URL, mirror.root)
    assert (mirror.paths.mirror_repository / "HEAD").read_bytes() == previous_head
    assert git(mirror.paths.mirror_repository, "rev-parse", "refs/heads/main") == fetched
    assert mirror.paths.clone_complete_marker.exists()
    import_repository(URL, mirror.root)
    assert git(mirror.paths.mirror_repository, "rev-parse", "HEAD") == original


def test_post_fetch_head_discovery_failure_retains_refs_for_retry(mirror: Mirror) -> None:
    import_repository(URL, mirror.root, adopt=True)
    previous_head = (mirror.paths.mirror_repository / "HEAD").read_bytes()
    git(mirror.source, "branch", "-m", "renamed")
    discover = git_mirror._remote_head
    discoveries = 0

    def failing_discovery(executable: Path, path: Path, url: str) -> git_mirror._RemoteHead:
        nonlocal discoveries
        discoveries += 1
        if discoveries == 2:
            raise subprocess.CalledProcessError(128, [str(executable), "ls-remote"])
        return discover(executable, path, url)

    with (
        patch("cache22.git_mirror._remote_head", side_effect=failing_discovery),
        pytest.raises(RuntimeError, match="refs were fetched, but HEAD synchronization failed"),
    ):
        import_repository(URL, mirror.root)
    assert (mirror.paths.mirror_repository / "HEAD").read_bytes() == previous_head
    assert git(mirror.paths.mirror_repository, "show-ref") == git(mirror.source, "show-ref")
    assert mirror.paths.clone_complete_marker.exists()
    import_repository(URL, mirror.root)
    assert git(mirror.paths.mirror_repository, "symbolic-ref", "HEAD") == "refs/heads/renamed"


@pytest.mark.parametrize(
    "problem",
    [
        "objects/info/alternates",
        "objects/info/http-alternates",
        "commondir",
        "shallow",
        "objects/pack/partial.promisor",
        "symlink_directory",
        "symlink_head",
        "special_file",
        "nested_boundary",
        "missing_refs",
    ],
)
def test_unsafe_initialized_layout_is_rejected_before_git(mirror: Mirror, problem: str) -> None:
    import_repository(URL, mirror.root, adopt=True)
    repo = mirror.paths.mirror_repository
    if problem == "symlink_directory":
        (repo / "refs/external").symlink_to(mirror.source / ".git/refs", target_is_directory=True)
    elif problem == "symlink_head":
        (repo / "HEAD").unlink()
        (repo / "HEAD").symlink_to(mirror.source / ".git/HEAD")
    elif problem == "special_file":
        os.mkfifo(repo / "objects/info/pipe")
    elif problem == "nested_boundary":
        (repo / "nested").mkdir()
        (repo / "nested/.lock").write_bytes(LOCK_SIGNATURE)
    elif problem == "missing_refs":
        shutil.rmtree(repo / "refs")
    else:
        (repo / problem).write_text(str(mirror.source / ".git/objects") + "\n")
    before = snapshot(mirror.paths.repository_dir)
    external_before = snapshot(mirror.source)

    with (
        patch("cache22.git_mirror.subprocess.run", side_effect=AssertionError("Git must not run")),
        pytest.raises(ValueError, match="Unsafe|self-contained|Partial|nested|bare Git mirror"),
    ):
        import_repository(URL, mirror.root)
    assert snapshot(mirror.paths.repository_dir) == before
    assert snapshot(mirror.source) == external_before


def test_ordinary_update_does_not_repeat_full_object_verification(mirror: Mirror) -> None:
    import_repository(URL, mirror.root, adopt=True)
    latest = mirror.commit("ordinary update")
    with patch("cache22.adoption._verify_mirror", side_effect=AssertionError("Do not repeat fsck")):
        import_repository(URL, mirror.root)
    assert git(mirror.paths.mirror_repository, "rev-parse", "HEAD") == latest


@pytest.mark.parametrize("contents", [b"", b"unfinished\n", b"complete", b"complete\nextra"])
def test_malformed_completion_marker_blocks_reuse_without_changes(
    mirror: Mirror, contents: bytes
) -> None:
    import_repository(URL, mirror.root, adopt=True)
    mirror.paths.clone_complete_marker.write_bytes(contents)
    before = snapshot(mirror.paths.repository_dir)
    with (
        patch("cache22.git_mirror.subprocess.run", side_effect=AssertionError("Git must not run")),
        pytest.raises(ValueError, match="Malformed clone completion marker"),
    ):
        import_repository(URL, mirror.root)
    assert snapshot(mirror.paths.repository_dir) == before
    assert clean_repository_import_state(URL, (mirror.root,)) == ()
    assert snapshot(mirror.paths.repository_dir) == before


@pytest.fixture
def audit_mirror(mirror: Mirror, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Mirror:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    add_archive_dir(mirror.root)
    return mirror


def test_audit_adopts_all_mirrors_offline(audit_mirror: Mirror) -> None:
    mirror = audit_mirror
    nested_url = "ssh://git@host/Team/Subgroup/Other.git"
    nested = archive_paths_for_repository(mirror.root, parse_repository_url(nested_url))
    shutil.copytree(mirror.paths.mirror_repository, nested.mirror_repository)
    git(nested.mirror_repository, "config", "remote.origin.url", nested_url)
    empty = archive_paths_for_repository(
        mirror.root, parse_repository_url("https://host/team/empty")
    )
    empty.repository_dir.mkdir(parents=True)
    git(mirror.source, "init", "--bare", str(empty.mirror_repository))
    git(empty.mirror_repository, "config", "remote.origin.url", "https://host/team/empty")
    git(empty.mirror_repository, "config", "remote.origin.mirror", "true")
    git(empty.mirror_repository, "config", "remote.origin.fetch", "+refs/*:refs/*")
    before = snapshot(mirror.paths.mirror_repository)
    run = subprocess.run

    def offline(args, **kwargs):
        assert not {"fetch", "clone", "ls-remote"}.intersection(args)
        return run(args, **kwargs)

    with patch("subprocess.run", side_effect=offline):
        result = CliRunner().invoke(app, ["repo", "audit", "--adopt", "--json"])
    assert result.exit_code == 0, result.output
    issues = json.loads(result.stdout)
    assert len(issues) == 3
    assert all(issue["fixed"] and issue["problem"] == "Adopted Git mirror" for issue in issues)
    index = Index()
    assert len(index.list()) == 3
    for record in index.list():
        assert record["local_state"] == "ready"
        assert record["remote_status"] == "unknown"
        assert record["last_fetched_at"] is None and record["last_checked_at"] is None
        assert not record["scheduled"] and not record["queued"]
    assert index.get(nested_url)["source_path"] == "host/Team/Subgroup/Other"
    assert index.get(nested_url)["source_url"] == nested_url
    assert snapshot(mirror.paths.mirror_repository) == before
    with patch("cache22.adoption._verify_mirror", side_effect=AssertionError("Already adopted")):
        assert audit(adopt=True, fix=True) == []


@pytest.mark.parametrize("fix", [False, True])
def test_audit_requires_explicit_adoption(audit_mirror: Mirror, fix: bool) -> None:
    before = snapshot(audit_mirror.paths.repository_dir)
    issues = audit(fix=fix)
    assert len(issues) == 1 and not issues[0]["fixed"]
    assert "--adopt" in issues[0]["problem"]
    assert Index().list() == []
    assert snapshot(audit_mirror.paths.repository_dir) == before


@pytest.mark.parametrize(
    "markers", [(), ("lock",), ("complete",), ("lock", "source"), ("lock", "complete")]
)
def test_audit_recovers_partial_adoption(audit_mirror: Mirror, markers: tuple[str, ...]) -> None:
    paths = audit_mirror.paths
    if "lock" in markers:
        paths.lock_file.write_bytes(LOCK_SIGNATURE)
    if "complete" in markers:
        paths.clone_complete_marker.write_text("complete\n")
    if "source" in markers:
        paths.source_file.write_text(json.dumps({"source_path": "host/team/project"}))
    index = Index()
    record = index.add(parse_repository_url(URL), audit_mirror.root)
    index.update(record["id"], reconciliation_required=True)
    assert all(issue["fixed"] for issue in audit(index=index, adopt=True))
    assert index.get(URL)["local_state"] == "ready"
    assert not index.get(URL)["reconciliation_required"]
    assert audit() == []


@pytest.mark.parametrize("problem", ["origin", "marker", "busy", "source"])
def test_audit_adoption_continues_after_failure(audit_mirror: Mirror, problem: str) -> None:
    mirror = audit_mirror
    sibling_url = "https://host/team/sibling"
    sibling = archive_paths_for_repository(mirror.root, parse_repository_url(sibling_url))
    shutil.copytree(mirror.paths.mirror_repository, sibling.mirror_repository)
    git(sibling.mirror_repository, "config", "remote.origin.url", sibling_url)
    if problem == "origin":
        git(mirror.paths.mirror_repository, "config", "remote.origin.url", sibling_url)
    elif problem == "marker":
        mirror.paths.clone_complete_marker.write_text("broken\n")
    elif problem == "source":
        Index().add(
            parse_repository_url("https://host/Team/Project", case_sensitive=True), mirror.root
        )
    before = snapshot(mirror.paths.repository_dir)
    directory_fd = os.open(mirror.paths.repository_dir, os.O_RDONLY)
    try:
        if problem == "busy":
            fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = CliRunner().invoke(app, ["repo", "audit", "--adopt", "--json"])
    finally:
        os.close(directory_fd)
    assert result.exit_code == 1, result.output
    issues = json.loads(result.stdout)
    assert any(not issue["fixed"] for issue in issues)
    assert any(issue["fixed"] and issue["path"] == str(sibling.repository_dir) for issue in issues)
    assert snapshot(mirror.paths.repository_dir) == before
    assert Index().get(sibling_url)["local_state"] == "ready"


@pytest.mark.parametrize("selected", [False, True])
def test_audit_adoption_duplicate_roots(
    audit_mirror: Mirror, tmp_path: Path, selected: bool
) -> None:
    mirror = audit_mirror
    duplicate = tmp_path / "duplicate"
    shutil.copytree(mirror.root, duplicate)
    add_archive_dir(duplicate)
    index = Index()
    winner = duplicate if selected else mirror.root
    loser = mirror.root if selected else duplicate
    if selected:
        index.add(parse_repository_url(URL), winner)
    before = snapshot(loser)
    issues = audit(index=index, adopt=True)
    assert any("Duplicate" in issue["problem"] and not issue["fixed"] for issue in issues)
    assert index.get(URL)["archive_root"] == str(winner)
    assert index.get(URL)["local_state"] == "ready"
    assert snapshot(loser) == before


def test_audit_adoption_inventory_failure_is_retryable(audit_mirror: Mirror) -> None:
    index = Index()
    with patch.object(index, "update", side_effect=sqlite3.OperationalError("disk full")):
        issues = audit(index=index, adopt=True)
    assert issues and all(not issue["fixed"] for issue in issues)
    assert audit_mirror.paths.lock_file.read_bytes() == LOCK_SIGNATURE
    with patch("cache22.adoption._verify_mirror", side_effect=AssertionError("Already verified")):
        assert all(issue["fixed"] for issue in audit(index=index, adopt=True))
    assert index.get(URL)["local_state"] == "ready"
    assert audit(index=index) == []


def test_audit_does_not_recreate_disappeared_candidate(
    audit_mirror: Mirror, tmp_path: Path
) -> None:
    mirror = audit_mirror
    moved = tmp_path / "moved"
    before = snapshot(mirror.paths.repository_dir)

    def disappear(*args, **kwargs):
        mirror.paths.repository_dir.rename(moved)
        return repository_operation(*args, **kwargs)

    with patch("cache22.repo_audit.repository_operation", side_effect=disappear):
        issues = audit(adopt=True)
    assert len(issues) == 1 and not issues[0]["fixed"]
    assert not mirror.paths.repository_dir.exists()
    assert snapshot(moved) == before
    assert Index().list() == []
