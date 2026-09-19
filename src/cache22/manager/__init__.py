"""Datasette hooks for the personal Cache22 manager."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from datasette import Response, hookimpl
from datasette.filters import FilterArguments
from datasette.permissions import Action
from datasette.resources import DatabaseResource
from datasette.utils.asgi import Request
from markupsafe import Markup, escape

from .. import manager_service as service
from ..config import list_archive_dirs
from ..operation import sanitize

PREFIX = "/-/cache22"
SUMMARY_COLUMNS = (
    "id",
    "project_name",
    "repo_key",
    "host",
    "archive_root",
    "local_state",
    "remote_status",
    "local_head_committed_at",
    "last_checked_at",
    "last_fetched_at",
    "scheduled",
    "schedule_blocked",
    "interval_seconds",
    "next_due_at",
    "queued",
    "running",
    "has_error",
)


def inventory_url() -> str:
    return "/index/inventory?" + urlencode(
        [("_col", c) for c in SUMMARY_COLUMNS]
        + [
            ("_sort", "repo_key"),
            ("_facet", "host"),
            ("_facet", "local_state"),
            ("_facet", "remote_status"),
        ]
    )


def enabled(datasette: Any) -> bool:
    return hasattr(datasette, "cache22_index")


@hookimpl
def register_actions(datasette: Any) -> list[Action]:
    return (
        [
            Action(
                name="cache22-manage",
                description="Submit Cache22 commands",
                resource_class=DatabaseResource,
            )
        ]
        if enabled(datasette)
        else []
    )


@hookimpl
def register_routes(datasette: Any) -> list[tuple[str, Any]]:
    if not enabled(datasette):
        return []
    return [
        (r"^/$", home),
        (r"^/-/cache22/api/(?P<endpoint>[a-z-]+)$", api),
        (r"^/-/cache22/(?P<page>add|queue|repository)(?:/(?P<repository_id>\d+))?$", page),
    ]


async def home(request: Any) -> Response:
    return Response.redirect(inventory_url())


async def permission(datasette: Any, request: Any) -> None:
    await datasette.ensure_permission(
        action="cache22-manage", resource=DatabaseResource("index"), actor=request.actor
    )


async def page(datasette: Any, request: Any) -> Response:
    await permission(datasette, request)
    if request.method != "GET":
        return Response.text("GET required", status=405)
    kind = request.url_vars["page"]
    data = {
        "inventory_url": inventory_url(),
        "page": kind,
        "repository_id": request.url_vars.get("repository_id"),
    }
    if kind == "repository" and not data["repository_id"]:
        return Response.text("Repository ID required", status=404)
    if kind == "add":
        data["roots"] = [str(p) for p in await asyncio.to_thread(list_archive_dirs)]
    return Response.html(await datasette.render_template("cache22.html", data, request=request))


async def api(datasette: Any, request: Any) -> Response:
    await permission(datasette, request)
    endpoint = request.url_vars["endpoint"]
    reads = {"queue", "detail", "health"}
    commands = {"preview", "register", "selection", "command"}
    if endpoint not in reads | commands:
        return Response.json({"error": "Unknown endpoint"}, status=404)
    if request.method != ("GET" if endpoint in reads else "POST"):
        return Response.json({"error": "Method not allowed"}, status=405)
    index = datasette.cache22_index
    try:
        if endpoint in reads:
            if endpoint == "detail":
                result = await asyncio.to_thread(
                    service.detail,
                    index,
                    int(request.args.get("id", "0")),
                    offset=int(request.args.get("offset", "0")),
                )
            else:
                result = await asyncio.to_thread(
                    service.queue_snapshot,
                    index,
                    section=request.args.get("section", "running"),
                    offset=int(request.args.get("offset", "0")),
                )
                if endpoint == "health":
                    result = {"workers": result["workers"], "observed_at": result["observed_at"]}
        else:
            body = await request.post_body()
            if len(body) > 2 * 1024 * 1024:
                return Response.json({"error": "Request exceeds 2 MiB"}, status=413)
            data = json.loads(body)
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
                # Reuse the pinned Datasette implementation: facets, repeated filters,
                # custom search, and _where must select exactly the rows being browsed.
                from datasette.views.table import _table_filters

                scope = dict(
                    request.scope,
                    method="GET",
                    path="/index/inventory",
                    query_string=query.encode(),
                )
                filtered_request = Request(scope, request.receive)
                _, where, params, _, _ = await _table_filters(
                    datasette, filtered_request, "index", "inventory"
                )
                result = {
                    "ids": await asyncio.to_thread(service.selected_ids, index, where, params)
                }
        return Response.json(service.json_value(result), headers={"Cache-Control": "no-store"})
    except (ValueError, TypeError, OverflowError, OSError, sqlite3.Error) as exc:
        return Response.json({"error": sanitize(str(exc))}, status=400)


@hookimpl
def filters_from_request(datasette: Any, request: Any, database: str, table: str) -> Any:
    if (
        enabled(datasette)
        and database == "index"
        and table == "inventory"
        and request.args.get("_q")
    ):
        query = request.args.get("_q")
        return FilterArguments(
            [
                "(instr(lower(project_name), lower(:cache22_q))>0 OR instr(lower(repo_key), lower(:cache22_q))>0)"
            ],
            {"cache22_q": query},
            [f"name or key contains {query}"],
        )


@hookimpl
def view_actions(datasette: Any, database: str, view: str) -> Any:
    if enabled(datasette) and database == "index" and view == "inventory":
        return [
            {"href": PREFIX + "/add", "label": "Add repositories"},
            {"href": PREFIX + "/queue", "label": "Queue and workers"},
        ]


@hookimpl
def menu_links(datasette: Any) -> Any:
    if enabled(datasette):
        return [
            {"href": inventory_url(), "label": "Cache22 inventory"},
            {"href": PREFIX + "/add", "label": "Add repositories"},
            {"href": PREFIX + "/queue", "label": "Queue and workers"},
        ]


@hookimpl
def extra_js_urls(datasette: Any) -> list[dict[str, Any]]:
    return (
        [{"url": "/-/static-plugins/cache22.manager/manager.js", "module": True}]
        if enabled(datasette)
        else []
    )


@hookimpl
def extra_css_urls(datasette: Any) -> list[str]:
    return ["/-/static-plugins/cache22.manager/manager.css"] if enabled(datasette) else []


@hookimpl
def render_cell(
    datasette: Any, database: str, table: str, column: str, value: Any, row: Any
) -> Any:
    if not enabled(datasette) or database != "index" or table != "inventory":
        return None
    if column == "id":
        return Markup(
            '<label><input type="checkbox" class="c22-select" value="{}" aria-label="Select repository {}"> <a href="{}/repository/{}">{}</a></label>'
        ).format(value, value, PREFIX, value, value)
    if column.endswith("_at") and isinstance(value, int):
        stamp = datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")
        return Markup('<time datetime="{}">{}</time>').format(stamp, stamp.replace("T", " "))
    if column in {"scheduled", "schedule_blocked", "queued", "running", "has_error"}:
        return "yes" if value else "no"
    return None


@hookimpl
def top_table(datasette: Any, database: str, table: str, request: Any) -> Any:
    if not enabled(datasette) or database != "index" or table != "inventory":
        return None
    return Markup("""<section id="c22-inventory" class="c22-panel">
    <label>Search names and keys <input id="c22-search" type="search" value="{}"></label>
    <button type="button" id="c22-search-apply">Search</button>
    <p>Remote status is the last observed comparison, dated by the last successful check.</p>
    <button type="button" id="c22-page-select">Select this page</button>
    <button type="button" id="c22-all-select">Select all filtered</button>
    <button type="button" id="c22-clear-select">Clear selection</button>
    <span id="c22-selection-count" aria-live="polite"></span>
    <div id="c22-actions"></div><div id="c22-results" aria-live="polite"></div>
    <p id="c22-connection" role="status"></p></section>""").format(
        escape(request.args.get("_q", ""))
    )


@hookimpl
def asgi_wrapper(datasette: Any) -> Any:
    if not enabled(datasette):
        return None

    def wrap(app: Any) -> Any:
        async def guarded(scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] not in {"http", "websocket"}:
                return await app(scope, receive, send)
            headers = {k.lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            host = headers.get(b"host", "")
            try:
                parsed = urlsplit("http://" + host)
                valid = (
                    parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                    and not parsed.username
                    and not parsed.path
                    and parsed.port != 0
                )
                origin = headers.get(b"origin")
                if origin is not None:
                    valid = valid and origin == f"{scope.get('scheme', 'http')}://{host}"
                if scope.get("method") not in {"GET", "HEAD", "OPTIONS"}:
                    valid = valid and headers.get(b"sec-fetch-site", "same-origin") in {
                        "same-origin",
                        "none",
                    }
            except ValueError:
                valid = False
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            elif not valid:
                await Response.text("Untrusted Host or Origin", status=403).asgi_send(send)
            else:
                await app(scope, receive, send)

        return guarded

    return wrap
