import pytest
from types import SimpleNamespace
from fake_ableton import make_harness
from test_device_read_fidelity import Proxy, Partial

@pytest.mark.parametrize('kind',['regular','return','master'])
def test_track_device_partial_iteration_retains_prefix(kind):
    h=make_harness(); dev=h.song.tracks[0].devices[0]
    track={'regular':h.song.tracks[0],'return':h.song.return_tracks[0],'master':h.song.master_track}[kind]
    track.devices=Partial(dev)
    if kind=='regular': row=h.mcp._snapshot_track(0,track,False,False,False,False,False)
    elif kind=='return': row=h.mcp._serialize_return_tracks(False)[0]
    else: row=h.mcp._serialize_master_track(False)
    assert row['devices_read_status']=='partial'
    assert len(row['devices'])==1


def test_failed_device_does_not_hide_later_device():
    h=make_harness();track=h.song.tracks[0];dev=track.devices[0]
    track.devices=[Proxy(dev,name=RuntimeError('identity unavailable')),dev]
    row=h.mcp._snapshot_track(0,track,False,False,False,False,False)
    assert row['devices_read_status']=='partial'
    assert [d['index'] for d in row['devices']]==[1]
    assert row['devices_read_errors'][0]['index']==0


def test_direct_rack_partial_chains_and_nested_devices():
    h=make_harness();track=h.song.tracks[0];dev=track.devices[0]
    chain=SimpleNamespace(name='one',devices=Partial(dev),mute=False,solo=False)
    rack=Proxy(dev,chains=Partial(chain),can_have_drum_pads=False)
    h.mcp._resolve_rack=lambda *a:(track,rack)
    h.mcp._rack_header=lambda *a:{}
    result=h.mcp._get_rack_chains()
    assert result['chains_read_status']=='partial'
    assert result['chain_count'] is None
    assert result['chains'][0]['devices_read_status']=='partial'


def test_valid_empty_direct_rack_is_empty_not_unavailable():
    h=make_harness();dev=h.song.tracks[0].devices[0]
    h.mcp._resolve_rack=lambda *a:(h.song.tracks[0],Proxy(dev,chains=[]))
    h.mcp._rack_header=lambda *a:{}
    result=h.mcp._get_rack_chains()
    assert result['chain_count']==0 and result['chains']==[]
    assert 'chains_read_status' not in result

@pytest.mark.parametrize('attr',['tracks','return_tracks'])
def test_top_level_partial_iteration_preserves_prefix(attr):
    h=make_harness(); first=getattr(h.song,attr)[0];setattr(h.song,attr,Partial(first))
    result=h.mcp._get_session_snapshot(include_notes=False,include_params=False)
    assert result[attr+'_read_status']=='partial'
    assert len(result[attr])==1


def test_actual_mcp_preserves_collection_partial_status():
    import asyncio
    from mcp.shared.memory import create_connected_server_and_client_session
    from ableton_mcp.app import build_app
    from test_full_stack import _make_deps
    h=make_harness();track=h.song.tracks[0];track.devices=Partial(track.devices[0])
    raw=h.mcp._snapshot_track(0,track,False,False,False,False,False)
    deps,_=_make_deps();deps.service.get_track_info=lambda *a,**kw:raw
    async def run():
        async with create_connected_server_and_client_session(build_app(deps=deps,output_mode='structured')) as session:
            return await session.call_tool('get_track_info',{'track_index':0})
    result=asyncio.run(run());assert not result.isError
    assert result.structuredContent==raw


def test_master_unavailable_is_explicit_not_plain_null():
    h=make_harness();h.song.master_track=Proxy(h.song.master_track,mixer_device=RuntimeError('master unavailable'))
    result=h.mcp._get_session_snapshot(include_notes=False,include_params=False)
    assert result['master_track'] is None
    assert result['master_track_read_status']=='unavailable'
