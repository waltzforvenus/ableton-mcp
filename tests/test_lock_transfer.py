"""Local fixtures only: no production lock or native save operations."""
import json
import pytest
from ableton_mcp.live_lock import LiveLock, LockError

def test_acquisition_is_not_a_musical_edit_and_name_is_not_ownership(tmp_path):
    path=str(tmp_path/'lock'); first=LiveLock(path)
    record=first.acquire('owner')['lock']
    assert record['last_edit_ts'] is None and record['save_state']=='unknown'
    other=LiveLock(path)
    with pytest.raises(LockError): other.acquire('owner')
    with pytest.raises(LockError): other.release('owner',str(tmp_path/'song.als'))
    first.touch(); updated=first.read()
    assert updated['last_edit_ts'] is not None and updated['save_state']=='dirty'
    assert updated['generation']!=record['generation']

def test_resume_requires_configured_authority_and_generation(tmp_path):
    path=str(tmp_path/'lock'); first=LiveLock(path); record=first.acquire('owner')['lock']
    verifier=lambda action, context, proof: action=='resume' and proof=='trusted'
    resumed=LiveLock(path,authorization_verifier=verifier)
    with pytest.raises(LockError): resumed.resume('owner','forged',record['generation'])
    with pytest.raises(LockError): resumed.resume('owner','trusted','stale')
    result=resumed.resume('owner','trusted',record['generation'])
    assert result['resumed']
    resumed.check_write('set_tempo')
    with pytest.raises(LockError): first.check_write('set_tempo')

def test_transfer_requires_trusted_fresh_save_and_never_unlocks(tmp_path):
    path=str(tmp_path/'lock')
    def verify(action,context,proof):
        return action=='transfer' and proof=={'verified':True,'generation':context['lock']['generation'],'current_set':'song'}
    lock=LiveLock(path,authorization_verifier=verify); record=lock.acquire('from')['lock']
    for proof in ({}, {'verified':True}, {'verified':True,'generation':'stale','current_set':'song'}):
        with pytest.raises(LockError): lock.transfer('from','to',proof,record['generation'])
        assert lock.read()==record
    result=lock.transfer('from','to',{'verified':True,'generation':record['generation'],'current_set':'song'},record['generation'])
    assert result['transferred'] and lock.read()['holder']=='to'
    assert lock.read()['save_state']=='verified' and lock.read()['instance_id'] is None
    with pytest.raises(LockError): lock.check_write('set_tempo')
    with pytest.raises(LockError): LiveLock(path).acquire('to')

def test_no_verifier_transfer_unsupported_and_failed_replace_preserves_owner(tmp_path,monkeypatch):
    path=str(tmp_path/'lock'); lock=LiveLock(path); record=lock.acquire('from')['lock']
    with pytest.raises(LockError,match='unsupported'): lock.transfer('from','to',{},record['generation'])
    lock._authorization_verifier=lambda *args:True
    def fail(*args): raise OSError('disk failure')
    monkeypatch.setattr('ableton_mcp.live_lock.os.replace',fail)
    with pytest.raises(OSError): lock.transfer('from','to',{},record['generation'])
    assert lock.read()==record
    lock.check_write('set_tempo')

def test_legacy_and_nonobject_records_never_become_free(tmp_path):
    path=tmp_path/'lock'; path.write_text(json.dumps({'holder':'owner','last_edit_ts':5}))
    lock=LiveLock(str(path))
    with pytest.raises(LockError): lock.acquire('owner')
    path.write_text('[]')
    with pytest.raises(LockError): lock.acquire('new')

def test_unknown_save_evidence_never_releases(tmp_path):
    path=str(tmp_path/'lock'); lock=LiveLock(path); lock.acquire('owner')
    saved=tmp_path/'song.als'; saved.write_bytes(b'saved')
    with pytest.raises(LockError,match='save_evidence_unknown'):
        lock.release('owner',str(saved))
    assert lock.read()['holder']=='owner'

def test_active_job_guard_blocks_transfer_and_resume(tmp_path):
    from contextlib import contextmanager
    from ableton_mcp.services import AbletonService
    class Jobs:
        @contextmanager
        def idle_guard(self):
            raise RuntimeError('active or reconciliation export job')
            yield
    lock=LiveLock(str(tmp_path/'lock'),authorization_verifier=lambda *args:True)
    record=lock.acquire('owner')['lock']
    service=AbletonService(None,None,lock=lock,render_jobs=Jobs())
    with pytest.raises(RuntimeError,match='export job'):
        service.transfer_live_lock('owner','next',{},record['generation'])
    with pytest.raises(RuntimeError,match='export job'):
        service.resume_live_lock('owner',{},record['generation'])
    assert lock.read()==record

def test_successful_service_handoff_does_not_send_native_commands(tmp_path):
    from ableton_mcp.services import AbletonService
    lock=LiveLock(str(tmp_path/'lock'),authorization_verifier=lambda *args:True)
    record=lock.acquire('owner')['lock']
    class NoNative:
        def send_command(self,*args): raise AssertionError('native write')
    service=AbletonService(NoNative(),None,lock=lock)
    assert service.transfer_live_lock('owner','next',{},record['generation'])['transferred']

def test_similar_set_name_is_not_current_set_proof(tmp_path):
    lock=LiveLock(str(tmp_path/'lock')); lock.acquire('owner')
    saved=tmp_path/'song.als'; saved.write_bytes(b'saved')
    with pytest.raises(LockError,match='live_unsaved'):
        lock.release('owner',str(saved),['other-song - Ableton Live'])
    assert lock.read()['holder']=='owner'

def test_competing_transfer_has_one_winner_and_no_unlocked_gap(tmp_path):
    import threading
    lock=LiveLock(str(tmp_path/'lock'),authorization_verifier=lambda *args:True)
    record=lock.acquire('owner')['lock']; barrier=threading.Barrier(2); results=[]
    def transfer(target):
        barrier.wait()
        try: results.append(lock.transfer('owner',target,{},record['generation'])['transferred'])
        except LockError: results.append(False)
    workers=[threading.Thread(target=transfer,args=(name,)) for name in ('a','b')]
    for worker in workers: worker.start()
    for worker in workers: worker.join(1); assert not worker.is_alive()
    assert sorted(results)==[False,True] and lock.read()['holder'] in ('a','b')
