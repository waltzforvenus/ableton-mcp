"""Unit tests for the min-version capability gate
(docs/REFACTOR_PLAN.md §4), now in its PR8 shape:
``ScriptHandshake.require`` in src/ableton_mcp/handshake.py, raising
``CapabilityError`` whose str() is the same message text the interim
string-returning ``require_capability`` produced.

The gate's contract, in order:

(a) no successful handshake cached -> attempt ONE lazy handshake through the
    caller-provided send_command, swallowing all failures;
(b) still no successful handshake -> return None (the gate PASSES, so the
    actual send fails with the truthful connection error — never a misleading
    "re-run the installer" about a script nobody has seen);
(c) capability name missing (from both the advertised list and the
    LEGACY_CAPABILITIES floor — the floor applies to EVERY script version,
    because historical scripts dispatched commands they never advertised)
    -> raise CapabilityError with the installer message;
(d) min_version set and the script older ("legacy" / lower / unparseable)
    -> raise CapabilityError naming both versions and the command;
(e) otherwise None.

State is seeded through the public API — perform() against a canned
send_command — never by poking module globals (they no longer exist).

The version-matrix section at the bottom runs the gate over the REAL
commands.py registry against the REAL capability lists of each script
generation (1.7.0's from git history, the current one AST-extracted from the
Remote Script), pinning plan PR10's behavior statement: up-to-date users see
no gate, 1.7.0 users are blocked only from the two genuinely-broken
commands, legacy users get installer messages instead of socket errors.

Runs anywhere — no Ableton, no network.
"""

import ast
from pathlib import Path

import pytest

from ableton_mcp.commands import COMMANDS
from ableton_mcp.handshake import (
    LEGACY_CAPABILITIES,
    CapabilityError,
    ScriptHandshake,
)
from ableton_mcp.remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION

# The REAL SCRIPT_CAPABILITIES list of the 1.7.0 Remote Script, verbatim from
# git history (commit 99ab851, the last pre-refactor state:
# `git show 99ab851:AbletonMCP_Remote_Script/__init__.py`). Note what it does
# NOT contain: 1.7.0 *dispatched* load_browser_item, set_tempo,
# set_track_name, set_clip_name, fire_clip, stop_clip, start_playback and
# stop_playback without advertising them — which is why the gate treats
# LEGACY_CAPABILITIES as a floor for every version instead of trusting the
# advertised list alone.
SCRIPT_CAPABILITIES_1_7_0 = [
    "get_session_info",
    "get_track_info",
    "get_script_info",
    "get_clip_notes",
    "get_device_parameters",
    "get_session_snapshot",
    "set_device_parameter",
    "create_midi_track",
    "create_audio_track",
    "create_clip",
    "create_audio_clip",
    "add_notes_to_clip",
    "load_instrument_or_effect",
    "get_arrangement_clips",
    "duplicate_session_clip_to_arrangement",
    "create_locator",
    "delete_clip",
    "clear_notes_from_clip",
    "get_track_routing",
    "delete_track",
    "delete_device",
    "set_track_volume",
    "set_track_pan",
    "set_track_mute",
    "create_return_track",
    "set_track_arm",
    "set_track_monitoring",
    "save_set",
    "set_track_send",
    "set_count_in",
    "back_to_arrangement",
    "set_track_routing",
    "set_clip_gain",
    "set_arrangement_clip_name",
    "switch_to_arrangement_view",
    "set_current_song_time",
    "get_browser_tree",
    "get_browser_items_at_path",
]


def _seeded(script_version, capabilities):
    """A ScriptHandshake that performed a successful handshake against a
    script reporting the given version and capability list."""
    handshake = ScriptHandshake()
    handshake.perform(lambda command_type, params=None: {
        "script_version": script_version,
        "capabilities": capabilities,
    })
    return handshake


def _seeded_legacy():
    """A handshake against a pre-get_script_info script ("Unknown command")."""
    handshake = ScriptHandshake()

    def send_command(command_type, params=None):
        raise RuntimeError("Unknown command: get_script_info")

    handshake.perform(send_command)
    return handshake


