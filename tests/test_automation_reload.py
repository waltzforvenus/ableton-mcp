"""Read-only reconciliation using injected observations and temporary copies."""
from copy import deepcopy
import pytest


def setup(tmp_path,state=2):
    from ableton_mcp.automation_reload import AutomationReload
    path=tmp_path/'copy.xml'; path.write_bytes(b'<Ableton/>')
    clock=[10.0]; calls=[]
    row={'supported':True,'source':'live','coverage':'state_only','automation_state':state,
         'overridden':state==2,'envelope_present':state!=0,'parameter_name':'Volume'}
    def observe(target): calls.append(deepcopy(target)); return deepcopy(row)
    model=AutomationReload(clock=lambda:clock[0])
    return model,path,clock,row,calls,observe


def test_dry_run_never_authorizes_reload_even_unchanged(tmp_path):
    model,path,clock,row,calls,observe=setup(tmp_path)
    plan=model.plan(str(path),[{'kind':'mixer','parameter':'volume'}],observe)
    assert plan['reload_authorized'] is False and plan['override_risk_indices']==[0]
    result=model.reconcile(plan['plan_id'],plan['fingerprint'],observe)
    assert result['comparison']=='unchanged' and result['reload_authorized'] is False
    assert result['identity_verified'] is False and len(calls)==2


def test_fresh_override_loss_reported_not_repaired(tmp_path):
    model,path,clock,row,calls,observe=setup(tmp_path)
    plan=model.plan(str(path),[{'parameter':0}],observe)
    row.update(automation_state=1,overridden=False)
    result=model.reconcile(plan['plan_id'],plan['fingerprint'],observe)
    assert result['comparison']=='changed' and result['possible_reenabled_indices']==[0]
    assert result['reconciliation_required'] and row['overridden'] is False


@pytest.mark.parametrize('fault',['unknown','saved','inconsistent','read_error'])
def test_unknown_never_equals_safe(tmp_path,fault):
    model,path,clock,row,calls,observe=setup(tmp_path)
    if fault=='unknown': row.update(automation_state=None,overridden=None,supported=False)
    if fault=='saved': row['source']='saved_copy'
    if fault=='inconsistent': row['overridden']=False
    if fault=='read_error':
        def observe(target): raise RuntimeError('unreadable')
    plan=model.plan(str(path),[{'parameter':0}],observe)
    result=model.reconcile(plan['plan_id'],plan['fingerprint'],observe)
    assert result['comparison']=='unknown' and result['reconciliation_required']
    assert not result['reload_authorized']


@pytest.mark.parametrize('fault',['file','fingerprint','expired'])
def test_stale_plan_refuses_before_reads(tmp_path,fault):
    model,path,clock,row,calls,observe=setup(tmp_path)
    plan=model.plan(str(path),[{'parameter':0}],observe)
    if fault=='file': path.write_bytes(b'changed')
    if fault=='fingerprint': plan['fingerprint']='forged'
    if fault=='expired': clock[0]+=301
    with pytest.raises(ValueError): model.reconcile(plan['plan_id'],plan['fingerprint'],observe)
    assert len(calls)==1


def test_returned_plan_cannot_replace_held_snapshot(tmp_path):
    model,path,clock,row,calls,observe=setup(tmp_path)
    plan=model.plan(str(path),[{'parameter':0}],observe)
    plan['observations'][0]['automation_state']=1
    plan['targets'][0]['parameter']=99
    assert model.reconcile(plan['plan_id'],plan['fingerprint'],observe)['comparison']=='unchanged'
    assert calls[-1]=={'parameter':0}


def test_saved_targets_and_unbounded_requests_refuse(tmp_path):
    model,path,clock,row,calls,observe=setup(tmp_path)
    for targets in ([],[{'saved_path':str(path)}],[{'parameter':0}]*65):
        with pytest.raises(ValueError): model.plan(str(path),targets,observe)
    assert calls==[]


def test_service_only_dispatches_state_reads(tmp_path):
    from ableton_mcp.automation_reload import AutomationReload
    from ableton_mcp.services import AbletonService
    from golden_runner import AllCapabilitiesHandshake
    path=tmp_path/'copy.xml'; path.write_bytes(b'<Ableton/>'); sent=[]
    class Client:
        def send_command(self,name,params):
            assert name=='get_parameter_automation'
            sent.append(params)
            return dict(supported=True,source='live',coverage='state_only',automation_state=1,
                        overridden=False,envelope_present=True)
    service=AbletonService(Client(),AllCapabilitiesHandshake(),automation_reload=AutomationReload())
    plan=service.plan_automation_reload(str(path),[{'parameter':0}])
    assert service.reconcile_automation_reload(plan['plan_id'],plan['fingerprint'])['comparison']=='unchanged'
    assert len(sent)==2 and path.read_bytes()==b'<Ableton/>'


@pytest.mark.parametrize('change',['file','time'])
def test_change_during_fresh_read_refuses(tmp_path,change):
    model,path,clock,row,calls,observe=setup(tmp_path)
    plan=model.plan(str(path),[{'parameter':0}],observe)
    def delayed(target):
        if change=='file': path.write_bytes(b'changed')
        else: clock[0]+=301
        return observe(target)
    with pytest.raises(ValueError): model.reconcile(plan['plan_id'],plan['fingerprint'],delayed)
