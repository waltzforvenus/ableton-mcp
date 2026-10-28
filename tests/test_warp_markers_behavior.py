"""Remote Script behavior for the warp-marker commands (script 1.17.0),
driven through the real dispatch against the fake LOM.

What the fake models, from the LOM docs (Live 11.0.5+ Clip API):
``warp_markers`` read-only pairs (beat_time in clip beats, sample_time in
file seconds); ``add_warp_marker`` takes a WarpMarker object and refuses
positions that break monotonic order, leave the file, or make a segment
slower than 5 / faster than 999 BPM; ``move_warp_marker`` and
``remove_warp_marker`` are "if possible" — an impossible request is a
silent no-op, which is exactly what the batch command's read-back
verification exists to catch.

No Ableton, no network, no numpy.
"""

from fake_ableton import FakeLiveConfig, FakeWarpMarker, make_harness


def _cmd(command_type, **params):
    return {"type": command_type, "params": params}


def _ok(response):
    assert response["status"] == "success", response
    return response["result"]


def _err(response):
    assert response["status"] == "error", response
    return response["message"]


def _warp_harness(**config):
    """Standard song; track 2's Arrangement clip "Vox Take" (8 beats, 4 s at
    the fake's 120 BPM) carries markers at beats 0 and 4, plus the hidden
    end marker at beat 8."""
    harness = make_harness(config=FakeLiveConfig(warp_markers=True, **config))
    clip = harness.song.tracks[2].arrangement_clips[0]
    clip.warp_markers.append(FakeWarpMarker(8.0, 4.0))
    return harness, clip


def _pairs(clip):
    return [(m.beat_time, m.sample_time) for m in clip.warp_markers]


def _edit(harness, edits, **extra):
    return harness.process(_cmd("edit_clip_warp_markers", track_index=2,
                                clip_index=0, edits=edits, **extra))


# --------------------------------------------------------------------------
# get_clip_warp_markers
# --------------------------------------------------------------------------

def test_get_clip_warp_markers_reports_markers_and_mapping_context():
    harness, clip = _warp_harness()
    result = _ok(harness.process(_cmd("get_clip_warp_markers",
                                      track_index=2, clip_index=0)))
    assert result["clip_name"] == "Vox Take"
    assert result["warp_markers"] == [
        {"beat_time": 0.0, "sample_time": 0.0},
        {"beat_time": 4.0, "sample_time": 2.0},
        {"beat_time": 8.0, "sample_time": 4.0},
    ]
    assert result["warp_marker_count"] == 3
    assert result["warping"] is True
    assert result["marker_units"] == "beats"
    assert result["sample_rate"] == 44100.0
    assert result["sample_seconds"] == 4.0
    assert result["file_path"] == clip.file_path
    assert result["song_tempo"] == 120.0
    assert result["signature_numerator"] == 4
    assert result["start_time"] == 0.0
    assert result["warp_marker_api"] == {
        "add_warp_marker": True, "move_warp_marker": True,
        "remove_warp_marker": True}


def test_get_clip_warp_markers_unwarped_says_seconds():
    harness, clip = _warp_harness()
    clip.warping = False
    result = _ok(harness.process(_cmd("get_clip_warp_markers",
                                      track_index=2, clip_index=0)))
    assert result["warping"] is False
    assert result["marker_units"] == "seconds"


def test_get_clip_warp_markers_refuses_midi_clip():
    harness, _ = _warp_harness()
    message = _err(harness.process(_cmd(
        "get_clip_warp_markers", track_index=0, clip_index=0,
        arrangement=False)))
    assert "audio clips only" in message


def test_get_clip_warp_markers_honours_track_name_guard():
    harness, _ = _warp_harness()
    message = _err(harness.process(_cmd(
        "get_clip_warp_markers", track_index=2, clip_index=0,
        expect_track_name="Bass")))
    assert "Bass" in message


def test_get_clip_warp_markers_on_a_build_without_marker_api():
    # Live 11.0.0 reads markers but cannot edit them: the probe says so up
    # front, so nobody builds a proposal they cannot apply.
    harness, _ = _warp_harness(warp_marker_edit_api=False)
    result = _ok(harness.process(_cmd("get_clip_warp_markers",
                                      track_index=2, clip_index=0)))
    assert result["warp_marker_api"] == {
        "add_warp_marker": False, "move_warp_marker": False,
        "remove_warp_marker": False}


