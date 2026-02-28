from __future__ import annotations

import subprocess
from pathlib import Path

import typer

from .config import ConfigError, add_archive_dir, list_archive_dirs
from .fossil import get_fossil_status

app = typer.Typer(
    help="Archive Git repositories into Fossil.",
    no_args_is_help=True,
)
config_app = typer.Typer(help="Manage cache22 configuration.", no_args_is_help=True)
archive_app = typer.Typer(help="Manage archive directories.", no_args_is_help=True)
status_app = typer.Typer(help="Inspect external tool availability.", no_args_is_help=True)

app.add_typer(config_app, name="config")
config_app.add_typer(archive_app, name="archive")
app.add_typer(status_app, name="status")


@archive_app.command("add")
def config_archive_add(path: Path) -> None:
    try:
        archive_dir, added = add_archive_dir(path)
    except (ConfigError, ValueError) as exc:
        _exit_with_error(exc)

    if added:
        typer.echo(f"Added archive directory: {archive_dir}")
        return

    typer.echo(f"Archive directory already configured: {archive_dir}")


@archive_app.command("list")
def config_archive_list() -> None:
    try:
        for archive_dir in list_archive_dirs():
            typer.echo(str(archive_dir))
    except ConfigError as exc:
        _exit_with_error(exc)


@status_app.command("fossil")
def status_fossil() -> None:
    try:
        status = get_fossil_status()
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except subprocess.CalledProcessError as exc:
        typer.echo(f"fossil version command failed with exit code {exc.returncode}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"path: {status.executable}")
    typer.echo(f"version: {status.version}")


def main() -> None:
    app()


def _exit_with_error(exc: Exception) -> None:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1) from exc
