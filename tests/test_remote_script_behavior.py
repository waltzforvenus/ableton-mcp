"""Behavior tests for the REAL Remote Script against the mock Ableton
(docs/REFACTOR_PLAN.md section 5 Level 3, section 6 PR4).

Every test drives the real ``AbletonMCP._process_command`` dispatch through
``fake_ableton.RemoteScriptHarness.process`` and asserts on the response
envelope and on the fake Live objects' mutated state. This level tests the
Remote Script half in isolation — MCP_Server is deliberately not imported.

The device-parameter tests encode the fork behavior of that pair, which was
broken from the 2026-08 upstream merge (`4878234`, duplicate method
definitions in the class body) until the plan-PR5 repair; they landed as
strict xfails proving the breakage and now pass outright.

Runs anywhere: no Ableton, no network.
    uv run pytest -v
"""

import pytest

from fake_ableton import FakeLiveConfig, make_harness


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _cmd(command_type, **params):
    return {"type": command_type, "params": params}


def _ok(response):
    """Assert a success envelope and return its result payload."""
    assert response["status"] == "success", response
    return response["result"]


def _err(response):
    """Assert an error envelope and return its message."""
    assert response["status"] == "error", response
    return response["message"]


def _base_note(note):
    return {key: note[key]
            for key in ("pitch", "start_time", "duration", "velocity", "mute")}


# --------------------------------------------------------------------------
# Device parameters — the fork behavior (track_type, name-or-index, clamping)
# restored by plan PR5 after the 2026-08 merge broke it
# --------------------------------------------------------------------------

def test_get_device_parameters_on_return_track():
    harness = make_harness()
    result = _ok(harness.process(_cmd(
        "get_device_parameters",
        track_index=0, device_index=0, track_type="return")))
    assert result["track_name"] == "A Reverb"
    assert result["device_name"] == "Reverb"
    assert result["parameter_count"] == 3
    names = [p["name"] for p in result["parameters"]]
    assert names == ["Device On", "Dry/Wet", "Decay Time"]
    # The fork version reports Live's UI display string per parameter.
    assert all("display_value" in p for p in result["parameters"])
    assert all("is_quantized" in p for p in result["parameters"])


def test_set_device_parameter_by_name_clamps_out_of_range():
    harness = make_harness()
    result = _ok(harness.process(_cmd(
        "set_device_parameter",
        track_index=0, device_index=0, parameter="Filter Freq", value=9999.0)))
    assert result["parameter_name"] == "Filter Freq"
    assert result["clamped"] is True
    assert result["value"] == 127.0
    assert result["max"] == 127.0
    # The fake parameter was really assigned the clamped value.
    param = harness.song.tracks[0].devices[0].parameters[1]
    assert param.value == 127.0


def test_set_device_parameter_by_index_default_track_type():
    harness = make_harness()
    result = _ok(harness.process(_cmd(
        "set_device_parameter",
        track_index=0, device_index=0, parameter=1, value=30.0)))
    assert result["parameter_name"] == "Filter Freq"
    assert result["value"] == 30.0
    assert result["clamped"] is False
    assert harness.song.tracks[0].devices[0].parameters[1].value == 30.0


def test_set_device_parameter_reports_old_value():
    # Grafted from upstream's (otherwise discarded) version during the PR5
    # repair: the result reports the value the write overwrote.
    harness = make_harness()
    param = harness.song.tracks[0].devices[0].parameters[1]
    assert param.value == 60.0  # the fake's initial Filter Freq
    result = _ok(harness.process(_cmd(
        "set_device_parameter",
        track_index=0, device_index=0, parameter="Filter Freq", value=30.0)))
    assert result["old_value"] == 60.0
    assert result["value"] == 30.0
    assert param.value == 30.0


# --------------------------------------------------------------------------
# Handshake and session reads
# --------------------------------------------------------------------------

def test_get_script_info():
    harness = make_harness()
    result = _ok(harness.process(_cmd("get_script_info")))
    assert result["name"] == "AbletonMCP"
    assert result["script_version"] == harness.module.SCRIPT_VERSION
    assert result["protocol_version"] == harness.module.PROTOCOL_VERSION
    assert result["capabilities"] == list(harness.module.SCRIPT_CAPABILITIES)
    assert result["snapshot_schema"] == "ableton_mcp_snapshot_v2"


