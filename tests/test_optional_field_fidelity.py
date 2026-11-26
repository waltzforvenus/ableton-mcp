from types import SimpleNamespace
import pytest
from fake_ableton import make_harness
from test_device_read_fidelity import Proxy

@pytest.mark.parametrize('value', [None, 'false', 0, [], RuntimeError('read failed')])
def test_parameter_optional_bool_unknown_keeps_row(value):
    h=make_harness(); dev=h.song.tracks[0].devices[0]
    p=Proxy(dev.parameters[0], is_enabled=value, is_quantized=value)
    result=h.mcp._serialize_device(Proxy(dev, parameters=[p]),0)
    assert len(result['parameters']) == 1
    assert result['parameters'][0]['is_enabled'] is None
    assert result['parameters'][0]['is_quantized'] is None

@pytest.mark.parametrize('value', [None, 'unknown', False, 0.5, RuntimeError('unreadable')])
def test_parameter_optional_display_and_automation_do_not_drop_row(value):
    h=make_harness();dev=h.song.tracks[0].devices[0]
    p=Proxy(dev.parameters[0], value_string=value, automation_state=value)
    result=h.mcp._serialize_device(Proxy(dev,parameters=[p]),0)
    assert len(result['parameters']) == 1
    assert result['parameters'][0]['automation_state'] is None
    if not isinstance(value,str): assert result['parameters'][0]['value_string'] is None

@pytest.mark.parametrize('value', [None, 'false', 0, [], RuntimeError('read failed')])
def test_chain_unknown_flags_and_mixer_are_null(value):
    h=make_harness();dev=h.song.tracks[0].devices[0]
    base=SimpleNamespace(devices=[],name='chain')
    chain=Proxy(base,mute=value,solo=value,mixer_device=RuntimeError('no mixer'),name=None)
    rack=Proxy(dev,can_have_chains=True,chains=[chain],return_chains=[])
    row=h.mcp._serialize_device(rack,0,include_rack_chains=True)['chains'][0]
    assert row['mute'] is None and row['solo'] is None
    assert row['volume'] is None and row['panning'] is None
    assert row['chain_name'] is None

@pytest.mark.parametrize('value',[True,False])
def test_valid_false_and_zero_remain_values(value):
    h=make_harness();dev=h.song.tracks[0].devices[0]
    p=Proxy(dev.parameters[0],is_enabled=value,is_quantized=value,automation_state=0,value_string='0')
    row=h.mcp._serialize_device(Proxy(dev,parameters=[p]),0)['parameters'][0]
    assert row['is_enabled'] is value and row['is_quantized'] is value
    assert row['automation_state'] == 0 and row['value_string']=='0'

@pytest.mark.parametrize('value',[None,'0',False,0.5,RuntimeError('bad enum')])
def test_invalid_automation_state_not_false_success(value):
    h=make_harness();dev=h.song.tracks[0].devices[0]
    p=Proxy(dev.parameters[0],automation_state=value)
    assert h.mcp._serialize_device(Proxy(dev,parameters=[p]),0)['automation_enabled'] is None

@pytest.mark.parametrize('value',[0.0,0.25,None,'0',False,float('nan'),float('inf')])
def test_chain_numeric_fields_are_finite_native_numbers(value):
    h=make_harness();dev=h.song.tracks[0].devices[0]
    chain=SimpleNamespace(name='',mute=False,solo=True,devices=[],mixer_device=SimpleNamespace(volume=SimpleNamespace(value=value),panning=SimpleNamespace(value=.5)))
    row=h.mcp._serialize_device(Proxy(dev,can_have_chains=True,chains=[chain],return_chains=[]),0,include_rack_chains=True)['chains'][0]
    assert row['volume'] == (value if type(value) is float and value in (0.0,.25) else None)
    assert row['panning']==.5 and row['mute'] is False and row['solo'] is True and row['chain_name']==''

@pytest.mark.parametrize('mode',['structured','both','legacy'])
def test_optional_fields_actual_mcp_serialization(mode):
    import asyncio,json
    from mcp.shared.memory import create_connected_server_and_client_session
    from ableton_mcp.app import build_app
    from test_full_stack import _make_deps
    h=make_harness();dev=h.song.tracks[0].devices[0]
    raw=h.mcp._serialize_device(Proxy(dev,parameters=[Proxy(dev.parameters[0],is_enabled=None,automation_state=None)]),0)
    deps,_=_make_deps();deps.service.get_device_parameters=lambda *a,**kw:raw
    async def run():
        async with create_connected_server_and_client_session(build_app(deps=deps,output_mode=mode)) as session:
            return await session.call_tool('get_device_parameters',{'track_index':0,'device_index':0})
    result=asyncio.run(run());assert not result.isError
    if mode!='legacy': assert result.structuredContent==raw
    else: assert json.loads(result.content[0].text)==raw
