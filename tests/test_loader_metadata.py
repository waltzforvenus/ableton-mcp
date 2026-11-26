import asyncio
import copy
import json
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from ableton_mcp.app import build_app
from ableton_mcp import tools
from test_full_stack import _make_deps, _ctx

CASES = [
 ('get_browser_tree', {}, {'categories': []}, False),
 ('get_browser_tree', {}, {'categories': [], 'available_categories': ['drums']}, False),
 ('get_browser_items_at_path', {'path': 'drums'}, {'items': []}, True),
 ('get_browser_items_at_path', {'path': 'drums'}, {'error': 'unknown', 'available_categories': []}, False),
 ('load_device_from_path', {'track_index': 0, 'path': '/synthetic/test.adv'}, {'loaded': False}, False),
 ('load_device_from_path', {'track_index': 0, 'path': '/synthetic/test.adv'}, {'loaded': True}, False),
 ('load_device_from_path', {'track_index': 0, 'path': '/synthetic/test.adv'}, {'reconciliation_required': True}, True),
 ('load_devices', {'track_index': 0, 'uris': ['test']}, {'loaded': [], 'failed': {'uri': 'test', 'error': 'unknown'}}, False),
 ('load_devices', {'track_index': 0, 'uris': ['test']}, {'partial_completion': True}, True),
 ('load_instrument_or_effect', {'track_index': 0, 'uri': 'test'}, {'loaded': False}, False),
 ('load_instrument_or_effect', {'track_index': 0, 'uri': 'test'}, {'loaded': True}, False),
 ('load_instrument_or_effect', {'track_index': 0, 'uri': 'test'}, {'changed_state': None}, True),
]
for stage in ['rack_failed', 'kit_lookup_failed', 'no_loadable', 'loaded', 'kit_failed']:
    CASES.append(('load_drum_kit', {'track_index': 0, 'rack_uri': 'test', 'kit_path': 'drums'},
                  {'stage': stage, 'rack_uri': 'actual', 'error': 'unknown', 'kit_path': 'actual',
                   'kit_name': 'Actual', 'track_index': 2}, stage == 'kit_failed'))

@pytest.mark.parametrize('mode', ['structured', 'both', 'legacy'])
@pytest.mark.parametrize('name,args,raw,json_branch', CASES)
def test_loader_branch_raw_metadata(mode, name, args, raw, json_branch):
    deps, _ = _make_deps()
    raw = dict(raw, unknown=None, verified=False, future={'opaque': '音楽'})
    original = copy.deepcopy(raw)
    calls = []
    def service(*a, **kw):
        calls.append(1)
        return raw
    setattr(deps.service, 'load_browser_item' if name == 'load_instrument_or_effect' else name, service)
    legacy = str(getattr(tools, name)(_ctx(deps), **args))
    calls.clear()
    async def run():
        async with create_connected_server_and_client_session(build_app(deps=deps, output_mode=mode)) as session:
            return await session.call_tool(name, args)
    result = asyncio.run(run())
    assert not result.isError
    assert raw == original and len(calls) == 1
    if mode != 'legacy':
        assert result.structuredContent == original
    if mode == 'legacy' or not json_branch:
        assert result.content[0].text == legacy
    elif mode == 'structured':
        assert result.content == []
    else:
        assert json.loads(result.content[0].text) == original


@pytest.mark.parametrize('mode', ['structured', 'both', 'legacy'])
@pytest.mark.parametrize('name,args', list({name: args for name,args,raw,j in CASES}.items()))
def test_loader_exception_keeps_existing_error_contract(mode, name, args):
    from ableton_mcp import presenters
    deps, _ = _make_deps()
    error = RuntimeError('synthetic loader failure')
    calls = []
    def fail(*a, **kw):
        calls.append(1)
        raise error
    setattr(deps.service, 'load_browser_item' if name == 'load_instrument_or_effect' else name, fail)
    async def run():
        async with create_connected_server_and_client_session(build_app(deps=deps, output_mode=mode)) as session:
            return await session.call_tool(name, args)
    result = asyncio.run(run())
    assert len(calls) == 1
    if mode == 'legacy':
        assert result.content[0].text == presenters.error_text(name, error)
    else:
        assert result.isError
        assert result.structuredContent == {'code': 'operation_failed',
            'message': str(error), 'details': {'tool': name}}
