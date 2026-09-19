"""Web child entrypoint; never launches workers or scheduling tasks."""

from __future__ import annotations

import sys
from typing import Any

import uvicorn

from ..worker import notify_ready
from .app import create_datasette


class Server(uvicorn.Server):
    async def startup(self, sockets: Any = None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            notify_ready()


def main() -> None:
    ds = create_datasette()
    Server(
        uvicorn.Config(
            ds.app(), host="127.0.0.1", port=int(sys.argv[1]), proxy_headers=False, access_log=False
        )
    ).run()


if __name__ == "__main__":
    main()
