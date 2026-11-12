"""Tests for error-rate spike detection."""

from __future__ import annotations

import inspect
import math
import os
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

import logwatcher
import logwatcher.spikes as spikes
from logwatcher.parser import LogRecord, parse_log
from logwatcher.spikes import (
    DEFAULT_MIN_COUNT,
    DEFAULT_MULTIPLIER,
    DEFAULT_WINDOW,
    RepeatedType,
    Spike,
    SpikeReport,
    detect_spikes,
)

FIXTURES = Path(__file__).parent / "fixtures"

# 12:00 UTC is an exact 5-minute boundary, so test times land on windows
# a person can check by hand: 12:00, 12:05, 12:10, and so on.
NOON = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
FIVE_MINUTES = timedelta(minutes=5)


def _error(
    moment: datetime,
    message: str = "connection refused",
    *,
    kind: str = "error",
    level: str = "ERROR",
) -> LogRecord:
    return LogRecord(
        timestamp=moment,
        level=level,
        message=message,
        raw=message,
        kind=kind,  # type: ignore[arg-type]
    )


def _at(minute: int, second: int = 0) -> datetime:
    return NOON + timedelta(minutes=minute, seconds=second)


def _series(
    counts: list[int],
    message: str = "connection refused",
    *,
    start: datetime = NOON,
    step: timedelta = FIVE_MINUTES,
) -> list[LogRecord]:
    """One timestamp per window. A 0 count leaves that window empty."""
    records: list[LogRecord] = []
    for index, count in enumerate(counts):
        moment = start + step * index
        records.extend(_error(moment, message) for _ in range(count))
    return records


def test_clear_spike_fixture() -> None:
    report = detect_spikes(parse_log(FIXTURES / "spike.log").records)
    assert report.repeated == ()
    assert report.spikes == (
        Spike(
            error_type="connection refused",
            window_start=_at(15),
            window_end=_at(20),
            count=6,
            baseline_median=1.0,
        ),
    )


def test_json_lines_follow_the_same_rule() -> None:
    lines = []
    for minute, count in ((0, 1), (5, 1), (10, 6)):
        stamp = f"2024-03-01T12:{minute:02d}:00Z"
        lines.extend(
            f'{{"ts": "{stamp}", "level": "ERROR", "message": "disk full"}}'
            for _ in range(count)
        )
    report = detect_spikes(parse_log(lines).records)
    assert report.repeated == ()
    assert report.spikes == (
        Spike(
            error_type="disk full",
            window_start=_at(10),
            window_end=_at(15),
            count=6,
            baseline_median=1.0,
        ),
    )


def test_steady_high_rate_is_repeated_not_a_spike() -> None:
    report = detect_spikes(parse_log(FIXTURES / "steady.log").records)
    assert report.spikes == ()
    assert report.repeated == (
        RepeatedType(
            error_type="disk full",
            window_starts=(_at(0), _at(5), _at(10), _at(15)),
            counts=(4, 4, 4, 4),
            median=4.0,
        ),
    )


def test_single_large_bucket_is_not_a_spike() -> None:
    records = parse_log(FIXTURES / "single_bucket.log").records
    assert len([record for record in records if record.kind == "error"]) == 8
    assert detect_spikes(records) == SpikeReport((), ())
    # Lowering both thresholds still cannot invent a baseline.
    assert detect_spikes(records, min_count=1, multiplier=1) == SpikeReport((), ())


def test_empty_error_list() -> None:
    assert detect_spikes([]) == SpikeReport((), ())
    records, skipped = parse_log(FIXTURES / "empty.log")
    assert records == []
    assert skipped == 0
    assert detect_spikes(records) == SpikeReport((), ())


def test_lines_without_timestamps_produce_no_spikes() -> None:
    result = parse_log(FIXTURES / "no_timestamps.log")
    assert result.skipped == 4
    assert [record.kind for record in result.records] == ["context", "context"]
    assert detect_spikes(result.records) == SpikeReport((), ())


