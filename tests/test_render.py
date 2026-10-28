"""export_audio, bounce_track and the audio checks — pure helpers, the full
dialog flow against a scripted fake UI (with real WAV files appearing on
disk), and audio_metrics against generated audio.

Nothing here runs osascript or touches Live. The fake dialog's labels and
layout are the documented 12.4.7 guesses, UNVERIFIED until the live pass.
Real-ffmpeg tests skip where ffmpeg is not installed.
"""

import math
import os
import struct
import wave

import pytest

from ableton_mcp import audio_metrics, presenters, render
from ableton_mcp.live_lock import LiveLock, LockError
from ableton_mcp.services import AbletonService
from ableton_mcp.ui_macos import MacUI, map_dialog_fields

from golden_runner import AllCapabilitiesHandshake


def _wav(path, seconds=2.0, rate=44100, amp=0.25, tail_silence=0.5, channels=2,
         clip=False):
    frames = int(seconds * rate)
    loud = frames - int(tail_silence * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(3)
        w.setframerate(rate)
        data = bytearray()
        for i in range(frames):
            v = amp * math.sin(2 * math.pi * 220 * i / rate) if i < loud else 0.0
            if clip and i < loud:
                v = 1.0 if v > 0 else -1.0
            iv = max(-(1 << 23), min((1 << 23) - 1, int(v * (1 << 23))))
            sample = struct.pack("<i", iv)[:3]
            data += sample * channels
        w.writeframes(bytes(data))
    return str(path)


# ── pure helpers ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("q, sig, position, expected", [
    (0.0, (4, 4), True, [1, 1, 1]),
    (5.25, (4, 4), True, [2, 2, 2]),
    (32.0, (4, 4), False, [8, 0, 0]),
    (32.1, (4, 4), False, [8, 0, 1]),     # lengths round UP to a sixteenth
    (3.0, (3, 4), True, [2, 1, 1]),
    (3.0, (6, 8), True, [2, 1, 1]),       # 6/8: a bar is 3 quarters, beats are eighths
    (1.5, (6, 8), False, [0, 3, 0]),
])
def test_beats_to_bbs(q, sig, position, expected):
    assert render.beats_to_bbs(q, sig[0], sig[1], position) == expected


DIALOG = [
    {"role": "AXStaticText", "value": "Rendered Track", "x": 20, "y": 40, "h": 16},
    {"role": "AXPopUpButton", "value": "Main", "x": 200, "y": 38, "h": 20},
    {"role": "AXStaticText", "value": "Render Start", "x": 20, "y": 70, "h": 16},
    {"role": "AXTextField", "value": "1", "x": 200, "y": 68, "h": 20},
    {"role": "AXTextField", "value": "1", "x": 240, "y": 68, "h": 20},
    {"role": "AXTextField", "value": "1", "x": 280, "y": 68, "h": 20},
    {"role": "AXStaticText", "value": "Render Length", "x": 20, "y": 100, "h": 16},
    {"role": "AXTextField", "value": "8.0.0", "x": 200, "y": 98, "h": 20},
    {"role": "AXCheckBox", "name": "Normalize", "value": 0, "x": 20, "y": 130, "h": 18},
    {"role": "AXStaticText", "value": "Sample Rate", "x": 20, "y": 160, "h": 16},
    {"role": "AXPopUpButton", "value": "22050", "x": 200, "y": 158, "h": 20},
]


def test_fields_are_found_by_label_never_by_index():
    m = map_dialog_fields(DIALOG, ["Rendered Track", "Render Start", "Render Length",
                                   "Normalize", "Sample Rate", "Create Video"])
    f = m["fields"]
    assert f["Rendered Track"]["controls"][0]["value"] == "Main"
    assert [c["x"] for c in f["Render Start"]["controls"]] == [200, 240, 280]
    assert len(f["Render Length"]["controls"]) == 1
    assert f["Normalize"]["controls"][0]["role"] == "AXCheckBox"
    assert f["Sample Rate"]["controls"][0]["value"] == "22050"
    assert m["missing"] == ["Create Video"]


