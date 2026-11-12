"""Tests for parsing text logs and JSON Lines into records."""

from __future__ import annotations

import ast
import io
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

import logwatcher
from logwatcher.parser import LogRecord, ParseError, ParseResult, parse_log

FIXTURES = Path(__file__).parent / "fixtures"


def test_iso8601_fixture() -> None:
    result = parse_log(FIXTURES / "iso8601.log")
    assert result.skipped == 0
    observed = [
        (record.level, record.kind, record.message) for record in result.records
    ]
    assert observed == [
        ("ERROR", "error", "connection refused"),
        ("WARN", "warning", "cache warming"),
        ("WARNING", "warning", "retry scheduled"),
        ("CRITICAL", "error", "out of memory"),
        ("FATAL", "error", "cannot bind port"),
        ("INFO", "context", "ready"),
        ("DEBUG", "context", "handshake complete"),
        ("NOTICE", "context", "quiet line"),
    ]
    assert result.records[0].timestamp == datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
    assert result.records[1].timestamp == datetime(
        2024, 3, 1, 12, 0, 1, 123000, tzinfo=UTC
    )
    assert result.records[2].timestamp == datetime(2024, 3, 1, 12, 0, 2, tzinfo=UTC)
    negative_five = timezone(timedelta(hours=-5))
    assert result.records[3].timestamp == datetime(
        2024, 3, 1, 13, 0, 3, 500000, tzinfo=negative_five
    )
    assert result.records[4].timestamp == datetime(2024, 3, 1, 18, 0, 4, tzinfo=UTC)
    assert result.records[7].timestamp == datetime(2024, 3, 1, 18, 0, 7, tzinfo=UTC)
    assert result.records[0].raw == "2024-03-01T12:00:00Z ERROR connection refused"
    assert result.records[7].raw == "2024-03-01 18:00:07Z notice quiet line"


def test_syslog_fixture() -> None:
    result = parse_log(FIXTURES / "syslog.log")
    assert result.skipped == 0
    observed = [
        (record.level, record.kind, record.message) for record in result.records
    ]
    assert observed == [
        ("ERROR", "error", "job failed"),
        ("WARNING", "warning", "slow response"),
        ("INFO", "context", "request finished"),
        ("DEBUG", "context", "extra: detail"),
    ]
    # The documented syslog shape has no timezone, so the datetime is naive.
    assert result.records[0].timestamp == datetime(2024, 3, 1, 12, 0, 0, 123000)
    assert result.records[0].timestamp.tzinfo is None
    assert result.records[1].timestamp == datetime(2024, 3, 1, 12, 0, 1)
    assert result.records[3].timestamp == datetime(2024, 3, 1, 12, 0, 3, 250000)
    assert "app.worker" not in result.records[0].message
    assert result.records[0].raw.startswith("2024-03-01 12:00:00,123 ERROR")


def test_json_lines_fixture() -> None:
    result = parse_log(FIXTURES / "jsonl.log")
    assert result.skipped == 0
    observed = [
        (record.level, record.kind, record.message) for record in result.records
    ]
    assert observed == [
        ("ERROR", "error", "disk full"),
        ("WARNING", "warning", "retrying"),
        ("INFO", "context", "started"),
        ("CRITICAL", "error", "aborted"),
    ]
    assert result.records[0].timestamp == datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
    assert result.records[1].timestamp == datetime(2024, 3, 1, 12, 0, 1, tzinfo=UTC)
    assert result.records[2].timestamp == datetime(2024, 3, 1, 12, 0, 2, tzinfo=UTC)
    # A logging-style comma timestamp is accepted inside a JSON string too.
    assert result.records[3].timestamp == datetime(2024, 3, 1, 12, 0, 3, 123000)
    assert result.records[3].timestamp.tzinfo is None
    assert result.records[1].raw.startswith('{"timestamp":')


