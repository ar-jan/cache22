"""Construct the personal-manager Datasette configuration."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from ..index import Index


def create_datasette(path: Path | None = None) -> Any:
    os.environ["DATASETTE_LOAD_PLUGINS"] = ""
    from datasette.app import Datasette
    from datasette.database import Database
    from datasette.plugins import pm

    from . import __name__ as plugin_name
    from . import register_routes

    plugin = sys.modules[plugin_name]
    if not any(getattr(p, "register_routes", None) is register_routes for p in pm.get_plugins()):
        pm.register(plugin, name="cache22-manager")
    index = Index.initialize(path)
    ds: Any = Datasette(
        default_deny=True,
        cache_headers=False,
        config={
            "permissions": {
                name: True
                for name in (
                    "view-instance",
                    "view-database",
                    "view-table",
                    "view-query",
                    "execute-sql",
                    "view-database-download",
                    "cache22-manage",
                )
            },
            "databases": {
                "index": {
                    "tables": {
                        "inventory": {
                            "title": "Cache22 inventory",
                            "sort": "repo_key",
                            "size": 100,
                        }
                    }
                }
            },
        },
        settings={
            "default_allow_sql": True,
            "default_cache_ttl": 0,
            "max_returned_rows": 1000,
            "sql_time_limit_ms": 2000,
        },
    )
    ds.cache22_index = index
    ds.add_database(Database(ds, path=str(index.path), is_mutable=True), name="index")
    return ds