def test_plan_takes_the_range_from_the_lom_plus_a_tail():
    status = {"tempo": 120.0, "signature_numerator": 4, "signature_denominator": 4,
              "last_event_time": 352.0}
    plan = render.plan_export(status, scope="main", start_beat=0.0, end_beat=None,
                              tail_beats=8.0, sample_rate=44100, bit_depth=24,
                              file_type="wav", normalize=False, convert_to_mono=False,
                              include_return_and_main_fx=True, dither="none", mp3=False)
    assert plan["end_beat"] == 360.0
    assert plan["fields"]["render_length"] == [90, 0, 0]
    assert plan["expected_duration_s"] == pytest.approx(180.0)
    assert plan["fields"]["normalize"] is False
    assert "include_return_and_main_fx" not in plan["fields"]  # stems only
    with pytest.raises(ValueError):
        render.plan_export(status, scope="main", start_beat=0, end_beat=None,
                           tail_beats=8, sample_rate=44100, bit_depth=24,
                           file_type="wav", normalize=False, convert_to_mono=False,
                           include_return_and_main_fx=True, dither="triangular", mp3=False)


# ── the export flow against a fake Live UI ──────────────────────────────────

class FakeExportUI(MacUI):
    """A scripted Export dialog. Field state lives here; "save" writes a WAV
    of the planned length into the folder the panel was pointed at."""

    def __init__(self, tmp_path, idle=120.0, sticky=None, seconds=None,
                 existing_alert=False, missing_label=None, live_12_4_7=False):
        super().__init__(runner=lambda a, t: (1, "", ""), platform="darwin",
                         sleep=lambda s: None, clock=self._tick)
        self._t = 0.0
        self.tmp = tmp_path
        self.idle = idle
        self.open = False
        self.calls = []
        self.sticky = sticky or {}        # label -> value the dialog refuses to change
        self.seconds = seconds
        self.missing_label = missing_label
        self.live_12_4_7 = live_12_4_7   # the real dialog's AX shape, see below
        self.panel = {}
        self.state = {"Rendered Track": "Main", "Render Start": ["1", "1", "1"],
                      "Render Length": "8.0.0", "Render as Loop": 0,
                      "Convert to Mono": 0, "Normalize": 1,
                      "Create Analysis File": 0, "Sample Rate": "22050",
                      "Encode PCM": 1, "File Type": "WAV", "Bit Depth": "16",
                      "Dither Options": "No Dither", "Encode MP3": 0,
                      "Create Video": 0, "Include Return and Main Effects": 0}
        if live_12_4_7:   # three length sliders, not one "b.b.s" text field
            self.state["Render Length"] = [8, 0, 0]

    def _tick(self):
        self._t += 1.0
        return self._t

    def user_idle_s(self):
        return self.idle

    def live_windows(self):
        wins = [{"title": "song [song Project] - Ableton Live 12 Suite",
                 "subrole": "AXStandardWindow"}]
        return {"frontmost": True, "windows": wins}

    def frontmost_app(self):
        return "Terminal"

    def activate(self, name):
        self.calls.append(("activate", name))
        return True

    def click_menu_item(self, menu, prefix, exclude=()):
        self.calls.append(("menu", menu, prefix))
        self.open = True
        return {"ok": True, "clicked": prefix + "…"}

    def read_window(self, title):
        if not self.open:
            return {"ok": False, "error": "no_window"}
        if self.live_12_4_7:
            return self._read_live_12_4_7()
        els, y = [], 20
        for label, value in self.state.items():
            if label == self.missing_label:
                continue
            y += 30
            if isinstance(value, int) and label != "Render Length":
                els.append({"role": "AXCheckBox", "name": label, "value": value,
                            "x": 20, "y": y, "h": 18})
                continue
            els.append({"role": "AXStaticText", "value": label, "x": 20, "y": y, "h": 16})
            if isinstance(value, list):
                for k, v in enumerate(value):
                    els.append({"role": "AXTextField", "value": v, "x": 200 + 40 * k,
                                "y": y - 2, "h": 20, "_label": label, "_part": k})
            else:
                role = "AXTextField" if label == "Render Length" else "AXPopUpButton"
                els.append({"role": role, "value": value, "x": 200, "y": y - 2,
                            "h": 20, "_label": label})
        els.append({"role": "AXButton", "name": "Export", "x": 200, "y": y + 40, "h": 20})
        return {"ok": True, "title": "Export Audio/Video", "elements": els}

    # Live 12.4.7's real Export dialog (AX dump, 10-28): toggles are nameless
    # AXButtons reading "On"/"Off", bar/beat/sixteenth boxes are AXSliders,
    # the MP3 label carries a suffix, and Export/Cancel have no AX name.
    SUFFIXED = {"Encode MP3": "Encode MP3 (CBR 320)"}

    def _read_live_12_4_7(self):
        els, y = [], 200
        for label, value in self.state.items():
            if label == self.missing_label:
                continue
            y += 22
            els.append({"role": "AXStaticText", "name": self.SUFFIXED.get(label, label),
                        "value": self.SUFFIXED.get(label, label), "x": 631, "y": y, "h": 11})
            if isinstance(value, list):
                for k, v in enumerate(value):
                    els.append({"role": "AXSlider", "name": None, "value": int(v),
                                "x": 815 + 25 * k, "y": y, "h": 11,
                                "_label": label, "_part": k})
            elif isinstance(value, int):
                els.append({"role": "AXButton", "name": None, "value": "On" if value else "Off",
                            "x": 831, "y": y - 2, "h": 16, "_label": label})
            else:
                els.append({"role": "AXPopUpButton", "name": None, "value": value,
                            "x": 831, "y": y - 2, "h": 18, "_label": label})
        els.append({"role": "AXButton", "name": None, "value": None, "x": 831, "y": y + 22,
                    "h": 16})                                   # Encoder Settings
        els.append({"role": "AXButton", "name": None, "value": None, "x": 685, "y": y + 60,
                    "h": 18, "_button": "Export"})
        els.append({"role": "AXButton", "name": None, "value": None, "x": 757, "y": y + 60,
                    "h": 18, "_button": "Cancel"})
        self._buttons = {(e["x"], e["y"]): e.get("_button") for e in els if e.get("_button")}
        self._by_pos = {(e["x"], e["y"]): e for e in els if e.get("_label")}
        return {"ok": True, "title": "Export Audio/Video", "elements": els}

    def act(self, window, element, action, value=None):
        if self.live_12_4_7:
            return self._act_live_12_4_7(element, action, value)
        self.calls.append(("act", element.get("_label") or element.get("name")
                           or element.get("match_name"), action, value))
        if action == "press":
            assert element.get("match_name") == "Export"
            self.open = False
            return {"ok": True}
        label = element.get("_label") or element.get("name")
        if label in self.sticky:
            return {"ok": True}
        if action == "toggle":
            self.state[label] = 1 if value else 0
        elif "_part" in element:
            self.state[label][element["_part"]] = str(value)
        else:
            self.state[label] = value
        return {"ok": True}

    def _act_live_12_4_7(self, element, action, value):
        # Like the JXA side, find the element by role + position only.
        pos = (element.get("x"), element.get("y"))
        self.calls.append(("act", element.get("_label") or self._buttons.get(pos)
                           or element.get("match_name"), action, value))
        if action == "press":
            if self._buttons.get(pos) != "Export":
                return {"ok": True}   # Cancel (or nothing): no save panel follows
            self.open = False
            return {"ok": True}
        hit = self._by_pos.get(pos)
        if hit is None:
            return {"ok": False, "error": "element_not_found"}
        label = hit["_label"]
        if label in self.sticky:
            return {"ok": True}
        if action == "toggle":
            assert hit["role"] == "AXButton"
            self.state[label] = 1 if value else 0
        elif action == "step":
            assert hit["role"] == "AXSlider"
            self.state[label][hit["_part"]] = int(value)
        elif action == "choose":
            self.state[label] = value
        else:
            return {"ok": False, "error": f"{action} unsupported on {hit['role']}"}
        return {"ok": True}

    def screenshot(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").write(b"png")
        return path

    def press_escape(self):
        self.calls.append(("escape",))
        self.open = False
        return True

    def save_panel(self, action, directory=None, filename=None, overwrite=False):
        self.calls.append(("panel", action, directory, filename, overwrite))
        if action == "fill":
            self.panel = {"dir": directory, "name": filename}
            return {"ok": True, "filename": filename}
        if action == "save":
            target = os.path.join(self.panel["dir"], self.panel["name"])
            if os.path.exists(target) and not overwrite:
                return {"ok": False, "error": "exists"}
            rate = int(self.state["Sample Rate"])
            _wav(target, seconds=self.seconds or 2.0, rate=rate, tail_silence=0.5)
            return {"ok": True}
        return {"ok": True}


class FakeService:
    """The four hooks render.py uses, without a wire."""

    def __init__(self, status=None, lock_error=None):
        self.status = {"is_playing": False, "record_mode": False,
                       "session_record": False, "tempo": 120.0,
                       "signature_numerator": 4, "signature_denominator": 4,
                       "last_event_time": 0.0, "soloed": [], "muted": [], "armed": []}
        self.status.update(status or {})
        self.lock_error = lock_error
        self.selected = []

    def check_ui_write(self, action):
        if self.lock_error:
            raise self.lock_error

    def live_status_raw(self):
        return dict(self.status)

    def select_track(self, index, track_type="regular", expect_track_name=None):
        self.selected.append(index)
        self.guards = getattr(self, "guards", []) + [expect_track_name]
        return {"selected": True, "track_name": "BASS"}


def _export(tmp_path, ui, service=None, **kw):
    kw.setdefault("output_dir", str(tmp_path / "renders"))
    kw.setdefault("file_stem", "mix")
    kw.setdefault("tail_beats", 0.0)
    kw.setdefault("end_beat", 4.0)  # 2 s at 120 BPM
    return render.export_audio(service or FakeService(), ui, sleep=lambda s: None,
                               clock=ui.clock, **kw)


def test_full_export_sets_reads_back_presses_export_and_verifies(tmp_path):
    ui = FakeExportUI(tmp_path)
    result = _export(tmp_path, ui, evidence_dir=str(tmp_path / "evidence"))
    assert result["ok"] is True, result
    # Stale dialog state was overwritten: 22050 → 44100, Normalize off.
    assert ui.state["Sample Rate"] == "44100" and ui.state["Normalize"] == 0
    assert ui.state["Render Start"] == ["1", "1", "1"]
    assert ui.state["Render Length"] == "1.0.0"
    assert result["dialog_readback"]["sample_rate"] == "44100"
    (f,) = result["files"]
    assert f["path"].endswith("renders/mix.wav") and f["track"] == "Main"
    assert f["duration_s"] == pytest.approx(2.0, abs=0.01)
    assert result["evidence_png"].startswith(str(tmp_path / "evidence"))
    # Export was pressed by its button; focus handed back.
    assert ("act", "Export", "press", None) in ui.calls
    assert ("activate", "Terminal") in ui.calls


def test_a_field_that_will_not_change_cancels_and_refuses(tmp_path):
    ui = FakeExportUI(tmp_path, sticky={"Sample Rate": True})
    with pytest.raises(render.RenderRefused) as e:
        _export(tmp_path, ui)
    assert e.value.reason == "readback_mismatch"
    assert e.value.details["diffs"][0]["field"] == "sample_rate"
    assert ("escape",) in ui.calls
    assert not any(c[:2] == ("act", "Export") for c in ui.calls)
    assert not os.path.exists(tmp_path / "renders" / "mix.wav")
    assert "22050" in presenters.render_refusal(e.value)


def test_a_renamed_label_refuses_before_setting_anything(tmp_path):
    ui = FakeExportUI(tmp_path, missing_label="Sample Rate")
    with pytest.raises(render.RenderRefused) as e:
        _export(tmp_path, ui)
    assert e.value.reason == "labels_missing" and e.value.details["missing"] == ["Sample Rate"]
    assert not any(c[0] == "act" for c in ui.calls)


def test_dry_run_reads_back_and_cancels(tmp_path):
    ui = FakeExportUI(tmp_path)
    result = _export(tmp_path, ui, dry_run=True)
    assert result["ok"] is True and result["dry_run"] is True
    assert ("escape",) in ui.calls
    assert not any(c[0] == "panel" for c in ui.calls)


@pytest.mark.parametrize("status, idle, reason", [
    ({"is_playing": True}, 120.0, "transport_busy"),
    ({"record_mode": True}, 120.0, "transport_busy"),
    ({}, 3.0, "user_active"),
])
def test_gates_refuse_before_any_click(tmp_path, status, idle, reason):
    ui = FakeExportUI(tmp_path, idle=idle)
    with pytest.raises(render.RenderRefused) as e:
        _export(tmp_path, ui, service=FakeService(status))
    assert e.value.reason == reason
    assert not any(c[0] == "menu" for c in ui.calls)


def test_the_lock_is_checked_first(tmp_path):
    ui = FakeExportUI(tmp_path)
    with pytest.raises(LockError):
        _export(tmp_path, ui, service=FakeService(lock_error=LockError("not_held")))
    assert ui.calls == []


def test_solos_warn_or_refuse(tmp_path):
    soloed = {"soloed": [{"index": 1, "name": "BASS"}]}
    ui = FakeExportUI(tmp_path)
    result = _export(tmp_path, ui, service=FakeService(soloed))
    assert any("soloed" in w for w in result["warnings"])
    with pytest.raises(render.RenderRefused) as e:
        _export(tmp_path, FakeExportUI(tmp_path), service=FakeService(soloed),
                strict_solo=True, overwrite=True)
    assert e.value.reason == "soloed"


def test_an_existing_file_is_never_replaced_silently(tmp_path):
    (tmp_path / "renders").mkdir()
    _wav(tmp_path / "renders" / "mix.wav")
    with pytest.raises(render.RenderRefused) as e:
        _export(tmp_path, FakeExportUI(tmp_path))
    assert e.value.reason == "exists"
    result = _export(tmp_path, FakeExportUI(tmp_path), overwrite=True)
    assert result["ok"] is True


def test_a_short_render_fails_verification(tmp_path):
    ui = FakeExportUI(tmp_path, seconds=1.0)  # Live wrote half the range
    result = _export(tmp_path, ui)
    assert result["ok"] is False
    assert any("duration" in f for f in result["files"][0]["failures"])


def test_track_scope_selects_and_verifies_the_track(tmp_path):
    service = FakeService()
    ui = FakeExportUI(tmp_path)
    result = _export(tmp_path, ui, service=service, scope="track", track=3)
    assert service.selected == [3]
    assert ui.state["Rendered Track"] == "Selected Tracks Only"
    assert ui.state["Include Return and Main Effects"] == 1
    assert result["ok"] is True


def test_service_requires_consent_and_macos(tmp_path):
    service = AbletonService(object(), AllCapabilitiesHandshake(),
                             ui=FakeExportUI(tmp_path))
    with pytest.raises(render.RenderRefused) as e:
        service.export_audio(output_dir=str(tmp_path), file_stem="x")
    assert e.value.reason == "not_allowed"
    assert "allow_ui_automation=true" in presenters.render_refusal(e.value)


# ── bounce_track ─────────────────────────────────────────────────────────────

class FakeBounceService(FakeService):
    def __init__(self, tmp_path, frozen=False, **kw):
        super().__init__(**kw)
        self.tmp = tmp_path
        self.frozen = frozen
        self.tracks = 3
        self.after_click = None
        self.checkpoints = []

    def track_render_state(self, index, track_type="regular"):
        files = []
        if index == 2 and self.tracks == 4:
            path = _wav(self.tmp / "bounce.wav")
            files = [{"name": "Bounce", "file_path": path}]
        return {"track_index": index, "track_name": "GTR", "track_count": self.tracks,
                "is_frozen": self.frozen, "can_be_frozen": True,
                "arrangement_files": files}

    def checkpoint_set(self, label, allow_ui_automation=False):
        self.checkpoints.append(label)
        return {"checkpointed": True, "checkpoint_path": "/x/Checkpoints/a.als"}


class FakeMenuUI(FakeExportUI):
    def __init__(self, tmp_path, service):
        super().__init__(tmp_path)
        self.service = service

    def click_menu_item(self, menu, prefix, exclude=()):
        self.calls.append(("menu", menu, prefix, tuple(exclude)))
        if prefix == "Freeze Track":
            self.service.frozen = True
        elif prefix == "Bounce to New Track":
            self.service.tracks = 4
        return {"ok": True, "clicked": prefix}


def test_bounce_to_new_track_returns_the_measured_file(tmp_path):
    service = FakeBounceService(tmp_path)
    ui = FakeMenuUI(tmp_path, service)
    result = render.bounce_track(service, ui, track_index=1, mode="bounce_to_new_track",
                                 expect_track_name="GTR")
    assert result["ok"] is True and service.selected == [1]
    assert service.guards == ["GTR"]  # the guard rides on the selection
    assert result["new_track"]["track_index"] == 2
    assert result["files"][0]["duration_s"] == pytest.approx(2.0, abs=0.01)
    assert ("menu", "Edit", "Bounce to New Track", ()) in ui.calls


def test_freeze_polls_the_frozen_flag_and_skips_when_already_frozen(tmp_path):
    service = FakeBounceService(tmp_path)
    ui = FakeMenuUI(tmp_path, service)
    assert render.bounce_track(service, ui, track_index=1, mode="freeze")["ok"] is True
    assert ("menu", "Edit", "Freeze Track", ("Freeze and Flatten",)) in ui.calls
    again = render.bounce_track(service, FakeMenuUI(tmp_path, service), track_index=1,
                                mode="freeze")
    assert again["note"] == "already frozen"


def test_bounce_in_place_needs_confirmation_and_checkpoints_first(tmp_path):
    service = FakeBounceService(tmp_path)
    with pytest.raises(render.RenderRefused) as e:
        render.bounce_track(service, FakeMenuUI(tmp_path, service), track_index=1,
                            mode="bounce_in_place")
    assert e.value.reason == "destructive" and service.checkpoints == []


def test_bounce_refuses_while_playing(tmp_path):
    service = FakeBounceService(tmp_path, status={"is_playing": True})
    with pytest.raises(render.RenderRefused) as e:
        render.bounce_track(service, FakeMenuUI(tmp_path, service), track_index=1,
                            mode="freeze")
    assert e.value.reason == "transport_busy"


# ── audio_metrics ───────────────────────────────────────────────────────────

SUMMARY = """[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -14.2 LUFS
    Threshold: -24.6 LUFS

  Loudness range:
    LRA:         6.1 LU

  True peak:
    Peak:       -0.8 dBFS
[Parsed_astats_1 @ 0x2] Peak level dB: -1.020000
[Parsed_astats_1 @ 0x2] RMS level dB: -17.500000
[silencedetect @ 0x3] silence_start: 178.5
[silencedetect @ 0x3] silence_end: 181.0 | silence_duration: 2.5
"""


def test_ffmpeg_summary_parsing():
    s = audio_metrics.parse_ffmpeg_summary(SUMMARY)
    assert (s["lufs_i"], s["lra"], s["true_peak_dbtp"]) == (-14.2, 6.1, -0.8)
    assert (s["peak_dbfs"], s["rms_db"]) == (-1.02, -17.5)
    assert s["silences"] == [{"start": 178.5, "end": 181.0}]


needs_ffmpeg = pytest.mark.skipif(audio_metrics.find_tool("ffmpeg") is None
                                  or audio_metrics.find_tool("ffprobe") is None,
                                  reason="ffmpeg/ffprobe not installed")


@needs_ffmpeg
def test_real_ffmpeg_measures_a_rendered_file(tmp_path):
    path = _wav(tmp_path / "a.wav", seconds=3.0, amp=0.5, tail_silence=1.0)
    m = audio_metrics.verify(path, expected_duration_s=3.0, sample_rate=44100,
                             bit_depth=24)
    assert m["ok"] is True, m["failures"]
    assert m["channels"] == 2 and m["bit_depth"] == 24
    assert m["peak_dbfs"] == pytest.approx(-6.0, abs=0.2)
    assert m["lufs_i"] is not None and m["true_peak_dbtp"] is not None
    assert m["trailing_silence_s"] == pytest.approx(1.0, abs=0.05)
    assert m["tail_ok"] is True and m["clipped_samples"] == 0


@needs_ffmpeg
def test_real_ffmpeg_flags_cut_tails_clipping_and_silence(tmp_path):
    cut = audio_metrics.verify(_wav(tmp_path / "cut.wav", tail_silence=0.0))
    assert cut["ok"] is False and any("tail cut" in f for f in cut["failures"])
    clipped = audio_metrics.verify(_wav(tmp_path / "clip.wav", clip=True))
    assert clipped["clipped_samples"] > 0
    assert any("clipping" in f for f in clipped["failures"])
    silent = audio_metrics.verify(_wav(tmp_path / "silent.wav", amp=0.0))
    assert any("silent" in f for f in silent["failures"])


# ── Live 12.4.7's real Export dialog shape (AX dump 10-28) ─────────────────

LIVE_12_4_7_ROWS = [
    {"role": "AXStaticText", "name": "Render Start", "value": "Render Start", "x": 631, "y": 243},
    {"role": "AXSlider", "value": 1, "x": 815, "y": 243},
    {"role": "AXStaticText", "name": ".", "value": ".", "x": 841, "y": 243},
    {"role": "AXSlider", "value": 1, "x": 846, "y": 243},
    {"role": "AXSlider", "value": 1, "x": 865, "y": 243},
    {"role": "AXStaticText", "name": "Render Length", "value": "Render Length", "x": 631, "y": 265},
    {"role": "AXSlider", "value": 0, "x": 815, "y": 265},
    {"role": "AXSlider", "value": 0, "x": 846, "y": 265},
    {"role": "AXSlider", "value": 1, "x": 865, "y": 265},
    {"role": "AXStaticText", "name": "Normalize", "value": "Normalize", "x": 631, "y": 379},
    {"role": "AXButton", "value": "Off", "x": 831, "y": 377},
    {"role": "AXStaticText", "name": "Encode MP3 (CBR 320)", "value": "Encode MP3 (CBR 320)",
     "x": 631, "y": 608},
    {"role": "AXButton", "value": "Off", "x": 831, "y": 606},
    {"role": "AXButton", "value": None, "x": 685, "y": 737},
    {"role": "AXButton", "value": None, "x": 757, "y": 737},
]


def test_live_12_4_7_rows_map_sliders_buttons_and_suffixed_labels():
    m = map_dialog_fields(LIVE_12_4_7_ROWS, ["Render Start", "Render Length",
                                             "Normalize", "Encode MP3"])
    assert m["missing"] == []
    f = m["fields"]
    assert [c["x"] for c in f["Render Start"]["controls"]] == [815, 846, 865]
    assert [c["y"] for c in f["Render Length"]["controls"]] == [265, 265, 265]
    assert f["Normalize"]["controls"][0]["role"] == "AXButton"
    assert f["Encode MP3"]["controls"][0]["y"] == 606


def test_on_off_and_integral_slider_values_read_back_normalized():
    assert render._normalize_readback("On") == "1"
    assert render._normalize_readback("Off") == "0"
    assert render._normalize_readback(1.0) == "1"


def test_nameless_export_button_is_the_left_of_the_bottom_pair():
    assert render.export_button(LIVE_12_4_7_ROWS) == {"role": "AXButton", "x": 685, "y": 737}
    assert render.export_button(LIVE_12_4_7_ROWS[:-1]) is None   # no pair: refuse, never guess


def test_full_export_against_the_live_12_4_7_dialog(tmp_path):
    ui = FakeExportUI(tmp_path, live_12_4_7=True)
    result = _export(tmp_path, ui, mp3=True)
    assert result["ok"] is True, result
    assert ui.state["Render Length"] == [1, 0, 0]          # 4 beats in 4/4, stepped
    assert ui.state["Encode MP3"] == 1 and ui.state["Normalize"] == 0
    assert ("act", "Export", "press", None) in ui.calls
    assert not any(c[:2] == ("act", "Cancel") for c in ui.calls)
