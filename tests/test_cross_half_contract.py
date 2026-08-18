"""
Guardrails for the contract between the two halves of the project: the MCP
server (`MCP_Server/server.py`) and the Ableton Remote Script
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

These parsers read today's structures (the elif ladders and membership
lists); they are the "migration adapter #3" the plan expects to be rewritten
against the dispatch table (PR6) and the commands registry (PR8).

Runs anywhere — no Ableton, no network.
"""

import ast
from functools import lru_cache
from pathlib import Path

from MCP_Server.script_handshake import _LEGACY_CAPABILITIES


REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTE_SCRIPT = REPO_ROOT / "AbletonMCP_Remote_Script" / "__init__.py"
SERVER = REPO_ROOT / "MCP_Server" / "server.py"

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


def _remote_script_main_thread_commands():
    """(b) the Remote Script's `command_type in [...]` main-thread list."""
    fn = _remote_script_function("_process_command")
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "command_type"
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.In)
            and isinstance(node.comparators[0], (ast.List, ast.Tuple))
        ):
            return [ast.literal_eval(e) for e in node.comparators[0].elts]
    raise AssertionError("main-thread membership list not found in _process_command")


def _script_capabilities():
    """(c) the Remote Script's module-level SCRIPT_CAPABILITIES list."""
    for node in _parse(REMOTE_SCRIPT).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "SCRIPT_CAPABILITIES":
                    return [ast.literal_eval(e) for e in node.value.elts]
    raise AssertionError("SCRIPT_CAPABILITIES not found in the Remote Script")


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


def _remote_script_function(name):
    for node in ast.walk(_parse(REMOTE_SCRIPT)):
        if isinstance(node, ast.ClassDef) and node.name == "AbletonMCP":
            fns = [
                n
                for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == name
            ]
            if fns:
                return fns[-1]  # the definition Python keeps
    raise AssertionError(f"AbletonMCP.{name} not found in the Remote Script")


def _long_running_timeouts(root):
    """The `long_running_commands = {...}` dict literal in the given tree."""
    for node in ast.walk(root):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "long_running_commands"
                for t in node.targets
            )
            and isinstance(node.value, ast.Dict)
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("long_running_commands dict not found")


# --------------------------------------------------------------------------
# The contract tests
# --------------------------------------------------------------------------

def test_modifying_command_sets_agree_across_the_halves():
    server_side = set(_server_modifying_commands())
    script_side = set(_remote_script_main_thread_commands())
    # The orphans must still be dispatchable RS-side — vanishing here means
    # they were dropped (or exposed) without updating KNOWN_ORPHANS.
    assert KNOWN_ORPHANS <= script_side, (
        f"known orphan commands missing from the Remote Script main-thread "
        f"list: {sorted(KNOWN_ORPHANS - script_side)}"
    )
    assert server_side == script_side - KNOWN_ORPHANS, (
        f"server-only: {sorted(server_side - script_side)}; "
        f"remote-script-only (beyond the known orphans): "
        f"{sorted(script_side - KNOWN_ORPHANS - server_side)}"
    )


def test_every_sent_command_is_advertised_or_legacy():
    advertised = set(_script_capabilities()) | set(_LEGACY_CAPABILITIES)
    missing = _server_sent_commands() - advertised
    assert missing == set(), (
        f"commands the server sends but no capability list advertises: "
        f"{sorted(missing)}"
    )


def test_long_running_commands_have_timeout_headroom():
    # The server's socket timeout must outlast the Remote Script's own queue
    # timeout by a margin, or the server gives up while Live is still working
    # and the late reply desyncs the connection.
    server_timeouts = _long_running_timeouts(
        _parse(SERVER)
    )
    script_timeouts = _long_running_timeouts(
        _remote_script_function("_process_command")
    )
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
