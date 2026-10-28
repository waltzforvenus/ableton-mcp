"""Behavior tests for the REAL Remote Script against the mock Ableton
(docs/REFACTOR_PLAN.md section 5 Level 3, section 6 PR4).

Every test drives the real ``AbletonMCP._process_command`` dispatch through
``fake_ableton.RemoteScriptHarness.process`` and asserts on the response
envelope and on the fake Live objects' mutated state. This level tests the
Remote Script half in isolation — ableton_mcp is deliberately not imported.

The device-parameter tests encode the fork behavior of that pair, which was
broken from the 2026-08 upstream merge (`4878234`, duplicate method
definitions in the class body) until the plan-PR5 repair; they landed as
strict xfails proving the breakage and now pass outright.

Runs anywhere: no Ableton, no network.
    uv run pytest -v
"""

import pytest

from fake_ableton import FakeClip, FakeDevice, FakeLiveConfig, make_harness


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
    # v3: the scoping flags landed and warp markers / rack chains stopped
    # being emitted unconditionally, so the same call returns a materially
    # different payload — a schema bump, not a cosmetic one.
    assert result["snapshot_schema"] == "ableton_mcp_snapshot_v3"


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
    # The resolved-track echo: every modifying result now names the track it
    # actually landed on, so a caller whose index went stale after a delete
    # can see it in the reply instead of discovering it in the mix.
    assert result == {"track_index": 1, "name": "Kicks",
                      "previous_name": "Drums"}
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
    # Measured, not echoed: `requested` is what the caller sent, `added` is
    # the before/after delta Live actually took. The old {"note_count": N}
    # printed the caller's own argument back and so said "Added 262 notes"
    # whether Live took 262 or none.
    assert added == {"track_index": 0, "track_name": "Lead",
                     "clip_index": 1, "clip_name": "",
                     "arrangement": False,
                     "requested": 2, "added": 2, "clip_note_count": 2}

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
    assert result == {"track_index": 0, "track_name": "Lead",
                      "clip_index": 0, "clip_name": "Lead Riff",
                      "arrangement": False,
                      "cleared_count": 3, "clip_note_count": 0}
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
    assert result == {"track_index": 0, "track_name": "Lead",
                      "clip_index": 0,
                      "deleted": True, "deleted_clip_name": "Lead Riff"}


def test_fire_clip_and_stop_clip():
    harness = make_harness()
    clip = harness.song.tracks[0].clip_slots[0].clip
    assert _ok(harness.process(_cmd("fire_clip", track_index=0,
                                    clip_index=0))) == {
        "track_index": 0, "track_name": "Lead",
        "clip_index": 0, "clip_name": "Lead Riff", "fired": True}
    assert clip.is_playing is True
    assert _ok(harness.process(_cmd("stop_clip", track_index=0,
                                    clip_index=0))) == {
        "track_index": 0, "track_name": "Lead",
        "clip_index": 0, "stopped": True}
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
    assert result == {"count_in_writable": True, "requested": "2 Bars",
                      "count_in_duration": 2, "count_in": "2 Bars",
                      "metronome": True}
    assert harness.song.count_in_duration == 2
    assert harness.song.metronome is True


def test_set_count_in_when_the_property_is_read_only():
    # Live 12.3+ exposes Song.count_in_duration read-only. The metronome half
    # is still writable and must be applied; the result reports the honest
    # partial outcome instead of failing outright.
    harness = make_harness(config=FakeLiveConfig(count_in_read_only=True))
    result = _ok(harness.process(_cmd("set_count_in", bars=2,
                                      metronome=True)))
    assert result == {"count_in_writable": False, "requested": "2 Bars",
                      "count_in_duration": 1, "count_in": "1 Bar",
                      "metronome": True}
    assert harness.song.count_in_duration == 1  # untouched
    assert harness.song.metronome is True       # still applied


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


# --------------------------------------------------------------------------
# The loop-phase guard — the session's worst correctness incident, mechanized
#
# Live crops whatever an Arrangement stamp lands on. Cropping a clip's TAIL
# keeps its start, so it keeps its phase; cropping its HEAD (or splitting it)
# leaves a right-hand survivor that still carries the clip's own loop_start,
# so from that beat on it replays the loop from the top. On a looping MIDI
# clip that is silent corruption that sounds plausible: three hat stamps
# scrambled bars 60-68, 156-164 and 204-208 and invented a crash nobody
# played, caught only by diffing a snapshot days later.
# --------------------------------------------------------------------------