def _failing_send(calls):
    """A send_command that records the attempt and fails like an unreachable
    Live would."""
    def send_command(command_type, params=None):
        calls.append(command_type)
        raise RuntimeError("no Ableton in tests")
    return send_command


# --------------------------------------------------------------------------
# (a)+(d) — an old-but-advertising script is caught by the version compare
# --------------------------------------------------------------------------

def test_old_script_version_raises_naming_both_versions():
    # 1.7.0 ADVERTISES set_device_parameter but serves the broken handler,
    # so the name check alone would pass; only min_version catches it.
    handshake = _seeded("1.7.0", ["set_device_parameter"])
    with pytest.raises(CapabilityError) as excinfo:
        handshake.require("set_device_parameter", min_version="1.8.0")
    message = str(excinfo.value)
    assert "1.7.0" in message
    assert "1.8.0" in message
    assert "set_device_parameter" in message
    assert "ableton-mcp-install-script" in message


def test_min_version_message_text_is_frozen():
    # The message IS the tool's response text (gated tools return str(e)
    # verbatim), so it is a frozen contract — byte-identical to the interim
    # require_capability's wording.
    handshake = _seeded("1.7.0", ["set_device_parameter"])
    with pytest.raises(CapabilityError) as excinfo:
        handshake.require("set_device_parameter", min_version="1.8.0")
    assert str(excinfo.value) == (
        "Ableton Remote Script v1.7.0 is older than v1.8.0, "
        "which 'set_device_parameter' requires. "
        "Run `ableton-mcp-install-script` to update the User Remote "
        "Script, then restart Ableton Live."
    )


# --------------------------------------------------------------------------
# (e) — an up-to-date script passes
# --------------------------------------------------------------------------

def test_up_to_date_script_passes():
    handshake = _seeded("1.8.0", ["set_device_parameter"])
    assert handshake.require("set_device_parameter",
                             min_version="1.8.0") is None


def test_newer_script_passes():
    # User ran the installer, then rolled the package back — plan §4 says
    # newer-than-expected proceeds.
    handshake = _seeded("1.9.0", ["set_device_parameter"])
    assert handshake.require("set_device_parameter",
                             min_version="1.8.0") is None


def test_single_arg_call_stays_backward_compatible():
    # The five pre-existing gated tools call with the name alone.
    handshake = _seeded("1.8.0", ["get_clip_notes"])
    assert handshake.require("get_clip_notes") is None


# --------------------------------------------------------------------------
# (a)+(b) — no successful handshake: try once lazily, then PASS
# --------------------------------------------------------------------------

def test_no_cached_info_and_unreachable_live_passes():
    handshake = ScriptHandshake()
    calls = []
    assert handshake.require("set_device_parameter", min_version="1.8.0",
                             send_command=_failing_send(calls)) is None
    # The lazy handshake was attempted (and its failure swallowed).
    assert calls == ["get_script_info"]


def test_no_cached_info_and_no_send_command_passes():
    # Nothing cached and no transport offered: the gate still passes rather
    # than inventing a verdict about a script nobody has seen.
    handshake = ScriptHandshake()
    assert handshake.require("get_clip_notes") is None


def test_failed_startup_handshake_is_retried_then_passes():
    # A cached script_version of None records a handshake that itself failed
    # (e.g. Live unreachable at startup) — treated the same as no cache.
    handshake = ScriptHandshake()
    failed_calls = []
    handshake.perform(_failing_send(failed_calls))  # the failed startup handshake
    assert handshake.info()["script_version"] is None

    calls = []
    assert handshake.require("get_clip_notes",
                             send_command=_failing_send(calls)) is None
    assert calls == ["get_script_info"]


def test_lazy_handshake_success_feeds_the_gate():
    # When Live IS reachable at gate time, the lazy handshake's result is
    # what the gate answers from.
    handshake = ScriptHandshake()

    def send_command(command_type, params=None):
        assert command_type == "get_script_info"
        return {"script_version": "1.7.0",
                "capabilities": ["set_device_parameter"]}

    with pytest.raises(CapabilityError) as excinfo:
        handshake.require("set_device_parameter", min_version="1.8.0",
                          send_command=send_command)
    message = str(excinfo.value)
    assert "1.7.0" in message and "1.8.0" in message


