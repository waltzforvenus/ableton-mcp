"""Model: render audio out of Live — the Export dialog and Edit-menu bounces.

Live's Python API has no render, export, freeze or bounce (only
``Track.is_frozen``), so every bounce so far was Mert pressing keys, or an
agent clicking blind: ~20 minutes "driving the export window without seeing
it", a 4-bar export of the wrong section from a stray Return, renders that
kept a stale 22050 Hz from the last export, a first render cut at 175.76 s
that lost the effect tails, stems written to ~/Desktop instead of the target
folder, and "Can't get slider 6 ... Invalid index" when a positional lookup
shifted. What did work, ~380 times, was Live's own offline render through
File › Export Audio/Video — so this module drives exactly that, but
deterministically:

- **gates first**: macOS, explicit consent, the Live lock, the user idle
  (not mid-edit), transport stopped and not recording, no dialog open;
- **range from the LOM**: ``last_event_time`` plus a tail, never whatever
  the dialog last held;
- **fields by label, read back**: every field is located by its on-screen
  label (``ui_macos.map_dialog_fields``), set, and read back; any mismatch
  cancels the dialog and refuses with the full diff;
- **Export by its button**, never a blind Return; the save panel's folder
  is typed into Go-to-folder and the name field verified; an existing file
  is never replaced unless asked;
- **the filesystem is the completion signal** (no LOM calls while Live's
  main thread renders), then every file is measured (audio_metrics).

Bounces (Freeze, Bounce to New Track, Bounce in Place) are Edit-menu items
acting on the selected track: select through the LOM, verify the selection,
click by title, then verify through the LOM (frozen flag, track count, the
new clip's file).

All of it is UNVERIFIED against a real Live until the live pass: the AX
labels, the save panel's shape and the timing are the documented guesses
the tests pin, not observations.
"""

from __future__ import annotations

import glob
import math
import os
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import audio_metrics
from .ui_macos import map_dialog_fields, summarize_windows

EXPORT_WINDOW = "Export Audio/Video"

# The Export dialog's labels on Live 12.4.7 (from an evidence screenshot of
# the dialog, 10-21). Field → (label, kind).
FIELD_LABELS = {
    "rendered_track": ("Rendered Track", "choose"),
    "render_start": ("Render Start", "position"),
    "render_length": ("Render Length", "length"),
    "include_return_and_main_fx": ("Include Return and Main Effects", "toggle"),
    "render_as_loop": ("Render as Loop", "toggle"),
    "convert_to_mono": ("Convert to Mono", "toggle"),
    "normalize": ("Normalize", "toggle"),
    "create_analysis_file": ("Create Analysis File", "toggle"),
    "sample_rate": ("Sample Rate", "choose"),
    "encode_pcm": ("Encode PCM", "toggle"),
    "file_type": ("File Type", "choose"),
    "bit_depth": ("Bit Depth", "choose"),
    "dither": ("Dither Options", "choose"),
    "encode_mp3": ("Encode MP3", "toggle"),
    "create_video": ("Create Video", "toggle"),
}

SCOPES = {"main": "Main", "all_tracks": "All Individual Tracks",
          "track": "Selected Tracks Only"}
FILE_TYPES = {"wav": "WAV", "aiff": "AIFF", "flac": "FLAC"}
DITHER = {"none": "No Dither", "triangular": "Triangular", "rectangular": "Rectangular",
          "pow-r 1": "POW-r 1", "pow-r 2": "POW-r 2", "pow-r 3": "POW-r 3"}
EXTENSIONS = {"wav": ".wav", "aiff": ".aif", "flac": ".flac"}

BOUNCE_MENU = {
    "freeze": ("Edit", "Freeze Track", ("Freeze and Flatten",)),
    "unfreeze": ("Edit", "Unfreeze Track", ()),
    "bounce_to_new_track": ("Edit", "Bounce to New Track", ()),
    "bounce_in_place": ("Edit", "Bounce Track in Place", ()),
}


class RenderRefused(Exception):
    """A gate or a readback said no. ``reason`` is a stable code; ``details``
    the data the presenter shows. Nothing was rendered."""

    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


# ── pure helpers ─────────────────────────────────────────────────────────────

