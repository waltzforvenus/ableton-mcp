"""A1b fake UI and decoder verification; no native operations."""
import pytest
from ableton_mcp import audio_metrics, render
from test_render import FakeExportUI, _export

@pytest.mark.parametrize('field,value,reason', [
 ('filename',None,'save_panel_name'), ('filename','wrong.wav','save_panel_name'),
 ('directory',None,'save_panel_destination'), ('directory','/wrong','save_panel_destination')])
def test_save_readback_required(tmp_path, field, value, reason):
    class UI(FakeExportUI):
        def save_panel(self, action, **kw):
            result = super().save_panel(action, **kw)
            if action == 'fill':
                result['directory'] = kw['directory']
                result[field] = value
            return result
    ui = UI(tmp_path)
    with pytest.raises(render.RenderRefused) as exc: _export(tmp_path, ui)
    assert exc.value.reason == reason
    assert not any(c[:2] == ('panel','save') for c in ui.calls)
    assert not list(tmp_path.rglob('*.wav'))

@pytest.mark.parametrize('changes', [{'bit_depth':None}, {'bit_depth':16},
 {'sample_rate':48000}, {'duration_s':2.02}, {'duration_s':float('nan')}, {'decoded':False}])
def test_artifact_unknown_or_wrong_never_verified(monkeypatch, changes):
    metrics = dict(decoded_duration_s=2.0,duration_s=2.0,sample_rate=44100,bit_depth=24,
                   engine=['ffprobe','ffmpeg'],warnings=[],codec='pcm_s24le',decoded=True)
    metrics.update(changes)
    if 'duration_s' in changes: metrics['decoded_duration_s'] = changes['duration_s']
    monkeypatch.setattr(audio_metrics,'analyze',lambda *a,**kw: metrics)
    result = audio_metrics.verify('fake.wav',expected_duration_s=2,sample_rate=44100,bit_depth=24)
    assert result['ok'] is False

def test_preparation_deadline_prevents_late_export(tmp_path):
    class UI(FakeExportUI):
        def read_window(self,title):
            result = super().read_window(title)
            self._t += 100
            return result
    ui = UI(tmp_path)
    with pytest.raises(render.RenderRefused) as exc: _export(tmp_path,ui,timeout_s=50)
    assert exc.value.reason == 'stage_timeout'
    assert not any(c[:2] == ('act','Export') for c in ui.calls)

def test_stale_overwrite_not_completion(tmp_path):
    class UI(FakeExportUI):
        def save_panel(self,action,**kw):
            if action == 'save': return {'ok':True}
            result = super().save_panel(action,**kw)
            if action == 'fill': result['directory'] = kw['directory']
            return result
    from test_render import _wav
    folder = tmp_path/'renders'
    folder.mkdir()
    _wav(folder/'mix.wav')
    result = _export(tmp_path,UI(tmp_path),overwrite=True,timeout_s=120)
    assert result['ok'] is False


def test_missing_mp3_is_not_success(tmp_path):
    result = _export(tmp_path, FakeExportUI(tmp_path), mp3=True, timeout_s=120)
    assert result['ok'] is False


def test_truncated_decoder_output_fails(tmp_path):
    from test_render import _wav
    path = tmp_path/'truncated.wav'
    _wav(path)
    path.write_bytes(path.read_bytes()[:2000])
    result = audio_metrics.verify(str(path), expected_duration_s=2,sample_rate=44100,bit_depth=24)
    assert result['ok'] is False


@pytest.mark.parametrize('seconds', [1,61])
def test_short_and_long_generated_audio_verified(tmp_path, seconds):
    from test_render import _wav
    path = tmp_path/'generated.wav'
    _wav(path,seconds=seconds,rate=8000)
    result = audio_metrics.verify(str(path),expected_duration_s=seconds,sample_rate=8000,bit_depth=24)
    assert result['ok'] is True, result
    assert result['decoded_duration_s'] == pytest.approx(seconds,abs=.01)


