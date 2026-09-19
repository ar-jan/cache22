"""Operation deadlines and claim fencing, shared by direct and queued Git work."""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


class ClaimLostError(RuntimeError):
    pass


class TransportError(RuntimeError):
    pass


@dataclass
class Operation:
    guard: Callable[[], None]
    deadline: float
    validate_db: Callable[[sqlite3.Connection], None]


current_operation: ContextVar[Operation | None] = ContextVar("operation", default=None)
lock_fds: ContextVar[tuple[int, ...]] = ContextVar("lock_fds", default=())


@contextmanager
def inherited_lock(fd: int) -> Iterator[None]:
    token = lock_fds.set((*lock_fds.get(), fd))
    try:
        yield
    finally:
        lock_fds.reset(token)


def guard() -> None:
    operation = current_operation.get()
    if operation:
        operation.guard()
        if time.monotonic() >= operation.deadline:
            raise TransportError("Operation timed out")


def run(
    args: list[str],
    *,
    check: bool = False,
    capture_output: bool = False,
    text: bool = False,
    env: dict[str, str] | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Keep inherited lock descriptors alive even if the parent is killed.

    In an indexed operation, poll the claim while waiting; kill the entire Git
    process group before relinquishing locks on cancellation or timeout.
    """
    operation = current_operation.get()
    if operation is None:
        return subprocess.run(
            args,
            check=check,
            capture_output=capture_output,
            text=text,
            env=env,
            pass_fds=lock_fds.get(),
            **kwargs,
        )
    guard()
    env = dict(os.environ if env is None else env)
    env["GIT_TERMINAL_PROMPT"] = "0"
    with subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        text=text,
        env=env,
        pass_fds=lock_fds.get(),
        start_new_session=True,
        **kwargs,
    ) as process:
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    guard()
            guard()
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
        result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result
