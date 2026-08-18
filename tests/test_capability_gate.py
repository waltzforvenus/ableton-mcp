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
(c) capability name missing -> raise CapabilityError with the installer
    message;
(d) min_version set and the script older ("legacy" / lower / unparseable)
    -> raise CapabilityError naming both versions and the command;
(e) otherwise None.

State is seeded through the public API — perform() against a canned
send_command — never by poking module globals (they no longer exist).

Runs anywhere — no Ableton, no network.
"""

import pytest

from ableton_mcp.handshake import CapabilityError, ScriptHandshake
from ableton_mcp.remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION


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
