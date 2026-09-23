"""Shared CLI error handling and explicit text/JSON rendering."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable
from functools import wraps
from typing import Annotated, Any

import typer

from .manager_service import json_value

JsonOption = Annotated[bool, typer.Option("--json", help="Emit JSON instead of text.")]
Selector = Annotated[
    str, typer.Argument(help="Exact canonical repository key or supported Git URL.")
]
Selectors = Annotated[
    list[str], typer.Argument(help="Exact canonical repository keys or supported Git URLs.")
]
OptionalSelectors = Annotated[
    list[str] | None,
    typer.Argument(
        help="Exact canonical repository keys or supported Git URLs; alternatively use --all."
    ),
]
REPOSITORY_COLUMNS = (
    "repo_key",
    "local_state",
    "remote_status",
    "local_head_committed_at",
    "last_checked_at",
    "storage_format",
    "archive_path",
)
RESULT_COLUMNS = ("selector", "repo_key", "repository_id", "status", "job_id", "error")


def command[**P](function: Callable[P, None]) -> Callable[P, None]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            function(*args, **kwargs)
        except (
            ValueError,
            RuntimeError,
            OSError,
            sqlite3.Error,
            subprocess.SubprocessError,
        ) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc

    return wrapped


def output(value: Any, as_json: bool, *, columns: tuple[str, ...] = ()) -> None:
    value = json_value(value)
    if as_json:
        typer.echo(json.dumps(value, ensure_ascii=False, indent=2))
    elif isinstance(value, list):
        for item in value:
            if columns:
                typer.echo(
                    "\t".join(str(item.get(k)) if item.get(k) is not None else "-" for k in columns)
                )
                if item.get("operation_error"):
                    typer.echo(
                        f"{item.get('repo_key', item.get('selector'))}: {item['operation_error']}",
                        err=True,
                    )
            else:
                output(item, False)
    else:
        for key, item in value.items():
            typer.echo(f"{key}: {item if item is not None else '-'}")