def test_records_with_no_timestamp_are_skipped() -> None:
    missing = [
        _error(None, "connection refused"),  # type: ignore[arg-type]
        _error(None, "connection refused"),  # type: ignore[arg-type]
        _error(None, "connection refused"),  # type: ignore[arg-type]
    ]
    assert detect_spikes(missing) == SpikeReport((), ())
    # A timestamped baseline still ignores the undated copies.
    records = [*missing, *_series([1, 1, 1, 3])]
    report = detect_spikes(records)
    assert report.spikes[0].count == 3


def test_single_spike_on_the_threshold() -> None:
    """Exactly N events and exactly K times the baseline is one spike.

    The fixture has no timezone, so this also checks that a naive clock
    time is read as UTC.
    """
    result = parse_log(FIXTURES / "single_spike.log")
    assert result.skipped == 0
    assert result.records[0].timestamp.tzinfo is None
    report = detect_spikes(result.records)
    assert report.repeated == ()
    assert report.spikes == (
        Spike(
            error_type="connection refused",
            window_start=_at(15),
            window_end=_at(20),
            count=3,
            baseline_median=1.0,
        ),
    )


def test_one_below_the_minimum_is_not_a_spike() -> None:
    report = detect_spikes(_series([1, 1, 2]))
    assert report == SpikeReport((), ())


def test_one_below_the_multiplier_is_not_a_spike() -> None:
    # 5 is at least the minimum, but it is not 3 times the other bucket.
    report = detect_spikes(_series([2, 5]))
    assert report == SpikeReport((), ())


def test_exactly_k_times_the_other_bucket_is_a_spike() -> None:
    report = detect_spikes(_series([2, 6]))
    assert report.spikes[0].count == 6
    assert report.spikes[0].baseline_median == 2.0
    assert report.repeated == ()


def test_baseline_excludes_the_candidate_bucket() -> None:
    # Median of the other bucket is 1, so 4 is a spike. Including the
    # candidate would make the median 2.5 and hide it (3 * 2.5 = 7.5).
    report = detect_spikes(_series([1, 4]))
    assert len(report.spikes) == 1
    assert report.spikes[0].baseline_median == 1.0


def test_baseline_is_the_median_not_the_mean() -> None:
    # Others for the last bucket are 1, 1, 100. The median is 1 and the
    # mean is 34. 30 is a spike only when the baseline is the median.
    report = detect_spikes(_series([1, 1, 100, 30]))
    assert [spike.count for spike in report.spikes] == [100, 30]
    assert report.spikes[1].baseline_median == 1.0

    # Others for 21 are 1, 9, 10. The median is 9 and the mean is about
    # 6.7. 21 clears 3 times the mean and does not clear 3 times the median.
    quiet = detect_spikes(_series([1, 9, 10, 21]))
    assert quiet == SpikeReport((), ())


def test_empty_windows_do_not_dilute_the_baseline() -> None:
    # An hour of silence sits between the baseline events and the burst.
    # Those empty windows are not zeros in the median.
    records = [
        _error(_at(0)),
        _error(_at(60)),
        *(_error(_at(120)) for _ in range(9)),
    ]
    report = detect_spikes(records)
    assert len(report.spikes) == 1
    assert report.spikes[0].window_start == _at(120)
    assert report.spikes[0].baseline_median == 1.0
    assert report.spikes[0].count == 9


def test_two_spikes_for_one_type_stay_in_time_order() -> None:
    report = detect_spikes(_series([1, 9, 1, 9]))
    assert [spike.count for spike in report.spikes] == [9, 9]
    assert [spike.window_start for spike in report.spikes] == [_at(5), _at(15)]
    assert report.repeated == ()


def test_equal_high_windows_are_repeated() -> None:
    report = detect_spikes(_series([4, 4]))
    assert report.spikes == ()
    assert report.repeated[0].counts == (4, 4)
    assert report.repeated[0].median == 4.0