def test_get_session_info():
    harness = make_harness()
    result = _ok(harness.process(_cmd("get_session_info")))
    assert result == {
        "tempo": 120.0,
        "signature_numerator": 4,
        "signature_denominator": 4,
        "track_count": 3,
        "return_track_count": 1,
        "master_track": {"name": "Master", "volume": 0.85, "panning": 0.0},
        "is_playing": False,
        "current_song_time": 0.0,
        "song_length": 32.0,
        "loop": False,
        "loop_start": 0.0,
        "loop_length": 16.0,
    }


def test_get_track_info():
    harness = make_harness()
    result = _ok(harness.process(_cmd("get_track_info", track_index=0)))
    assert result["index"] == 0
    assert result["name"] == "Lead"
    assert result["is_midi_track"] is True
    assert result["is_audio_track"] is False
    assert result["volume"] == 0.85
    assert len(result["clip_slots"]) == 4
    slot0 = result["clip_slots"][0]
    assert slot0["has_clip"] is True
    assert slot0["clip"]["name"] == "Lead Riff"
    assert slot0["clip"]["length"] == 4.0
    assert result["clip_slots"][2] == {"index": 2, "has_clip": False, "clip": None}
    assert result["devices"] == [
        {"index": 0, "name": "Operator", "class_name": "Operator",
         "type": "unknown"},
    ]


# --------------------------------------------------------------------------
# Track creation and naming — FakeSong mutation is the proof
# --------------------------------------------------------------------------

def test_create_midi_track_appends_to_song():
    harness = make_harness()
    result = _ok(harness.process(_cmd("create_midi_track", index=-1)))
    assert len(harness.song.tracks) == 4
    new_track = harness.song.tracks[3]
    assert new_track.has_midi_input is True
    assert result == {"index": 3, "name": new_track.name}


def test_create_audio_track_appends_to_song():
    harness = make_harness()
    result = _ok(harness.process(_cmd("create_audio_track", index=-1)))
    assert len(harness.song.tracks) == 4
    new_track = harness.song.tracks[3]
    assert new_track.has_audio_input is True
    assert new_track.has_midi_input is False
    assert result == {"index": 3, "name": new_track.name}


def test_create_return_track_appends_to_song():
    harness = make_harness()
    result = _ok(harness.process(_cmd("create_return_track")))
    assert len(harness.song.return_tracks) == 2
    assert result == {
        "return_index": 1,
        "name": harness.song.return_tracks[1].name,
        "return_track_count": 2,
    }


def test_set_track_name():
    harness = make_harness()
    result = _ok(harness.process(_cmd("set_track_name", track_index=1,
                                      name="Kicks")))
    assert result == {"name": "Kicks"}
    assert harness.song.tracks[1].name == "Kicks"


# --------------------------------------------------------------------------
# Notes — round-trip on both note-API generations
# (writes are always legacy set_notes; reads prefer get_notes_extended)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("note_api", ["legacy", "both"])
def test_create_clip_add_notes_get_notes_round_trip(note_api):
    config = FakeLiveConfig(note_api=note_api,
                            extended_note_fields=(note_api == "both"))
    harness = make_harness(config=config)

    created = _ok(harness.process(_cmd("create_clip", track_index=0,
                                       clip_index=1, length=4.0)))
    assert created["length"] == 4.0
    assert harness.song.tracks[0].clip_slots[1].has_clip is True

    notes = [
        {"pitch": 60, "start_time": 0.0, "duration": 0.5, "velocity": 100,
         "mute": False},
        {"pitch": 67, "start_time": 1.0, "duration": 0.25, "velocity": 90,
         "mute": True},
    ]
    added = _ok(harness.process(_cmd("add_notes_to_clip", track_index=0,
                                     clip_index=1, notes=notes)))
    assert added == {"note_count": 2}

    read = _ok(harness.process(_cmd("get_clip_notes", track_index=0,
                                    clip_index=1)))
    assert read["note_count"] == 2
    assert read["length"] == 4.0
    assert [_base_note(n) for n in read["notes"]] == [
        {"pitch": 60, "start_time": 0.0, "duration": 0.5, "velocity": 100.0,
         "mute": False},
        {"pitch": 67, "start_time": 1.0, "duration": 0.25, "velocity": 90.0,
         "mute": True},
    ]
    if note_api == "both":
        # Extended reads surface the optional per-field-hasattr attributes.
        assert all("note_id" in n for n in read["notes"])
    else:
        assert all("note_id" not in n for n in read["notes"])


