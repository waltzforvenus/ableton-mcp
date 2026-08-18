"""
Guardrail: the README's tool reference stays in step with the code.

CLAUDE.md convention: "Keep the README's tool reference in step when adding
or removing a tool." The reference lives under the `### Tool reference`
heading as markdown tables whose first column is the backticked tool name
(optionally followed by the fork marker †). Every registered tool must appear
there (docs/REFACTOR_PLAN.md §5 guardrail table).

Runs anywhere — no Ableton, no network.
"""

import asyncio
import re
from pathlib import Path

import ableton_mcp.server as server


REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"

# A table row of the tool reference: | `tool_name` † | args | description |
_TOOL_ROW = re.compile(r"^\|\s*`([A-Za-z0-9_]+)`", re.MULTILINE)


def _tool_reference_section():
    text = README.read_text(encoding="utf-8")
    match = re.search(
        r"^### Tool reference$(.*?)(?=^### )", text, re.MULTILINE | re.DOTALL
    )
    assert match, "README has no `### Tool reference` section"
    return match.group(1)


def _readme_tool_names():
    names = set(_TOOL_ROW.findall(_tool_reference_section()))
    assert names, "no tool-table rows found under `### Tool reference`"
    return names


def test_every_registered_tool_is_in_the_readme_reference():
    registered = {t.name for t in asyncio.run(server.mcp.list_tools())}
    missing = registered - _readme_tool_names()
    assert missing == set(), (
        f"tools registered but missing from the README tool reference: "
        f"{sorted(missing)}"
    )
