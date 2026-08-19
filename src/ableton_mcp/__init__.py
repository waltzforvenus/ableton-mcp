"""Ableton Live integration through the Model Context Protocol."""

from importlib.metadata import PackageNotFoundError, version

# Single-sourced from pyproject.toml via the installed distribution's
# metadata, so this can never drift from the published version again. The
# fallback covers running from a checkout that was never `uv sync`ed /
# pip-installed — the only case where the distribution is unknown.
try:
    __version__ = version("ableton-mcp")
except PackageNotFoundError:  # pragma: no cover — not installed
    __version__ = "0.0.0.dev0"