@pytest.mark.parametrize("note_api", ["legacy", "both"])
def test_clear_notes_from_clip_on_both_note_apis(note_api):
    harness = make_harness(config=FakeLiveConfig(note_api=note_api))
    result = _ok(harness.process(_cmd("clear_notes_from_clip", track_index=0,
                                      clip_index=0)))
    assert result == {"track_index": 0, "clip_index": 0,
                      "clip_name": "Lead Riff", "cleared_count": 3}
    assert harness.song.tracks[0].clip_slots[0].clip.stored_notes == []


def test_add_notes_breaks_on_a_new_api_only_live():
    # Faithful to the real constraint: the script writes via legacy
    # set_notes unconditionally, so a Live exposing only the extended
    # note API breaks add_notes_to_clip.
    harness = make_harness(config=FakeLiveConfig(note_api="extended_only"))
    _ok(harness.process(_cmd("create_clip", track_index=0, clip_index=1,
                             length=4.0)))
    message = _err(harness.process(_cmd(
        "add_notes_to_clip", track_index=0, clip_index=1,
        notes=[{"pitch": 60, "start_time": 0.0, "duration": 1.0,
                "velocity": 100, "mute": False}])))
    assert "set_notes" in message


def test_extended_read_failure_falls_back_to_legacy_get_notes():
    # The extended read path try/excepts into the legacy API; the fake can
    # raise from get_notes_extended to exercise exactly that branch.
    harness = make_harness(config=FakeLiveConfig(note_api="both",
                                                 extended_read_raises=True))
    read = _ok(harness.process(_cmd("get_clip_notes", track_index=0,
                                    clip_index=0)))
    assert read["note_count"] == 3
    assert [n["pitch"] for n in read["notes"]] == [60, 64, 67]
    assert any("falling back" in line for line in harness.logs)


# --------------------------------------------------------------------------
# Clip lifecycle
# --------------------------------------------------------------------------

def test_delete_clip_current_semantics():
    # The PR5 repair kept upstream's semantics: an occupied slot deletes and
    # reports deleted true; an already-empty slot is a no-op reporting
    # deleted false, not an error.
    harness = make_harness()
    occupied = _ok(harness.process(_cmd("delete_clip", track_index=0,
                                        clip_index=0)))
    assert occupied["deleted"] is True
    assert harness.song.tracks[0].clip_slots[0].has_clip is False

    empty = _ok(harness.process(_cmd("delete_clip", track_index=0,
                                     clip_index=0)))
    assert empty["deleted"] is False
    assert "empty" in empty["reason"]


def test_delete_clip_echoes_the_deleted_clip_name():
    # The fork's name echo, restored onto upstream's version by the PR5
    # repair: read before deletion, reported after.
    harness = make_harness()
    result = _ok(harness.process(_cmd("delete_clip", track_index=0,
                                      clip_index=0)))
    assert result == {"deleted": True, "deleted_clip_name": "Lead Riff"}


def test_fire_clip_and_stop_clip():
    harness = make_harness()
    clip = harness.song.tracks[0].clip_slots[0].clip
    assert _ok(harness.process(_cmd("fire_clip", track_index=0,
                                    clip_index=0))) == {"fired": True}
    assert clip.is_playing is True
    assert _ok(harness.process(_cmd("stop_clip", track_index=0,
                                    clip_index=0))) == {"stopped": True}
    assert clip.is_playing is False


def test_start_and_stop_playback():
    harness = make_harness()
    assert _ok(harness.process(_cmd("start_playback"))) == {"playing": True}
    assert harness.song.is_playing is True
    assert _ok(harness.process(_cmd("stop_playback"))) == {"playing": False}
    assert harness.song.is_playing is False


