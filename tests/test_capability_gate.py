"""Unit tests for the interim min-version capability gate
(docs/REFACTOR_PLAN.md §4, added by plan PR5 in
src/ableton_mcp/script_handshake.py; plan PR8 later absorbs it into
ScriptHandshake.require).

The gate's contract, in order:

(a) no successful handshake cached -> attempt ONE lazy handshake, swallowing
    all exceptions;
(b) still no successful handshake -> return None (the gate PASSES, so the
    actual send fails with the truthful connection error — never a misleading
    "re-run the installer" about a script nobody has seen);
(c) capability name missing -> the installer message;
(d) min_version set and the script older ("legacy" / lower / unparseable)
    -> an installer message naming both versions and the command;
(e) otherwise None.

Module state (_script_info/_handshake_done) is manipulated directly under
_lock with save/restore, the same way tests/golden_runner.py does.

Runs anywhere — no Ableton, no network.
"""

import pytest

import ableton_mcp.script_handshake as sh
import ableton_mcp.server as server
from ableton_mcp.remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION


def _info(script_version, capabilities):
    return {
        "script_version": script_version,
        "capabilities": capabilities,
        "expected_version": EXPECTED_REMOTE_SCRIPT_VERSION,
        "up_to_date": script_version == EXPECTED_REMOTE_SCRIPT_VERSION,
        "error": None,
    }


@pytest.fixture(autouse=True)
def no_real_connection(monkeypatch):
    """Fail any lazy-handshake connection attempt, and record that it was
    tried. Keeps every test off the network and lets the no-handshake tests
    prove the lazy attempt actually happened."""
    calls = []

    def _raise():
        calls.append(1)
        raise RuntimeError("no Ableton in tests")

    monkeypatch.setattr(server, "get_ableton_connection", _raise)
    return calls


@pytest.fixture
def set_script_info():
    """Set the cached handshake state directly; restore the original after."""
    with sh._lock:
        orig_info = sh._script_info
        orig_done = sh._handshake_done

    def _set(info):
        with sh._lock:
            sh._script_info = info
            sh._handshake_done = info is not None

    yield _set

    with sh._lock:
        sh._script_info = orig_info
        sh._handshake_done = orig_done


# --------------------------------------------------------------------------
# (a)+(d) — an old-but-advertising script is caught by the version compare
# --------------------------------------------------------------------------

def test_old_script_version_yields_message_naming_both_versions(set_script_info):
    # 1.7.0 ADVERTISES set_device_parameter but serves the broken handler,
    # so the name check alone would pass; only min_version catches it.
    set_script_info(_info("1.7.0", ["set_device_parameter"]))
    message = sh.require_capability("set_device_parameter", min_version="1.8.0")
    assert message is not None
    assert "1.7.0" in message
    assert "1.8.0" in message
    assert "set_device_parameter" in message
    assert "ableton-mcp-install-script" in message


# --------------------------------------------------------------------------
# (e)/(b of the ordering) — an up-to-date script passes
# --------------------------------------------------------------------------

def test_up_to_date_script_passes(set_script_info):
    set_script_info(_info("1.8.0", ["set_device_parameter"]))
    assert sh.require_capability("set_device_parameter",
                                 min_version="1.8.0") is None


def test_newer_script_passes(set_script_info):
    # User ran the installer, then rolled the package back — plan §4 says
    # newer-than-expected proceeds.
    set_script_info(_info("1.9.0", ["set_device_parameter"]))
    assert sh.require_capability("set_device_parameter",
                                 min_version="1.8.0") is None


def test_single_arg_call_stays_backward_compatible(set_script_info):
    # The five pre-existing gated tools call with the name alone.
    set_script_info(_info("1.8.0", ["get_clip_notes"]))
    assert sh.require_capability("get_clip_notes") is None


# --------------------------------------------------------------------------
# (a)+(b) — no successful handshake: try once lazily, then PASS
# --------------------------------------------------------------------------

def test_no_cached_info_and_unreachable_live_passes(set_script_info,
                                                    no_real_connection):
    set_script_info(None)
    assert sh.require_capability("set_device_parameter",
                                 min_version="1.8.0") is None
    # The lazy handshake was attempted (and its failure swallowed).
    assert no_real_connection


def test_failed_startup_handshake_is_retried_then_passes(set_script_info,
                                                         no_real_connection):
    # A cached script_version of None records a handshake that itself failed
    # (e.g. Live unreachable at startup) — treated the same as no cache.
    set_script_info(_info(None, []))
    assert sh.require_capability("get_clip_notes") is None
    assert no_real_connection


def test_lazy_handshake_success_feeds_the_gate(set_script_info, monkeypatch):
    # When Live IS reachable at gate time, the lazy handshake's result is
    # what the gate answers from.
    set_script_info(None)

    class _Conn:
        def send_command(self, command_type, params=None):
            assert command_type == "get_script_info"
            return {"script_version": "1.7.0",
                    "capabilities": ["set_device_parameter"]}

    monkeypatch.setattr(server, "get_ableton_connection", lambda: _Conn())
    message = sh.require_capability("set_device_parameter", min_version="1.8.0")
    assert message is not None
    assert "1.7.0" in message and "1.8.0" in message


# --------------------------------------------------------------------------
# (d) — legacy and unparseable versions count as older than min_version
# --------------------------------------------------------------------------

def test_legacy_script_with_min_version_yields_message(set_script_info):
    # create_clip is in the legacy capability set, so the name check passes
    # and the version compare is what rejects.
    set_script_info(_info("legacy", []))
    message = sh.require_capability("create_clip", min_version="1.8.0")
    assert message is not None
    assert "legacy" in message
    assert "1.8.0" in message
    assert "create_clip" in message


def test_unparseable_version_counts_as_older(set_script_info):
    set_script_info(_info("garbage", ["set_device_parameter"]))
    message = sh.require_capability("set_device_parameter", min_version="1.8.0")
    assert message is not None
    assert "garbage" in message and "1.8.0" in message


# --------------------------------------------------------------------------
# (c) — a missing capability still yields the original installer message
# --------------------------------------------------------------------------

def test_missing_capability_yields_message(set_script_info):
    set_script_info(_info("1.8.0", ["get_clip_notes"]))
    message = sh.require_capability("set_device_parameter")
    assert message is not None
    assert "missing capability 'set_device_parameter'" in message
    assert "ableton-mcp-install-script" in message
