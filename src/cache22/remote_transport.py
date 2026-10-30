"""Prefer SSH for remote Git commands and fall back to HTTPS when SSH fails."""

from __future__ import annotations

import subprocess
from collections.abc import Callable

from .diagnostics import failure_message
from .errors import TransportError
from .repository_ref import transport_urls


def with_transport_fallback[T](
    url: str,
    attempt: Callable[[str], T],
    summary: Callable[[subprocess.CalledProcessError], str],
) -> tuple[str, T]:
    """Run ``attempt`` per transport URL until one succeeds; return that URL and result.

    Only a failed Git command (``CalledProcessError``) moves on to the next transport.
    Deadlines, cancellation, and local failures propagate from the attempt unchanged.
    """
    candidates = transport_urls(url)
    sources = (url, *candidates)
    failures: list[tuple[str, subprocess.CalledProcessError]] = []
    for candidate in candidates:
        try:
            return candidate, attempt(candidate)
        except subprocess.CalledProcessError as exc:
            failures.append((candidate, exc))
    if len(failures) == 1:
        exc = failures[0][1]
        raise TransportError(failure_message(summary(exc), exc, *sources)) from exc
    limit = 4000 // len(failures) - 1
    message = "\n".join(
        failure_message(f"{_transport(candidate)}: {summary(exc)}", exc, *sources, limit=limit)
        for candidate, exc in failures
    )
    raise TransportError(message) from failures[-1][1]


def _transport(url: str) -> str:
    return "HTTPS" if url.casefold().startswith("https://") else "SSH"
