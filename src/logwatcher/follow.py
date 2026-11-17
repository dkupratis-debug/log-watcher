"""Follow a log as it grows and report spikes when windows close.

The default reader polls the file by name. A renamed or replaced file is
picked up from the new contents, and a file that shrinks is read again
from the start. Partial lines wait for a newline. ``time.sleep`` is the
default pause between polls. Tests pass their own ``clock``, ``sleep``,
and ``reader`` so they do not wait on wall time.

A window is closed when ``clock`` is at or past that window's end. Spike
detection then runs on the closed windows only, so a burst still inside
the current window is not reported yet. Error lines are reported as soon
as they are complete. Records are kept for the life of the process,
because a later window is compared with the earlier ones.

Only the Python standard library is used.
"""

from __future__ import annotations

import codecs
import json
import math
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, Protocol

from logwatcher.parser import LogRecord, ParseError, parse_log
from logwatcher.spikes import (
    DEFAULT_WINDOW,
    RepeatedType,
    Spike,
    detect_spikes,
    window_for,
)

# Enough of the file's prefix to notice a rewrite that never shrank below
# the offset we had already read (copytruncate that grew again).
_HEAD_BYTES = 256


class LineReader(Protocol):
    """Something that yields completed lines as they become available."""

    def poll(self) -> list[str]:
        """Return lines finished since the last poll. Empty when none."""

    def close(self) -> None:
        """Release whatever the reader is holding."""