def test_set_tempo():
    harness = make_harness()
    assert _ok(harness.process(_cmd("set_tempo", tempo=92.5))) == {"tempo": 92.5}
    assert harness.song.tempo == 92.5


# --------------------------------------------------------------------------
# Mixer
# --------------------------------------------------------------------------

def test_set_track_volume_clamps_and_reports_display():
    harness = make_harness()
    result = _ok(harness.process(_cmd("set_track_volume", track_index=0,
                                      value=1.5)))
    assert result["field"] == "volume"
    assert result["requested"] == 1.5
    assert result["value"] == 1.0
    assert result["clamped"] is True
    assert result["display_value"] == "1.00 dB"
    assert harness.song.tracks[0].mixer_device.volume.value == 1.0


def test_set_track_pan():
    harness = make_harness()
    result = _ok(harness.process(_cmd("set_track_pan", track_index=0,
                                      value=-0.25)))
    assert result["field"] == "panning"
    assert result["value"] == -0.25
    assert result["clamped"] is False
    assert harness.song.tracks[0].mixer_device.panning.value == -0.25


def test_set_track_mute():
    harness = make_harness()
    result = _ok(harness.process(_cmd("set_track_mute", track_index=0,
                                      value=True)))
    assert result == {"track_index": 0, "track_name": "Lead", "mute": True}
    assert harness.song.tracks[0].mute is True


def test_set_track_send_reports_display_value():
    harness = make_harness()
    result = _ok(harness.process(_cmd("set_track_send", track_index=0,
                                      send_index=0, value=0.7)))
    assert result["send_index"] == 0
    assert result["value"] == 0.7
    assert result["clamped"] is False
    assert result["display_value"] == "0.70 dB"
    assert harness.song.tracks[0].mixer_device.sends[0].value == 0.7


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------

def test_get_track_routing():
    harness = make_harness()
    result = _ok(harness.process(_cmd("get_track_routing", track_index=0)))
    assert result["track_name"] == "Lead"
    assert result["output_routing_type"] == "Main"
    assert result["available_output_routing_types"] == ["Main", "Sends Only"]
    assert result["input_routing_type"] == "No Input"
    assert result["available_input_routing_types"] == ["No Input", "Resampling"]


def test_set_track_routing_matches_display_name_case_insensitively():
    harness = make_harness()
    result = _ok(harness.process(_cmd("set_track_routing", track_index=0,
                                      field="output_routing_type",
                                      target="sends only")))
    assert result["value"] == "Sends Only"
    assert harness.song.tracks[0].output_routing_type.display_name == "Sends Only"


# --------------------------------------------------------------------------
# Count-in and save
# --------------------------------------------------------------------------

def test_set_count_in_with_metronome():
    harness = make_harness()
    result = _ok(harness.process(_cmd("set_count_in", bars=2, metronome=True)))
    assert result == {"count_in_duration": 2, "count_in": "2 Bars",
                      "metronome": True}
    assert harness.song.count_in_duration == 2
    assert harness.song.metronome is True


def test_set_count_in_when_the_property_is_read_only():
    # Live 12.3.2 exposes Song.count_in_duration read-only; the failure must
    # be reported, not swallowed.
    harness = make_harness(config=FakeLiveConfig(count_in_read_only=True))
    message = _err(harness.process(_cmd("set_count_in", bars=2)))
    assert "no setter" in message
    assert harness.song.count_in_duration == 1  # untouched


@pytest.mark.parametrize("owner", ["song", "application"])
def test_save_set_with_a_candidate_present(owner):
    harness = make_harness(config=FakeLiveConfig(save_owner=owner))
    result = _ok(harness.process(_cmd("save_set")))
    assert result["saved"] is True
    assert result["method"] == "%s.save_set()" % owner
    saver = harness.song if owner == "song" else harness.app
    assert saver.save_calls == ["save_set"]


def test_save_set_with_no_candidates():
    harness = make_harness(config=FakeLiveConfig(save_owner=None))
    result = _ok(harness.process(_cmd("save_set")))
    assert result["saved"] is False
    assert result["method"] is None
    assert "saved from the UI" in result["message"]


