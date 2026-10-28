"""Level-4 full-stack tests (docs/REFACTOR_PLAN.md §5) — still no Live.

Two compositions close the loop:

(a) a REAL FastMCP session over mcp.shared.memory: build_app(deps=fakes),
    connect an in-memory client, and call tools through genuine MCP dispatch
    — proving tool registration, the lifespan yielding Deps, and the
    ctx.request_context.lifespan_context plumbing all survive SDK behavior
    (the one failure class direct function calls cannot see);

(b) end-to-end against the mock Ableton: the Deps client is a shim
    (migration adapter #2) that feeds every command dict straight into the
    real Remote Script's _process_command via the fake_ableton harness —
    MCP tool -> handshake gate -> real dispatch -> real handlers -> FakeSong,
    with not a single component mocked except Live itself.

Runs anywhere: no Ableton, no network (the in-memory transport is exactly
that — memory).
"""

import asyncio
import json
from types import SimpleNamespace

from mcp.shared.memory import create_connected_server_and_client_session

import ableton_mcp.tools as tools
from ableton_mcp.app import Deps, build_app
from ableton_mcp.commands import COMMANDS
from ableton_mcp.handshake import ScriptHandshake
from ableton_mcp.services import AbletonService
from fake_ableton import FakePluginDevice, make_harness


class FakeAbletonClient:
    """Migration adapter #2: send_command -> the real Remote Script dispatch.

    Feeds the command dict into RemoteScriptHarness.process() and unwraps the
    response envelope exactly as AbletonConnection does — an error envelope
    raises with AbletonConnection's wrapped message shape (its envelope raise
    is re-caught by its own generic handler), so tool error text is identical
    to a real socket exchange.
    """

    def __init__(self, harness):
        self.harness = harness
        self.sent = []  # (command_type, params) pairs, in order

    def send_command(self, command_type, params=None):
        self.sent.append((command_type, params or {}))
        response = self.harness.process({"type": command_type,
                                         "params": params or {}})
        if response.get("status") == "error":
            message = response.get("message", "Unknown error from Ableton")
            raise Exception(f"Communication error with Ableton: {message}")
        return response.get("result", {})


def _make_deps():
    """A full fake-Ableton world: shim client + a REAL ScriptHandshake (the
    gate lazily handshakes against the real Remote Script's get_script_info
    handler, exactly as production would)."""
    harness = make_harness()
    client = FakeAbletonClient(harness)
    handshake = ScriptHandshake()
    return Deps(client=client, handshake=handshake,
                service=AbletonService(client, handshake)), harness


def _ctx(deps):
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=deps))


# --------------------------------------------------------------------------
# (a) Real FastMCP dispatch over the in-memory transport
# --------------------------------------------------------------------------

def test_in_memory_session_matches_direct_calls():
    deps, _harness = _make_deps()
    app = build_app(deps=deps)

    async def run():
        async with create_connected_server_and_client_session(app) as session:
            info = await session.call_tool("get_session_info", {})
            notes = await session.call_tool(
                "get_clip_notes", {"track_index": 0, "clip_index": 0})
            return info, notes

    info_result, notes_result = asyncio.run(run())
    assert not info_result.isError
    assert not notes_result.isError

    # The exact text a direct call produces against an identical world —
    # byte-equality here proves the lifespan yielded Deps and FastMCP handed
    # them to the sync tool through ctx.request_context.lifespan_context.
    direct_deps, _ = _make_deps()
    assert info_result.content[0].text == tools.get_session_info(_ctx(direct_deps))
    assert notes_result.content[0].text == tools.get_clip_notes(
        _ctx(direct_deps), track_index=0, clip_index=0)

    # get_clip_notes is gated: the text must be the clip payload, not the
    # installer message and not an error string.
    payload = json.loads(notes_result.content[0].text)
    assert payload["clip_name"] == "Lead Riff"
    assert payload["note_count"] == 3


def test_in_memory_session_lists_all_62_tools():
    deps, _harness = _make_deps()
    app = build_app(deps=deps)

    async def run():
        async with create_connected_server_and_client_session(app) as session:
            return await session.list_tools()

    listed = asyncio.run(run())
    assert len(listed.tools) == 62


# --------------------------------------------------------------------------
# (b) End-to-end scenarios against the mock Ableton
# --------------------------------------------------------------------------

def test_scenario_build_a_midi_clip_and_read_it_back():
    """Create a MIDI track -> create a clip -> add notes -> read them back,
    every step through the real tool, the real gate, and the real Remote
    Script handlers mutating the FakeSong."""
    deps, harness = _make_deps()
    ctx = _ctx(deps)

    out = tools.create_midi_track(ctx, index=-1)
    assert out == "Created new MIDI track: 4 MIDI"
    track_index = len(harness.song.tracks) - 1

    out = tools.create_clip(ctx, track_index=track_index, clip_index=0,
                             length=4.0)
    assert out == (f"Created new clip at track {track_index}, slot 0 "
                   f"with length 4.0 beats")

    notes = [
        {"pitch": 60, "start_time": 0.0, "duration": 0.5, "velocity": 100, "mute": False},
        {"pitch": 64, "start_time": 1.0, "duration": 0.5, "velocity": 90, "mute": False},
        {"pitch": 67, "start_time": 2.0, "duration": 1.0, "velocity": 80, "mute": True},
    ]
    out = tools.add_notes_to_clip(ctx, track_index, 0, notes)
    # Counted by the handler before and after the write, not echoed back
    # from the caller's own list — and the clip Live just created has no
    # name yet, which the presenter says rather than quoting an empty one.
    assert out == (f"Added 3 notes to the clip at track {track_index}, "
                   f"slot 0; the clip now holds 3")

    # Read back through the gated reader (a REAL handshake ran lazily first).
    payload = json.loads(tools.get_clip_notes(ctx, track_index, 0))
    assert payload["note_count"] == 3
    read_back = [
        {key: note[key]
         for key in ("pitch", "start_time", "duration", "velocity", "mute")}
        for note in payload["notes"]
    ]
    assert read_back == notes

    # The FakeSong really holds the clip — nothing was mocked in between.
    slot = harness.song.tracks[track_index].clip_slots[0]
    assert slot.has_clip and slot.clip.length == 4.0

    # The gate's lazy handshake is visible on the wire exactly once,
    # immediately before the first gated send (nothing earlier needed it).
    # Which send that is is derived from the registry rather than named:
    # add_notes_to_clip took the position from get_clip_notes in 1.15.0 when
    # it gained a version floor, and pinning the tool name here would make
    # this test fail for a gating change it is not about.
    commands = [command for command, _ in deps.client.sent]
    assert commands.count("get_script_info") == 1
    first_gated = next(i for i, c in enumerate(commands)
                       if c != "get_script_info" and COMMANDS[c].gated)
    assert commands.index("get_script_info") == first_gated - 1
    # ...and it really is a gate-triggered send, not the first command overall.
    assert first_gated > 0