def test_bad_json_line_is_skipped() -> None:
    result = parse_log(FIXTURES / "bad_json.log")
    assert result.skipped == 1
    assert len(result.records) == 1
    assert result.records[0].message == "kept"
    assert result.records[0].kind == "error"
    assert all(not record.raw.startswith("{this") for record in result.records)


def test_mixed_fixture_counts_skips_and_keeps_matches() -> None:
    records, skipped = parse_log(FIXTURES / "mixed.log")
    assert skipped == 2
    assert [record.message for record in records] == [
        "connection refused",
        "slow response",
        "up",
    ]
    assert [record.kind for record in records] == ["error", "warning", "context"]
    assert records[1].timestamp == datetime(2024, 3, 1, 12, 5)


def test_empty_file() -> None:
    records, skipped = parse_log(FIXTURES / "empty.log")
    assert records == []
    assert skipped == 0


def test_empty_iterable() -> None:
    assert parse_log([]) == ParseResult([], 0)


def test_blank_lines_are_ignored() -> None:
    assert parse_log(["", "   ", "\t", "\n"]) == ParseResult([], 0)


def test_newline_only_file_is_empty_success(tmp_path: Path) -> None:
    path = tmp_path / "newlines.log"
    path.write_text("\n\n\n", encoding="utf-8")
    assert parse_log(path) == ParseResult([], 0)


def test_blank_lines_between_records_are_ignored() -> None:
    lines = [
        "2024-03-01T12:00:00Z ERROR first",
        "",
        "   ",
        "2024-03-01T12:00:01Z INFO second",
    ]
    result = parse_log(lines)
    assert result.skipped == 0
    assert [record.message for record in result.records] == ["first", "second"]


def test_no_matches_raises_a_clear_error() -> None:
    with pytest.raises(ParseError) as caught:
        parse_log(FIXTURES / "no_match.log")
    message = str(caught.value)
    assert caught.value.skipped == 3
    assert "No log lines matched" in message
    assert "ISO-8601" in message
    assert "JSON" in message
    assert "YYYY-MM-DD" in message


def test_file_of_only_bad_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "broken.log"
    path.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(ParseError) as caught:
        parse_log(path)
    assert caught.value.skipped == 1


def test_unmatched_line_is_skipped_not_recorded_as_an_error() -> None:
    lines = [
        "2024-03-01T12:00:00Z ERROR real failure",
        "this is not a log line",
    ]
    result = parse_log(lines)
    assert result.skipped == 1
    assert len(result.records) == 1
    assert result.records[0].message == "real failure"
    assert result.records[0].kind == "error"


def test_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "no-such.log"
    with pytest.raises(FileNotFoundError, match="Log file not found"):
        parse_log(missing)


def test_missing_string_path(tmp_path: Path) -> None:
    missing = tmp_path / "also-missing.log"
    with pytest.raises(FileNotFoundError, match="Log file not found"):
        parse_log(str(missing))


def test_string_path_and_path_object_match() -> None:
    path = FIXTURES / "syslog.log"
    assert parse_log(path) == parse_log(str(path))


def test_iterable_of_lines_matches_the_same_file() -> None:
    path = FIXTURES / "iso8601.log"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert parse_log(lines) == parse_log(path)


def test_open_file_iterable(tmp_path: Path) -> None:
    path = tmp_path / "stream.log"
    path.write_text(
        "2024-03-01T12:00:00Z ERROR from a file object\n",
        encoding="utf-8",
    )
    with path.open(encoding="utf-8") as handle:
        result = parse_log(handle)
    assert result.skipped == 0
    assert result.records[0].message == "from a file object"
    assert result.records[0].raw == "2024-03-01T12:00:00Z ERROR from a file object"


def test_stringio_iterable() -> None:
    stream = io.StringIO("2024-03-01T12:00:00Z WARNING from memory\n")
    record = parse_log(stream).records[0]
    assert record.level == "WARNING"
    assert record.kind == "warning"
    assert record.message == "from memory"