def _looping_hats(harness, track_index=0, start_time=0.0, length=16.0):
    """Put one LOOPING arrangement clip on a track and return it."""
    track = harness.song.tracks[track_index]
    hats = FakeClip("Hats 8", length, midi=True, config=harness.config,
                    start_time=start_time)
    hats.looping = True
    hats.loop_start = 0.0
    hats.loop_end = 8.0
    track.arrangement_clips.append(hats)
    return hats


def test_stamp_is_refused_when_it_would_re_phase_a_looping_clip():
    harness = make_harness()
    _looping_hats(harness)

    # Beats 16-20 are free; the clip under the stamp is the one at 0-16.
    # Stamping at beat 4 lands strictly inside it, which splits it and
    # leaves a right-hand survivor from beat 8 on — the damaging shape.
    message = _err(harness.process(_cmd(
        "duplicate_session_clip_to_arrangement",
        track_index=0, clip_index=0, destination_time=4.0)))
    assert "Refusing this stamp at beat 4.0" in message
    assert "'Hats 8'" in message
    # The refusal names the two safe shapes and the override, because a
    # refusal the caller cannot act on just becomes a retry loop.
    assert "destination_time 12.0" in message   # end the stamp at the clip's end
    assert "destination_time 0.0" in message    # or cover it exactly
    assert "allow_loop_phase_reset=true" in message

    # "Nothing was changed" has to be literally true: the guard runs BEFORE
    # the stamp, so the timeline is untouched and the victim keeps its shape.
    clips = harness.song.tracks[0].arrangement_clips
    assert len(clips) == 1
    assert (clips[0].name, clips[0].start_time, clips[0].end_time) == (
        "Hats 8", 0.0, 16.0)


def test_stamp_is_allowed_over_a_non_looping_clip():
    # The guard is about LOOP PHASE, not about overlaps in general. The same
    # geometry over a non-looping clip has no phase to scramble, so it must
    # go through — a guard that refused this would make ordinary comping
    # impossible.
    harness = make_harness()
    hats = _looping_hats(harness)
    hats.looping = False

    result = _ok(harness.process(_cmd(
        "duplicate_session_clip_to_arrangement",
        track_index=0, clip_index=0, destination_time=4.0)))
    assert result["success"] is True
    assert "loop_phase_reset" not in result
    assert len(harness.song.tracks[0].arrangement_clips) == 3  # split + stamp


def test_stamp_over_a_looping_clips_tail_is_allowed():
    # The other half of the geometry: a stamp reaching PAST the victim's end
    # crops only its tail, so nothing survives to the right and nothing can
    # be re-phased. Refusing this would be a false positive.
    harness = make_harness()
    _looping_hats(harness, length=16.0)

    # Source clip is 4 beats, so a stamp at 14.0 covers 14-18 and the victim
    # (0-16) loses only its tail.
    result = _ok(harness.process(_cmd(
        "duplicate_session_clip_to_arrangement",
        track_index=0, clip_index=0, destination_time=14.0)))
    assert result["success"] is True
    assert "loop_phase_reset" not in result
    survivor = harness.song.tracks[0].arrangement_clips[0]
    assert (survivor.name, survivor.start_time, survivor.end_time) == (
        "Hats 8", 0.0, 14.0)


def test_allow_loop_phase_reset_stamps_anyway_and_says_what_it_re_phased():
    # The override exists so a caller who has decided the re-phasing is what
    # they want is not stuck. What it must NOT do is go quiet: the reply
    # names every clip whose phase was reset, so the decision is on the
    # record rather than something to rediscover in a snapshot diff.
    harness = make_harness()
    _looping_hats(harness)

    result = _ok(harness.process(_cmd(
        "duplicate_session_clip_to_arrangement",
        track_index=0, clip_index=0, destination_time=4.0,
        allow_loop_phase_reset=True)))
    assert result["success"] is True
    assert result["loop_phase_reset"] == ["Hats 8"]

    # And it really stamped: the victim was split around the 4.0-8.0 stamp.
    clips = harness.song.tracks[0].arrangement_clips
    assert [(c.name, c.start_time, c.end_time) for c in clips] == [
        ("Hats 8", 0.0, 4.0),
        ("Lead Riff", 4.0, 8.0),
        ("Hats 8", 8.0, 16.0),
    ]


