"""User Library preset loading by path (Remote Script) and the offline
plugin-state decoder (plugin_state.py), on synthetic files.

The XML below is a minimal stand-in for Live's .als/.adv structure (tracks
→ device chains → PluginDevice → Preset → Vst3Preset → ProcessorState as
wrapped hex). The real element nesting on 12.4.7 is UNVERIFIED until the
live pass decodes a real saved set. Runs anywhere — no Ableton, no network.
"""

import binascii
import gzip
import struct

import pytest

from ableton_mcp import plugin_state
from fake_ableton import FakeBrowserItem, make_harness

MODEL = "/Users/someone/Models/Amp Capture 01.json"
IR = "/Users/someone/IR/Cab 2x12.wav"


def _hex_wrapped(blob):
    h = binascii.hexlify(blob).decode("ascii").upper()
    return "\n".join(h[i:i + 64] for i in range(0, len(h), 64))


def _state_blob():
    # Length-prefixed UTF-8 + a UTF-16 string, as plugins tend to store them.
    utf16 = IR.encode("utf-16-le")
    return (b"\x00\x01" + struct.pack("<I", len(MODEL)) + MODEL.encode()
            + b"\x00\x00" + utf16 + b"\x00\x00{\"gain\": 0.5}")


def _als(tmp_path, gz=True, name="song.als"):
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Ableton><LiveSet><Tracks>
  <AudioTrack Id="1"><Name><EffectiveName Value="GTR"/><UserName Value=""/></Name>
    <DeviceChain><DeviceChain><Devices>
      <PluginDevice Id="2"><PluginDesc><Vst3PluginInfo Id="0"><Name Value="AIDA-X"/></Vst3PluginInfo></PluginDesc>
        <Preset><Vst3Preset Id="3"><ProcessorState>
{_hex_wrapped(_state_blob())}
        </ProcessorState><ControllerState></ControllerState></Vst3Preset></Preset>
      </PluginDevice>
      <PluginDevice Id="4"><PluginDesc><Vst3PluginInfo Id="0"><Name Value="Surge XT"/></Vst3PluginInfo></PluginDesc>
        <Preset><Vst3Preset Id="5"><ProcessorState>DEADBEEF</ProcessorState></Vst3Preset></Preset>
      </PluginDevice>
    </Devices></DeviceChain></DeviceChain>
  </AudioTrack>
  <MidiTrack Id="6"><Name><EffectiveName Value="KEYS"/><UserName Value="Keys Bus"/></Name>
    <DeviceChain><DeviceChain><Devices>
      <AuPluginDevice Id="7"><PluginDesc><AuPluginInfo Id="0"><Name Value="Some AU"/></AuPluginInfo></PluginDesc>
        <Preset><AuPreset Id="8"><Buffer>{_hex_wrapped(b"au-state " + MODEL.encode())}</Buffer></AuPreset></Preset>
      </AuPluginDevice>
    </Devices></DeviceChain></DeviceChain>
  </MidiTrack>
