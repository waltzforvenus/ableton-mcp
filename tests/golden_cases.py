"""Golden-case definitions for all 55 MCP tools (docs/REFACTOR_PLAN.md
section 5 Level 1, section 6 PR3).

Each case names a tool, the arguments to call it with, and the exact ordered
wire exchange the tool is expected to perform (command + params asserted,
response canned). Canned responses are chosen to exercise the tool's
formatting branches. ``expect`` strings are NOT defined here — they are
recorded from the current code by tests/record_goldens.py into
tests/goldens/<tool>.json, and replayed byte-for-byte by
tests/test_goldens.py.

On top of the branch cases below, one "error" case per tool is generated
automatically: the tool is called with its first case's args, the first wire
send raises RuntimeError("boom"), and the recorded output freezes the tool's
error string. (get_remote_script_info is the one tool whose handshake
swallows the send error and returns JSON with the error embedded — the
golden records that real behavior.)

A case may also carry an optional ``script_info`` seed (plan PR10's
gated-path cases): the runner then builds a REAL ``ScriptHandshake`` cached
with exactly that get_script_info reply instead of the all-capabilities
stub, so the registry gate itself runs and its verdict — installer message
or pass — is what the golden freezes. Gate-blocked cases script an EMPTY
wire exchange: the friendly message exists precisely so nothing touches the
wire.

Pure data, with two exceptions: EXPECTED_REMOTE_SCRIPT_VERSION is imported
so the up-to-date handshake case tracks the package's expected script
version instead of hardcoding it, and SCRIPT_CAPABILITIES_1_7_0 — the real
1.7.0 advertised list, extracted from git history — comes from
test_capability_gate so the two suites cannot drift apart on what 1.7.0
actually advertised. (The editable install and the tests dir on sys.path
resolve both imports under pytest and when record_goldens.py runs as a
script.)
"""

from ableton_mcp.remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION
from test_capability_gate import SCRIPT_CAPABILITIES_1_7_0


def _case(tool, name, args, wire, script_info=None):
    case = {"tool": tool, "name": name, "args": args, "wire": wire}
    if script_info is not None:
        case["script_info"] = script_info
    return case


def _ok(command, params, response):
    return {"command": command, "params": params, "response": response}


def _boom(command, params, message="boom"):
    return {"command": command, "params": params, "raise": message}


# A small note payload shaped as the Remote Script returns / accepts it.
NOTES = [
    {"pitch": 60, "start_time": 0.0, "duration": 0.5, "velocity": 100, "mute": False},
    {"pitch": 64, "start_time": 1.0, "duration": 0.25, "velocity": 90, "mute": True},
]