def test_duplicate_arrangement_clip_carries_the_same_guard():
    # Identical LOM call, identical physics — and no batch form, so it only
    # ever refuses whole. Worth its own test because "the other stamp is
    # guarded" is exactly the assumption that would let a rewrite drop this.
    harness = make_harness()
    hats = _looping_hats(harness, track_index=2, start_time=16.0, length=16.0)

    message = _err(harness.process(_cmd(
        "duplicate_arrangement_clip", track_index=2, clip_index=0,
        destination_time=20.0)))
    assert "Refusing this stamp at beat 20.0" in message
    assert "'Hats 8'" in message
    assert (hats.start_time, hats.end_time) == (16.0, 32.0)

    # ...and the override reaches this handler too.
    result = _ok(harness.process(_cmd(
        "duplicate_arrangement_clip", track_index=2, clip_index=0,
        destination_time=20.0, allow_loop_phase_reset=True)))
    assert result["loop_phase_reset"] == ["Hats 8"]


def test_move_arrangement_clip_carries_the_same_guard():
    # move IS a stamp: it duplicates to the destination and deletes the
    # original, the identical LOM call the other two guard. Review caught this
    # one unguarded — duplicate_arrangement_clip refused a placement while
    # move performed the very same geometry and silently re-phased the victim.
    # One stamp site guarded and another not is the drift the shared check
    # exists to prevent, so this asserts move consults it too.
    harness = make_harness()
    mover = _looping_hats(harness, track_index=2, start_time=0.0, length=8.0)
    victim = _looping_hats(harness, track_index=2, start_time=16.0, length=16.0)

    message = _err(harness.process(_cmd(
        "move_arrangement_clip", track_index=2, clip_index=0,
        destination_time=20.0)))
    assert "Refusing this stamp at beat 20.0" in message
    # Refused means refused: the source is still where it was and the victim
    # is untouched. A guard that refused *after* moving would be worse than none.
    assert (mover.start_time, mover.end_time) == (0.0, 8.0)
    assert (victim.start_time, victim.end_time) == (16.0, 32.0)

    # ...and the override reaches this handler too.
    result = _ok(harness.process(_cmd(
        "move_arrangement_clip", track_index=2, clip_index=0,
        destination_time=20.0, allow_loop_phase_reset=True)))
    assert result["moved"] is True


def test_move_to_clear_timeline_is_not_refused():
    # The false-positive case. Moving into empty space must stay ordinary —
    # a guard that refused every move would be a worse bug than the one it fixes.
    harness = make_harness()
    mover = _looping_hats(harness, track_index=2, start_time=0.0, length=8.0)

    result = _ok(harness.process(_cmd(
        "move_arrangement_clip", track_index=2, clip_index=0,
        destination_time=64.0)))
    assert result["moved"] is True
    assert result["start_time"] == 64.0


# --------------------------------------------------------------------------
# Batched stamps — the partial-success path
# --------------------------------------------------------------------------

def test_batched_stamp_places_every_destination_in_one_call():
    harness = make_harness()
    result = _ok(harness.process(_cmd(
        "duplicate_session_clip_to_arrangement",
        track_index=0, clip_index=0, destination_times=[0.0, 8.0, 16.0])))
    assert result["success"] is True
    assert (result["requested_count"], result["placed_count"],
            result["failed_count"]) == (3, 3, 0)
    assert [row["ok"] for row in result["placements"]] == [True, True, True]
    assert [c.start_time
            for c in harness.song.tracks[0].arrangement_clips] == [0.0, 8.0, 16.0]