def beats_to_bbs(quarters: float, numerator: int, denominator: int,
                 position: bool) -> List[int]:
    """Quarter-note beats → Live's bar / beat / sixteenth.

    A beat is the time signature's denominator note (an eighth in 6/8), and
    a sixteenth is a quarter of a quarter. Positions are 1-based ("1.1.1" is
    the start); lengths are counts ("8.0.0" is eight bars) and round UP to
    the next sixteenth, so a range never ends a hair before the tail.
    """
    if numerator <= 0 or denominator <= 0:
        raise ValueError("bad time signature %r/%r" % (numerator, denominator))
    beat_q = 4.0 / denominator
    bar_q = numerator * beat_q
    sixteenths_total = quarters / 0.25
    sixteenths_total = (math.ceil(sixteenths_total - 1e-9) if not position
                        else int(round(sixteenths_total)))
    per_beat = int(round(beat_q / 0.25)) or 1
    per_bar = per_beat * numerator
    bars, rem = divmod(int(sixteenths_total), per_bar)
    beats, six = divmod(rem, per_beat)
    if position:
        return [bars + 1, beats + 1, six + 1]
    return [bars, beats, six]


def _bbs_text(parts: Sequence[int]) -> str:
    return ".".join(str(p) for p in parts)


def _normalize_readback(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    text = str(value if value is not None else "").strip()
    return {"true": "1", "false": "0"}.get(text.lower(), text)


# ── export_audio ─────────────────────────────────────────────────────────────

def plan_export(status: Dict[str, Any], *, scope: str, start_beat: float,
                end_beat: Optional[float], tail_beats: float, sample_rate: int,
                bit_depth: int, file_type: str, normalize: bool,
                convert_to_mono: bool, include_return_and_main_fx: bool,
                dither: str, mp3: bool) -> Dict[str, Any]:
    """The exact dialog state to request, from the LOM preflight."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {sorted(SCOPES)}")
    if file_type not in FILE_TYPES:
        raise ValueError(f"file_type must be one of {sorted(FILE_TYPES)}")
    if bit_depth not in (16, 24, 32):
        raise ValueError("bit_depth must be 16, 24 or 32")
    if dither not in DITHER:
        raise ValueError(f"dither must be one of {sorted(DITHER)}")
    if dither != "none" and bit_depth != 16:
        raise ValueError("dither only applies to 16-bit renders")
    num = status.get("signature_numerator") or 4
    den = status.get("signature_denominator") or 4
    if end_beat is None:
        last = status.get("last_event_time")
        if last is None:
            last = status.get("song_length")
        if last is None:
            raise RenderRefused("no_end", note="Live reported no last_event_time or song_length; pass end_beat")
        end_beat = float(last) + float(tail_beats)
    if end_beat <= start_beat:
        raise ValueError(f"end_beat {end_beat} must be after start_beat {start_beat}")
    tempo = status.get("tempo") or 120.0
    length = end_beat - start_beat
    start_parts = beats_to_bbs(start_beat, num, den, position=True)
    length_parts = beats_to_bbs(length, num, den, position=False)
    fields: Dict[str, Any] = {
        "rendered_track": SCOPES[scope],
        "render_start": start_parts,
        "render_length": length_parts,
        "render_as_loop": False,
        "convert_to_mono": bool(convert_to_mono),
        "normalize": bool(normalize),
        "create_analysis_file": False,
        "sample_rate": str(int(sample_rate)),
        "encode_pcm": True,
        "file_type": FILE_TYPES[file_type],
        "bit_depth": str(int(bit_depth)),
        "dither": DITHER[dither],
        "encode_mp3": bool(mp3),
        "create_video": False,
    }
    if scope != "main":
        fields["include_return_and_main_fx"] = bool(include_return_and_main_fx)
    # Live renders whole sixteenths; the expected duration is that span.
    rendered_beats = sum(p * w for p, w in zip(
        length_parts, (num * 4.0 / den, 4.0 / den, 0.25)))
    return {"fields": fields, "start_beat": start_beat, "end_beat": end_beat,
            "expected_duration_s": rendered_beats * 60.0 / float(tempo),
            "tempo": tempo, "signature": [num, den]}


def _apply_field(ui, mapping: Dict[str, Any], key: str, want: Any) -> Dict[str, Any]:
    label, kind = FIELD_LABELS[key]
    entry = mapping["fields"].get(label)
    if entry is None:
        return {"ok": False, "error": "label_missing", "label": label}
    controls = entry["controls"]
    if kind == "toggle":
        return ui.act(EXPORT_WINDOW, controls[0], "toggle", bool(want))
    if kind == "choose":
        return ui.act(EXPORT_WINDOW, controls[0], "choose", want)
    # position / length: one text field "b.b.s", or three boxes.
    parts = list(want)
    if len(controls) >= 3:
        results = [ui.act(EXPORT_WINDOW, c, "text", str(p))
                   for c, p in zip(controls[:3], parts)]
        bad = [r for r in results if not r.get("ok")]
        return bad[0] if bad else {"ok": True}
    return ui.act(EXPORT_WINDOW, controls[0], "text", _bbs_text(parts))


def _readback(mapping: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, (label, kind) in FIELD_LABELS.items():
        entry = mapping["fields"].get(label)
        if entry is None:
            continue
        values = [c.get("value") for c in entry["controls"]]
        if kind in ("position", "length"):
            if len(values) >= 3:
                out[key] = [_normalize_readback(v) for v in values[:3]]
            else:
                out[key] = _normalize_readback(values[0]).split(".")
        else:
            out[key] = _normalize_readback(values[0])
    return out


def _diff(fields: Dict[str, Any], readback: Dict[str, Any]) -> List[Dict[str, Any]]:
    diffs = []
    for key, want in fields.items():
        got = readback.get(key)
        if isinstance(want, list):
            want_n = [str(int(p)) for p in want]
            got_n = [str(g).strip().lstrip("0") or "0" for g in (got or [])]
            ok = got_n == [w.lstrip("0") or "0" for w in want_n]
        else:
            want_n = _normalize_readback(want)
            ok = got is not None and (
                str(got).lower() == want_n.lower()
                or str(got).lower().startswith(want_n.lower()))
        if not ok:
            diffs.append({"field": key, "label": FIELD_LABELS[key][0],
                          "requested": want, "dialog_shows": got})
    return diffs


def export_audio(service: Any, ui: Any, *, scope: str = "main",
                 track: Optional[int] = None, start_beat: float = 0.0,
                 end_beat: Optional[float] = None, tail_beats: float = 8.0,
                 output_dir: str, file_stem: str, sample_rate: int = 44100,
                 bit_depth: int = 24, file_type: str = "wav", mp3: bool = False,
                 normalize: bool = False, convert_to_mono: bool = False,
                 include_return_and_main_fx: bool = True, dither: str = "none",
                 overwrite: bool = False, require_user_idle_s: float = 30.0,
                 dry_run: bool = False, evidence_dir: Optional[str] = None,
                 timeout_s: float = 900.0, strict_solo: bool = False,
                 ffmpeg: Optional[str] = None, ffprobe: Optional[str] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
    started = clock()
    warnings: List[str] = []
    output_dir = os.path.abspath(os.path.expanduser(output_dir or ""))
    if not file_stem or "/" in file_stem:
        raise ValueError("file_stem must be a plain file name without folders")
    if os.path.realpath(output_dir).startswith(("/tmp", "/private/tmp")) and evidence_dir is None:
        warnings.append("output_dir is under /tmp: some image/file viewers refuse it")

    # 0. gates (lock first: nothing else may run without it)
    service.check_ui_write("export_audio")
    idle = ui.user_idle_s()
    if require_user_idle_s and idle is not None and idle < require_user_idle_s:
        raise RenderRefused("user_active", idle_s=idle, required_s=require_user_idle_s)
    windows = summarize_windows(ui.live_windows())
    if not windows["readable"]:
        raise RenderRefused("ui_unreadable")
    if windows["modal_dialog"]:
        raise RenderRefused("modal_open", modal_dialog=windows["modal_dialog"])

    # 1. LOM preflight
    status = service.live_status_raw()
    if status.get("is_playing") or status.get("record_mode") or status.get("session_record"):
        raise RenderRefused("transport_busy", is_playing=status.get("is_playing"),
                            record_mode=status.get("record_mode"),
                            session_record=status.get("session_record"))
    soloed = status.get("soloed") or []
    if soloed:
        names = [s.get("name") for s in soloed]
        if strict_solo:
            raise RenderRefused("soloed", soloed=names)
        warnings.append(f"tracks are soloed and will shape the render: {names}")
    if scope == "track":
        if track is None:
            raise ValueError("scope='track' needs track")
        sel = service.select_track(track)
        if not sel.get("selected"):
            raise RenderRefused("select_failed", selection=sel)

    plan = plan_export(status, scope=scope, start_beat=start_beat, end_beat=end_beat,
                       tail_beats=tail_beats, sample_rate=sample_rate,
                       bit_depth=bit_depth, file_type=file_type, normalize=normalize,
                       convert_to_mono=convert_to_mono,
                       include_return_and_main_fx=include_return_and_main_fx,
                       dither=dither, mp3=mp3)
    ext = EXTENSIONS[file_type]
    target = os.path.join(output_dir, file_stem + ext)
    if scope == "main" and os.path.exists(target) and not overwrite:
        raise RenderRefused("exists", path=target)
    os.makedirs(output_dir, exist_ok=True)
    before = set(glob.glob(os.path.join(glob.escape(output_dir), glob.escape(file_stem) + "*" + ext)))

    previous_app = ui.frontmost_app()
    result: Dict[str, Any] = {"ok": False, "plan": plan, "warnings": warnings,
                              "preflight": {"soloed": [s.get("name") for s in soloed],
                                            "muted": [m.get("name") for m in status.get("muted") or []],
                                            "armed": [a.get("name") for a in status.get("armed") or []],
                                            "playing": status.get("is_playing"),
                                            "user_idle_s": idle}}
    opened = False
    try:
        # 2. open the dialog by its menu title (prefix: "…" vs "..." bit once)
        click = ui.click_menu_item("File", "Export Audio/Video")
        if not click.get("ok"):
            raise RenderRefused("menu", menu=click)
        if not ui.wait_for(lambda: (ui.read_window(EXPORT_WINDOW) or {}).get("ok"), 8.0):
            raise RenderRefused("dialog_did_not_open")
        opened = True

        # 3. map labels → controls; refuse on any missing label
        labels = [label for key, (label, _k) in FIELD_LABELS.items() if key in plan["fields"]]
        mapping = map_dialog_fields(ui.read_window(EXPORT_WINDOW).get("elements") or [], labels)
        if mapping["missing"]:
            raise RenderRefused("labels_missing", missing=mapping["missing"])

        # 4. set each field, then re-read everything and diff
        problems = []
        for key, want in plan["fields"].items():
            r = _apply_field(ui, mapping, key, want)
            if not r.get("ok"):
                problems.append({"field": key, **r})
            mapping = map_dialog_fields(
                ui.read_window(EXPORT_WINDOW).get("elements") or [], labels)
        readback = _readback(mapping)
        result["dialog_readback"] = readback
        diffs = _diff(plan["fields"], readback)
        if problems or diffs:
            raise RenderRefused("readback_mismatch", diffs=diffs, problems=problems)

        # 5. evidence, and the dry run stops here
        folder = evidence_dir
        if folder:
            shot = os.path.join(os.path.expanduser(folder),
                                f"export-dialog-{time.strftime('%Y%m%d-%H%M%S')}.png")
            result["evidence_png"] = ui.screenshot(shot)
        if dry_run:
            ui.press_escape()
            opened = False
            result.update(ok=True, dry_run=True)
            return result

        # 6. Export by its button, then the save panel
        pressed = ui.act(EXPORT_WINDOW, {"role": "AXButton", "match_name": "Export"}, "press")
        if not pressed.get("ok"):
            raise RenderRefused("export_button", detail=pressed)
        opened = False  # the dialog has gone; the save panel owns the screen
        filled = ui.save_panel("fill", directory=output_dir, filename=file_stem + ext)
        if not filled.get("ok"):
            ui.save_panel("cancel")
            raise RenderRefused("save_panel", detail=filled)
        if filled.get("filename") not in (None, file_stem + ext, file_stem):
            ui.save_panel("cancel")
            raise RenderRefused("save_panel_name", shows=filled.get("filename"),
                                wanted=file_stem + ext)
        saved = ui.save_panel("save", overwrite=overwrite)
        if not saved.get("ok"):
            if saved.get("error") == "exists":
                raise RenderRefused("exists", path=target)
            raise RenderRefused("save_panel", detail=saved)

        # 7. the filesystem is the completion signal
        def finished():
            now = set(glob.glob(os.path.join(glob.escape(output_dir),
                                             glob.escape(file_stem) + "*" + ext)))
            fresh = sorted(p for p in now if p not in before or overwrite)
            if not fresh:
                return None
            sizes = [os.path.getsize(p) for p in fresh]
            sleep(2.0)
            if [os.path.getsize(p) for p in fresh] != sizes or 0 in sizes:
                return None
            busy = summarize_windows(ui.live_windows())
            if busy.get("modal_dialog"):
                return None
            return fresh

        files = ui.wait_for(finished, timeout_s, interval_s=1.0)
        if not files:
            result.update(error="timeout", partial=sorted(
                set(glob.glob(os.path.join(glob.escape(output_dir), glob.escape(file_stem) + "*"))) - before))
            return result

        # 8. measure every file
        track_names = {t.get("name") for t in (status.get("all_tracks") or [])}
        out_files = []
        all_ok = True
        for path in files:
            base = os.path.splitext(os.path.basename(path))[0]
            suffix = base[len(file_stem):].strip()
            m = audio_metrics.verify(path, expected_duration_s=plan["expected_duration_s"],
                                     tolerance_s=0.1, sample_rate=sample_rate,
                                     bit_depth=bit_depth, ffmpeg=ffmpeg, ffprobe=ffprobe)
            m["track"] = suffix or ("Main" if scope == "main" else None)
            all_ok = all_ok and m.get("ok")
            out_files.append(m)
        result.update(ok=all_ok, files=out_files,
                      expected_duration_s=plan["expected_duration_s"])
        if track_names:
            result["unmatched_stems"] = [f["track"] for f in out_files
                                         if f["track"] and f["track"] not in track_names
                                         and f["track"] != "Main"]
        return result
    finally:
        if opened:
            ui.press_escape()
        if previous_app and not previous_app.startswith("Live"):
            ui.activate(previous_app)
        result["elapsed_s"] = round(clock() - started, 1)


# ── bounce_track ─────────────────────────────────────────────────────────────

def bounce_track(service: Any, ui: Any, *, track_index: int, mode: str,
                 expect_track_name: Optional[str] = None,
                 confirm_destructive: bool = False, timeout_s: float = 300.0,
                 checkpoint_label: Optional[str] = None,
                 ffmpeg: Optional[str] = None, ffprobe: Optional[str] = None) -> Dict[str, Any]:
    if mode not in BOUNCE_MENU:
        raise ValueError(f"mode must be one of {sorted(BOUNCE_MENU)}")
    if mode == "bounce_in_place" and not confirm_destructive:
        raise RenderRefused("destructive", mode=mode)
    service.check_ui_write("bounce_track")
    windows = summarize_windows(ui.live_windows())
    if not windows["readable"]:
        raise RenderRefused("ui_unreadable")
    if windows["modal_dialog"]:
        raise RenderRefused("modal_open", modal_dialog=windows["modal_dialog"])
    status = service.live_status_raw()
    if status.get("is_playing") or status.get("record_mode") or status.get("session_record"):
        raise RenderRefused("transport_busy", is_playing=status.get("is_playing"),
                            record_mode=status.get("record_mode"))
    result: Dict[str, Any] = {"ok": False, "mode": mode}
    if mode == "bounce_in_place":
        # Destructive: keep a way back first (R1's checkpoint).
        cp = service.checkpoint_set(checkpoint_label or "before-bounce-in-place",
                                    allow_ui_automation=True)
        result["checkpoint"] = cp
        if not cp.get("checkpointed"):
            raise RenderRefused("checkpoint_failed", checkpoint=cp)
    before = service.track_render_state(track_index)
    if mode in ("freeze", "bounce_to_new_track", "bounce_in_place") and before.get("can_be_frozen") is False:
        raise RenderRefused("cannot_freeze", track=before.get("track_name"))
    if mode == "freeze" and before.get("is_frozen"):
        result.update(ok=True, note="already frozen", after=before)
        return result
    sel = service.select_track(track_index, expect_track_name=expect_track_name)
    if not sel.get("selected"):
        raise RenderRefused("select_failed", selection=sel)
    menu, prefix, exclude = BOUNCE_MENU[mode]
    previous_app = ui.frontmost_app()
    try:
        click = ui.click_menu_item(menu, prefix, exclude=exclude)
        if not click.get("ok"):
            raise RenderRefused("menu", menu=click)
        result["clicked"] = click.get("clicked")

        def done():
            try:
                state = service.track_render_state(track_index)
            except Exception:
                return None  # Live's main thread is busy rendering: wait
            if mode == "freeze":
                return state if state.get("is_frozen") else None
            if mode == "unfreeze":
                return state if state.get("is_frozen") is False else None
            if mode == "bounce_to_new_track":
                if state.get("track_count", 0) > before.get("track_count", 0):
                    return state
                return None
            # in place: the track's clips now point at new files
            old = {f.get("file_path") for f in before.get("arrangement_files") or []}
            new = {f.get("file_path") for f in state.get("arrangement_files") or []}
            return state if new and new != old else None

        after = ui.wait_for(done, timeout_s, interval_s=1.0)
        if not after:
            result["error"] = "not_confirmed"
            return result
        result["after"] = after
        if mode == "bounce_to_new_track":
            new_track = service.track_render_state(track_index + 1)
            result["new_track"] = new_track
            files = [f["file_path"] for f in new_track.get("arrangement_files") or []]
        elif mode == "bounce_in_place":
            files = [f["file_path"] for f in after.get("arrangement_files") or []]
        else:
            files = []
        result["files"] = [audio_metrics.analyze(p, ffmpeg=ffmpeg, ffprobe=ffprobe)
                           for p in files if p and os.path.isfile(p)]
        result["ok"] = True
        return result
    finally:
        if previous_app and not previous_app.startswith("Live"):
            ui.activate(previous_app)
