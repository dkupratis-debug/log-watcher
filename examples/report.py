"""Print parse and spike results for one log file.

This is a demo of the library. It is not the product command.
``logwatcher scan`` is not implemented yet.
"""

from __future__ import annotations

import sys

from logwatcher import detect_spikes, parse_log


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv[0]`` and print the spike report. Return a process status."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python3 examples/report.py LOG", file=sys.stderr)
        return 2
    result = parse_log(args[0])
    report = detect_spikes(result.records)
    print(f"records: {len(result.records)}")
    print(f"skipped: {result.skipped}")
    print(f"spikes: {len(report.spikes)}")
    for index, spike in enumerate(report.spikes, start=1):
        print(f"  [{index}] {spike.error_type}")
        print(f"      count: {spike.count}")
        print(f"      baseline_median: {spike.baseline_median}")
        print(
            "      window: "
            f"{spike.window_start.isoformat()} .. "
            f"{spike.window_end.isoformat()}"
        )
    print(f"repeated: {len(report.repeated)}")
    for index, item in enumerate(report.repeated, start=1):
        counts = ", ".join(str(count) for count in item.counts)
        print(f"  [{index}] {item.error_type}")
        print(f"      median: {item.median}")
        print(f"      counts: {counts}")
        print("      windows:")
        for start in item.window_starts:
            print(f"        {start.isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
