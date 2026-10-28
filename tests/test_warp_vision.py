"""Warp vision: the audio analysis, the orchestration, and the whole
analyze -> propose -> apply -> verify loop, on synthetic audio whose
ground truth is known to the sample.

Three levels:

1. ``audio_analysis`` on its own — clicks at known times, a drifting drum
   take, a band-split signal, a sine sweep;
2. ``warp_vision`` + the four tools against a scripted wire, including the
   MCP image content the image tools return;
3. the full stack: a real FastMCP session, the real Remote Script on the
   fake LOM, and a real WAV on disk — the agent workflow end to end.

Needs the ``analysis`` extra (installed by ``--extra dev``); skipped
cleanly without it. No Ableton, no network.
"""

import asyncio
import io
import json
import math
import struct
import wave
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
sf = pytest.importorskip("soundfile")
PIL_Image = pytest.importorskip("PIL.Image")

from mcp.server.fastmcp import Image as McpImage  # noqa: E402
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402
from mcp.types import ImageContent, TextContent  # noqa: E402

from ableton_mcp import audio_analysis as aa  # noqa: E402
from ableton_mcp import warp_vision  # noqa: E402
from ableton_mcp.app import Deps, build_app  # noqa: E402
from ableton_mcp.handshake import ScriptHandshake  # noqa: E402
from ableton_mcp.services import AbletonService  # noqa: E402
import ableton_mcp.tools as tools  # noqa: E402
from fake_ableton import FakeLiveConfig, FakeWarpMarker, make_harness  # noqa: E402
from golden_runner import AllCapabilitiesHandshake, FakeWireClient, make_ctx  # noqa: E402

SR = 44100


# ── Synthetic audio ──────────────────────────────────────────────────────────

def _kick(sr=SR):
    t = np.arange(int(0.15 * sr)) / sr
    return (np.sin(2 * np.pi * (50 + 80 * np.exp(-t * 30)) * t)
            * np.exp(-t * 12)).astype(np.float32)


def _hat(rng, sr=SR):
    n = int(0.04 * sr)
    return (rng.standard_normal(n) * np.exp(-np.arange(n) / sr * 90)
            ).astype(np.float32)


def _click(sr=SR):
    """A 3 ms decaying 2 kHz burst: sharp, broadband enough to detect."""
    t = np.arange(int(0.003 * sr)) / sr
    return (np.sin(2 * np.pi * 2000 * t) * np.exp(-t * 800)).astype(np.float32)


def _place(x, sound, t, gain=1.0, sr=SR):
    n = int(round(t * sr))
    end = min(len(x), n + len(sound))
    x[n:end] += gain * sound[:end - n]


def write_clicks(path, times, duration, sr=SR):
    x = np.zeros(int(duration * sr), np.float32)
    for t in times:
        _place(x, _click(sr), t, 0.8, sr)
    sf.write(str(path), x, sr)
    return path


def write_drums(path, bpm=123.0, duration=16.0, start=0.03, wobble=0.004,
                sr=SR):
    """Kicks on the beat, hats on the off-beat, a 220 Hz hum, and a slow
    tempo wobble — a take Live's straight-120 Auto-Warp would mangle.
    Returns (path, kick_times)."""
    rng = np.random.default_rng(0)
    x = np.zeros(int(duration * sr), np.float32)
    spb = 60.0 / bpm
    kicks = []
    k = 0
    while True:
        t = start + k * spb * (1 + wobble * math.sin(k / 5.0))
        if t > duration - 0.3:
            break
        kicks.append(t)
        _place(x, _kick(sr), t, 0.8, sr)
        _place(x, _hat(rng, sr), t + spb / 2, 0.25, sr)
        k += 1
    x += 0.05 * np.sin(2 * np.pi * 220 * np.arange(len(x)) / sr).astype(np.float32)
    sf.write(str(path), np.stack([x, x], axis=1), sr)
    return path, kicks


# ── 1. audio_analysis ────────────────────────────────────────────────────────

