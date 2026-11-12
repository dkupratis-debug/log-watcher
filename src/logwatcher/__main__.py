"""Command-line entry point for logwatcher."""

from __future__ import annotations

import argparse

USAGE_HINT = (
    "logwatcher: work in progress. "
    "Log parsing and detection are not implemented yet. "
    "Run `logwatcher --help` for usage."
)


def main(argv: list[str] | None = None) -> int:
    """Print a short usage hint and exit successfully."""
    parser = argparse.ArgumentParser(
        prog="logwatcher",
        description=(
            "Watch logs and report notable events. "
            "Parsing and detection are not implemented yet."
        ),
    )
    parser.parse_args(argv)
    print(USAGE_HINT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
