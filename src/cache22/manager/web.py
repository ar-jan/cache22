"""Run the web server independently of workers."""

from __future__ import annotations

import socket
from typing import Annotated

import typer
import uvicorn

from ..cli_support import command
from .app import LOOPBACK_HOSTS, create_app

WILDCARD_HOSTS = frozenset({"0.0.0.0", "::"})


def local_hostnames() -> set[str]:
    """Names a browser on this machine or the LAN may use to reach it."""
    names = {socket.gethostname(), socket.getfqdn()}
    names |= {f"{name}.local" for name in tuple(names) if name}
    return {name.lower() for name in names if name}


@command
def serve(
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8001,
    host: Annotated[
        str, typer.Option(help="Interface to bind; use 0.0.0.0 for all IPv4 interfaces.")
    ] = "127.0.0.1",
    allow_host: Annotated[
        list[str] | None,
        typer.Option("--allow-host", help="Additional Host header name to accept."),
    ] = None,
) -> None:
    """Serve the browser manager on HOST; start workers separately."""
    loopback = host in LOOPBACK_HOSTS
    allowed_hosts = set(LOOPBACK_HOSTS) | local_hostnames() | set(allow_host or ())
    if not loopback and host not in WILDCARD_HOSTS:
        allowed_hosts.add(host)
    app = create_app(
        allowed_hosts=allowed_hosts,
        allow_ip_literals=not loopback,
    )
    uvicorn.run(app, host=host, port=port, proxy_headers=False, access_log=False)