def test_mild_bump_stays_repeated_until_the_multiplier_says_otherwise() -> None:
    records = _series([3, 3, 6])
    steady = detect_spikes(records)
    assert steady.spikes == ()
    assert steady.repeated[0].counts == (3, 3, 6)
    # The even baseline is the average of 3 and 3, which is 3, so 6 is
    # exactly 2 times the median and not 3 times it.
    spiked = detect_spikes(records, multiplier=2)
    assert [spike.count for spike in spiked.spikes] == [6]
    assert spiked.spikes[0].baseline_median == 3.0
    assert spiked.repeated == ()


def test_a_dip_below_the_minimum_is_not_repeated() -> None:
    assert detect_spikes(_series([3, 3, 3, 2])) == SpikeReport((), ())


def test_a_flat_low_rate_is_not_repeated() -> None:
    assert detect_spikes(_series([1, 1, 1, 1])) == SpikeReport((), ())


def test_types_do_not_share_a_baseline() -> None:
    records = [
        *_series([1, 1, 1, 10], "connection refused"),
        *_series([5, 5, 5, 5], "disk full"),
    ]
    report = detect_spikes(records)
    assert [spike.error_type for spike in report.spikes] == ["connection refused"]
    assert report.spikes[0].count == 10
    assert [item.error_type for item in report.repeated] == ["disk full"]


def test_same_message_at_error_and_critical_is_one_type() -> None:
    records = [
        _error(_at(0), level="ERROR"),
        _error(_at(5), level="FATAL"),
        _error(_at(10), level="CRITICAL"),
        _error(_at(15), level="ERROR"),
        _error(_at(15), level="CRITICAL"),
        _error(_at(15), level="FATAL"),
    ]
    report = detect_spikes(records)
    assert len(report.spikes) == 1
    assert report.spikes[0].count == 3
    assert report.spikes[0].error_type == "connection refused"


def test_warnings_and_context_are_not_errors() -> None:
    records = [
        *_series([1, 1, 1]),
        _error(_at(15), kind="warning", level="WARNING"),
        _error(_at(15), kind="warning", level="WARNING"),
        _error(_at(15), kind="warning", level="WARNING"),
        _error(_at(15), kind="context", level="INFO"),
    ]
    assert detect_spikes(records) == SpikeReport((), ())


def test_different_messages_are_different_types() -> None:
    records = [_error(_at(0), "disk full"), _error(_at(0), "timeout")]
    assert detect_spikes(records) == SpikeReport((), ())


def test_window_end_is_exclusive() -> None:
    # 12:05:00 starts the next window. One microsecond earlier stays in
    # the 12:00 window, so that baseline count is 2 and the burst is 6.
    # If the boundary event were pulled into the burst, the count would
    # be 7 and the baseline would be 1.
    just_before = datetime(2024, 3, 1, 12, 4, 59, 999999, tzinfo=UTC)
    records = [
        _error(_at(0)),
        _error(just_before),
        *(_error(_at(5)) for _ in range(6)),
    ]
    report = detect_spikes(records)
    assert report.spikes == (
        Spike(
            error_type="connection refused",
            window_start=_at(5),
            window_end=_at(10),
            count=6,
            baseline_median=2.0,
        ),
    )


def test_naive_and_utc_timestamps_share_a_window() -> None:
    naive = datetime(2024, 3, 1, 12, 15, 0)
    aware = datetime(2024, 3, 1, 12, 15, 1, tzinfo=UTC)
    records = [
        *_series([1, 1, 1]),
        _error(naive),
        _error(aware),
        _error(_at(15, 2)),
    ]
    report = detect_spikes(records)
    assert report.spikes[0].window_start == _at(15)
    assert report.spikes[0].window_start.tzinfo is UTC
    assert report.spikes[0].count == 3


