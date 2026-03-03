from __future__ import annotations

from functools import wraps
from pathlib import Path
from typing import Callable, NoReturn, ParamSpec

import typer

from .config import (
    ConfigError,
    add_archive_dir,
    default_archive_type,
    list_archive_dirs,
    set_archive_type,
)
from .import_git import (
    clean_all_import_state,
    clean_repository_import_state,
    import_git_repository,
)
from .system_tools import get_fossil_status

app = typer.Typer(
    help="Archive Git repositories as Git mirrors or Fossil repositories.",
    no_args_is_help=True,
)
config_app = typer.Typer(help="Manage cache22 configuration.", no_args_is_help=True)
archive_app = typer.Typer(help="Manage archive directories.", no_args_is_help=True)
archive_type_app = typer.Typer(help="Manage the default archival format.", no_args_is_help=True)
status_app = typer.Typer(help="Inspect external tool availability.", no_args_is_help=True)
clean_app = typer.Typer(help="Clean partial import state.", no_args_is_help=True)

app.add_typer(config_app, name="config")
config_app.add_typer(archive_app, name="archive")
config_app.add_typer(archive_type_app, name="archive-type")
app.add_typer(status_app, name="status")
app.add_typer(clean_app, name="clean")

P = ParamSpec("P")


def _user_command(command: Callable[P, None]) -> Callable[P, None]:
    @wraps(command)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            command(*args, **kwargs)
        except (ConfigError, OSError, RuntimeError, ValueError) as exc:
            _exit_with_error(exc)

    return wrapper


@archive_app.command("add")
@_user_command
def config_archive_add(path: Path) -> None:
    archive_dir, added = add_archive_dir(path)

    if added:
        typer.echo(f"Added archive directory: {archive_dir}")
        return

    typer.echo(f"Archive directory already configured: {archive_dir}")


@archive_app.command("list")
@_user_command
def config_archive_list() -> None:
    for archive_dir in list_archive_dirs():
        typer.echo(str(archive_dir))


@archive_type_app.command("show")
@_user_command
def config_archive_type_show() -> None:
    typer.echo(default_archive_type())


@archive_type_app.command("set")
@_user_command
def config_archive_type_set(archive_type: str) -> None:
    configured_archive_type = set_archive_type(archive_type)
    typer.echo(f"Default archive type: {configured_archive_type}")


@status_app.command("fossil")
@_user_command
def status_fossil() -> None:
    status = get_fossil_status()
    typer.echo(f"path: {status.executable}")
    typer.echo(f"version: {status.version}")


@app.command(
    "import",
    help=("Import repositories into the archive. Repository URLs are treated as Git repositories."),
)
@_user_command
def import_repository(url: str) -> None:
    _run_git_import(url)


def _run_git_import(url: str) -> None:
    result = import_git_repository(url)
    for message in result.info_messages:
        typer.echo(message)
    typer.echo(f"Imported archive: {result.archive_path}")


@clean_app.command("repo")
@_user_command
def clean_repo(url: str) -> None:
    _report_clean_result(
        clean_repository_import_state(url),
        empty_message=f"No partial import state found for repository: {url}",
    )


@clean_app.command("all")
@_user_command
def clean_all() -> None:
    _report_clean_result(
        clean_all_import_state(),
        empty_message="No partial import state found in configured archive directories.",
    )


def _report_clean_result(removed_paths: tuple[Path, ...], *, empty_message: str) -> None:
    if not removed_paths:
        typer.echo(empty_message)
        return

    for removed_path in removed_paths:
        typer.echo(f"Removed partial import state: {removed_path}")


def main() -> None:
    app()


def _exit_with_error(exc: Exception) -> NoReturn:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1) from exc
