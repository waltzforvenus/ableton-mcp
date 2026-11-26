"""Real in-memory MCP serialization: no Live, listeners or files."""
import asyncio
import copy
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from ableton_mcp.app import build_app
from test_full_stack import _make_deps


def fixture():
    return {'tracks': [{'index': i, 'name': '音楽🎹' * 100} for i in range(3)],
            'return_tracks': [], 'master_track': {'name': 'Master'}}


def call(payload, args, mode='structured'):
    deps, _ = _make_deps()
    deps.service.get_session_snapshot = lambda *a, **kw: copy.deepcopy(payload)
    async def run():
        async with create_connected_server_and_client_session(build_app(deps=deps, output_mode=mode)) as session:
            return await session.call_tool('get_session_snapshot', args)
    return asyncio.run(run())


def size(result):
    return len(result.model_dump_json(by_alias=True, exclude_none=True).encode('utf-8'))


@pytest.mark.parametrize('mode', ['structured', 'both'])
def test_budget_measures_final_utf8_wrapper_and_pages_without_loss(mode):
    payload = fixture()
    args = {'max_bytes': 4000, 'include_notes': False, 'include_params': False}
    seen = []
    for _ in range(4):
        result = call(payload, args, mode)
        assert not result.isError
        assert size(result) <= 4000
        body = result.structuredContent
        seen.extend(t['index'] for t in body['tracks'])
        assert body['requested_scope']['include_notes'] is False
        cursor = body['next_cursor']
        if cursor is None:
            break
        assert body['omitted_tracks']
        args['cursor'] = cursor
    assert seen == [0, 1, 2]


def test_stale_cursor_refuses_changed_snapshot():
    payload = fixture()
    first = call(payload, {'max_bytes': 2200})
    cursor = first.structuredContent['next_cursor']
    assert cursor
    payload['tracks'][0]['name'] = 'changed'
    result = call(payload, {'max_bytes': 2200, 'cursor': cursor})
    assert result.isError
    assert result.structuredContent['code'] == 'invalid_argument'
    assert 'stale' in result.structuredContent['message']


def test_tiny_budget_refuses_instead_of_oversized_success():
    result = call(fixture(), {'max_bytes': 1})
    assert result.isError
    assert 'budget' in result.structuredContent['message']


def test_single_track_cannot_fit_refuses_not_truncated_success():
    result = call(fixture(), {'max_bytes': 800})
    assert result.isError
    assert 'budget' in result.structuredContent['message']


def test_exact_boundary_and_unicode_bytes_not_characters():
    payload = {'tracks': [], 'session': {'name': '音楽🎹'}}
    first = call(payload, {'max_bytes': 10000})
    exact = size(first)
    assert not call(payload, {'max_bytes': exact}).isError
    assert call(payload, {'max_bytes': exact - 1}).isError
    assert exact > len(first.model_dump_json(by_alias=True, exclude_none=True))


@pytest.mark.parametrize('args', [{'max_bytes': 0}, {'max_bytes': -1},
                                 {'max_bytes': 2200, 'cursor': 'nonsense'},
                                 {'cursor': 'nonsense'}])
def test_invalid_budget_or_cursor_refused(args):
    assert call(fixture(), args).isError


def test_scope_change_invalidates_cursor():
    first = call(fixture(), {'max_bytes': 2200})
    result = call(fixture(), {'max_bytes': 2200, 'include_params': False,
                            'cursor': first.structuredContent['next_cursor']})
    assert result.isError and 'stale' in result.structuredContent['message']


def test_already_truncated_input_cannot_be_promoted_to_complete():
    payload = fixture()
    payload['truncated'] = True
    assert call(payload, {'max_bytes': 20000}).isError


def test_legacy_explicitly_refuses_transport_budget():
    deps, _ = _make_deps()
    from ableton_mcp import tools
    from test_full_stack import _ctx
    assert 'require structured' in tools.get_session_snapshot(_ctx(deps), max_bytes=1000)


def test_real_fake_service_flags_apply_to_returns_and_master():
    deps, _ = _make_deps()
    async def run():
        async with create_connected_server_and_client_session(build_app(deps=deps)) as session:
            return await session.call_tool('get_session_snapshot',
                {'include_notes': False, 'include_params': False, 'max_bytes': 40000})
    result = asyncio.run(run())
    assert not result.isError and size(result) <= 40000
    body = result.structuredContent
    assert body['requested_scope']['include_params'] is False
    for track in body['tracks'] + body['return_tracks'] + [body['master_track']]:
        for device in track.get('devices', []):
            assert not device.get('parameters')
        for clip in (track.get('arrangement_clips') or []):
            assert not clip.get('notes')
    assert 'notes' in body['omitted_fields'] and 'parameters' in body['omitted_fields']
