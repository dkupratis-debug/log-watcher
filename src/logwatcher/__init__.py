"""Watch logs and report notable events.

``parse_log`` reads a text log or a JSON Lines log into structured records.
``detect_spikes`` flags a sudden burst of errors and reports a steady high
rate as repeated. The ``logwatcher`` command scans saved logs and can follow
a file as it grows. Pattern detection is not implemented yet.
"""

from __future__ import annotations

from logwatcher.parser import LogRecord, ParseError, ParseResult, parse_log
from logwatcher.spikes import (
    DEFAULT_MIN_COUNT,
    DEFAULT_MULTIPLIER,
    DEFAULT_WINDOW,
    RepeatedType,
    Spike,
    SpikeReport,
    detect_spikes,
)

__all__ = [
    "DEFAULT_MIN_COUNT",
    "DEFAULT_MULTIPLIER",
    "DEFAULT_WINDOW",
    "LogRecord",
    "ParseError",
    "ParseResult",
    "RepeatedType",
    "Spike",
    "SpikeReport",
    "detect_spikes",
    "parse_log",
    "__version__",
]

__version__ = "0.1.0"