def test_scenario_set_device_parameter_by_name_reports_clamping():
    """Out-of-range value by parameter NAME: the real Remote Script clamps
    into the parameter's range and the tool text carries the '(clamped)'
    note — crossing the min-version gate, the registry, the dispatch table,
    and the FakeSong in one call."""
    deps, harness = _make_deps()
    ctx = _ctx(deps)

    out = tools.set_device_parameter(ctx, track_index=0, device_index=0,
                                      parameter="Filter Freq", value=9999.0)
    assert out == "Set Operator 'Filter Freq' to 127.00 Hz (clamped)"

    # The fake parameter was really assigned the clamped maximum.
    param = harness.song.tracks[0].devices[0].parameters[1]
    assert param.value == 127.0


def test_scenario_select_a_plugin_preset_by_name():
    """The plugin-preset pair end to end. The fake hosts a VST3 whose only
    parameter is "Device On" — exactly what Live shows for an unconfigured
    TAL-NoiseMaker — so the preset list is the whole interface:
    get_plugin_presets lists the bank and set_plugin_preset by an
    unambiguous fragment lands and verifies, through the real gate, the
    real dispatch and the real handlers."""
    deps, harness = _make_deps()
    ctx = _ctx(deps)
    track = harness.song.tracks[0]
    track.devices.append(FakePluginDevice(
        "TAL-NoiseMaker", presets=["Init", "Warm Pad", "Bass Growl"]))

    listed = json.loads(tools.get_plugin_presets(ctx, track_index=0,
                                                 device_index=1))
    assert listed["is_plugin"] is True
    assert listed["presets"] == ["Init", "Warm Pad", "Bass Growl"]
    assert listed["selected_preset_index"] == 0
    # The parameter surface really is just the switch.
    params = json.loads(tools.get_device_parameters(ctx, track_index=0,
                                                    device_index=1))
    assert [p["name"] for p in params["parameters"]] == ["Device On"]

    out = tools.set_plugin_preset(ctx, track_index=0, device_index=1,
                                  preset_name="growl")
    assert out == ("Selected preset 2 'Bass Growl' on TAL-NoiseMaker "
                   "(track 0 'Lead'); was 0 'Init'")
    assert track.devices[1].selected_preset_index == 2

    # A refusal comes through the real envelope with the script's wording.
    out = tools.set_plugin_preset(ctx, track_index=0, device_index=0,
                                  preset_index=0)
    assert out.startswith(
        "Error setting plugin preset: Communication error with Ableton: "
        "Device 0 on track 0 is 'Operator' (Operator), not a hosted "
        "VST/VST3/AU plugin")


def test_scenario_audition_a_part_with_solo():
    """set_track_solo end to end: solos accumulate and the reply says so,
    unsolo clears the picture, and the master is refused before the wire."""
    deps, harness = _make_deps()
    ctx = _ctx(deps)

    out = tools.set_track_solo(ctx, track_index=0, solo=True)
    assert out == ("Track 0 ('Lead') soloed. Soloed now: Lead (Exclusive "
                   "Solo is off in Live's preferences, so solos add up)")
    out = tools.set_track_solo(ctx, track_index=0, solo=True,
                               track_type="return")
    assert out.startswith("Return track 0 ('A Reverb') soloed. Soloed now: "
                          "Lead, A Reverb")
    assert harness.song.return_tracks[0].solo is True

    tools.set_track_solo(ctx, track_index=0, solo=False, track_type="return")
    out = tools.set_track_solo(ctx, track_index=0, solo=False,
                               expect_track_name="Lead")
    assert out.startswith("Track 0 ('Lead') unsoloed. Nothing is soloed now")
    assert [t.solo for t in harness.song.tracks] == [False, False, False]

    sent_before = len(deps.client.sent)
    out = tools.set_track_solo(ctx, track_index=0, solo=True,
                               track_type="master")
    assert out.startswith("Error setting track solo: The master track cannot "
                          "be soloed in Live")
    assert len(deps.client.sent) == sent_before


def test_scenario_error_text_comes_through_the_real_envelope():
    # A genuine Remote Script error (occupied slot) must surface through the
    # tool with AbletonConnection's wrapped message shape.
    deps, _harness = _make_deps()
    ctx = _ctx(deps)
    out = tools.create_clip(ctx, track_index=0, clip_index=0, length=4.0)
    assert out == ("Error creating clip: Communication error with Ableton: "
                   "Clip slot already has a clip")
