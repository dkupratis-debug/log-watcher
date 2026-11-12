"""Detect a sudden burst of errors, not a rate that is merely high.

``detect_spikes`` groups error records by their message (the error type)
and counts each type in fixed windows. The defaults are:

- window: 5 minutes (``DEFAULT_WINDOW``)
- minimum events in a spike bucket: 3 (``DEFAULT_MIN_COUNT``)
- spike multiplier: 3 (``DEFAULT_MULTIPLIER``)

A bucket is a spike when it holds at least that minimum and its count is
at least that many times the median of the same type's other non-empty
buckets. One bucket is never a spike: there is no other bucket to use as
a baseline. Empty buckets are ignored, so a quiet gap does not pull the
baseline down to zero.

A type that stays at or above the minimum in every non-empty bucket,
with no bucket high enough to be a spike, is reported as repeated rather
than as a spike. A type is never both.

The error type is the message text exactly as parsed. Two messages that
differ only by an id are different types. Pattern normalization is a
separate step and is not done here.

Timestamps with no timezone are read as UTC. Aware timestamps are
converted to UTC before they are placed in a window. A record with no
timestamp cannot be placed, so it is skipped. Only records whose ``kind``
is ``error`` are counted.

Only the Python standard library is used.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from logwatcher.parser import LogRecord

# Defaults live here so the function body never repeats the magic numbers.
# The module docstring above is the place those numbers are explained.
DEFAULT_WINDOW = timedelta(minutes=5)
DEFAULT_MIN_COUNT = 3
DEFAULT_MULTIPLIER = 3

# Windows are aligned to this instant so a timestamp always falls in the
# same bucket, no matter the order of the records.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Spike:
    """One window where an error type jumped above its own baseline.

    ``window_end`` is exclusive: an event at that instant belongs to the
    next window. ``baseline_median`` is the median count of this type's
    other non-empty windows, not of this window itself.
    """

    error_type: str
    window_start: datetime
    window_end: datetime
    count: int
    baseline_median: float


@dataclass(frozen=True)
class RepeatedType:
    """An error type that stayed high in every window, with no spike.

    ``counts`` lines up with ``window_starts``. Both are in time order.
    ``median`` is the median of ``counts``, which is the steady level.
    """

    error_type: str
    window_starts: tuple[datetime, ...]
    counts: tuple[int, ...]
    median: float


@dataclass(frozen=True)
class SpikeReport:
    """Spikes and steadily high types found in one pass.

    A given error type appears in ``spikes`` or in ``repeated``, or in
    neither. It is never in both. Both sequences are ordered: spikes by
    window start then type, repeated types by type name.
    """

    spikes: tuple[Spike, ...]
    repeated: tuple[RepeatedType, ...]


def detect_spikes(
    records: Iterable[LogRecord],
    *,
    window: timedelta = DEFAULT_WINDOW,
    min_count: int = DEFAULT_MIN_COUNT,
    multiplier: int | float = DEFAULT_MULTIPLIER,
) -> SpikeReport:
    """Find error-rate spikes and steadily high error types.

    ``records`` is an iterable of parsed log records. It is read once and
    is not reordered. Warnings, context lines, and records with no
    timestamp are ignored.

    ``window`` is the fixed bucket width. ``min_count`` is N, the least
    number of events a bucket must hold. ``multiplier`` is K: a bucket
    spikes when its count is at least K times the median of the other
    non-empty buckets of the same error type. The defaults are a 5-minute
    window, N of 3, and K of 3.

    Raises:
        ValueError: ``window`` is not a positive ``timedelta``,
            ``min_count`` is not an integer of at least 1, or
            ``multiplier`` is not a finite number greater than 0.
    """
    _check_settings(window, min_count, multiplier)
    buckets = _count_buckets(records, window)
    return _report(buckets, window, min_count, multiplier)


def _check_settings(
    window: timedelta,
    min_count: int,
    multiplier: int | float,
) -> None:
    """Reject settings that would make the rule meaningless.

    ``bool`` is a subclass of ``int``, so it is rejected on its own.
    Otherwise ``True`` would silently mean a multiplier of 1.
    """
    if not isinstance(window, timedelta) or window <= timedelta(0):
        raise ValueError("window must be a positive timedelta")
    if isinstance(min_count, bool) or not isinstance(min_count, int) or min_count < 1:
        raise ValueError("min_count must be an integer of at least 1")
    if (
        isinstance(multiplier, bool)
        or not isinstance(multiplier, (int, float))
        or not math.isfinite(multiplier)
        or multiplier <= 0
    ):
        raise ValueError("multiplier must be a finite number greater than 0")


def _count_buckets(
    records: Iterable[LogRecord],
    window: timedelta,
) -> dict[str, dict[datetime, int]]:
    """Count error records per type and window start.

    The inner dict only contains windows that have at least one event.
    Missing keys are empty windows, and empty windows are not a baseline.
    """
    buckets: dict[str, dict[datetime, int]] = {}
    for record in records:
        if record.kind != "error" or record.timestamp is None:
            continue
        start = _bucket_start(record.timestamp, window)
        per_type = buckets.setdefault(record.message, {})
        per_type[start] = per_type.get(start, 0) + 1
    return buckets


def _report(
    buckets: dict[str, dict[datetime, int]],
    window: timedelta,
    min_count: int,
    multiplier: int | float,
) -> SpikeReport:
    """Turn per-type counts into spikes and repeated types."""
    spikes: list[Spike] = []
    repeated: list[RepeatedType] = []
    for error_type, counts_by_start in buckets.items():
        starts = sorted(counts_by_start)
        # A single bucket has no other window to compare with.
        if len(starts) < 2:
            continue
        series = [counts_by_start[start] for start in starts]
        found = _spikes_for_type(
            error_type,
            starts,
            series,
            window,
            min_count,
            multiplier,
        )
        if found:
            spikes.extend(found)
            continue
        # Steadily high: every window clears the same minimum, and none
        # of them cleared the multiplier. A dip below the minimum is not
        # "steady", so it is not reported as repeated either.
        if all(count >= min_count for count in series):
            repeated.append(
                RepeatedType(
                    error_type=error_type,
                    window_starts=tuple(starts),
                    counts=tuple(series),
                    median=_median(series),
                )
            )
    spikes.sort(key=lambda spike: (spike.window_start, spike.error_type))
    repeated.sort(key=lambda item: item.error_type)
    return SpikeReport(tuple(spikes), tuple(repeated))


def _spikes_for_type(
    error_type: str,
    starts: list[datetime],
    series: list[int],
    window: timedelta,
    min_count: int,
    multiplier: int | float,
) -> list[Spike]:
    """Return the spike buckets for one type, in time order.

    Each bucket is compared with the median of the other counts only.
    Including itself would let a burst raise its own baseline.

    The counts are sorted once. Each baseline is then the middle of that
    list with one copy of the candidate removed, so a long steady history
    does not sort again for every window.
    """
    found: list[Spike] = []
    ordered = sorted(series)
    for start, count in zip(starts, series, strict=True):
        if count < min_count:
            continue
        # Exactly K times the baseline still counts: the rule is "at least".
        baseline, clears = _baseline_without(ordered, count, multiplier)
        if clears:
            found.append(
                Spike(
                    error_type=error_type,
                    window_start=start,
                    window_end=start + window,
                    count=count,
                    baseline_median=baseline,
                )
            )
    return found


def _baseline_without(
    ordered: list[int],
    count: int,
    multiplier: int | float,
) -> tuple[float, bool]:
    """Median of ``ordered`` without one ``count``, and whether it is cleared.

    ``ordered`` holds every bucket for this type. Equal counts sit together,
    and the median only depends on values, so dropping any one copy of
    ``count`` is the same as dropping this bucket.

    An even number of remaining buckets has a baseline halfway between the
    two middle counts. Compare ``count * 2`` with ``multiplier * (low + high)``
    so an integer multiplier stays on whole numbers instead of a fraction
    such as 1.5.
    """
    skipped = bisect.bisect_left(ordered, count)
    remaining = len(ordered) - 1
    middle = remaining // 2
    if remaining % 2 == 1:
        baseline = _value_skipping(ordered, skipped, middle)
        return float(baseline), count >= multiplier * baseline
    low = _value_skipping(ordered, skipped, middle - 1)
    high = _value_skipping(ordered, skipped, middle)
    return (low + high) / 2, count * 2 >= multiplier * (low + high)


def _value_skipping(ordered: list[int], skipped: int, index: int) -> int:
    """Value at ``index`` in ``ordered`` after deleting ``ordered[skipped]``."""
    if index < skipped:
        return ordered[index]
    return ordered[index + 1]


def _median(values: list[int]) -> float:
    """Median of a non-empty list of counts.

    An odd count returns the middle value. An even count averages the two
    middle values, so the baseline can sit halfway between two buckets.
    """
    if not values:
        raise ValueError("median requires at least one value")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def _bucket_start(moment: datetime, window: timedelta) -> datetime:
    """Start of the fixed window that contains ``moment``.

    The width is measured in whole microseconds from the Unix epoch, then
    integer-divided. Floor division (not truncation toward zero) keeps a
    timestamp just before the epoch in the earlier window.
    """
    utc = _as_utc(moment)
    size = _microseconds(window)
    elapsed = _microseconds(utc - _EPOCH)
    index = elapsed // size
    return _EPOCH + timedelta(microseconds=index * size)


def _as_utc(moment: datetime) -> datetime:
    """Return ``moment`` as an aware UTC datetime.

    A naive value has no zone. Read that clock time as UTC. Do not convert
    it from the machine's local zone.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _microseconds(delta: timedelta) -> int:
    """Whole microseconds in ``delta``, including a negative delta.

    ``timedelta`` stores a negative value with a negative ``days`` and
    non-negative ``seconds`` and ``microseconds``. Adding the three parts
    still yields the signed total.
    """
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
