from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from cache22.index import Index
from cache22.job_queue import Queue
from cache22.repo_service import add_repository


def test_worker_sigterm_stops_git_and_recovers_attempt(tmp_path: Path) -> None:
    index = Index()
    repository = add_repository("https://host/team/repo", tmp_path, index=index)
    queue = Queue(index)
    queue.enqueue(repository["id"], "check")
    # A real child replaces the remote transport. No provider or network dependency.
    git = tmp_path / "git"
    pid_file = tmp_path / "git.pid"
    git.write_text(
        f"#!{sys.executable}\nimport os,time\nfrom pathlib import Path\nPath({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(60)\n"
    )
    git.chmod(0o755)
    process = subprocess.Popen(
        [sys.executable, "-m", "cache22", "worker", "run", "--continuous"],
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.05)
        assert pid_file.exists()
        git_pid = int(pid_file.read_text())
        process.terminate()
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, (stdout, stderr)
        with pytest.raises(ProcessLookupError):
            os.kill(git_pid, 0)
        job = queue.list(repository["id"])[0]
        assert job["state"] == "pending"
        assert job["retry_count"] == 0
        assert job["attempts"][0]["outcome"] == "interrupted"
        assert index.get(repository["id"])["reconciliation_required"]
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def test_combined_launcher_owns_worker_and_fails_with_child(tmp_path: Path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with (tmp_path / "manager.log").open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "cache22", "manager", "run", "--port", str(port)],
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 10
            workers = []
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    with urllib.request.urlopen(
                        base + "/-/cache22/api/health", timeout=0.2
                    ) as response:
                        workers = json.load(response)["workers"]
                    if workers:
                        break
                except OSError:
                    pass
                time.sleep(0.05)
            assert len(workers) == 1
            worker_pid = workers[0]["pid"]
            # Opening another page cannot launch another worker.
            urllib.request.urlopen(base + "/-/cache22/queue", timeout=2).close()
            with urllib.request.urlopen(base + "/-/cache22/api/health", timeout=2) as response:
                assert len(json.load(response)["workers"]) == 1
            os.kill(worker_pid, signal.SIGTERM)
            process.wait(timeout=10)
            assert process.returncode == 1
            with pytest.raises(urllib.error.URLError, match="Connection refused"):
                urllib.request.urlopen(base, timeout=0.5)
            with pytest.raises(ProcessLookupError):
                os.kill(worker_pid, 0)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
