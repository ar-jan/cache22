"""Operation deadlines and claim fencing, shared by direct and queued Git work."""

from __future__ import annotations

import os
import selectors
import signal
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from .diagnostics import GitOutputDecoder, parse_git_progress, sanitize


class ClaimLostError(RuntimeError):
    pass


class TransportError(RuntimeError):
    pass


class OperationInterrupted(RuntimeError):
    pass


@dataclass
class Operation:
    guard: Callable[[], None]
    deadline: float
    validate_db: Callable[[sqlite3.Connection], None]
    publish: Callable[[dict[str, Any]], None] = lambda snapshot: None


def progress(phase: str, **fields: Any) -> None:
    operation = current_operation.get()
    if operation is not None:
        if "detail" in fields:
            fields["detail"] = sanitize(fields["detail"])
        operation.publish({"phase": phase, **fields})


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
    observe_progress: bool = False,
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
    if observe_progress:
        return _run_streaming(
            args, check=check, capture_output=capture_output, text=text, env=env, **kwargs
        )
    input_data = kwargs.pop("input", None)
    with subprocess.Popen(
        args,
        stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
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
                    stdout, stderr = process.communicate(input=input_data, timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    input_data = None
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


def _run_streaming(
    args: list[str],
    *,
    check: bool,
    capture_output: bool,
    text: bool,
    env: dict[str, str],
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    # Binary reads preserve carriage-return progress and never wait for a newline.
    with (
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE,
            env=env,
            pass_fds=lock_fds.get(),
            start_new_session=True,
            **kwargs,
        ) as process,
        selectors.DefaultSelector() as selector,
    ):
        assert process.stderr is not None
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        if process.stdout is not None:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        stdout = bytearray()
        stderr = bytearray()
        decoder = GitOutputDecoder()

        def observe(line: str) -> None:
            # Only sanitized complete lines enter the retained tail or publication.
            stderr.extend(line.encode("utf-8") + b"\n")
            del stderr[:-65536]
            snapshot = parse_git_progress(line)
            if snapshot is not None:
                progress(**snapshot)

        try:
            while selector.get_map() or process.poll() is None:
                guard()
                for key, _ in selector.select(0.2):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "stdout":
                        stdout.extend(chunk)
                        continue
                    for line in decoder.feed(chunk):
                        observe(line)
            for line in decoder.finish():
                observe(line)
            guard()
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
        out: Any = bytes(stdout) if capture_output else None
        err: Any = bytes(stderr)
        if text:
            out = out.decode("utf-8", errors="replace") if out is not None else None
            err = err.decode("utf-8", errors="replace")
        result = subprocess.CompletedProcess(args, process.wait(), out, err)
        if check:
            result.check_returncode()
        return result
