"""Smoke tests for the package scaffold."""

from __future__ import annotations

import subprocess
import sys
from importlib.metadata import distribution

import pytest

import logwatcher
from logwatcher.__main__ import main


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


def test_main_without_arguments_prints_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 0
    captured = capsys.readouterr()
    assert "usage: logwatcher" in captured.out.lower()
    assert "follow" in captured.out.lower()
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
