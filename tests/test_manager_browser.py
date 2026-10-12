"""Browser acceptance: skipped when Playwright's Chromium is not installed."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from cache22 import operation
from cache22.config import add_archive_dir
from cache22.index import Index
from cache22.inventory_service import list_inventory
from cache22.job_operation import running_job
from cache22.job_queue import Queue
from cache22.repo_service import add_repository
from cache22.scheduler import Scheduler


def test_browser_selection_refresh_registration_and_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    archive = tmp_path / "archive"
    archive.mkdir()
    add_archive_dir(archive)
    index = Index.initialize()
    for i in range(3):
        add_repository(f"https://example.org/team/repo{i}", archive, index=index)
    with sync_playwright() as playwright:
        executable = os.environ.get("CACHE22_BROWSER", playwright.chromium.executable_path)
        if not Path(executable).exists():
            pytest.skip("Install Chromium with playwright install chromium, or set CACHE22_BROWSER")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        with (tmp_path / "web.log").open("w+") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "cache22",
                    "web",
                    "--port",
                    str(port),
                ],
                stdout=log,
                stderr=log,
            )
            browser = None
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    assert process.poll() is None
                    try:
                        urllib.request.urlopen(base, timeout=0.2).close()
                        break
                    except OSError:
                        time.sleep(0.05)
                browser = playwright.chromium.launch(executable_path=executable, headless=True)
                page = browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base)
                page.locator("#c22-actions button").wait_for()
                assert page.locator(".c22-select").count() == 3
                assert not page.locator("#c22-actions input").is_visible()
                page.locator("#c22-actions select").select_option("schedule")
                assert page.locator("#c22-actions input").is_visible()
                page.locator("#c22-actions select").select_option("check")
                assert not page.locator("#c22-actions input").is_visible()
                page.locator("#c22-search").fill("repo1")
                page.locator("#c22-search-apply").click()
                page.wait_for_url("**q=repo1**")
                page.locator("#c22-all-select").click()
                page.wait_for_function(
                    "document.querySelector('#c22-selection-count').textContent.startsWith('1 selected')"
                )
                page.locator("#c22-actions select").select_option("fetch")
                page.locator("#c22-actions button").click()
                page.wait_for_function(
                    "document.querySelector('#c22-results').textContent.includes('accepted')"
                )
                assert len(Queue(index).list()) == 1
                # Selection persists after navigation/reload and a snapshot refresh.
                page.reload()
                page.wait_for_function("document.querySelector('.c22-select')?.checked")
                page.wait_for_function(
                    "document.querySelector('#c22-connection').textContent.startsWith('Updated')"
                )
                assert page.locator(".c22-select").is_checked()
                page.goto(base + "/queue")
                page.locator("#c22-section").select_option("runnable")
                page.wait_for_function(
                    "document.querySelector('#c22-jobs').textContent.includes('repo1')"
                )
                assert "No available worker" in page.locator("#c22-workers").inner_text()
                repository_id = index.get("example.org/team/repo1")["id"]
                page.goto(base + f"/repositories/{repository_id}")
                page.locator("#c22-actions select").select_option("convert")
                page.locator("#c22-actions button").click()
                page.wait_for_function(
                    "document.querySelector('#c22-results').textContent.includes('accepted')"
                )
                assert Queue(index).list(repository_id)[0]["kind"] == "convert"
                page.goto(base + "/queue")
                page.locator("#c22-section").select_option("deferred")
                page.wait_for_function(
                    "document.querySelector('#c22-jobs').textContent.includes('convert')"
                )
                assert "Waiting for job" in page.locator("#c22-jobs").inner_text()
                page.goto(base + "/add")
                page.locator("#c22-urls").fill(
                    "https://example.org/team/new\ninvalid\nhttps://example.org/team/new"
                )
                page.locator("#c22-preview").click()
                page.wait_for_function(
                    "document.querySelector('#c22-results').textContent.includes('duplicate')"
                )
                assert len(list_inventory(index)) == 3
                page.locator("#c22-add button[type=submit]").click()
                page.wait_for_function(
                    "document.querySelector('#c22-connection').textContent === 'Submission complete.'"
                )
                assert len(list_inventory(index)) == 4
                # Page selection accumulates explicit IDs across pagination and filters.
                page.goto(base + "/?limit=1")
                page.locator("#c22-clear-select").click()
                page.locator("#c22-page-select").click()
                page.locator("#c22-next").click()
                page.locator("#c22-page-select").click()
                assert page.locator("#c22-selection-count").inner_text().startswith("2 selected")
                page.locator("#c22-search").fill("no-such-repository")
                page.locator("#c22-search-apply").click()
                page.wait_for_url("**q=no-such-repository**")
                assert (
                    "No matching repositories" in page.locator("#c22-inventory-data").inner_text()
                )
                assert page.locator("#c22-selection-count").inner_text().startswith("2 selected")
                page.goto(base + "/?limit=1&offset=100")
                assert (
                    "No repositories on this page"
                    in page.locator("#c22-inventory-data").inner_text()
                )
                page.locator("#c22-prev").click()
                assert page.locator(".c22-select").count() == 1
                page.goto(base + "/?limit=1")
                page.wait_for_function(
                    "document.querySelector('#c22-connection').textContent.startsWith('Updated')"
                )
                page.locator(".c22-select").focus()
                with page.expect_response("**/inventory/fragment*"):
                    page.evaluate("window.dispatchEvent(new Event('online'))")
                page.wait_for_function("document.activeElement?.classList.contains('c22-select')")
                assert page.locator(".c22-select").is_checked()
                # Failed refreshes retain rows and selection; reconnect recovers.
                page.route(
                    "**/inventory/fragment*",
                    lambda route: route.fulfill(status=503, body="Unavailable"),
                )
                page.evaluate("window.dispatchEvent(new Event('online'))")
                page.wait_for_function(
                    "document.querySelector('#c22-connection').textContent.includes('Refresh failed')"
                )
                assert page.locator(".c22-select").is_checked()
                page.unroute("**/inventory/fragment*")
                page.evaluate("window.dispatchEvent(new Event('online'))")
                page.wait_for_function(
                    "document.querySelector('#c22-connection').textContent.startsWith('Updated')"
                )
                # Progress and attempt details come from the independently updated index.
                scheduler = Scheduler(index)
                job = scheduler.claim()
                assert job is not None
                with running_job(Queue(index), job, 60):
                    operation.progress("fetching", completed=1, total=2, percentage=50)
                page.goto(base + "/queue")
                page.wait_for_function("document.querySelector('#c22-jobs progress')?.value === 50")
                scheduler.finish(job)
                page.goto(base + f"/repositories/{repository_id}")
                page.wait_for_function(
                    "document.querySelector('#c22-attempts').textContent.includes('succeeded')"
                )
                assert not errors
            finally:
                if browser is not None:
                    browser.close()
                process.terminate()
                process.wait(timeout=15)
