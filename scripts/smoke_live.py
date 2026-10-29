"""Manual smoke test against a REAL running Ableton Live instance.

The automated suite never touches Live (tests/ runs anywhere); this script is
the deliberate opposite: it drives a real Live session through the production
stack — Settings -> build_deps -> AbletonClient (TCP) -> AbletonService
(registry + gating) -> Remote Script -> the Live Object Model — and reports
PASS / SKIP / FAIL per step.

Run it on the machine where Ableton is running, with the AbletonMCP control
surface enabled, in an EMPTY / SCRATCH Live set:

    uv run python scripts/smoke_live.py --yes

What it touches: only tracks it creates itself (deleted again at the end),
and the tempo (restored). What it cannot clean up, because Live's API has no
delete for them: one return track and one locator named "smoke-test" may
remain — hence the scratch-set requirement.

Since 1.19 every write needs the shared Live lock: pass --holder (default
"smoke-live"); a lock taken under that name with tools/ableton-lock is
adopted. --allow-audio authorizes the steps that make sound (playback), and
--allow-ui the ones that drive Live's menus and dialogs (save_set,
checkpoint_set, export_audio, bounce_track). Without them those steps SKIP.

This is intentionally NOT a pytest test: CLAUDE.md's rule is that tests/
never needs Ableton, and this file needs nothing else.
"""

from __future__ import annotations

import argparse
import math
import os
import struct
import sys
import tempfile
import time
import wave

from ableton_mcp.app import Settings, build_deps
from ableton_mcp.render import RenderRefused
from ableton_mcp.remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION


class Skip(Exception):
    """Raised by a step to report an unmet precondition, not a failure."""