def test_batched_stamp_reports_a_partial_run_without_losing_what_landed():
    # The case the whole per-placement shape exists for. Placement 2 of 3 is
    # refused by the loop-phase guard; placements 1 and 3 are REAL and stay
    # on the timeline. Nothing raises, because a raise would throw away the
    # only record of what did land.
    harness = make_harness()
    _looping_hats(harness, start_time=8.0, length=16.0)  # spans beats 8-24

    result = _ok(harness.process(_cmd(
        "duplicate_session_clip_to_arrangement",
        track_index=0, clip_index=0, destination_times=[0.0, 12.0, 32.0])))

    # success is False the moment ANY placement fails — a half-landed batch
    # is not a success, and the caller must see that without reading rows.
    assert result["success"] is False
    assert (result["requested_count"], result["placed_count"],
            result["failed_count"]) == (3, 2, 1)

    # Each placement is guarded on its own geometry, and a refusal does not
    # abort the run: the 4-beat source lands clear at 0-4, is refused at
    # 12-16 (strictly inside the looping 8-24 clip), and lands again at
    # 32-36. A run that stopped at the first refusal would silently drop the
    # placement after it.
    rows = {row["destination_time"]: row for row in result["placements"]}
    assert rows[0.0]["ok"] is True and rows[0.0]["error"] is None
    assert rows[32.0]["ok"] is True and rows[32.0]["error"] is None
    assert rows[12.0]["ok"] is False
    assert "Refusing this stamp at beat 12.0" in rows[12.0]["error"]

    # What landed is REAL and on the timeline; what was refused left no
    # trace, and the victim is intact because the guard ran before the stamp.
    placed = [(c.name, c.start_time)
              for c in harness.song.tracks[0].arrangement_clips]
    assert ("Lead Riff", 0.0) in placed
    assert ("Lead Riff", 32.0) in placed
    assert ("Lead Riff", 12.0) not in placed
    assert ("Hats 8", 8.0) in placed
    hats = [c for c in harness.song.tracks[0].arrangement_clips
            if c.name == "Hats 8"]
    assert len(hats) == 1 and hats[0].end_time == 24.0


def test_batched_stamp_refuses_a_run_longer_than_the_cap():
    # The cap is not a style rule: the whole run happens inside one
    # main-thread task, where Live's UI and audio housekeeping live.
    harness = make_harness()
    over = [float(i) for i in range(harness.module.MAX_STAMP_PLACEMENTS + 1)]
    message = _err(harness.process(_cmd(
        "duplicate_session_clip_to_arrangement",
        track_index=0, clip_index=0, destination_times=over)))
    assert "Nothing was stamped" in message
    assert harness.song.tracks[0].arrangement_clips == []


def test_batched_stamp_refuses_an_empty_list_rather_than_succeeding_at_nothing():
    harness = make_harness()
    message = _err(harness.process(_cmd(
        "duplicate_session_clip_to_arrangement",
        track_index=0, clip_index=0, destination_times=[])))
    assert "nothing was stamped" in message.lower()


# --------------------------------------------------------------------------
# delete_device — verified, and deliberately not retried
# --------------------------------------------------------------------------

def test_delete_device_verifies_against_a_before_count():
    harness = make_harness()
    track = harness.song.tracks[0]
    track.devices.append(FakeDevice("Auto Pan", class_name="AutoPan"))
    assert [d.name for d in track.devices] == ["Operator", "Auto Pan"]

    result = _ok(harness.process(_cmd("delete_device", track_index=0,
                                      device_index=1)))
    assert result["deleted"] is True
    assert result["deleted_device_name"] == "Auto Pan"
    assert result["device_count_before"] == 2
    assert result["remaining_matches_expected"] is True
    # Names, not a bare count: a sweep walking a chain highest-index-first
    # can re-anchor on these when the ordinals renumber under it.
    assert result["remaining_devices"] == [{"index": 0, "name": "Operator"}]
    assert result["remaining_device_count"] == 1


def test_delete_device_reports_deleted_false_when_nothing_went():
    # The bug this replaced: the handler returned len(track.devices) with no
    # before-count, so a delete that did nothing still printed a plausible
    # "deleted X; N devices remain" — and a sweep built on that lie then
    # removed the wrong devices.
    harness = make_harness(config=FakeLiveConfig(device_delete_lands=False))
    track = harness.song.tracks[0]
    track.devices.append(FakeDevice("Auto Pan", class_name="AutoPan"))

    result = _ok(harness.process(_cmd("delete_device", track_index=0,
                                      device_index=1)))
    assert result["deleted"] is False
    assert result["device_count_before"] == 2
    assert result["remaining_device_count"] == 2
    assert result["remaining_matches_expected"] is False
    # Deliberately NO retry: if the read was merely stale rather than the
    # delete having failed, a blind second delete removes the NEXT device
    # and destroys a chain that was dialled in by hand. The chain is
    # untouched and the caller is told to look.
    assert [d.name for d in track.devices] == ["Operator", "Auto Pan"]


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