class FileTail:
    """Read new lines from a path, following truncation and replacement.

    The path is followed by name. When that path points at a new file, or
    the bytes at the start of the file change, or the file shrinks below
    the offset already read, the next poll returns the new contents from
    the beginning. A line without a newline yet is held until it finishes
    or the file is replaced.

    The first time an existing file is opened, the read starts at the end
    unless ``from_start`` is set. A file that appears later is read from
    the beginning, because none of it was skipped on purpose.
    """

    def __init__(self, path: str | Path, *, from_start: bool = False) -> None:
        self.path = Path(path)
        # Seek to the end only for a file that is already there. If it is
        # missing, or it disappears before the first read, the bytes that
        # show up later are new and should not be skipped.
        self._start_at_end = not from_start and self.path.is_file()
        self._file: BinaryIO | None = None
        self._inode: int | None = None
        self._device: int | None = None
        self._partial = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._head = b""
        self._has_head = False
        self._saw_file = False
        self._reset = False

    def poll(self) -> list[str]:
        """Return newly completed lines, possibly none."""
        self._sync()
        if self._file is None:
            return []
        chunk = self._file.read()
        if not chunk:
            return []
        text = self._decoder.decode(chunk)
        return self._completed_lines(text)

    def close(self) -> None:
        """Close the file if it is open."""
        self._close_file()

    def take_reset(self) -> bool:
        """Return whether the file was replaced or truncated since last time.

        The flag is cleared. A reset means the lines from this poll belong
        to a new byte stream, so earlier records are not part of that file
        anymore.
        """
        happened = self._reset
        self._reset = False
        return happened

    def _sync(self) -> None:
        """Reopen or rewind when the path no longer matches what we read."""
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            # The name is gone. The next file to appear at this path is a
            # new file, including the case where we had not opened it yet.
            self._start_at_end = False
            self._close_file()
            return
        except PermissionError as exc:
            if not self._saw_file:
                raise PermissionError(f"Cannot read log file: {self.path}") from exc
            self._close_file()
            return

        replaced = (
            self._file is None
            or stat.st_ino != self._inode
            or stat.st_dev != self._device
        )
        if replaced:
            self._open(at_end=self._start_at_end)
            return
        assert self._file is not None
        if stat.st_size < self._file.tell():
            self._rewind()
            return
        if self._prefix_changed():
            self._rewind()

    def _open(self, *, at_end: bool) -> None:
        """Open ``path`` and remember its identity. Missing is not an error."""
        # A second open is a new file at the same path. The first open is
        # just the start of the stream.
        replaced = self._saw_file
        self._close_file()
        try:
            opened = self.path.open("rb")
        except FileNotFoundError:
            self._start_at_end = False
            return
        except PermissionError as exc:
            if not self._saw_file:
                raise PermissionError(f"Cannot read log file: {self.path}") from exc
            return
        try:
            stat = os.fstat(opened.fileno())
            if at_end:
                opened.seek(0, os.SEEK_END)
            self._remember_prefix(opened)
        except OSError:
            opened.close()
            raise
        self._file = opened
        self._inode = stat.st_ino
        self._device = stat.st_dev
        self._partial = ""
        self._decoder.reset()
        self._saw_file = True
        self._start_at_end = False
        if replaced:
            self._reset = True

    def _rewind(self) -> None:
        """Read the same file again from the start."""
        assert self._file is not None
        self._file.seek(0)
        self._partial = ""
        self._decoder.reset()
        self._remember_prefix(self._file)
        self._reset = True

    def _remember_prefix(self, opened: BinaryIO) -> None:
        """Store the leading bytes, then seek back to where reading was."""
        position = opened.tell()
        opened.seek(0)
        self._head = opened.read(_HEAD_BYTES)
        opened.seek(position)
        self._has_head = True

    def _prefix_changed(self) -> bool:
        """True when the leading bytes are not the ones we started reading.

        Appending leaves the prefix in place. A rewrite that is already
        larger than our offset still changes the prefix, which is how a
        truncation we did not poll in time is noticed.
        """
        assert self._file is not None
        if not self._has_head:
            return False
        position = self._file.tell()
        self._file.seek(0)
        head = self._file.read(len(self._head))
        self._file.seek(position)
        return head != self._head

    def _completed_lines(self, text: str) -> list[str]:
        """Split ``text`` on newlines and keep an unfinished tail."""
        if text == "":
            return []
        parts = (self._partial + text).split("\n")
        self._partial = parts.pop()
        return [part[:-1] if part.endswith("\r") else part for part in parts]

    def _close_file(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        self._inode = None
        self._device = None
        self._partial = ""
        self._decoder.reset()
        self._head = b""
        self._has_head = False


class _State:
    """Records seen so far, and which findings were already printed."""

    def __init__(self) -> None:
        self.records: list[LogRecord] = []
        self.spikes = 0
        self._spike_keys: set[tuple[str, datetime]] = set()
        self._repeated: set[str] = set()


class _TextEvents:
    """One human-readable line per follow event."""

    def error(self, record: LogRecord) -> str:
        stamp = record.timestamp.isoformat()
        if record.message:
            return f"error: {stamp} {record.level} {record.message}"
        return f"error: {stamp} {record.level}"

    def spike(self, spike: Spike) -> str:
        return (
            f"spike: {spike.error_type} "
            f"count={spike.count} "
            f"baseline_median={spike.baseline_median} "
            f"window={spike.window_start.isoformat()} .. "
            f"{spike.window_end.isoformat()}"
        )

    def repeated(self, item: RepeatedType) -> str:
        counts = ", ".join(str(count) for count in item.counts)
        return f"repeated: {item.error_type} median={item.median} counts={counts}"


class _JsonEvents:
    """One JSON object per follow event, with no extra whitespace."""

    def error(self, record: LogRecord) -> str:
        return json.dumps(
            {
                "event": "error",
                "timestamp": record.timestamp.isoformat(),
                "level": record.level,
                "message": record.message,
            }
        )

    def spike(self, spike: Spike) -> str:
        return json.dumps(
            {
                "event": "spike",
                "error_type": spike.error_type,
                "count": spike.count,
                "baseline_median": spike.baseline_median,
                "window_start": spike.window_start.isoformat(),
                "window_end": spike.window_end.isoformat(),
            }
        )

    def repeated(self, item: RepeatedType) -> str:
        return json.dumps(
            {
                "event": "repeated",
                "error_type": item.error_type,
                "median": item.median,
                "counts": list(item.counts),
                "windows": [start.isoformat() for start in item.window_starts],
            }
        )


def follow_path(
    path: str | Path,
    *,
    from_start: bool = False,
    emit: Callable[[str], None] = print,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    interval: float = 0.5,
    max_polls: int | None = None,
    reader: LineReader | None = None,
    output_format: str = "text",
    window: timedelta = DEFAULT_WINDOW,
) -> int:
    """Watch ``path`` and emit errors, spikes, and repeated types.

    Returns how many spikes were emitted. ``KeyboardInterrupt`` is not
    caught: Ctrl-C and ``SIGTERM`` leave the caller, which exits 0.

    ``clock`` is the current time. A window is reported only once ``clock``
    has reached its end. ``sleep`` is called with ``interval`` between
    polls; the default is ``time.sleep``. ``max_polls`` stops the loop
    after that many reads, before sleeping, so a caller can run one pass.
    ``reader`` replaces the file tail. ``output_format`` is ``text`` or
    ``json`` (one JSON object per line).

    Raises:
        ValueError: ``interval`` is not greater than 0, ``window`` is not
            a positive ``timedelta``, or ``output_format`` is unknown.
        OSError: The file cannot be opened on the first poll.
    """
    if (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not math.isfinite(interval)
        or interval <= 0
    ):
        raise ValueError("interval must be greater than 0")
    if output_format == "text":
        events: _TextEvents | _JsonEvents = _TextEvents()
    elif output_format == "json":
        events = _JsonEvents()
    else:
        raise ValueError("output format must be 'text' or 'json'")

    if clock is None:
        clock = _now
    if sleep is None:
        sleep = time.sleep
    own_reader = reader is None
    if reader is None:
        reader = FileTail(path, from_start=from_start)

    state = _State()
    polls = 0
    try:
        while True:
            lines = reader.poll()
            # A replaced or truncated file is a new byte stream. Drop the
            # records from the bytes that are gone so they are not a baseline
            # for the new contents. Lines from this poll are the new stream.
            if isinstance(reader, FileTail) and reader.take_reset():
                state = _State()
            for line in lines:
                _accept(line, state, events, emit)
            _emit_closed(state, clock(), window, events, emit)
            polls += 1
            if max_polls is not None and polls >= max_polls:
                return state.spikes
            sleep(interval)
    finally:
        if own_reader:
            reader.close()


def _accept(
    line: str,
    state: _State,
    events: _TextEvents | _JsonEvents,
    emit: Callable[[str], None],
) -> None:
    """Parse one finished line. A line that does not match is ignored.

    ``parse_log`` raises when every line in its input failed to match.
    One bad line in a stream is that case, and follow keeps going.
    """
    try:
        result = parse_log([line])
    except ParseError:
        return
    for record in result.records:
        state.records.append(record)
        if record.kind == "error":
            emit(events.error(record))


def _emit_closed(
    state: _State,
    now: datetime,
    window: timedelta,
    events: _TextEvents | _JsonEvents,
    emit: Callable[[str], None],
) -> None:
    """Emit findings whose windows have ended, once each.

    The open window is left out of the baseline. Including it would let
    a burst that has not finished yet change the report.
    """
    moment = _as_utc(now)
    closed = [
        record
        for record in state.records
        if window_for(record.timestamp, window)[1] <= moment
    ]
    report = detect_spikes(closed, window=window)
    for spike in report.spikes:
        key = (spike.error_type, spike.window_start)
        if key in state._spike_keys:
            continue
        state._spike_keys.add(key)
        state.spikes += 1
        emit(events.spike(spike))
    for item in report.repeated:
        if item.error_type in state._repeated:
            continue
        state._repeated.add(item.error_type)
        emit(events.repeated(item))


def _now() -> datetime:
    """Current time in UTC. Tests pass their own clock instead."""
    return datetime.now(UTC)


def _as_utc(moment: datetime) -> datetime:
    """Read a naive clock value as UTC. Convert an aware value to UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)
