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