def test_naive_timestamp_is_not_read_as_local_time() -> None:
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        # March 2024 in New York is UTC-5, so this only proves the point
        # when the zone data is actually applied.
        local_as_utc = datetime(2024, 3, 1, 12, 0).astimezone(UTC)
        assert local_as_utc == datetime(2024, 3, 1, 17, 0, tzinfo=UTC)
        records = [
            _error(datetime(2024, 3, 1, 12, 0, 0)),
            _error(datetime(2024, 3, 1, 12, 5, 0)),
            _error(datetime(2024, 3, 1, 12, 10, 0)),
            _error(datetime(2024, 3, 1, 12, 15, 0)),
            _error(datetime(2024, 3, 1, 12, 15, 1)),
            _error(datetime(2024, 3, 1, 12, 15, 2)),
        ]
        report = detect_spikes(records)
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()
    assert report.spikes[0].window_start == datetime(2024, 3, 1, 12, 15, tzinfo=UTC)


def test_aware_offset_is_converted_to_utc() -> None:
    plus_five = timezone(timedelta(hours=5))
    # 17:00 at UTC+5 is 12:00 UTC, so these share the noon windows.
    records = [
        _error(datetime(2024, 3, 1, 17, 0, tzinfo=plus_five)),
        _error(datetime(2024, 3, 1, 17, 5, tzinfo=plus_five)),
        _error(datetime(2024, 3, 1, 17, 10, tzinfo=plus_five)),
        _error(datetime(2024, 3, 1, 17, 15, tzinfo=plus_five)),
        _error(datetime(2024, 3, 1, 17, 15, 1, tzinfo=plus_five)),
        _error(datetime(2024, 3, 1, 17, 15, 2, tzinfo=plus_five)),
    ]
    report = detect_spikes(records)
    assert report.spikes[0].window_start == _at(15)
    assert report.spikes[0].count == 3


def test_input_order_does_not_change_the_report() -> None:
    records = _series([1, 1, 1, 6], "connection refused") + _series(
        [4, 4, 4], "disk full"
    )
    reversed_records = list(reversed(records))
    assert detect_spikes(reversed_records) == detect_spikes(records)
    assert detect_spikes(iter(records)) == detect_spikes(records)


def test_two_types_spiking_in_one_window_are_ordered_by_name() -> None:
    records = _series([1, 9], "timeout") + _series([1, 9], "disk full")
    report = detect_spikes(records)
    assert [spike.error_type for spike in report.spikes] == ["disk full", "timeout"]
    assert [spike.window_start for spike in report.spikes] == [_at(5), _at(5)]


def test_repeated_types_are_ordered_by_name() -> None:
    records = _series([3, 3], "timeout") + _series([4, 4], "disk full")
    report = detect_spikes(records)
    assert [item.error_type for item in report.repeated] == ["disk full", "timeout"]


def test_window_argument_can_remove_the_spike() -> None:
    records = parse_log(FIXTURES / "spike.log").records
    # One hour holds every event of the fixture, so there is no baseline.
    report = detect_spikes(records, window=timedelta(hours=1))
    assert report == SpikeReport((), ())


def test_min_count_argument_can_remove_the_spike() -> None:
    records = parse_log(FIXTURES / "spike.log").records
    assert detect_spikes(records, min_count=7) == SpikeReport((), ())


def test_multiplier_argument_can_remove_the_spike() -> None:
    records = parse_log(FIXTURES / "spike.log").records
    # 6 is three times the baseline of 1, and it is not seven times it.
    assert detect_spikes(records, multiplier=7) == SpikeReport((), ())


def test_lower_thresholds_flag_a_smaller_bump() -> None:
    records = _series([1, 2], step=timedelta(seconds=30))
    # The default 5-minute window merges these into one bucket.
    assert detect_spikes(records) == SpikeReport((), ())
    report = detect_spikes(
        records,
        window=timedelta(seconds=30),
        min_count=2,
        multiplier=2,
    )
    assert report.spikes == (
        Spike(
            error_type="connection refused",
            window_start=NOON + timedelta(seconds=30),
            window_end=NOON + timedelta(seconds=60),
            count=2,
            baseline_median=1.0,
        ),
    )


