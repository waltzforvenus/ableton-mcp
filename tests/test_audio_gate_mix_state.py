"""The audio authorization gate (S1), the snapshot size budget and the
compact get_mix_state (T1). Runs anywhere — no Ableton, no network."""

import json
from types import SimpleNamespace

import pytest

from ableton_mcp import tools
from ableton_mcp.app import Deps, Settings
from ableton_mcp.commands import COMMANDS
from ableton_mcp.services import AbletonService, AudioAuthRequired, _fit_snapshot

from fake_ableton import make_harness
from golden_runner import AllCapabilitiesHandshake


class Client:
    def __init__(self):
        self.sent = []

    def send_command(self, command_type, params=None):
        self.sent.append((command_type, params))
        return {}


def _service(require=True):
    client = Client()
    return AbletonService(client, AllCapabilitiesHandshake(),
                          require_audio_auth=require), client


def _ctx(service, client):
    deps = Deps(client=client, handshake=AllCapabilitiesHandshake(), service=service)
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=deps))


def test_audible_rows_are_exactly_these():
    assert sorted(n for n, s in COMMANDS.items() if s.audible) == [
        "fire_clip", "set_track_arm", "start_playback"]


@pytest.mark.parametrize("call", [
    lambda s: s.start_playback(),
    lambda s: s.fire_clip(0, 0),
    lambda s: s.set_track_arm(0, True),
])
def test_audible_calls_are_refused_before_the_wire(call):
    service, client = _service()
    with pytest.raises(AudioAuthRequired):
        call(service)
    assert client.sent == []


def test_authorized_calls_and_safe_ones_pass():
    service, client = _service()
    service.start_playback(authorized=True)
    service.fire_clip(0, 1, authorized=True)
    service.set_track_arm(0, False)          # disarming is never gated
    service.stop_playback()
    assert [c for c, _ in client.sent] == ["start_playback", "fire_clip",
                                           "set_track_arm", "stop_playback"]


def test_gate_off_restores_the_old_behaviour():
    service, client = _service(require=False)
    service.start_playback()
    assert client.sent == [("start_playback", {})]


def test_tool_refusal_wording_and_authorized_argument():
    service, client = _service()
    text = tools.start_playback(_ctx(service, client))
    assert text.startswith("Refused: this starts playback")
    assert "authorized=true" in text and client.sent == []
    tools.fire_clip(_ctx(service, client), track_index=0, clip_index=0, authorized=True)
    assert client.sent[0][0] == "fire_clip"


def test_settings_default_and_opt_out():
    assert Settings.from_env({}).require_audio_auth is True
    assert Settings.from_env({"ABLETON_MCP_REQUIRE_AUDIO_AUTH": "0"}).require_audio_auth is False


# ── snapshot budget ─────────────────────────────────────────────────────────

def _big_snapshot(n=30):
    return {"schema": "ableton_mcp_snapshot_v3", "session": {"tempo": 120},
            "tracks": [{"index": i, "name": "T%d" % i, "pad": "x" * 500}
                       for i in range(n)],
            "return_tracks": [], "master_track": {}, "scenes": [], "cue_points": []}


def test_fit_keeps_whole_tracks_and_names_the_rest():
    snap = _big_snapshot()
    fitted = _fit_snapshot(snap, 5000)
    assert len(json.dumps(fitted, indent=2)) <= 5000
    kept = [t["index"] for t in fitted["tracks"]]
    assert kept == list(range(len(kept))) and 0 < len(kept) < 30
    assert fitted["truncated"] is True
    assert fitted["continuation"] == {"tracks": list(range(len(kept), 30))}
    assert fitted["omitted_tracks"][0] == {"index": len(kept), "name": "T%d" % len(kept)}


def test_fit_leaves_a_small_snapshot_alone():
    snap = _big_snapshot(2)
    assert _fit_snapshot(snap, 100000) is snap


def test_snapshot_tool_applies_the_default_budget():
    class Big(Client):
        def send_command(self, command_type, params=None):
            self.sent.append((command_type, params))
            return _big_snapshot(200)

    client = Big()
    service = AbletonService(client, AllCapabilitiesHandshake())
    text = tools.get_session_snapshot(_ctx(service, client), include_notes=False,
                                      include_params=False)
    assert '"truncated": true' in text and len(text) < 45000
    unlimited = tools.get_session_snapshot(_ctx(service, client), max_chars=0)
    assert '"truncated"' not in unlimited


# ── get_mix_state (Remote Script half) ──────────────────────────────────────

def test_mix_state_rows_show_displayed_levels():
    harness = make_harness()
    harness.song.tracks[0].solo = True
    resp = harness.process({"type": "get_mix_state", "params": {}})
    assert resp["status"] == "success", resp
    mix = resp["result"]
    assert len(mix["tracks"]) == len(harness.song.tracks)
    row = mix["tracks"][0]
    assert row["solo"] is True and isinstance(row["volume"], str)
    assert {"pan", "mute", "arm", "sends", "output", "devices"} <= set(row)
    assert mix["master"]["track_type"] == "master"
    assert all(r["track_type"] == "return" for r in mix["returns"])
