from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .config import ArchiveType, default_archive_dir, default_archive_type, normalize_archive_dir
from .system_tools import find_fossil_executable, find_git_executable

_STAGING_ROOT_NAME = ".cache22"
_GIT_IMPORT_STAGE_NAME = "git-import"
_CLONE_COMPLETE_MARKER_NAME = ".clone-complete"
_CASEFOLDED_REPOSITORY_HOSTS = frozenset({"github.com", "gitlab.com"})


@dataclass(frozen=True, slots=True)
class GitRepository:
    host: str
    namespace: tuple[str, ...]
    name: str


@dataclass(frozen=True, slots=True)
class ImportPaths:
    repository_dir: Path
    mirror_repository: Path
    fossil_repository: Path
    git_marks: Path
    fossil_marks: Path
    stage_root: Path
    stage_dir: Path
    staged_fossil_repository: Path
    staged_git_marks: Path
    staged_fossil_marks: Path
    clone_complete_marker: Path


@dataclass(frozen=True, slots=True)
class ImportResult:
    archive_path: Path
    info_messages: tuple[str, ...] = ()


def parse_repository_url(url: str) -> GitRepository:
    raw_url = url.strip()
    if not raw_url:
        raise ValueError("Repository URL must not be empty")

    host, raw_path = _split_clone_url(raw_url)
    return _repository_from_path(host, raw_path)


def import_git_repository(
    url: str,
    archive_dir: Path | None = None,
    archive_type: ArchiveType | None = None,
) -> ImportResult:
    repository = parse_repository_url(url)
    resolved_archive_dir = _resolve_archive_dir(archive_dir)
    resolved_archive_type = _resolve_archive_type(archive_type)
    paths = archive_paths_for_repository(resolved_archive_dir, repository)
    requested_archive = _existing_requested_archive_path(paths, resolved_archive_type)
    info_messages: list[str] = []

    if requested_archive is not None:
        info_messages.append(f"INFO: archive already exists: {requested_archive}")
        return ImportResult(requested_archive, tuple(info_messages))

    git_executable = find_git_executable()

    try:
        existing_mirror_message = _ensure_final_git_mirror(
            git_executable=git_executable,
            url=url,
            paths=paths,
        )
        if existing_mirror_message is not None:
            info_messages.append(existing_mirror_message)

        if resolved_archive_type == "git":
            return ImportResult(paths.mirror_repository, tuple(info_messages))

        fossil_executable = find_fossil_executable()
        _prepare_fossil_stage(paths)
        _run_fast_export_import(
            git_executable=git_executable,
            fossil_executable=fossil_executable,
            paths=paths,
        )
        _promote_staged_import_state(paths)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            _import_failure_message(url, paths, resolved_archive_type, str(exc))
        ) from exc

    _clear_stage_dir_after_success(paths)
    return ImportResult(paths.fossil_repository, tuple(info_messages))


def archive_paths_for_repository(archive_dir: Path, repository: GitRepository) -> ImportPaths:
    repository_dir = _repository_root(archive_dir, repository)

    stage_root = archive_dir / _STAGING_ROOT_NAME / _GIT_IMPORT_STAGE_NAME
    stage_dir = _repository_root(stage_root, repository)

    return ImportPaths(
        repository_dir=repository_dir,
        mirror_repository=repository_dir / f"{repository.name}.git",
        fossil_repository=repository_dir / f"{repository.name}.fossil",
        git_marks=repository_dir / "git.marks",
        fossil_marks=repository_dir / "fossil.marks",
        stage_root=stage_root,
        stage_dir=stage_dir,
        staged_fossil_repository=stage_dir / f"{repository.name}.fossil",
        staged_git_marks=stage_dir / "git.marks",
        staged_fossil_marks=stage_dir / "fossil.marks",
        clone_complete_marker=repository_dir / _CLONE_COMPLETE_MARKER_NAME,
    )


def clear_git_import_stage(url: str, archive_dir: Path | None = None) -> tuple[Path, bool]:
    repository = parse_repository_url(url)
    resolved_archive_dir = _resolve_archive_dir(archive_dir)
    paths = archive_paths_for_repository(resolved_archive_dir, repository)
    if paths.stage_dir.exists():
        shutil.rmtree(paths.stage_dir)
        _remove_empty_stage_parents(paths.stage_dir.parent, stop=paths.stage_root)
        return paths.stage_dir, True

    if paths.mirror_repository.exists() and not paths.clone_complete_marker.exists():
        shutil.rmtree(paths.mirror_repository)
        return paths.mirror_repository, True

    return paths.repository_dir, False


def _resolve_archive_dir(archive_dir: Path | None) -> Path:
    if archive_dir is None:
        return default_archive_dir()

    return normalize_archive_dir(archive_dir)


def _resolve_archive_type(archive_type: ArchiveType | None) -> ArchiveType:
    if archive_type is not None:
        return archive_type

    return default_archive_type()


def _split_clone_url(raw_url: str) -> tuple[str, str]:
    if "://" not in raw_url:
        user_host, separator, path = raw_url.partition(":")
        if separator and "@" in user_host:
            _, _, host = user_host.rpartition("@")
            if host:
                return host.lower(), path

    parsed = urlsplit(raw_url)
    if parsed.scheme not in {"https", "ssh"}:
        raise ValueError(f"Only HTTPS and SSH clone URLs are supported: {raw_url}")
    if parsed.query or parsed.fragment:
        raise ValueError(f"Repository URL must not include query or fragment data: {raw_url}")

    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError(f"Repository URL must include a hostname: {raw_url}")

    return host, parsed.path


