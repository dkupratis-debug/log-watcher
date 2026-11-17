"""Build and print a scan report for one or more saved log files.

A report counts lines, records, errors, and skipped lines, then lists
spikes and repeated error types from ``detect_spikes``. ``new_patterns``
is always empty: pattern detection is a separate step and is not done
here. Text and JSON use the same fields.

Only the Python standard library is used.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from logwatcher.parser import parse_log
from logwatcher.spikes import RepeatedType, Spike, detect_spikes

# Printed in this order so a text report and its JSON object line up.
REPORT_FIELDS = (
    "file",
    "lines",
    "records",
    "errors",
    "skipped",
    "spikes",
    "new_patterns",
    "repeated",
)


@dataclass(frozen=True)
class FileReport:
    """The scan summary for a single log file.

    ``lines`` counts every line ``splitlines`` returns, including blanks.
    ``records`` counts lines that parsed. ``errors`` counts records whose
    kind is ``error``. ``skipped`` is the parser's skip count. ``spikes``
    and ``repeated`` come from ``detect_spikes`` with its defaults.
    ``new_patterns`` is reserved for pattern detection and stays empty.
    """

    path: str
    lines: int
    records: int
    errors: int
    skipped: int
    spikes: tuple[Spike, ...]
    repeated: tuple[RepeatedType, ...]
    new_patterns: tuple[str, ...] = ()


def scan_file(path: str | Path) -> FileReport:
    """Parse ``path`` and detect spikes.

    ``path`` is stored on the report as it was given, so a relative path
    stays relative in the output.

    Raises:
        FileNotFoundError: No file is at ``path``.
        PermissionError: The file cannot be read.
        IsADirectoryError: ``path`` is a directory.
        OSError: The file cannot be read for another reason, including
            text that is not valid UTF-8.
        ParseError: The file has lines and none of them match.
    """
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Log file not found: {source}") from exc
    except PermissionError as exc:
        raise PermissionError(f"Cannot read log file: {source}") from exc
    except IsADirectoryError as exc:
        raise IsADirectoryError(f"Not a log file: {source}") from exc
    except UnicodeDecodeError as exc:
        raise OSError(f"{source} is not valid UTF-8") from exc
    lines = text.splitlines()
    parsed = parse_log(lines)
    found = detect_spikes(parsed.records)
    errors = sum(1 for record in parsed.records if record.kind == "error")
    return FileReport(
        path=str(path),
        lines=len(lines),
        records=len(parsed.records),
        errors=errors,
        skipped=parsed.skipped,
        spikes=found.spikes,
        repeated=found.repeated,
    )


def scan_files(paths: Iterable[str | Path]) -> list[FileReport]:
    """Scan each path in order.

    The first file that cannot be read or parsed raises, and later paths
    are not opened. Nothing is printed.
    """
    return [scan_file(path) for path in paths]


def format_reports(reports: Sequence[FileReport], output_format: str) -> str:
    """Return the text or JSON summary, including a trailing newline.

    One file's JSON object has the report fields at the top level. More
    than one file is a JSON object with a ``files`` array of those same
    objects. Text prints one block per file, with a blank line between
    blocks.

    Raises:
        ValueError: ``output_format`` is not ``text`` or ``json``.
    """
    if output_format == "text":
        blocks = [_format_text(report) for report in reports]
        return "\n\n".join(blocks) + "\n"
    if output_format == "json":
        if len(reports) == 1:
            payload: object = _as_dict(reports[0])
        else:
            payload = {"files": [_as_dict(report) for report in reports]}
        return json.dumps(payload, indent=2) + "\n"
    raise ValueError("output format must be 'text' or 'json'")


def _format_text(report: FileReport) -> str:
    """One file's text block, without a trailing newline."""
    lines = [
        f"file: {report.path}",
        f"lines: {report.lines}",
        f"records: {report.records}",
        f"errors: {report.errors}",
        f"skipped: {report.skipped}",
        f"spikes: {len(report.spikes)}",
    ]
    for index, spike in enumerate(report.spikes, start=1):
        lines.append(f"  [{index}] {spike.error_type}")
        lines.append(f"      count: {spike.count}")
        lines.append(f"      baseline_median: {spike.baseline_median}")
        lines.append(
            "      window: "
            f"{spike.window_start.isoformat()} .. "
            f"{spike.window_end.isoformat()}"
        )
    lines.append(f"new_patterns: {len(report.new_patterns)}")
    for index, pattern in enumerate(report.new_patterns, start=1):
        lines.append(f"  [{index}] {pattern}")
    lines.append(f"repeated: {len(report.repeated)}")
    for index, item in enumerate(report.repeated, start=1):
        counts = ", ".join(str(count) for count in item.counts)
        lines.append(f"  [{index}] {item.error_type}")
        lines.append(f"      median: {item.median}")
        lines.append(f"      counts: {counts}")
        lines.append("      windows:")
        for start in item.window_starts:
            lines.append(f"        {start.isoformat()}")
    return "\n".join(lines)


def _as_dict(report: FileReport) -> dict[str, object]:
    """The JSON object for one file. Keys follow ``REPORT_FIELDS``."""
    return {
        "file": report.path,
        "lines": report.lines,
        "records": report.records,
        "errors": report.errors,
        "skipped": report.skipped,
        "spikes": [
            {
                "error_type": spike.error_type,
                "count": spike.count,
                "baseline_median": spike.baseline_median,
                "window_start": spike.window_start.isoformat(),
                "window_end": spike.window_end.isoformat(),
            }
            for spike in report.spikes
        ],
        "new_patterns": list(report.new_patterns),
        "repeated": [
            {
                "error_type": item.error_type,
                "median": item.median,
                "counts": list(item.counts),
                "windows": [start.isoformat() for start in item.window_starts],
            }
            for item in report.repeated
        ],
    }


__all__ = [
    "REPORT_FIELDS",
    "FileReport",
    "format_reports",
    "scan_file",
    "scan_files",
]
