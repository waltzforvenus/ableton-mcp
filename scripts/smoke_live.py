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

This is intentionally NOT a pytest test: CLAUDE.md's rule is that tests/
never needs Ableton, and this file needs nothing else.
"""

from __future__ import annotations

import argparse
import sys
import time

from ableton_mcp.app import Settings, build_deps
from ableton_mcp.remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION


class Skip(Exception):
    """Raised by a step to report an unmet precondition, not a failure."""


class Smoke:
    def __init__(self, deps):
        self.deps = deps
        self.svc = deps.service
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

    def step_playback(self):
        self.svc.start_playback()
        time.sleep(0.3)
        self.svc.stop_playback()
        return "started, stopped"

    def step_save_set(self):
        result = self.svc.save_set()
        if result.get("saved"):
            return f"saved via {result.get('method')}"
        raise Skip(f"this Live build exposes no save API (tried {result.get('attempts')})")

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
    smoke = Smoke(deps)
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
        smoke.run("playback start/stop", smoke.step_playback)
        smoke.run("save set", smoke.step_save_set)
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
