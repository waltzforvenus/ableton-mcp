"""
Guardrails for the contract between the two halves of the project: the MCP
server (the ``ableton_mcp`` package) and the Ableton Remote Script
(`remote_script/__init__.py`).

The Remote Script cannot import the server package inside Live, so nothing
reconciles their shared lists at runtime — they are reconciled here, by test
(docs/REFACTOR_PLAN.md §3.4 and the §5 guardrail table). The lists had
drifted silently once already — `set_arrangement_clip_name` ran on Live's
main thread but got the short non-modifying server timeout, and
`load_browser_item` — the command the three load tools actually send — was
advertised by neither `SCRIPT_CAPABILITIES` nor the legacy set
(docs/REFACTOR_PLAN.md §1 item 4, repaired by plan PR5). These tests keep
the halves from drifting again.

Both halves of "migration adapter #3" (§5) are now complete: plan PR6
replaced the Remote Script's elif ladders with the module-level `COMMANDS`
literal, which the Remote-Script-side parsers here read via AST; plan PR8
replaced the server's embedded modifying/timeout lists with the
`ableton_mcp.commands` registry, which the server side imports directly —
the registry is a plain importable dict, so no AST is needed there. What
stays AST-extracted is the set of wire-command literals the server can send:
`_send("X", ...)` calls in services.py (plan PR9 moved every tool's sends
behind AbletonService._send) plus `send_command("X", ...)` calls in
handshake.py, cross-checked against the registry so a tool cannot send a
command the registry (and therefore the transport's timeout policy) knows
nothing about.

Runs anywhere — no Ableton, no network.
"""

import ast
from functools import lru_cache
from pathlib import Path

from ableton_mcp.commands import COMMANDS
from ableton_mcp.handshake import LEGACY_CAPABILITIES


REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTE_SCRIPT = REPO_ROOT / "remote_script" / "__init__.py"
SERVICES = REPO_ROOT / "src" / "ableton_mcp" / "services.py"
HANDSHAKE = REPO_ROOT / "src" / "ableton_mcp" / "handshake.py"

# The two rack commands are dispatchable on the Remote Script's main thread
# but deliberately have no server-side tool (and so no modifying registry
# row): docs/REFACTOR_PLAN.md §9 defers the expose-or-delete decision. This
# constant pins that state — the equality check below tolerates exactly these
# two, and asserts they are still present RS-side so they can neither be
# dropped nor resurrected server-side without this test noticing.
KNOWN_ORPHANS = frozenset({"map_rack_magnitude", "inspect_rack"})


# --------------------------------------------------------------------------
# Extraction — (a) through (e) of the contract
# --------------------------------------------------------------------------

@lru_cache(maxsize=None)
def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _registry_modifying_commands():
    """(a) the commands.py registry rows flagged as state-modifying."""
    return {name for name, spec in COMMANDS.items() if spec.modifying}


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


def _sent_command_literals(path):
    """Every wire-command first-arg literal in a source file: calls named
    `send_command` (the transport seam — handshake.py calls it as a bare
    injected callable, so both `<x>.send_command(...)` and
    `send_command(...)` shapes count) and `_send` (AbletonService's private
    send, the only way the tool layer reaches the wire since the PR9 MVC
    split)."""
    sent = set()
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            called = node.func.attr
        elif isinstance(node.func, ast.Name):
            called = node.func.id
        else:
            continue
        if (
            called in ("send_command", "_send")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            sent.add(node.args[0].value)
    return sent


def _server_sent_commands():
    """(d) every command the server package can put on the wire: the
    registry's keys plus the AST-extracted wire literals from the service
    layer (services.py) and the handshake (get_script_info)."""
    literals = _sent_command_literals(SERVICES) | _sent_command_literals(HANDSHAKE)
    assert literals, "no send_command call sites found in the server package"
    return set(COMMANDS) | literals


# (e) LEGACY_CAPABILITIES is imported directly — handshake.py is importable
# outside Live.


def _registry_timeout_overrides():
    """The registry rows with an explicit socket-timeout override."""
    return {
        name: spec.timeout
        for name, spec in COMMANDS.items()
        if spec.timeout is not None
    }


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
    server_side = _registry_modifying_commands()
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


def test_every_sent_command_literal_has_a_registry_row():
    # The registry is the transport's timeout policy AND the model's gating
    # data: a tool sending a command with no row would silently get default
    # treatment — the exact half-done-upstream-port failure §5 guards.
    # (AbletonService._send would also KeyError at runtime now, but this
    # test catches the drift without executing anything.)
    literals = _sent_command_literals(SERVICES) | _sent_command_literals(HANDSHAKE)
    missing = literals - set(COMMANDS)
    assert missing == set(), (
        f"send_command literals without a commands.py registry row: "
        f"{sorted(missing)}"
    )


def test_every_sent_command_is_advertised_or_legacy():
    advertised = _script_capabilities() | set(LEGACY_CAPABILITIES)
    missing = _server_sent_commands() - advertised
    assert missing == set(), (
        f"commands the server sends but no capability list advertises: "
        f"{sorted(missing)}"
    )


def test_every_registry_row_is_gated_unless_floor_or_probe():
    # Plan PR10 pins the gated set: every registry row carries gated=True
    # unless its command sits in the LEGACY_CAPABILITIES floor (the floor
    # makes the gate always pass, so gating it is a semantic no-op) or is
    # get_script_info (the handshake's own probe — the gate answers FROM its
    # reply and cannot gate it). A future command added without gated=True
    # fails here instead of silently shipping raw "Unknown command" socket
    # errors to users on old Remote Scripts.
    expected_ungated = (set(LEGACY_CAPABILITIES) | {"get_script_info"}) & set(COMMANDS)
    ungated = {name for name, spec in COMMANDS.items() if not spec.gated}
    assert ungated == expected_ungated, (
        f"ungated rows outside the LEGACY floor (missing gated=True): "
        f"{sorted(ungated - expected_ungated)}; "
        f"floor rows needlessly gated: {sorted(expected_ungated - ungated)}"
    )


def test_min_version_floors_are_exactly_the_repaired_pair():
    # The 1.8.0 min-version floor exists for the two commands whose 1.7.0
    # handlers were broken-but-advertised (plan §4); nothing else needs one.
    floored = {name: spec.min_script_version
               for name, spec in COMMANDS.items()
               if spec.min_script_version is not None}
    assert floored == {
        "get_device_parameters": "1.8.0",
        "set_device_parameter": "1.8.0",
    }


def test_long_running_commands_have_timeout_headroom():
    # The server's socket timeout must outlast the Remote Script's own queue
    # timeout by a margin, or the server gives up while Live is still working
    # and the late reply desyncs the connection.
    server_timeouts = _registry_timeout_overrides()
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
