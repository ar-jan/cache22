from __future__ import annotations

import sys
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import NoReturn

import typer

from .adoption import AdoptionRequiredError
from .config import (
    ConfigError,
    add_archive_dir,
    default_archive_type,
    list_archive_dirs,
    set_archive_type,
)
from .import_service import ImportResult, import_repository
from .import_state import clean_all_import_state, clean_repository_import_state
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
import_clean_app = typer.Typer(help="Clean partial import state.", no_args_is_help=True)

app.add_typer(config_app, name="config")
config_app.add_typer(archive_app, name="archive")
config_app.add_typer(archive_type_app, name="archive-type")
app.add_typer(status_app, name="status")
app.add_typer(import_app, name="import")
import_app.add_typer(import_clean_app, name="clean")


def _user_command[**P](command: Callable[P, None]) -> Callable[P, None]:
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


@import_app.command(
    "repo",
    help="Import or update a Git mirror, or create a Fossil archive.",
)
@_user_command
def import_repo(
    url: str,
    case_sensitive: bool = typer.Option(
        False, "--case-sensitive", help="Preserve remote repository path casing."
    ),
    adopt: bool = typer.Option(
        False, "--adopt", help="Verify and initialize an existing Git mirror before updating it."
    ),
) -> None:
    try:
        result = import_repository(url, case_sensitive=case_sensitive, adopt=adopt)
    except AdoptionRequiredError as exc:
        if adopt or not _is_interactive():
            raise
        typer.echo(exc.conflict_message, err=True)
        if not typer.confirm(
            "Verify and adopt the existing Git mirror, then fetch updates?", default=False, err=True
        ):
            raise typer.Exit(code=1) from exc
        # The first attempt has released its locks. Retry against the same root
        # and archive mode even if configuration changed while awaiting input.
        result = import_repository(
            url,
            archive_dir=exc.archive_dir,
            archive_type="git",
            case_sensitive=case_sensitive,
            adopt=True,
        )
    _report_import_result(result)


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


@import_clean_app.command("repo")
@_user_command
def clean_repo(url: str) -> None:
    _report_clean_result(
        clean_repository_import_state(url),
        empty_message=f"No partial import state found for repository: {url}",
    )


@import_clean_app.command("all")
@_user_command
def clean_all() -> None:
    _report_clean_result(
        clean_all_import_state(),
        empty_message="No partial import state found in configured archive directories.",
    )


def _report_import_result(result: ImportResult) -> None:
    for message in result.info_messages:
        typer.echo(message)
    typer.echo(f"Imported archive: {result.archive_path}")


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
