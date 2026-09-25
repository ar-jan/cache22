"""The loopback-only Cache22 application; workers have an independent lifecycle."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import inventory_service as inventory
from .. import manager_service as service
from ..config import list_archive_dirs
from ..index import Index
from ..operation import sanitize

ASSETS = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=ASSETS / "templates")


class BrowserBoundary:
    """Validate loopback Host and same-origin requests, including forwarded ports."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = Request(scope).headers
        host = headers.get("host", "")
        try:
            parsed = urlsplit("http://" + host)
            valid = (
                len(headers.getlist("host")) == 1
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                and not parsed.username
                and not parsed.password
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
                and parsed.port != 0
            )
            origin = headers.get("origin")
            if origin is not None:
                valid = (
                    valid
                    and len(headers.getlist("origin")) == 1
                    and origin == f"{scope.get('scheme', 'http')}://{host}"
                )
            if scope.get("method") not in {"GET", "HEAD", "OPTIONS"}:
                valid = valid and headers.get("sec-fetch-site", "same-origin") in {
                    "same-origin",
                    "none",
                }
        except ValueError:
            valid = False
        if not valid:
            await Response("Untrusted Host or Origin", status_code=403)(scope, receive, send)
            return

        async def no_store(message: Any) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"cache-control", b"no-store"))
            await send(message)

        await self.app(scope, receive, no_store)


def _render(request: Request, template: str, **context: Any) -> Response:
    return TEMPLATES.TemplateResponse(request, template, context)


async def invalid(request: Request, exc: Exception) -> Response:
    message = sanitize(str(exc))
    if request.url.path.startswith("/api/") or request.url.path.endswith((".json", ".csv")):
        return JSONResponse({"error": message}, status_code=400)
    return TEMPLATES.TemplateResponse(request, "error.html", {"error": message}, status_code=400)


async def inventory_view(request: Request) -> Response:
    selection = inventory.parse_query(request.url.query)
    snapshot = await asyncio.to_thread(inventory.inventory_page, request.app.state.index, selection)
    context = {
        "snapshot": snapshot,
        "rows": service.json_value(snapshot.rows),
        "query": selection.query,
        "selection": selection,
        "categories": inventory.CATEGORIES,
        "flags": inventory.FLAGS,
        "enums": inventory.ENUMS,
        "sorts": inventory.SORTS,
    }
    template = (
        "inventory_fragment.html" if request.url.path.endswith("/fragment") else "inventory.html"
    )
    return _render(request, template, **context)


def _download(index: Index, query: inventory.InventoryQuery, csv_format: bool) -> Response:
    rows = service.json_value(inventory.export_inventory(index, query))
    if not csv_format:
        return JSONResponse(
            rows, headers={"Content-Disposition": 'attachment; filename="inventory.json"'}
        )
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=inventory.PUBLIC_FIELDS)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                key: str(value).lower() if type(value) is bool else value
                for key, value in row.items()
            }
        )
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="inventory.csv"'},
    )


async def download(request: Request) -> Response:
    selection = inventory.parse_query(request.url.query)
    return await asyncio.to_thread(
        _download, request.app.state.index, selection.query, request.url.path.endswith(".csv")
    )


async def page(request: Request) -> Response:
    kind = "repository" if "repository_id" in request.path_params else request.url.path.strip("/")
    repository_id = request.path_params.get("repository_id")
    if kind == "repository":
        try:
            await asyncio.to_thread(request.app.state.index.get, repository_id)
        except ValueError:
            return Response("Repository not found", status_code=404)
    roots = await asyncio.to_thread(list_archive_dirs) if kind == "add" else []
    return _render(request, "cache22.html", page=kind, repository_id=repository_id, roots=roots)


async def api(request: Request) -> Response:
    endpoint = request.url.path.rsplit("/", 1)[-1]
    index = request.app.state.index
    if request.method in {"GET", "HEAD"}:
        if endpoint == "detail":
            result = await asyncio.to_thread(
                service.detail,
                index,
                int(request.query_params.get("id", "0")),
                offset=int(request.query_params.get("offset", "0")),
            )
        else:
            result = await asyncio.to_thread(
                service.queue_snapshot,
                index,
                section=request.query_params.get("section", "running"),
                offset=int(request.query_params.get("offset", "0")),
            )
            if endpoint == "health":
                result = {"workers": result["workers"], "observed_at": result["observed_at"]}
    else:
        data = json.loads(await request.body())
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        if endpoint in {"preview", "register"}:
            urls = data.get("urls")
            if not isinstance(urls, str):
                raise ValueError("urls must be newline-separated text")
            root = data.get("root")
            roots = await asyncio.to_thread(list_archive_dirs)
            if root is not None and (
                not isinstance(root, str) or root not in {str(p) for p in roots}
            ):
                raise ValueError("Choose a configured archive root")
            for flag in ("case_sensitive", "fetch"):
                if type(data.get(flag, False)) is not bool:
                    raise ValueError(f"{flag} must be boolean")
            result = {
                "results": await asyncio.to_thread(
                    service.register_batch,
                    index,
                    urls.splitlines(),
                    root=Path(root) if root else None,
                    case_sensitive=data.get("case_sensitive", False),
                    fetch=data.get("fetch", False),
                    preview=endpoint == "preview",
                )
            }
        elif endpoint == "command":
            ids, action, every = data.get("ids"), data.get("action"), data.get("every")
            if (
                not isinstance(ids, list)
                or not isinstance(action, str)
                or (every is not None and not isinstance(every, str))
            ):
                raise ValueError("Expected ids list, action string, and optional every string")
            result = {
                "results": await asyncio.to_thread(
                    service.bulk_command, index, ids, action, every=every
                )
            }
        else:
            query = data.get("query", "")
            if not isinstance(query, str):
                raise ValueError("query must be a query string")
            selection = inventory.parse_query(query)
            result = {
                "ids": await asyncio.to_thread(inventory.selected_ids, index, selection.query)
            }
    return JSONResponse(service.json_value(result))


def create_app(path: Path | None = None) -> Starlette:
    app = Starlette(
        routes=[
            Route("/", inventory_view),
            Route("/inventory/fragment", inventory_view),
            Route("/inventory.json", download),
            Route("/inventory.csv", download),
            Route("/add", page, name="add"),
            Route("/queue", page, name="queue"),
            Route("/repositories/{repository_id:int}", page, name="repository"),
            *[
                Route(f"/api/{name}", api, name=name, methods=["GET"])
                for name in ("queue", "health", "detail")
            ],
            *[
                Route(f"/api/{name}", api, name=name, methods=["POST"])
                for name in ("preview", "register", "selection", "command")
            ],
            Mount("/static", StaticFiles(directory=ASSETS / "static")),
        ],
        exception_handlers={
            exception: invalid
            for exception in (ValueError, TypeError, OverflowError, OSError, sqlite3.Error)
        },
        max_body_size=2 * 1024 * 1024,
    )
    app.add_middleware(BrowserBoundary)
    app.state.index = Index.initialize(path)
    return app