@pytest.mark.parametrize(
    ("level", "kind"),
    [
        ("ERROR", "error"),
        ("FATAL", "error"),
        ("CRITICAL", "error"),
        ("WARN", "warning"),
        ("WARNING", "warning"),
        ("INFO", "context"),
        ("DEBUG", "context"),
        ("TRACE", "context"),
        ("NOTICE", "context"),
        ("VERBOSE", "context"),
    ],
)
def test_level_kinds(level: str, kind: str) -> None:
    line = f"2024-03-01T12:00:00Z {level} hello"
    result = parse_log([line])
    assert result.skipped == 0
    record = result.records[0]
    assert record.level == level
    assert record.kind == kind
    assert record.message == "hello"


@pytest.mark.parametrize(
    ("written", "level", "kind"),
    [
        ("error", "ERROR", "error"),
        ("Fatal", "FATAL", "error"),
        ("critical", "CRITICAL", "error"),
        ("warn", "WARN", "warning"),
        ("Warning", "WARNING", "warning"),
        ("info", "INFO", "context"),
    ],
)
def test_level_matching_is_case_insensitive(
    written: str,
    level: str,
    kind: str,
) -> None:
    line = f"2024-03-01T12:00:00Z {written} hello"
    record = parse_log([line]).records[0]
    assert record.level == level
    assert record.kind == kind


def test_iso_line_without_a_message() -> None:
    record = parse_log(["2024-03-01T12:00:00Z ERROR"]).records[0]
    assert record.message == ""
    assert record.kind == "error"


def test_syslog_line_without_a_message() -> None:
    record = parse_log(["2024-03-01 12:00:00,123 ERROR app.worker:"]).records[0]
    assert record.message == ""
    assert record.level == "ERROR"


def test_leading_whitespace_does_not_prevent_a_match() -> None:
    raw = "  2024-03-01T12:00:00Z ERROR padded"
    record = parse_log([raw]).records[0]
    assert record.message == "padded"
    assert record.raw == raw


def test_impossible_timestamp_is_skipped() -> None:
    lines = [
        "2024-13-01T12:00:00Z ERROR not a real month",
        "2024-03-01T12:00:00Z ERROR real",
    ]
    result = parse_log(lines)
    assert result.skipped == 1
    assert [record.message for record in result.records] == ["real"]


def test_json_missing_timestamp_or_level_is_skipped() -> None:
    lines = [
        '{"level": "ERROR", "message": "no time"}',
        '{"ts": "2024-03-01T12:00:00Z", "message": "no level"}',
        '{"ts": "2024-03-01T12:00:00Z", "level": "ERROR", "message": "complete"}',
    ]
    result = parse_log(lines)
    assert result.skipped == 2
    assert [record.message for record in result.records] == ["complete"]


def test_json_prefers_ts_level_and_message_keys() -> None:
    line = (
        '{"ts": "2024-03-01T12:00:00Z", '
        '"timestamp": "2024-03-01T00:00:00Z", '
        '"level": "ERROR", "severity": "INFO", '
        '"message": "from message", "msg": "from msg"}'
    )
    record = parse_log([line]).records[0]
    assert record.timestamp == datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
    assert record.level == "ERROR"
    assert record.kind == "error"
    assert record.message == "from message"


def test_json_null_timestamp_falls_through_to_timestamp_key() -> None:
    line = (
        '{"ts": null, "timestamp": "2024-03-01T12:00:00Z", '
        '"level": "INFO", "message": "used the other key"}'
    )
    record = parse_log([line]).records[0]
    assert record.timestamp == datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
    assert record.message == "used the other key"


def test_json_unparsed_timestamp_is_skipped() -> None:
    lines = [
        '{"ts": "yesterday", "level": "ERROR", "message": "bad clock"}',
        '{"ts": "2024-03-01T12:00:00Z", "level": "ERROR", "message": "good clock"}',
    ]
    result = parse_log(lines)
    assert result.skipped == 1
    assert result.records[0].message == "good clock"


