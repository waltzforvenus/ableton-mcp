from fake_ableton import make_harness


def test_snapshot_known_mute_and_monitoring_without_note_read():
    h = make_harness()
    track = h.song.tracks[0]
    track.current_monitoring_state = 2
    clip = track.clip_slots[0].clip
    clip.muted = True
    result = h.mcp._snapshot_track(0, track, False, False, False, False, False)
    assert result['monitoring_state'] == 2
    data = result['clip_slots'][0]['clip']
    assert data['muted'] is True
    assert data['note_count'] is None
    assert 'notes' not in data


def test_common_clip_unknown_mute_and_count_are_null():
    h = make_harness()
    class Unknown:
        @property
        def muted(self): raise RuntimeError('unreadable')
    result = h.mcp._serialize_clip_common(Unknown())
    assert result['muted'] is None
    assert result['note_count'] is None


def test_return_master_monitoring_and_device_state_parity():
    h = make_harness()
    h.song.return_tracks[0].current_monitoring_state = 1
    h.song.master_track.current_monitoring_state = 0
    returns = h.mcp._serialize_return_tracks(include_params=False)
    master = h.mcp._serialize_master_track(include_params=False)
    assert returns[0]['monitoring_state'] == 1
    assert master['monitoring_state'] == 0
    for track in returns + [master]:
        for device in track['devices']:
            assert 'is_active' in device
            assert 'parameters' not in device


def test_unavailable_monitoring_is_null_not_default_mode():
    h = make_harness()
    track = h.song.tracks[0]
    class Unavailable:
        def __getattr__(self, name):
            if name == 'current_monitoring_state':
                raise RuntimeError('read unavailable')
            return getattr(track, name)
    result = h.mcp._snapshot_track(0, Unavailable(), False, False, False, False, False)
    assert result['monitoring_state'] is None


import pytest


@pytest.mark.parametrize('value,expected', [(None, None), ('false', None),
    ('true', None), (0, None), (1, None), ([], None), ({}, None),
    (False, False), (True, True)])
@pytest.mark.parametrize('kind', ['session', 'arrangement'])
def test_mute_accepts_only_native_boolean_not_truthiness(value, expected, kind):
    h = make_harness()
    clip = h.song.tracks[0].clip_slots[0].clip
    clip.start_time, clip.end_time = 0, 4
    clip.muted = value
    result = getattr(h.mcp, '_serialize_' + kind + '_clip')(clip, include_notes=False)
    assert result['muted'] is expected


def test_missing_mute_is_unknown():
    h = make_harness()
    assert h.mcp._serialize_clip_common(object())['muted'] is None


def test_explicit_unknown_mute_remains_json_null_over_mcp():
    import asyncio
    import json
    from mcp.shared.memory import create_connected_server_and_client_session
    from ableton_mcp.app import build_app
    from test_full_stack import _make_deps
    deps, h = _make_deps()
    h.song.tracks[0].clip_slots[0].clip.muted = None
    async def run():
        async with create_connected_server_and_client_session(build_app(deps=deps)) as session:
            return await session.call_tool('get_session_snapshot', {'max_bytes': 40000})
    result = asyncio.run(run())
    assert not result.isError
    wire = json.loads(result.model_dump_json(by_alias=True, exclude_none=True))
    assert wire['structuredContent']['tracks'][0]['clip_slots'][0]['clip']['muted'] is None
