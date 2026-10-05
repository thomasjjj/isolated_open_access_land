"""Compatibility entry point; use `uv run access-islands --help`."""

from access_islands.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
