"""Synthetic schema qualification only; not a Live-version or reload fixture."""
import pytest
from ableton_mcp.automation import Automation


@pytest.fixture
def source(tmp_path):
    path=tmp_path/'source.xml'
    path.write_bytes(b'''<?xml version="1.0" encoding="UTF-8"?>
<Ableton><Unknown data="untouched"><![CDATA[opaque & <bytes>]]></Unknown><!--keep-->
<AutomationEnvelope Id="0"><EnvelopeTarget><PointeeId Value="10"/></EnvelopeTarget><Automation><Events>
<FloatEvent Id="0" Time="0" Value="0.2"/>
<FloatEvent Id="1" Time="16" Value="0.8"/>
</Events></Automation></AutomationEnvelope>
<AutomationEnvelope Id="1"><EnvelopeTarget><PointeeId Value="20"/></EnvelopeTarget><Automation><Events><UnknownEvent value="opaque"/></Events></Automation></AutomationEnvelope>
</Ableton>''')
    return path


def model_and_target(source):
    from ableton_mcp.automation_writer import SavedAutomationWriter
    backup=source.with_name('backup.xml'); backup.write_bytes(source.read_bytes())
    return Automation(writer=SavedAutomationWriter()), dict(saved_path=str(source),pointee_id='10',
        output_path=str(source.with_name('output.xml')),backup_path=str(backup),schema='leaf-float-values-v1')


POINTS=[{'time':0,'value':.4},{'time':16,'value':.6}]


def test_new_copy_preserves_all_unrelated_bytes(source):
    model,target=model_and_target(source); before=source.read_bytes()
    plan=model.plan(target,POINTS,'replace')
    assert plan['supported'] is True and plan['apply_mode']=='saved_copy_only'
    result=model.apply(plan['manifest_id'],plan['fingerprint'])
    assert result['applied'] is True and result['live_applied'] is False
    from pathlib import Path
    assert Path(target['output_path']).read_bytes()==before.replace(b'Value="0.2"',b'Value="0.4"').replace(b'Value="0.8"',b'Value="0.6"')
    assert source.read_bytes()==before and Path(target['backup_path']).read_bytes()==before
    with pytest.raises(ValueError): model.apply(plan['manifest_id'],plan['fingerprint'])


@pytest.mark.parametrize('change',['source','backup','destination','same_source','same_backup'])
def test_stale_or_existing_destination_zero_writes(source,change):
    from pathlib import Path
    model,target=model_and_target(source)
    if change=='same_source': target['output_path']=str(source)
    if change=='same_backup': target['output_path']=target['backup_path']
    if change.startswith('same_'):
        with pytest.raises(ValueError): model.plan(target,POINTS,'replace')
        return
    plan=model.plan(target,POINTS,'replace')
    path=source if change=='source' else Path(target['backup_path'] if change=='backup' else target['output_path'])
    path.write_bytes(b'changed')
    with pytest.raises(ValueError): model.apply(plan['manifest_id'],plan['fingerprint'])
    assert path.read_bytes()==b'changed'
    if change!='destination': assert not Path(target['output_path']).exists()


@pytest.mark.parametrize('change',['nested','curve','times','insert','schema','gzip','doctype'])
def test_unqualified_write_never_creates_output(source,change):
    import gzip
    from pathlib import Path
    model,target=model_and_target(source); points=POINTS; operation='replace'
    data=source.read_bytes()
    if change=='nested': data=data.replace(b'Value="0.2"/>',b'Value="0.2"><Curve/></FloatEvent>')
    if change=='curve': data=data.replace(b'Value="0.2"',b'Value="0.2" Curve="unknown"')
    if change=='times': points=[{'time':1,'value':.4},{'time':16,'value':.6}]
    if change=='insert': operation='insert'; points=[{'time':8,'value':.5}]
    if change=='schema': target['schema']='unknown'
    if change=='gzip': data=gzip.compress(data)
    if change=='doctype': data=data.replace(b'<Ableton>',b'<!DOCTYPE Ableton><Ableton>')
    source.write_bytes(data); Path(target['backup_path']).write_bytes(data)
    with pytest.raises(ValueError): model.plan(target,points,operation)
    assert not Path(target['output_path']).exists() and source.read_bytes()==data


def test_unrelated_unknown_float_curve_bytes_preserved(source):
    source.write_bytes(source.read_bytes().replace(b'<UnknownEvent value="opaque"/>',b'<FloatEvent Time="0" Value="1" Curve="unknown"><Opaque/></FloatEvent>'))
    model,target=model_and_target(source); before=source.read_bytes()
    plan=model.plan(target,POINTS,'replace')
    assert model.apply(plan['manifest_id'],plan['fingerprint'])['applied'] is True
    from pathlib import Path
    assert b'<FloatEvent Time="0" Value="1" Curve="unknown"><Opaque/></FloatEvent>' in Path(target['output_path']).read_bytes()
    assert source.read_bytes()==before


