"""Raw track/warp outcomes survive prose and prose-plus-JSON views."""
import asyncio
import copy
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from ableton_mcp.app import build_app
from ableton_mcp import tools, presenters
from ableton_mcp.live_lock import LockError
from test_full_stack import _make_deps, _ctx

CASES = [
 ('create_midi_track', {'index': 2}),
 ('create_audio_track', {'index': 2}),
 ('duplicate_track', {'track_index': 0, 'expect_track_name': 'expected'}),
 ('create_return_track', {}),
 ('set_clip_gain', {'track_index': 0, 'clip_index': 0, 'gain': .4}),
 ('set_clip_warp', {'track_index': 0, 'clip_index': 0, 'warping': True}),
 ('edit_clip_warp_markers', {'track_index': 0, 'clip_index': 0, 'edits': []}),
 ('set_count_in', {'bars': 1, 'metronome': True}),
]

async def call(deps, mode, name, args):
    async with create_connected_server_and_client_session(build_app(deps=deps, output_mode=mode)) as session:
        return await session.call_tool(name, args)

@pytest.mark.parametrize('mode', ['structured', 'both', 'legacy'])
@pytest.mark.parametrize('name,args', CASES)
@pytest.mark.parametrize('state', [True, False, None])
def test_track_clip_raw_not_requested_or_inferred(mode, name, args, state):
    deps, _ = _make_deps()
    raw = {'name': 'actual', 'index': 7, 'return_index': 3,
           'duplicated': state, 'source_track_name': 'actual source',
           'source_track_index': 6, 'track_count_after': 8,
           'clip_name': 'actual clip', 'track_name': 'actual track',
           'gain': None, 'gain_display': None, 'warping': state, 'length': None,
           'verified': state, 'applied': 0, 'requested': 1, 'failed': 1,
           'not_attempted': 0, 'mismatches': [], 'warp_markers_before': [],
           'warp_markers_after': None, 'count_in_writable': state,
           'count_in': None, 'metronome': False, 'future': {'opaque': '音楽'}}
    original = copy.deepcopy(raw)
    calls = []
    def service(*a, **kw):
        calls.append((a, kw))
        return raw
    setattr(deps.service, name, service)
    # Exact preexisting wording is additionally protected by golden tests.
    legacy = str(getattr(tools, name)(_ctx(deps), **args))
    direct_calls = list(calls)
    calls.clear()
    result = asyncio.run(call(deps, mode, name, args))
    assert calls == direct_calls and len(calls) == 1
    assert raw == original and not result.isError
    assert result.content[0].text == legacy
    if mode != 'legacy': assert result.structuredContent == original

@pytest.mark.parametrize('mode', ['structured', 'both', 'legacy'])
@pytest.mark.parametrize('name,args', CASES)
@pytest.mark.parametrize('refusal', [False, True])
def test_track_clip_errors_keep_existing_contract(mode, name, args, refusal):
    deps, _ = _make_deps()
    error = LockError('not_held') if refusal else RuntimeError('synthetic operation failed')
    calls = []
    def service(*a, **kw):
        calls.append(1)
        raise error
    setattr(deps.service, name, service)
    result = asyncio.run(call(deps, mode, name, args))
    assert len(calls) == 1
    if mode == 'legacy':
        assert result.content[0].text == (presenters.lock_refusal(error) if refusal else presenters.error_text(name, error))
    else:
        assert result.isError
        assert result.structuredContent['code'] == ('lock_refused' if refusal else 'operation_failed')
        assert result.structuredContent['details']['tool'] == name