def test_clicks_are_found_at_their_attacks(tmp_path):
    times = [0.25, 0.731, 1.2, 1.9055, 2.5, 3.3333]
    path = write_clicks(tmp_path / "clicks.wav", times, 4.0)
    an = aa.analyse(str(path), (0.0, 4.0))
    found = [t.time for t in an.transients]
    assert len(found) == len(times)
    for want, got in zip(times, found):
        assert abs(got - want) < 0.001, (want, got)


def test_kick_attacks_are_sub_millisecond_and_classified_low(tmp_path):
    # A 60 Hz kick ripples its own 1 ms envelope at every zero crossing; an
    # attack search that walks back from the peak lands ~4 ms late.
    path, kicks = write_drums(tmp_path / "drums.wav")
    an = aa.analyse(str(path), (0.0, 16.0))
    found = np.array([t.time for t in an.transients])
    for k in kicks:
        assert np.min(np.abs(found - k)) < 0.001
    near = {round(k, 3): min(an.transients, key=lambda t: abs(t.time - k))
            for k in kicks}
    assert all(t.band == "low" for t in near.values())
    hats = [t for t in an.transients
            if min(abs(t.time - k) for k in kicks) > 0.1]
    assert hats and all(t.band != "low" for t in hats)


def test_tempo_estimate_and_phase_follow_the_kicks(tmp_path):
    path, kicks = write_drums(tmp_path / "drums.wav", wobble=0.0, bpm=128.0)
    an = aa.analyse(str(path), (0.0, 16.0), prior_bpm=120.0)
    assert an.tempo is not None
    assert abs(an.tempo.bpm - 128.0) < 0.6
    assert an.tempo.confidence > 2.0
    # The phase lands on the kicks (low band), not the brighter off-beat hats.
    phase_err = min(abs(b - k) for b in an.tempo.beats_between(0, 16)
                    for k in kicks[:4])
    assert phase_err < 0.02


def test_band_energy_separates_low_and_high(tmp_path):
    t = np.arange(int(4.0 * SR)) / SR
    x = np.where(t < 2.0, np.sin(2 * np.pi * 80 * t),
                 np.sin(2 * np.pi * 6000 * t)).astype(np.float32) * 0.5
    path = tmp_path / "split.wav"
    sf.write(str(path), x, SR)
    an = aa.analyse(str(path), (0.0, 4.0), band_points=8)
    low, high = an.bands["low_db"], an.bands["high_db"]
    assert low[1] - high[1] > 30  # first half: 80 Hz
    assert high[-2] - low[-2] > 30  # second half: 6 kHz
    assert len(an.bands["times"]) == 8


def test_sine_sweep_spectrogram_rises(tmp_path):
    # Log sweep 100 Hz -> 8 kHz: the spectrogram's loudest bin must climb.
    dur = 3.0
    t = np.arange(int(dur * SR)) / SR
    f0, f1 = 100.0, 8000.0
    k = math.log(f1 / f0) / dur
    x = (0.5 * np.sin(2 * np.pi * f0 * (np.exp(k * t) - 1) / k)).astype(np.float32)
    audio = aa.AudioWindow(samples=x[::2].copy(), sr=SR // 2, start=0.0,
                           file_duration=dur, native_sr=SR, channels=1)
    mag = aa.stft_magnitude(audio.samples)
    peaks = mag.argmax(axis=1)
    quarter = len(peaks) // 4
    assert peaks[quarter] < peaks[2 * quarter] < peaks[3 * quarter]
    freq = peaks[2 * quarter] * (SR / 2) / aa.N_FFT
    assert abs(freq - f0 * math.exp(k * dur / 2)) / (f0 * math.exp(k * dur / 2)) < 0.1