def test_jump_to_locator_while_stopped_plants_the_start_marker():
    harness = make_harness()
    result = _ok(harness.process(_cmd("jump_to_locator", name="Verse")))
    assert result == {"success": True, "name": "Verse", "time": 8.0,
                      "was_playing": False, "start_marker_set": True}
    assert harness.song.current_song_time == 8.0
    # The whole point of jump over set_current_song_time: the start marker
    # (where play/record launch from) moved too.
    assert harness.song.start_marker_time == 8.0


def test_jump_to_locator_matches_by_time_and_reports_playing_transport():
    harness = make_harness()
    harness.song.is_playing = True
    result = _ok(harness.process(_cmd("jump_to_locator", time=8.0)))
    assert result == {"success": True, "name": "Verse", "time": 8.0,
                      "was_playing": True, "start_marker_set": False}
    # Playing transport: playback relocated, start marker untouched.
    assert harness.song.current_song_time == 8.0
    assert harness.song.start_marker_time == 0.0


def test_jump_to_locator_with_no_match_lists_the_available_locators():
    harness = make_harness()
    message = _err(harness.process(_cmd("jump_to_locator", name="Bridge")))
    assert "No locator matches" in message
    assert "'Verse' at 8.0" in message


def test_jump_to_locator_with_no_criteria_asks_for_one():
    harness = make_harness()
    message = _err(harness.process(_cmd("jump_to_locator")))
    assert "Give a locator name or a beat time" in message
    assert "'Verse' at 8.0" in message


def test_trim_arrangement_clip_tail():
    harness = make_harness()
    result = _ok(harness.process(_cmd("trim_arrangement_clip", track_index=2,
                                      clip_index=0, end_time=6.0)))
    assert result == {"track_index": 2, "track_name": "Audio",
                      "clip_name": "Vox Take",
                      "start_time": 0.0, "end_time": 6.0,
                      "requested_start_time": 0.0, "requested_end_time": 6.0,
                      "trimmed_head": False, "trimmed_tail": True,
                      "refusals": []}
    track = harness.song.tracks[2]
    # No eraser stamp, no split-off shard left on the timeline.
    assert len(track.arrangement_clips) == 1
    clip = track.arrangement_clips[0]
    assert clip.start_time == 0.0 and clip.end_time == 6.0
    # The crop narrowed the content window — real editing, not bookkeeping.
    assert abs(clip.end_marker - 6.0) < 1e-6
    # The temporary eraser Session clip is gone again.
    assert all(not slot.has_clip for slot in track.clip_slots)


def test_trim_arrangement_clip_head():
    harness = make_harness()
    result = _ok(harness.process(_cmd("trim_arrangement_clip", track_index=2,
                                      clip_index=0, start_time=2.0)))
    assert result["trimmed_head"] is True
    assert result["trimmed_tail"] is False
    assert result["refusals"] == []
    track = harness.song.tracks[2]
    assert len(track.arrangement_clips) == 1
    clip = track.arrangement_clips[0]
    assert abs(clip.start_time - 2.0) < 1e-6
    assert clip.end_time == 8.0
    assert abs(clip.start_marker - 2.0) < 1e-6
    assert all(not slot.has_clip for slot in track.clip_slots)


def test_trim_arrangement_clip_both_edges_on_a_midi_track():
    # MIDI tracks build their eraser with ClipSlot.create_clip — no audio
    # file involved — and must leave the occupied Session slots alone.
    harness = make_harness()
    track = harness.song.tracks[0]
    track.arrangement_clips.append(
        FakeClip("Lead Take", 8.0, midi=True, config=harness.config,
                 start_time=0.0))
    result = _ok(harness.process(_cmd("trim_arrangement_clip", track_index=0,
                                      clip_index=0, start_time=1.0,
                                      end_time=5.0)))
    assert result["trimmed_head"] is True and result["trimmed_tail"] is True
    assert result["refusals"] == []
    assert result["start_time"] == 1.0 and result["end_time"] == 5.0
    assert len(track.arrangement_clips) == 1
    clip = track.arrangement_clips[0]
    assert clip.start_time == 1.0 and clip.end_time == 5.0
    # Slot 0 still holds Lead Riff; the eraser borrowed a free slot only.
    assert track.clip_slots[0].clip.name == "Lead Riff"
    assert all(not slot.has_clip for slot in track.clip_slots[1:])


