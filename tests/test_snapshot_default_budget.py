import pytest
from test_snapshot_output import call, size


def payload():
    return {'tracks': [{'index': i, 'name': '音楽🎹' * 1000} for i in range(8)]}


@pytest.mark.parametrize('mode', ['structured', 'both'])
def test_default_snapshot_is_bounded_and_projected(mode):
    result = call(payload(), {}, mode)
    assert not result.isError and size(result) <= 40000
    assert result.structuredContent['next_cursor']
    assert result.structuredContent['tracks'][0]['monitoring_state'] is None


@pytest.mark.parametrize('legacy_cap', [None, 0])
def test_legacy_unlimited_switch_does_not_disable_structured_safety(legacy_cap):
    result = call(payload(), {'max_chars': legacy_cap})
    assert not result.isError and size(result) <= 40000


def test_positive_legacy_cap_maps_to_stricter_byte_budget():
    result = call(payload(), {'max_chars': 12000})
    assert not result.isError and size(result) <= 12000


def test_explicit_bytes_overrides_legacy_cap():
    result = call(payload(), {'max_chars': 1, 'max_bytes': 25000})
    assert not result.isError and size(result) <= 25000


def test_legacy_mode_keeps_original_text_without_projection():
    import json
    result = call(payload(), {}, 'legacy')
    assert json.loads(result.content[0].text) == payload()


def test_default_cursor_continues_without_explicit_byte_argument():
    args = {}
    seen = []
    for _ in range(8):
        result = call(payload(), args)
        assert not result.isError and size(result) <= 40000
        seen.extend(t['index'] for t in result.structuredContent['tracks'])
        cursor = result.structuredContent['next_cursor']
        if cursor is None:
            break
        args['cursor'] = cursor
    assert seen == list(range(8))


def test_default_refuses_single_oversized_track():
    result = call({'tracks': [{'index': 0, 'name': '音' * 20000}]}, {})
    assert result.isError
    assert 'budget' in result.structuredContent['message']


def test_positive_tiny_compatibility_cap_refuses_not_oversized_success():
    result = call(payload(), {'max_chars': 1})
    assert result.isError
