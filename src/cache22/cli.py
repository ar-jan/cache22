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
from .import_git import clear_git_import_stage, import_git_repository
from .system_tools import get_fossil_status

app = typer.Typer(
    help="Archive Git repositories as Git mirrors or Fossil repositories.",
    no_args_is_help=True,
)
config_app = typer.Typer(help="Manage cache22 configuration.", no_args_is_help=True)
archive_app = typer.Typer(help="Manage archive directories.", no_args_is_help=True)
archive_type_app = typer.Typer(help="Manage the default archival format.", no_args_is_help=True)
status_app = typer.Typer(help="Inspect external tool availability.", no_args_is_help=True)
import_app = typer.Typer(help="Import repositories into the archive.", no_args_is_help=True)

app.add_typer(config_app, name="config")
config_app.add_typer(archive_app, name="archive")
config_app.add_typer(archive_type_app, name="archive-type")
app.add_typer(status_app, name="status")
app.add_typer(import_app, name="import")

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


@import_app.command("git")
@_user_command
def import_git(url: str) -> None:
    result = import_git_repository(url)
    for message in result.info_messages:
        typer.echo(message)
    typer.echo(f"Imported archive: {result.archive_path}")


@import_app.command("git-clear")
@_user_command
def import_git_clear(url: str) -> None:
    stage_dir, cleared = clear_git_import_stage(url)

    if cleared:
        typer.echo(f"Cleared Git import state: {stage_dir}")
        return

    typer.echo(f"No Git import state found: {stage_dir}")


def main() -> None:
    app()


def _exit_with_error(exc: Exception) -> NoReturn:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1) from exc