def test_trim_arrangement_clip_unwarped_audio_keeps_second_markers():
    # Unwarped audio keeps its content markers in seconds; the overlap crop
    # must narrow them in seconds too (0.5 s per beat at the fake's 120 BPM).
    harness = make_harness()
    clip = harness.song.tracks[2].arrangement_clips[0]
    clip.warping = False
    clip.end_marker = 4.0  # 8 beats * 0.5 s
    result = _ok(harness.process(_cmd("trim_arrangement_clip", track_index=2,
                                      clip_index=0, end_time=6.0)))
    assert result["trimmed_tail"] is True
    assert abs(clip.end_time - 6.0) < 1e-6
    # 2 trimmed beats = 1.0 s of content window.
    assert abs(clip.end_marker - 3.0) < 1e-6


def test_trim_arrangement_clip_trims_looping_clips():
    # The crop is Live's own overlap handling, which treats looping clips
    # exactly as the UI does — no unloop-first refusal any more.
    harness = make_harness()
    clip = harness.song.tracks[2].arrangement_clips[0]
    clip.looping = True
    result = _ok(harness.process(_cmd("trim_arrangement_clip", track_index=2,
                                      clip_index=0, end_time=6.0)))
    assert result["trimmed_tail"] is True
    assert clip.end_time == 6.0


def test_trim_arrangement_clip_micro_trim_overhangs_into_clear_timeline():
    # A region narrower than the eraser's footprint is fine as long as the
    # timeline past the take is empty: the stamp may overhang into it.
    harness = make_harness()
    result = _ok(harness.process(_cmd("trim_arrangement_clip", track_index=2,
                                      clip_index=0, end_time=7.95)))
    assert result["trimmed_tail"] is True
    track = harness.song.tracks[2]
    assert len(track.arrangement_clips) == 1
    assert abs(track.arrangement_clips[0].end_time - 7.95) <= 1e-3


def test_trim_arrangement_clip_refuses_an_unsafe_micro_trim():
    # The same micro-trim with a neighbouring take butted against the clip:
    # the stamp could overrun into the neighbour, so the edge is refused
    # BEFORE anything is modified.
    harness = make_harness()
    track = harness.song.tracks[2]
    track.arrangement_clips.append(
        FakeClip("Next Take", 8.0, midi=False, config=harness.config,
                 start_time=8.0))
    result = _ok(harness.process(_cmd("trim_arrangement_clip", track_index=2,
                                      clip_index=0, end_time=7.95)))
    assert result["trimmed_tail"] is False
    assert len(result["refusals"]) == 1
    assert "safety zone" in result["refusals"][0]
    # Neither clip changed, and nothing was left behind.
    assert [(c.start_time, c.end_time) for c in track.arrangement_clips] \
        == [(0.0, 8.0), (8.0, 16.0)]
    assert all(not slot.has_clip for slot in track.clip_slots)


def test_trim_arrangement_clip_with_nothing_to_trim():
    harness = make_harness()
    result = _ok(harness.process(_cmd("trim_arrangement_clip", track_index=2,
                                      clip_index=0, start_time=0.0,
                                      end_time=8.0)))
    assert result == {"track_index": 2, "track_name": "Audio",
                      "clip_name": "Vox Take",
                      "start_time": 0.0, "end_time": 8.0,
                      "requested_start_time": 0.0, "requested_end_time": 8.0,
                      "trimmed_head": False, "trimmed_tail": False,
                      "refusals": []}
    assert len(harness.song.tracks[2].arrangement_clips) == 1


def test_trim_arrangement_clip_refuses_outward_trims():
    harness = make_harness()
    message = _err(harness.process(_cmd("trim_arrangement_clip", track_index=2,
                                        clip_index=0, end_time=10.0)))
    assert "inward" in message


def test_trim_arrangement_clip_needs_an_empty_session_slot():
    harness = make_harness()
    track = harness.song.tracks[2]
    for slot in track.clip_slots:
        slot.clip = FakeClip("filler", 4.0, midi=False, config=harness.config)
    message = _err(harness.process(_cmd("trim_arrangement_clip", track_index=2,
                                        clip_index=0, end_time=6.0)))
    assert "Session slot" in message
    clip = harness.song.tracks[2].arrangement_clips[0]
    assert clip.start_time == 0.0 and clip.end_time == 8.0


