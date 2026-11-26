"""SDK dispatch boundary exercised over real in-memory MCP, no Live."""
import json
import pytest
from test_structured_output import call


@pytest.mark.parametrize('mode', ['structured', 'both'])
@pytest.mark.parametrize('name,args,code', [
    ('get_track_info', {'track_index': {'bad': 1}}, 'invalid_argument'),
    ('get_track_info', {}, 'invalid_argument'),
    ('does_not_exist', {}, 'unknown_tool'),
])
def test_dispatch_errors_are_typed(mode, name, args, code):
    result = call(mode, name, args)
    assert result.isError
    wire = json.loads(result.model_dump_json(by_alias=True, exclude_none=True))
    payload = wire['structuredContent']
    assert set(payload) == {'code', 'message', 'details'}
    assert payload['code'] == code
    assert payload['details']['tool'] == name
    assert payload['message']
    if code == 'invalid_argument':
        assert payload['details']['validation'][0]['loc'] == ['track_index']
        assert 'input' not in payload['details']['validation'][0]
    if mode == 'both':
        assert json.loads(result.content[0].text) == payload
    else:
        assert result.content == []


@pytest.mark.parametrize('name,args', [
    ('get_track_info', {'track_index': {}}), ('get_track_info', {}),
    ('does_not_exist', {}),
])
def test_legacy_dispatch_keeps_sdk_text_errors(name, args):
    result = call('legacy', name, args)
    assert result.isError
    assert result.content[0].type == 'text'
    assert result.structuredContent is None


@pytest.mark.parametrize('mode', ['structured', 'both'])
def test_sdk_execution_failure_not_misclassified_as_validation(mode):
    import asyncio
    from mcp.shared.memory import create_connected_server_and_client_session
    from ableton_mcp.app import build_app
    from test_full_stack import _make_deps
    deps, _ = _make_deps()
    app = build_app(deps=deps, output_mode=mode)

    def dispatch_failure():
        raise RuntimeError('synthetic dispatch failure')

    app.add_tool(dispatch_failure)

    async def run():
        async with create_connected_server_and_client_session(app) as session:
            return await session.call_tool('dispatch_failure', {})

    result = asyncio.run(run())
    assert result.isError
    assert result.structuredContent['code'] == 'operation_failed'
    assert result.structuredContent['details'] == {'tool': 'dispatch_failure'}
    assert 'synthetic dispatch failure' in result.structuredContent['message']
