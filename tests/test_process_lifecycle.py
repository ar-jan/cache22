from __future__ import annotations

import json
import os
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
    index = Index.initialize()
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
        [sys.executable, "-m", "cache22", "worker"],
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


def test_web_runs_without_worker_and_stops_independently(tmp_path: Path) -> None:
    index = Index.initialize()
    repository = add_repository("https://host/team/repo", tmp_path, index=index)
    queue = Queue(index)
    queue.enqueue(repository["id"], "check")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with (tmp_path / "web.log").open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "cache22", "web", "--port", str(port)],
            stdout=log,
            stderr=log,
        )
        worker = None
        try:
            deadline = time.monotonic() + 10
            health = None
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    with urllib.request.urlopen(base + "/api/health", timeout=0.2) as response:
                        health = json.load(response)
                    break
                except OSError:
                    time.sleep(0.05)
            assert health is not None
            assert health["workers"] == []
            urllib.request.urlopen(base + "/queue", timeout=2).close()
            assert queue.list()[0]["state"] == "pending"
            assert queue.list()[0]["attempts"] == []
            queue.unqueue(repository["id"])
            worker = subprocess.Popen(
                [sys.executable, "-m", "cache22", "worker"], stdout=log, stderr=log
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and worker.poll() is None:
                with urllib.request.urlopen(base + "/api/health", timeout=1) as response:
                    health = json.load(response)
                if health["workers"]:
                    break
                time.sleep(0.05)
            assert len(health["workers"]) == 1
            assert health["workers"][0]["pid"] == worker.pid
            process.terminate()
            process.wait(timeout=10)
            assert process.returncode in (0, -15)
            assert worker.poll() is None
            assert queue.list()[0]["state"] == "cancelled"
            with pytest.raises(urllib.error.URLError, match="Connection refused"):
                urllib.request.urlopen(base, timeout=0.5)
        finally:
            if worker is not None and worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=10)
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
