"""insert_device / move_device / sidechain routing (M2) and arrangement-
native clip creation (M1) through the real Remote Script on the fake LOM.

Track.insert_device, Song.move_device, a device's input routing and
Track.create_audio_clip / create_midi_clip are documented in Live 12.4.7's
binary and never called by this fork before; the fake models the
documented shapes, which are UNVERIFIED until the live pass.
"""

import wave

from fake_ableton import FakeClip, FakeDevice, FakeSidechainDevice, make_harness


def _run(harness, command, **params):
    return harness.process({"type": command, "params": params})


def _ok(resp):
    assert resp["status"] == "success", resp
    return resp["result"]


def test_insert_device_at_a_position():
    harness = make_harness()
    track = harness.song.tracks[0]
    names_before = [d.name for d in track.devices]
    result = _ok(_run(harness, "insert_device", track_index=0, device_name="EQ Eight",
                      index=0))
    assert result["device_index"] == 0 and result["devices_after"][0] == "EQ Eight"
    assert result["devices_after"][1:] == names_before


def test_insert_device_refuses_without_the_api():
    harness = make_harness()
    del harness.song.tracks[0].insert_device
    resp = _run(harness, "insert_device", track_index=0, device_name="EQ Eight")
    assert resp["status"] == "error" and "Track.insert_device" in resp["message"]


def test_move_device_within_and_across_tracks():
    harness = make_harness()
    a, b = harness.song.tracks[0], harness.song.tracks[1]
    a.devices = [FakeDevice("Amp"), FakeDevice("Cabinet"), FakeDevice("Tuner")]
    b.devices = [FakeDevice("Reverb")]
    moved = _ok(_run(harness, "move_device", track_index=0, device_index=2, to_index=0))
    assert moved["to_index"] == 0 and moved["devices_after"] == ["Tuner", "Amp", "Cabinet"]
    across = _ok(_run(harness, "move_device", track_index=0, device_index=2,
                      to_index=1, to_track_index=1))
    assert across["to_track"] == b.name and across["devices_after"] == ["Reverb", "Cabinet"]
    assert across["source_devices_after"] == ["Tuner", "Amp"]


def test_sidechain_read_set_and_switch_on():
    harness = make_harness()
    comp = FakeSidechainDevice()
    harness.song.tracks[1].devices = [comp]
    state = _ok(_run(harness, "get_device_sidechain", track_index=1, device_index=0))
    assert state["supported"] is True and state["source"] == "No Input"
    assert "1-Kick" in state["available_sources"] and state["enabled"] is False
    done = _ok(_run(harness, "set_device_sidechain", track_index=1, device_index=0,
                    source="kick", channel="Pre FX"))
    assert done["source"] == "1-Kick" and done["channel"] == "Pre FX"
    assert done["enabled"] is True


def test_sidechain_refusals():
    harness = make_harness()
    harness.song.tracks[1].devices = [FakeSidechainDevice(sources=("No Input", "1-Kick", "3-Kick 2")),
                                      FakeDevice("Reverb")]
    ambiguous = _run(harness, "set_device_sidechain", track_index=1, device_index=0,
                     source="Kick")
    assert ambiguous["status"] == "error" and "several" in ambiguous["message"]
    missing = _run(harness, "set_device_sidechain", track_index=1, device_index=0,
                   source="Snare")
    assert "Available" in missing["message"]
    plain = _ok(_run(harness, "get_device_sidechain", track_index=1, device_index=1))
    assert plain["supported"] is False


def _wav(path):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00" * 4410)
    return str(path)


def test_audio_clip_lands_on_the_arrangement(tmp_path):
    harness = make_harness()
    track = harness.song.tracks[1]
    track.arrangement_clips = []
    path = _wav(tmp_path / "take.wav")
    result = _ok(_run(harness, "create_arrangement_audio_clip", track_index=1,
                      path=path, start_time=32.0, name="Take A"))
    assert result["start_time"] == 32.0 and result["clip_name"] == "Take A"
    assert result["file_path"] == path and result["cropped_or_replaced"] == []


def test_audio_clip_overlap_is_refused_unless_allowed(tmp_path):
    harness = make_harness()
    track = harness.song.tracks[1]
    track.arrangement_clips = [FakeClip("Old", 16.0, midi=False, start_time=24.0)]
    path = _wav(tmp_path / "take.wav")
    refused = _run(harness, "create_arrangement_audio_clip", track_index=1,
                   path=path, start_time=32.0)
    assert refused["status"] == "error" and "allow_overlap" in refused["message"]
    assert [c.name for c in track.arrangement_clips] == ["Old"]
    allowed = _ok(_run(harness, "create_arrangement_audio_clip", track_index=1,
                       path=path, start_time=32.0, allow_overlap=True))
    assert allowed["cropped_or_replaced"][0]["name"] == "Old"
    bad = _run(harness, "create_arrangement_audio_clip", track_index=1,
               path="relative.wav", start_time=0.0)
    assert "absolute" in bad["message"]


def test_midi_clip_with_notes():
    harness = make_harness()
    track = harness.song.tracks[0]
    track.arrangement_clips = []
    result = _ok(_run(harness, "create_arrangement_midi_clip", track_index=0,
                      start_time=16.0, length=4.0, name="Hook",
                      notes=[{"pitch": 60, "start_time": 0.0, "duration": 1.0},
                             {"pitch": 64, "start_time": 1.0, "duration": 1.0}]))
    assert (result["start_time"], result["end_time"]) == (16.0, 20.0)
    assert result["clip_name"] == "Hook" and result["notes_added"] == 2
    overlap = _run(harness, "create_arrangement_midi_clip", track_index=0,
                   start_time=18.0, length=4.0)
    assert overlap["status"] == "error" and "overlap" in overlap["message"]


def test_arrangement_creation_refuses_without_the_api(tmp_path):
    harness = make_harness()
    track = harness.song.tracks[0]
    del track.create_midi_clip
    resp = _run(harness, "create_arrangement_midi_clip", track_index=0,
                start_time=0.0, length=4.0)
    assert resp["status"] == "error" and "create_midi_clip" in resp["message"]
