"""Native loader safety via fake LOM; no GUI, Live, sockets or media."""
import json
import pytest
from fake_ableton import make_harness, FakeTrack, FakeDevice, FakeBrowserItem
from ableton_mcp.services import AbletonService
from ableton_mcp import presenters


def fixture():
    h = make_harness()
    item = FakeBrowserItem("Fixture Device", "fixture", is_loadable=True)
    h.mcp._find_browser_item_by_uri = lambda *a: item
    h.mcp._browser_item_for_path = lambda *a: item
    return h, item


def load(h, mode='uri'):
    command = 'load_browser_item' if mode == 'uri' else 'load_browser_item_by_path'
    params = {'track_index':1, 'item_uri':'fixture'} if mode == 'uri' else {'track_index':1,'path':'/fixture.adv'}
    response = h.process({'type':command,'params':params})
    assert response['status'] == 'success', response
    return response['result']


@pytest.mark.parametrize('mode', ['uri','path'])
def test_unchanged_states_are_reported_not_hidden(mode):
    h, item = fixture()
    h.song.tracks[0].arm = True
    h.song.tracks[0].current_monitoring_state = 2
    result = load(h, mode)
    assert result['loaded'] is True
    assert result['state_preservation'] == 'unchanged_at_return'
    assert result['changed_state'] == []
    assert h.song.tracks[0].arm is True
    assert h.song.tracks[0].current_monitoring_state == 2
    assert h.song.tracks[1].arm is False


@pytest.mark.parametrize('mode', ['uri','path'])
def test_unattributed_exclusive_arm_transfer_is_not_blindly_restored(mode):
    h, item = fixture()
    h.song.tracks[0].arm = True
    h.song.exclusive_arm = True
    native = h.app.browser.load_item
    def transfer(item):
        native(item)
        h.song.tracks[0].arm = False
        h.song.tracks[1].arm = True
        h.song.tracks[1].current_monitoring_state = 0
    h.app.browser.load_item = transfer
    result = load(h, mode)
    assert result['reconciliation_required'] is True
    assert result['loaded'] is True  # device arrived, but not a safe completed workflow
    assert len(result['changed_state']) == 2
    assert h.song.tracks[0].arm is False and h.song.tracks[1].arm is True
    assert h.song.exclusive_arm is True


@pytest.mark.parametrize('change', ['human','reorder','delete','new_track','raise_after'])
def test_uncertain_load_reports_observed_state_without_compensation(change):
    h, item = fixture()
    native = h.app.browser.load_item
    def changed(item):
        native(item)
        if change == 'human': h.song.tracks[0].current_monitoring_state = 0
        if change == 'reorder': h.song.tracks.reverse()
        if change == 'delete': h.song.tracks.pop(0)
        if change == 'new_track': h.song.tracks.append(FakeTrack('New'))
        if change == 'raise_after': raise RuntimeError('native partial failure')
    h.app.browser.load_item = changed
    result = load(h)
    assert result['reconciliation_required'] is True
    assert result['retry_safe'] is False
    if change == 'human': assert h.song.tracks[0].current_monitoring_state == 0
    if change == 'new_track': assert h.song.tracks[-1].arm is False
    assert 'changed_state' in result


def test_same_count_replacement_is_verified_by_identity():
    h, item = fixture()
    def replace(item): h.song.tracks[1].devices = [FakeDevice('Replacement')]
    h.app.browser.load_item = replace
    result = load(h)
    assert result['loaded'] is True
    assert result['new_devices'] == ['Replacement']
    assert result['state_preservation'] == 'unchanged_at_return'


def test_no_device_arrival_requires_reconciliation():
    h, item = fixture()
    h.app.browser.load_item = lambda item: None
    result = load(h)
    assert result['loaded'] is False
    assert result['reconciliation_required'] is True


def test_batch_stops_at_second_reconciliation_and_keeps_evidence():
    service = AbletonService(None, None)
    calls = []
    def one(*args):
        calls.append(args)
        return {'loaded':True,'item_name':'Device','devices_after':['Device'],
                'changed_state':[], 'reconciliation_required':len(calls)==2}
    service.load_browser_item = one
    result = service.load_devices(0, ['one','two','three'])
    assert len(calls) == 2
    assert result['loaded_count'] == 1
    assert result['failed']['result']['reconciliation_required'] is True
    assert result['not_attempted'] == ['three']
    assert result['loaded'][0]['changed_state'] == []