# --------------------------------------------------------------------------
# edit_clip_warp_markers — the happy path
# --------------------------------------------------------------------------

def test_batch_add_move_remove_is_applied_and_verified():
    harness, clip = _warp_harness()
    result = _ok(_edit(harness, [
        {"op": "add", "beat_time": 2.0, "sample_time": 0.98},
        {"op": "move", "beat_time": 4.0, "new_beat_time": 4.25},
        {"op": "add", "beat_time": 6.0, "sample_time": 3.0},
        {"op": "remove", "beat_time": 6.0},
    ]))
    assert result["verified"] is True
    assert result["applied"] == result["requested"] == 4
    assert result["failed"] == result["not_attempted"] == 0
    assert result["mismatches"] == []
    assert _pairs(clip) == [(0.0, 0.0), (2.0, 0.98), (4.25, 2.0), (8.0, 4.0)]
    assert result["warp_markers_before"][1] == {"beat_time": 4.0,
                                                "sample_time": 2.0}
    assert result["warp_markers_after"] == [
        {"beat_time": b, "sample_time": s} for b, s in _pairs(clip)]
    # The move reports where the marker went and that its audio stayed put.
    move = result["results"][1]
    assert move == {"index": 1, "op": "move", "ok": True, "beat_time": 4.0,
                    "new_beat_time": 4.25, "sample_time": 2.0}


def test_move_by_distance():
    harness, clip = _warp_harness()
    result = _ok(_edit(harness, [
        {"op": "move", "beat_time": 4.0, "distance": -0.5}]))
    assert result["verified"] is True
    assert _pairs(clip)[1] == (3.5, 2.0)


def test_move_and_remove_tolerate_a_hair_of_beat_rounding():
    # A caller typing 4 for a marker Live holds at 4.0000001 still hits it.
    harness, clip = _warp_harness()
    clip.warp_markers[1].beat_time = 4.0000001
    result = _ok(_edit(harness, [{"op": "remove", "beat_time": 4.0}]))
    assert result["verified"] is True
    assert _pairs(clip) == [(0.0, 0.0), (8.0, 4.0)]


def test_add_without_sample_time_pins_what_currently_plays():
    # Live's beat_to_sample_time returns FRAMES; the handler converts to
    # seconds with the clip's rate, as Max for Live does.
    harness, clip = _warp_harness()
    result = _ok(_edit(harness, [{"op": "add", "beat_time": 6.0}]))
    assert result["verified"] is True
    assert result["results"][0]["sample_time"] == 3.0
    assert (6.0, 3.0) in _pairs(clip)


def test_session_clip_addressing():
    harness, _ = _warp_harness()
    from fake_ableton import FakeClip
    track = harness.song.tracks[2]
    session = FakeClip("Loop", 8.0, midi=False, config=harness.config)
    track.clip_slots[1].clip = session
    result = _ok(harness.process(_cmd(
        "edit_clip_warp_markers", track_index=2, clip_index=1,
        arrangement=False,
        edits=[{"op": "move", "beat_time": 4.0, "new_beat_time": 3.75}])))
    assert result["verified"] is True
    assert result["arrangement"] is False
    assert _pairs(session)[1] == (3.75, 2.0)


# --------------------------------------------------------------------------
# edit_clip_warp_markers — refusals and silent no-ops
# --------------------------------------------------------------------------

def test_a_refused_edit_stops_the_batch_and_reports_what_did_not_happen():
    harness, clip = _warp_harness()
    result = _ok(_edit(harness, [
        {"op": "add", "beat_time": 2.0, "sample_time": 1.0},
        # sample time 2.5 at beat 3 crosses the beat-4 marker at 2.0 s
        {"op": "add", "beat_time": 3.0, "sample_time": 2.5},
        {"op": "remove", "beat_time": 4.0},
    ]))
    assert result["verified"] is False
    assert result["applied"] == 1
    assert result["failed"] == 1
    assert result["not_attempted"] == 1
    assert result["results"][1]["ok"] is False
    assert "Invalid warp marker" in result["results"][1]["error"]
    # Nothing after the refusal ran: the beat-4 marker is still there.
    assert (4.0, 2.0) in _pairs(clip)
    assert (2.0, 1.0) in _pairs(clip)


