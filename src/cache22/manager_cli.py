"""Own one web process and, unless web-only, one independent worker."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import threading
import time

import typer

from .index import Index
from .repo_cli import command
from .worker import shutdown_signals

manager_app = typer.Typer(help="Run the personal Datasette manager.", no_args_is_help=True)


@manager_app.command("run")
@command
def run(port: int = 8001, web_only: bool = False) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    Index()  # Initialize WAL/schema before children open the same new database.
    stop = threading.Event()
    children: list[subprocess.Popen[bytes]] = []
    with shutdown_signals(stop), selectors.DefaultSelector() as selector:
        try:
            commands = [[sys.executable, "-m", "cache22.manager.web", str(port)]]
            if not web_only:
                commands.append([sys.executable, "-m", "cache22", "worker", "run", "--continuous"])
            for args in commands:
                read_fd, write_fd = os.pipe()
                try:
                    child = subprocess.Popen(
                        args,
                        env={
                            **os.environ,
                            "CACHE22_READY_FD": str(write_fd),
                            "DATASETTE_LOAD_PLUGINS": "",
                        },
                        pass_fds=(write_fd,),
                        start_new_session=True,
                    )
                    children.append(child)
                    selector.register(read_fd, selectors.EVENT_READ)
                except BaseException:
                    os.close(read_fd)
                    raise
                finally:
                    os.close(write_fd)
            deadline = time.monotonic() + 20
            while selector.get_map() and not stop.is_set():
                if any(child.poll() is not None for child in children):
                    raise RuntimeError("Manager child exited during startup")
                if time.monotonic() >= deadline:
                    raise RuntimeError("Manager startup timed out")
                for key, _ in selector.select(0.2):
                    if os.read(key.fd, 1) != b"1":
                        raise RuntimeError("Manager child failed before readiness")
                    selector.unregister(key.fd)
                    os.close(key.fd)
            if not stop.is_set():
                typer.echo(f"Cache22 manager: http://127.0.0.1:{port}/")
            while not stop.wait(0.2):
                if any(child.poll() is not None for child in children):
                    raise RuntimeError(
                        "Manager child exited unexpectedly; stopping remaining children"
                    )
        finally:
            for key in list(selector.get_map().values()):
                os.close(key.fd)
            for child in children:
                if child.poll() is None:
                    child.send_signal(signal.SIGTERM)
            deadline = time.monotonic() + 10
            for child in children:
                try:
                    child.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    typer.echo(f"Child {child.pid} did not stop gracefully; terminating", err=True)
                    child.kill()
                    child.wait()
