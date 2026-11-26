import pytest
from fake_ableton import make_harness

class Proxy:
    def __init__(self, target, **overrides): self.target, self.overrides = target, overrides
    def __getattr__(self, name):
        value = self.overrides.get(name, self)
        if isinstance(value, Exception): raise value
        return getattr(self.target, name) if value is self else value

@pytest.mark.parametrize('track_type', ['regular','return','master'])
def test_parameter_enumeration_unavailable_is_not_empty(track_type):
    h = make_harness()
    track = {'regular': h.song.tracks[0], 'return': h.song.return_tracks[0], 'master': h.song.master_track}[track_type]
    device = h.song.tracks[0].devices[0]
    track.devices = [Proxy(device, parameters=RuntimeError('unreadable'))]
    if track_type == 'regular':
        result = h.mcp._snapshot_track(0, track, False, True, False, False, False)['devices'][0]
    elif track_type == 'return':
        result = h.mcp._serialize_return_tracks()[0]['devices'][0]
    else:
        result = h.mcp._serialize_master_track()['devices'][0]
    assert result['parameters'] == []
    assert result['parameters_read_status'] == 'unavailable'
    assert result['automation_enabled'] is None


def test_parameter_row_failure_keeps_successful_rows_and_index():
    h = make_harness(); device = h.song.tracks[0].devices[0]
    params = list(device.parameters)
    params[0] = Proxy(params[0], value=RuntimeError('broken value'))
    result = h.mcp._serialize_device(Proxy(device, parameters=params), 0)
    assert result['parameters_read_status'] == 'partial'
    assert result['parameter_read_errors'][0]['index'] == 0
    assert [p['index'] for p in result['parameters']] == list(range(1,len(params)))


def test_empty_parameters_remain_successful_empty():
    h = make_harness(); device = h.song.tracks[0].devices[0]
    result = h.mcp._serialize_device(Proxy(device, parameters=[]), 0)
    assert result['parameters'] == []
    assert 'parameters_read_status' not in result


def test_chain_count_unavailable_not_zero():
    h = make_harness(); device = h.song.tracks[0].devices[0]
    result = h.mcp._serialize_device(Proxy(device, can_have_chains=True, chains=RuntimeError('broken chains')), 0)
    assert result['chain_count'] is None


def test_requested_chains_unavailable_not_successful_empty():
    h = make_harness(); device = h.song.tracks[0].devices[0]
    result = h.mcp._serialize_device(Proxy(device, can_have_chains=True, chains=RuntimeError('broken chains')), 0, include_rack_chains=True)
    assert result['chains'] == []
    assert result['chains_read_status'] == 'unavailable'


def test_nested_device_read_failure_and_parameter_failure():
    from types import SimpleNamespace
    h = make_harness(); device = h.song.tracks[0].devices[0]
    chain = SimpleNamespace(name='nested', mute=False, solo=False,
        devices=[Proxy(device, parameters=RuntimeError('nested parameters'))])
    rack = Proxy(device, can_have_chains=True, chains=[chain], return_chains=[])
    result = h.mcp._serialize_device(rack, 0, include_rack_chains=True)
    assert result['chains'][0]['devices'][0]['parameters_read_status'] == 'unavailable'
    broken = Proxy(chain, devices=RuntimeError('nested devices'))
    result = h.mcp._serialize_device(Proxy(rack, chains=[broken]), 0, include_rack_chains=True)
    assert result['chains'][0]['devices_read_status'] == 'unavailable'


def test_actual_mcp_retains_native_failure_metadata():
    import asyncio
    from mcp.shared.memory import create_connected_server_and_client_session
    from ableton_mcp.app import build_app
    from test_full_stack import _make_deps
    h = make_harness(); device = h.song.tracks[0].devices[0]
    raw = h.mcp._serialize_device(Proxy(device, parameters=RuntimeError('unreadable')),0)
    deps, _ = _make_deps()
    deps.service.get_device_parameters = lambda *a, **kw: raw
    async def run():
        async with create_connected_server_and_client_session(build_app(deps=deps, output_mode='structured')) as session:
            return await session.call_tool('get_device_parameters', {'track_index': 0, 'device_index': 0})
    result = asyncio.run(run())
    assert not result.isError
    assert result.structuredContent == raw

class Partial:
    def __init__(self, first): self.first = first
    def __iter__(self):
        yield self.first
        raise RuntimeError('enumeration interrupted')


def test_parameter_iteration_partial_keeps_observed_prefix():
    h = make_harness(); device = h.song.tracks[0].devices[0]
    result = h.mcp._serialize_device(Proxy(device, parameters=Partial(device.parameters[0])),0)
    assert result['parameters_read_status'] == 'partial'
    assert len(result['parameters']) == 1
    assert result['parameter_read_errors'][-1]['index'] is None


def test_chain_iteration_partial_keeps_observed_prefix():
    from types import SimpleNamespace
    h = make_harness(); device = h.song.tracks[0].devices[0]
    chain = SimpleNamespace(name='one', mute=False, solo=False, devices=[])
    result = h.mcp._serialize_device(Proxy(device, can_have_chains=True, chains=Partial(chain), return_chains=[]),0,include_rack_chains=True)
    assert result['chains_read_status'] == 'partial'
    assert len(result['chains']) == 1
