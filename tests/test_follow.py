"""Follow mode, driven by an injected clock, sleep, and reader.

Nothing in this file waits on wall time. ``time.sleep`` is patched to
fail, so a poll loop that forgot to use the injected sleep cannot pass.
"""

from __future__ import annotations

import json
import os
import signal
from datetime import UTC, datetime
from pathlib import Path

import pytest

from logwatcher.__main__ import _request_stop, main
from logwatcher.follow import FileTail, follow_path

FUTURE = datetime(2024, 3, 1, 13, 0, tzinfo=UTC)


class ManualClock:
    """A clock tests move by assigning ``now``."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class ScriptedReader:
    """Yield prebuilt batches of lines, one batch per poll."""

    def __init__(self, batches: list[list[str]]) -> None:
        self._batches = list(batches)
        self.closed = False

    def poll(self) -> list[str]:
        if not self._batches:
            return []
        return self._batches.pop(0)

    def close(self) -> None:
        self.closed = True


def _error(minute: int, message: str = "connection refused") -> str:
    return f"2024-03-01T12:{minute:02d}:00Z ERROR {message}"


@pytest.fixture(autouse=True)
def _forbid_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(seconds: float) -> None:
        raise AssertionError(f"time.sleep({seconds}) was called")

    monkeypatch.setattr("logwatcher.follow.time.sleep", fail)


def test_new_line_appended_after_start_is_reported(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text(_error(0, "before") + "\n", encoding="utf-8")
    step = {"n": 0}

    def sleep(_seconds: float) -> None:
        if step["n"] == 0:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(_error(5, "after") + "\n")
            step["n"] = 1
            return
        raise KeyboardInterrupt

    assert main(["follow", str(path)], sleep=sleep, clock=lambda: FUTURE) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "before" not in captured.out
    assert "after" in captured.out
    assert "error:" in captured.out


def test_scan_follow_reads_existing_lines_with_from_start(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text(_error(0, "already") + "\n", encoding="utf-8")

    def sleep(_seconds: float) -> None:
        raise KeyboardInterrupt

    status = main(
        ["scan", "--follow", "--from-start", str(path)],
        sleep=sleep,
        clock=lambda: FUTURE,
    )
    assert status == 0
    assert "already" in capsys.readouterr().out


def test_from_start_reads_existing_lines(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text(_error(0, "already") + "\n", encoding="utf-8")

    def sleep(_seconds: float) -> None:
        raise KeyboardInterrupt

    assert (
        main(
            ["follow", "--from-start", str(path)],
            sleep=sleep,
            clock=lambda: FUTURE,
        )
        == 0
    )
    assert "already" in capsys.readouterr().out


def test_without_from_start_skips_existing_lines(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text(_error(0, "already") + "\n", encoding="utf-8")

    def sleep(_seconds: float) -> None:
        raise KeyboardInterrupt

    assert main(["-f", str(path)], sleep=sleep, clock=lambda: FUTURE) == 0
    assert capsys.readouterr().out == ""


def test_truncated_file_is_followed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text(_error(0, "old line is fairly long") + "\n", encoding="utf-8")
    inode = path.stat().st_ino
    step = {"n": 0}

    def sleep(_seconds: float) -> None:
        if step["n"] == 0:
            path.write_text(_error(30, "short") + "\n", encoding="utf-8")
            assert path.stat().st_ino == inode
            step["n"] = 1
            return
        raise KeyboardInterrupt

    assert main(["-f", str(path)], sleep=sleep, clock=lambda: FUTURE) == 0
    captured = capsys.readouterr()
    assert "short" in captured.out
    assert "old line" not in captured.out
    assert "Traceback" not in captured.err


def test_replaced_file_is_followed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text(_error(0, "old") + "\n", encoding="utf-8")
    previous = path.stat().st_ino
    step = {"n": 0}

    def sleep(_seconds: float) -> None:
        if step["n"] == 0:
            replacement = tmp_path / "app.log.next"
            replacement.write_text(_error(30, "rotated") + "\n", encoding="utf-8")
            replacement.replace(path)
            assert path.stat().st_ino != previous
            step["n"] = 1
            return
        raise KeyboardInterrupt

    assert main(["follow", str(path)], sleep=sleep, clock=lambda: FUTURE) == 0
    captured = capsys.readouterr()
    assert "rotated" in captured.out
    assert "old" not in captured.out


def test_rewrite_that_only_grows_is_still_noticed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The replacement is longer than the offset we had reached, so a size
    # check alone would keep reading from the middle of the new bytes.
    path = tmp_path / "app.log"
    path.write_text(_error(0, "a" * 400) + "\n", encoding="utf-8")
    step = {"n": 0}

    def sleep(_seconds: float) -> None:
        if step["n"] == 0:
            rewritten = _error(30, "fresh") + "\n" + ("b" * 500) + "\n"
            path.write_text(rewritten, encoding="utf-8")
            step["n"] = 1
            return
        raise KeyboardInterrupt

    assert main(["-f", str(path)], sleep=sleep, clock=lambda: FUTURE) == 0
    captured = capsys.readouterr()
    assert "fresh" in captured.out
    assert "aaaa" not in captured.out


def test_partial_line_waits_for_a_newline(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text(_error(0, "hel"), encoding="utf-8")
    step = {"n": 0}

    def sleep(_seconds: float) -> None:
        if step["n"] == 0:
            with path.open("a", encoding="utf-8") as handle:
                handle.write("lo\n")
            step["n"] = 1
            return
        raise KeyboardInterrupt

    assert (
        main(
            ["follow", "--from-start", str(path)],
            sleep=sleep,
            clock=lambda: FUTURE,
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "hello" in output
    assert output.count("error:") == 1


def test_empty_log_notices_a_same_length_rewrite(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An empty file snapshots zero prefix bytes. After one line arrives,
    # a rewrite of that same length must still be read.
    path = tmp_path / "app.log"
    path.write_text("", encoding="utf-8")
    first = _error(0, "aaa")
    second = _error(5, "bbb")
    assert len(first) == len(second)
    step = {"n": 0}

    def sleep(_seconds: float) -> None:
        if step["n"] == 0:
            path.write_text(first + "\n", encoding="utf-8")
            step["n"] = 1
            return
        if step["n"] == 1:
            path.write_text(second + "\n", encoding="utf-8")
            step["n"] = 2
            return
        raise KeyboardInterrupt

    assert main(["-f", str(path)], sleep=sleep, clock=lambda: FUTURE) == 0
    output = capsys.readouterr().out
    assert output.count("aaa") == 1
    assert "bbb" in output


def test_empty_log_append_is_not_reread(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text("", encoding="utf-8")
    step = {"n": 0}

    def sleep(_seconds: float) -> None:
        if step["n"] == 0:
            path.write_text(_error(0, "aaa") + "\n", encoding="utf-8")
            step["n"] = 1
            return
        if step["n"] == 1:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(_error(5, "bbb") + "\n")
            step["n"] = 2
            return
        raise KeyboardInterrupt

    assert main(["-f", str(path)], sleep=sleep, clock=lambda: FUTURE) == 0
    output = capsys.readouterr().out
    assert output.count("aaa") == 1
    assert output.count("bbb") == 1


def test_file_created_after_start_is_read(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "later.log"

    def sleep(_seconds: float) -> None:
        if not path.exists():
            path.write_text(_error(0, "appeared") + "\n", encoding="utf-8")
            return
        raise KeyboardInterrupt

    assert main(["-f", str(path)], sleep=sleep, clock=lambda: FUTURE) == 0
    assert "appeared" in capsys.readouterr().out


def test_bad_line_does_not_stop_the_next_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text("", encoding="utf-8")
    step = {"n": 0}

    def sleep(_seconds: float) -> None:
        if step["n"] == 0:
            with path.open("a", encoding="utf-8") as handle:
                handle.write("not a log\n")
                handle.write(_error(0, "good") + "\n")
            step["n"] = 1
            return
        raise KeyboardInterrupt

    assert main(["-f", str(path)], sleep=sleep, clock=lambda: FUTURE) == 0
    captured = capsys.readouterr()
    assert "good" in captured.out
    assert "Traceback" not in captured.err
    assert "not a log" not in captured.out


def test_keyboard_interrupt_exits_zero_even_when_a_spike_was_printed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    lines = [_error(0), _error(5), *([_error(10)] * 6)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def sleep(_seconds: float) -> None:
        raise KeyboardInterrupt

    assert (
        main(
            ["follow", str(path), "--from-start"],
            sleep=sleep,
            clock=lambda: FUTURE,
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "spike: connection refused count=6 baseline_median=1.0" in output
    assert capsys.readouterr().err == ""


def test_spike_is_reported_only_after_the_window_closes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = ManualClock(datetime(2024, 3, 1, 12, 14, tzinfo=UTC))
    batches = [[_error(0), _error(5), *([_error(10)] * 6)], [], []]
    reader = ScriptedReader(batches)

    def sleep(_seconds: float) -> None:
        clock.now = datetime(2024, 3, 1, 12, 15, tzinfo=UTC)

    count = follow_path(
        "unused.log",
        reader=reader,
        clock=clock,
        sleep=sleep,
        max_polls=3,
    )
    output = capsys.readouterr().out
    # The 12:10 window ends at 12:15. The first poll is still at 12:14.
    assert output.count("error:") == 8
    assert output.count("spike:") == 1
    assert count == 1
    assert "window=2024-03-01T12:10:00+00:00 .. 2024-03-01T12:15:00+00:00" in output


def test_open_window_is_not_a_spike(capsys: pytest.CaptureFixture[str]) -> None:
    clock = ManualClock(datetime(2024, 3, 1, 12, 14, tzinfo=UTC))
    reader = ScriptedReader([[_error(0), _error(5), *([_error(10)] * 6)]])
    count = follow_path(
        "unused.log",
        reader=reader,
        clock=clock,
        sleep=lambda _seconds: None,
        max_polls=1,
    )
    assert count == 0
    output = capsys.readouterr().out
    assert "error:" in output
    assert "spike:" not in output


def test_repeated_type_is_emitted_once_when_windows_close(
    capsys: pytest.CaptureFixture[str],
) -> None:
    lines = [
        _error(minute, "disk full")
        for minute in (0, 5, 10, 15)
        for _ in range(3)
    ]
    clock = ManualClock(datetime(2024, 3, 1, 12, 20, tzinfo=UTC))
    reader = ScriptedReader([lines, []])
    count = follow_path(
        "unused.log",
        reader=reader,
        clock=clock,
        sleep=lambda _seconds: None,
        max_polls=2,
    )
    assert count == 0
    output = capsys.readouterr().out
    assert output.count("repeated:") == 1
    assert "repeated: disk full median=3.0 counts=3, 3, 3, 3" in output
    assert "spike:" not in output


def test_replacement_does_not_keep_the_old_baseline(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Two quiet windows, then the file is replaced by six events in one
    # window. Those six are not a spike: there is no other window left
    # to compare with. Keeping the deleted lines would make them one.
    path = tmp_path / "app.log"
    path.write_text(_error(0) + "\n" + _error(5) + "\n", encoding="utf-8")
    step = {"n": 0}

    def sleep(_seconds: float) -> None:
        if step["n"] == 0:
            replacement = tmp_path / "next.log"
            body = "\n".join([_error(10)] * 6) + "\n"
            replacement.write_text(body, encoding="utf-8")
            replacement.replace(path)
            step["n"] = 1
            return
        raise KeyboardInterrupt

    # 12:15 closes the 12:10 window the replacement lands in.
    def clock() -> datetime:
        return datetime(2024, 3, 1, 12, 15, tzinfo=UTC)

    assert main(["follow", "--from-start", str(path)], sleep=sleep, clock=clock) == 0
    output = capsys.readouterr().out
    assert output.count("error:") == 8
    assert "spike:" not in output


def test_json_follow_is_one_object_per_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    reader = ScriptedReader([[_error(0, "disk full")]])
    follow_path(
        "unused.log",
        reader=reader,
        clock=lambda: FUTURE,
        sleep=lambda _seconds: None,
        max_polls=1,
        output_format="json",
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "event": "error",
        "timestamp": "2024-03-01T12:00:00+00:00",
        "level": "ERROR",
        "message": "disk full",
    }


def test_warnings_are_not_follow_events(capsys: pytest.CaptureFixture[str]) -> None:
    lines = [
        "2024-03-01T12:00:00Z WARNING queue delay",
        "2024-03-01T12:00:01Z INFO ready",
    ]
    reader = ScriptedReader([lines])
    follow_path(
        "unused.log",
        reader=reader,
        clock=lambda: FUTURE,
        sleep=lambda _seconds: None,
        max_polls=1,
    )
    assert capsys.readouterr().out == ""


def test_sigterm_handler_is_installed_and_restored(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.log"
    path.write_text("", encoding="utf-8")
    seen: dict[str, object] = {}

    def sleep(_seconds: float) -> None:
        seen["term"] = signal.getsignal(signal.SIGTERM)
        seen["int"] = signal.getsignal(signal.SIGINT)
        raise KeyboardInterrupt

    before_term = signal.getsignal(signal.SIGTERM)
    before_int = signal.getsignal(signal.SIGINT)
    assert main(["-f", str(path)], sleep=sleep, clock=lambda: FUTURE) == 0
    assert seen["term"] is _request_stop
    assert seen["int"] is _request_stop
    assert signal.getsignal(signal.SIGTERM) is before_term
    assert signal.getsignal(signal.SIGINT) is before_int


def test_stop_handler_raises_keyboard_interrupt() -> None:
    with pytest.raises(KeyboardInterrupt):
        _request_stop(signal.SIGTERM, None)


def test_interval_is_passed_to_sleep(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.log"
    path.write_text("", encoding="utf-8")
    seen: list[float] = []

    def sleep(seconds: float) -> None:
        seen.append(seconds)
        raise KeyboardInterrupt

    status = main(
        ["-f", str(path), "--interval", "0.2"],
        sleep=sleep,
        clock=lambda: FUTURE,
    )
    assert status == 0
    assert seen == [0.2]


def _refuse_output(path: Path, output: Path) -> int:
    original = path.read_text(encoding="utf-8")

    def sleep(_seconds: float) -> None:
        raise AssertionError("follow started")

    status = main(
        ["-f", str(path), "-o", str(output)],
        sleep=sleep,
        clock=lambda: FUTURE,
    )
    assert path.read_text(encoding="utf-8") == original
    return status


def test_follow_refuses_to_overwrite_the_log(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text(_error(0, "keep") + "\n", encoding="utf-8")
    assert _refuse_output(path, path) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("logwatcher: ")
    assert "Traceback" not in captured.err


def test_follow_refuses_a_symlink_to_the_log(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text(_error(0, "keep") + "\n", encoding="utf-8")
    link = tmp_path / "alias.log"
    link.symlink_to(path)
    assert _refuse_output(path, link) == 2
    assert capsys.readouterr().err.startswith("logwatcher: ")


def test_follow_refuses_a_hard_link_to_the_log(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text(_error(0, "keep") + "\n", encoding="utf-8")
    hard = tmp_path / "hard.log"
    os.link(path, hard)
    assert _refuse_output(path, hard) == 2
    assert capsys.readouterr().err.startswith("logwatcher: ")


def test_follow_refuses_a_relative_alias_of_the_log(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text(_error(0, "keep") + "\n", encoding="utf-8")
    relative = Path(os.path.relpath(path))
    assert _refuse_output(path.resolve(), relative) == 2
    assert capsys.readouterr().err.startswith("logwatcher: ")


def test_follow_rejects_more_than_one_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["follow", "a.log", "b.log"]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("logwatcher: ")
    assert captured.out == ""


def test_follow_json_flag_on_the_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "app.log"
    path.write_text(_error(0, "disk full") + "\n", encoding="utf-8")

    def sleep(_seconds: float) -> None:
        raise KeyboardInterrupt

    assert (
        main(
            ["--format", "json", "follow", "--from-start", str(path)],
            sleep=sleep,
            clock=lambda: FUTURE,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["message"] == "disk full"


def test_file_tail_closes_its_handle(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_text(_error(0, "line") + "\n", encoding="utf-8")
    tail = FileTail(path, from_start=True)
    assert tail.poll() == [_error(0, "line")]
    tail.close()
    # close() drops the handle. A later poll opens the path again.
    assert tail.poll() == [_error(0, "line")]