def test_stdlib_wav_fallback_without_soundfile(tmp_path, monkeypatch):
    # 24-bit PCM through the wave module, as when soundfile is missing.
    path = tmp_path / "pcm24.wav"
    frames = np.zeros(SR, np.float32)
    _place(frames, _click(), 0.5, 0.8)
    ints = (frames * (2 ** 23 - 1)).astype(np.int32)
    raw = b"".join(struct.pack("<i", int(v))[:3] for v in ints)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(3)
        w.setframerate(SR)
        w.writeframes(raw)
    import builtins
    real_import = builtins.__import__

    def no_soundfile(name, *args, **kwargs):
        if name == "soundfile":
            raise ImportError("no soundfile")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_soundfile)
    an = aa.analyse(str(path), (0.0, 1.0))
    assert [round(t.time, 3) for t in an.transients] == [0.5]


def test_beat_map_round_trips_and_extrapolates():
    bm = aa.BeatMap([(0.0, 0.1), (4.0, 2.1), (8.0, 4.3)], 0.5)
    for beat in (-1.0, 0.0, 1.5, 4.0, 6.0, 8.0, 10.0):
        assert abs(bm.seconds_to_beat(bm.beat_to_seconds(beat)) - beat) < 1e-9
    assert bm.beat_to_seconds(2.0) == pytest.approx(1.1)
    assert bm.beat_to_seconds(10.0) == pytest.approx(4.3 + 2 * 0.55)
    assert bm.bpm_at(1.0) == pytest.approx(120.0)
    assert bm.bpm_at(3.0) == pytest.approx(60 / 0.55)
    single = aa.BeatMap([(0.0, 1.0)], 0.5)
    assert single.beat_to_seconds(4.0) == pytest.approx(3.0)


def test_grid_alignment_measures_the_warp_not_the_music():
    clicks = [aa.Transient(0.5 * k, 1.0, "low", 1.0) for k in range(16)]
    on = aa.BeatMap([(0.0, 0.0), (16.0, 8.0)], 0.5)
    assert aa.grid_alignment(clicks, on, 1.0)["median_abs_ms"] == 0.0
    late = aa.BeatMap([(0.0, -0.012), (16.0, 7.988)], 0.5)
    assert aa.grid_alignment(clicks, late, 1.0)["median_abs_ms"] == pytest.approx(12.0)
    # An off-beat ghost note is not counted as an error.
    ghost = clicks + [aa.Transient(0.25, 1.0)]
    assert aa.grid_alignment(ghost, on, 1.0)["count"] == 16


def _monotonic(markers):
    pts = sorted((m["beat_time"], m["sample_time"]) for m in markers)
    return all(b[1] > a[1] for a, b in zip(pts, pts[1:]))


def test_transients_mode_pulls_slightly_late_hits_onto_the_grid():
    # Hits 10-20 ms late against a straight 120 BPM warp.
    hits = [aa.Transient(0.5 * k + 0.010 + 0.0025 * (k % 5), 1.0, "low", 1.0)
            for k in range(1, 15)]
    markers = [{"beat_time": 0.0, "sample_time": 0.0},
               {"beat_time": 4.0, "sample_time": 2.0},
               {"beat_time": 16.0, "sample_time": 8.0}]
    bm = aa.BeatMap.from_markers(markers)
    out = aa.suggest_edits(hits, markers, bm, (0.0, 8.0), subdivision=1.0)
    assert out["edits"]
    assert _monotonic(out["markers_after"])
    after = aa.BeatMap.from_markers(out["markers_after"])
    assert aa.grid_alignment(hits, after, 1.0)["max_abs_ms"] < 0.01
    # The marker that sits near a hit (beat 4 at 2.0 s, hit at 2.0175) is
    # re-pinned, never duplicated; the hidden end marker is never touched.
    assert not any(e["beat_time"] == 16.0 for e in out["edits"])
    assert out["markers_after"][-1] == {"beat_time": 16.0, "sample_time": 8.0}
    # Removes come first so a batch never trips on its own state.
    ops = [e["op"] for e in out["edits"]]
    assert ops == sorted(ops, key=["remove", "move", "add"].index)
    # Replaying the edits gives the predicted state.
    replay = aa.apply_edits_to_markers(markers, out["edits"])
    assert replay == out["markers_after"]