def _repository_from_path(host: str, raw_path: str) -> GitRepository:
    normalized_path = raw_path.strip().strip("/")
    if normalized_path.casefold().endswith(".git"):
        normalized_path = normalized_path[:-4]

    parts = [part for part in normalized_path.split("/") if part]
    if len(parts) < 2 or any(part in {".", ".."} for part in parts):
        raise ValueError(
            f"Repository URL must have the form HOST/NAMESPACE[/SUBGROUP/...]/REPO(.git): {host}/{raw_path.strip('/')}"
        )

    normalized_parts = _normalize_repository_parts(host, parts)
    return GitRepository(
        host=host,
        namespace=tuple(normalized_parts[:-1]),
        name=normalized_parts[-1],
    )


def _ensure_final_git_mirror(
    *,
    git_executable: Path,
    url: str,
    paths: ImportPaths,
) -> str | None:
    if paths.clone_complete_marker.exists():
        if not paths.mirror_repository.exists():
            raise ValueError(
                f"Clone marker exists but mirror repository is missing: {paths.repository_dir}"
            )
        return f"INFO: archive already exists: {paths.mirror_repository}"

    if paths.mirror_repository.exists():
        raise ValueError(
            f"Mirror repository exists without a completion marker: {paths.mirror_repository}. "
            f"Clear it with 'cache22 import git-clear {url}' to start over."
        )

    paths.repository_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [str(git_executable), "clone", "--mirror", url, str(paths.mirror_repository)],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        _clear_incomplete_mirror(paths)
        raise RuntimeError(f"git clone --mirror failed with exit code {exc.returncode}") from exc

    try:
        paths.clone_complete_marker.write_text("complete\n")
    except OSError:
        _clear_incomplete_mirror(paths)
        raise

    return None


def _prepare_fossil_stage(paths: ImportPaths) -> None:
    if paths.stage_dir.exists():
        shutil.rmtree(paths.stage_dir)

    paths.stage_dir.mkdir(parents=True, exist_ok=True)


def _clear_incomplete_mirror(paths: ImportPaths) -> None:
    if paths.clone_complete_marker.exists():
        try:
            paths.clone_complete_marker.unlink()
        except OSError:
            return

    if not paths.mirror_repository.exists():
        return

    try:
        shutil.rmtree(paths.mirror_repository)
    except OSError:
        return


def _promote_staged_import_state(paths: ImportPaths) -> None:
    staged_targets = (
        (paths.staged_fossil_repository, paths.fossil_repository),
        (paths.staged_git_marks, paths.git_marks),
        (paths.staged_fossil_marks, paths.fossil_marks),
    )
    for staged_path, _ in staged_targets:
        if not staged_path.exists():
            raise RuntimeError(f"Expected staged import output was not created: {staged_path}")

    paths.repository_dir.mkdir(parents=True, exist_ok=True)
    for staged_path, final_path in staged_targets:
        staged_path.replace(final_path)


def _clear_stage_dir(paths: ImportPaths) -> None:
    if not paths.stage_dir.exists():
        return

    shutil.rmtree(paths.stage_dir)
    _remove_empty_stage_parents(paths.stage_dir.parent, stop=paths.stage_root)


def _clear_stage_dir_after_success(paths: ImportPaths) -> None:
    try:
        _clear_stage_dir(paths)
    except OSError:
        # The archive is already promoted. A cleanup failure should not turn a successful import
        # into a retry trap where the final archive exists but the command reported failure.
        return


def _remove_empty_stage_parents(start: Path, *, stop: Path) -> None:
    current = start
    stop_parent = stop.parent
    while current != stop_parent:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _import_failure_message(
    url: str,
    paths: ImportPaths,
    archive_type: ArchiveType,
    message: str,
) -> str:
    if archive_type == "fossil" and paths.stage_dir.exists():
        return (
            f"{message}. Staged Fossil import state was kept at {paths.stage_dir}. "
            f"Clear it with 'cache22 import git-clear {url}' to start over."
        )

    return message


def _existing_requested_archive_path(paths: ImportPaths, archive_type: ArchiveType) -> Path | None:
    if archive_type == "git":
        if paths.clone_complete_marker.exists() and paths.mirror_repository.exists():
            return paths.mirror_repository
        return None

    if paths.fossil_repository.exists():
        return paths.fossil_repository

    return None


def _repository_root(root: Path, repository: GitRepository) -> Path:
    return root.joinpath(repository.host, *repository.namespace, repository.name)


def _normalize_repository_parts(host: str, parts: list[str]) -> list[str]:
    if host in _CASEFOLDED_REPOSITORY_HOSTS:
        return [part.casefold() for part in parts]

    return parts


def _run_fast_export_import(
    *,
    git_executable: Path,
    fossil_executable: Path,
    paths: ImportPaths,
) -> None:
    git_command = [
        str(git_executable),
        "-C",
        str(paths.mirror_repository),
        "fast-export",
        "--all",
        "--signed-tags=warn-strip",
        f"--export-marks={paths.staged_git_marks}",
    ]
    fossil_command = [
        str(fossil_executable),
        "import",
        "--git",
        "--export-marks",
        str(paths.staged_fossil_marks),
        str(paths.staged_fossil_repository),
    ]

    with subprocess.Popen(git_command, stdout=subprocess.PIPE) as git_process:
        if git_process.stdout is None:
            raise RuntimeError("git fast-export did not provide a stdout stream")

        with subprocess.Popen(fossil_command, stdin=git_process.stdout) as fossil_process:
            git_process.stdout.close()
            fossil_returncode = fossil_process.wait()

        git_returncode = git_process.wait()

    if fossil_returncode != 0:
        raise RuntimeError(f"fossil import --git failed with exit code {fossil_returncode}")
    if git_returncode != 0:
        raise RuntimeError(f"git fast-export --all failed with exit code {git_returncode}")
