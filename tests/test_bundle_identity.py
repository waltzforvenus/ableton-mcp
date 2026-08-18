"""
Guardrail: the bundled Remote Script that `ableton-mcp-install-script`
actually installs into Live must be byte-identical to the canonical
`AbletonMCP_Remote_Script/__init__.py`.

The two drifted once before this fork's history began — the bundled copy
silently reverted the loopback bind and dropped every fork-added tool — and
the CLAUDE.md rule ("never edit the bundled copy by hand; regenerate it") has
been enforced only by vigilance until now. See docs/REFACTOR_PLAN.md §5
(test_bundle_identity row of the guardrail table).

Runs anywhere — no Ableton, no network.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL = REPO_ROOT / "AbletonMCP_Remote_Script" / "__init__.py"
BUNDLED = (
    REPO_ROOT / "MCP_Server" / "bundled_ableton_remote_script" / "AbletonMCP_init.py"
)


def test_bundled_remote_script_is_byte_identical_to_canonical():
    assert CANONICAL.is_file(), f"canonical Remote Script missing: {CANONICAL}"
    assert BUNDLED.is_file(), f"bundled Remote Script missing: {BUNDLED}"
    if CANONICAL.read_bytes() != BUNDLED.read_bytes():
        raise AssertionError(
            "Bundled Remote Script differs from the canonical one. Regenerate "
            "it — never edit it by hand:\n"
            "  cp AbletonMCP_Remote_Script/__init__.py "
            "MCP_Server/bundled_ableton_remote_script/AbletonMCP_init.py\n"
            "(or: uv run ableton-mcp-install-script --sync-bundle)"
        )