@pytest.mark.parametrize("marker_sec,expect_ops", [
    (2.0, {"move"}),             # marker sits on the hit: drag it
    (2.003, {"remove", "add"}),  # 3 ms off the attack: replace it
])
def test_transients_mode_batches_apply_cleanly_in_the_remote_script(
        marker_sec, expect_ops):
    # The proposal's edit ORDER must be valid for the real batch handler,
    # not just its end state: replay it through the Remote Script on the
    # fake LOM and demand a verified result equal to the prediction.
    markers = [{"beat_time": 0.0, "sample_time": 0.0},
               {"beat_time": 4.1, "sample_time": marker_sec},
               {"beat_time": 16.0, "sample_time": 8.0}]
    hits = [aa.Transient(0.5 * k, 1.0, "low", 1.0) for k in range(1, 15)]
    out = aa.suggest_edits(hits, markers, aa.BeatMap.from_markers(markers),
                           (0.0, 8.0))
    touching = {e["op"] for e in out["edits"]
                if e["beat_time"] in (4.0, 4.1)}
    assert touching == expect_ops
    harness = make_harness(config=FakeLiveConfig(warp_markers=True))
    clip = harness.song.tracks[2].arrangement_clips[0]
    clip.length = clip.end_marker = 16.0
    clip.sample_length = 8 * SR
    clip.warp_markers = [FakeWarpMarker(m["beat_time"], m["sample_time"])
                         for m in markers]
    response = harness.process({"type": "edit_clip_warp_markers", "params": {
        "track_index": 2, "clip_index": 0,
        "edits": [{k: e[k] for k in ("op", "beat_time", "sample_time",
                                     "new_beat_time") if k in e}
                  for e in out["edits"]]}})
    assert response["status"] == "success", response
    result = response["result"]
    assert result["verified"] is True, result
    assert result["warp_markers_after"] == [
        {"beat_time": m["beat_time"], "sample_time": pytest.approx(m["sample_time"])}
        for m in out["markers_after"]]


def test_rewarp_tracks_a_drifting_take(tmp_path):
    # 123 BPM with wobble under a straight 120 BPM warp: within four bars the
    # nearest grid line is the wrong one, so snapping fails; tracking works.
    path, kicks = write_drums(tmp_path / "drift.wav")
    an = aa.analyse(str(path), (0.0, 16.0), prior_bpm=120.0)
    markers = [{"beat_time": 0.0, "sample_time": 0.0},
               {"beat_time": 32.0, "sample_time": 16.0}]
    bm = aa.BeatMap.from_markers(markers)
    out = aa.rewarp_edits(an.transients, markers, bm, an.window, tempo=an.tempo)
    assert _monotonic(out["markers_after"])
    after = aa.BeatMap.from_markers(out["markers_after"])
    # Every kick except the last (in the hidden marker's tail segment) now
    # sits on a whole beat.
    for k in kicks[:-1]:
        b = after.seconds_to_beat(k)
        assert abs(b - round(b)) < 0.005, (k, b)
    before = aa.grid_alignment(an.transients, bm, 1.0)
    fixed = aa.grid_alignment(an.transients, after, 1.0)
    assert before["median_abs_ms"] > 50
    assert fixed["p90_abs_ms"] < 1.0
    assert aa.apply_edits_to_markers(markers, out["edits"]) == out["markers_after"]


def test_tempo_mode_proposes_two_anchors(tmp_path):
    path, kicks = write_drums(tmp_path / "steady.wav", wobble=0.0)
    an = aa.analyse(str(path), (0.0, 16.0), prior_bpm=120.0)
    markers = [{"beat_time": 0.0, "sample_time": 0.0},
               {"beat_time": 32.0, "sample_time": 16.0}]
    out = aa.tempo_edits(an.tempo, markers, aa.BeatMap.from_markers(markers),
                         an.window, an.transients)
    adds = [e for e in out["edits"] if e["op"] == "add"]
    assert len(adds) == 2
    (b0, s0), (b1, s1) = [(a["beat_time"], a["sample_time"]) for a in adds]
    assert 60.0 * (b1 - b0) / (s1 - s0) == pytest.approx(123.0, abs=0.3)
    assert (b1 - b0) % 4 == 0