# --------------------------------------------------------------------------
# Arrangement
# --------------------------------------------------------------------------

def test_get_arrangement_clips():
    harness = make_harness()
    result = _ok(harness.process(_cmd("get_arrangement_clips", track_index=2)))
    assert result["track_name"] == "Audio"
    assert result["clip_count"] == 1
    clip = result["clips"][0]
    assert clip["name"] == "Vox Take"
    assert clip["start_time"] == 0.0
    assert clip["end_time"] == 8.0
    assert clip["is_audio_clip"] is True
    assert clip["gain"] == 0.5
    assert clip["gain_display"] == "0.0 dB"


def test_duplicate_session_clip_to_arrangement():
    harness = make_harness()
    result = _ok(harness.process(_cmd(
        "duplicate_session_clip_to_arrangement",
        track_index=0, clip_index=0, destination_time=16.0)))
    assert result["success"] is True
    assert result["clip_name"] == "Lead Riff"
    assert result["destination_time"] == 16.0
    track = harness.song.tracks[0]
    assert len(track.arrangement_clips) == 1
    duplicate = track.arrangement_clips[0]
    assert duplicate.start_time == 16.0
    assert duplicate.end_time == 20.0
    # The Session clip stays where it was.
    assert track.clip_slots[0].clip.name == "Lead Riff"


def test_create_locator_creates_and_restores_the_playhead():
    harness = make_harness()
    result = _ok(harness.process(_cmd("create_locator", name="Drop",
                                      time=16.0)))
    assert result == {"success": True, "time": 16.0, "name": "Drop"}
    assert len(harness.song.cue_points) == 2
    assert {(c.name, c.time) for c in harness.song.cue_points} == {
        ("Verse", 8.0), ("Drop", 16.0)}
    # The playhead was moved to toggle the cue, then restored.
    assert harness.song.current_song_time == 0.0


def test_create_locator_renames_an_existing_cue_at_the_same_time():
    harness = make_harness()
    result = _ok(harness.process(_cmd("create_locator", name="Chorus",
                                      time=8.0)))
    assert result == {"success": True, "time": 8.0, "name": "Chorus"}
    # Renamed in place, not toggled away or duplicated.
    assert len(harness.song.cue_points) == 1
    assert harness.song.cue_points[0].name == "Chorus"


def test_switch_to_arrangement_view():
    harness = make_harness()
    result = _ok(harness.process(_cmd("switch_to_arrangement_view")))
    assert result == {"view": "Arranger"}
    assert harness.app.view.shown == ["Arranger"]


def test_set_current_song_time():
    harness = make_harness()
    result = _ok(harness.process(_cmd("set_current_song_time", time=32.0)))
    assert result == {"current_song_time": 32.0}
    assert harness.song.current_song_time == 32.0


# --------------------------------------------------------------------------
# Session snapshot
# --------------------------------------------------------------------------

def test_get_session_snapshot_smoke():
    harness = make_harness()
    result = _ok(harness.process(_cmd("get_session_snapshot",
                                      include_notes=True,
                                      include_params=True)))
    assert result["schema"] == "ableton_mcp_snapshot_v2"
    assert result["session"]["tempo"] == 120.0
    assert [t["name"] for t in result["tracks"]] == ["Lead", "Drums", "Audio"]

    lead = result["tracks"][0]
    assert lead["sends"] == [{"index": 0, "value": 0.0, "name": "Send A"}]
    slot0 = lead["clip_slots"][0]
    assert slot0["has_clip"] is True
    clip = slot0["clip"]
    assert clip["name"] == "Lead Riff"
    assert clip["is_midi_clip"] is True
    assert clip["note_count"] == 3
    assert [n["pitch"] for n in clip["notes"]] == [60, 64, 67]

    device = lead["devices"][0]
    assert device["name"] == "Operator"
    param_names = [p["name"] for p in device["parameters"]]
    assert param_names == ["Device On", "Filter Freq", "Resonance"]
    for param in device["parameters"]:
        assert {"index", "name", "value", "min", "max", "is_enabled",
                "is_quantized"} <= set(param)

    audio = result["tracks"][2]
    assert audio["arrangement_clips"][0]["name"] == "Vox Take"

    assert result["return_tracks"][0]["name"] == "A Reverb"
    assert result["return_tracks"][0]["devices"][0]["name"] == "Reverb"
    assert result["master_track"]["volume"] == 0.85
    assert result["cue_points"] == [{"name": "Verse", "time": 8.0}]
    assert len(result["scenes"]) == 4


