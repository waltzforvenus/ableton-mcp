import asyncio
import copy
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from ableton_mcp.app import build_app
from ableton_mcp import tools
from test_full_stack import _make_deps, _ctx

CASES = [
 ('set_device_parameter', {'track_index': 0, 'device_index': 0, 'parameter': 'Gain', 'value': .5}),
 ('set_device_parameters', {'track_index': 0, 'device_index': 0, 'parameters': {'Gain': .5}}),
 ('insert_device', {'track_index': 0, 'device_name': 'Utility'}),
 ('move_device', {'track_index': 0, 'device_index': 0, 'to_index': 1}),
 ('delete_device', {'track_index': 0, 'device_index': 0}),
 ('set_device_sidechain', {'track_index': 0, 'device_index': 0, 'source': 'Kick'}),
 ('create_arrangement_midi_clip', {'track_index': 0, 'start_time': 4.0}),
 ('set_arrangement_time', {'time': 4.0}),
]

@pytest.mark.parametrize('mode', ['structured', 'both', 'legacy'])
@pytest.mark.parametrize('name,args', CASES)
@pytest.mark.parametrize('failed', [False, True])
def test_device_arrangement_raw_outcome_and_legacy_parity(mode, name, args, failed):
    deps, _ = _make_deps()
    raw = {'track_name': 'Actual', 'value': None, 'mute': False, 'solo': False,
           'arm': None, 'monitoring': None, 'exclusive_solo': None,
           'deleted': False, 'settled': False, 'current_song_time': None,
           'future': {'opaque': ['音楽', False]}}
    original = copy.deepcopy(raw)
    calls = []
    def service(*a, **kw):
        calls.append((a, kw))
        if failed: raise RuntimeError('synthetic read failed')
        return raw
    setattr(deps.service, 'set_current_song_time' if name == 'set_arrangement_time' else name, service)
    legacy = str(getattr(tools, name)(_ctx(deps), **args))
    calls.clear()
    async def run():
        async with create_connected_server_and_client_session(build_app(deps=deps, output_mode=mode)) as session:
            return await session.call_tool(name, args)
    result = asyncio.run(run())
    assert len(calls) == 1
    assert raw == original
    if failed and mode != 'legacy':
        assert result.isError
        assert result.structuredContent == {'code': 'operation_failed',
            'message': 'synthetic read failed', 'details': {'tool': name}}
    else:
        assert result.content[0].text == legacy
        if mode != 'legacy':
            assert not result.isError
            assert result.structuredContent == original