class Smoke:
    def __init__(self, deps, allow_audio=False, allow_ui=False, scratch_dir=None):
        self.deps = deps
        self.svc = deps.service
        self.allow_audio = allow_audio
        self.allow_ui = allow_ui
        self.scratch_dir = scratch_dir or tempfile.mkdtemp(prefix="ableton-mcp-smoke-")
        self.audio_track: int | None = None
        self.click_wav: str | None = None
        self.created_tracks: list[int] = []   # regular-track indices we made
        self.original_tempo: float | None = None
        self.results: list[tuple[str, str, str]] = []  # (name, status, detail)

    # -- helpers -------------------------------------------------------------

    def track_count(self) -> int:
        return int(self.svc.get_session_info().get("track_count"))

    def run(self, name: str, fn) -> None:
        try:
            detail = fn() or ""
            self.results.append((name, "PASS", str(detail)))
            print(f"  PASS  {name}" + (f" — {detail}" if detail else ""))
        except Skip as s:
            self.results.append((name, "SKIP", str(s)))
            print(f"  SKIP  {name} — {s}")
        except Exception as e:  # a real failure against real Live
            self.results.append((name, "FAIL", f"{type(e).__name__}: {e}"))
            print(f"  FAIL  {name} — {type(e).__name__}: {e}")

    # -- steps ---------------------------------------------------------------

    def step_handshake(self):
        info = self.deps.handshake.perform(self.deps.client.send_command)
        version = info.get("script_version")
        if version is None:
            # perform() swallows transport errors; surface them as what they
            # are so the runner can abort with connection guidance.
            raise ConnectionError(
                f"could not connect to Ableton: {info.get('error')}"
            )
        if version != EXPECTED_REMOTE_SCRIPT_VERSION:
            raise AssertionError(
                f"Remote Script is v{version}, expected "
                f"v{EXPECTED_REMOTE_SCRIPT_VERSION}. Run "
                f"`uv run ableton-mcp-install-script`, then restart Live, "
                f"then re-run this script."
            )
        return f"script v{version}, {len(info.get('capabilities') or [])} capabilities"

    def step_session_info(self):
        info = self.svc.get_session_info()
        assert "track_count" in info and "tempo" in info, f"unexpected shape: {info}"
        self.original_tempo = float(info["tempo"])
        return f"{info['track_count']} tracks, tempo {info['tempo']}"

    def step_tempo(self):
        self.svc.set_tempo(123.0)
        now = float(self.svc.get_session_info()["tempo"])
        assert now == 123.0, f"tempo read back {now}"
        self.svc.set_tempo(self.original_tempo)
        return f"set 123.0, restored {self.original_tempo}"

    def step_create_midi_track(self):
        before = self.track_count()
        result = self.svc.create_midi_track(-1)
        after = self.track_count()
        assert after == before + 1, f"track_count {before} -> {after}"
        index = int(result.get("index", after - 1))
        self.created_tracks.append(index)
        self.svc.set_track_name(index, "smoke-midi")
        return f"index {index}, renamed to smoke-midi"

    def step_note_round_trip(self):
        t = self.created_tracks[0]
        self.svc.create_clip(t, 0, 4.0)
        notes = [
            {"pitch": 60, "start_time": 0.0, "duration": 0.5, "velocity": 100, "mute": False},
            {"pitch": 64, "start_time": 1.0, "duration": 0.5, "velocity": 90, "mute": False},
            {"pitch": 67, "start_time": 2.0, "duration": 0.5, "velocity": 80, "mute": False},
        ]
        self.svc.add_notes_to_clip(t, 0, notes)
        read = self.svc.get_clip_notes(t, 0)
        got = sorted(n["pitch"] for n in read.get("notes", []))
        assert got == [60, 64, 67], f"read back pitches {got}"
        cleared = self.svc.clear_notes_from_clip(t, 0)
        assert int(cleared.get("cleared_count", -1)) == 3, f"cleared {cleared}"
        return "wrote 3 notes, read 3 back, cleared 3"

    def step_fire_stop_clip(self):
        if not self.allow_audio:
            raise Skip("makes sound: re-run with --allow-audio")
        t = self.created_tracks[0]
        self.svc.add_notes_to_clip(t, 0, [
            {"pitch": 60, "start_time": 0.0, "duration": 4.0, "velocity": 100, "mute": False},
        ])
        self.svc.fire_clip(t, 0)
        time.sleep(0.3)
        self.svc.stop_clip(t, 0)
        self.svc.back_to_arrangement()
        return "fired, stopped, back to arrangement"

    def step_mixer(self):
        t = self.created_tracks[0]
        vol = self.svc.set_track_volume(t, 0.85, "regular")
        pan = self.svc.set_track_pan(t, 0.0, "regular")
        self.svc.set_track_mute(t, True)
        self.svc.set_track_mute(t, False)
        return (f"volume -> {vol.get('display_value') or vol.get('value')}, "
                f"pan -> {pan.get('display_value') or pan.get('value')}, mute toggled")

    def step_return_and_send(self):
        r = self.svc.create_return_track()
        t = self.created_tracks[0]
        s = self.svc.set_track_send(t, int(r.get("return_index", 0)), 0.5)
        return (f"return '{r.get('name')}' created (Live has no API to delete "
                f"it — scratch set!), send -> {s.get('display_value') or s.get('value')}")

    def step_device_parameter(self):
        # The headline 1.8.0 repair: parameter by NAME, clamped, on real Live.
        # get_browser_tree reports names only; get_browser_items_at_path is
        # the API that carries uri/is_loadable, so walk paths breadth-first.
        t = self.created_tracks[0]
        found = None
        queue = ["audio_effects"]
        for _ in range(12):  # bounded breadth-first walk, two-ish levels deep
            if found or not queue:
                break
            path = queue.pop(0)
            listing = self.svc.get_browser_items_at_path(path)
            for item in listing.get("items", []):
                if item.get("is_loadable") and item.get("uri"):
                    found = (item["uri"], item.get("name"))
                    break
                if item.get("is_folder") and item.get("name"):
                    queue.append(f"{path}/{item['name']}")
        if not found:
            raise Skip("no loadable audio effect found under audio_effects")
        uri, name = found
        loaded = self.svc.load_browser_item(t, uri, "regular")
        if not loaded.get("loaded"):
            raise Skip(f"browser refused to load '{name}'")

        params = self.svc.get_device_parameters(t, 0, "regular")
        target = next(
            (p for p in params.get("parameters", [])
             if not p.get("is_quantized") and p.get("min") is not None
             and p.get("max") is not None and p["min"] < p["max"]),
            None,
        )
        if target is None:
            raise Skip(f"'{name}' exposes no continuous parameter")

        over = float(target["max"]) + abs(float(target["max"])) + 1.0
        result = self.svc.set_device_parameter(t, 0, target["name"], over, "regular")
        assert result.get("clamped") is True, f"expected clamped=True, got {result}"
        assert float(result["value"]) == float(target["max"]), f"not clamped to max: {result}"
        self.svc.set_device_parameter(t, 0, target["name"],
                                      float(result["old_value"]), "regular")
        self.svc.delete_device(t, 0, "regular")
        return (f"loaded '{name}', set '{target['name']}' by NAME over max -> "
                f"clamped to {result['value']}, restored, device deleted")

    def step_arrangement(self):
        t = self.created_tracks[0]
        self.svc.duplicate_session_clip_to_arrangement(t, 0, 0.0)
        clips = self.svc.get_arrangement_clips(t)
        assert clips.get("clips"), f"no arrangement clips after duplicate: {clips}"
        self.svc.create_locator("smoke-test", 0.0)
        self.svc.set_current_song_time(0.0)
        self.svc.switch_to_arrangement_view()
        return (f"{len(clips['clips'])} arrangement clip(s); locator "
                f"'smoke-test' set (no delete API — scratch set!)")

    def step_arrangement_editing(self):
        """The 1.12.0 arrangement-editing surface, on a clip this run made.

        This is the designated real-Live evidence for what the fake cannot
        prove: that stamping a clip over an Arrangement region permanently
        crops what it covers (the overlap physics trim_arrangement_clip is
        built on — eraser measuring stamp, crop, and cleanup included),
        that duplicate+delete is a faithful move, and that
        Track.delete_clip / duplicate_clip_to_arrangement accept
        Arrangement clips. Everything happens on the scratch track
        step_arrangement populated, far to the right of its clip.
        """
        t = self.created_tracks[0]
        before = self.svc.get_arrangement_clips(t)["clips"]
        if not before:
            raise Skip("no arrangement clip to edit (step_arrangement failed?)")
        src = len(before) - 1  # rightmost = highest start_time
        slots_before = [s["has_clip"] for s in
                        self.svc.get_track_info(t)["clip_slots"]]

        # duplicate the arrangement clip to a clear spot
        self.svc.duplicate_arrangement_clip(t, src, 64.0)
        clips = self.svc.get_arrangement_clips(t)["clips"]
        assert len(clips) == len(before) + 1, f"no copy appeared: {clips}"
        copy_index = len(clips) - 1
        copy = clips[copy_index]
        assert abs(copy["start_time"] - 64.0) <= 1e-3, f"copy not at 64: {copy}"

        # trim the copy's tail by one beat, then its head by one beat; each
        # edge must land where requested, per the tool's own readback AND an
        # independent get_arrangement_clips readback afterwards
        length = copy["end_time"] - copy["start_time"]
        if length < 2.5:
            raise Skip(f"clip too short to trim safely ({length} beats)")
        trim = self.svc.trim_arrangement_clip(t, copy_index,
                                              None, copy["end_time"] - 1.0)
        assert trim.get("refusals") == [], f"tail trim refused: {trim}"
        assert trim.get("trimmed_tail") is True, f"tail not trimmed: {trim}"
        assert abs(trim["end_time"] - (copy["end_time"] - 1.0)) <= 1e-3
        trim = self.svc.trim_arrangement_clip(t, copy_index,
                                              copy["start_time"] + 1.0, None)
        assert trim.get("refusals") == [], f"head trim refused: {trim}"
        assert trim.get("trimmed_head") is True, f"head not trimmed: {trim}"
        assert abs(trim["start_time"] - (copy["start_time"] + 1.0)) <= 1e-3

        clips = self.svc.get_arrangement_clips(t)["clips"]
        assert len(clips) == len(before) + 1, \
            f"stray clip left behind by trim (eraser stamp or shard?): {clips}"
        edited = clips[copy_index]
        assert abs(edited["start_time"] - (copy["start_time"] + 1.0)) <= 1e-3, \
            f"trimmed head not at {copy['start_time'] + 1.0}: {edited}"
        assert abs(edited["end_time"] - (copy["end_time"] - 1.0)) <= 1e-3, \
            f"trimmed tail not at {copy['end_time'] - 1.0}: {edited}"
        slots_after = [s["has_clip"] for s in
                       self.svc.get_track_info(t)["clip_slots"]]
        assert slots_after == slots_before, \
            "trim leaked its temporary eraser Session clip"

        # move the trimmed copy (duplicate+delete under the hood)
        moved = self.svc.move_arrangement_clip(t, copy_index, 96.0)
        assert moved.get("moved") is True, f"move did not report moved: {moved}"
        assert abs(moved["start_time"] - 96.0) <= 1e-3, f"not at 96: {moved}"

        # jump to the locator step_arrangement created (transport stopped:
        # this also plants the start marker there)
        jumped = self.svc.jump_to_locator("smoke-test", None)
        assert jumped.get("start_marker_set") is True, f"no marker: {jumped}"

        # delete the copy; the track is back to its pre-step clip count
        clips = self.svc.get_arrangement_clips(t)["clips"]
        target = next(i for i, c in enumerate(clips)
                      if abs(c["start_time"] - 96.0) <= 1e-3)
        gone = self.svc.delete_arrangement_clip(t, target)
        assert gone.get("deleted") is True, f"delete failed: {gone}"
        clips = self.svc.get_arrangement_clips(t)["clips"]
        assert len(clips) == len(before), f"clip count off after delete: {clips}"
        return ("duplicated, trimmed both edges (verified by readback, "
                "eraser cleaned up), moved to 96, jumped to locator "
                "(start marker planted), deleted the copy")

    def step_playback(self):
        if not self.allow_audio:
            raise Skip("makes sound: re-run with --allow-audio")
        self.svc.start_playback(authorized=True)
        time.sleep(0.3)
        self.svc.stop_playback()
        return "started, stopped"

    def step_save_set(self):
        if not self.allow_ui:
            raise Skip("saves through Live's File menu: re-run with --allow-ui")
        result = self.svc.save_set(allow_ui_automation=True)
        assert result.get("saved"), f"not saved: {result}"
        return f"saved via {result.get('method')}"

    # -- 1.17–1.26 -----------------------------------------------------------

    def _wav(self, name, beats=16, bpm=120.0, rate=44100, push_ms=()):
        """A click on every beat; ``push_ms`` = {beat: ms late} to give the
        warp tools a timing error to find."""
        path = os.path.join(self.scratch_dir, name)
        frames = int(beats * 60.0 / bpm * rate)
        data = bytearray(frames * 2)
        for b in range(beats):
            late = dict(push_ms).get(b, 0.0) / 1000.0
            start = int((b * 60.0 / bpm + late) * rate)
            for i in range(min(800, frames - start)):
                v = 0.6 * math.exp(-i / 120.0) * math.sin(2 * math.pi * 1000 * i / rate)
                struct.pack_into("<h", data, 2 * (start + i), int(v * 32767))
        with wave.open(path, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
            w.writeframes(bytes(data))
        return path

    def step_live_status(self):
        st = self.svc.get_live_status()
        assert st.get("reachable", True) is not False, f"not reachable: {st}"
        lock = st.get("lock") or {}
        assert lock.get("held_by_this_server") or lock.get("holder"), f"no lock info: {st}"
        return f"script {st.get('script_version')}, lock holder {lock.get('holder')}"

    def step_snapshot_and_mix(self):
        snap = self.svc.get_session_snapshot(False, False, include_warp_markers=True,
                                             include_rack_chains=True, max_chars=20000)
        text = str(snap)
        assert len(text) <= 40000, f"snapshot ignored its budget ({len(text)} chars)"
        mix = self.svc.get_mix_state()
        rows = mix.get("tracks") or []
        assert any(r.get("name") == "smoke-midi" for r in rows), f"mix_state rows: {rows}"
        return f"snapshot {len(text)} chars under budget, mix_state {len(rows)} tracks"

    def step_solo(self):
        t = self.created_tracks[0]
        self.svc.set_track_solo(t, True, "regular", expect_track_name="smoke-midi")
        on = next(r for r in self.svc.get_mix_state()["tracks"] if r.get("name") == "smoke-midi")
        self.svc.set_track_solo(t, False, "regular", expect_track_name="smoke-midi")
        off = next(r for r in self.svc.get_mix_state()["tracks"] if r.get("name") == "smoke-midi")
        assert on.get("solo") is True and off.get("solo") is False, f"{on} / {off}"
        return "solo on, read back, off"

    def step_song_scale(self):
        before = self.svc.get_song_scale()
        self.svc.set_song_scale(root_note=2, scale_name="Minor")
        now = self.svc.get_song_scale()
        assert now.get("root_note") == 2 and "minor" in str(now.get("scale_name")).lower(), now
        self.svc.set_song_scale(root_note=before.get("root_note"),
                                scale_name=before.get("scale_name"),
                                scale_mode=before.get("scale_mode"))
        return f"D Minor set and read back, restored {before.get('scale_name')}"

    def step_arrangement_midi_clip(self):
        t = self.created_tracks[0]
        r = self.svc.create_arrangement_midi_clip(
            t, 32.0, 4.0, notes=[{"pitch": 48, "start_time": 0.0, "duration": 1.0,
                                  "velocity": 100}], name="smoke-arr-midi",
            expect_track_name="smoke-midi")
        clips = self.svc.get_arrangement_clips(t)["clips"]
        hit = [c for c in clips if abs(c["start_time"] - 32.0) <= 1e-3]
        assert hit, f"no clip at 32: {clips} ({r})"
        return f"MIDI clip at beat 32 ({hit[0].get('name')})"

    def step_audio_clip_and_warp(self):
        before = self.track_count()
        self.svc.create_audio_track(-1)
        self.audio_track = before
        self.created_tracks.append(before)
        self.svc.set_track_name(before, "smoke-audio")
        tempo = float(self.svc.get_session_info()["tempo"])
        self.click_wav = self._wav("smoke-click.wav", bpm=tempo, push_ms={5: 30, 9: 25})
        self.svc.create_arrangement_audio_clip(before, self.click_wav, 0.0,
                                               name="smoke-click",
                                               expect_track_name="smoke-audio")
        self.svc.set_clip_warp(before, 0, True, None, True, expect_track_name="smoke-audio")
        st = self.svc.get_clip_warp_markers(before, 0, True, expect_track_name="smoke-audio")
        assert st.get("warping") is True and st.get("warp_markers"), st
        return f"audio clip placed, warping on, {len(st['warp_markers'])} markers"

    def step_warp_vision(self):
        t = self.audio_track
        if t is None:
            raise Skip("no audio clip (previous step failed)")
        a = self.svc.analyze_clip_audio(t, 0, True, expect_track_name="smoke-audio",
                                        include_image=False)
        assert a, "analyze_clip_audio returned nothing"
        dry = self.svc.suggest_warp_markers(t, 0, True, apply=False, include_image=False,
                                            expect_track_name="smoke-audio",
                                            mode="transients", subdivision=1.0)
        edits = dry.get("edits") or []
        assert edits, f"no edits proposed for 30 ms late clicks: {dry}"
        applied = self.svc.suggest_warp_markers(t, 0, True, apply=True, include_image=False,
                                                expect_track_name="smoke-audio",
                                                mode="transients", subdivision=1.0)
        assert applied.get("verified") is not False, f"apply not verified: {applied}"
        # hand-edit round trip: add one marker, then remove it again
        st = self.svc.get_clip_warp_markers(t, 0, True, expect_track_name="smoke-audio")
        n = len(st["warp_markers"])
        spb = 60.0 / float(st.get("tempo") or self.svc.get_session_info()["tempo"])
        add = self.svc.edit_clip_warp_markers(
            t, 0, [{"op": "add", "beat_time": 14.0, "sample_time": 14.0 * spb}], True,
            expect_track_name="smoke-audio")
        assert add.get("verified") is True, f"add not verified: {add}"
        rm = self.svc.edit_clip_warp_markers(t, 0, [{"op": "remove", "beat_time": 14.0}],
                                             True, expect_track_name="smoke-audio")
        assert rm.get("verified") is True, f"remove not verified: {rm}"
        after = len(self.svc.get_clip_warp_markers(t, 0, True)["warp_markers"])
        assert after == n, f"marker count {n} -> {after}"
        return (f"analyzed; {len(edits)} edits proposed and applied (verified); "
                f"manual add/remove verified, back to {n} markers")

    def step_devices_and_sidechain(self):
        t = self.audio_track
        if t is None:
            raise Skip("no audio track")
        self.svc.insert_device(t, "Compressor", -1, expect_track_name="smoke-audio")
        self.svc.insert_device(t, "Saturator", 0, expect_track_name="smoke-audio")
        mix = next(r for r in self.svc.get_mix_state()["tracks"] if r.get("name") == "smoke-audio")
        names = [str(d.get("name") if isinstance(d, dict) else d) for d in mix.get("devices") or []]
        assert names[:2] == ["Saturator", "Compressor"], f"insert order wrong: {names}"
        self.svc.move_device(t, 1, 0, expect_track_name="smoke-audio")
        mix = next(r for r in self.svc.get_mix_state()["tracks"] if r.get("name") == "smoke-audio")
        names = [str(d.get("name") if isinstance(d, dict) else d) for d in mix.get("devices") or []]
        assert names[:2] == ["Compressor", "Saturator"], f"move failed: {names}"
        # Live only offers AUDIO-capable sources in the sidechain menu, and
        # a bare MIDI track (no instrument) is not one; smoke-audio is, and
        # it is ours, so the lookup stays deterministic.
        sc = self.svc.set_device_sidechain(t, 0, "smoke-audio", enable=True,
                                           expect_track_name="smoke-audio")
        got = self.svc.get_device_sidechain(t, 0)
        assert got.get("enabled") is True, f"sidechain not on: {got} ({sc})"
        self.svc.set_device_sidechain(t, 0, "smoke-audio", enable=False,
                                      expect_track_name="smoke-audio")
        params = self.svc.get_device_parameters(t, 0, "regular")
        shown = [p for p in params.get("parameters", []) if p.get("display_value")]
        assert shown, "1.21 display strings missing from get_device_parameters"
        return (f"inserted at index, moved, sidechain from smoke-audio on/off; "
                f"{len(shown)} params carry display strings")

    def step_rack_chains(self):
        t = self.audio_track
        if t is None:
            raise Skip("no audio track")
        self.svc.insert_device(t, "Audio Effect Rack", -1, expect_track_name="smoke-audio")
        rack = len(self.svc.get_mix_state() and next(
            r for r in self.svc.get_mix_state()["tracks"] if r.get("name") == "smoke-audio"
        ).get("devices") or []) - 1
        self.svc.insert_rack_chain(t, rack, -1, name="smoke-chain",
                                   expect_track_name="smoke-audio")
        self.svc.insert_chain_device(t, rack, 0, "Utility",
                                     expect_track_name="smoke-audio")
        self.svc.set_chain_mixer(t, rack, 0, {"volume": 0.5},
                                 expect_track_name="smoke-audio")
        chains = self.svc.get_rack_chains(t, rack).get("chains") or []
        assert chains and chains[0].get("name") == "smoke-chain", chains
        devs = [str(d.get("name") if isinstance(d, dict) else d) for d in chains[0].get("devices") or []]
        assert "Utility" in devs, f"chain devices {devs}"
        return f"rack at {rack}: chain 'smoke-chain' with Utility, mixer set"

    def step_load_devices(self):
        t = self.created_tracks[0]
        lib = os.path.expanduser("~/Music/Ableton/User Library/Presets")
        presets: list[str] = []
        for root, _d, files in os.walk(lib):
            for f in files:
                if f.endswith(".adv"):
                    presets.append(os.path.join(root, f))
        if not presets:
            raise Skip("no .adv preset in the User Library")
        # The server refuses a document Live would reject (preset_corrupt,
        # nothing sent), so walk a few presets and skip the step (not fail)
        # when none of them load. A live_alert is different: Live answered
        # with a modal, and if it is still up every further load queues
        # behind it - on 2026-10-28 that turned one bad preset into a Live
        # that never answered again. Stop there.
        notes = []
        for preset in presets[:5]:
            try:
                r = self.svc.load_device_from_path(t, preset,
                                                   expect_track_name="smoke-midi")
            except RenderRefused as e:
                if e.reason == "live_alert":
                    raise AssertionError(
                        f"{os.path.basename(preset)}: Live alert "
                        f"{e.details.get('alert_text')!r} - "
                        f"{e.details.get('detail')}; stopped loading") from e
                notes.append(f"{os.path.basename(preset)}: {e.reason} "
                             f"{str(e.details.get('summary') or '')[:80]}")
                continue
            except Exception as e:
                notes.append(f"{os.path.basename(preset)}: {str(e)[:90]}")
                continue
            if r.get("loaded") is False:
                notes.append(f"{os.path.basename(preset)}: loaded=false "
                             f"({str(r.get('alert') or '')[:60]})")
                continue
            state = self.svc.get_plugin_state(preset, include_strings=False)
            assert state, "get_plugin_state returned nothing"
            which = f" (of {min(5, len(presets))} tried)" if notes else ""
            return (f"loaded {os.path.basename(preset)}{which}; offline "
                    f"plugin state read")
        raise Skip("no loadable .adv in the User Library: " + " | ".join(notes))

    def step_audio_metrics(self):
        if not self.click_wav:
            raise Skip("no test wav")
        a = self.svc.analyze_audio(self.click_wav)
        v = self.svc.verify_audio(self.click_wav, sample_rate=44100)
        assert v.get("ok") is True, f"verify_audio: {v}"
        return f"analyze_audio peak {a.get('peak_dbfs') or a.get('peak_db')}, verify ok"

    def step_checkpoint(self):
        if not self.allow_ui:
            raise Skip("saves through Live's File menu: re-run with --allow-ui")
        r = self.svc.checkpoint_set("smoke", allow_ui_automation=True,
                                    checkpoint_dir=self.scratch_dir)
        # checkpoint_set answers {checkpointed, checkpoint_path, sha256}; the
        # old check read keys it never returns and failed a good checkpoint.
        assert r.get("checkpointed") is True, r
        assert os.path.isfile(r["checkpoint_path"]), r
        return (f"checkpoint {os.path.basename(r['checkpoint_path'])} "
                f"(sha256 {str(r.get('sha256'))[:12]})")

    def step_export(self):
        if not self.allow_ui:
            raise Skip("drives the Export dialog: re-run with --allow-ui")
        # End between clicks (the click wav has one on every beat): a range
        # ending ON beat 8 cuts that click, and the render QC rightly calls
        # the tail cut.
        r = self.svc.export_audio(allow_ui_automation=True, scope="main",
                                  start_beat=0.0, end_beat=7.5, tail_beats=0.0,
                                  output_dir=self.scratch_dir, file_stem="smoke-export",
                                  require_user_idle_s=0.0, overwrite=True, timeout_s=180)
        assert r.get("ok") is True, r
        return f"rendered + verified {[os.path.basename(f.get('path', '')) for f in r['files']]}"

    def step_bounce(self):
        if not self.allow_ui:
            raise Skip("drives Live's Edit menu: re-run with --allow-ui")
        t = self.audio_track
        if t is None:
            raise Skip("no audio track")
        fr = self.svc.bounce_track(t, "freeze", allow_ui_automation=True,
                                   expect_track_name="smoke-audio")
        assert fr.get("ok") is True, f"freeze: {fr}"
        un = self.svc.bounce_track(t, "unfreeze", allow_ui_automation=True,
                                   expect_track_name="smoke-audio")
        assert un.get("ok") is True, f"unfreeze: {un}"
        return "froze and unfroze smoke-audio"

    def cleanup(self):
        print("\nCleanup:")
        for index in sorted(self.created_tracks, reverse=True):
            try:
                gone = self.svc.delete_track(index)
                print(f"  deleted track {index} ('{gone.get('deleted_track_name')}')")
            except Exception as e:
                print(f"  could not delete track {index}: {e}")
        if self.original_tempo is not None:
            try:
                self.svc.set_tempo(self.original_tempo)
            except Exception as e:
                print(f"  could not restore tempo: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=None, help="Ableton host (default: env/localhost)")
    parser.add_argument("--port", type=int, default=None, help="Ableton port (default: env/9877)")
    parser.add_argument("--yes", action="store_true",
                        help="skip the scratch-set confirmation prompt")
    parser.add_argument("--keep", action="store_true", help="skip cleanup")
    parser.add_argument("--holder", default="smoke-live",
                        help="Live lock holder name (adopts a lock taken under it)")
    parser.add_argument("--allow-audio", action="store_true",
                        help="authorize steps that make sound (playback)")
    parser.add_argument("--allow-ui", action="store_true",
                        help="allow steps that drive Live's menus/dialogs")
    args = parser.parse_args()

    settings = Settings.from_env()
    if args.host or args.port:
        settings = Settings(host=args.host or settings.host,
                            port=args.port or settings.port)

    print(f"Smoke test against Ableton at {settings.host}:{settings.port}")
    print("This creates tracks/clips and may leave one return track and one")
    print("locator behind. Run it in an EMPTY / SCRATCH Live set.")
    if not args.yes:
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 2

    deps = build_deps(settings)
    smoke = Smoke(deps, allow_audio=args.allow_audio, allow_ui=args.allow_ui)
    try:
        deps.service.acquire_live_lock(args.holder, "smoke_live.py")
    except Exception as e:
        print(f"Cannot take the Live lock as '{args.holder}': {e}")
        return 1
    try:
        print("\nSteps:")
        smoke.run("handshake / script version", smoke.step_handshake)
        if smoke.results[0][1] == "FAIL" and "connect" in smoke.results[0][2].lower():
            print("\nCannot reach Ableton at all — aborting the remaining steps.")
            print("Checklist: is Live running on this machine? Is the AbletonMCP")
            print("control surface selected under Settings -> Link, Tempo & MIDI?")
            print("Did you restart Live after running ableton-mcp-install-script?")
            return 1
        smoke.run("session info", smoke.step_session_info)
        smoke.run("tempo set + restore", smoke.step_tempo)
        smoke.run("create MIDI track + rename", smoke.step_create_midi_track)
        if smoke.created_tracks:
            smoke.run("MIDI note write/read/clear round-trip", smoke.step_note_round_trip)
            smoke.run("fire/stop clip", smoke.step_fire_stop_clip)
            smoke.run("mixer volume/pan/mute", smoke.step_mixer)
            smoke.run("return track + send", smoke.step_return_and_send)
            smoke.run("device parameter by name, clamped (the 1.8.0 repair)",
                      smoke.step_device_parameter)
            smoke.run("arrangement: duplicate clip + locator", smoke.step_arrangement)
            smoke.run("arrangement editing (the 1.11.0 surface)",
                      smoke.step_arrangement_editing)
        if smoke.created_tracks:
            smoke.run("live status + lock (1.19)", smoke.step_live_status)
            smoke.run("snapshot budget + mix state (1.25)", smoke.step_snapshot_and_mix)
            smoke.run("track solo", smoke.step_solo)
            smoke.run("song scale", smoke.step_song_scale)
            smoke.run("arrangement-native MIDI clip (1.26)", smoke.step_arrangement_midi_clip)
            smoke.run("arrangement-native audio clip + warp (1.26/1.17)",
                      smoke.step_audio_clip_and_warp)
            smoke.run("warp vision: analyze/suggest/apply/edit (1.17)", smoke.step_warp_vision)
            smoke.run("device placement + sidechain + display values (1.26/1.21)",
                      smoke.step_devices_and_sidechain)
            smoke.run("rack chains (1.23)", smoke.step_rack_chains)
            smoke.run("load preset by path + plugin state (1.22)", smoke.step_load_devices)
            smoke.run("analyze/verify audio (1.24)", smoke.step_audio_metrics)
        smoke.run("playback start/stop", smoke.step_playback)
        smoke.run("save set", smoke.step_save_set)
        smoke.run("checkpoint set", smoke.step_checkpoint)
        smoke.run("export audio (1.24)", smoke.step_export)
        smoke.run("freeze / unfreeze (1.24)", smoke.step_bounce)
    finally:
        if not args.keep:
            smoke.cleanup()
        try:
            deps.client.close()
        except Exception:
            pass

    failed = [r for r in smoke.results if r[1] == "FAIL"]
    skipped = [r for r in smoke.results if r[1] == "SKIP"]
    print(f"\nResult: {len(smoke.results) - len(failed) - len(skipped)} passed, "
          f"{len(skipped)} skipped, {len(failed)} failed")
    for name, _, detail in failed:
        print(f"  FAILED: {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
