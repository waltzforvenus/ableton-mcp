import copy
import pytest
from test_snapshot_output import call


def fixture():
    device = {'name': 'Rack', 'parameters': [{'name': 'Gain'}],
              'chains': [{'devices': [{'name': 'Nested', 'parameters': [{'value': .5}]}]}]}
    track = {'devices': [device], 'clip_slots': [{'clip': {'name': 'MIDI'}}]}
    return {'tracks': [dict(track, index=0)], 'return_tracks': [copy.deepcopy(track)],
            'master_track': copy.deepcopy(track)}


def test_old_script_unknowns_normalized_across_track_kinds_and_chains():
    result = call(fixture(), {'max_bytes': 20000, 'include_rack_chains': True})
    assert not result.isError
    body = result.structuredContent
    for track in body['tracks'] + body['return_tracks'] + [body['master_track']]:
        assert track['monitoring_state'] is None
        device = track['devices'][0]
        assert device['is_active'] is None
        assert device['chains'][0]['devices'][0]['is_active'] is None
        clip = track['clip_slots'][0]['clip']
        assert clip['muted'] is None and clip['note_count'] is None


def test_flags_remove_nested_data_for_returns_master_and_regular_tracks():
    payload = fixture()
    for track in payload['tracks'] + payload['return_tracks'] + [payload['master_track']]:
        track['clip_slots'][0]['clip']['notes'] = [{'pitch': 60}]
    body = call(payload, {'max_bytes': 20000, 'include_notes': False,
                         'include_params': False, 'include_rack_chains': True}).structuredContent
    for track in body['tracks'] + body['return_tracks'] + [body['master_track']]:
        assert 'parameters' not in track['devices'][0]
        assert 'parameters' not in track['devices'][0]['chains'][0]['devices'][0]
        assert 'notes' not in track['clip_slots'][0]['clip']


def test_missing_device_and_clip_collections_are_unknown_not_empty():
    body = call({'tracks': [{'index': 0}], 'return_tracks': [{}], 'master_track': {}},
                {'max_bytes': 20000}).structuredContent
    for track in body['tracks'] + body['return_tracks'] + [body['master_track']]:
        assert track['devices'] is None
        assert track['clip_slots'] is None
        assert track['arrangement_clips'] is None


def test_legacy_snapshot_shape_unchanged():
    import json
    payload = fixture()
    result = call(payload, {}, 'legacy')
    assert json.loads(result.content[0].text) == payload


@pytest.mark.parametrize('mode', ['structured', 'both'])
def test_old_native_unknowns_survive_actual_json_serialization(mode):
    import json
    payload = {'tracks': [{'index': 0, 'monitoring_state': 'off',
                          'devices': [{'is_active': 'false'}]}]}
    result = call(payload, {'max_bytes': 20000}, mode)
    assert not result.isError
    wire = json.loads(result.model_dump_json(by_alias=True, exclude_none=True))
    track = wire['structuredContent']['tracks'][0]
    assert track['monitoring_state'] is None
    assert track['devices'][0]['is_active'] is None
    if mode == 'both':
        assert json.loads(result.content[0].text) == result.structuredContent


def test_malformed_nested_chain_refuses_instead_of_silently_empty():
    payload = fixture()
    payload['master_track']['devices'][0]['chains'] = 'unknown'
    result = call(payload, {'max_bytes': 20000, 'include_rack_chains': True})
    assert result.isError
    assert 'malformed' in result.structuredContent['message']


def test_projection_does_not_mutate_source_or_discard_unrelated_fields():
    from ableton_mcp.snapshot_projection import normalize
    payload = fixture()
    payload['master_track']['future'] = {'opaque': [1, 2]}
    original = copy.deepcopy(payload)
    result = normalize(payload, {'include_notes': False, 'include_params': False,
                                'include_rack_chains': True, 'include_warp_markers': False})
    assert payload == original
    assert result['master_track']['future'] == {'opaque': [1, 2]}


def leaf_target(payload, kind, field, nested=False):
    track = (payload['master_track'] if kind == 'master' else
             payload['return_tracks' if kind == 'return' else 'tracks'][0])
    if field == 'parameters':
        device = track['devices'][0]
        return device['chains'][0]['devices'][0] if nested else device
    if nested:
        if 'arrangement_clips' not in track:
            track['arrangement_clips'] = [copy.deepcopy(track['clip_slots'][0]['clip'])]
        return track['arrangement_clips'][0]
    return track['clip_slots'][0]['clip']


LEAF_FLAGS = {'notes': 'include_notes', 'parameters': 'include_params',
              'warp_markers': 'include_warp_markers'}


@pytest.mark.parametrize('kind', ['regular', 'return', 'master'])
@pytest.mark.parametrize('field', LEAF_FLAGS)
@pytest.mark.parametrize('value', ['unavailable', {'unexpected': 1}, [42]])
@pytest.mark.parametrize('nested', [False, True])
def test_requested_malformed_leaves_refuse_through_mcp(kind, field, value, nested):
    payload = fixture()
    leaf_target(payload, kind, field, nested)[field] = value
    original = copy.deepcopy(payload)
    result = call(payload, {LEAF_FLAGS[field]: True, 'include_rack_chains': True})
    assert result.isError
    assert result.structuredContent['code'] == 'invalid_argument'
    assert 'malformed snapshot collection: ' + field in result.structuredContent['message']
    assert payload == original


@pytest.mark.parametrize('mode', ['structured', 'both'])
@pytest.mark.parametrize('field', LEAF_FLAGS)
@pytest.mark.parametrize('value', [None, [], [{'future': {'opaque': [1, 'λ']}}]])
def test_requested_valid_leaf_shapes_preserved(mode, field, value):
    import json
    payload = fixture()
    for kind in ['regular', 'return', 'master']:
        for nested in [False, True]:
            leaf_target(payload, kind, field, nested)[field] = copy.deepcopy(value)
    original = copy.deepcopy(payload)
    result = call(payload, {LEAF_FLAGS[field]: True, 'include_rack_chains': True}, mode)
    assert not result.isError
    body = json.loads(result.model_dump_json(by_alias=True, exclude_none=True))['structuredContent']
    for kind in ['regular', 'return', 'master']:
        for nested in [False, True]:
            assert leaf_target(body, kind, field, nested)[field] == value
    assert payload == original


@pytest.mark.parametrize('field', LEAF_FLAGS)
def test_missing_requested_leaves_unknown_and_excluded_malformed_dropped(field):
    from ableton_mcp.snapshot_projection import normalize
    payload = fixture()
    scope = dict(include_notes=True, include_params=True,
                 include_rack_chains=True, include_warp_markers=True)
    for kind in ['regular', 'return', 'master']:
        leaf_target(payload, kind, field).pop(field, None)
    body = normalize(payload, scope)
    for kind in ['regular', 'return', 'master']:
        assert leaf_target(body, kind, field)[field] is None
        for nested in [False, True]:
            leaf_target(payload, kind, field, nested)[field] = 'malformed'
    original = copy.deepcopy(payload)
    scope[LEAF_FLAGS[field]] = False
    body = normalize(payload, scope)
    for kind in ['regular', 'return', 'master']:
        for nested in [False, True]:
            assert field not in leaf_target(body, kind, field, nested)
    assert payload == original