def test_invalidate_forgets_the_cache_and_rehandshakes():
    # invalidate() is what the transport's on_reconnect calls: the next gated
    # call must re-earn its answer instead of trusting the old socket's.
    handshake = _seeded("1.8.0", ["get_clip_notes"])
    assert handshake.require("get_clip_notes") is None
    handshake.invalidate()
    assert handshake.info() is None
    calls = []
    assert handshake.require("get_clip_notes",
                             send_command=_failing_send(calls)) is None
    assert calls == ["get_script_info"]  # the lazy attempt ran again


# --------------------------------------------------------------------------
# (d) — legacy and unparseable versions count as older than min_version
# --------------------------------------------------------------------------

def test_legacy_script_with_min_version_raises():
    # create_clip is in the legacy capability set, so the name check passes
    # and the version compare is what rejects.
    handshake = _seeded_legacy()
    with pytest.raises(CapabilityError) as excinfo:
        handshake.require("create_clip", min_version="1.8.0")
    message = str(excinfo.value)
    assert "legacy" in message
    assert "1.8.0" in message
    assert "create_clip" in message


def test_unparseable_version_counts_as_older():
    handshake = _seeded("garbage", ["set_device_parameter"])
    with pytest.raises(CapabilityError) as excinfo:
        handshake.require("set_device_parameter", min_version="1.8.0")
    message = str(excinfo.value)
    assert "garbage" in message and "1.8.0" in message


# --------------------------------------------------------------------------
# (c) — a missing capability still yields the original installer message
# --------------------------------------------------------------------------

def test_missing_capability_raises():
    handshake = _seeded("1.8.0", ["get_clip_notes"])
    with pytest.raises(CapabilityError) as excinfo:
        handshake.require("set_device_parameter")
    message = str(excinfo.value)
    assert "missing capability 'set_device_parameter'" in message
    assert "ableton-mcp-install-script" in message


def test_missing_capability_message_text_is_frozen():
    handshake = _seeded("1.8.0", ["get_clip_notes"])
    with pytest.raises(CapabilityError) as excinfo:
        handshake.require("set_device_parameter")
    assert str(excinfo.value) == (
        "Ableton Remote Script missing capability 'set_device_parameter' "
        f"(loaded='1.8.0', expected={EXPECTED_REMOTE_SCRIPT_VERSION}). "
        "Run `ableton-mcp-install-script` to update the User Remote Script, "
        "then restart Ableton Live."
    )


# --------------------------------------------------------------------------
# The version matrix over the real registry (plan PR10)
# --------------------------------------------------------------------------

# The gated registry rows, split by whether they carry a version floor.
GATED = {name: spec for name, spec in COMMANDS.items() if spec.gated}
GATED_NO_FLOOR = sorted(n for n, s in GATED.items() if s.min_script_version is None)
GATED_WITH_FLOOR = sorted(n for n, s in GATED.items() if s.min_script_version is not None)

# Floor commands 1.7.0 dispatched WITHOUT advertising — the exact set whose
# existence forces the floor-for-every-version rule.
FLOOR_UNADVERTISED_IN_1_7_0 = sorted(
    LEGACY_CAPABILITIES - set(SCRIPT_CAPABILITIES_1_7_0)
)


def _seeded_1_7_0():
    return _seeded("1.7.0", list(SCRIPT_CAPABILITIES_1_7_0))


def _current_advertised_capabilities():
    """The current Remote Script's SCRIPT_CAPABILITIES, derived the same way
    the script derives it (advertise-flagged COMMANDS rows), read by AST —
    the script imports _Framework and cannot be imported outside Live."""
    remote_script = (Path(__file__).resolve().parent.parent
                     / "AbletonMCP_Remote_Script" / "__init__.py")
    tree = ast.parse(remote_script.read_text(encoding="utf-8"),
                     filename=str(remote_script))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "COMMANDS":
                    table = ast.literal_eval(node.value)
                    return sorted(n for n, row in table.items() if row[3])
    raise AssertionError("COMMANDS table not found in the Remote Script")


