# logwatcher

`logwatcher` will be a command-line tool that watches logs and reports notable events.

**Status:** work in progress. The package can be installed, and the `logwatcher` command prints a short usage hint and exits successfully. Log parsing and detection are not implemented yet.

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
