"""Tests for the scan command: text, JSON, output path, and exit codes."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from logwatcher.__main__ import main
from logwatcher.report import REPORT_FIELDS, format_reports, scan_file

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"

QUIET = """\
2024-03-01T12:00:00Z INFO ready

2024-03-01T12:00:01Z ERROR one
not a log
"""


def test_text_summary_lists_the_documented_fields(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_text(QUIET, encoding="utf-8")
    report = scan_file(path)
    assert report.lines == 4
    assert report.records == 2
    assert report.errors == 1
    assert report.skipped == 1
    assert report.spikes == ()
    assert report.new_patterns == ()
    assert report.repeated == ()
    text = format_reports([report], "text")
    assert text == (
        f"file: {path}\n"
        "lines: 4\n"
        "records: 2\n"
        "errors: 1\n"
        "skipped: 1\n"
        "spikes: 0\n"
        "new_patterns: 0\n"
        "repeated: 0\n"
    )
    payload = json.loads(format_reports([report], "json"))
    assert list(payload) == list(REPORT_FIELDS)
    assert payload["file"] == str(path)
    assert payload["lines"] == 4
    assert payload["records"] == 2
    assert payload["errors"] == 1
    assert payload["skipped"] == 1
    assert payload["spikes"] == []
    assert payload["new_patterns"] == []
    assert payload["repeated"] == []
    for field in REPORT_FIELDS:
        assert f"{field}:" in text


def test_scan_happy_path_stdout_stderr_and_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text(QUIET, encoding="utf-8")
    assert main([str(path)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == format_reports([scan_file(path)], "text")


def test_scan_subcommand_matches_a_bare_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text(QUIET, encoding="utf-8")
    assert main(["scan", str(path)]) == 0
    direct = capsys.readouterr().out
    assert main([str(path)]) == 0
    assert capsys.readouterr().out == direct


def test_format_json_is_the_same_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text(QUIET, encoding="utf-8")
    assert main(["--format", "json", str(path)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload == json.loads(format_reports([scan_file(path)], "json"))


def test_format_flag_may_precede_the_scan_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text(QUIET, encoding="utf-8")
    assert main(["--format", "json", "scan", str(path)]) == 0
    first = capsys.readouterr().out
    assert main(["scan", "--format", "json", str(path)]) == 0
    assert capsys.readouterr().out == first


def test_output_flag_writes_the_report_and_leaves_stdout_empty(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text(QUIET, encoding="utf-8")
    destination = tmp_path / "out" / "report.txt"
    destination.parent.mkdir()
    assert main([str(path), "-o", str(destination)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert destination.read_text(encoding="utf-8") == format_reports(
        [scan_file(path)], "text"
    )


def test_output_long_option_with_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text(QUIET, encoding="utf-8")
    destination = tmp_path / "report.json"
    assert main(["--format", "json", "--output", str(destination), str(path)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert json.loads(destination.read_text(encoding="utf-8"))["errors"] == 1


def test_spikes_exit_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = FIXTURES / "spike.log"
    assert main([str(path)]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "connection refused" in captured.out
    assert "spikes: 1" in captured.out
    payload = json.loads(format_reports([scan_file(path)], "json"))
    assert payload["spikes"][0]["count"] == 6
    assert payload["spikes"][0]["baseline_median"] == 1.0
    assert "window_start" in payload["spikes"][0]
    assert "window_end" in payload["spikes"][0]


def test_repeated_without_a_spike_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = FIXTURES / "steady.log"
    assert main([str(path)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "spikes: 0" in captured.out
    assert "disk full" in captured.out
    assert "repeated: 1" in captured.out


def test_empty_file_exits_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "empty.log"
    path.write_text("", encoding="utf-8")
    assert main([str(path)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == (
        f"file: {path}\n"
        "lines: 0\n"
        "records: 0\n"
        "errors: 0\n"
        "skipped: 0\n"
        "spikes: 0\n"
        "new_patterns: 0\n"
        "repeated: 0\n"
    )


def test_missing_file_exits_2_without_a_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "missing.log"
    assert main([str(missing)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("logwatcher: ")
    assert "missing.log" in captured.err
    assert "Traceback" not in captured.err


def test_invalid_utf8_exits_2_without_a_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "bad.log"
    path.write_bytes(b"\xff\xfe not utf-8\n")
    assert main([str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("logwatcher: ")
    assert "UTF-8" in captured.err
    assert "Traceback" not in captured.err


def test_unparsed_file_exits_2_without_a_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "notes.log"
    path.write_text("hello this is not a log line\n", encoding="utf-8")
    assert main([str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("logwatcher: ")
    assert "Traceback" not in captured.err


def test_directory_exits_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("logwatcher: ")
    assert "Traceback" not in captured.err


def test_permission_error_exits_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "secret.log"
    path.write_text("2024-03-01T12:00:00Z ERROR hidden\n", encoding="utf-8")
    path.chmod(0)
    try:
        path.read_text(encoding="utf-8")
    except PermissionError:
        pass
    else:
        pytest.skip("this user can read a mode 0 file")
    assert main([str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("logwatcher: ")
    assert "Traceback" not in captured.err


def test_later_missing_file_prints_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    good = tmp_path / "good.log"
    good.write_text(QUIET, encoding="utf-8")
    missing = tmp_path / "missing.log"
    assert main([str(good), str(missing)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("logwatcher: ")


def test_two_files_json_and_exit_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    quiet = tmp_path / "quiet.log"
    quiet.write_text(QUIET, encoding="utf-8")
    spike = FIXTURES / "spike.log"
    assert main(["--format", "json", str(quiet), str(spike)]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert [item["file"] for item in payload["files"]] == [str(quiet), str(spike)]
    assert payload["files"][0]["spikes"] == []
    assert len(payload["files"][1]["spikes"]) == 1
    text = format_reports([scan_file(quiet), scan_file(spike)], "text")
    assert "\n\n" in text


def test_bad_format_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--format", "xml", "anything.log"]) == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err


def test_from_start_without_follow_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--from-start", "anything.log"]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("logwatcher: ")
    assert "follow" in captured.err


def test_scan_without_a_file_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scan"]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("logwatcher: ")
    assert captured.out == ""


def test_interval_must_be_positive(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--interval", "0", "anything.log"]) == 2
    assert main(["--interval", "-1", "anything.log"]) == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err


def test_scan_refuses_to_overwrite_its_log(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    original = "2024-03-01T12:00:00Z ERROR keep\n"
    path.write_text(original, encoding="utf-8")
    link = tmp_path / "alias.log"
    link.symlink_to(path)
    assert main([str(path), "-o", str(link)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("logwatcher: ")
    assert "Traceback" not in captured.err
    assert path.read_text(encoding="utf-8") == original


def test_unwritable_output_exits_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text(QUIET, encoding="utf-8")
    assert main([str(path), "-o", str(tmp_path / "missing" / "out.txt")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("logwatcher: ")
    assert "Traceback" not in captured.err


def test_module_scan_matches_the_library() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "logwatcher", "examples/api.log"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert completed.stderr == ""
    expected = format_reports([scan_file(ROOT / "examples" / "api.log")], "text")
    # The report stores the path it was given. The module was given the
    # relative path, and scan_file above was given an absolute one.
    expected = expected.replace(
        str(ROOT / "examples" / "api.log"),
        "examples/api.log",
    )
    assert completed.stdout == expected


def test_readme_quotes_the_commands_run_on_the_example_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The usage section copies real stdout. Re-run the commands and compare."""
    text = subprocess.run(
        ["logwatcher", "examples/api.log"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert text.returncode == 1
    assert text.stderr == ""
    module = subprocess.run(
        [sys.executable, "-m", "logwatcher", "examples/api.log"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert module.returncode == 1
    assert module.stdout == text.stdout
    scanned = subprocess.run(
        ["logwatcher", "scan", "examples/api.log"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert scanned.returncode == 1
    assert scanned.stdout == text.stdout
    as_json = subprocess.run(
        ["logwatcher", "--format", "json", "examples/api.log"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert as_json.returncode == 1
    assert as_json.stderr == ""

    # The example log is in the past, so one poll closes every window.
    # This is the same text `logwatcher follow --from-start` prints
    # before it waits.
    status = main(
        ["follow", "--from-start", "examples/api.log"],
        max_polls=1,
        clock=lambda: datetime(2026, 11, 17, tzinfo=UTC),
    )
    assert status == 1
    follow_out = capsys.readouterr().out

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "logwatcher examples/api.log" in readme
    assert "logwatcher --format json examples/api.log" in readme
    assert "logwatcher follow --from-start examples/api.log" in readme
    assert text.stdout in readme
    assert as_json.stdout in readme
    assert follow_out in readme
    assert "work in progress" not in readme.lower()


def test_console_script_scan_exits_when_a_spike_is_found() -> None:
    completed = subprocess.run(
        ["logwatcher", "--format", "json", "examples/api.log"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    assert payload["file"] == "examples/api.log"
    assert payload["spikes"][0]["error_type"] == "connection refused"
    assert payload["repeated"][0]["error_type"] == "disk full"