def test_trim_arrangement_clip_audio_needs_the_live_12_0_5_api():
    harness = make_harness(config=FakeLiveConfig(create_audio_clip_api=False))
    message = _err(harness.process(_cmd("trim_arrangement_clip", track_index=2,
                                        clip_index=0, end_time=6.0)))
    assert "12.0.5" in message
    clip = harness.song.tracks[2].arrangement_clips[0]
    assert clip.start_time == 0.0 and clip.end_time == 8.0


def test_trim_arrangement_clip_refuses_without_the_live_11_api():
    harness = make_harness(config=FakeLiveConfig(track_delete_clip_api=False))
    message = _err(harness.process(_cmd("trim_arrangement_clip", track_index=2,
                                        clip_index=0, end_time=6.0)))
    assert "Live 11" in message
    clip = harness.song.tracks[2].arrangement_clips[0]
    assert clip.start_time == 0.0 and clip.end_time == 8.0


def test_delete_arrangement_clip():
    harness = make_harness()
    result = _ok(harness.process(_cmd("delete_arrangement_clip",
                                      track_index=2, clip_index=0)))
    # matched_by records WHICH key resolved the clip: clip_index is the
    # positional ordinal that renumbers on every delete, start_time is the
    # stable one (Live permits no overlaps, so it is a genuine unique key).
    assert result == {"track_index": 2, "track_name": "Audio",
                      "clip_index": 0, "matched_by": "clip_index",
                      "deleted": True, "deleted_clip_name": "Vox Take",
                      "start_time": 0.0, "end_time": 8.0,
                      "arrangement_clip_count": 0}
    assert harness.song.tracks[2].arrangement_clips == []


def test_delete_arrangement_clip_refuses_without_the_live_11_api():
    harness = make_harness(config=FakeLiveConfig(track_delete_clip_api=False))
    message = _err(harness.process(_cmd("delete_arrangement_clip",
                                        track_index=2, clip_index=0)))
    assert "Live 11" in message
    assert len(harness.song.tracks[2].arrangement_clips) == 1


def test_move_arrangement_clip_duplicates_then_deletes():
    harness = make_harness()
    result = _ok(harness.process(_cmd("move_arrangement_clip", track_index=2,
                                      clip_index=0, destination_time=16.0)))
    assert result == {"track_index": 2, "track_name": "Audio",
                      "clip_name": "Vox Take", "start_time": 16.0,
                      "end_time": 24.0, "moved": True}
    clips = harness.song.tracks[2].arrangement_clips
    # One clip: the copy at the destination; the original is gone.
    assert len(clips) == 1
    assert clips[0].start_time == 16.0 and clips[0].end_time == 24.0
    assert clips[0].name == "Vox Take"


def test_move_arrangement_clip_to_its_own_position_is_a_noop():
    harness = make_harness()
    result = _ok(harness.process(_cmd("move_arrangement_clip", track_index=2,
                                      clip_index=0, destination_time=0.0)))
    assert result["moved"] is False
    assert len(harness.song.tracks[2].arrangement_clips) == 1


def test_move_arrangement_clip_refuses_a_self_overlapping_destination():
    harness = make_harness()
    message = _err(harness.process(_cmd("move_arrangement_clip", track_index=2,
                                        clip_index=0, destination_time=4.0)))
    assert "overlaps the clip's own span" in message
    clip = harness.song.tracks[2].arrangement_clips[0]
    assert clip.start_time == 0.0 and clip.end_time == 8.0


def test_duplicate_arrangement_clip_reuses_a_take_elsewhere():
    harness = make_harness()
    result = _ok(harness.process(_cmd("duplicate_arrangement_clip",
                                      track_index=2, clip_index=0,
                                      destination_time=16.0)))
    # The stamp reports its own footprint and what it landed on top of:
    # overlapped_clips is empty here (bar 16 was free) and clips_in_span_after
    # is the after-picture the loop-phase guard is built to protect.
    assert result == {"track_index": 2, "track_name": "Audio",
                      "clip_name": "Vox Take", "destination_time": 16.0,
                      "source_start_time": 0.0, "source_end_time": 8.0,
                      "stamp_start_time": 16.0, "stamp_end_time": 24.0,
                      "overlapped_clips": [],
                      "clips_in_span_after": [
                          {"name": "Vox Take", "start_time": 16.0,
                           "end_time": 24.0, "looping": False,
                           "loop_start": 0.0, "loop_end": 8.0}]}
    clips = harness.song.tracks[2].arrangement_clips
    assert len(clips) == 2
    assert clips[0].start_time == 0.0
    assert clips[1].start_time == 16.0 and clips[1].end_time == 24.0


