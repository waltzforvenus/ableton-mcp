import asyncio
import copy
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from ableton_mcp.app import build_app
from ableton_mcp import tools
from test_full_stack import _make_deps, _ctx

CASES = [
 ('set_track_volume', {'track_index': 0, 'value': .5}),
 ('set_track_pan', {'track_index': 0, 'value': .5}),
 ('set_track_send', {'track_index': 0, 'send_index': 0, 'value': .5}),
 ('set_track_mute', {'track_index': 0, 'mute': True}),
 ('set_track_solo', {'track_index': 0, 'solo': True}),
 ('set_track_arm', {'track_index': 0, 'armed': True, 'authorized': True}),
 ('set_track_monitoring', {'track_index': 0, 'state': 'auto'}),
 ('set_track_routing', {'track_index': 0, 'target': 'Main'}),
]

@pytest.mark.parametrize('mode', ['structured', 'both', 'legacy'])
@pytest.mark.parametrize('name,args', CASES)
@pytest.mark.parametrize('failed', [False, True])
def test_mixer_raw_outcome_and_legacy_parity(mode, name, args, failed):
    deps, _ = _make_deps()
    raw = {'track_name': 'Actual', 'value': None, 'mute': False, 'solo': False,
           'arm': None, 'monitoring': None, 'exclusive_solo': None,
           'future': {'opaque': ['音楽', False]}}
    original = copy.deepcopy(raw)
    calls = []
    def service(*a, **kw):
        calls.append((a, kw))
        if failed: raise RuntimeError('synthetic read failed')
        return raw
    setattr(deps.service, name, service)
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
