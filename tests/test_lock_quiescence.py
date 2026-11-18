"""A7 review races: temporary files and event-controlled fake clients only."""
import os
import threading
import pytest
from ableton_mcp.live_lock import LiveLock, LockError
from ableton_mcp.services import AbletonService
from golden_runner import AllCapabilitiesHandshake

@pytest.mark.parametrize('action', ['transfer', 'resume', 'release'])
def test_admitted_write_blocks_all_ownership_changes(tmp_path, action):
    entered, finish = threading.Event(), threading.Event()
    lock = LiveLock(str(tmp_path/'lock'), authorization_verifier=lambda *a: True)
    original = lock.acquire('owner')['lock']
    applied = []
    class Client:
        def send_command(self, name, params):
            entered.set()
            assert finish.wait(2)
            applied.append(name)
            return {}
    service = AbletonService(Client(), AllCapabilitiesHandshake(), lock=lock)
    worker = threading.Thread(target=lambda: service.set_tempo(90))
    worker.start()
    try:
        assert entered.wait(1)
        current = lock.read()
        assert current['generation'] != original['generation']
        assert current['save_state'] != 'verified'
        with pytest.raises(LockError):
            if action == 'transfer':
                service.transfer_live_lock('owner','next',{},current['generation'])
            elif action == 'resume':
                LiveLock(lock.path,authorization_verifier=lambda *a:True).resume('owner',{},current['generation'])
            else:
                saved=tmp_path/'song.als'; saved.write_bytes(b'file')
                lock.release('owner',str(saved),['song - Ableton Live'])
        assert lock.read()['instance_id'] == original['instance_id']
    finally:
        finish.set(); worker.join(2)
    assert not worker.is_alive() and applied == ['set_tempo']
    assert lock.read()['save_state'] == 'dirty'


def test_unknown_old_same_name_file_never_releases(tmp_path):
    lock=LiveLock(str(tmp_path/'lock'),clock=lambda:1000)
    lock.acquire('owner')
    path=tmp_path/'song.als'; path.write_bytes(b'old alternate set')
    os.utime(path,(1,1))
    with pytest.raises(LockError): lock.release('owner',str(path),['song - Ableton Live'])
    assert lock.read()['save_state']=='unknown'


def test_timeout_retains_reservation_across_instances(tmp_path):
    lock=LiveLock(str(tmp_path/'lock'),authorization_verifier=lambda *a:True)
    lock.acquire('owner')
    class Client:
        def send_command(self,*a): raise TimeoutError('unknown native outcome')
    service=AbletonService(Client(),AllCapabilitiesHandshake(),lock=lock)
    with pytest.raises(TimeoutError): service.set_tempo(90)
    record=lock.read()
    with pytest.raises(LockError): lock.transfer('owner','next',{},record['generation'])
    with pytest.raises(LockError):
        LiveLock(lock.path,authorization_verifier=lambda *a:True).resume('owner',{},record['generation'])
    assert lock.read()['holder']=='owner'

class DocumentUI:
    """Build only a fake MacUI with an actual AXDocument observation."""
    @staticmethod
    def make(path):
        from test_save_checkpoint import FakeUI
        from urllib.parse import quote
        class UI(FakeUI):
            def live_windows(self):
                result=super().live_windows()
                result['windows'][0]['document']='file://'+quote(self.set_path)
                return result
        return UI(set_path=path)

@pytest.mark.parametrize('case', ['fresh', 'dirty', 'api_only', 'wrong_path', 'no_identity'])
def test_release_requires_generation_bound_current_document_save(tmp_path, case):
    from test_save_checkpoint import FakeUI, _service
    path=tmp_path/'song.als'; path.write_bytes(b'saved'); os.utime(path,(1000,1000))
    lock=LiveLock(str(tmp_path/'lock'),clock=lambda:1005)
    lock.acquire('owner')
    ui=FakeUI(set_path=str(path)) if case=='no_identity' else DocumentUI.make(str(path))
    service,_=_service(ui,saved=case=='api_only',lock=lock)
    result=service.save_set(allow_ui_automation=True)
    assert result['saved']
    if case=='dirty': service.set_tempo(90)
    release_path=str(path)
    if case=='wrong_path':
        alternate=tmp_path/'alternate'; alternate.mkdir()
        other=alternate/'song.als'; other.write_bytes(b'other'); os.utime(other,(1010,1010))
        release_path=str(other)
    if case=='fresh':
        assert service.release_live_lock('owner',release_path)['released']
    else:
        with pytest.raises(LockError): service.release_live_lock('owner',release_path)
        assert lock.read()['holder']=='owner'


def test_ui_save_blocks_handoff_and_new_edit_invalidates_observation(tmp_path):
    from test_save_checkpoint import _service
    path=tmp_path/'song.als'; path.write_bytes(b'saved'); os.utime(path,(1000,1000))
    lock=LiveLock(str(tmp_path/'lock'),clock=lambda:1005,authorization_verifier=lambda *a:True)
    lock.acquire('owner')
    ui=DocumentUI.make(str(path)); entered, finish=threading.Event(),threading.Event()
    original=ui.click_menu_item
    def click(*args,**kwargs):
        entered.set(); assert finish.wait(2)
        return original(*args,**kwargs)
    ui.click_menu_item=click
    service,_=_service(ui,lock=lock); results=[]
    thread=threading.Thread(target=lambda:results.append(service.save_set(allow_ui_automation=True)))
    thread.start()
    try:
        assert entered.wait(1)
        generation=lock.read()['generation']
        with pytest.raises(LockError,match='operations_unresolved'):
            lock.transfer('owner','next',{},generation)
        with pytest.raises(LockError,match='operations_unresolved'):
            LiveLock(lock.path,authorization_verifier=lambda *a:True).resume('owner',{},generation)
    finally:
        finish.set(); thread.join(2)
    assert not thread.is_alive() and results[0]['saved']
    assert lock.read()['save_observation']['generation']==generation
    service.set_tempo(90)
    assert 'save_observation' not in lock.read()
    with pytest.raises(LockError,match='generation_conflict'):
        lock.transfer('owner','next',{},generation)
    with pytest.raises(LockError): service.release_live_lock('owner',str(path))


