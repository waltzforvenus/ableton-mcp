"""Display-string parameter values, per-device on/off state, and the
load_devices batch — the MCP-BOTTLENECKS quick wins.

The fake parameters below render like real Live devices do (a log-scaled
time that switches from "ms" to "s", a dB gain, a labelled picker), so the
inverse search is exercised against the same shapes it meets in Live.
Runs anywhere — no Ableton, no network.
"""

import math

import pytest

from fake_ableton import FakeDevice, FakeParameter, make_harness


class LogTimeParameter(FakeParameter):
    """0..1 → 10 ms .. 10 s, logarithmic, shown as Live shows times."""

    def str_for_value(self, value):
        seconds = 0.01 * 10 ** (3 * float(value))
        if seconds < 1.0:
            return "%.1f ms" % (seconds * 1000)
        return "%.2f s" % seconds


class CurvedGainParameter(FakeParameter):
    """0..1 → -inf .. +6 dB on a curve, "-inf dB" at the bottom."""

    def str_for_value(self, value):
        v = float(value)
        if v <= 0:
            return "-inf dB"
        return "%.1f dB" % (20 * math.log10(v ** 2) + 6)


class PercentParameter(FakeParameter):
    """0..1 shown as a percentage, as Live's Dry/Wet knobs are."""

    def str_for_value(self, value):
        return "%.0f %%" % (float(value) * 100)


class PickerParameter(FakeParameter):
    def __init__(self, name, labels, value=0.0):
        super().__init__(name, value, 0.0, float(len(labels) - 1), is_quantized=True)
        self.value_items = list(labels)

    def str_for_value(self, value):
        return self.value_items[int(round(float(value)))]


def _harness_with_device():
    harness = make_harness()
    device = FakeDevice("Reverb", parameters=[
        FakeParameter("Device On", 1.0, 0.0, 1.0, is_quantized=True),
        LogTimeParameter("Decay Time", 0.5),
        CurvedGainParameter("Output", 0.5),
        PickerParameter("Quality", ["Eco", "Mid", "Hi Quality"]),
        PercentParameter("Dry/Wet", 0.5),
    ])
    track = harness.song.tracks[0]
    track.devices = list(track.devices) + [device]
    return harness, len(track.devices) - 1, device


def _set_many(harness, device_index, parameters):
    resp = harness.process({"type": "set_device_parameters", "params": {
        "track_index": 0, "device_index": device_index, "parameters": parameters}})
    assert resp["status"] == "success", resp
    return {row["name"]: row for row in resp["result"]["parameters"]}


def test_log_time_display_strings_land_on_the_right_raw_value():
    harness, index, device = _harness_with_device()
    rows = _set_many(harness, index, {"Decay Time": "3.6 s"})
    row = rows["Decay Time"]
    assert row["display_value"] == "3.60 s"
    assert row["requested_display"] == "3.6 s"
    assert row["resolved_by"] == "display_search"
    assert row["value"] == pytest.approx(math.log10(360) / 3, abs=2e-3)
    # The ms branch of the same parameter, and "0.028 s" means the same.
    assert _set_many(harness, index, {"Decay Time": "28 ms"})["Decay Time"][
        "display_value"] == "28.0 ms"
    assert _set_many(harness, index, {"Decay Time": "0.028 s"})["Decay Time"][
        "display_value"] == "28.0 ms"


def test_curved_gain_including_minus_infinity():
    harness, index, _ = _harness_with_device()
    assert _set_many(harness, index, {"Output": "-1.5 dB"})["Output"][
        "display_value"] == "-1.5 dB"
    row = _set_many(harness, index, {"Output": "-inf dB"})["Output"]
    assert row["value"] == 0.0 and row["display_value"] == "-inf dB"


def test_out_of_range_display_values_clamp_and_say_so():
    harness, index, _ = _harness_with_device()
    row = _set_many(harness, index, {"Decay Time": "60 s"})["Decay Time"]
    assert row["value"] == 1.0 and row["clamped"] is True
    assert row["resolved_by"] == "display_clamped"


def test_picker_labels_and_percent():
    harness, index, _ = _harness_with_device()
    rows = _set_many(harness, index, {"Quality": "hi quality", "Dry/Wet": "35 %"})
    assert rows["Quality"]["value"] == 2.0 and rows["Quality"]["resolved_by"] == "label"
    assert rows["Dry/Wet"]["display_value"] == "35 %"
    assert rows["Dry/Wet"]["value"] == pytest.approx(0.35, abs=1e-2)


def test_bad_display_strings_fail_their_row_only():
    harness, index, device = _harness_with_device()
    rows = _set_many(harness, index, {"Decay Time": "-3 dB", "Quality": "Ultra",
                                      "Output": "-6 dB"})
    assert "different units" in rows["Decay Time"]["error"]
    assert "not one of" in rows["Quality"]["error"] and "Hi Quality" in rows["Quality"]["error"]
    assert rows["Output"]["display_value"] == "-6.0 dB"  # still landed


def test_bare_numbers_even_as_text_stay_raw():
    harness, index, device = _harness_with_device()
    row = _set_many(harness, index, {"Decay Time": "0.25"})["Decay Time"]
    assert row["value"] == 0.25 and "requested_display" not in row
    resp = harness.process({"type": "set_device_parameter", "params": {
        "track_index": 0, "device_index": index, "parameter": "Decay Time",
        "value": 0.75}})
    assert resp["result"]["value"] == 0.75
    assert "requested_display" not in resp["result"]


def test_singular_setter_accepts_display_strings():
    harness, index, _ = _harness_with_device()
    resp = harness.process({"type": "set_device_parameter", "params": {
        "track_index": 0, "device_index": index, "parameter": "Output",
        "value": "+2 dB"}})
    assert resp["status"] == "success", resp
    assert resp["result"]["display_value"] == "2.0 dB"
    assert resp["result"]["requested_display"] == "+2 dB"


# ── device on/off state ─────────────────────────────────────────────────────

def test_track_info_and_snapshot_report_device_state():
    harness, index, device = _harness_with_device()
    device.parameters[0].value = 0.0
    device.is_active = False
    info = harness.process({"type": "get_track_info", "params": {"track_index": 0}})
    entry = info["result"]["devices"][index]
    assert entry["is_active"] is False and entry["device_on"] is False
    snap = harness.process({"type": "get_session_snapshot", "params": {
        "include_notes": False, "include_params": False, "tracks": [0]}})
    snap_entry = snap["result"]["tracks"][0]["devices"][index]
    assert snap_entry["is_active"] is False and snap_entry["device_on"] is False


def test_device_state_is_none_where_unreadable():
    harness, index, device = _harness_with_device()
    del device.is_active
    device.parameters = [FakeParameter("Gain", 0.5)]
    info = harness.process({"type": "get_track_info", "params": {"track_index": 0}})
    entry = info["result"]["devices"][index]
    assert entry["is_active"] is None and entry["device_on"] is None