def test_lossy_depth_is_not_applicable(monkeypatch):
    monkeypatch.setattr(audio_metrics,'analyze',lambda *a,**k: dict(
        decoded=True,decoded_duration_s=2,duration_s=2,codec='mp3',sample_rate=44100,
        bit_depth=32, warnings=[]))
    result = audio_metrics.verify('fake.mp3',expected_duration_s=2,sample_rate=44100,bit_depth=24)
    assert result['ok'] is True
    assert result['bit_depth_check'] == 'not_applicable'


def test_ui_budget_caps_runner_and_stops_expired_call():
    from ableton_mcp.ui_macos import MacUI
    now = [0.0]
    calls = []
    def runner(args, timeout):
        calls.append(timeout)
        return 0, '{}', ''
    ui = MacUI(runner=runner,platform='darwin')
    with ui.operation_budget(3,lambda: now[0]):
        ui.read_window('Export')
        now[0] = 4
        with pytest.raises(TimeoutError): ui.read_window('Export')
    assert calls == [3]
    ui.read_window('Export')
    assert calls[-1] == 30


def test_decoder_budget_caps_all_subprocesses(tmp_path,monkeypatch):
    path = tmp_path/'audio.wav'
    path.write_bytes(b'fixture')
    monkeypatch.setattr(audio_metrics,'find_tool',lambda name,override=None: name)
    now = [0.0]
    calls = []
    def runner(args,timeout):
        calls.append(timeout)
        now[0] = 10
        return 0, '{"streams":[{"sample_rate":"44100"}],"format":{"duration":"2"}}', ''
    with pytest.raises(TimeoutError):
        audio_metrics.verify(str(path),runner=runner,timeout_s=5,clock=lambda:now[0])
    assert calls == [5]


def test_unknown_destination_adapter_cannot_echo_requested_path():
    from ableton_mcp.ui_macos import MacUI
    ui = MacUI(runner=lambda a,t:(0,'{"ok":true,"filename":"mix.wav","directory_url":"file:///tmp/a%20b/"}',''),platform='darwin')
    result = ui.save_panel('fill',directory='/tmp/wrong',filename='mix.wav')
    assert result['directory'] == '/tmp/a b/'


def _save_script(alerts, overwrite=True):
    """Execute actual JXA orchestration as JS with fake AX objects, not osascript."""
    import json, shutil, subprocess
    from ableton_mcp.ui_macos import _JXA_SAVE_PANEL
    node = shutil.which('node')
    if not node: pytest.skip('node required for isolated JXA fake')
    source = _JXA_SAVE_PANEL % {'proc':json.dumps('Live'),'spec':json.dumps(dict(
        action='save',overwrite=overwrite,filenames=['mix.wav','mix.mp3']))}
    harness = r'''
const input = JSON.parse(require('fs').readFileSync(0,'utf8'));
let active = false; const clicks = []; const texts = input.alerts.slice();
const sheet = () => ({
  entireContents: () => [{role:()=> 'AXStaticText',value:()=>texts[0]}],
  buttons: {whose: ({name}) => ['Replace','Cancel'].includes(name) ? [{click:()=> {clicks.push(name+':'+texts.shift());}}] : []}
});
const sheets = () => active && texts.length ? [sheet()] : [];
const win = {textFields:[{}],sheets,subrole:()=> 'AXDialog',name:()=> 'Save',
 buttons:{whose:({name})=> name==='Save'?[{click:()=>{active=true;clicks.push('Save')}}]:[]}};
const p={windows:()=>[win]};
global.Application=()=>({processes:{whose:()=>[p]}}); global.delay=()=>{};
const result=eval(input.source); console.log(JSON.stringify({result:JSON.parse(result),clicks}));
'''
    proc = subprocess.run([node,'-e',harness],input=json.dumps(dict(source=source,alerts=alerts)),text=True,capture_output=True,timeout=5,check=True)
    return json.loads(proc.stdout)