def test_partial_publication_retained_without_replay(source):
    from ableton_mcp.automation_writer import SavedAutomationWriter
    from pathlib import Path
    model,target=model_and_target(source)
    class FailAfterWrite(SavedAutomationWriter):
        def publish(self,path,data,destination):
            super().publish(path,data,destination)
            raise OSError('simulated acknowledgement loss')
    model._writer=FailAfterWrite()
    before=source.read_bytes(); plan=model.plan(target,POINTS,'replace')
    result=model.apply(plan['manifest_id'],plan['fingerprint'])
    assert result['applied'] is None and result['reconciliation_required'] and not result['retry_safe']
    assert Path(target['output_path']).exists() and source.read_bytes()==before
    with pytest.raises(ValueError): model.apply(plan['manifest_id'],plan['fingerprint'])


def test_symlink_destination_cannot_replace_original(source):
    from pathlib import Path
    model,target=model_and_target(source)
    plan=model.plan(target,POINTS,'replace'); before=source.read_bytes()
    Path(target['output_path']).symlink_to(source)
    with pytest.raises(ValueError): model.apply(plan['manifest_id'],plan['fingerprint'])
    assert source.read_bytes()==before


def test_backup_same_inode_is_not_independent_copy(source):
    import os
    from pathlib import Path
    model,target=model_and_target(source)
    Path(target['backup_path']).unlink(); os.link(source,target['backup_path'])
    with pytest.raises(ValueError): model.plan(target,POINTS,'replace')


@pytest.mark.parametrize('replacement',['symlink','directory'])
def test_output_parent_retarget_before_apply_refuses(source,replacement):
    model,target=model_and_target(source)
    root=source.parent; approved=root/'approved'; approved.mkdir(); other=root/'other'; other.mkdir()
    parent=root/'destination'
    if replacement=='symlink': parent.symlink_to(approved, target_is_directory=True)
    else: parent.mkdir()
    target['output_path']=str(parent/'out.xml')
    plan=model.plan(target,POINTS,'replace')
    if replacement=='symlink': parent.unlink(); parent.symlink_to(other,target_is_directory=True)
    else: parent.rename(root/'old'); parent.mkdir()
    with pytest.raises(ValueError): model.apply(plan['manifest_id'],plan['fingerprint'])
    assert not (parent/'out.xml').exists() and not (approved/'out.xml').exists() and not (other/'out.xml').exists()


def test_parent_retarget_at_publication_never_redirects(source,monkeypatch):
    import os
    model,target=model_and_target(source)
    root=source.parent; approved=root/'approved'; approved.mkdir(); other=root/'other'; other.mkdir()
    parent=root/'destination'; parent.symlink_to(approved,target_is_directory=True)
    target['output_path']=str(parent/'out.xml'); plan=model.plan(target,POINTS,'replace')
    original_open=os.open
    def retarget_on_create(path,flags,*args,**kwargs):
        if flags & os.O_CREAT:
            parent.unlink(); parent.symlink_to(other,target_is_directory=True)
        return original_open(path,flags,*args,**kwargs)
    monkeypatch.setattr(os,'open',retarget_on_create)
    result=model.apply(plan['manifest_id'],plan['fingerprint'])
    assert not (other/'out.xml').exists()
    assert result['applied'] is None and result['reconciliation_required']
    with pytest.raises(ValueError): model.apply(plan['manifest_id'],plan['fingerprint'])


@pytest.mark.parametrize('retarget',[False,True])
def test_parent_identity_rechecked_when_publication_opens(source,retarget):
    from ableton_mcp.automation_writer import SavedAutomationWriter
    model,target=model_and_target(source)
    root=source.parent; approved=root/'approved'; approved.mkdir(); other=root/'other'; other.mkdir()
    parent=root/'destination'; parent.symlink_to(approved,target_is_directory=True)
    target['output_path']=str(parent/'out.xml')
    class BoundaryWriter(SavedAutomationWriter):
        def publish(self,path,data,destination):
            if retarget:
                parent.unlink(); parent.symlink_to(other,target_is_directory=True)
            return super().publish(path,data,destination)
    model._writer=BoundaryWriter()
    plan=model.plan(target,POINTS,'replace')
    result=model.apply(plan['manifest_id'],plan['fingerprint'])
    assert not (other/'out.xml').exists()
    if retarget:
        assert result['applied'] is None and not (approved/'out.xml').exists()
        assert result['destination_binding']['resolved_parent']==str(approved.resolve())
    else:
        assert result['applied'] is True and (approved/'out.xml').exists()
