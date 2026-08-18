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

import ableton_mcp.server as server
from ableton_mcp.app import Deps, build_app
from ableton_mcp.handshake import ScriptHandshake
from fake_ableton import make_harness


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
    return Deps(client=client, handshake=ScriptHandshake()), harness


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
    assert info_result.content[0].text == server.get_session_info(_ctx(direct_deps))
    assert notes_result.content[0].text == server.get_clip_notes(
        _ctx(direct_deps), track_index=0, clip_index=0)

    # get_clip_notes is gated: the text must be the clip payload, not the
    # installer message and not an error string.
    payload = json.loads(notes_result.content[0].text)
    assert payload["clip_name"] == "Lead Riff"
    assert payload["note_count"] == 3


def test_in_memory_session_lists_all_46_tools():
    deps, _harness = _make_deps()
    app = build_app(deps=deps)

    async def run():
        async with create_connected_server_and_client_session(app) as session:
            return await session.list_tools()

    listed = asyncio.run(run())
    assert len(listed.tools) == 46


# --------------------------------------------------------------------------
# (b) End-to-end scenarios against the mock Ableton
# --------------------------------------------------------------------------

def test_scenario_build_a_midi_clip_and_read_it_back():
    """Create a MIDI track -> create a clip -> add notes -> read them back,
    every step through the real tool, the real gate, and the real Remote
    Script handlers mutating the FakeSong."""
    deps, harness = _make_deps()
    ctx = _ctx(deps)

    out = server.create_midi_track(ctx, index=-1)
    assert out == "Created new MIDI track: 4 MIDI"
    track_index = len(harness.song.tracks) - 1

    out = server.create_clip(ctx, track_index=track_index, clip_index=0,
                             length=4.0)
    assert out == (f"Created new clip at track {track_index}, slot 0 "
                   f"with length 4.0 beats")

    notes = [
        {"pitch": 60, "start_time": 0.0, "duration": 0.5, "velocity": 100, "mute": False},
        {"pitch": 64, "start_time": 1.0, "duration": 0.5, "velocity": 90, "mute": False},
        {"pitch": 67, "start_time": 2.0, "duration": 1.0, "velocity": 80, "mute": True},
    ]
    out = server.add_notes_to_clip(ctx, track_index, 0, notes)
    assert out == f"Added 3 notes to clip at track {track_index}, slot 0"

    # Read back through the gated reader (a REAL handshake ran lazily first).
    payload = json.loads(server.get_clip_notes(ctx, track_index, 0))
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
    commands = [command for command, _ in deps.client.sent]
    assert commands.count("get_script_info") == 1
    assert commands.index("get_script_info") == commands.index("get_clip_notes") - 1


def test_scenario_set_device_parameter_by_name_reports_clamping():
    """Out-of-range value by parameter NAME: the real Remote Script clamps
    into the parameter's range and the tool text carries the '(clamped)'
    note — crossing the min-version gate, the registry, the dispatch table,
    and the FakeSong in one call."""
    deps, harness = _make_deps()
    ctx = _ctx(deps)

    out = server.set_device_parameter(ctx, track_index=0, device_index=0,
                                      parameter="Filter Freq", value=9999.0)
    assert out == "Set Operator 'Filter Freq' to 127.00 Hz (clamped)"

    # The fake parameter was really assigned the clamped maximum.
    param = harness.song.tracks[0].devices[0].parameters[1]
    assert param.value == 127.0


def test_scenario_error_text_comes_through_the_real_envelope():
    # A genuine Remote Script error (occupied slot) must surface through the
    # tool with AbletonConnection's wrapped message shape.
    deps, _harness = _make_deps()
    ctx = _ctx(deps)
    out = server.create_clip(ctx, track_index=0, clip_index=0, length=4.0)
    assert out == ("Error creating clip: Communication error with Ableton: "
                   "Clip slot already has a clip")
