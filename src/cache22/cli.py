from __future__ import annotations

from pathlib import Path

import typer

from .config import add_archive_dir, list_archive_dirs
from .manager_cli import manager_app
from .repo_cli import command, repo_app, worker_app

app = typer.Typer(
    help="Archive Git repositories as Git mirrors or bundles.",
    no_args_is_help=True,
)
config_app = typer.Typer(help="Manage cache22 configuration.", no_args_is_help=True)
archive_app = typer.Typer(help="Manage archive directories.", no_args_is_help=True)

app.add_typer(repo_app, name="repo")
app.add_typer(worker_app, name="worker")
app.add_typer(manager_app, name="manager")
app.add_typer(config_app, name="config")
config_app.add_typer(archive_app, name="archive")


@archive_app.command("add")
@command
def config_archive_add(path: Path) -> None:
    archive_dir, added = add_archive_dir(path)

    if added:
        typer.echo(f"Added archive directory: {archive_dir}")
        return

    typer.echo(f"Archive directory already configured: {archive_dir}")


@archive_app.command("list")
@command
def config_archive_list() -> None:
    for archive_dir in list_archive_dirs():
        typer.echo(str(archive_dir))


def main() -> None:
    app()
