from __future__ import annotations

from pathlib import Path

import typer

from .cli_support import command
from .config import add_archive_dir, list_archive_dirs
from .jobs_cli import jobs
from .manager.web import serve
from .repo_cli import repo_app

app = repo_app
app.command("jobs")(jobs)
app.command("web")(serve)
config_app = typer.Typer(help="Manage cache22 configuration.", no_args_is_help=True)
root_app = typer.Typer(help="Manage archive roots.", no_args_is_help=True)

app.add_typer(config_app, name="config")
config_app.add_typer(root_app, name="root")


@root_app.command("add")
@command
def config_root_add(path: Path) -> None:
    archive_dir, added = add_archive_dir(path)

    if added:
        typer.echo(f"Added archive root: {archive_dir}")
        return

    typer.echo(f"Archive root already configured: {archive_dir}")


@root_app.command("list")
@command
def config_root_list() -> None:
    for archive_dir in list_archive_dirs():
        typer.echo(str(archive_dir))


def main() -> None:
    app()
