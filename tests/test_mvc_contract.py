"""The three MVC port-guardrails (docs/REFACTOR_PLAN.md §5), landed with the
plan-PR9 tools/services/presenters split. They keep a half-done upstream port
from compiling quietly:

(a) every wire-command literal the service layer sends has a commands.py
    registry row — a command without a row would KeyError in
    ``AbletonService._send``, and this catches it without executing anything
    (plus: the controllers never touch the wire themselves);
(b) every capability the Remote Script advertises — minus the known orphans
    and the one deliberately never-sent command — is reachable from the
    service layer. This catches "merged their Remote Script, forgot our
    server side", the likeliest miss, since the Remote Script half merges
    automatically;
(c) every registered tool has a presenter and an error wording (an
    ``ERROR_PHRASES`` entry or a bespoke ``ERROR_RENDERERS`` entry), so no
    tool can fall back to an unowned string.

Runs anywhere — no Ableton, no network.
"""

import ast
from pathlib import Path

from ableton_mcp import presenters
from ableton_mcp.commands import COMMANDS
from ableton_mcp.tools import TOOLS


REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES = REPO_ROOT / "src" / "ableton_mcp" / "services.py"
TOOLS_SOURCE = REPO_ROOT / "src" / "ableton_mcp" / "tools.py"
HANDSHAKE = REPO_ROOT / "src" / "ableton_mcp" / "handshake.py"
REMOTE_SCRIPT = REPO_ROOT / "remote_script" / "__init__.py"

EXPECTED_TOOL_COUNT = 46

# Dispatchable but deliberately tool-less (docs/REFACTOR_PLAN.md §9 defers
# the expose-or-delete decision); mirrors test_cross_half_contract's pin.
KNOWN_ORPHANS = frozenset({"map_rack_magnitude", "inspect_rack"})

# Advertised and dispatchable, but never sent by our tools: the tool of that
# name has always sent load_browser_item instead. The wire command stays for
# third-party MCP clients driving the Remote Script directly (plan §9), so
# its absence from the service layer is deliberate, not a porting gap.
NEVER_SENT = frozenset({"load_instrument_or_effect"})


def _call_name(node):
    """The called name for `<x>.<name>(...)` and bare `<name>(...)` calls —
    handshake.py sends through an injected callable (`send_command(...)`,
    a plain Name), while services.py sends through `self._send(...)`."""
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _wire_call_sites(path, names):
    """Every Call node whose called name is one of ``names``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node) in names
    ]


def _wire_call_literals(path, attrs):
    """The string literals those call sites pass as their first argument."""
    return {
        node.args[0].value
        for node in _wire_call_sites(path, attrs)
        if node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }


def _service_send_literals():
    literals = _wire_call_literals(SERVICES, {"_send"})
    assert literals, "no _send call sites found in services.py"
    return literals


def _advertised_capabilities():
    """SCRIPT_CAPABILITIES as the Remote Script derives it: the COMMANDS rows
    whose advertise flag is set. Read by AST — the script imports _Framework
    and cannot be imported outside Live."""
    tree = ast.parse(REMOTE_SCRIPT.read_text(encoding="utf-8"),
                     filename=str(REMOTE_SCRIPT))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "COMMANDS":
                    table = ast.literal_eval(node.value)
                    return {name for name, row in table.items() if row[3]}
    raise AssertionError("COMMANDS table not found in the Remote Script")


# --------------------------------------------------------------------------
# (a) services ↔ registry
# --------------------------------------------------------------------------

def test_every_service_send_literal_has_a_registry_row():
    missing = _service_send_literals() - set(COMMANDS)
    assert missing == set(), (
        f"services.py sends commands with no commands.py registry row "
        f"(no timeout policy, no gating data): {sorted(missing)}"
    )


def test_controllers_never_touch_the_wire():
    # The controller layer delegates; only AbletonService (and the handshake)
    # may send. A send_command or _send call in tools.py means a tool body
    # bypassed the Model — and with it the registry's gate.
    offenders = [
        f"tools.py:{node.lineno}: {_call_name(node)}(...)"
        for node in _wire_call_sites(TOOLS_SOURCE, {"send_command", "_send"})
    ]
    assert offenders == [], (
        "controllers must not send wire commands directly:\n"
        + "\n".join(offenders)
    )


# --------------------------------------------------------------------------
# (b) capabilities ↔ services
# --------------------------------------------------------------------------

def test_every_advertised_capability_is_reachable_from_the_service_layer():
    # get_script_info is sent by ScriptHandshake.perform — which both the
    # service's gate and its get_remote_script_info orchestration drive — so
    # handshake.py's send_command literal counts as reachable too.
    reachable = (_service_send_literals()
                 | _wire_call_literals(HANDSHAKE, {"send_command"}))
    unreachable = (_advertised_capabilities()
                   - KNOWN_ORPHANS - NEVER_SENT - reachable)
    assert unreachable == set(), (
        f"Remote Script capabilities no service method can send — a merged "
        f"script half whose server half was never ported: "
        f"{sorted(unreachable)}"
    )


# --------------------------------------------------------------------------
# (c) tools ↔ presenters/error wording
# --------------------------------------------------------------------------

def test_every_tool_has_error_wording_and_a_presenter():
    names = [fn.__name__ for fn in TOOLS]
    assert len(names) == len(set(names)) == EXPECTED_TOOL_COUNT

    unworded = [
        name for name in names
        if name not in presenters.ERROR_PHRASES
        and name not in presenters.ERROR_RENDERERS
    ]
    assert unworded == [], (
        f"tools with neither an ERROR_PHRASES entry nor an ERROR_RENDERERS "
        f"entry — their failures would have no owned wording: {unworded}"
    )

    # Presenter mapping is by convention: the renderer is the presenters
    # attribute named exactly after the tool (the JSON tools are aliases of
    # the shared as_json).
    unrendered = [
        name for name in names
        if not callable(getattr(presenters, name, None))
    ]
    assert unrendered == [], (
        f"tools without a presenters.<tool_name> renderer: {unrendered}"
    )


def test_error_wording_tables_are_exact():
    # No stale rows (a renamed tool must not leave its wording behind), and
    # no tool claiming both the uniform phrase and a bespoke renderer —
    # error_text would silently prefer the renderer.
    names = {fn.__name__ for fn in TOOLS}
    stale = (set(presenters.ERROR_PHRASES)
             | set(presenters.ERROR_RENDERERS)) - names
    assert stale == set(), f"error wording for nonexistent tools: {sorted(stale)}"
    overlap = set(presenters.ERROR_PHRASES) & set(presenters.ERROR_RENDERERS)
    assert overlap == set(), (
        f"tools with both a phrase and a bespoke renderer: {sorted(overlap)}"
    )
