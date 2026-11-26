import asyncio
import base64
import json
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from ableton_mcp.app import build_app
from test_full_stack import _make_deps


@pytest.mark.parametrize('mode', ['structured', 'both', 'legacy'])
@pytest.mark.parametrize('with_image', [True, False])
def test_audio_metadata_and_native_image_survive_transport(mode, with_image):
    deps, _ = _make_deps()
    metadata = {'clip': {'clip_name': '音楽'}, 'transients_total': 2}
    source = dict(metadata, **({'image': b'png-fixture-bytes'} if with_image else {}))
    deps.service.analyze_clip_audio = lambda *a, **kw: source
    async def run():
        async with create_connected_server_and_client_session(build_app(deps=deps, output_mode=mode)) as s:
            return await s.call_tool('analyze_clip_audio', {'track_index': 0, 'clip_index': 0})
    result = asyncio.run(run())
    assert not result.isError
    assert result.content[0].type == 'text'
    assert result.content[0].text.startswith('Analysed ')
    if with_image:
        assert len(result.content) == 2
        assert result.content[1].type == 'image'
        assert result.content[1].mimeType == 'image/png'
        assert base64.b64decode(result.content[1].data) == b'png-fixture-bytes'
        assert source['image'] == b'png-fixture-bytes'
    else:
        assert len(result.content) == 1
    if mode != 'legacy':
        assert result.structuredContent == metadata
        assert 'image' not in json.loads(result.model_dump_json())['structuredContent']


def test_native_content_blocks_preserve_metadata_and_annotations(monkeypatch):
    from ableton_mcp import presenters
    from ableton_mcp.presentation import MetadataBlocks
    from mcp.types import TextContent, ImageContent
    text = TextContent(type='text', text='original', annotations={'audience': ['user']})
    image = ImageContent(type='image', data='YWJj', mimeType='image/png')
    monkeypatch.setattr(presenters, 'analyze_clip_audio',
                        lambda _: MetadataBlocks([text, image], {'known': True}))
    deps, _ = _make_deps()
    deps.service.analyze_clip_audio = lambda *a, **kw: {}
    async def run():
        async with create_connected_server_and_client_session(build_app(deps=deps)) as s:
            return await s.call_tool('analyze_clip_audio', {'track_index': 0, 'clip_index': 0})
    result = asyncio.run(run())
    assert result.content == [text, image]
    assert result.structuredContent == {'known': True}
