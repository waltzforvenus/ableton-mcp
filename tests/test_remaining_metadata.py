"""Final ordinary prose views retain raw outcomes, not wording-derived facts."""
import asyncio
import copy
import json
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from ableton_mcp.app import build_app
from ableton_mcp import tools, presenters
from ableton_mcp.live_lock import LockError
from test_full_stack import _make_deps, _ctx

CASES = [
 ('delete_track', {'track_index': 0}),
 ('add_notes_to_clip', {'track_index': 0, 'clip_index': 0, 'notes': []}),
 ('clear_notes_from_clip', {'track_index': 0, 'clip_index': 0}),
 ('set_song_scale', {'root_note': 'C'}),
 ('duplicate_to_arrangement', {'track_index': 0, 'clip_index': 0, 'destination_time': 4.0}),
 ('create_locator', {'name': 'requested', 'time': 4.0}),
 ('jump_to_locator', {'name': 'requested'}),
 ('delete_locator', {'name': 'requested'}),
 ('trim_arrangement_clip', {'track_index': 0, 'clip_index': 0, 'end_time': 4.0}),
 ('delete_arrangement_clip', {'track_index': 0, 'clip_index': 0}),
 ('move_arrangement_clip', {'track_index': 0, 'clip_index': 0, 'destination_time': 4.0}),
 ('duplicate_arrangement_clip', {'track_index': 0, 'clip_index': 0, 'destination_time': 4.0}),
]

def service_name(name):
    return 'duplicate_session_clip_to_arrangement' if name == 'duplicate_to_arrangement' else name

async def call(deps, mode, name, args):
    async with create_connected_server_and_client_session(build_app(deps=deps, output_mode=mode)) as session:
        return await session.call_tool(name, args)

ROWS = [(name, args, {'unknown': None, 'future': {'opaque': '音楽'}}) for name,args in CASES]
for name,args in CASES:
    for state in [True, False, None]:
        raw = {'verified': state, 'success': state, 'deleted': state,
               'moved': state, 'start_marker_set': state, 'trimmed_head': state,
               'name': None, 'time': None, 'clip_note_count': None}
        if name == 'add_notes_to_clip': raw.update(expression_verified=state, requested=2, added=1 if state else None)
        if name == 'set_song_scale': raw.update(requested={'root_name': 'actual'}, mismatched=['scale_name'])
        if name == 'duplicate_to_arrangement': raw.update(destination_times=[4], placements=[{'ok': state, 'destination_time': 4, 'error': None}], placed_count=None, requested_count=1)
        if name == 'delete_arrangement_clip': raw.update(deletions=[{'ok': state, 'start_time': 4, 'error': None}], deleted_count=None, requested_count=1)
        if name == 'trim_arrangement_clip': raw.update(refusals=['edge not writable'])
        ROWS.append((name,args,raw))

@pytest.mark.parametrize('mode', ['structured', 'both', 'legacy'])
@pytest.mark.parametrize('name,args,raw', ROWS)
def test_remaining_raw_metadata_and_branch_parity(mode, name, args, raw):
    deps, _ = _make_deps(); raw = copy.deepcopy(raw); original = copy.deepcopy(raw); calls = []
    def service(*a, **kw):
        calls.append((a,kw)); return raw
    setattr(deps.service, service_name(name), service)
    legacy = str(getattr(tools,name)(_ctx(deps), **args))
    direct_calls = list(calls); calls.clear()
    result = asyncio.run(call(deps,mode,name,args))
    assert calls == direct_calls and len(calls) == 1
    assert not result.isError and raw == original
    if mode != 'legacy': assert result.structuredContent == original
    json_branch = name == 'add_notes_to_clip' and raw.get('expression_verified') is False
    if mode == 'legacy' or not json_branch: assert result.content[0].text == legacy
    elif mode == 'structured': assert result.content == []
    else: assert json.loads(result.content[0].text) == original

@pytest.mark.parametrize('mode', ['structured', 'both', 'legacy'])
@pytest.mark.parametrize('name,args', CASES)
@pytest.mark.parametrize('refusal', [False, True])
def test_remaining_error_and_refusal_contract(mode, name, args, refusal):
    deps, _ = _make_deps(); calls = []
    error = LockError('not_held') if refusal else RuntimeError('synthetic failure')
    def service(*a, **kw):
        calls.append(1); raise error
    setattr(deps.service,service_name(name),service)
    result = asyncio.run(call(deps,mode,name,args))
    assert len(calls) == 1
    if mode == 'legacy': assert result.content[0].text == (presenters.lock_refusal(error) if refusal else presenters.error_text(name,error))
    else:
        assert result.isError
        assert result.structuredContent['code'] == ('lock_refused' if refusal else 'operation_failed')
        assert result.structuredContent['details']['tool'] == name

@pytest.mark.parametrize('mode', ['structured', 'both', 'legacy'])
@pytest.mark.parametrize('name,args', [
 ('set_song_scale', {}), ('set_song_scale', {'scale_name': ' '}),
 ('duplicate_to_arrangement', {'track_index': 0, 'clip_index': 0}),
 ('duplicate_to_arrangement', {'track_index': 0, 'clip_index': 0, 'destination_time': 4, 'time': 8}),
 ('duplicate_to_arrangement', {'track_index': 0, 'clip_index': 0, 'destination_time': 4, 'destination_times': [8]}),
 ('delete_arrangement_clip', {'track_index': 0, 'clip_index': 0, 'start_time': 4}),
])
def test_remaining_invalid_forms_do_not_invoke_service(mode,name,args):
    deps, _ = _make_deps(); calls = []
    def service(*a,**kw):
        calls.append(1); raise AssertionError('must not reach service')
    setattr(deps.service,service_name(name),service)
    result = asyncio.run(call(deps,mode,name,args))
    assert calls == []
    if mode != 'legacy':
        assert result.isError and result.structuredContent['code'] == 'invalid_argument'
