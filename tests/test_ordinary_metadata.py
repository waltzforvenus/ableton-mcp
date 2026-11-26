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
