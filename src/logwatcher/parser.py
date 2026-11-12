"""Parse common log lines into structured records.

Call ``parse_log`` with a file path or an iterable of lines. It returns
a ``ParseResult`` of ``(records, skipped)``.

Supported shapes
----------------
ISO-8601 with a trailing level. The date and time are separated by ``T``
or a space. A timezone (``Z`` or ``±HH:MM``) is optional::

    2024-03-01T12:00:00Z ERROR connection refused

Syslog-style, matching Python logging's default ``asctime`` (a comma
before the milliseconds) plus ``LEVEL logger.name: message``::

    2024-03-01 12:00:00,123 ERROR app.worker: job failed

JSON Lines, one JSON object per line. The object needs a timestamp under
``ts`` or ``timestamp`` and a level under ``level`` or ``severity``.
The message is read from ``message`` or, if that is absent, ``msg``.
A numeric timestamp is seconds since the Unix epoch, or milliseconds
when the number is large enough to be milliseconds::

    {"ts": "2024-03-01T12:00:00Z", "level": "ERROR", "message": "disk full"}

Levels
------
``ERROR``, ``FATAL``, and ``CRITICAL`` are errors (``record.kind == "error"``).
``WARN`` and ``WARNING`` are warnings (``record.kind == "warning"``).
Every other level is context (``record.kind == "context"``). Matching is
case-insensitive; the stored level word is uppercased, so ``WARN`` and
``WARNING`` stay distinct.

Blank lines are ignored. Any other line that does not match is counted in
``skipped`` and is not stored as an error record. When the source contains
at least one non-blank line and none of them match, ``parse_log`` raises
``ParseError`` instead of returning an empty success.

A timestamp written without a timezone is a naive datetime. Treat a naive
value as UTC when an absolute instant is needed. ``Z``, a numeric offset,
and Unix epoch numbers are timezone-aware.

Only the Python standard library is used.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NamedTuple

# The three buckets the rest of the tool can count. Stored as strings so a
# later JSON summary can emit them without converting an enum.
Kind = Literal["error", "warning", "context"]

_ERROR_LEVELS = frozenset({"ERROR", "FATAL", "CRITICAL"})
_WARNING_LEVELS = frozenset({"WARN", "WARNING"})

# Python's logging module writes milliseconds after a comma
# (2024-03-01 12:00:00,123). datetime.fromisoformat wants a dot.
_COMMA_FRACTION = re.compile(r"(\d{2}:\d{2}:\d{2}),(\d+)")

# 2024-03-01 12:00:00,123 ERROR app.worker: job failed
# The logger name is required so this shape is not confused with a plain
# "timestamp level message" line. It is not stored on the record; the
# message is the text after "name:".
_SYSLOG_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})"
    r"[ \t]+(?P<level>[A-Za-z]+)"
    r"[ \t]+(?P<logger>[\w.-]+)"
    r":[ \t]*(?P<message>.*)"
)

# 2024-03-01T12:00:00.123Z ERROR connection refused
# Fractional seconds and the timezone are optional. A comma is not
# accepted here; that form belongs to the syslog shape above.
_ISO_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?)"
    r"[ \t]+(?P<level>[A-Za-z]+)"
    r"(?:[ \t]+(?P<message>.*))?"
)

# JSON objects may use either name for each field. The first key in the
# tuple wins when both are present.
_TIMESTAMP_KEYS = ("ts", "timestamp")
_LEVEL_KEYS = ("level", "severity")
_MESSAGE_KEYS = ("message", "msg")

# A 13-digit value is milliseconds (about 1.7e12 today). A 10-digit value
# is seconds. 1e12 seconds would be year 33658, so a number that large is
# treated as milliseconds.
_EPOCH_MILLIS_THRESHOLD = 1_000_000_000_000


@dataclass(frozen=True)
class LogRecord:
    """One log line that matched a supported shape.

    Attributes:
        timestamp: When the event happened. Naive when the line had no
            timezone; treat a naive value as UTC.
        level: Level word, uppercased. ``WARN`` and ``WARNING`` both mean
            a warning, and both spellings are preserved.
        message: Text after the level. For a syslog line this is the text
            after ``name:``. Empty when the line has no message. For JSON,
            ``message`` is used before ``msg``.
        raw: The original line, without a trailing newline.
        kind: ``error``, ``warning``, or ``context``. See the module
            docstring for which level words land in each bucket.
    """

    timestamp: datetime
    level: str
    message: str
    raw: str
    kind: Kind


class ParseResult(NamedTuple):
    """Parsed records and how many non-blank lines were skipped.

    This is a ``(records, skipped)`` pair with names, so both of these work::

        result = parse_log(path)
        records = result.records
        skipped = result.skipped

        records, skipped = parse_log(path)

    Blank lines are not included in ``skipped``. A skipped line is a
    non-blank line that did not match any supported shape.
    """

    records: list[LogRecord]
    skipped: int


class ParseError(Exception):
    """The source had lines, and none of them matched a supported shape.

    An empty source, or a source made only of blank lines, is a successful
    empty result. This error is for the other case: there was something to
    read, and it was not a log this parser understands. Returning no records
    there would look like a healthy log with zero events.

    Attributes:
        skipped: How many non-blank lines were seen.
    """

    def __init__(self, skipped: int) -> None:
        self.skipped = skipped
        super().__init__(
            f"No log lines matched a supported format ({skipped} skipped). "
            "Expected an ISO-8601 timestamp with a trailing level "
            "(example: 2024-03-01T12:00:00Z ERROR connection refused), "
            "a syslog-style 'YYYY-MM-DD HH:MM:SS,mmm LEVEL name: message' "
            "line, or a JSON object with ts/timestamp and level/severity."
        )


def parse_log(source: str | Path | Iterable[str]) -> ParseResult:
    """Parse ``source`` into records and a skip count.

    ``source`` is a filesystem path (``str`` or ``pathlib.Path``) or an
    iterable of lines. A plain ``str`` is always a path. Pass a list (or
    any other non-string iterable, such as an open text file) to parse lines
    that are already in memory.

    Raises:
        FileNotFoundError: ``source`` is a path and no file is there.
        ParseError: The source has non-blank lines and none of them match.
    """
    # str and Path are both iterable. Check for a path first so a path is
    # not walked character by character, and a Path is not walked as its
    # directory parts.
    if isinstance(source, (str, Path)):
        lines: Iterable[str] = _read_path(Path(source))
    else:
        lines = source
    return _parse_lines(lines)


def _read_path(path: Path) -> list[str]:
    """Read a UTF-8 log file into lines, without trailing newlines."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Log file not found: {path}") from exc
    return text.splitlines()