def test_fractional_multiplier() -> None:
    report = detect_spikes(_series([2, 2, 5]), multiplier=2.5)
    assert [spike.count for spike in report.spikes] == [5]
    assert report.spikes[0].baseline_median == 2.0


def test_even_median_averages_the_two_middle_counts() -> None:
    # Others for 10 are 2 and 4. The two middle values are both of them.
    report = detect_spikes(_series([2, 4, 10]))
    assert report.spikes[0].count == 10
    assert report.spikes[0].baseline_median == 3.0


def test_non_minute_window() -> None:
    start = datetime(2024, 3, 1, tzinfo=UTC)
    step = timedelta(seconds=90)
    records = _series([1, 1, 1, 3], start=start, step=step)
    report = detect_spikes(records, window=step)
    assert report.spikes[0].window_start == start + step * 3
    assert report.spikes[0].window_end == start + step * 4
    assert report.spikes[0].count == 3


def test_window_before_the_epoch_floors_downward() -> None:
    # One second before the epoch belongs to the window that started at
    # 23:55, not the window that starts at 00:00.
    before = datetime(1969, 12, 31, 23, 59, 59, tzinfo=UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    later = datetime(1970, 1, 1, 0, 6, tzinfo=UTC)
    records = [
        *(_error(before) for _ in range(3)),
        _error(epoch),
        _error(later),
    ]
    report = detect_spikes(records)
    assert len(report.spikes) == 1
    assert report.spikes[0].window_start == datetime(1969, 12, 31, 23, 55, tzinfo=UTC)
    assert report.spikes[0].count == 3
    assert report.spikes[0].baseline_median == 1.0


def test_calling_twice_returns_the_same_report() -> None:
    records = _series([1, 1, 9])
    assert detect_spikes(records) == detect_spikes(records)


def test_defaults_match_the_documented_constants() -> None:
    assert DEFAULT_WINDOW == timedelta(minutes=5)
    assert DEFAULT_MIN_COUNT == 3
    assert DEFAULT_MULTIPLIER == 3
    signature = inspect.signature(detect_spikes)
    assert signature.parameters["window"].default is DEFAULT_WINDOW
    assert signature.parameters["min_count"].default == DEFAULT_MIN_COUNT
    assert signature.parameters["multiplier"].default == DEFAULT_MULTIPLIER
    documented = spikes.__doc__ or ""
    assert "5 minutes" in documented
    assert "minimum events in a spike bucket: 3" in documented
    assert "spike multiplier: 3" in documented
    assert "UTC" in documented
    assert "repeated" in documented


def test_findings_are_frozen() -> None:
    report = detect_spikes(_series([1, 1, 9]))
    with pytest.raises(AttributeError):
        setattr(report.spikes[0], "count", 0)


def test_detect_spikes_is_exported_from_the_package() -> None:
    assert logwatcher.detect_spikes is detect_spikes
    assert logwatcher.Spike is Spike
    assert logwatcher.RepeatedType is RepeatedType
    assert logwatcher.SpikeReport is SpikeReport
    assert logwatcher.DEFAULT_WINDOW is DEFAULT_WINDOW
    assert logwatcher.DEFAULT_MIN_COUNT == DEFAULT_MIN_COUNT
    assert logwatcher.DEFAULT_MULTIPLIER == DEFAULT_MULTIPLIER


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"window": timedelta(0)}, "window"),
        ({"window": timedelta(seconds=-1)}, "window"),
        ({"window": 300}, "window"),
        ({"min_count": 0}, "min_count"),
        ({"min_count": -1}, "min_count"),
        ({"min_count": True}, "min_count"),
        ({"min_count": 1.5}, "min_count"),
        ({"multiplier": 0}, "multiplier"),
        ({"multiplier": -2}, "multiplier"),
        ({"multiplier": True}, "multiplier"),
        ({"multiplier": math.nan}, "multiplier"),
        ({"multiplier": math.inf}, "multiplier"),
    ],
)
def test_bad_settings_raise(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        detect_spikes(_series([1, 1, 9]), **kwargs)  # type: ignore[arg-type]

