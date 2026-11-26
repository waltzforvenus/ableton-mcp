"""Explicit raw metadata, with preserved arrangement refusal JSON branches."""
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
 ('set_plugin_preset', {'track_index': 0, 'device_index': 0, 'preset_index': 1}),
 ('insert_rack_chain', {'track_index': 0, 'device_index': 0, 'name': 'requested'}),
 ('insert_chain_device', {'track_index': 0, 'device_index': 0, 'chain_index': 0, 'device_name': 'Utility'}),
 ('set_chain_mixer', {'track_index': 0, 'device_index': 0, 'chain_index': 0, 'volume': .5}),
 ('create_audio_clip', {'track_index': 0, 'clip_index': 0, 'path': '/synthetic/audio.wav'}),
 ('create_arrangement_audio_clip', {'track_index': 0, 'path': '/synthetic/audio.wav', 'start_time': 0}),
]

async def call(deps, mode, name, args):
    async with create_connected_server_and_client_session(build_app(deps=deps, output_mode=mode)) as session:
        return await session.call_tool(name, args)

@pytest.mark.parametrize('mode', ['structured', 'both', 'legacy'])
@pytest.mark.parametrize('name,args', CASES)
@pytest.mark.parametrize('state', [True, False, None])
def test_rack_audio_raw_unknowns_and_branch_parity(mode, name, args, state):
    deps, _ = _make_deps()
    raw = {'verified': state, 'name': 'actual', 'device_name': 'actual device',
           'track_name': 'actual track', 'chain_index': 7,
           'changes': {'volume': {'value': None}, 'mute': False},
           'chain': {}, 'warping': state, 'warp_error': None,
           'length_matches_expected': state, 'expected_beats': 4,
           'supported': state, 'future': {'opaque': '音楽'}, 'unknown': None}
    original = copy.deepcopy(raw)
    calls = []
    def service(*a, **kw):
        calls.append((a, kw)); return raw
    setattr(deps.service, name, service)
    legacy = str(getattr(tools, name)(_ctx(deps), **args))
    direct_calls = list(calls); calls.clear()
    result = asyncio.run(call(deps, mode, name, args))
    assert calls == direct_calls and len(calls) == 1
    assert raw == original and not result.isError
    if mode != 'legacy': assert result.structuredContent == original
    json_branch = name == 'create_arrangement_audio_clip' and state is False
    if mode == 'legacy' or not json_branch: assert result.content[0].text == legacy
    elif mode == 'structured': assert result.content == []
    else: assert json.loads(result.content[0].text) == original

@pytest.mark.parametrize('mode', ['structured', 'both', 'legacy'])
@pytest.mark.parametrize('name,args', CASES)
@pytest.mark.parametrize('refusal', [False, True])
def test_rack_audio_exception_contract(mode, name, args, refusal):
    deps, _ = _make_deps()
    error = LockError('not_held') if refusal else RuntimeError('synthetic failure')
    calls = []
    def service(*a, **kw):
        calls.append(1); raise error
    setattr(deps.service, name, service)
    result = asyncio.run(call(deps, mode, name, args))
    assert len(calls) == 1
    if mode == 'legacy':
        assert result.content[0].text == (presenters.lock_refusal(error) if refusal else presenters.error_text(name, error))
    else:
        assert result.isError
        assert result.structuredContent['code'] == ('lock_refused' if refusal else 'operation_failed')
        assert result.structuredContent['details']['tool'] == name

@pytest.mark.parametrize('mode', ['structured', 'both', 'legacy'])
def test_arrangement_uncertain_write_json_preserved(mode):
    deps, _ = _make_deps()
    raw = {'reconciliation_required': True, 'supported': True, 'changed_state': None}
    calls = []
    def service(*a, **kw):
        calls.append(1); return raw
    deps.service.create_arrangement_audio_clip = service
    name, args = CASES[-1]
    result = asyncio.run(call(deps, mode, name, args))
    assert len(calls) == 1 and not result.isError
    if mode != 'legacy': assert result.structuredContent == raw
    if mode == 'structured': assert result.content == []
    else: assert json.loads(result.content[0].text) == raw

@pytest.mark.parametrize('mode', ['structured', 'both', 'legacy'])
@pytest.mark.parametrize('name,args', [
 ('create_audio_clip', {'track_index': 0, 'clip_index': 0}),
 ('create_audio_clip', {'track_index': 0, 'clip_index': 0, 'path': '/a', 'file_path': '/b'}),
 ('set_chain_mixer', {'track_index': 0, 'device_index': 0, 'chain_index': 0, 'allow_automation_override': 'yes'}),
])
def test_existing_ingress_guards_never_call_service(mode, name, args):
    deps, _ = _make_deps(); calls = []
    def service(*a, **kw):
        calls.append(1); raise AssertionError('must not invoke native boundary')
    setattr(deps.service, name, service)
    result = asyncio.run(call(deps, mode, name, args))
    assert calls == []
    if mode != 'legacy':
        assert result.isError
        assert result.structuredContent['code'] == 'invalid_argument'
