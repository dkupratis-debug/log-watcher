"""Smoke tests for the package scaffold."""

from __future__ import annotations

import subprocess
import sys
from importlib.metadata import distribution

import pytest

import logwatcher
from logwatcher.__main__ import USAGE_HINT, main


def test_package_imports() -> None:
    assert logwatcher.__version__ == "0.1.0"


def test_console_script_declared() -> None:
    entry_points = distribution("logwatcher").entry_points
    scripts = [ep for ep in entry_points if ep.group == "console_scripts"]
    assert len(scripts) == 1
    assert scripts[0].name == "logwatcher"
    assert scripts[0].value == "logwatcher.__main__:main"


def test_no_runtime_dependencies() -> None:
    requires = distribution("logwatcher").requires or []
    runtime = [req for req in requires if "extra ==" not in req]
    assert runtime == []


def test_main_prints_usage_hint_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == USAGE_HINT
    assert captured.err == ""


def test_module_help_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "logwatcher", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "usage: logwatcher" in result.stdout.lower()


def test_console_script_help_exits_zero() -> None:
    result = subprocess.run(
        ["logwatcher", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "usage: logwatcher" in result.stdout.lower()