BASE_CASES = [
    # ── get_session_info ──────────────────────────────────────────────────
    _case("get_session_info", "success", {}, [
        _ok("get_session_info", {}, {
            "tempo": 120.0,
            "signature_numerator": 4,
            "signature_denominator": 4,
            "track_count": 2,
            "tracks": [
                {"index": 0, "name": "1-MIDI"},
                {"index": 1, "name": "2-Audio"},
            ],
        }),
    ]),

    # ── get_remote_script_info ────────────────────────────────────────────
    # Runs a real handshake: sends get_script_info, then reports the cached
    # info plus expected_version.
    _case("get_remote_script_info", "success_up_to_date", {}, [
        _ok("get_script_info", {}, {
            "script_version": EXPECTED_REMOTE_SCRIPT_VERSION,
            "capabilities": ["delete_clip", "get_clip_notes", "get_session_snapshot"],
        }),
    ]),
    _case("get_remote_script_info", "success_version_mismatch", {}, [
        _ok("get_script_info", {}, {
            "script_version": "1.6.0",
            "capabilities": ["get_clip_notes"],
        }),
    ]),
    # Old scripts answer "Unknown command" — the handshake maps that to the
    # sentinel version "legacy".
    _case("get_remote_script_info", "legacy_script", {}, [
        _boom("get_script_info", {}, "Unknown command: get_script_info"),
    ]),

    # ── get_track_info ────────────────────────────────────────────────────
    _case("get_track_info", "success", {"track_index": 1}, [
        _ok("get_track_info", {"track_index": 1}, {
            "index": 1,
            "name": "2-Audio",
            "is_midi_track": False,
            "mute": False,
            "solo": False,
            "arm": True,
            "clip_slots": [
                {"index": 0, "has_clip": True,
                 "clip": {"name": "Take 1", "length": 4.0}},
                {"index": 1, "has_clip": False, "clip": None},
            ],
            "devices": [{"index": 0, "name": "Reverb", "class_name": "Reverb"}],
        }),
    ]),

    # ── get_clip_notes (gated) ────────────────────────────────────────────
    _case("get_clip_notes", "success", {"track_index": 0, "clip_index": 2}, [
        _ok("get_clip_notes",
            {"track_index": 0, "clip_index": 2, "arrangement": False}, {
                "clip_name": "Bassline",
                "length": 4.0,
                "note_count": 2,
                "notes": NOTES,
            }),
    ]),
    # Which view a note read comes from is never left to the script's
    # default — the flag is explicit in both directions.
    _case("get_clip_notes", "success_arrangement",
          {"track_index": 0, "clip_index": 2, "arrangement": True}, [
        _ok("get_clip_notes",
            {"track_index": 0, "clip_index": 2, "arrangement": True}, {
                "clip_name": "Bassline",
                "arrangement": True,
                "length": 4.0,
                "note_count": 2,
                "notes": NOTES,
            }),
    ]),

    # ── get_session_snapshot (gated) ──────────────────────────────────────
    # Default args must reach the wire as include_notes/include_params True.
    _case("get_session_snapshot", "success_defaults", {}, [
        _ok("get_session_snapshot",
            {"include_notes": True, "include_params": True,
             "include_warp_markers": False, "include_rack_chains": False,
             "include_empty_slots": False}, {
                "tempo": 120.0,
                "track_count": 1,
                "tracks": [{
                    "index": 0, "name": "1-MIDI",
                    "clips": [{"name": "Bassline", "notes": NOTES}],
                    "devices": [{"name": "Operator",
                                 "parameters": [{"name": "Volume", "value": 0.85}]}],
                }],
            }),
    ]),
    _case("get_session_snapshot", "success_lean",
          {"include_notes": False, "include_params": False}, [
        _ok("get_session_snapshot",
            {"include_notes": False, "include_params": False,
             "include_warp_markers": False, "include_rack_chains": False,
             "include_empty_slots": False}, {
                "tempo": 120.0,
                "track_count": 1,
                "tracks": [{"index": 0, "name": "1-MIDI"}],
            }),
    ]),
    # A bare selector is wrapped into the wire's list form by the
    # controller — "just the DRUMS bus" is the common case.
    _case("get_session_snapshot", "success_scoped_to_one_track",
          {"tracks": "DRUMS"}, [
        _ok("get_session_snapshot",
            {"include_notes": True, "include_params": True,
             "include_warp_markers": False, "include_rack_chains": False,
             "include_empty_slots": False, "tracks": ["DRUMS"]}, {
                "schema": "ableton_mcp_snapshot_v3",
                "tracks_selected": [2],
                "tracks": [{"index": 2, "name": "DRUMS"}],
            }),
    ]),

    # ── create_midi_track ─────────────────────────────────────────────────
    _case("create_midi_track", "success", {"index": 2}, [
        _ok("create_midi_track", {"index": 2}, {"index": 2, "name": "3-MIDI"}),
    ]),
    # Default index (-1 = append) on the wire; nameless result falls back to
    # the literal 'unknown'.
    _case("create_midi_track", "success_unnamed_result", {}, [
        _ok("create_midi_track", {"index": -1}, {}),
    ]),

    # ── create_audio_track ────────────────────────────────────────────────
    _case("create_audio_track", "success", {}, [
        _ok("create_audio_track", {"index": -1}, {"index": 3, "name": "4-Audio"}),
    ]),

    # ── duplicate_track ───────────────────────────────────────────────────
    _case("duplicate_track", "success", {"track_index": 2}, [
        _ok("duplicate_track", {"track_index": 2},
            {"source_track_index": 2, "source_track_name": "GTR L",
             "duplicated": True, "index": 3, "name": "GTR L 2",
             "track_count_before": 6, "track_count_after": 7}),
    ]),
    # The count did not move: Live returned without inserting anything, so
    # the reply must not hand back an index to address.
    _case("duplicate_track", "not_duplicated", {"track_index": 2}, [
        _ok("duplicate_track", {"track_index": 2},
            {"source_track_index": 2, "source_track_name": "GTR L",
             "duplicated": False, "index": None, "name": None,
             "track_count_before": 6, "track_count_after": 6}),
    ]),
    # The guard rides along only when the caller asks for it.
    _case("duplicate_track", "success_guarded",
          {"track_index": 2, "expect_track_name": "GTR L"}, [
        _ok("duplicate_track",
            {"track_index": 2, "expect_track_name": "GTR L"},
            {"source_track_index": 2, "source_track_name": "GTR L",
             "duplicated": True, "index": 3, "name": "GTR L 2",
             "track_count_before": 6, "track_count_after": 7}),
    ]),

    # ── set_track_name ────────────────────────────────────────────────────
    _case("set_track_name", "success", {"track_index": 0, "name": "Drums"}, [
        _ok("set_track_name", {"track_index": 0, "name": "Drums"},
            {"name": "Drums"}),
    ]),

    # ── create_clip ───────────────────────────────────────────────────────
    _case("create_clip", "success",
          {"track_index": 1, "clip_index": 0, "length": 8.0}, [
        _ok("create_clip", {"track_index": 1, "clip_index": 0, "length": 8.0},
            {"name": "Clip", "length": 8.0}),
    ]),

    # ── set_clip_gain ─────────────────────────────────────────────────────
    # gain_display present vs absent (falls back to the raw gain value).
    _case("set_clip_gain", "success_display",
          {"track_index": 0, "clip_index": 1, "gain": 0.42}, [
        _ok("set_clip_gain",
            {"track_index": 0, "clip_index": 1, "gain": 0.42,
             "arrangement": True},
            {"clip_name": "Take 2", "track_name": "Vocals",
             "gain": 0.42, "gain_display": "-3.5 dB"}),
    ]),
    _case("set_clip_gain", "success_no_display",
          {"track_index": 0, "clip_index": 1, "gain": 0.42,
           "arrangement": False}, [
        _ok("set_clip_gain",
            {"track_index": 0, "clip_index": 1, "gain": 0.42,
             "arrangement": False},
            {"clip_name": "Take 2", "track_name": "Vocals", "gain": 0.42}),
    ]),

    # ── set_clip_warp ─────────────────────────────────────────────────────
    # Warp off is the stems fix: the response reports the clip's restored
    # native length, which is how a caller confirms stems now agree.
    _case("set_clip_warp", "warp_off_restores_native_length",
          {"track_index": 2, "clip_index": 0, "warping": False}, [
        _ok("set_clip_warp",
            {"track_index": 2, "clip_index": 0, "warping": False,
             "warp_mode": None, "arrangement": True},
            {"clip_name": "session_Drums", "track_name": "Drums",
             "warping": False, "warp_mode": 0, "length": 520.834}),
    ]),
    _case("set_clip_warp", "warp_on_with_mode",
          {"track_index": 2, "clip_index": 0, "warping": True,
           "warp_mode": 4}, [
        _ok("set_clip_warp",
            {"track_index": 2, "clip_index": 0, "warping": True,
             "warp_mode": 4, "arrangement": True},
            {"clip_name": "session_Drums", "track_name": "Drums",
             "warping": True, "warp_mode": 4, "length": 530.16}),
    ]),

    # ── back_to_arrangement ───────────────────────────────────────────────
    _case("back_to_arrangement", "success", {}, [
        _ok("back_to_arrangement", {}, {"ok": True}),
    ]),

    # ── get_track_routing ─────────────────────────────────────────────────
    _case("get_track_routing", "success", {"track_index": 2}, [
        _ok("get_track_routing", {"track_index": 2}, {
            "track_name": "Bass",
            "output_routing_type": "Main",
            "available_output_routing_types": ["Main", "Sends Only", "Bus"],
            "input_routing_type": "No Input",
            "available_input_routing_types": ["No Input", "Ext. In"],
        }),
    ]),

    # ── set_track_routing ─────────────────────────────────────────────────
    # Default field must reach the wire as "output_routing_type".
    _case("set_track_routing", "success", {"track_index": 2, "target": "Bus"}, [
        _ok("set_track_routing",
            {"track_index": 2, "field": "output_routing_type", "target": "Bus"},
            {"track_name": "Bass", "field": "output_routing_type",
             "value": "Bus"}),
    ]),

    # ── set_count_in ──────────────────────────────────────────────────────
    # The metronome on/off wording is a branch.
    _case("set_count_in", "success_metronome_on",
          {"bars": 2, "metronome": True}, [
        _ok("set_count_in", {"bars": 2, "metronome": True},
            {"count_in": "2 Bars", "metronome": True}),
    ]),
    _case("set_count_in", "success_metronome_off",
          {"bars": 0, "metronome": False}, [
        _ok("set_count_in", {"bars": 0, "metronome": False},
            {"count_in": "None", "metronome": False}),
    ]),
    # Live 12.3+: the script reports the count-in property as read-only and
    # the honest partial outcome (metronome still applied) must surface.
    _case("set_count_in", "read_only_partial",
          {"bars": 2, "metronome": True}, [
        _ok("set_count_in", {"bars": 2, "metronome": True},
            {"count_in_writable": False, "requested": "2 Bars",
             "count_in_duration": 1, "count_in": "1 Bar",
             "metronome": True}),
    ]),

    # ── set_track_send ────────────────────────────────────────────────────
    # display_value present vs absent (falls back to the raw value).
    _case("set_track_send", "success_display",
          {"track_index": 0, "send_index": 1, "value": 0.65}, [
        _ok("set_track_send",
            {"track_index": 0, "send_index": 1, "value": 0.65},
            {"track_name": "Vocals", "value": 0.65,
             "display_value": "-12.0 dB"}),
    ]),
    _case("set_track_send", "success_no_display",
          {"track_index": 0, "send_index": 1, "value": 0.65}, [
        _ok("set_track_send",
            {"track_index": 0, "send_index": 1, "value": 0.65},
            {"track_name": "Vocals", "value": 0.65}),
    ]),

    # ── save_set ──────────────────────────────────────────────────────────
    _case("save_set", "success_saved", {}, [
        _ok("save_set", {}, {"saved": True, "method": "song.save_set"}),
    ]),
    _case("save_set", "success_not_saved", {}, [
        _ok("save_set", {}, {
            "saved": False,
            "attempts": ["song.save_set", "song.save", "app.save_document"],
        }),
    ]),

    # ── create_return_track ───────────────────────────────────────────────
    _case("create_return_track", "success", {}, [
        _ok("create_return_track", {}, {"name": "A-Reverb", "return_index": 0}),
    ]),

    # ── set_track_arm ─────────────────────────────────────────────────────
    # armed default True; wording branches on result["arm"].
    _case("set_track_arm", "success_armed", {"track_index": 1}, [
        _ok("set_track_arm", {"track_index": 1, "value": True},
            {"track_name": "Vocals", "arm": True}),
    ]),
    _case("set_track_arm", "success_disarmed",
          {"track_index": 1, "armed": False}, [
        _ok("set_track_arm", {"track_index": 1, "value": False},
            {"track_name": "Vocals", "arm": False}),
    ]),

    # ── set_track_monitoring ──────────────────────────────────────────────
    _case("set_track_monitoring", "success",
          {"track_index": 0, "state": "in"}, [
        _ok("set_track_monitoring", {"track_index": 0, "value": "in"},
            {"track_name": "Guitar", "monitoring": "in"}),
    ]),

    # ── get_device_parameters ─────────────────────────────────────────────
    _case("get_device_parameters", "success",
          {"track_index": 0, "device_index": 1}, [
        _ok("get_device_parameters",
            {"track_index": 0, "device_index": 1, "track_type": "regular"}, {
                "device_name": "Reverb",
                "parameters": [
                    {"index": 0, "name": "Dry/Wet", "value": 1.0,
                     "min": 0.0, "max": 1.0, "display_value": "100 %"},
                    {"index": 1, "name": "Decay Time", "value": 1200.0,
                     "min": 100.0, "max": 60000.0, "display_value": "1.20 s"},
                ],
            }),
    ]),
    _case("get_device_parameters", "success_return_track",
          {"track_index": 0, "device_index": 0, "track_type": "return"}, [
        _ok("get_device_parameters",
            {"track_index": 0, "device_index": 0, "track_type": "return"}, {
                "device_name": "Delay",
                "parameters": [
                    {"index": 0, "name": "Feedback", "value": 0.35,
                     "min": 0.0, "max": 0.95, "display_value": "35 %"},
                ],
            }),
    ]),

    # ── set_device_parameter ──────────────────────────────────────────────
    # Name stays a string on the wire; clamped=true appends " (clamped)".
    _case("set_device_parameter", "success_clamped_by_name",
          {"track_index": 0, "device_index": 1, "parameter": "Dry/Wet",
           "value": 1.5}, [
        _ok("set_device_parameter",
            {"track_index": 0, "device_index": 1, "parameter": "Dry/Wet",
             "value": 1.5, "track_type": "regular"},
            {"device_name": "Reverb", "parameter_name": "Dry/Wet",
             "value": 1.0, "display_value": "100 %", "clamped": True}),
    ]),
    # "3" is coerced to int 3 on the wire; no display_value falls back to
    # the raw value; no clamped flag, no suffix.
    _case("set_device_parameter", "success_by_index_string",
          {"track_index": 1, "device_index": 0, "parameter": "3",
           "value": 0.35}, [
        _ok("set_device_parameter",
            {"track_index": 1, "device_index": 0, "parameter": 3,
             "value": 0.35, "track_type": "regular"},
            {"device_name": "Delay", "parameter_name": "Feedback",
             "value": 0.35}),
    ]),

    # ── set_device_parameters (the batch form) ────────────────────────────
    # One key that resolves to nothing does not cost the caller the rest of
    # the batch: it comes back as its own row, echoing the key as written.
    _case("set_device_parameters", "success_with_one_unknown_key",
          {"track_index": 0, "device_index": 1,
           "parameters": {"Dry/Wet": 0.25, "Decay Time": 3.2,
                          "Wetness": 0.5}}, [
        _ok("set_device_parameters",
            {"track_index": 0, "device_index": 1,
             "parameters": {"Dry/Wet": 0.25, "Decay Time": 3.2,
                            "Wetness": 0.5},
             "track_type": "regular"},
            {"track_index": 0, "track_name": "GUITARS", "device_index": 1,
             "device_name": "Reverb", "requested_count": 3,
             "applied_count": 2, "not_found": ["Wetness"],
             "parameters": [
                 {"name": "Dry/Wet", "requested": 0.25, "old_value": 0.4,
                  "value": 0.25, "display_value": "25 %", "clamped": False,
                  "min": 0.0, "max": 1.0, "found": True},
                 {"name": "Decay Time", "requested": 3.2, "old_value": 1.5,
                  "value": 3.2, "display_value": "3.20 s", "clamped": False,
                  "min": 0.2, "max": 60.0, "found": True},
                 {"name": "Wetness", "requested": 0.5, "value": None,
                  "display_value": "", "clamped": False, "found": False,
                  "error": "No parameter named 'Wetness'. Available: "
                           "Dry/Wet, Decay Time"},
             ]}),
    ]),
    _case("set_device_parameters", "success_clamped",
          {"track_index": 1, "device_index": 0,
           "parameters": {"Feedback": 1.5}, "track_type": "return"}, [
        _ok("set_device_parameters",
            {"track_index": 1, "device_index": 0,
             "parameters": {"Feedback": 1.5}, "track_type": "return"},
            {"track_index": 1, "track_name": "B-Delay", "device_index": 0,
             "device_name": "Delay", "requested_count": 1,
             "applied_count": 1, "not_found": [],
             "parameters": [
                 {"name": "Feedback", "requested": 1.5, "old_value": 0.3,
                  "value": 1.0, "display_value": "100 %", "clamped": True,
                  "min": 0.0, "max": 1.0, "found": True},
             ]}),
    ]),

    # ── delete_device ─────────────────────────────────────────────────────
    # The verified delete: before/after counts and the surviving chain by
    # name, so a highest-index-first sweep can re-anchor on names.
    _case("delete_device", "success", {"track_index": 0, "device_index": 2}, [
        _ok("delete_device",
            {"track_index": 0, "device_index": 2, "track_type": "regular"},
            {"track_index": 0, "track_name": "Drums", "deleted": True,
             "deleted_device_index": 2,
             "deleted_device_name": "Compressor",
             "device_count_before": 3, "remaining_matches_expected": True,
             "remaining_devices": [{"index": 0, "name": "EQ Eight"},
                                   {"index": 1, "name": "Saturator"}],
             "remaining_device_count": 2}),
    ]),
    # The lie this command was rebuilt to stop: the count did not drop, so
    # nothing was deleted — and the reply must not invite a blind retry,
    # which would take the NEXT device.
    _case("delete_device", "nothing_was_deleted",
          {"track_index": 0, "device_index": 2}, [
        _ok("delete_device",
            {"track_index": 0, "device_index": 2, "track_type": "regular"},
            {"track_index": 0, "track_name": "Drums", "deleted": False,
             "deleted_device_index": 2,
             "deleted_device_name": "Compressor",
             "device_count_before": 3, "remaining_matches_expected": False,
             "remaining_devices": [{"index": 0, "name": "EQ Eight"},
                                   {"index": 1, "name": "Saturator"},
                                   {"index": 2, "name": "Compressor"}],
             "remaining_device_count": 3}),
    ]),

    # ── set_track_volume ──────────────────────────────────────────────────
    _case("set_track_volume", "success", {"track_index": 0, "value": 0.85}, [
        _ok("set_track_volume",
            {"track_index": 0, "value": 0.85, "track_type": "regular"},
            {"track_name": "Drums", "value": 0.85, "display_value": "0.0 dB"}),
    ]),

    # ── set_track_pan ─────────────────────────────────────────────────────
    _case("set_track_pan", "success", {"track_index": 0, "value": -0.5}, [
        _ok("set_track_pan",
            {"track_index": 0, "value": -0.5, "track_type": "regular"},
            {"track_name": "Drums", "value": -0.5, "display_value": "25L"}),
    ]),

    # ── set_track_mute ────────────────────────────────────────────────────
    _case("set_track_mute", "success_muted", {"track_index": 0, "mute": True}, [
        _ok("set_track_mute", {"track_index": 0, "value": True},
            {"track_name": "Drums", "mute": True}),
    ]),
    _case("set_track_mute", "success_unmuted",
          {"track_index": 0, "mute": False}, [
        _ok("set_track_mute", {"track_index": 0, "value": False},
            {"track_name": "Drums", "mute": False}),
    ]),

    # ── delete_track ──────────────────────────────────────────────────────
    _case("delete_track", "success", {"track_index": 2}, [
        _ok("delete_track", {"track_index": 2},
            {"deleted_track_name": "Old Synth", "remaining_track_count": 3}),
    ]),

    # ── create_audio_clip ─────────────────────────────────────────────────
    _case("create_audio_clip", "success",
          {"track_index": 1, "clip_index": 0, "path": "/tmp/loop.wav"}, [
        _ok("create_audio_clip",
            {"track_index": 1, "clip_index": 0, "path": "/tmp/loop.wav"},
            {"track_name": "Stems", "name": "loop", "length": 16.0,
             "is_audio_clip": True, "warping": False}),
    ]),
    # file_path is the spelling callers reach for; it lands on `path`.
    _case("create_audio_clip", "success_via_file_path_alias",
          {"track_index": 1, "clip_index": 0,
           "file_path": "/tmp/loop.wav"}, [
        _ok("create_audio_clip",
            {"track_index": 1, "clip_index": 0, "path": "/tmp/loop.wav"},
            {"track_name": "Stems", "name": "loop", "length": 16.0,
             "is_audio_clip": True, "warping": False}),
    ]),
    # The six-stems incident, caught at import: Live's auto-warp guessed a
    # source tempo and produced twice the length the caller expected.
    _case("create_audio_clip", "length_disagrees_with_expected_beats",
          {"track_index": 1, "clip_index": 0, "path": "/tmp/stem.wav",
           "expected_beats": 16.0}, [
        _ok("create_audio_clip",
            {"track_index": 1, "clip_index": 0, "path": "/tmp/stem.wav",
             "expected_beats": 16.0},
            {"track_name": "Stems", "name": "stem", "length": 32.0,
             "is_audio_clip": True, "warping": False,
             "expected_beats": 16.0, "length_matches_expected": False}),
    ]),

    # ── add_notes_to_clip ─────────────────────────────────────────────────
    _case("add_notes_to_clip", "success",
          {"track_index": 0, "clip_index": 0, "notes": NOTES}, [
        _ok("add_notes_to_clip",
            {"track_index": 0, "clip_index": 0, "notes": NOTES,
             "arrangement": False},
            {"track_name": "Drums", "clip_name": "Beat",
             "arrangement": False, "requested": 2, "added": 2,
             "clip_note_count": 2}),
    ]),
    # The measured delta disagreeing with the request is the whole reason
    # the result stopped echoing len(notes) back.
    _case("add_notes_to_clip", "live_took_fewer_than_were_sent",
          {"track_index": 0, "clip_index": 0, "notes": NOTES}, [
        _ok("add_notes_to_clip",
            {"track_index": 0, "clip_index": 0, "notes": NOTES,
             "arrangement": False},
            {"track_name": "Drums", "clip_name": "Beat",
             "arrangement": False, "requested": 2, "added": 1,
             "clip_note_count": 1}),
    ]),
    # A clip whose notes could not be counted: "I could not check" is not
    # "none arrived", and the text must not conflate them.
    _case("add_notes_to_clip", "count_unavailable",
          {"track_index": 0, "clip_index": 0, "notes": NOTES}, [
        _ok("add_notes_to_clip",
            {"track_index": 0, "clip_index": 0, "notes": NOTES,
             "arrangement": False},
            {"track_name": "Drums", "clip_name": "Beat",
             "arrangement": False, "requested": 2, "added": None,
             "clip_note_count": None}),
    ]),
    # expect_count and the arrangement write path, both on the wire.
    _case("add_notes_to_clip", "arrangement_write_with_expect_count",
          {"track_index": 0, "clip_index": 3, "notes": NOTES,
           "expect_count": 2, "arrangement": True}, [
        _ok("add_notes_to_clip",
            {"track_index": 0, "clip_index": 3, "notes": NOTES,
             "arrangement": True, "expect_count": 2},
            {"track_name": "Drums", "clip_name": "Beat 60",
             "arrangement": True, "requested": 2, "added": 2,
             "clip_note_count": 2}),
    ]),

    # ── clear_notes_from_clip (gated) ─────────────────────────────────────
    _case("clear_notes_from_clip", "success",
          {"track_index": 0, "clip_index": 0}, [
        _ok("clear_notes_from_clip",
            {"track_index": 0, "clip_index": 0, "arrangement": False},
            {"clip_name": "Beat", "arrangement": False,
             "cleared_count": 5, "clip_note_count": 0}),
    ]),
    _case("clear_notes_from_clip", "success_on_an_arrangement_clip",
          {"track_index": 0, "clip_index": 3, "arrangement": True}, [
        _ok("clear_notes_from_clip",
            {"track_index": 0, "clip_index": 3, "arrangement": True},
            {"clip_name": "Beat 60", "arrangement": True,
             "cleared_count": 5, "clip_note_count": 0}),
    ]),

    # ── set_clip_name ─────────────────────────────────────────────────────
    _case("set_clip_name", "success",
          {"track_index": 0, "clip_index": 1, "name": "Chorus"}, [
        _ok("set_clip_name",
            {"track_index": 0, "clip_index": 1, "name": "Chorus"},
            {"name": "Chorus"}),
    ]),

    # ── set_arrangement_clip_name ─────────────────────────────────────────
    _case("set_arrangement_clip_name", "success",
          {"track_index": 0, "clip_index": 2, "name": "Verse"}, [
        _ok("set_arrangement_clip_name",
            {"track_index": 0, "clip_index": 2, "name": "Verse"},
            {"name": "Verse"}),
    ]),

    # ── set_tempo ─────────────────────────────────────────────────────────
    _case("set_tempo", "success", {"tempo": 98.5}, [
        _ok("set_tempo", {"tempo": 98.5}, {"tempo": 98.5}),
    ]),

    # ── load_instrument_or_effect (sends load_browser_item) ──────────────
    _case("load_instrument_or_effect", "success_new_devices",
          {"track_index": 0, "uri": "query:Synths#Operator"}, [
        _ok("load_browser_item",
            {"track_index": 0, "item_uri": "query:Synths#Operator",
             "track_type": "regular"},
            {"loaded": True, "new_devices": ["Operator"]}),
    ]),
    # loaded but nothing new detected: falls back to devices_after.
    _case("load_instrument_or_effect", "success_no_new_devices",
          {"track_index": 0, "uri": "query:Synths#Operator"}, [
        _ok("load_browser_item",
            {"track_index": 0, "item_uri": "query:Synths#Operator",
             "track_type": "regular"},
            {"loaded": True, "new_devices": [],
             "devices_after": ["Operator", "Reverb"]}),
    ]),
    # A 1.8.0-or-older script sends neither new_devices nor devices_after;
    # the presenter must fall back to the item name, not print an empty list.
    _case("load_instrument_or_effect", "loaded_no_device_report",
          {"track_index": 0, "uri": "query:Drums#FileId_5483"}, [
        _ok("load_browser_item",
            {"track_index": 0, "item_uri": "query:Drums#FileId_5483",
             "track_type": "regular"},
            {"loaded": True, "item_name": "808 Core Kit.adg",
             "track_name": "Kick"}),
    ]),
    _case("load_instrument_or_effect", "not_loaded",
          {"track_index": 0, "uri": "query:Synths#Operator"}, [
        _ok("load_browser_item",
            {"track_index": 0, "item_uri": "query:Synths#Operator",
             "track_type": "regular"},
            {"loaded": False}),
    ]),

    # ── fire_clip ─────────────────────────────────────────────────────────
    _case("fire_clip", "success", {"track_index": 0, "clip_index": 0}, [
        _ok("fire_clip", {"track_index": 0, "clip_index": 0}, {"fired": True}),
    ]),

    # ── stop_clip ─────────────────────────────────────────────────────────
    _case("stop_clip", "success", {"track_index": 0, "clip_index": 0}, [
        _ok("stop_clip", {"track_index": 0, "clip_index": 0}, {"stopped": True}),
    ]),

    # ── delete_clip (gated) ───────────────────────────────────────────────
    _case("delete_clip", "success", {"track_index": 0, "clip_index": 1}, [
        _ok("delete_clip", {"track_index": 0, "clip_index": 1},
            {"deleted_clip_name": "Old Take", "had_clip": True,
             "track_index": 0, "clip_index": 1}),
    ]),

    # ── start_playback ────────────────────────────────────────────────────
    _case("start_playback", "success", {}, [
        _ok("start_playback", {}, {"playing": True}),
    ]),

    # ── stop_playback ─────────────────────────────────────────────────────
    _case("stop_playback", "success", {}, [
        _ok("stop_playback", {}, {"playing": False}),
    ]),

    # ── get_browser_tree ──────────────────────────────────────────────────
    # Nested children, path rendering, and the has_more "[...]" marker.
    _case("get_browser_tree", "success_tree",
          {"category_type": "instruments"}, [
        _ok("get_browser_tree", {"category_type": "instruments"}, {
            "categories": [{
                "name": "Instruments",
                "path": "instruments",
                "has_more": False,
                "children": [
                    {"name": "Drum Rack",
                     "path": "instruments/Drum Rack",
                     "has_more": True,
                     "children": [
                         {"name": "Kits",
                          "path": "instruments/Drum Rack/Kits",
                          "has_more": False,
                          "children": []},
                     ]},
                    {"name": "Operator",
                     "path": "instruments/Operator",
                     "has_more": False,
                     "children": []},
                ],
            }],
            "total_folders": 4,
        }),
    ]),
    # Empty categories + available_categories = the "No categories found"
    # branch.
    _case("get_browser_tree", "success_no_categories",
          {"category_type": "bogus"}, [
        _ok("get_browser_tree", {"category_type": "bogus"}, {
            "categories": [],
            "available_categories": ["instruments", "sounds", "drums"],
        }),
    ]),
    # Message-sniffing error branches.
    _case("get_browser_tree", "error_browser_unavailable",
          {"category_type": "all"}, [
        _boom("get_browser_tree", {"category_type": "all"},
              "Browser is not available"),
    ]),
    _case("get_browser_tree", "error_live_app_unavailable",
          {"category_type": "all"}, [
        _boom("get_browser_tree", {"category_type": "all"},
              "Could not access Live application"),
    ]),

    # ── get_browser_items_at_path ─────────────────────────────────────────
    _case("get_browser_items_at_path", "success",
          {"path": "instruments/Drum Rack"}, [
        _ok("get_browser_items_at_path", {"path": "instruments/Drum Rack"}, {
            "path": "instruments/Drum Rack",
            "items": [
                {"name": "Kit One", "uri": "query:Drums#Kit%20One",
                 "is_folder": False, "is_device": False, "is_loadable": True},
                {"name": "More Kits", "uri": "",
                 "is_folder": True, "is_device": False, "is_loadable": False},
            ],
        }),
    ]),
    # An error payload (not an exception) carrying available_categories.
    _case("get_browser_items_at_path", "error_payload_with_categories",
          {"path": "bogus/thing"}, [
        _ok("get_browser_items_at_path", {"path": "bogus/thing"}, {
            "error": "Unknown category 'bogus'",
            "available_categories": ["instruments", "sounds", "drums"],
        }),
    ]),
    # Message-sniffing error branches.
    _case("get_browser_items_at_path", "error_browser_unavailable",
          {"path": "instruments"}, [
        _boom("get_browser_items_at_path", {"path": "instruments"},
              "Browser is not available"),
    ]),
    _case("get_browser_items_at_path", "error_live_app_unavailable",
          {"path": "instruments"}, [
        _boom("get_browser_items_at_path", {"path": "instruments"},
              "Could not access Live application"),
    ]),
    _case("get_browser_items_at_path", "error_unknown_category",
          {"path": "bogus"}, [
        _boom("get_browser_items_at_path", {"path": "bogus"},
              "Unknown or unavailable category 'bogus'"),
    ]),
    _case("get_browser_items_at_path", "error_path_not_found",
          {"path": "instruments/Nope"}, [
        _boom("get_browser_items_at_path", {"path": "instruments/Nope"},
              "Path part 'Nope' not found"),
    ]),

    # ── load_drum_kit ─────────────────────────────────────────────────────
    # Full success = the complete multi-command wire sequence, in order:
    # load rack -> browse kit path -> load first loadable kit. Note the rack
    # load sends NO track_type, and the non-loadable folder is filtered out.
    _case("load_drum_kit", "success",
          {"track_index": 0, "rack_uri": "Drums/Drum Rack",
           "kit_path": "drums/acoustic"}, [
        _ok("load_browser_item",
            {"track_index": 0, "item_uri": "Drums/Drum Rack"},
            {"loaded": True}),
        _ok("get_browser_items_at_path", {"path": "drums/acoustic"}, {
            "items": [
                {"name": "More Kits", "uri": "", "is_loadable": False},
                {"name": "Kit One", "uri": "query:Drums#Kit%20One",
                 "is_loadable": True},
                {"name": "Kit Two", "uri": "query:Drums#Kit%20Two",
                 "is_loadable": True},
            ],
        }),
        _ok("load_browser_item",
            {"track_index": 0, "item_uri": "query:Drums#Kit%20One"},
            {"loaded": True}),
    ]),
    # kit_path pointing directly at a kit FILE: the node itself is loadable
    # and has no children; it must be loaded rather than bailing no_loadable.
    _case("load_drum_kit", "success_direct_kit_file",
          {"track_index": 0, "rack_uri": "Drums/Drum Rack",
           "kit_path": "drums/808 Core Kit.adg"}, [
        _ok("load_browser_item",
            {"track_index": 0, "item_uri": "Drums/Drum Rack"},
            {"loaded": True}),
        _ok("get_browser_items_at_path", {"path": "drums/808 Core Kit.adg"}, {
            "name": "808 Core Kit.adg", "uri": "query:Drums#FileId_5483",
            "is_loadable": True, "items": [],
        }),
        _ok("load_browser_item",
            {"track_index": 0, "item_uri": "query:Drums#FileId_5483"},
            {"loaded": True}),
    ]),
    # Early bail #1: the rack itself fails to load — one wire call only.
    _case("load_drum_kit", "bail_rack_not_loaded",
          {"track_index": 0, "rack_uri": "Drums/Drum Rack",
           "kit_path": "drums/acoustic"}, [
        _ok("load_browser_item",
            {"track_index": 0, "item_uri": "Drums/Drum Rack"},
            {"loaded": False}),
    ]),
    # Early bail #2: the kit path lookup returns an error payload.
    _case("load_drum_kit", "bail_kit_path_error",
          {"track_index": 0, "rack_uri": "Drums/Drum Rack",
           "kit_path": "drums/missing"}, [
        _ok("load_browser_item",
            {"track_index": 0, "item_uri": "Drums/Drum Rack"},
            {"loaded": True}),
        _ok("get_browser_items_at_path", {"path": "drums/missing"},
            {"error": "Path part 'missing' not found"}),
    ]),
    # Early bail #3: the path exists but holds nothing loadable.
    _case("load_drum_kit", "bail_no_loadable_kits",
          {"track_index": 0, "rack_uri": "Drums/Drum Rack",
           "kit_path": "drums/empty"}, [
        _ok("load_browser_item",
            {"track_index": 0, "item_uri": "Drums/Drum Rack"},
            {"loaded": True}),
        _ok("get_browser_items_at_path", {"path": "drums/empty"}, {
            "items": [{"name": "Folder", "uri": "", "is_loadable": False}],
        }),
    ]),

    # ── switch_to_arrangement_view ────────────────────────────────────────
    _case("switch_to_arrangement_view", "success", {}, [
        _ok("switch_to_arrangement_view", {}, {"view": "Arranger"}),
    ]),

    # ── set_arrangement_time (sends set_current_song_time) ───────────────
    _case("set_arrangement_time", "success", {"time": 8.0}, [
        _ok("set_current_song_time", {"time": 8.0},
            {"current_song_time": 8.0}),
    ]),

    # Transport still settling after a stop: the stale read-back must be
    # flagged, not stated as the outcome.
    _case("set_arrangement_time", "unsettled_after_stop", {"time": 0.0}, [
        _ok("set_current_song_time", {"time": 0.0},
            {"current_song_time": 5.2, "requested": 0.0, "settled": False}),
    ]),

    # ── get_arrangement_clips ─────────────────────────────────────────────
    _case("get_arrangement_clips", "success", {"track_index": 0}, [
        _ok("get_arrangement_clips", {"track_index": 0}, {
            "track_name": "Drums",
            "clip_count": 2,
            "clips": [
                {"index": 0, "name": "Intro", "start_time": 0.0,
                 "end_time": 8.0, "length": 8.0, "type": "midi"},
                {"index": 1, "name": "Verse", "start_time": 8.0,
                 "end_time": 24.0, "length": 16.0, "type": "midi"},
            ],
        }),
    ]),

    # ── duplicate_to_arrangement (sends duplicate_session_clip_to_arrangement)
    _case("duplicate_to_arrangement", "success",
          {"track_index": 0, "clip_index": 0, "destination_time": 16.0}, [
        _ok("duplicate_session_clip_to_arrangement",
            {"track_index": 0, "clip_index": 0, "destination_time": 16.0},
            {"clip_name": "Chorus Beat", "track_name": "Drums"}),
    ]),
    # The spelling this argument gets guessed as — seven times in four days
    # of one session. It reaches the canonical key.
    _case("duplicate_to_arrangement", "success_via_arrangement_time_alias",
          {"track_index": 0, "clip_index": 0, "arrangement_time": 16.0}, [
        _ok("duplicate_session_clip_to_arrangement",
            {"track_index": 0, "clip_index": 0, "destination_time": 16.0},
            {"clip_name": "Chorus Beat", "track_name": "Drums"}),
    ]),
    # Two spellings, two different values: refused in the controller, so
    # the wire exchange is empty — nothing was stamped anywhere.
    _case("duplicate_to_arrangement", "conflicting_spellings_refused",
          {"track_index": 0, "clip_index": 0, "destination_time": 16.0,
           "arrangement_time": 32.0}, []),
    # The batch: one call, a whole run of placements, each reported.
    _case("duplicate_to_arrangement", "success_batch",
          {"track_index": 0, "clip_index": 0,
           "destination_times": [0.0, 4.0, 8.0]}, [
        _ok("duplicate_session_clip_to_arrangement",
            {"track_index": 0, "clip_index": 0,
             "destination_times": [0.0, 4.0, 8.0]},
            {"success": True, "track_index": 0, "track_name": "Drums",
             "clip_name": "Chorus Beat",
             "destination_times": [0.0, 4.0, 8.0], "requested_count": 3,
             "placed_count": 3, "failed_count": 0,
             "placements": [
                 {"destination_time": 0.0, "ok": True, "error": None,
                  "stamp_start_time": 0.0, "stamp_end_time": 4.0,
                  "overlapped_clips": [], "clips_in_span_after": 1},
                 {"destination_time": 4.0, "ok": True, "error": None,
                  "stamp_start_time": 4.0, "stamp_end_time": 8.0,
                  "overlapped_clips": [], "clips_in_span_after": 1},
                 {"destination_time": 8.0, "ok": True, "error": None,
                  "stamp_start_time": 8.0, "stamp_end_time": 12.0,
                  "overlapped_clips": [], "clips_in_span_after": 1},
             ]}),
    ]),
    # A run that half landed. The placements that landed are REAL and stay
    # on the timeline, so the text has to name both halves — this is the
    # case that must never read as a clean failure.
    _case("duplicate_to_arrangement", "partial_batch_with_a_refusal",
          {"track_index": 0, "clip_index": 0,
           "destination_times": [0.0, 4.0]}, [
        _ok("duplicate_session_clip_to_arrangement",
            {"track_index": 0, "clip_index": 0,
             "destination_times": [0.0, 4.0]},
            {"success": False, "track_index": 0, "track_name": "Drums",
             "clip_name": "Chorus Beat", "destination_times": [0.0, 4.0],
             "requested_count": 2, "placed_count": 1, "failed_count": 1,
             "placements": [
                 {"destination_time": 0.0, "ok": True, "error": None,
                  "stamp_start_time": 0.0, "stamp_end_time": 4.0,
                  "overlapped_clips": [], "clips_in_span_after": 1},
                 {"destination_time": 4.0, "ok": False,
                  "stamp_start_time": 4.0, "stamp_end_time": 8.0,
                  "error": "Refusing to stamp beats 4.0-8.0: it would leave "
                           "the looping clip 'Hats 8' (beats 0.0-16.0) "
                           "surviving to the right, which restarts its loop "
                           "from the top and re-phases everything after the "
                           "cut."},
             ]}),
    ]),
    # The whole-call refusal, which is the OTHER half of the guard's
    # contract: with a scalar destination_time the script raises instead of
    # returning rows, because nothing landed and there is nothing to report.
    # The message is long on purpose — it names the victim, the beat the
    # survivor would re-phase from, and the two safe shapes — and freezing
    # it here is what keeps a future edit from quietly shortening it into
    # advice the model cannot act on.
    _case("duplicate_to_arrangement", "loop_phase_refused",
          {"track_index": 0, "clip_index": 0, "destination_time": 4.0}, [
        _boom("duplicate_session_clip_to_arrangement",
              {"track_index": 0, "clip_index": 0, "destination_time": 4.0},
              "Refusing this stamp at beat 4.0 (footprint 4.0 to 8.0 on "
              "'Drums'): it would re-phase 1 looping clip(s). Live crops "
              "whatever a stamp lands on, and a survivor to the RIGHT of the "
              "stamp keeps its own loop_start, so it replays the loop from "
              "the top from that beat on — silently, and it sounds "
              "plausible. 'Hats 8' spans beats 0.0 to 16.0 (loop_start 0.0) "
              "and would survive from beat 8.0 on with its loop restarted "
              "from the top; to end this stamp at that clip's end instead "
              "use destination_time 12.0, or cover the clip exactly with "
              "destination_time 0.0 and a 16.0-beat source. Nothing was "
              "changed. Delete or unloop the clip first, use one of the safe "
              "shapes above, or pass allow_loop_phase_reset=true to accept "
              "the re-phasing."),
    ]),
    # The override taken deliberately. allow_loop_phase_reset reaches the
    # wire only when the caller actually passed it, and the reply's
    # loop_phase_reset list becomes a footnote naming what was re-phased —
    # the decision goes on the record instead of vanishing into a success.
    _case("duplicate_to_arrangement", "loop_phase_reset_accepted",
          {"track_index": 0, "clip_index": 0, "destination_time": 4.0,
           "allow_loop_phase_reset": True}, [
        _ok("duplicate_session_clip_to_arrangement",
            {"track_index": 0, "clip_index": 0, "destination_time": 4.0,
             "allow_loop_phase_reset": True},
            {"success": True, "track_index": 0, "track_name": "Drums",
             "clip_name": "Chorus Beat", "destination_time": 4.0,
             "stamp_start_time": 4.0, "stamp_end_time": 8.0,
             "overlapped_clips": [
                 {"name": "Hats 8", "start_time": 0.0, "end_time": 16.0,
                  "looping": True, "loop_start": 0.0, "loop_end": 8.0}],
             "clips_in_span_after": 2,
             "loop_phase_reset": ["Hats 8"],
             "placements": [
                 {"destination_time": 4.0, "ok": True, "error": None,
                  "stamp_start_time": 4.0, "stamp_end_time": 8.0,
                  "overlapped_clips": [], "clips_in_span_after": 2,
                  "loop_phase_reset": ["Hats 8"]},
             ]}),
    ]),

    # ── create_locator (gated) ────────────────────────────────────────────
    _case("create_locator", "success", {"name": "Chorus", "time": 16.0}, [
        _ok("create_locator", {"name": "Chorus", "time": 16.0},
            {"name": "Chorus", "time": 16.0}),
    ]),

    # ── jump_to_locator (gated) ───────────────────────────────────────────
    # The service omits absent criteria from the wire params, so a
    # name-only call sends {"name": ...} with no "time" key.
    _case("jump_to_locator", "success", {"name": "Chorus"}, [
        _ok("jump_to_locator", {"name": "Chorus"},
            {"name": "Chorus", "time": 16.0,
             "was_playing": False, "start_marker_set": True}),
    ]),

    # ── delete_locator (gated) ────────────────────────────────────────────
    _case("delete_locator", "success", {"name": "Chorus"}, [
        _ok("delete_locator", {"name": "Chorus"},
            {"success": True, "deleted": True, "name": "Chorus",
             "time": 16.0, "cue_point_count_before": 4,
             "cue_point_count": 3}),
    ]),
    # The toggle did not remove it. Saying so is the point: the same call
    # fired one beat off would have CREATED a locator instead.
    _case("delete_locator", "still_there", {"time": 16.0}, [
        _ok("delete_locator", {"time": 16.0},
            {"success": False, "deleted": False, "name": "Chorus",
             "time": 16.0, "cue_point_count_before": 4,
             "cue_point_count": 4}),
    ]),

    # ── arrangement clip editing (gated) ──────────────────────────────────
    # trim omits absent edges from the wire, like jump_to_locator's criteria.
    _case("trim_arrangement_clip", "success",
          {"track_index": 0, "clip_index": 0, "end_time": 16.0}, [
        _ok("trim_arrangement_clip",
            {"track_index": 0, "clip_index": 0, "end_time": 16.0},
            {"start_time": 0.0, "end_time": 16.0,
             "requested_start_time": 0.0, "requested_end_time": 16.0,
             "trimmed_head": False, "trimmed_tail": True, "refusals": []}),
    ]),
    # A refused edge: the script's refusal strings are self-describing, and
    # the presenter joins them without adding claims of its own.
    _case("trim_arrangement_clip", "edge_refused",
          {"track_index": 0, "clip_index": 0, "end_time": 7.95}, [
        _ok("trim_arrangement_clip",
            {"track_index": 0, "clip_index": 0, "end_time": 7.95},
            {"start_time": 0.0, "end_time": 8.0,
             "requested_start_time": 0.0, "requested_end_time": 7.95,
             "trimmed_head": False, "trimmed_tail": False,
             "refusals": ["end: a clip sits within the stamp's safety zone "
                          "right of the take (2x the eraser's 0.1000-beat "
                          "footprint past the cut), so stamping risks "
                          "cropping it; this edge is untouched — trim it in "
                          "the UI"]}),
    ]),
    _case("trim_arrangement_clip", "nothing_to_trim",
          {"track_index": 0, "clip_index": 0, "start_time": 0.0,
           "end_time": 8.0}, [
        _ok("trim_arrangement_clip",
            {"track_index": 0, "clip_index": 0, "start_time": 0.0,
             "end_time": 8.0},
            {"start_time": 0.0, "end_time": 8.0,
             "requested_start_time": 0.0, "requested_end_time": 8.0,
             "trimmed_head": False, "trimmed_tail": False, "refusals": []}),
    ]),
    _case("delete_arrangement_clip", "success",
          {"track_index": 0, "clip_index": 0}, [
        _ok("delete_arrangement_clip", {"track_index": 0, "clip_index": 0},
            {"deleted": True, "deleted_clip_name": "Vox Take",
             "start_time": 0.0, "end_time": 8.0}),
    ]),
    # Addressed by beat: the stable key, since Live permits no overlaps.
    _case("delete_arrangement_clip", "success_by_start_time",
          {"track_index": 0, "start_time": 64.0}, [
        _ok("delete_arrangement_clip",
            {"track_index": 0, "start_time": 64.0},
            {"deleted": True, "deleted_clip_name": "Vox Take",
             "clip_index": 2, "matched_by": "start_time",
             "start_time": 64.0, "end_time": 72.0,
             "arrangement_clip_count": 5}),
    ]),
    # The plural: every position resolved before anything is deleted, and
    # the caller never maintains the highest-index-first ritual.
    _case("delete_arrangement_clip", "success_by_start_times",
          {"track_index": 0, "start_times": [64.0, 96.0]}, [
        _ok("delete_arrangement_clip",
            {"track_index": 0, "start_times": [64.0, 96.0]},
            {"track_index": 0, "track_name": "ELIZABETH", "success": True,
             "requested_count": 2, "deleted_count": 2, "failed_count": 0,
             "arrangement_clip_count": 4,
             "deletions": [
                 {"start_time": 64.0, "clip_index": 2, "ok": True,
                  "error": None, "deleted_clip_name": "Vox Take",
                  "end_time": 72.0},
                 {"start_time": 96.0, "clip_index": 3, "ok": True,
                  "error": None, "deleted_clip_name": "Vox Take 2",
                  "end_time": 104.0},
             ]}),
    ]),
    _case("move_arrangement_clip", "success",
          {"track_index": 0, "clip_index": 0, "destination_time": 32.0}, [
        _ok("move_arrangement_clip",
            {"track_index": 0, "clip_index": 0, "destination_time": 32.0},
            {"clip_name": "Vox Take", "start_time": 32.0, "end_time": 40.0,
             "moved": True}),
    ]),
    _case("duplicate_arrangement_clip", "success",
          {"track_index": 0, "clip_index": 0, "destination_time": 32.0}, [
        _ok("duplicate_arrangement_clip",
            {"track_index": 0, "clip_index": 0, "destination_time": 32.0},
            {"clip_name": "Vox Take", "destination_time": 32.0,
             "source_start_time": 0.0, "source_end_time": 8.0}),
    ]),
    # The arrangement-side stamp makes the identical Live call as the
    # session one, so it carries the identical guard — and, having no batch
    # form, only ever refuses whole. Frozen separately because "the other
    # stamp is guarded" is exactly the assumption that lets a rewrite drop
    # this one.
    _case("duplicate_arrangement_clip", "loop_phase_refused",
          {"track_index": 0, "clip_index": 0, "destination_time": 4.0}, [
        _boom("duplicate_arrangement_clip",
              {"track_index": 0, "clip_index": 0, "destination_time": 4.0},
              "Refusing this stamp at beat 4.0 (footprint 4.0 to 12.0 on "
              "'Vox'): it would re-phase 1 looping clip(s). Live crops "
              "whatever a stamp lands on, and a survivor to the RIGHT of the "
              "stamp keeps its own loop_start, so it replays the loop from "
              "the top from that beat on — silently, and it sounds "
              "plausible. 'Vox Double' spans beats 0.0 to 24.0 (loop_start "
              "0.0) and would survive from beat 12.0 on with its loop "
              "restarted from the top; to end this stamp at that clip's end "
              "instead use destination_time 16.0, or cover the clip exactly "
              "with destination_time 0.0 and a 24.0-beat source. Nothing was "
              "changed. Delete or unloop the clip first, use one of the safe "
              "shapes above, or pass allow_loop_phase_reset=true to accept "
              "the re-phasing."),
    ]),
    # The same override on this side, so the footnote is frozen for both
    # stamps rather than only the one that happened to get a case.
    _case("duplicate_arrangement_clip", "loop_phase_reset_accepted",
          {"track_index": 0, "clip_index": 0, "destination_time": 4.0,
           "allow_loop_phase_reset": True}, [
        _ok("duplicate_arrangement_clip",
            {"track_index": 0, "clip_index": 0, "destination_time": 4.0,
             "allow_loop_phase_reset": True},
            {"clip_name": "Vox Take", "destination_time": 4.0,
             "source_start_time": 0.0, "source_end_time": 8.0,
             "stamp_start_time": 4.0, "stamp_end_time": 12.0,
             "loop_phase_reset": ["Vox Double"]}),
    ]),

    # ── Gated-path cases (plan PR10) ──────────────────────────────────────
    # These run the REAL gate against seeded handshake state; the blocked
    # ones freeze the installer message and prove nothing touches the wire.
    #
    # A legacy (pre-get_script_info) script never dispatched set_track_volume,
    # so the gate refuses it with the missing-capability installer message.
    _case("set_track_volume", "gated_legacy_script_blocked",
          {"track_index": 0, "value": 0.85}, [],
          script_info={"script_version": "legacy", "capabilities": []}),
    # 1.7.0 ADVERTISES set_track_volume, so the same newly-gated command
    # sails through the gate there and performs its normal exchange — the
    # PR10 flip takes nothing away from a 1.7.0 user.
    _case("set_track_volume", "gated_1_7_0_passes",
          {"track_index": 0, "value": 0.85}, [
        _ok("set_track_volume",
            {"track_index": 0, "value": 0.85, "track_type": "regular"},
            {"track_name": "Drums", "value": 0.85, "display_value": "0.0 dB"}),
    ], script_info={"script_version": "1.7.0",
                    "capabilities": SCRIPT_CAPABILITIES_1_7_0}),
    # 1.7.0 advertises the device-parameter pair but serves the broken
    # duplicate-definition handlers; the registry's min_script_version="1.8.0"
    # floor is what blocks them, with the message naming both versions.
    _case("set_device_parameter", "gated_1_7_0_min_version_blocked",
          {"track_index": 0, "device_index": 1, "parameter": "Dry/Wet",
           "value": 0.5}, [],
          script_info={"script_version": "1.7.0",
                       "capabilities": SCRIPT_CAPABILITIES_1_7_0}),
    _case("get_device_parameters", "gated_1_7_0_min_version_blocked",
          {"track_index": 0, "device_index": 1}, [],
          script_info={"script_version": "1.7.0",
                       "capabilities": SCRIPT_CAPABILITIES_1_7_0}),
]


def _generated_error_cases(base_cases):
    """One error case per tool: same args as the tool's first case, first
    send raises RuntimeError("boom"), freezing the tool's error string."""
    seen = []
    out = []
    for case in base_cases:
        tool = case["tool"]
        if tool in seen:
            continue
        seen.append(tool)
        first_step = case["wire"][0]
        out.append(_case(
            tool, "error", case["args"],
            [_boom(first_step["command"], first_step["params"])],
        ))
    return out


CASES = BASE_CASES + _generated_error_cases(BASE_CASES)

# All tools, in server-definition order of first appearance.
TOOLS = list(dict.fromkeys(case["tool"] for case in CASES))
