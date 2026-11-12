# logwatcher

`logwatcher` will be a command-line tool that watches logs and reports notable events.

**Status:** work in progress. The package can be installed, and the `logwatcher` command prints a short usage hint and exits successfully. It does not read or analyze log files yet. Pattern detection and follow mode are not implemented.

`parse_log` in `logwatcher.parser` parses a path or an iterable of lines into records plus a skip count. It accepts three line shapes: an ISO-8601 timestamp with a trailing level (`2024-03-01T12:00:00Z ERROR connection refused`), a syslog-style `YYYY-MM-DD HH:MM:SS,mmm LEVEL name: message` line, and a JSON Lines object with `ts` or `timestamp` and `level` or `severity`. `ERROR`, `FATAL`, and `CRITICAL` are errors. `WARN` and `WARNING` are warnings. Any other level is context. Blank lines are ignored. Lines that do not match are skipped. A non-empty source that matches nothing raises `ParseError`.

`detect_spikes` in `logwatcher.spikes` reads those records and looks for a sudden burst of errors. It keeps records whose kind is `error`, groups them by the message text, and counts each message in fixed windows. The default window is 5 minutes. A window is a spike when it contains at least 3 events and that count is at least 3 times the median of the same message's other non-empty windows. One window is never a spike, because there is no baseline. A message that stays at or above 3 events in every non-empty window, with no window high enough to be a spike, is reported as repeated. Timestamps without a timezone are treated as UTC. The window, the minimum count, and the multiplier are arguments.

## Requirements

Python 3.11 or newer. There are no runtime dependencies.

## Install for development

```bash
python -m pip install -e ".[dev]"
```

The `dev` extra installs pytest and ruff.

## Run tests

```bash
ruff check .
pytest
```

GitHub Actions runs those two commands on Python 3.11 and 3.12 for every push and pull request.

## Usage

```bash
logwatcher --help
python -m logwatcher --help
```

Both commands exit 0. They do not read or analyze log files yet.

Released under the MIT License.