def test_render_png_and_jpeg_fit_the_budget(tmp_path):
    path, _ = write_drums(tmp_path / "drums.wav")
    an = aa.analyse(str(path), (0.0, 16.0))
    bm = aa.BeatMap([(0.0, 0.0), (32.0, 16.0)])
    panels = [aa.Panel("current", bm, [{"beat_time": 0.0, "sample_time": 0.0}])]
    png = aa.render(an.audio, an.mag, an.window, panels, an.transients, "t",
                    tempo=an.tempo)
    img = PIL_Image.open(io.BytesIO(png))
    assert img.format == "PNG" and img.size[0] == 1200
    assert len(png) < 300_000  # palette PNG; truecolour was ~2x
    jpg = aa.render(an.audio, an.mag, an.window, panels, an.transients, "t",
                    width=600, fmt="jpeg")
    assert PIL_Image.open(io.BytesIO(jpg)).size[0] == 600


# ── 2. warp_vision + tools over a scripted wire ──────────────────────────────

def _state(path, **over):
    state = {
        "track_index": 2, "track_name": "Drums", "clip_index": 0,
        "clip_name": "Take", "arrangement": True, "is_audio_clip": True,
        "warping": True, "warp_mode": 0, "marker_units": "beats",
        "start_marker": 0.0, "end_marker": 32.0, "start_time": 64.0,
        "end_time": 96.0, "sample_rate": 44100.0, "sample_seconds": 16.0,
        "song_tempo": 120.0, "signature_numerator": 4, "file_path": str(path),
        "warp_markers": [{"beat_time": 0.0, "sample_time": 0.0},
                         {"beat_time": 32.0, "sample_time": 16.0}],
    }
    state.update(over)
    return state


def _call(name, args, wire):
    fake = FakeWireClient(wire)
    handshake = AllCapabilitiesHandshake()
    deps = Deps(client=fake, handshake=handshake,
                service=AbletonService(fake, handshake))
    out = getattr(tools, name)(make_ctx(deps), **args)
    fake.assert_consumed()
    return out


def _read(params, response):
    return {"command": "get_clip_warp_markers", "params": params,
            "response": response}


def test_clip_context_maps_arrangement_clips_onto_song_beats(tmp_path):
    path, _ = write_drums(tmp_path / "d.wav")
    ctx = warp_vision.clip_context(_state(path))
    assert ctx.grid_offset == 64.0
    assert ctx.grid_beat(2.0) == pytest.approx(68.0)
    assert warp_vision.resolve_window(ctx, start_beat=72, end_beat=80) == \
        pytest.approx((4.0, 8.0))
    unwarped = warp_vision.clip_context(_state(
        path, warping=False, start_marker=1.0, end_marker=9.0))
    assert unwarped.region == (1.0, 9.0)
    assert unwarped.grid_beat(1.5) == pytest.approx(65.0)
    assert any("warping is OFF" in n for n in unwarped.notes)


@pytest.mark.parametrize("over,phrase", [
    ({"is_audio_clip": False}, "this is MIDI"),
    ({"file_path": None}, "no file path"),
    ({"file_path": "/nonexistent/x.wav"}, "not on this machine"),
])
def test_clip_context_refusals(tmp_path, over, phrase):
    path, _ = write_drums(tmp_path / "d.wav", duration=2.0)
    with pytest.raises(aa.AnalysisError, match=phrase):
        warp_vision.clip_context(_state(path, **over))


