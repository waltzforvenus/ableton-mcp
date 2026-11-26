import pytest
from fake_ableton import make_harness


@pytest.mark.parametrize('kind', ['session', 'arrangement'])
def test_failed_note_read_is_unknown_not_empty(kind):
    h = make_harness()
    clip = h.song.tracks[0].clip_slots[0].clip
    clip.start_time, clip.end_time = 0, 4
    def fail(*args): raise RuntimeError('native read unavailable')
    clip.get_notes_extended = fail
    clip.get_notes = fail
    result = getattr(h.mcp, '_serialize_' + kind + '_clip')(clip)
    assert result['notes'] is None and result['note_count'] is None
    assert result['notes_status'] == 'unavailable'
    assert 'native read unavailable' in result['notes_error']


def test_midstream_extended_failure_does_not_expose_partial_notes():
    h = make_harness()
    clip = h.song.tracks[0].clip_slots[0].clip
    original = list(clip.get_notes_extended(0, 128, 0, 100))
    assert original
    def partial(*args):
        yield original[0]
        raise RuntimeError('interrupted enumeration')
    clip.get_notes_extended = partial
    result = h.mcp._serialize_session_clip(clip)
    assert result['notes'] is None
    assert result['note_count'] is None


def test_successful_empty_read_is_distinct_from_failure():
    h = make_harness()
    clip = h.song.tracks[0].clip_slots[0].clip
    clip.get_notes_extended = lambda *args: []
    result = h.mcp._serialize_session_clip(clip)
    assert result['notes'] == [] and result['note_count'] == 0
    assert result['notes_status'] == 'read'


def test_excluded_notes_never_read_and_report_not_requested():
    h = make_harness()
    clip = h.song.tracks[0].clip_slots[0].clip
    def fail(*args): pytest.fail('unrequested note read')
    clip.get_notes_extended = fail
    result = h.mcp._serialize_session_clip(clip, include_notes=False)
    assert 'notes' not in result
    assert result['notes_status'] == 'not_requested'


def test_malformed_extended_note_is_not_fabricated_as_middle_c():
    h = make_harness()
    clip = h.song.tracks[0].clip_slots[0].clip
    clip.get_notes_extended = lambda *args: [object()]
    result = h.mcp._serialize_session_clip(clip)
    assert result['notes_status'] == 'unavailable'
    assert result['note_count'] is None


def test_legacy_reader_failure_propagates_in_strict_mode():
    h = make_harness()
    class Legacy:
        is_midi_clip = True
        length = 4
        def get_notes(self, *args): raise RuntimeError('legacy unavailable')
    with pytest.raises(RuntimeError, match='legacy unavailable'):
        h.mcp._notes_from_clip(Legacy(), strict=True)


def test_failed_snapshot_notes_remain_null_over_actual_mcp():
    import asyncio
    from mcp.shared.memory import create_connected_server_and_client_session
    from ableton_mcp.app import build_app
    from test_full_stack import _make_deps
    deps, h = _make_deps()
    def fail(*args): raise RuntimeError('native unavailable')
    h.song.tracks[0].clip_slots[0].clip.get_notes_extended = fail
    async def run():
        async with create_connected_server_and_client_session(build_app(deps=deps)) as session:
            return await session.call_tool('get_session_snapshot', {'max_bytes': 40000})
    result = asyncio.run(run())
    assert not result.isError
    clip = result.structuredContent['tracks'][0]['clip_slots'][0]['clip']
    assert clip['notes'] is None and clip['note_count'] is None
    assert clip['notes_status'] == 'unavailable'