def _parse_lines(lines: Iterable[str]) -> ParseResult:
    records: list[LogRecord] = []
    skipped = 0
    for line in lines:
        raw = line.rstrip("\r\n")
        # Blank and whitespace-only lines are not events and are not skips.
        if raw.strip() == "":
            continue
        record = _parse_line(raw)
        if record is None:
            skipped += 1
            continue
        records.append(record)
    # A source of only unmatched lines should fail loudly. An empty source
    # is a real result: there were no events to report.
    if not records and skipped:
        raise ParseError(skipped)
    return ParseResult(records, skipped)


def _parse_line(raw: str) -> LogRecord | None:
    """Return a record for ``raw``, or None when the line does not match."""
    stripped = raw.strip()
    if stripped.startswith("{"):
        # Lines that start with "{" are JSON Lines. A broken object is a
        # skip; it is not tried again as text.
        return _parse_json(stripped, raw)
    return _parse_text(stripped, raw)


def _parse_text(stripped: str, raw: str) -> LogRecord | None:
    """Parse an ISO-8601 line or a syslog-style line."""
    # Match the syslog shape first so the logger name stays out of the message.
    match = _SYSLOG_RE.fullmatch(stripped)
    if match is None:
        match = _ISO_RE.fullmatch(stripped)
    if match is None:
        return None
    timestamp = _parse_timestamp(match.group("ts"))
    if timestamp is None:
        # The shape looked right, but the clock values are impossible
        # (for example month 13). Treat that as unmatched.
        return None
    message = match.group("message") or ""
    return _make_record(timestamp, match.group("level"), message, raw)


def _parse_json(text: str, raw: str) -> LogRecord | None:
    """Parse one JSON Lines object, or return None when it does not match."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    raw_timestamp = _first_present(payload, _TIMESTAMP_KEYS)
    raw_level = _first_present(payload, _LEVEL_KEYS)
    if raw_timestamp is None or raw_level is None:
        return None
    if not isinstance(raw_level, str) or raw_level.strip() == "":
        return None
    timestamp = _parse_timestamp(raw_timestamp)
    if timestamp is None:
        return None

    message = ""
    for key in _MESSAGE_KEYS:
        value = payload.get(key)
        if isinstance(value, str):
            message = value
            break
    return _make_record(timestamp, raw_level, message, raw)


def _first_present(payload: dict[str, object], keys: tuple[str, ...]) -> object | None:
    """Return the first key's value, or None when none of the keys exist.

    A present value of JSON ``null`` counts as missing, so the other key
    can still supply the field.
    """
    for key in keys:
        if key not in payload:
            continue
        value = payload[key]
        if value is None:
            continue
        return value
    return None


def _parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO-8601 string, a logging datetime, or a Unix epoch number.

    Returns None when ``value`` is not one of those. Naive strings stay
    naive. ``Z`` and numeric offsets become aware datetimes. Epoch numbers
    are UTC.
    """
    if isinstance(value, str):
        text = _COMMA_FRACTION.sub(r"\1.\2", value.strip(), count=1)
        if text == "":
            return None
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    # bool is a subclass of int. JSON true/false must not become a timestamp.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    if abs(seconds) >= _EPOCH_MILLIS_THRESHOLD:
        seconds /= 1000.0
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _make_record(
    timestamp: datetime,
    level: str,
    message: str,
    raw: str,
) -> LogRecord:
    normalized = level.strip().upper()
    return LogRecord(
        timestamp=timestamp,
        level=normalized,
        message=message,
        raw=raw,
        kind=_kind_for(normalized),
    )


def _kind_for(level: str) -> Kind:
    if level in _ERROR_LEVELS:
        return "error"
    if level in _WARNING_LEVELS:
        return "warning"
    return "context"
