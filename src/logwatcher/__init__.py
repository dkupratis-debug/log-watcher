"""Watch logs and report notable events.

``parse_log`` reads a text log or a JSON Lines log into structured records.
Spike detection and pattern detection are not implemented yet, and the
command-line tool does not read log files yet.
"""

from __future__ import annotations

from logwatcher.parser import LogRecord, ParseError, ParseResult, parse_log

__all__ = [
    "LogRecord",
    "ParseError",
    "ParseResult",
    "parse_log",
    "__version__",
]

__version__ = "0.1.0"