def test_the_matrix_is_not_vacuous():
    # The flip happened: the gated surface is the whole registry minus the
    # floor and the probe, so the parametrized suites below cover dozens of
    # commands, not the historical five.
    assert len(GATED_NO_FLOOR) >= 20
    assert GATED_WITH_FLOOR == ["get_device_parameters", "set_device_parameter"]
    assert FLOOR_UNADVERTISED_IN_1_7_0 == [
        "fire_clip", "load_browser_item", "set_clip_name", "set_tempo",
        "set_track_name", "start_playback", "stop_clip", "stop_playback",
    ]


@pytest.mark.parametrize("name", FLOOR_UNADVERTISED_IN_1_7_0)
def test_1_7_0_passes_commands_it_dispatches_but_never_advertised(name):
    # The semantic fix: 1.7.0 serves load_browser_item (every historical
    # script has) yet its SCRIPT_CAPABILITIES never listed it. The floor —
    # applied to every version, not just legacy handshakes — is what keeps
    # the gate from falsely blocking a working install.
    handshake = _seeded_1_7_0()
    assert handshake.require(name) is None


@pytest.mark.parametrize("name", GATED_NO_FLOOR)
def test_1_7_0_passes_every_gated_command_it_advertises(name):
    # Everything the flip newly gated (bar the repaired pair) is advertised
    # by 1.7.0, so a 1.7.0 user keeps every command that worked before.
    handshake = _seeded_1_7_0()
    assert handshake.require(name, min_version=None) is None


@pytest.mark.parametrize("name", GATED_WITH_FLOOR)
def test_1_7_0_is_blocked_only_from_the_repaired_pair(name):
    # 1.7.0 advertises both device-parameter commands but serves the broken
    # duplicate-definition handlers; the version floor is what catches them.
    handshake = _seeded_1_7_0()
    with pytest.raises(CapabilityError) as excinfo:
        handshake.require(name, min_version=GATED[name].min_script_version)
    message = str(excinfo.value)
    assert "1.7.0" in message and "1.8.0" in message
    assert "ableton-mcp-install-script" in message


@pytest.mark.parametrize("name", sorted(GATED))
def test_legacy_script_gets_the_installer_message_for_every_gated_command(name):
    # No gated command sits in the legacy floor, so a pre-get_script_info
    # script is (correctly) refused all of them — with the friendly message
    # a raw "Unknown command" socket error used to occupy.
    handshake = _seeded_legacy()
    with pytest.raises(CapabilityError) as excinfo:
        handshake.require(name, min_version=GATED[name].min_script_version)
    message = str(excinfo.value)
    assert f"missing capability '{name}'" in message
    assert "ableton-mcp-install-script" in message


@pytest.mark.parametrize("name", sorted(LEGACY_CAPABILITIES))
def test_legacy_script_keeps_its_whole_floor(name):
    handshake = _seeded_legacy()
    assert handshake.require(name) is None


def test_no_handshake_and_unreachable_live_passes_a_newly_gated_command():
    # Plan §4's no-handshake rule holds for the widened gated surface: with
    # Live unreachable the gate passes after one lazy attempt, so the send
    # fails with the truthful connection error, not installer advice.
    handshake = ScriptHandshake()
    calls = []
    assert handshake.require("set_track_volume",
                             send_command=_failing_send(calls)) is None
    assert calls == ["get_script_info"]


@pytest.mark.parametrize("name", sorted(GATED))
def test_up_to_date_script_passes_the_entire_gated_registry(name):
    # Seeded with the REAL current advertised list (AST-extracted from the
    # Remote Script), the expected version passes every gated row, version
    # floors included — the flip changes nothing for up-to-date users.
    handshake = _seeded(EXPECTED_REMOTE_SCRIPT_VERSION,
                        _current_advertised_capabilities())
    assert handshake.require(name, min_version=GATED[name].min_script_version) is None