</Tracks></LiveSet></Ableton>"""
    path = tmp_path / name
    data = xml.encode("utf-8")
    path.write_bytes(gzip.compress(data) if gz else data)
    return str(path)


def test_decodes_every_plugin_with_paths_and_strings(tmp_path):
    result = plugin_state.read_plugin_states(_als(tmp_path))
    assert result["plugin_count"] == 3
    aida = result["plugins"][0]
    assert (aida["track"], aida["plugin"], aida["ordinal"]) == ("GTR", "AIDA-X", 0)
    state = aida["states"]["ProcessorState"]
    assert MODEL in state["paths"] and IR in state["paths"]
    assert any('"gain"' in s for s in state["strings"])
    assert state["bytes"] == len(_state_blob())
    # Empty ControllerState is skipped; the AU's Buffer is read, UserName wins.
    assert "ControllerState" not in aida["states"]
    au = result["plugins"][2]
    assert au["track"] == "Keys Bus" and au["device_tag"] == "AuPluginDevice"
    assert MODEL in au["states"]["Buffer"]["paths"]


def test_filters_by_track_and_plugin(tmp_path):
    path = _als(tmp_path)
    only = plugin_state.read_plugin_states(path, track="gtr", plugin="surge")
    assert [p["plugin"] for p in only["plugins"]] == ["Surge XT"]
    assert only["plugins"][0]["ordinal"] == 1


def test_reads_an_uncompressed_preset_too(tmp_path):
    path = _als(tmp_path, gz=False, name="amp.adv")
    assert plugin_state.read_plugin_states(path)["kind"] == "adv"


def test_vstpreset_chunks(tmp_path):
    comp = b"comp " + MODEL.encode()
    cont = b"controller-bits"
    header_len = 48
    list_offset = header_len + len(comp) + len(cont)
    data = (b"VST3" + struct.pack("<i", 1) + b"0123456789ABCDEF0123456789ABCDEF"
            + struct.pack("<q", list_offset) + comp + cont
            + b"List" + struct.pack("<i", 2)
            + b"Comp" + struct.pack("<qq", header_len, len(comp))
            + b"Cont" + struct.pack("<qq", header_len + len(comp), len(cont)))
    path = tmp_path / "amp.vstpreset"
    path.write_bytes(data)
    result = plugin_state.read_plugin_states(str(path))
    states = result["plugins"][0]["states"]
    assert MODEL in states["ProcessorState"]["paths"]
    assert states["ControllerState"]["bytes"] == len(cont)


def test_unreadable_files_are_refused(tmp_path):
    bad = tmp_path / "x.als"
    bad.write_bytes(b"not xml at all")
    with pytest.raises(plugin_state.PluginStateError):
        plugin_state.read_plugin_states(str(bad))
    with pytest.raises(plugin_state.PluginStateError):
        plugin_state.read_plugin_states(str(tmp_path / "missing.als"))
    notvst = tmp_path / "x.vstpreset"
    notvst.write_bytes(b"\x00" * 64)
    with pytest.raises(plugin_state.PluginStateError):
        plugin_state.read_plugin_states(str(notvst))


# ── the Remote Script half: path → browser item → load ─────────────────────

def _harness_with_user_library():
    harness = make_harness()
    preset = FakeBrowserItem("Amp A", uri="userlibrary:Presets/AudioFx/Amp A",
                             is_loadable=True, is_device=True)
    fx = FakeBrowserItem("Audio Effects", uri="userlibrary:Presets/AudioFx",
                         children=[preset])
    presets = FakeBrowserItem("Presets", uri="userlibrary:Presets", children=[fx])
    harness.app.browser.user_library = FakeBrowserItem(
        "User Library", uri="userlibrary:", children=[presets])
    rack = FakeBrowserItem("Kit Rack.adg", uri="places:Kits/Kit Rack",
                           is_loadable=True, is_device=True)
    harness.app.browser.user_folders = [
        FakeBrowserItem("Sound Kits", uri="places:Kits", children=[rack])]
    return harness


def _load(harness, path):
    return harness.process({"type": "load_browser_item_by_path", "params": {
        "track_index": 0, "path": path}})


def test_user_library_path_loads_with_the_extension_hidden():
    harness = _harness_with_user_library()
    resp = _load(harness, "/Users/x/Music/Ableton/User Library/Presets/Audio Effects/Amp A.adv")
    assert resp["status"] == "success", resp
    assert resp["result"]["item_name"] == "Amp A"
    assert resp["result"]["new_devices"] == ["Amp A"]
    assert harness.app.browser.loads[-1][0].uri == "userlibrary:Presets/AudioFx/Amp A"


def test_places_folder_path_loads():
    harness = _harness_with_user_library()
    resp = _load(harness, "/Volumes/Samples/Sound Kits/Kit Rack.adg")
    assert resp["status"] == "success", resp
    assert resp["result"]["item_name"] == "Kit Rack.adg"


def test_a_miss_names_the_level_and_its_contents():
    harness = _harness_with_user_library()
    resp = _load(harness, "/Users/x/Music/Ableton/User Library/Presets/Audio Effects/Amp B.adv")
    assert resp["status"] == "error"
    assert "no 'Amp B.adv'" in resp["message"] and "Amp A" in resp["message"]
    assert "not be indexed yet" in resp["message"]
    outside = _load(harness, "/tmp/elsewhere/thing.adv")
    assert "not inside the User Library" in outside["message"]


def test_a_load_that_adds_no_device_reports_loaded_false():
    # Live refuses some broken documents ("Not all list members have Ids")
    # with no alert and no error; the reply must not claim a load.
    harness = _harness_with_user_library()
    harness.app.browser.load_item = lambda item: None
    resp = _load(harness, "/Users/x/Music/Ableton/User Library/Presets/Audio Effects/Amp A.adv")
    assert resp["status"] == "success", resp
    assert resp["result"]["loaded"] is False
    assert resp["result"]["new_devices"] == []
    assert "no device appeared" in resp["result"]["reason"]


def test_reloading_a_same_named_preset_still_counts_as_loaded():
    harness = _harness_with_user_library()
    path = "/Users/x/Music/Ableton/User Library/Presets/Audio Effects/Amp A.adv"
    assert _load(harness, path)["result"]["loaded"] is True
    again = _load(harness, path)["result"]
    assert again["loaded"] is True and again["new_devices"] == ["Amp A"]
