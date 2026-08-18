"""
Guardrail: every console script of the installed ``ableton-mcp`` distribution
must actually resolve. The `ableton-mcp` entry point was dead from the
telemetry-removal commit e57c257 (which swept `main()` out of the end of
`MCP_Server/server.py`, docs/REFACTOR_PLAN.md §1 item 2) until plan PR2
restored it; this guardrail keeps every declared entry point loadable.

Runs anywhere — no Ableton, no network.
"""

import importlib.metadata

import pytest


DISTRIBUTION = "ableton-mcp"

# Frozen contract (docs/REFACTOR_PLAN.md §2): these NAMES are referenced
# verbatim by users' MCP client configs, README, Docker, and smithery.
EXPECTED_CONSOLE_SCRIPTS = {"ableton-mcp", "ableton-mcp-install-script"}


def _console_scripts():
    """The distribution's console-script entry points, by name."""
    dist = importlib.metadata.distribution(DISTRIBUTION)
    return {ep.name: ep for ep in dist.entry_points if ep.group == "console_scripts"}


def test_console_script_names_are_the_frozen_contract():
    assert set(_console_scripts()) == EXPECTED_CONSOLE_SCRIPTS


def test_ableton_mcp_entry_point_loads():
    target = _console_scripts()["ableton-mcp"].load()
    assert callable(target)


def test_install_script_entry_point_loads():
    target = _console_scripts()["ableton-mcp-install-script"].load()
    assert callable(target)
