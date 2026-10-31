"""Pure, bounded diagnostics for Git output and user-facing errors."""

from __future__ import annotations

import re
import subprocess
from typing import NotRequired, TypedDict


class ProgressSnapshot(TypedDict):
    phase: str
    detail: str
    percentage: NotRequired[int]
    completed: NotRequired[int]
    total: NotRequired[int]
    unit: NotRequired[str]


def sanitize(value: str, *sources: str) -> str:
    for source in sources:
        if source:
            value = value.replace(source, "<source URL>")
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    value = re.sub(r"(https?://|ssh://)[^\s/@]+@", r"\1<credentials>@", value)
    return "".join(c for c in value if c == "\n" or (ord(c) >= 32 and ord(c) != 127))[:4000]


def error_text(exc: BaseException, *sources: str) -> str:
    """One rendering boundary for persisted and returned exception diagnostics."""
    return sanitize(str(exc), *sources)


def failure_message(
    summary: str, error: subprocess.CalledProcessError, *sources: str, limit: int = 4000
) -> str:
    """Keep the final diagnostic after progress chatter, within the persisted error limit."""
    summary = sanitize(summary, *sources)[:limit]
    stderr = error.stderr
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if not stderr:
        return summary
    # Sanitize before taking the tail so truncation cannot split a credential-bearing URL.
    # Work line-by-line to avoid sanitize()'s prefix limit discarding the final error.
    detail = "\n".join(sanitize(line, *sources) for line in stderr.splitlines()).strip()
    remaining = limit - len(summary) - 1
    if not detail or remaining <= 0:
        return summary
    return summary + "\n" + detail[-remaining:]


def parse_git_progress(line: str) -> ProgressSnapshot | None:
    """Unknown Git messages remain bounded detail; percentages are phase-specific."""
    line = sanitize(line).strip()
    if not line:
        return None
    match = re.search(r"([\w ]+):\s+(\d+)%\s+\((\d+)/(\d+)\)", line)
    if match:
        phase, percent, completed, total = match.groups()
        try:
            percentage, done, count = int(percent), int(completed), int(total)
            if not (0 <= percentage <= 100 and 0 <= done <= count <= 2**63 - 1):
                raise ValueError("Invalid Git progress counters")
        except ValueError:
            pass
        else:
            return ProgressSnapshot(
                phase=phase.strip().lower(),
                percentage=percentage,
                completed=done,
                total=count,
                unit="objects",
                detail=line,
            )
    return ProgressSnapshot(phase="git transfer", detail=line)


class GitOutputDecoder:
    """Frame complete lines before redaction, with bounded memory.

    Oversized lines are omitted in their entirety: keeping either a prefix or a
    suffix could sever a credential-bearing URL before it can be recognized.
    Discard until the next CR/LF, then resume normal diagnostics.
    """

    line_limit = 65536
    omitted = "[Git output line omitted: exceeds 65536 bytes]"

    def __init__(self) -> None:
        self._pending = b""
        self._oversized = False

    def feed(self, chunk: bytes) -> list[str]:
        lines: list[str] = []
        parts = re.split(rb"[\r\n]", chunk)
        for position, part in enumerate(parts):
            if not self._oversized:
                if len(self._pending) + len(part) > self.line_limit:
                    self._pending = b""
                    self._oversized = True
                else:
                    self._pending += part
            if position < len(parts) - 1:
                lines.extend(self.finish())
        return lines

    def finish(self) -> list[str]:
        line = (
            self.omitted
            if self._oversized
            else sanitize(self._pending.decode("utf-8", errors="replace"))
        )
        self._pending = b""
        self._oversized = False
        return [line] if line else []
