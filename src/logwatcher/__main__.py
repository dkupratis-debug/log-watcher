"""Command-line entry point for logwatcher.

``logwatcher FILE`` scans saved logs. ``logwatcher follow FILE`` (or
``-f``) watches one file as it grows. ``python -m logwatcher`` runs the
same entry point. Scan exits 1 when a spike is found so a CI or cron job
can fail, and exits 2 when a file cannot be read or parsed. Follow exits
0 on Ctrl-C or SIGTERM.

Known failures print ``logwatcher: ...`` on stderr and do not print a
traceback. Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import math
import signal
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from typing import TextIO

from logwatcher.follow import LineReader, follow_path
from logwatcher.parser import ParseError
from logwatcher.report import FileReport, format_reports, scan_files

# A file argument may follow one of these options, so the option's value
# is not itself treated as the scan/follow command.
_OPTIONS_WITH_VALUE = frozenset({"-o", "--output", "--format", "--interval"})

_EPILOG = """\
commands:
  logwatcher [scan] FILE [FILE ...]
        scan files and print one report
  logwatcher follow FILE
        watch FILE as it grows (same as --follow / -f)

scan is the default when FILE is the first argument. A scan exits 0 when
no spike is found, 1 when a spike is found, and 2 when a file cannot be
read or parsed. follow exits 0 on Ctrl-C or SIGTERM.

new-pattern detection is not implemented, so new_patterns is empty.
"""


def main(
    argv: list[str] | None = None,
    *,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], datetime] | None = None,
    max_polls: int | None = None,
    reader: LineReader | None = None,
) -> int:
    """Run a scan or a follow. Return a process status code.

    ``sleep``, ``clock``, ``max_polls``, and ``reader`` exist so tests can
    drive follow without waiting on the wall clock. The command-line
    defaults are ``time.sleep``, the current UTC time, and the real file.
    """
    try:
        return _run(
            argv,
            sleep=sleep,
            clock=clock,
            max_polls=max_polls,
            reader=reader,
        )
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return 0


def _run(
    argv: list[str] | None,
    *,
    sleep: Callable[[float], None] | None,
    clock: Callable[[], datetime] | None,
    max_polls: int | None,
    reader: LineReader | None,
) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    command, rest = _split_command(raw)
    parser = _build_parser()
    try:
        options = parser.parse_args(rest)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 2

    follow = command == "follow" or options.follow
    if options.from_start and not follow:
        return _fail("--from-start is only valid with follow")
    if not math.isfinite(options.interval) or options.interval <= 0:
        return _fail("--interval must be greater than 0")
    if follow and len(options.paths) != 1:
        return _fail("follow watches one log file")
    if not options.paths:
        if command is None and not options.follow:
            parser.print_help(sys.stdout)
            return 0
        return _fail("give at least one log file")

    if follow:
        return _run_follow(
            options,
            sleep=sleep,
            clock=clock,
            max_polls=max_polls,
            reader=reader,
        )
    return _run_scan(options)


def _run_scan(options: argparse.Namespace) -> int:
    try:
        reports = scan_files(options.paths)
    except (OSError, ParseError) as exc:
        return _fail(str(exc))
    text = format_reports(reports, options.format)
    if options.output is None:
        sys.stdout.write(text)
        sys.stdout.flush()
        return _scan_status(reports)
    try:
        with open(options.output, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as exc:
        return _fail(f"Cannot write output: {options.output} ({exc.strerror})")
    return _scan_status(reports)


def _scan_status(reports: list[FileReport]) -> int:
    """Exit 1 when any file has a spike, otherwise 0."""
    if any(report.spikes for report in reports):
        return 1
    return 0


def _run_follow(
    options: argparse.Namespace,
    *,
    sleep: Callable[[float], None] | None,
    clock: Callable[[], datetime] | None,
    max_polls: int | None,
    reader: LineReader | None,
) -> int:
    output, close_output = _open_output(options.output)
    if isinstance(output, str):
        # _open_output returns an error message instead of a stream.
        return _fail(output)

    def emit(line: str) -> None:
        print(line, file=output, flush=True)

    restore = _install_stop_signals()
    try:
        try:
            count = follow_path(
                options.paths[0],
                from_start=options.from_start,
                emit=emit,
                clock=clock,
                sleep=sleep,
                interval=options.interval,
                max_polls=max_polls,
                reader=reader,
                output_format=options.format,
            )
        except OSError as exc:
            return _fail(str(exc))
    finally:
        restore()
        if close_output:
            output.close()
    return 1 if count else 0


def _open_output(path: str | None) -> tuple[TextIO, bool] | tuple[str, bool]:
    """Open ``path`` for writing, or return stdout.

    A string in the first position is an error message. The bool says
    whether the caller should close the stream.
    """
    if path is None:
        return sys.stdout, False
    try:
        handle = open(path, "w", encoding="utf-8", buffering=1)
    except OSError as exc:
        return f"Cannot write output: {path} ({exc.strerror})", False
    return handle, True


def _fail(message: str) -> int:
    """Print a known failure on stderr and return the usage/error status."""
    print(f"logwatcher: {message}", file=sys.stderr)
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="logwatcher",
        description="Read log files and report error spikes and repeated errors.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="FILE",
        help="Log file to scan. follow accepts one file.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="text (default) or json. follow writes one JSON object per line.",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help="Write the report to PATH instead of stdout.",
    )
    parser.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="Watch FILE as it grows. Report spikes when a window closes.",
    )
    parser.add_argument(
        "--from-start",
        action="store_true",
        help="With follow, read existing lines instead of starting at the end.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        metavar="SECONDS",
        help="Seconds between follow reads (default: 0.5).",
    )
    return parser


def _split_command(argv: list[str]) -> tuple[str | None, list[str]]:
    """Remove a leading ``scan`` or ``follow`` word from ``argv``.

    The word is optional, and a log path is allowed as the first argument,
    so this is not an argparse subparser. Options and their values are
    left in place. ``--`` keeps every later argument, including a file
    whose name is ``scan`` or ``follow``.
    """
    command: str | None = None
    rest: list[str] = []
    expect_value = False
    for index, arg in enumerate(argv):
        if arg == "--":
            rest.extend(argv[index:])
            break
        if expect_value:
            rest.append(arg)
            expect_value = False
            continue
        if command is None and arg in {"scan", "follow"}:
            command = arg
            continue
        rest.append(arg)
        if arg in _OPTIONS_WITH_VALUE:
            expect_value = True
    return command, rest


def _request_stop(signum: int, frame: object) -> None:
    """Turn SIGINT and SIGTERM into the same stop Ctrl-C already uses."""
    del signum, frame
    raise KeyboardInterrupt


def _install_stop_signals() -> Callable[[], None]:
    """Install the stop handler and return a function that restores it.

    Signal handlers can only be set from the main thread. Follow still
    runs from another thread; it then stops only when the injected sleep
    or the caller raises ``KeyboardInterrupt``.
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous: dict[int, signal.Handlers | Callable[[int, object], object]] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, _request_stop)
        except ValueError:
            continue

    def restore() -> None:
        for sig, old in previous.items():
            signal.signal(sig, old)

    return restore


if __name__ == "__main__":
    raise SystemExit(main())
