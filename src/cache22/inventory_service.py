"""Shared, parameterized inventory reads. Never inspect archives or contact Git."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qs, urlencode

from .index import Index

MAX_SELECTION = 10_000
CATEGORIES = ("host", "archive_root", "local_state", "remote_status", "storage_format")
FLAGS = (
    "queued",
    "running",
    "scheduled",
    "schedule_blocked",
    "has_error",
    "reconciliation_required",
)
ENUMS = {
    "local_state": ("unknown", "absent", "ready", "missing", "incomplete"),
    "remote_status": ("unknown", "not_fetched", "current", "updates_available"),
    "storage_format": ("git", "bundle"),
}
SORTS = (
    "repo_key",
    "project_name",
    "host",
    "archive_root",
    "local_state",
    "remote_status",
    "storage_format",
    "local_head_committed_at",
    "last_checked_at",
    "last_fetched_at",
    "last_converted_at",
    "next_due_at",
)
# Explicit browser/download projection, independent of future schema additions.
PUBLIC_FIELDS = (
    "id",
    "repo_key",
    "project_name",
    "display_path",
    "host",
    "source_path",
    "archive_root",
    "storage_format",
    "archive_file",
    "repository_dir",
    "archive_path",
    "created_at",
    "updated_at",
    "local_state",
    "local_observed_at",
    "reconciliation_required",
    "local_head_ref",
    "local_head_oid",
    "local_head_committed_at",
    "local_ref_digest",
    "remote_head_ref",
    "remote_head_oid",
    "remote_ref_digest",
    "remote_status",
    "last_checked_at",
    "last_fetched_at",
    "last_converted_at",
    "last_error",
    "last_error_kind",
    "last_error_category",
    "last_error_at",
    "has_error",
    "queued",
    "running",
    "scheduled",
    "schedule_blocked",
    "interval_seconds",
    "next_due_at",
)


@dataclass(frozen=True)
class InventoryQuery:
    q: str = ""
    host: tuple[str, ...] = ()
    archive_root: tuple[str, ...] = ()
    local_state: tuple[str, ...] = ()
    remote_status: tuple[str, ...] = ()
    storage_format: tuple[str, ...] = ()
    queued: bool | None = None
    running: bool | None = None
    scheduled: bool | None = None
    schedule_blocked: bool | None = None
    has_error: bool | None = None
    reconciliation_required: bool | None = None
    sort: str = "repo_key"
    descending: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.q, str) or self.sort not in SORTS:
            raise ValueError("Invalid search or sort field")
        if type(self.descending) is not bool:
            raise ValueError("descending must be boolean")
        for field in CATEGORIES:
            values = getattr(self, field)
            if not isinstance(values, tuple) or any(
                not isinstance(v, str) or not v for v in values
            ):
                raise ValueError(f"{field} must contain nonempty strings")
            if field in ENUMS and any(v not in ENUMS[field] for v in values):
                raise ValueError(f"Invalid {field}")
        for field in FLAGS:
            value = getattr(self, field)
            if value is not None and type(value) is not bool:
                raise ValueError(f"{field} must be boolean")

    def parameters(self) -> dict[str, Any]:
        result = {field: getattr(self, field) for field in CATEGORIES if getattr(self, field)}
        result.update(
            {
                field: "true" if getattr(self, field) else "false"
                for field in FLAGS
                if getattr(self, field) is not None
            }
        )
        if self.q:
            result["q"] = self.q
        result["sort"] = self.sort
        if self.descending:
            result["descending"] = "true"
        return result


@dataclass(frozen=True)
class InventoryRequest:
    query: InventoryQuery = InventoryQuery()
    limit: int = 100
    offset: int = 0

    def __post_init__(self) -> None:
        if type(self.limit) is not int or not 1 <= self.limit <= 500:
            raise ValueError("Limit must be 1–500")
        if type(self.offset) is not int or not 0 <= self.offset <= 2**63 - 1:
            raise ValueError("Offset must be a nonnegative SQLite integer")

    def url(self, path: str = "/", **changes: Any) -> str:
        values = self.query.parameters() | {"limit": self.limit, "offset": self.offset}
        values.update(changes)
        return path + "?" + urlencode(values, doseq=True)


def parse_query(raw: str) -> InventoryRequest:
    values = parse_qs(raw, keep_blank_values=True, max_num_fields=200)
    unknown = values.keys() - {*CATEGORIES, *FLAGS, "q", "sort", "descending", "limit", "offset"}
    if unknown:
        raise ValueError(f"Unsupported inventory parameters: {', '.join(sorted(unknown))}")
    data: dict[str, Any] = {}
    page: dict[str, int] = {}
    for field, items in values.items():
        if field in CATEGORIES:
            data[field] = tuple(dict.fromkeys(item for item in items if item))
            continue
        if len(items) != 1:
            raise ValueError(f"Provide {field} once")
        value = items[0]
        if field in FLAGS or field == "descending":
            if not value:
                continue
            if value not in {"true", "false"}:
                raise ValueError(f"{field} must be true or false")
            data[field] = value == "true"
        elif field in {"limit", "offset"}:
            page[field] = int(value)
        else:
            data[field] = value
    return InventoryRequest(InventoryQuery(**data), **page)


def _predicate(query: InventoryQuery) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if query.q:
        clauses.append(
            "(instr(lower(project_name),lower(?))>0 OR instr(lower(repo_key),lower(?))>0)"
        )
        params.extend([query.q, query.q])
    for field in CATEGORIES:
        values = getattr(query, field)
        if values:
            clauses.append(f"{field} IN ({','.join('?' for _ in values)})")
            params.extend(values)
    for field in FLAGS:
        value = getattr(query, field)
        if value is not None:
            clauses.append(f"{field}=?")
            params.append(int(value))
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def _rows(
    db: sqlite3.Connection, query: InventoryQuery, page: InventoryRequest | None = None
) -> list[dict[str, Any]]:
    where, params = _predicate(query)
    sql = f"SELECT * FROM inventory{where} ORDER BY {query.sort} {'DESC' if query.descending else 'ASC'},id"
    if page:
        sql += " LIMIT ? OFFSET ?"
        params.extend([page.limit, page.offset])
    return [Index._record(row) for row in db.execute(sql, params)]


def list_inventory(
    index: Index, query: InventoryQuery | None = None, *, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    query = query or InventoryQuery()
    page = InventoryRequest(query, limit, offset)
    with index.connect() as db:
        return _rows(db, query, page)


@dataclass(frozen=True)
class InventoryPage:
    rows: list[dict[str, Any]]
    total: int
    facets: dict[str, list[dict[str, Any]]]
    request: InventoryRequest

    @property
    def previous_offset(self) -> int | None:
        if not self.request.offset:
            return None
        last = max(0, (self.total - 1) // self.request.limit * self.request.limit)
        return min(last, max(0, self.request.offset - self.request.limit))

    @property
    def next_offset(self) -> int | None:
        offset = self.request.offset + self.request.limit
        return offset if offset < self.total else None


def public_record(row: dict[str, Any]) -> dict[str, Any]:
    return {field: row[field] for field in PUBLIC_FIELDS}


def inventory_page(index: Index, request: InventoryRequest | None = None) -> InventoryPage:
    request = request or InventoryRequest()
    query = request.query
    where, params = _predicate(query)
    with index.connect() as db:
        db.execute("BEGIN")
        total = db.execute(f"SELECT count(*) FROM inventory{where}", params).fetchone()[0]
        rows = [public_record(row) for row in _rows(db, query, request)]
        facets = {}
        for field in ("host", "local_state", "remote_status"):
            clause, bindings = _predicate(replace(query, **{field: ()}))
            facets[field] = [
                dict(row)
                for row in db.execute(
                    f"SELECT {field} AS value,count(*) AS count FROM inventory{clause} GROUP BY {field} ORDER BY {field}",
                    bindings,
                )
            ]
    return InventoryPage(rows, total, facets, request)


def selected_ids(index: Index, query: InventoryQuery) -> list[int]:
    where, params = _predicate(query)
    with index.connect() as db:
        rows = db.execute(
            f"SELECT id FROM inventory{where} ORDER BY id LIMIT ?", [*params, MAX_SELECTION + 1]
        ).fetchall()
    if len(rows) > MAX_SELECTION:
        raise ValueError(f"Selection exceeds {MAX_SELECTION} repositories; narrow the filters")
    return [row[0] for row in rows]


def export_inventory(index: Index, query: InventoryQuery) -> list[dict[str, Any]]:
    with index.connect() as db:
        return [public_record(row) for row in _rows(db, query)]
