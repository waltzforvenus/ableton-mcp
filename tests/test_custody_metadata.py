"""Custody transport must not replace raw failures/unknowns with prose facts."""
import asyncio
import copy
import json
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from ableton_mcp.app import build_app
from ableton_mcp import presenters
from ableton_mcp.live_lock import LockError
from test_full_stack import _make_deps

CASES = [
 ('acquire_live_lock', {'holder': 'requested'}, {'lock': {'holder': 'actual'}, 'adopted': False}, "Acquired the Live lock as 'actual'. It never expires: save, then release it with release_live_lock."),
 ('acquire_live_lock', {'holder': 'requested'}, {'lock': {'holder': 'actual'}, 'adopted': True}, "You already held the Live lock as 'actual'; this server now writes under it. Release it with release_live_lock after saving."),
 ('release_live_lock', {'holder': 'requested'}, {'released': False}, 'No Live lock was held; nothing to release.'),
 ('release_live_lock', {'holder': 'requested'}, {'released': None}, 'No Live lock was held; nothing to release.'),
 ('release_live_lock', {'holder': 'requested'}, {'released': True, 'saved_set': '/synthetic/set.als', 'save': {'saved': True}}, 'Released the Live lock; the set is saved at /synthetic/set.als (saved first).'),
 ('save_set', {}, {'saved': True, 'method': 'api'}, 'Saved the Live Set via api'),
 ('save_set', {}, {'saved': None, 'attempts': []}, "Could NOT save — this Live build exposes no callable save through the Python API. Tried: []. The set must be saved from Live's UI."),
 ('save_set', {}, {'saved': False, 'ui_path': 'attempted', 'error': 'unverified'}, 'Could NOT save: unverified.'),
 ('checkpoint_set', {'label': 'requested'}, {'checkpointed': True, 'checkpoint_path': '/synthetic/copy.als', 'bytes': 12, 'sha256': 'abcdef', 'set_path': '/synthetic/set.als'}, 'Checkpoint saved: /synthetic/copy.als (12 bytes, sha256 abcdef…), a verified copy of /synthetic/set.als'),
 ('checkpoint_set', {'label': 'requested'}, {'checkpointed': False, 'error': 'save_failed', 'save': {'saved': False, 'ui_path': 'attempted', 'error': 'unverified'}}, 'No checkpoint: Could NOT save: unverified.'),
 ('checkpoint_set', {'label': 'requested'}, {'checkpointed': False, 'error': 'set_path_unknown', 'candidates': []}, "Saved, but no checkpoint: the open set's .als could not be identified (candidates: []). Pass set_path."),
 ('checkpoint_set', {'label': 'requested'}, {'checkpointed': None, 'checkpoint_path': None}, 'Checkpoint copy at None does NOT match the saved set; do not rely on it.'),
]

async def call(deps, mode, name, args):
    async with create_connected_server_and_client_session(build_app(deps=deps, output_mode=mode)) as session:
        return await session.call_tool(name, args)

@pytest.mark.parametrize('mode', ['structured', 'both', 'legacy'])
@pytest.mark.parametrize('name,args,raw,text', CASES)
def test_custody_raw_outcomes_not_inferred_from_text(mode, name, args, raw, text):
    deps, _ = _make_deps()
    raw = dict(raw, unknown=None, verified=False, future={'opaque': '音楽'})
    original = copy.deepcopy(raw)
    calls = []
    def service(*a, **kw):
        calls.append((a, kw))
        return raw
    setattr(deps.service, name, service)
    result = asyncio.run(call(deps, mode, name, args))
    assert not result.isError
    assert len(calls) == 1 and raw == original
    assert result.content[0].text == text
    if mode != 'legacy':
        assert result.structuredContent == original

@pytest.mark.parametrize('mode', ['structured', 'both', 'legacy'])
@pytest.mark.parametrize('name,args', list({name: args for name,args,raw,text in CASES}.items()))
@pytest.mark.parametrize('refusal', [False, True])
def test_custody_exceptions_never_become_success(mode, name, args, refusal):
    deps, _ = _make_deps()
    error = LockError('not_held') if refusal else RuntimeError('synthetic save failure')
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

@pytest.mark.parametrize('mode', ['structured', 'both', 'legacy'])
@pytest.mark.parametrize('name,args', [
 ('get_live_status', {}),
 ('resume_live_lock', {'owner': 'test', 'owner_proof': {}, 'expected_generation': 'g'}),
 ('transfer_live_lock', {'from_owner': 'test', 'to_owner': 'next', 'save_proof': {}, 'expected_generation': 'g'}),
])
def test_status_and_handoff_aliases_already_json(mode, name, args):
    deps, _ = _make_deps()
    raw = {'verified': False, 'unknown': None, 'future': {'opaque': '音楽'}}
    calls = []
    def service(*a, **kw):
        calls.append(1)
        return copy.deepcopy(raw)
    setattr(deps.service, name, service)
    result = asyncio.run(call(deps, mode, name, args))
    assert not result.isError and len(calls) == 1
    if mode != 'legacy': assert result.structuredContent == raw
    if mode == 'structured': assert result.content == []
    else: assert json.loads(result.content[0].text) == raw