def test_json_non_object_is_skipped() -> None:
    lines = [
        '["not", "an", "object"]',
        '"just a string"',
        "42",
        '{"ts": "2024-03-01T12:00:00Z", "level": "INFO", "message": "ok"}',
    ]
    result = parse_log(lines)
    assert result.skipped == 3
    assert result.records[0].message == "ok"


def test_json_level_is_stripped_and_uppercased() -> None:
    line = '{"ts": "2024-03-01T12:00:00Z", "level": " error ", "message": "x"}'
    record = parse_log([line]).records[0]
    assert record.level == "ERROR"
    assert record.kind == "error"


def test_json_message_keeps_internal_spacing() -> None:
    line = '{"ts": "2024-03-01T12:00:00Z", "level": "INFO", "message": "a  b"}'
    assert parse_log([line]).records[0].message == "a  b"


def test_json_unix_epoch_seconds_and_millis() -> None:
    seconds = '{"ts": 1710000000, "level": "ERROR", "message": "seconds"}'
    millis = '{"ts": 1710000000123, "level": "WARN", "message": "millis"}'
    result = parse_log([seconds, millis])
    assert result.skipped == 0
    assert result.records[0].timestamp == datetime(2024, 3, 9, 16, 0, tzinfo=UTC)
    assert result.records[0].kind == "error"
    assert result.records[1].timestamp == datetime(
        2024, 3, 9, 16, 0, 0, 123000, tzinfo=UTC
    )
    assert result.records[1].level == "WARN"
    assert result.records[1].kind == "warning"


def test_json_fractional_epoch_seconds() -> None:
    line = '{"timestamp": 1710000000.5, "severity": "info", "msg": "half"}'
    record = parse_log([line]).records[0]
    assert record.timestamp == datetime(2024, 3, 9, 16, 0, 0, 500000, tzinfo=UTC)
    assert record.level == "INFO"
    assert record.message == "half"
    assert record.kind == "context"


def test_json_bool_timestamp_is_skipped() -> None:
    lines = [
        '{"ts": true, "level": "ERROR", "message": "not a clock"}',
        '{"ts": "2024-03-01T12:00:00Z", "level": "INFO", "message": "real"}',
    ]
    result = parse_log(lines)
    assert result.skipped == 1
    assert result.records[0].message == "real"


def test_utf8_message(tmp_path: Path) -> None:
    path = tmp_path / "utf8.log"
    path.write_text("2024-03-01T12:00:00Z ERROR café down\n", encoding="utf-8")
    assert parse_log(path).records[0].message == "café down"


def test_crlf_file(tmp_path: Path) -> None:
    path = tmp_path / "crlf.log"
    path.write_bytes(b"2024-03-01T12:00:00Z ERROR crlf line\r\n\r\n")
    result = parse_log(path)
    assert result.skipped == 0
    assert result.records[0].message == "crlf line"
    assert result.records[0].raw == "2024-03-01T12:00:00Z ERROR crlf line"


def test_records_are_frozen() -> None:
    record = parse_log(["2024-03-01T12:00:00Z INFO steady"]).records[0]
    assert isinstance(record, LogRecord)
    with pytest.raises(AttributeError):
        setattr(record, "message", "changed")


def test_parse_log_is_exported_from_the_package() -> None:
    assert logwatcher.parse_log is parse_log
    assert logwatcher.ParseError is ParseError
    assert logwatcher.LogRecord is LogRecord


def test_package_imports_stdlib_only() -> None:
    """The runtime package stays on the standard library, with no network code."""
    root = Path(__file__).resolve().parents[1] / "src" / "logwatcher"
    imported: set[str] = set()
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                if node.module:
                    imported.add(node.module.split(".", 1)[0])
    extra = imported - sys.stdlib_module_names - {"logwatcher"}
    assert extra == set()
