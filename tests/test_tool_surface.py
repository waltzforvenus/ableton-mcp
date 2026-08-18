"""
Guardrail: the registered MCP tool surface is exactly the checked-in snapshot.

FastMCP registers tools by name and later definitions win silently — two
`@mcp.tool()` functions with the same name means one vanishes with no error
(the hazard CLAUDE.md documents). Pinning `list_tools()` against
tests/data/tool_names.txt catches silent tool loss, rename, and duplication
in one assertion (docs/REFACTOR_PLAN.md §5 guardrail table).

Docstrings are read by the model at runtime, so they are part of the
interface — every tool must carry a non-empty description.

The surface is read from the app the composition root actually builds
(``build_app()``), so a tool that appends to server.TOOLS but fails FastMCP
registration cannot slip past.

Runs anywhere — no Ableton, no network (build_app wires nothing until its
lifespan runs, and list_tools never enters the lifespan).
"""

import asyncio
from pathlib import Path

from ableton_mcp.app import build_app


REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = REPO_ROOT / "tests" / "data" / "tool_names.txt"
EXPECTED_TOOL_COUNT = 46


def _registered_tools():
    return asyncio.run(build_app().list_tools())


def _snapshot_names():
    names = [
        line.strip()
        for line in SNAPSHOT.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert names == sorted(names), "snapshot must stay sorted, one name per line"
    return names


def test_tool_surface_matches_snapshot():
    names = [t.name for t in _registered_tools()]
    assert len(names) == len(set(names)) == EXPECTED_TOOL_COUNT, (
        f"expected {EXPECTED_TOOL_COUNT} unique tools, got {len(names)} "
        f"({len(set(names))} unique) — a duplicate name silently drops a tool"
    )
    assert sorted(names) == _snapshot_names(), (
        "registered tools differ from tests/data/tool_names.txt — tool names "
        "are a frozen contract (docs/REFACTOR_PLAN.md §2); if this change is "
        "deliberate, update the snapshot (and the README) in the same commit"
    )


def test_every_tool_has_a_description():
    undescribed = [
        t.name for t in _registered_tools() if not (t.description or "").strip()
    ]
    assert undescribed == [], (
        f"tools without a docstring/description (the model-facing interface): "
        f"{undescribed}"
    )
