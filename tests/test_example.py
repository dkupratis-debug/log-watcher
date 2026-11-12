"""The README sample is a real spike and a real repeated type."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from logwatcher import detect_spikes, parse_log
from logwatcher.spikes import RepeatedType, Spike

ROOT = Path(__file__).resolve().parents[1]

# Captured from `python3 examples/report.py examples/api.log`.
DEMO_STDOUT = """\
records: 23
skipped: 1
spikes: 1
  [1] connection refused
      count: 6
      baseline_median: 1.0
      window: 2024-03-01T12:15:00+00:00 .. 2024-03-01T12:20:00+00:00
repeated: 1
  [1] disk full
      median: 3.0
      counts: 3, 3, 3, 3
      windows:
        2024-03-01T12:00:00+00:00
        2024-03-01T12:05:00+00:00
        2024-03-01T12:10:00+00:00
        2024-03-01T12:15:00+00:00
"""


def _at(minute: int) -> datetime:
    return datetime(2024, 3, 1, 12, minute, tzinfo=UTC)


def test_example_log_has_one_spike_and_one_repeated_type() -> None:
    result = parse_log(ROOT / "examples" / "api.log")
    assert len(result.records) == 23
    assert result.skipped == 1
    report = detect_spikes(result.records)
    assert report.spikes == (
        Spike(
            error_type="connection refused",
            window_start=_at(15),
            window_end=_at(20),
            count=6,
            baseline_median=1.0,
        ),
    )
    assert report.repeated == (
        RepeatedType(
            error_type="disk full",
            window_starts=(_at(0), _at(5), _at(10), _at(15)),
            counts=(3, 3, 3, 3),
            median=3.0,
        ),
    )


def test_readme_demo_matches_the_example_command() -> None:
    completed = subprocess.run(
        [sys.executable, "examples/report.py", "examples/api.log"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stderr == ""
    assert completed.stdout == DEMO_STDOUT
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "python3 examples/report.py examples/api.log" in readme
    assert DEMO_STDOUT in readme
    recording = (ROOT / "docs" / "spike-demo.svg").read_text(encoding="utf-8")
    assert "python3 examples/report.py examples/api.log" in recording
    for line in DEMO_STDOUT.splitlines():
        assert line in recording
