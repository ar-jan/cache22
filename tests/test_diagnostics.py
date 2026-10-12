"""Diagnostics remain useful and safe across Git stream boundaries."""

import subprocess
import sys
import time

import pytest

from cache22 import operation
from cache22.diagnostics import GitOutputDecoder, failure_message, parse_git_progress


@pytest.mark.parametrize(
    "line",
    [
        "Receiving objects: 101% (5/10)",
        "Receiving objects: 50% (11/10)",
        f"Receiving objects: 50% (1/{2**63})",
        "Receiving objects: malformed",
    ],
)
def test_invalid_progress_remains_detail(line: str) -> None:
    assert parse_git_progress(line) == {"phase": "git transfer", "detail": line}


def test_progress_parser_is_pure_and_redacts_detail() -> None:
    assert parse_git_progress("\x1b[31mReceiving objects: 50% (5/10)\x1b[0m") == {
        "phase": "receiving objects",
        "percentage": 50,
        "completed": 5,
        "total": 10,
        "unit": "objects",
        "detail": "Receiving objects: 50% (5/10)",
    }
    assert parse_git_progress("\r\n") is None
    snapshot = parse_git_progress("fatal: https://user:secret@host/repo")
    assert snapshot is not None and "secret" not in snapshot["detail"]


@pytest.mark.parametrize("chunk_size", [1, 4096, 8192])
def test_decoder_redacts_before_truncating_and_resumes_after_oversized_line(
    chunk_size: int,
) -> None:
    payload = (
        b"https://user:secret@host/"
        + b"x" * 4096
        + b"\r"
        + b"https://user:"
        + b"s" * 70000
        + b"secret@host/\n"
        + b"fatal: https://user:secret@host/repo: connection refused"
    )
    decoder = GitOutputDecoder()
    lines: list[str] = []
    for offset in range(0, len(payload), chunk_size):
        lines.extend(decoder.feed(payload[offset : offset + chunk_size]))
    lines.extend(decoder.finish())
    assert len(lines) == 3
    assert "secret" not in "\n".join(lines)
    assert lines[1] == decoder.omitted
    assert lines[-1].endswith("connection refused")
    assert all(len(line) <= 4000 for line in lines)


def test_failure_message_retains_final_error_and_bounds_summary() -> None:
    error = subprocess.CalledProcessError(
        128, "git", stderr=b"progress\r" * 10000 + b"fatal: https://u:secret@host/repo refused"
    )
    message = failure_message("Fetch failed", error)
    assert len(message) <= 4000
    assert message.endswith("@host/repo refused") and "secret" not in message
    assert len(failure_message("x" * 5000, error)) == 4000


def test_streaming_redacts_progress_and_retained_failure() -> None:
    snapshots: list[dict] = []
    token = operation.current_operation.set(
        operation.Operation(lambda: None, time.monotonic() + 10, lambda db: None, snapshots.append)
    )
    script = (
        "import os; "
        "os.write(2, b'https://user:secret@host/' + b'x'*4096 + b'\\r'); "
        "os.write(2, b'https://user:' + b's'*70000 + b'secret@host/\\n'); "
        "os.write(2, b'fatal: connection refused'); raise SystemExit(128)"
    )
    try:
        with pytest.raises(subprocess.CalledProcessError) as caught:
            operation.run([sys.executable, "-c", script], check=True, observe_progress=True)
    finally:
        operation.current_operation.reset(token)
    assert snapshots and "secret" not in str(snapshots)
    assert b"secret" not in caught.value.stderr
    assert b"connection refused" in caught.value.stderr
    assert len(caught.value.stderr) <= 65536
