"""Golden-case definitions for all 46 MCP tools (docs/REFACTOR_PLAN.md
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

Pure data — no imports, so it is loadable from both pytest and the recorder
script.
"""


def _case(tool, name, args, wire):
    return {"tool": tool, "name": name, "args": args, "wire": wire}


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
            "script_version": "1.7.0",
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
        _ok("get_clip_notes", {"track_index": 0, "clip_index": 2}, {
            "clip_name": "Bassline",
            "length": 4.0,
            "note_count": 2,
            "notes": NOTES,
        }),
    ]),

    # ── get_session_snapshot (gated) ──────────────────────────────────────
    # Default args must reach the wire as include_notes/include_params True.
    _case("get_session_snapshot", "success_defaults", {}, [
        _ok("get_session_snapshot",
            {"include_notes": True, "include_params": True}, {
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
            {"include_notes": False, "include_params": False}, {
                "tempo": 120.0,
                "track_count": 1,
                "tracks": [{"index": 0, "name": "1-MIDI"}],
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

    # ── delete_device ─────────────────────────────────────────────────────
    _case("delete_device", "success", {"track_index": 0, "device_index": 2}, [
        _ok("delete_device",
            {"track_index": 0, "device_index": 2, "track_type": "regular"},
            {"deleted_device_name": "Compressor",
             "remaining_device_count": 2}),
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
            {"name": "loop", "length": 16.0}),
    ]),

    # ── add_notes_to_clip ─────────────────────────────────────────────────
    _case("add_notes_to_clip", "success",
          {"track_index": 0, "clip_index": 0, "notes": NOTES}, [
        _ok("add_notes_to_clip",
            {"track_index": 0, "clip_index": 0, "notes": NOTES},
            {"note_count": 2}),
    ]),

    # ── clear_notes_from_clip (gated) ─────────────────────────────────────
    _case("clear_notes_from_clip", "success",
          {"track_index": 0, "clip_index": 0}, [
        _ok("clear_notes_from_clip", {"track_index": 0, "clip_index": 0},
            {"clip_name": "Beat", "cleared_count": 5}),
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

    # ── create_locator (gated) ────────────────────────────────────────────
    _case("create_locator", "success", {"name": "Chorus", "time": 16.0}, [
        _ok("create_locator", {"name": "Chorus", "time": 16.0},
            {"name": "Chorus", "time": 16.0}),
    ]),
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
