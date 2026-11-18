"""A4 failure reporting only; fake clients and no native UI/network."""
import json
import pytest
from ableton_mcp import presenters, render
from ableton_mcp.services import AbletonService
from ableton_mcp.live_lock import LockError
from golden_runner import AllCapabilitiesHandshake
from test_load_state_preservation import fixture, load


@pytest.mark.parametrize('path', [False, True])
def test_standalone_post_dispatch_loss_is_unknown(path):
    class Client:
        def send_command(self, *args): raise RuntimeError('response lost after native load')
    service = AbletonService(Client(), AllCapabilitiesHandshake())
    result = service.load_device_from_path(0, '/fixture.adv') if path else service.load_browser_item(0, 'fixture')
    assert result['loaded'] is None
    assert result['reconciliation_required'] is True
    assert result['retry_safe'] is False
    assert result['state_preservation'] == 'unknown'
    text = presenters.load_device_from_path(result) if path else presenters.load_instrument_or_effect(result,0,'fixture')
    assert json.loads(text)['loaded'] is None


@pytest.mark.parametrize('path', [False, True])
def test_proven_presend_lock_refusal_stays_refusal(path):
    class Lock:
        def check_write(self, *a): raise LockError('not_holder')
    class Client:
        def send_command(self, *a): pytest.fail('must not send')
    service = AbletonService(Client(), AllCapabilitiesHandshake(), lock=Lock())
    with pytest.raises(LockError):
        if path: service.load_device_from_path(0,'/fixture.adv')
        else: service.load_browser_item(0,'fixture')


def test_first_rack_uncertainty_has_stage_and_no_lookup():
    class Client:
        def send_command(self, cmd, params):
            assert cmd == 'load_browser_item'
            raise RuntimeError('response lost')
    service = AbletonService(Client(), AllCapabilitiesHandshake())
    result = service.load_drum_kit(0,'rack','kit')
    assert result['stage'] == 'rack_failed'
    assert result['rack_result']['loaded'] is None
    assert result['reconciliation_required'] is True


def test_lookup_exception_keeps_completed_rack_without_inventing_kit_mutation():
    service = AbletonService(None,None)
    service.load_browser_item = lambda *a,**k: {'loaded':True,'changed_state':[]}
    def fail(*a): raise RuntimeError('lookup response lost')
    service.get_browser_items_at_path = fail
    result = service.load_drum_kit(0,'rack','kit')
    assert result['stage'] == 'kit_lookup_failed'
    assert result['rack_result']['loaded'] is True
    assert result['partial_completion'] is True
    assert result['whole_workflow_retry_safe'] is False
    assert result['kit_load_attempted'] is False
    assert result['reconciliation_required'] is False
    assert json.loads(presenters.load_drum_kit(result))['rack_result']['loaded'] is True


def test_second_load_lock_refusal_keeps_rack_and_refusal():
    service = AbletonService(None,None)
    calls = []
    def one(*a,**k):
        calls.append(a)
        if len(calls) == 2: raise LockError('not_holder', holder='other')
        return {'loaded':True,'changed_state':[]}
    service.load_browser_item = one
    service.get_browser_items_at_path = lambda *a: {'items':[{'uri':'kit','is_loadable':True}]}
    result = service.load_drum_kit(0,'rack','kit')
    assert result['stage'] == 'kit_failed'
    assert result['rack_result']['loaded'] is True
    assert result['kit_result']['loaded'] is False
    assert result['kit_result']['reason'] == 'not_holder'
    assert result['kit_load_attempted'] is False
    assert result['whole_workflow_retry_safe'] is False
    assert result['reconciliation_required'] is False
    assert len(calls) == 2


def test_native_exception_retains_bounded_device_evidence_not_success():
    h,item = fixture()
    native = h.app.browser.load_item
    def fail(item):
        native(item)
        raise RuntimeError('native after arrival')
    h.app.browser.load_item = fail
    result = load(h)
    assert result['loaded'] is None
    assert result['reconciliation_required'] is True
    assert result['devices_after'][-1] == 'Fixture Device'
    assert result['new_devices'] == ['Fixture Device']


def test_batch_later_lock_refusal_retains_completed_load():
    service = AbletonService(None,None)
    calls=[]
    def one(*a,**k):
        calls.append(a)
        if len(calls)==2: raise LockError('not_holder')
        return {'loaded':True,'changed_state':[]}
    service.load_browser_item=one
    result=service.load_devices(0,['one','two','three'])
    assert result['loaded_count']==1
    assert result['failed']['result']['state_preservation']=='not_attempted'
    assert result['reconciliation_required'] is False
    assert result['whole_workflow_retry_safe'] is False
    assert json.loads(presenters.load_devices(result))['loaded_count']==1


def test_gate_failure_before_dispatch_is_not_uncertain():
    class Gate:
        def require(self,*a,**k): raise ValueError('gate refused')
    class Client:
        def send_command(self,*a): pytest.fail('no dispatch')
    service=AbletonService(Client(),Gate())
    with pytest.raises(ValueError,match='gate refused'):
        service.load_browser_item(0,'fixture')


def test_watchdog_diagnostics_survive_uncertainty(tmp_path):
    from test_load_alert_watchdog import FakeUI, FakeClient, ALERT, _service
    ui=FakeUI()
    client=FakeClient(error=TimeoutError('No data received'))
    client.gate=ui.unblock
    service=_service(client,ui=ui,tmp_path=tmp_path)
    result=service.load_device_from_path(0,'/fixture.adv')
    assert result['loaded'] is None
    assert result['diagnostics']['reason']=='live_alert'
    assert result['diagnostics']['details']['alert_text']==ALERT['text']


def test_native_failure_device_evidence_is_bounded():
    from fake_ableton import FakeDevice
    h,item=fixture()
    def fail(item):
        h.song.tracks[1].devices=[FakeDevice(str(i)) for i in range(140)]
        raise RuntimeError('partial')
    h.app.browser.load_item=fail
    result=load(h)
    assert result['loaded'] is None
    assert len(result['devices_after'])==128
    assert result['device_evidence_complete'] is False
