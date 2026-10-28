"""Rack chains through the real Remote Script on the fake LOM: read, add a
chain, insert a device into a chain, set a chain's mixer, fix a Drum Rack
pad's receiving note — and both sides of every Live-12-only probe.

RackDevice.insert_chain and Chain.insert_device are documented in Live
12.4.7's binary but never called by this fork before; their argument
shapes here are the fake's guess and UNVERIFIED until the live pass.
"""

from fake_ableton import FakeDevice, FakeDrumChain, FakeChain, FakeRackDevice, make_harness


def _harness(rack):
    harness = make_harness()
    track = harness.song.tracks[0]
    track.devices = list(track.devices) + [rack]
    return harness, len(track.devices) - 1


def _run(harness, command, **params):
    params.setdefault("track_index", 0)
    return harness.process({"type": command, "params": params})


def _ok(resp):
    assert resp["status"] == "success", resp
    return resp["result"]


def test_build_a_parallel_blend():
    rack = FakeRackDevice(chains=[FakeChain("Amp A", devices=[FakeDevice("Amp")])])
    harness, index = _harness(rack)
    added = _ok(_run(harness, "insert_rack_chain", device_index=index, name="Amp B"))
    assert added["chain_index"] == 1 and added["chain_count"] == 2
    assert rack.chains[1].name == "Amp B"
    inserted = _ok(_run(harness, "insert_chain_device", device_index=index,
                        chain_index=1, device_name="Utility"))
    assert [d["name"] for d in inserted["chain"]["devices"]] == ["Utility"]
    mixed = _ok(_run(harness, "set_chain_mixer", device_index=index, chain_index=1,
                     volume=0.5, mute=True, name="Amp B (inv)"))
    assert mixed["changes"]["volume"]["value"] == 0.5
    assert mixed["changes"]["mute"] is True and rack.chains[1].mute is True
    assert rack.chains[1].name == "Amp B (inv)"
    listed = _ok(_run(harness, "get_rack_chains", device_index=index))
    assert listed["chain_count"] == 2 and listed["can_insert_chain"] is True
    assert listed["chains"][0]["devices"][0]["name"] == "Amp"
    assert listed["chains"][1]["volume_display"] == "0.50 dB"


def test_insert_chain_at_an_index_and_device_at_a_position():
    rack = FakeRackDevice(chains=[FakeChain("A"), FakeChain("B")])
    harness, index = _harness(rack)
    added = _ok(_run(harness, "insert_rack_chain", device_index=index, index=0, name="First"))
    assert added["chain_index"] == 0 and [c.name for c in rack.chains] == ["First", "A", "B"]
    rack.chains[1].devices = [FakeDevice("EQ Eight")]
    _ok(_run(harness, "insert_chain_device", device_index=index, chain_index=1,
             device_name="Utility", index=0))
    assert [d.name for d in rack.chains[1].devices] == ["Utility", "EQ Eight"]


def test_drum_chain_in_note():
    rack = FakeRackDevice("Drum Rack", drum=True,
                          chains=[FakeDrumChain("Kick", in_note=36),
                                  FakeDrumChain("Snare", in_note=12)])
    harness, index = _harness(rack)
    listed = _ok(_run(harness, "get_rack_chains", device_index=index))
    assert listed["is_drum_rack"] is True and listed["chains"][1]["in_note"] == 12
    fixed = _ok(_run(harness, "set_chain_mixer", device_index=index, chain_index=1,
                     in_note=38))
    assert fixed["changes"]["in_note"] == 38 and rack.chains[1].in_note == 38
    bad = _run(harness, "set_chain_mixer", device_index=index, chain_index=1, in_note=200)
    assert bad["status"] == "error" and "0-127" in bad["message"]


def test_refusals_change_nothing():
    rack = FakeRackDevice(chains=[FakeChain("A")], insert_chain_api=False,
                          chain_insert_device_api=False)
    rack.chains = [FakeChain("A", insert_device_api=False)]
    harness, index = _harness(rack)
    no_api = _run(harness, "insert_rack_chain", device_index=index)
    assert no_api["status"] == "error" and "insert_chain" in no_api["message"]
    assert len(rack.chains) == 1
    no_dev_api = _run(harness, "insert_chain_device", device_index=index,
                      chain_index=0, device_name="Utility")
    assert "Chain.insert_device" in no_dev_api["message"]
    not_rack = _run(harness, "get_rack_chains", device_index=0)
    assert not_rack["status"] == "error" and "not a rack" in not_rack["message"]
    out_of_range = _run(harness, "set_chain_mixer", device_index=index, chain_index=5, mute=True)
    assert "out of range" in out_of_range["message"]
    nothing = _run(harness, "set_chain_mixer", device_index=index, chain_index=0)
    assert "Nothing to set" in nothing["message"]
    not_drum = _run(harness, "set_chain_mixer", device_index=index, chain_index=0, in_note=36)
    assert "not a Drum Rack chain" in not_drum["message"]


def test_a_device_insert_that_lands_nothing_is_reported():
    chain = FakeChain("A")
    chain.insert_device = lambda name, index=-1: None  # returns, adds nothing
    rack = FakeRackDevice(chains=[chain])
    harness, index = _harness(rack)
    resp = _run(harness, "insert_chain_device", device_index=index, chain_index=0,
                device_name="Some VST")
    assert resp["status"] == "error" and "native device" in resp["message"]


def test_expect_track_name_guards_chain_writes():
    rack = FakeRackDevice(chains=[FakeChain("A")])
    harness, index = _harness(rack)
    resp = _run(harness, "set_chain_mixer", device_index=index, chain_index=0,
                mute=True, expect_track_name="Not This Track")
    assert resp["status"] == "error"
    assert rack.chains[0].mute is False
