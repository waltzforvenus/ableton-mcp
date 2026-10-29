"""The offline preset check that keeps a broken document away from Live.

Live answers "Non-unique list ids" with a modal that wedges its main thread
and "Not all list members have Ids" with nothing at all, so the service
refuses such a file before sending. The rules match Live 12.4's own Core
Library: same-tag siblings may not share an Id, and every device inside a
<Device> holder has one.
"""

import gzip

import pytest

from ableton_mcp import presenters, render
from ableton_mcp.preset_integrity import check_preset_file

from test_load_alert_watchdog import FakeClient, _service


def _write(tmp_path, name, body):
    p = tmp_path / name
    with gzip.open(p, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n' + body.encode())
    return str(p)


GOOD = """<Ableton><MultiSampler><Player><MultiSampleMap><SampleParts>
<MultiSamplePart Id="0"/><MultiSamplePart Id="1"/></SampleParts></MultiSampleMap>
</Player><Volume><LomId Value="0"/><AutomationTarget Id="7"/><ModulationTarget Id="7"/>
</Volume></MultiSampler></Ableton>"""

DUPLICATE = GOOD.replace('<MultiSamplePart Id="1"/>', '<MultiSamplePart Id="0"/>')

MISSING = """<Ableton><GroupDevicePreset><BranchPresets><AudioEffectBranchPreset Id="0">
<DevicePresets><AbletonDevicePreset Id="0"><Device><Pedal><On/></Pedal></Device>
</AbletonDevicePreset></DevicePresets></AudioEffectBranchPreset></BranchPresets>
</GroupDevicePreset></Ableton>"""


def test_a_clean_document_and_cross_tag_id_reuse_pass(tmp_path):
    # AutomationTarget and ModulationTarget sharing Id 7 is how Live writes it
    assert check_preset_file(_write(tmp_path, "ok.adv", GOOD)) is None


def test_duplicate_ids_in_one_list_are_named(tmp_path):
    out = check_preset_file(_write(tmp_path, "dup.adv", DUPLICATE))
    assert out["problems"][0] == {"rule": "duplicate_id", "list": "SampleParts",
                                  "member": "MultiSamplePart", "id": "0"}
    assert "Non-unique list ids" in out["summary"]


def test_a_device_without_an_id_is_named(tmp_path):
    out = check_preset_file(_write(tmp_path, "rack.adg", MISSING))
    assert out["problems"][0]["rule"] == "missing_id"
    assert out["problems"][0]["member"] == "Pedal"


def test_other_files_are_not_judged(tmp_path):
    assert check_preset_file(str(tmp_path / "absent.adv")) is None
    plug = tmp_path / "x.vstpreset"
    plug.write_bytes(b"\x00not xml")
    assert check_preset_file(str(plug)) is None
    bad = tmp_path / "trunc.adv"
    bad.write_bytes(b"not gzip")
    assert check_preset_file(str(bad))["problems"] == [{"rule": "unreadable"}]


def test_the_service_refuses_without_sending(tmp_path):
    client = FakeClient()
    service = _service(client, ui=None, tmp_path=tmp_path)
    path = _write(tmp_path, "dup.adv", DUPLICATE)
    with pytest.raises(render.RenderRefused) as caught:
        service.load_device_from_path(0, path)
    assert caught.value.reason == "preset_corrupt"
    assert client.sent == []
    text = presenters.render_refusal(caught.value)
    assert "Non-unique list ids" in text and "Nothing was sent" in text
