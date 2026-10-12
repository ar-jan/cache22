"""Run the web server independently of workers."""

from __future__ import annotations

from typing import Annotated

import typer
import uvicorn

from ..cli_support import command
from .app import create_app


@command
def serve(port: Annotated[int, typer.Option(min=1, max=65535)] = 8001) -> None:
    """Serve the browser manager on loopback; start workers separately."""
    app = create_app()
    uvicorn.run(app, host="127.0.0.1", port=port, proxy_headers=False, access_log=False)