def test_analyze_tool_returns_text_and_an_image(tmp_path):
    path, _ = write_drums(tmp_path / "d.wav")
    params = {"track_index": 2, "clip_index": 0, "arrangement": True}
    out = _call("analyze_clip_audio",
                {"track_index": 2, "clip_index": 0, "start_beat": 64,
                 "end_beat": 80},
                [_read(params, _state(path))])
    text, image = out
    assert isinstance(image, McpImage)
    content = image.to_image_content()
    assert content.mimeType == "image/png"
    head, body = text.split("\n", 1)
    assert "Analysed 'Take' on 'Drums'" in head
    data = json.loads(body.split("\n\nImage key:")[0])
    assert data["window"]["start_beat"] == pytest.approx(64.0)
    assert data["window"]["end_beat"] == pytest.approx(80.0)
    assert data["source_tempo_estimate"]["bpm"] == pytest.approx(123, abs=1)
    assert data["grid_alignment"]["beat"]["median_abs_ms"] > 10
    assert data["transients"] and {"t", "beat", "off_16th_ms", "strength",
                                   "band"} <= set(data["transients"][0])
    assert set(data["band_energy_db"]) == {"times", "low_db", "mid_db", "high_db"}
    assert "image" not in data


def test_analyze_tool_text_only_is_a_plain_string(tmp_path):
    path, _ = write_drums(tmp_path / "d.wav", duration=4.0)
    out = _call("analyze_clip_audio",
                {"track_index": 2, "clip_index": 0, "include_image": False,
                 "max_transients": 5},
                [_read({"track_index": 2, "clip_index": 0, "arrangement": True},
                       _state(path, sample_seconds=4.0, end_marker=8.0))])
    assert isinstance(out, str)
    data = json.loads(out.split("\n", 1)[1])
    assert len(data["transients"]) == 5
    assert data["transients_total"] > 5


def test_suggest_dry_run_sends_nothing_but_the_read(tmp_path):
    path, _ = write_drums(tmp_path / "d.wav")
    out = _call("suggest_warp_markers",
                {"track_index": 2, "clip_index": 0, "mode": "rewarp"},
                [_read({"track_index": 2, "clip_index": 0, "arrangement": True},
                       _state(path))])
    text, image = out
    assert isinstance(image, McpImage)
    assert text.startswith("Dry run — nothing changed.")
    data = json.loads(text.split("\n", 1)[1].split("\n\nImage key:")[0])
    assert data["alignment_after_is"] == "predicted"
    assert data["alignment_after"]["p90_abs_ms"] < 1.0
    assert data["edits"] and all(set(e) <= {"op", "beat_time", "sample_time",
                                            "new_beat_time", "distance"}
                                 for e in data["edits"])


def test_suggest_refuses_unwarped_clip(tmp_path):
    path, _ = write_drums(tmp_path / "d.wav", duration=2.0)
    out = _call("suggest_warp_markers",
                {"track_index": 2, "clip_index": 0, "include_image": False},
                [_read({"track_index": 2, "clip_index": 0, "arrangement": True},
                       _state(path, warping=False))])
    assert out.startswith("Error suggesting warp markers: warping is off")


def test_suggest_rejects_unknown_mode(tmp_path):
    path, _ = write_drums(tmp_path / "d.wav", duration=2.0)
    out = _call("suggest_warp_markers",
                {"track_index": 2, "clip_index": 0, "mode": "magic",
                 "include_image": False},
                [_read({"track_index": 2, "clip_index": 0, "arrangement": True},
                       _state(path))])
    assert "mode must be one of transients, rewarp, tempo" in out


# ── 3. The whole loop: FastMCP session -> Remote Script -> fake LOM ──────────

class _HarnessClient:
    """send_command -> the real Remote Script dispatch (test_full_stack's
    adapter, repeated here to keep this suite self-contained)."""

    def __init__(self, harness):
        self.harness = harness
        self.sent = []

    def send_command(self, command_type, params=None):
        self.sent.append((command_type, params or {}))
        response = self.harness.process({"type": command_type,
                                         "params": params or {}})
        if response.get("status") == "error":
            raise Exception("Communication error with Ableton: %s"
                            % response.get("message"))
        return response.get("result", {})


