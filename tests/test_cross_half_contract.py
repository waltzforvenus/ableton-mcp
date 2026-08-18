"""
Guardrails for the contract between the two halves of the project: the MCP
server (`src/ableton_mcp/server.py`) and the Ableton Remote Script
(`AbletonMCP_Remote_Script/__init__.py`).

The Remote Script cannot import the server package inside Live, so nothing
reconciles their shared lists at runtime — they are reconciled here, by test
(docs/REFACTOR_PLAN.md §3.4 and the §5 guardrail table). The lists had
drifted silently once already — `set_arrangement_clip_name` ran on Live's
main thread but got the short non-modifying server timeout, and
`load_browser_item` — the command the three load tools actually send — was
advertised by neither `SCRIPT_CAPABILITIES` nor the legacy set
(docs/REFACTOR_PLAN.md §1 item 4, repaired by plan PR5). These tests keep
the halves from drifting again.

Plan PR6 replaced the Remote Script's elif ladders with the module-level
`COMMANDS` literal, so the Remote-Script-side parsers here read that table
(the RS half of "migration adapter #3", §5): the main-thread set is the rows
with the main_thread flag, the advertised set is derived from the advertise
flag, and per-command queue timeouts are the rows' queue_timeout column. The
server-side parsers still read server.py's ladder structures until the PR8
commands registry completes the adapter.

Runs anywhere — no Ableton, no network.
"""

import ast
from functools import lru_cache
from pathlib import Path

from ableton_mcp.script_handshake import _LEGACY_CAPABILITIES


REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTE_SCRIPT = REPO_ROOT / "AbletonMCP_Remote_Script" / "__init__.py"
SERVER = REPO_ROOT / "src" / "ableton_mcp" / "server.py"

# The two rack commands are dispatchable on the Remote Script's main thread
# but deliberately have no server-side tool (and so no modifying-list entry):
# docs/REFACTOR_PLAN.md §9 defers the expose-or-delete decision. This constant
# pins that state — the equality check below tolerates exactly these two, and
# asserts they are still present RS-side so they can neither be dropped nor
# resurrected server-side without this test noticing.
KNOWN_ORPHANS = frozenset({"map_rack_magnitude", "inspect_rack"})


# --------------------------------------------------------------------------
# AST extraction — (a) through (e) of the contract
# --------------------------------------------------------------------------

@lru_cache(maxsize=None)
def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _server_modifying_commands():
    """(a) server.py's `is_modifying_command = command_type in [...]` list."""
    for node in ast.walk(_parse(SERVER)):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "is_modifying_command"
                for t in node.targets
            )
            and isinstance(node.value, ast.Compare)
            and isinstance(node.value.comparators[0], (ast.List, ast.Tuple))
        ):
            return [ast.literal_eval(e) for e in node.value.comparators[0].elts]
    raise AssertionError("is_modifying_command membership list not found in server.py")


def _remote_script_commands_table():
    """The Remote Script's module-level COMMANDS literal:
    name -> (method, main_thread, queue_timeout, advertise)."""
    for node in _parse(REMOTE_SCRIPT).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "COMMANDS":
                    return ast.literal_eval(node.value)
    raise AssertionError("COMMANDS table not found in the Remote Script")


def _remote_script_main_thread_commands():
    """(b) the commands the Remote Script schedules onto Live's main thread —
    the COMMANDS rows whose main_thread flag is set."""
    return {
        name
        for name, row in _remote_script_commands_table().items()
        if row[1]
    }


def _script_capabilities():
    """(c) the advertised capability set — SCRIPT_CAPABILITIES as the Remote
    Script derives it, from the COMMANDS rows whose advertise flag is set."""
    return {
        name
        for name, row in _remote_script_commands_table().items()
        if row[3]
    }


def _server_sent_commands():
    """(d) every `<x>.send_command("X", ...)` first-arg literal in server.py."""
    sent = set()
    for node in ast.walk(_parse(SERVER)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "send_command"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            sent.add(node.args[0].value)
    assert sent, "no send_command call sites found in server.py"
    return sent


# (e) _LEGACY_CAPABILITIES is imported directly — script_handshake.py is
# importable outside Live.


def _server_long_running_timeouts():
    """The `long_running_commands = {...}` dict literal in server.py."""
    for node in ast.walk(_parse(SERVER)):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "long_running_commands"
                for t in node.targets
            )
            and isinstance(node.value, ast.Dict)
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("long_running_commands dict not found in server.py")


def _remote_script_queue_timeouts():
    """Per-command queue-timeout overrides from the COMMANDS table — the rows
    whose queue_timeout column is not None (None means the 10.0 default)."""
    return {
        name: row[2]
        for name, row in _remote_script_commands_table().items()
        if row[2] is not None
    }


# --------------------------------------------------------------------------
# The contract tests
# --------------------------------------------------------------------------

def test_modifying_command_sets_agree_across_the_halves():
    server_side = set(_server_modifying_commands())
    script_side = _remote_script_main_thread_commands()
    # The orphans must still be dispatchable RS-side — vanishing here means
    # they were dropped (or exposed) without updating KNOWN_ORPHANS.
    assert KNOWN_ORPHANS <= script_side, (
        f"known orphan commands missing from the Remote Script main-thread "
        f"set: {sorted(KNOWN_ORPHANS - script_side)}"
    )
    assert server_side == script_side - KNOWN_ORPHANS, (
        f"server-only: {sorted(server_side - script_side)}; "
        f"remote-script-only (beyond the known orphans): "
        f"{sorted(script_side - KNOWN_ORPHANS - server_side)}"
    )


def test_every_sent_command_is_advertised_or_legacy():
    advertised = _script_capabilities() | set(_LEGACY_CAPABILITIES)
    missing = _server_sent_commands() - advertised
    assert missing == set(), (
        f"commands the server sends but no capability list advertises: "
        f"{sorted(missing)}"
    )


def test_long_running_commands_have_timeout_headroom():
    # The server's socket timeout must outlast the Remote Script's own queue
    # timeout by a margin, or the server gives up while Live is still working
    # and the late reply desyncs the connection.
    server_timeouts = _server_long_running_timeouts()
    script_timeouts = _remote_script_queue_timeouts()
    assert "create_audio_clip" in script_timeouts
    for command, script_timeout in script_timeouts.items():
        assert command in server_timeouts, (
            f"{command} has a Remote Script queue timeout override but no "
            f"matching server-side socket timeout"
        )
        assert server_timeouts[command] >= script_timeout + 5, (
            f"{command}: server timeout {server_timeouts[command]} s must be "
            f">= Remote Script queue timeout {script_timeout} s + 5 s headroom"
        )
