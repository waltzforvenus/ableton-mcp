"""Real MCP serialization of ordinary raw service outcomes; no Live."""
import asyncio
import copy
import json
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from ableton_mcp.app import build_app
from test_full_stack import _make_deps

CASES = [
    ('create_clip', {'track_index': 0, 'clip_index': 1, 'length': 4.0},
     'Created new clip at track 0, slot 1 with length 4.0 beats'),
    ('set_track_name', {'track_index': 0, 'name': 'Asked'}, 'Renamed track to: Actual'),
    ('set_tempo', {'tempo': 123.0}, 'Set tempo to 123.0 BPM'),
    ('start_playback', {'authorized': True}, 'Started playback'),
    ('stop_playback', {}, 'Stopped playback'),
]


BATCH2 = [
    ('set_clip_name', {'track_index': 0, 'clip_index': 1, 'name': 'Asked'},
     "Renamed clip at track 0, slot 1 to 'Asked'"),
    ('set_arrangement_clip_name', {'track_index': 0, 'clip_index': 1, 'name': 'Asked'},
     "Renamed arrangement clip at track 0, index 1 to 'Asked'"),
    ('fire_clip', {'track_index': 0, 'clip_index': 1, 'authorized': True},
     'Started playing clip at track 0, slot 1'),
    ('stop_clip', {'track_index': 0, 'clip_index': 1},
     'Stopped clip at track 0, slot 1'),
    ('back_to_arrangement', {}, 'All tracks returned to Arrangement playback'),
    ('switch_to_arrangement_view', {}, 'Switched to Arrangement view'),
]
CASES += BATCH2


@pytest.mark.parametrize('mode', ['structured', 'both', 'legacy'])
@pytest.mark.parametrize('name,args,text', CASES)
def test_raw_result_survives_with_exact_legacy_wording(mode, name, args, text):
    deps, _ = _make_deps()
    raw = {'name': 'Actual', 'tempo': 121.5, 'is_playing': None,
           'future': {'opaque': ['音楽', False]}, 'verified': False}
    original = copy.deepcopy(raw)
    calls = []

    def result(*a, **kw):
        calls.append((a, kw))
        return raw

    setattr(deps.service, name, result)

    async def run():
        async with create_connected_server_and_client_session(build_app(deps=deps, output_mode=mode)) as session:
            return await session.call_tool(name, args)

    response = asyncio.run(run())
    assert not response.isError
    assert response.content[0].text == text
    if mode != 'legacy':
        wire = json.loads(response.model_dump_json(by_alias=True, exclude_none=True))
        assert wire['structuredContent'] == original
        assert 'message' not in wire['structuredContent']
    assert raw == original
    assert len(calls) == 1


@pytest.mark.parametrize('mode', ['structured', 'both', 'legacy'])
@pytest.mark.parametrize('name,args,text', BATCH2)
def test_batch2_failure_never_reports_success_metadata(mode, name, args, text):
    from ableton_mcp import presenters
    deps, _ = _make_deps()
    exc = RuntimeError('synthetic readback unavailable')
    def fail(*a, **kw):
        raise exc
    setattr(deps.service, name, fail)
    async def run():
        async with create_connected_server_and_client_session(build_app(deps=deps, output_mode=mode)) as session:
            return await session.call_tool(name, args)
    result = asyncio.run(run())
    if mode == 'legacy':
        assert result.content[0].text == presenters.error_text(name, exc)
    else:
        assert result.isError
        assert result.structuredContent == {'code': 'operation_failed',
            'message': str(exc), 'details': {'tool': name}}


@pytest.mark.parametrize('mode', ['structured', 'both', 'legacy'])
def test_fire_clip_audio_refusal_keeps_existing_contract(mode):
    from ableton_mcp.services import AudioAuthRequired
    from ableton_mcp import presenters
    deps, _ = _make_deps()
    exc = AudioAuthRequired('fire_clip')
    def refuse(*a, **kw):
        raise exc
    deps.service.fire_clip = refuse
    async def run():
        async with create_connected_server_and_client_session(build_app(deps=deps, output_mode=mode)) as session:
            return await session.call_tool('fire_clip', {'track_index': 0, 'clip_index': 1})
    result = asyncio.run(run())
    if mode == 'legacy':
        assert result.content[0].text == presenters.audio_auth_required(exc)
    else:
        assert result.isError
        assert result.structuredContent['code'] == 'audio_authorization_required'
        assert result.structuredContent['details'] == {'tool': 'fire_clip'}