def test_create_locator_retries_a_swallowed_transport_write():
    harness = make_harness(config=FakeLiveConfig(transport_write_lag="once"))
    result = _ok(harness.process(_cmd("create_locator", name="Drop",
                                      time=16.0)))
    assert result["time"] == 16.0 and result["name"] == "Drop"
    # No stray cue was toggled at the stale playhead position.
    assert {(c.name, c.time) for c in harness.song.cue_points} == {
        ("Verse", 8.0), ("Drop", 16.0)}


def test_create_locator_refuses_without_toggling_when_transport_never_settles():
    harness = make_harness(config=FakeLiveConfig(transport_write_lag="always"))
    message = _err(harness.process(_cmd("create_locator", name="Drop",
                                        time=16.0)))
    assert "did not settle" in message
    # The refusal IS the safety: a wrong-position toggle would corrupt.
    assert {(c.name, c.time) for c in harness.song.cue_points} == {
        ("Verse", 8.0)}


def test_switch_to_arrangement_view():
    harness = make_harness()
    result = _ok(harness.process(_cmd("switch_to_arrangement_view")))
    assert result == {"view": "Arranger"}
    assert harness.app.view.shown == ["Arranger"]


def test_set_current_song_time():
    harness = make_harness()
    result = _ok(harness.process(_cmd("set_current_song_time", time=32.0)))
    assert result == {"current_song_time": 32.0, "requested": 32.0,
                      "settled": True}
    assert harness.song.current_song_time == 32.0


def test_set_current_song_time_retries_when_the_transport_swallows_a_write():
    # Right after stop_playback, Live is still resetting the transport and
    # can overwrite the first playhead write. The handler must notice the
    # mismatch on read-back and write once more.
    harness = make_harness()
    song = harness.song

    class _Settling(type(song)):
        @property
        def current_song_time(self):
            return self._t
        @current_song_time.setter
        def current_song_time(self, value):
            if getattr(self, "_swallow_once", False):
                self._swallow_once = False  # the stop-reset wins this write
                return
            self._t = value

    song.__class__ = _Settling
    song._t = 20.8
    song._swallow_once = True
    result = _ok(harness.process(_cmd("set_current_song_time", time=0.0)))
    assert result == {"current_song_time": 0.0, "requested": 0.0,
                      "settled": True}
    assert song.current_song_time == 0.0


def test_set_current_song_time_reports_a_stale_read_back_honestly():
    # Verified on Live 12.4.3: during the tick after stop_playback the write
    # lands, but read-backs keep returning the stopping position until the
    # next tick. The handler must report settled=False rather than echoing
    # the stale value as if it were the outcome.
    harness = make_harness()
    song = harness.song

    class _StaleReads(type(song)):
        @property
        def current_song_time(self):
            return 5.2  # the stopping position, for the whole tick
        @current_song_time.setter
        def current_song_time(self, value):
            self._written = value  # lands, but is not visible this tick

    song.__class__ = _StaleReads
    result = _ok(harness.process(_cmd("set_current_song_time", time=0.0)))
    assert result == {"current_song_time": 5.2, "requested": 0.0,
                      "settled": False}
    assert song._written == 0.0  # the write itself did land


# --------------------------------------------------------------------------
# Session snapshot
# --------------------------------------------------------------------------

def test_get_session_snapshot_smoke():
    harness = make_harness()
    result = _ok(harness.process(_cmd("get_session_snapshot",
                                      include_notes=True,
                                      include_params=True)))
    assert result["schema"] == "ableton_mcp_snapshot_v3"
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
                      "track_name": "Drums", "uri": "query:Synths#Operator",
                      "new_devices": ["Operator"],
                      "devices_after": ["Impulse", "Operator"]}
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
