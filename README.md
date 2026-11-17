# logwatcher

`logwatcher` reads application logs and reports an error that suddenly bursts. A file can also be full of errors that have been noisy the whole time. The tool separates those two cases, so a burst is visible on its own.

Parsing, spike detection, a text and JSON report, and follow mode are implemented. The runtime is the Python standard library only. New-pattern detection is listed under [What's next](#whats-next).

## What it does

### Parse a log

`parse_log` reads a path or an iterable of lines and returns the records plus a count of skipped lines.

A line can have one of three shapes:

- An ISO-8601 timestamp, a level, and an optional message: `2024-03-01T12:00:00Z ERROR connection refused`. The date and time may be separated by `T` or a space. A timezone (`Z` or `±HH:MM`) is optional.
- A syslog-style line, matching Python logging's default timestamp: `2024-03-01 12:00:00,123 ERROR app.worker: job failed`. The logger name is required. The stored message is the text after `name:`.
- One JSON object per line, with a timestamp under `ts` or `timestamp` and a level under `level` or `severity`. The message is taken from `message`, or from `msg` when that is absent. A numeric timestamp is seconds since the Unix epoch, or milliseconds when the number is at least `1e12`.

`ERROR`, `FATAL`, and `CRITICAL` are errors. `WARN` and `WARNING` are warnings. Any other level is context. Level matching is case-insensitive. Blank lines are ignored and are not counted as skipped. Any other non-blank line that matches none of the shapes is skipped. When the source has non-blank lines and none of them match, `parse_log` raises `ParseError`. An empty file is a successful empty result.

A timestamp written without a timezone is a naive datetime. Spike detection reads that clock time as UTC.

### Detect a spike

`detect_spikes` counts records whose kind is `error`. Warnings, context lines, and records with no timestamp are left out. Aware timestamps are converted to UTC before they are placed in a window.

The error type is the message text exactly as parsed. Two messages that differ only by an id are different types. Folding those together is pattern detection, which is [issue #4](https://github.com/dkupratis-debug/log-watcher/issues/4) and is not implemented.

Counts for each message fall into fixed windows. The default window is 5 minutes, aligned to the Unix epoch, so a timestamp always lands in the same window. Empty windows are ignored. A quiet gap does not pull the baseline down to zero.

A window is a spike when both of these hold:

- it contains at least 3 events of that message
- that count is at least 3 times the median of the same message's other non-empty windows

The window under test is left out of its own median. Exactly 3 events, and exactly 3 times the baseline, still counts as a spike. A message that appears in only one window is never a spike, because there is no other window to compare with.

A message that stays at or above 3 events in every non-empty window, and never reaches the multiplier, is reported as repeated. The same message is not reported as both a spike and a repeated type.

The window, the minimum count, and the multiplier are arguments of `detect_spikes`. The defaults above are the ones the demo uses.

## Tech stack

- Python 3.11 and 3.12. The package requires Python 3.11 or newer, and [CI](.github/workflows/ci.yml) tests 3.11 and 3.12.
- The Python standard library only, at runtime. There are no runtime dependencies.
- setuptools builds the package.
- pytest and ruff are the development tools. They are installed with the `dev` extra.
- GitHub Actions workflow [`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on `ubuntu-latest` for every push and pull request. It installs the package with the dev extra, then runs `ruff check .` and `pytest`, once per Python version in the matrix.

## Install

From a clone of this repository:

```bash
python3 -m pip install -e ".[dev]"
```

The `dev` extra installs pytest and ruff. The package install also creates the `logwatcher` console script.

## Usage

The install creates the `logwatcher` command. `python3 -m logwatcher` is the same entry point. With no arguments, or with `--help`, it prints help and exits 0.

`logwatcher FILE [FILE ...]` scans saved logs. `logwatcher scan FILE` is the same command. The report lists lines, records, errors, skipped lines, spikes, new patterns, and repeated types. `--format json` prints that same report as JSON. `-o PATH` writes it to a file instead of stdout.

`new_patterns` is in the report so the text and the JSON stay aligned. Pattern detection is not implemented, so that list is empty.

A scan exits 0 when the files were read and no spike was found, 1 when a spike was found, and 2 when a path is missing, unreadable, or not a log this parser understands. A known problem is printed as `logwatcher: ...` on stderr and does not include a traceback.

Run these from the repository root after the install command above. The block under each command is that process's standard output.

```bash
logwatcher examples/api.log
```

```
file: examples/api.log
lines: 24
records: 23
errors: 21
skipped: 1
spikes: 1
  [1] connection refused
      count: 6
      baseline_median: 1.0
      window: 2024-03-01T12:15:00+00:00 .. 2024-03-01T12:20:00+00:00
new_patterns: 0
repeated: 1
  [1] disk full
      median: 3.0
      counts: 3, 3, 3, 3
      windows:
        2024-03-01T12:00:00+00:00
        2024-03-01T12:05:00+00:00
        2024-03-01T12:10:00+00:00
        2024-03-01T12:15:00+00:00
```

That process exited 1. `python3 -m logwatcher examples/api.log` and `logwatcher scan examples/api.log` printed the same text and also exited 1.

```bash
logwatcher --format json examples/api.log
```

```
{
  "file": "examples/api.log",
  "lines": 24,
  "records": 23,
  "errors": 21,
  "skipped": 1,
  "spikes": [
    {
      "error_type": "connection refused",
      "count": 6,
      "baseline_median": 1.0,
      "window_start": "2024-03-01T12:15:00+00:00",
      "window_end": "2024-03-01T12:20:00+00:00"
    }
  ],
  "new_patterns": [],
  "repeated": [
    {
      "error_type": "disk full",
      "median": 3.0,
      "counts": [
        3,
        3,
        3,
        3
      ],
      "windows": [
        "2024-03-01T12:00:00+00:00",
        "2024-03-01T12:05:00+00:00",
        "2024-03-01T12:10:00+00:00",
        "2024-03-01T12:15:00+00:00"
      ]
    }
  ]
}
```

That process exited 1 as well.

`logwatcher follow FILE` watches a file that is still being written. `-f` and `--follow` do the same thing, including `logwatcher scan --follow FILE`. Follow starts at the end of the file unless `--from-start` is passed. Each new error line is printed when its newline arrives. A spike is printed when the clock reaches the end of that window, and a repeated type is printed once, when its closed windows qualify. If the file shrinks, or the path comes to point at a different file, follow reads the new contents instead of stopping. Ctrl-C and SIGTERM exit 0. `--interval SECONDS` is the pause between reads. The default is 0.5.

`examples/api.log` is already complete, so this run uses `--from-start`. The block under the command is the standard output from that process. Every window in the file had already closed, so the spike and the repeated type were printed on the first read. The process then waited, and SIGINT exited 0.

```bash
logwatcher follow --from-start examples/api.log
```

```
error: 2024-03-01T12:00:10+00:00 ERROR connection refused
error: 2024-03-01T12:05:10+00:00 ERROR connection refused
error: 2024-03-01T12:10:10+00:00 ERROR connection refused
error: 2024-03-01T12:15:01+00:00 ERROR connection refused
error: 2024-03-01T12:15:02+00:00 ERROR connection refused
error: 2024-03-01T12:15:03+00:00 ERROR connection refused
error: 2024-03-01T12:15:04+00:00 ERROR connection refused
error: 2024-03-01T12:15:05+00:00 ERROR connection refused
error: 2024-03-01T12:15:06+00:00 ERROR connection refused
error: 2024-03-01T12:00:20+00:00 ERROR disk full
error: 2024-03-01T12:00:21+00:00 ERROR disk full
error: 2024-03-01T12:00:22+00:00 ERROR disk full
error: 2024-03-01T12:05:20+00:00 ERROR disk full
error: 2024-03-01T12:05:21+00:00 ERROR disk full
error: 2024-03-01T12:05:22+00:00 ERROR disk full
error: 2024-03-01T12:10:20+00:00 ERROR disk full
error: 2024-03-01T12:10:21+00:00 ERROR disk full
error: 2024-03-01T12:10:22+00:00 ERROR disk full
error: 2024-03-01T12:15:20+00:00 ERROR disk full
error: 2024-03-01T12:15:21+00:00 ERROR disk full
error: 2024-03-01T12:15:22+00:00 ERROR disk full
spike: connection refused count=6 baseline_median=1.0 window=2024-03-01T12:15:00+00:00 .. 2024-03-01T12:20:00+00:00
repeated: disk full median=3.0 counts=3, 3, 3, 3
```

[`examples/report.py`](examples/report.py) is a short demo of the library calls. The command above is the interface that scans files and follows a growing log.

## Demo

[`examples/api.log`](examples/api.log) is fictional. It contains no hostnames, customer names, or credentials. Every matched line is ISO-8601 with a `Z` timezone.

From 12:00 to 12:20 UTC on 2024-03-01 the file has:

- `connection refused` once in each of the first three 5-minute windows, then six times in the window that starts at 12:15
- `disk full` three times in each of those four windows
- one `INFO` line (`worker started`) and one `WARNING` line (`queue delay`), which parse as records and are not counted as errors
- a final line, `this line is not a log`, which matches no shape and is skipped

Run this from the repository root after the install command above. The block under the command is the program's standard output from that run:

```bash
python3 examples/report.py examples/api.log
```

```
records: 23
skipped: 1
spikes: 1
  [1] connection refused
      count: 6
      baseline_median: 1.0
      window: 2024-03-01T12:15:00+00:00 .. 2024-03-01T12:20:00+00:00
repeated: 1
  [1] disk full
      median: 3.0
      counts: 3, 3, 3, 3
      windows:
        2024-03-01T12:00:00+00:00
        2024-03-01T12:05:00+00:00
        2024-03-01T12:10:00+00:00
        2024-03-01T12:15:00+00:00
```

`connection refused` in the 12:15 window has count 6. The median of its other windows is 1, and 6 is at least 3 times that baseline, so the window is a spike. `disk full` stays at 3 in every window. That meets the minimum of 3, and 3 is below 3 times the median of the other windows, so it is reported as repeated.

[`docs/spike-demo.svg`](docs/spike-demo.svg) and the still below are that session, drawn in DejaVu Sans Mono with each character on a fixed grid. The shell prompt and the command are shown around the standard output above, including its indentation.

![Demo session in a monospace terminal](docs/spike-demo.png)

## Tests

```bash
ruff check .
pytest
```

The CI workflow runs those two commands on Python 3.11 and Python 3.12.

## What's next

Pattern detection is still open.

- [#4 Detect new error patterns](https://github.com/dkupratis-debug/log-watcher/issues/4). Normalize each error to a pattern key: an `error_type` or `exception` field, then an exception class name in the message, then an HTTP 5xx token, then the message with digits and UUIDs replaced by a placeholder. The first time a key appears, mark it as new, even when it happens only once.

## License

Released under the [MIT License](LICENSE).
