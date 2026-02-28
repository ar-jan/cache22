from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from cache22.cli import app


def test_status_fossil_reports_version() -> None:
    runner = CliRunner()
    fossil_path = Path("/tmp/fossil")
    completed = subprocess.CompletedProcess(
        args=[str(fossil_path), "version"],
        returncode=0,
        stdout="fossil 2.27\n",
        stderr="",
    )

    with patch("cache22.fossil.shutil.which", return_value=str(fossil_path)):
        with patch("cache22.fossil.subprocess.run", return_value=completed) as run:
            result = runner.invoke(app, ["status", "fossil"])

    assert result.exit_code == 0
    assert f"path: {fossil_path.resolve()}" in result.output
    assert "version: fossil 2.27" in result.output
    assert "Traceback" not in result.output
    run.assert_called_once_with(
        [str(fossil_path), "version"],
        check=True,
        capture_output=True,
        text=True,
    )


def test_status_fossil_reports_missing_binary_without_traceback() -> None:
    runner = CliRunner()

    with patch("cache22.fossil.shutil.which", return_value=None):
        result = runner.invoke(app, ["status", "fossil"])

    assert result.exit_code == 1
    assert "fossil executable not found in PATH" in result.output
    assert "Traceback" not in result.output


def test_status_fossil_reports_version_failure_without_traceback() -> None:
    runner = CliRunner()
    fossil_path = Path("/tmp/fossil")
    error = subprocess.CalledProcessError(
        returncode=2,
        cmd=[str(fossil_path), "version"],
    )

    with patch("cache22.fossil.shutil.which", return_value=str(fossil_path)):
        with patch("cache22.fossil.subprocess.run", side_effect=error):
            result = runner.invoke(app, ["status", "fossil"])

    assert result.exit_code == 1
    assert "fossil version command failed with exit code 2" in result.output
    assert "Traceback" not in result.output