def test_kit_does_not_continue_past_uncertain_rack():
    service = AbletonService(None, None)
    service.load_browser_item = lambda *a, **k: {'loaded':True,'reconciliation_required':True,'changed_state':[{'arm':True}]}
    service.get_browser_items_at_path = lambda *a: pytest.fail('must stop')
    result = service.load_drum_kit(0,'rack','kit')
    assert result['reconciliation_required'] is True
    assert result['stage'] == 'rack_failed'


@pytest.mark.parametrize('kind', ['uri','path','batch','kit'])
def test_presenters_keep_reconciliation_and_state_diff(kind):
    result = {'loaded':True,'reconciliation_required':True,'changed_state':[{'arm':True}]}
    if kind == 'uri': text = presenters.load_instrument_or_effect(result, 0, 'fixture')
    elif kind == 'path': text = presenters.load_device_from_path(result)
    elif kind == 'batch': text = presenters.load_devices(result)
    else: text = presenters.load_drum_kit(dict(result,stage='rack_failed'))
    assert json.loads(text)['reconciliation_required'] is True


def test_batch_identity_and_state_token_prevents_second_wrong_track_load():
    h, item = fixture()
    first = load(h)
    token = first['load_state_token']
    count = len(h.app.browser.loads)
    h.song.tracks.reverse()
    response = h.process({'type':'load_browser_item','params':{
        'track_index':1,'item_uri':'fixture','expect_load_state_token':token}})
    assert response['status'] == 'success'
    assert response['result']['reconciliation_required'] is True
    assert len(h.app.browser.loads) == count


def test_batch_forwards_prior_state_token_and_keeps_diffs():
    service = AbletonService(None, None)
    calls = []
    def one(*args, **kwargs):
        calls.append(kwargs)
        return {'loaded':True,'load_state_token':'token','changed_state':[], 'new_devices':['X']}
    service.load_browser_item = one
    result = service.load_devices(0,['one','two'])
    assert calls[1]['expect_load_state_token'] == 'token'
    assert json.loads(presenters.load_devices(result))['loaded'][0]['changed_state'] == []


def test_successful_kit_exposes_both_load_state_reports():
    service = AbletonService(None, None)
    calls = []
    def one(*args, **kwargs):
        calls.append(kwargs)
        return {'loaded':True,'load_state_token':'token','changed_state':[]}
    service.load_browser_item = one
    service.get_browser_items_at_path = lambda *a: {'items':[{'is_loadable':True,'uri':'kit','name':'Kit'}]}
    result = service.load_drum_kit(0,'rack','kit')
    assert calls[1]['expect_load_state_token'] == 'token'
    assert json.loads(presenters.load_drum_kit(result))['kit_result']['changed_state'] == []


def test_changed_state_holds_followup_loads():
    h, item = fixture()
    native = h.app.browser.load_item
    def changed(item):
        native(item)
        h.song.tracks[0].current_monitoring_state = 0
    h.app.browser.load_item = changed
    assert load(h)['reconciliation_required'] is True
    count = len(h.app.browser.loads)
    assert load(h)['reconciliation_required'] is True
    assert len(h.app.browser.loads) == count


def test_partial_second_load_keeps_latest_device_readback():
    service = AbletonService(None, None)
    results = iter([
        {'loaded':True,'devices_after':['First'],'changed_state':[]},
        {'loaded':True,'devices_after':['First','Partial'],'reconciliation_required':True,'changed_state':[{'monitoring':0}]},
    ])
    service.load_browser_item = lambda *a, **k: next(results)
    result = service.load_devices(0,['one','two','three'])
    assert [d['name'] for d in result['devices']] == ['First','Partial']
    assert result['failed']['result']['changed_state'] == [{'monitoring':0}]


def test_kit_second_send_failure_keeps_rack_evidence_and_marks_uncertainty():
    service = AbletonService(None, None)
    calls = []
    def one(*a, **k):
        calls.append(a)
        if len(calls) == 2: raise RuntimeError('lost response')
        return {'loaded':True,'changed_state':[]}
    service.load_browser_item = one
    service.get_browser_items_at_path = lambda *a: {'items':[{'is_loadable':True,'uri':'kit'}]}
    result = service.load_drum_kit(0,'rack','kit')
    assert result['reconciliation_required'] is True
    assert result['rack_result']['changed_state'] == []
    assert result['kit_result']['loaded'] is None


def test_batch_unknown_send_failure_does_not_claim_no_reconciliation():
    service = AbletonService(None, None)
    def fail(*a, **k): raise RuntimeError('lost response')
    service.load_browser_item = fail
    result = service.load_devices(0,['one','two'])
    assert result['reconciliation_required'] is True
    assert result['failed']['result']['loaded'] is None
    assert result['not_attempted'] == ['two']