def test_stop_on_error_false_runs_the_rest():
    harness, clip = _warp_harness()
    result = _ok(_edit(harness, [
        {"op": "add", "beat_time": 3.0, "sample_time": 2.5},
        {"op": "remove", "beat_time": 4.0},
    ], stop_on_error=False))
    assert result["failed"] == 1
    assert result["applied"] == 1
    assert result["not_attempted"] == 0
    assert (4.0, 2.0) not in _pairs(clip)


def test_moving_a_marker_that_does_not_exist_is_an_error_not_a_noop():
    harness, clip = _warp_harness()
    result = _ok(_edit(harness, [
        {"op": "move", "beat_time": 5.0, "new_beat_time": 5.5}]))
    assert result["results"][0]["ok"] is False
    assert "No warp marker at beat 5" in result["results"][0]["error"]
    assert "nearest is at 4" in result["results"][0]["error"]


def test_silent_noop_edits_are_caught_by_read_back_verification():
    # Live's edit calls are documented "if possible": nothing raises, the
    # call just does not land. Only the read-back can tell.
    harness, clip = _warp_harness(warp_edit_lands=False)
    result = _ok(_edit(harness, [
        {"op": "move", "beat_time": 4.0, "new_beat_time": 4.5},
        {"op": "add", "beat_time": 6.0, "sample_time": 3.1},
    ]))
    assert result["applied"] == 2  # every call returned normally...
    assert result["verified"] is False  # ...and none of it is there
    beats = sorted(m["beat_time"] for m in result["mismatches"])
    assert beats == [4.0, 4.5, 6.0]
    assert _pairs(clip) == [(0.0, 0.0), (4.0, 2.0), (8.0, 4.0)]


def test_an_impossible_move_is_silent_in_live_and_flagged_here():
    # Moving beat 4 past the beat-8 marker breaks monotonic order: the fake
    # (like Live, "if possible") ignores it without raising.
    harness, clip = _warp_harness()
    result = _ok(_edit(harness, [
        {"op": "move", "beat_time": 4.0, "new_beat_time": 9.0}]))
    assert result["results"][0]["ok"] is True
    assert result["verified"] is False
    assert _pairs(clip) == [(0.0, 0.0), (4.0, 2.0), (8.0, 4.0)]


def test_malformed_batches_are_refused_before_anything_runs():
    harness, clip = _warp_harness()
    before = _pairs(clip)
    for edits, phrase in (
        ([], "non-empty list"),
        ([{"op": "stretch", "beat_time": 1.0}], "op must be add, move or remove"),
        ([{"op": "remove"}], "beat_time is required"),
        ([{"op": "remove", "beat_time": 0.0},
          {"op": "move", "beat_time": 4.0}], "new_beat_time or distance"),
        ([{"op": "remove", "beat_time": 4.0}] * 257, "At most 256"),
    ):
        assert phrase in _err(_edit(harness, edits))
        assert _pairs(clip) == before


def test_unwarped_clip_is_refused():
    harness, clip = _warp_harness()
    clip.warping = False
    assert "not warped" in _err(_edit(
        harness, [{"op": "remove", "beat_time": 4.0}]))


def test_missing_edit_api_names_the_live_version():
    harness, _ = _warp_harness(warp_marker_edit_api=False)
    message = _err(_edit(harness, [{"op": "remove", "beat_time": 4.0}]))
    assert "remove_warp_marker" in message
    assert "11.0.5" in message


def test_midi_clip_is_refused():
    harness, _ = _warp_harness()
    message = _err(harness.process(_cmd(
        "edit_clip_warp_markers", track_index=0, clip_index=0,
        arrangement=False, edits=[{"op": "remove", "beat_time": 0.0}])))
    assert "audio clips only" in message


def test_edit_runs_on_the_main_thread_and_is_advertised():
    harness, _ = _warp_harness()
    module = harness.module
    assert module.COMMANDS["edit_clip_warp_markers"][1] is True
    assert module.COMMANDS["get_clip_warp_markers"][1] is False
    assert "edit_clip_warp_markers" in module.SCRIPT_CAPABILITIES
    assert "get_clip_warp_markers" in module.SCRIPT_CAPABILITIES
    _ok(_edit(harness, [{"op": "remove", "beat_time": 4.0}]))
    assert harness.scheduled, "warp edits must be scheduled onto Live's main thread"