def _live_world(tmp_path):
    """Track 2's Arrangement clip, pointed at a drifting 123 BPM take and
    warped as Live's Auto-Warp would mis-guess it: straight 120 BPM."""
    path, kicks = write_drums(tmp_path / "take.wav")
    harness = make_harness(config=FakeLiveConfig(warp_markers=True))
    clip = harness.song.tracks[2].arrangement_clips[0]
    clip.file_path = str(path)
    clip.length = clip.end_marker = clip.loop_end = 32.0
    clip.end_time = clip.start_time + 32.0
    clip.sample_length = 16 * SR
    clip.warp_markers = [FakeWarpMarker(0.0, 0.0), FakeWarpMarker(32.0, 16.0)]
    client = _HarnessClient(harness)
    handshake = ScriptHandshake()
    deps = Deps(client=client, handshake=handshake,
                service=AbletonService(client, handshake))
    return deps, harness, clip, kicks


def test_agent_workflow_end_to_end_over_mcp(tmp_path):
    deps, harness, clip, kicks = _live_world(tmp_path)
    app = build_app(deps=deps)
    target = {"track_index": 2, "clip_index": 0}

    async def run():
        async with create_connected_server_and_client_session(app) as s:
            look = await s.call_tool("analyze_clip_audio", target)
            dry = await s.call_tool("suggest_warp_markers",
                                    dict(target, mode="rewarp"))
            markers_after_dry = [(m.beat_time, m.sample_time)
                                 for m in clip.warp_markers]
            applied = await s.call_tool("suggest_warp_markers",
                                        dict(target, mode="rewarp", apply=True))
            verify = await s.call_tool("analyze_clip_audio",
                                       dict(target, include_image=False))
            return look, dry, markers_after_dry, applied, verify

    look, dry, after_dry, applied, verify = asyncio.run(run())
    for result in (look, dry, applied, verify):
        assert not result.isError

    # 1. LOOK: real MCP image content travels alongside the text.
    kinds = [type(c) for c in look.content]
    assert kinds == [TextContent, ImageContent]
    assert look.content[1].mimeType == "image/png"
    before = json.loads(look.content[0].text.split("\n", 1)[1]
                        .split("\n\nImage key:")[0])
    assert before["grid_alignment"]["beat"]["median_abs_ms"] > 50

    # 2. The dry run changed nothing in Live.
    assert after_dry == [(0.0, 0.0), (32.0, 16.0)]

    # 3. APPLY: verified against the Remote Script's read-back.
    applied_text = applied.content[0].text
    assert applied_text.startswith("Applied ")
    assert "verified" in applied_text.split("\n", 1)[0]
    payload = json.loads(applied_text.split("\n", 1)[1]
                         .split("\n\nImage key:")[0])
    assert payload["applied"]["verified"] is True
    assert payload["alignment_after_is"] == "measured_from_live_readback"
    assert len(clip.warp_markers) > 20

    # 4. VERIFY: a fresh analysis of the new warping.
    after = json.loads(verify.content[0].text.split("\n", 1)[1])
    assert after["grid_alignment"]["beat"]["p90_abs_ms"] < 1.0
    # The kicks now land on whole song beats (clip starts at song beat 0).
    m = aa.BeatMap([(w.beat_time, w.sample_time) for w in clip.warp_markers])
    for k in kicks[:-1]:
        b = m.seconds_to_beat(k)
        assert abs(b - round(b)) < 0.005

    sent = [c for c, _ in deps.client.sent]
    assert sent.count("edit_clip_warp_markers") == 1


def test_image_tools_are_unstructured_so_images_survive():
    # FastMCP builds an output schema from a `-> str` annotation and would
    # then try to validate the image list as structured output; the image
    # tools are annotated `Any` so their content passes through as-is.
    async def listed():
        return await build_app().list_tools()

    by_name = {t.name: t for t in asyncio.run(listed())}
    for name in ("analyze_clip_audio", "suggest_warp_markers"):
        assert by_name[name].outputSchema is None
