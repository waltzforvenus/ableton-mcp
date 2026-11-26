"""Server-side read projections, with unchanged native wire requirements."""
import copy
import pytest
from test_full_stack import _make_deps
from test_structured_output import call


def service_with(rows):
    deps, _ = _make_deps()
    payload = {'device_name': '音楽', 'parameter_count': len(rows), 'parameters': rows}
    deps.service._send = lambda command, params: payload
    return deps.service, payload


def test_exact_names_preserve_source_order_duplicates_and_report_missing():
    service, payload = service_with([
        {'index': 0, 'name': '音', 'value': .2},
        {'index': 1, 'name': 'Other', 'value': .3},
        {'index': 2, 'name': '音', 'value': .4}])
    before = copy.deepcopy(payload)
    result = service.get_device_parameters(0, 0, 'regular', names=['Missing', '音'])
    assert [p['index'] for p in result['parameters']] == [0, 2]
    assert result['missing_names'] == ['Missing']
    assert result['returned_parameter_count'] == 2
    assert result['parameter_count'] == 3
    assert payload == before


def test_empty_filter_means_none_not_all():
    service, _ = service_with([{'index': 0, 'name': 'A'}])
    assert service.get_device_parameters(0, 0, 'regular', names=[])['parameters'] == []


def test_compact_keeps_identity_values_and_reports_omitted_fields():
    service, payload = service_with([{'index': 3, 'name': 'A', 'value': .2,
                                     'min': 0, 'max': 1, 'future_field': 'opaque'}])
    result = service.get_device_parameters(0, 0, 'regular', compact=True)
    assert result['parameters'] == [{'index': 3, 'name': 'A', 'value': .2, 'display_value': None}]
    assert result['omitted_parameter_fields'] == ['future_field', 'max', 'min']
    assert payload['parameters'][0]['future_field'] == 'opaque'


@pytest.mark.parametrize('names,compact', [('A', False), ([1], False), (None, 'false')])
def test_invalid_projection_refused_before_read(names, compact):
    service, _ = service_with([])
    def forbidden(*args): pytest.fail('invalid request read native data')
    service._send = forbidden
    with pytest.raises(ValueError):
        service.get_device_parameters(0, 0, 'regular', names=names, compact=compact)


def test_actual_mcp_filters_and_compacts_without_nested_json():
    result = call('structured', 'get_device_parameters',
                  {'track_index': 0, 'device_index': 0, 'names': [], 'compact': True})
    assert not result.isError
    assert result.structuredContent['parameters'] == []
    assert result.structuredContent['requested_scope'] == {'names': [], 'compact': True}
    assert result.content == []


@pytest.mark.parametrize('track_type', ['regular', 'return', 'master'])
def test_projection_preserves_native_address_and_unknown_read_refuses(track_type):
    service, _ = service_with([])
    def send(command, params):
        assert command == 'get_device_parameters'
        assert params == {'track_index': 2, 'device_index': 1, 'track_type': track_type}
        return {'parameters': None}
    service._send = send
    with pytest.raises(ValueError, match='known parameter list'):
        service.get_device_parameters(2, 1, track_type, names=[])


def test_mcp_nonempty_filter_retains_parameter_identity():
    result = call('structured', 'get_device_parameters',
                  {'track_index': 0, 'device_index': 0, 'names': ['Device On'], 'compact': True})
    assert not result.isError
    assert result.structuredContent['parameters'][0]['name'] == 'Device On'
    assert len(result.structuredContent['parameters']) == 1
    assert set(result.structuredContent['parameters'][0]) == {'index', 'name', 'value', 'display_value'}