def test_two_matching_overwrite_sheets_are_distinct():
    result = _save_script(['“mix.wav” already exists. Replace?', '“mix.mp3” already exists. Replace?'])
    assert result['result']['ok'] is True
    assert len(result['clicks']) == 3
    assert result['result']['replaced_files'] == ['mix.wav','mix.mp3']


@pytest.mark.parametrize('overwrite',[False,True])
def test_unrelated_replace_sheet_never_dismissed(overwrite):
    result = _save_script(['“other.wav” already exists. Replace?'],overwrite)
    assert result['result']['ok'] is False
    assert result['clicks'] == ['Save']


def test_engine_off_refuses_before_ui_mutation(tmp_path):
    class UI(FakeExportUI):
        def export_readiness(self):
            return {'supported':True,'engine_enabled':False,'output_device':'fixture','sample_rate':44100}
    ui = UI(tmp_path)
    with pytest.raises(render.RenderRefused) as exc: _export(tmp_path,ui)
    assert exc.value.reason == 'audio_not_ready'
    assert not ui.calls


def test_unknown_readiness_not_reported_as_ready(tmp_path):
    result = _export(tmp_path,FakeExportUI(tmp_path))
    assert result['preflight']['audio_readiness']['engine_enabled'] is None
    assert any('readiness unknown' in w for w in result['warnings'])


def test_device_rate_mismatch_warning(tmp_path):
    class UI(FakeExportUI):
        def export_readiness(self):
            return {'supported':True,'engine_enabled':True,'output_device':'fixture','sample_rate':48000}
    result = _export(tmp_path,UI(tmp_path))
    assert any('48000' in w and '44100' in w for w in result['warnings'])


def test_post_admission_timeout_never_presented_as_nothing_rendered(tmp_path):
    from ableton_mcp import presenters
    class UI(FakeExportUI):
        def save_panel(self,action,**kw):
            if action == 'save': raise TimeoutError('uncertain native save')
            return super().save_panel(action,**kw)
    with pytest.raises(render.RenderRefused) as exc: _export(tmp_path,UI(tmp_path))
    assert exc.value.details['outcome'] == 'needs_reconciliation'
    message = presenters.render_refusal(exc.value)
    assert 'nothing rendered' not in message
    assert 'reconciliation' in message.lower()


@pytest.mark.parametrize('seconds',[1,61])
def test_short_and_long_complete_fake_export(tmp_path,seconds):
    ui = FakeExportUI(tmp_path,seconds=seconds)
    result = _export(tmp_path,ui,end_beat=seconds*2,sample_rate=8000)
    assert result['ok'] is True, result
    assert result['files'][0]['decoded_duration_s'] == pytest.approx(seconds,abs=.01)
    assert result['save_readback']['filename'] == 'mix.wav'


def test_unreadable_final_ui_cannot_prove_completion(tmp_path):
    class UI(FakeExportUI):
        def live_windows(self):
            if self.panel: return None
            return super().live_windows()
    result = _export(tmp_path,UI(tmp_path),timeout_s=120)
    assert result['ok'] is False


def test_existing_mp3_refused_before_opening_export(tmp_path):
    folder = tmp_path/'renders'
    folder.mkdir()
    (folder/'mix.mp3').write_bytes(b'original')
    ui = FakeExportUI(tmp_path)
    with pytest.raises(render.RenderRefused) as exc: _export(tmp_path,ui,mp3=True)
    assert exc.value.reason == 'exists'
    assert not any(c[0]=='menu' for c in ui.calls)
    assert (folder/'mix.mp3').read_bytes() == b'original'


def test_invalid_audio_is_never_success(tmp_path):
    path = tmp_path/'invalid.wav'
    path.write_bytes(b'not audio')
    with pytest.raises(ValueError): audio_metrics.verify(str(path),expected_duration_s=2)