def test_structured_native_uncertainty_blocks_handoff_and_replay(tmp_path):
    lock=LiveLock(str(tmp_path/'lock'),authorization_verifier=lambda *a:True)
    lock.acquire('owner'); calls=[]
    class Client:
        def send_command(self,*args):
            calls.append(args)
            return {'reconciliation_required':True}
    service=AbletonService(Client(),AllCapabilitiesHandshake(),lock=lock)
    assert service.set_tempo(90)['reconciliation_required']
    with pytest.raises(LockError,match='operations_unresolved'): service.set_tempo(90)
    with pytest.raises(LockError,match='operations_unresolved'):
        lock.transfer('owner','next',{},lock.read()['generation'])
    assert len(calls)==1


def test_save_cannot_admit_during_pending_write(tmp_path):
    from test_save_checkpoint import _service
    path=tmp_path/'song.als'; path.write_bytes(b'saved'); os.utime(path,(1000,1000))
    lock=LiveLock(str(tmp_path/'lock'),clock=lambda:1005); lock.acquire('owner')
    ui=DocumentUI.make(str(path)); service,client=_service(ui,lock=lock)
    entered, finish = threading.Event(), threading.Event()
    def pending():
        with lock.operation('pending_native_write',edit=True):
            entered.set(); assert finish.wait(2)
    worker=threading.Thread(target=pending); worker.start()
    try:
        assert entered.wait(1)
        with pytest.raises(LockError,match='operations_unresolved'):
            service.save_set(allow_ui_automation=True)
    finally:
        finish.set(); worker.join(2)
    assert not worker.is_alive()
    assert client.sent==[] and 'save_observation' not in lock.read()


def test_write_cannot_admit_during_save(tmp_path):
    lock=LiveLock(str(tmp_path/'lock')); lock.acquire('owner')
    outcomes=[]
    class Client:
        def send_command(self,*args): outcomes.append('sent'); return {}
    service=AbletonService(Client(),AllCapabilitiesHandshake(),lock=lock)
    with lock.operation('save',exclusive=True):
        def write():
            try: service.set_tempo(90)
            except LockError as exc: outcomes.append(exc.reason)
        worker=threading.Thread(target=write); worker.start(); worker.join(1)
        assert not worker.is_alive()
    assert outcomes==['operations_unresolved']

@pytest.mark.parametrize('current', ['bare', 'modal', 'unreadable'])
def test_saved_observation_does_not_replace_current_clean_state(tmp_path,current):
    from test_save_checkpoint import _service
    path=tmp_path/'song.als'; path.write_bytes(b'saved'); os.utime(path,(1000,1000))
    lock=LiveLock(str(tmp_path/'lock'),clock=lambda:1005); lock.acquire('owner')
    ui=DocumentUI.make(str(path)); service,_=_service(ui,lock=lock)
    assert service.save_set(allow_ui_automation=True)['saved']
    original=ui.live_windows
    def changed():
        result=original()
        if current=='bare': result['windows'][0]['title']='song'
        if current=='modal': result['windows'].append({'title':'Preferences','subrole':'AXDialog'})
        if current=='unreadable': return None
        return result
    ui.live_windows=changed
    with pytest.raises(LockError): service.release_live_lock('owner',str(path))
    assert lock.read()['holder']=='owner'

@pytest.mark.parametrize('operation', ['bounce','export'])
@pytest.mark.parametrize('refusal', ['no_ui','no_consent','invalid'])
def test_local_render_refusal_does_not_poison_reservations(tmp_path,operation,refusal):
    from test_save_checkpoint import _service, FakeUI
    lock=LiveLock(str(tmp_path/'lock')); lock.acquire('owner'); before=lock.read()
    ui=None if refusal=='no_ui' else FakeUI()
    service,client=_service(ui,lock=lock)
    with pytest.raises(Exception):
        if operation=='bounce':
            service.bounce_track(0,'invalid' if refusal=='invalid' else 'freeze',allow_ui_automation=refusal=='invalid')
        else:
            service.export_audio(output_dir=str(tmp_path),file_stem='bad/name' if refusal=='invalid' else 'song',allow_ui_automation=refusal=='invalid')
    assert client.sent==[]
    assert lock.read()==before
    service.set_tempo(90)
    assert client.sent==['set_tempo']


def test_job_local_preflight_does_not_erase_prior_uncertainty(tmp_path):
    from test_save_checkpoint import _service, FakeUI
    lock=LiveLock(str(tmp_path/'lock')); lock.acquire('owner')
    service,client=_service(FakeUI(),lock=lock)
    before=lock.read()
    request={'operation':'bounce','options':{'track_index':0,'mode':'invalid','allow_ui_automation':True}}
    with pytest.raises(ValueError): service.run_export_job(request,lambda stage:None)
    assert lock.read()==before and client.sent==[]
    with pytest.raises(TimeoutError):
        with lock.operation('real_admitted_write',edit=True): raise TimeoutError('unknown')
    uncertain=lock.read()
    with pytest.raises(ValueError): service.run_export_job(request,lambda stage:None)
    assert lock.read()==uncertain
    with pytest.raises(LockError,match='operations_unresolved'): service.set_tempo(90)