# --------------------------------------------------------------------------
# Audio clips — the Live 12.0.5 ClipSlot.create_audio_clip gate
# --------------------------------------------------------------------------

def test_create_audio_clip_with_the_api_present():
    harness = make_harness()
    result = _ok(harness.process(_cmd("create_audio_clip", track_index=2,
                                      clip_index=0, path="/tmp/loop.wav")))
    assert result["name"] == "loop"
    assert result["is_audio_clip"] is True
    slot = harness.song.tracks[2].clip_slots[0]
    assert slot.has_clip is True
    assert slot.clip.file_path == "/tmp/loop.wav"


def test_create_audio_clip_with_the_api_absent():
    harness = make_harness(config=FakeLiveConfig(create_audio_clip_api=False))
    message = _err(harness.process(_cmd("create_audio_clip", track_index=2,
                                        clip_index=0, path="/tmp/loop.wav")))
    assert "12.0.5" in message
    assert harness.song.tracks[2].clip_slots[0].has_clip is False


# --------------------------------------------------------------------------
# Browser
# --------------------------------------------------------------------------

def test_get_browser_tree():
    harness = make_harness()
    result = _ok(harness.process(_cmd("get_browser_tree", category_type="all")))
    names = [c["name"] for c in result["categories"]]
    assert names == ["Instruments", "Sounds", "Drums", "Audio Effects",
                     "MIDI Effects"]
    instruments = result["categories"][0]
    assert instruments["is_folder"] is True
    assert instruments["uri"] == "query:Instruments"
    assert "instruments" in result["available_categories"]


def test_get_browser_items_at_path():
    harness = make_harness()
    result = _ok(harness.process(_cmd("get_browser_items_at_path",
                                      path="instruments/Synths")))
    assert result["name"] == "Synths"
    assert result["is_folder"] is True
    assert [item["name"] for item in result["items"]] == ["Operator",
                                                          "Wavetable"]
    assert all(item["is_loadable"] for item in result["items"])
    assert all(item["is_device"] for item in result["items"])


def test_load_browser_item_loads_onto_the_selected_track():
    harness = make_harness()
    result = _ok(harness.process(_cmd("load_browser_item", track_index=1,
                                      item_uri="query:Synths#Operator",
                                      track_type="regular")))
    assert result == {"loaded": True, "item_name": "Operator",
                      "track_name": "Drums", "uri": "query:Synths#Operator"}
    drums = harness.song.tracks[1]
    assert harness.song.view.selected_track is drums
    assert [d.name for d in drums.devices] == ["Impulse", "Operator"]
    assert len(harness.app.browser.loads) == 1
    loaded_item, loaded_track = harness.app.browser.loads[0]
    assert loaded_item.name == "Operator"
    assert loaded_track is drums


# --------------------------------------------------------------------------
# Envelope contract
# --------------------------------------------------------------------------

def test_unknown_command_returns_the_error_envelope():
    harness = make_harness()
    message = _err(harness.process(
        {"type": "definitely_not_a_command", "params": {}}))
    assert message == "Unknown command: definitely_not_a_command"


def test_handler_exception_returns_a_status_error_envelope():
    harness = make_harness()
    message = _err(harness.process(_cmd("get_track_info", track_index=99)))
    assert message == "Track index out of range"


def test_schedule_message_assertion_falls_back_to_direct_execution():
    # In Live, schedule_message asserts when the caller is already on the
    # main thread; _process_command then runs the task directly. Prove the
    # fallback by making schedule_message raise and observing the command
    # still succeed.
    harness = make_harness(schedule_raises=True)
    result = _ok(harness.process(_cmd("set_tempo", tempo=140.0)))
    assert result == {"tempo": 140.0}
    assert harness.song.tempo == 140.0
    assert len(harness.scheduled) == 1  # schedule_message was attempted
