"""A5 offline probes and fault injection, never a Live connection."""
import pytest
from fake_ableton import make_harness


def parameter_fixture(state=0):
    h = make_harness()
    p = h.song.tracks[0].devices[0].parameters[0]
    p.automation_state = state
    return h, p


@pytest.mark.parametrize('state,present,override', [(0,False,False),(1,True,False),(2,True,True),(9,None,None),(None,None,None)])
def test_live_automation_visibility_truthful(state,present,override):
    h,p = parameter_fixture(state)
    result = h.mcp._get_parameter_automation(target={'parameter':0})
    assert result['source'] == 'live'
    assert result['envelope_present'] is present and result['overridden'] is override
    assert result['supported'] is (present is not None)
    assert result['coverage'] == 'state_only' and result['points'] is None


@pytest.mark.parametrize('state', [1,2,None,9])
def test_automated_or_unknown_parameter_write_requires_intent(state):
    h,p = parameter_fixture(state); old = p.value
    with pytest.raises(ValueError, match='automation'):
        h.mcp._set_device_parameter(parameter=0,value=.4)
    assert p.value == old


def test_explicit_override_is_not_envelope_deletion():
    h,p = parameter_fixture(1)
    result = h.mcp._set_device_parameter(parameter=0,value=.4,allow_automation_override=True)
    assert p.value == .4 and result['value'] == .4
    assert p.automation_state == 1  # fake doesn't model actual override transitions


def test_batch_preflights_automation_before_any_write():
    h,p = parameter_fixture(0); other = h.song.tracks[0].devices[0].parameters[1]
    other.automation_state=1; old = p.value
    with pytest.raises(ValueError, match='automation'):
        h.mcp._set_device_parameters(parameters={p.name:.4,other.name:.3})
    assert p.value == old


def test_unknown_state_cannot_be_overridden_by_guess():
    h,p = parameter_fixture(None)
    with pytest.raises(ValueError, match='automation'):
        h.mcp._set_device_parameter(parameter=0,value=.4,allow_automation_override=True)


@pytest.fixture
def saved_copy(tmp_path):
    p=tmp_path/'copy.als'
    p.write_text('''<Ableton><LiveSet><Tracks><MidiTrack Id="1"><AutomationEnvelopes><Envelopes>
<AutomationEnvelope Id="0"><EnvelopeTarget><PointeeId Value="10"/></EnvelopeTarget><Automation><Events><FloatEvent Time="0" Value="0.2"/><FloatEvent Time="16" Value="0.8"/></Events></Automation></AutomationEnvelope>
<AutomationEnvelope Id="1"><EnvelopeTarget><PointeeId Value="20"/></EnvelopeTarget><Automation><Events><FloatEvent Time="0" Value="0.7"/></Events></Automation></AutomationEnvelope>
</Envelopes></AutomationEnvelopes></MidiTrack></Tracks></LiveSet></Ableton>''')
    return p


def test_saved_visibility_and_nonmutating_diff(saved_copy):
    from ableton_mcp.automation import Automation
    model=Automation(); target={'saved_path':str(saved_copy),'pointee_id':'10'}
    before=saved_copy.read_bytes(); result=model.read_saved(target)
    assert result['source']=='saved_copy' and result['overridden'] is None
    assert [p['time'] for p in result['points']] == [0,16]
    plan=model.plan(target,[{'time':8,'value':.5}], 'insert')
    assert plan['preview_points']==[{'time':0.,'value':.2},{'time':8.,'value':.5},{'time':16.,'value':.8}]
    assert plan['unrelated_envelope_hashes'] and plan['supported'] is False
    assert plan['reload_risk']=='may_reenable_overrides'
    assert saved_copy.read_bytes()==before
    assert model.apply(plan['manifest_id'],plan['fingerprint'])['applied'] is False


def test_saved_plan_staleness_and_explicit_deletion(saved_copy):
    from ableton_mcp.automation import Automation
    model=Automation(); target={'saved_path':str(saved_copy),'pointee_id':'10'}
    with pytest.raises(ValueError): model.plan(target,[], 'insert')
    plan=model.plan(target,[], 'delete')
    assert plan['preview_points']==[] and plan['operation']=='delete'
    saved_copy.write_text(saved_copy.read_text().replace('0.7','0.6'))
    with pytest.raises(ValueError, match='stale'): model.apply(plan['manifest_id'],plan['fingerprint'])


def test_xml_decompression_budget(tmp_path):
    import gzip
    from ableton_mcp.plugin_state import _read_xml, PluginStateError
    p=tmp_path/'large.als'; p.write_bytes(gzip.compress(b'<Ableton>'+b' '*1000+b'</Ableton>'))
    with pytest.raises(PluginStateError, match='budget'):
        _read_xml(str(p), max_bytes=100)


@pytest.mark.parametrize('mutation', ['curve','duplicate','order'])
def test_saved_unknown_or_ambiguous_schema_refuses(saved_copy,mutation):
    from ableton_mcp.automation import Automation
    s=saved_copy.read_text()
    if mutation=='curve': s=s.replace('Time="16"','CurveControl1X="0.5" Time="16"')
    if mutation=='duplicate': s=s.replace('Value="20"','Value="10"')
    if mutation=='order': s=s.replace('Time="16"','Time="0"')
    saved_copy.write_text(s)
    with pytest.raises(ValueError):
        Automation().read_saved({'saved_path':str(saved_copy),'pointee_id':'10'})


def test_service_saved_plan_and_refusal_never_checkpoint_or_dispatch(saved_copy):
    from ableton_mcp.automation import Automation
    from ableton_mcp.services import AbletonService
    class Forbidden:
        def __getattr__(self, name): raise AssertionError('unexpected native/save call')
    service=AbletonService(Forbidden(),Forbidden(),automation=Automation())
    service.checkpoint_set=Forbidden()
    target={'saved_path':str(saved_copy),'pointee_id':'10'}
    assert service.get_parameter_automation(target, {'start':8,'end':32})['points']==[{'time':16.,'value':.8}]
    plan=service.plan_automation_edit(target,[{'time':8,'value':.5}],'insert')
    assert service.apply_automation_edit(plan['manifest_id'],plan['fingerprint'])['applied'] is False
    assert service.plan_automation_edit({'parameter':0},[], 'delete')['supported'] is False


@pytest.mark.parametrize('intent', ['true',1,None])
def test_native_override_intent_must_be_boolean(intent):
    h,p=parameter_fixture(1); old=p.value
    with pytest.raises(ValueError,match='boolean'):
        h.mcp._set_device_parameter(parameter=0,value=.4,allow_automation_override=intent)
    assert p.value==old


def test_live_state_read_failure_is_unknown():
    h,p=parameter_fixture(0)
    del p.automation_state
    assert h.mcp._get_parameter_automation(target={'parameter':0})['supported'] is False
    with pytest.raises(ValueError,match='automation'):
        h.mcp._set_device_parameter(parameter=0,value=.4)


def test_nested_event_schema_refused_without_changes(saved_copy):
    from ableton_mcp.automation import Automation
    saved_copy.write_text(saved_copy.read_text().replace('<FloatEvent Time="0" Value="0.2"/>','<FloatEvent Time="0" Value="0.2"><UnknownCurve/></FloatEvent>'))
    before=saved_copy.read_bytes()
    with pytest.raises(ValueError):
        Automation().plan({'saved_path':str(saved_copy),'pointee_id':'10'},[{'time':8,'value':.5}],'insert')
    assert saved_copy.read_bytes()==before


@pytest.mark.parametrize('kind',['regular','return','master','send'])
@pytest.mark.parametrize('state',[0,1,2,None])
@pytest.mark.parametrize('intent',[False,True])
def test_mixer_guard(kind,state,intent):
    h=make_harness()
    track=h.song.tracks[0] if kind in ('regular','send') else (h.song.return_tracks[0] if kind=='return' else h.song.master_track)
    param=track.mixer_device.sends[0] if kind=='send' else track.mixer_device.volume
    param.automation_state=state; old=param.value
    def call():
        if kind=='send': return h.mcp._set_track_send(value=.4,allow_automation_override=intent)
        return h.mcp._set_track_volume(value=.4,track_type=kind,allow_automation_override=intent)
    if state is None or (state in (1,2) and not intent):
        with pytest.raises(ValueError,match='automation'): call()
        assert param.value==old
    else:
        call(); assert param.value==.4


@pytest.mark.parametrize('state',[0,1,2,None])
@pytest.mark.parametrize('intent',[False,True])
def test_chain_all_parameter_guards_precede_any_field_write(state,intent):
    from fake_ableton import FakeRackDevice,FakeChain
    h=make_harness(); chain=FakeChain('original'); rack=FakeRackDevice(chains=[chain])
    h.song.tracks[0].devices=[rack]
    chain.mixer_device.panning.automation_state=state
    old=chain.mixer_device.volume.value
    def call(): return h.mcp._set_chain_mixer(volume=.4,panning=.3,name='changed',mute=True,allow_automation_override=intent)
    if state is None or (state in (1,2) and not intent):
        with pytest.raises(ValueError,match='automation'): call()
        assert chain.mixer_device.volume.value==old and chain.name=='original' and not chain.mute
    else:
        call(); assert chain.mixer_device.volume.value==.4 and chain.name=='changed'


@pytest.mark.parametrize('name',['set_device_parameter','set_device_parameters','set_track_volume','set_track_pan','set_track_send','set_chain_mixer'])
@pytest.mark.parametrize('invalid',['false','true',1,None])
def test_override_annotation_rejects_nonboolean_json(name,invalid):
    import inspect
    from pydantic import TypeAdapter,ValidationError
    from ableton_mcp import tools
    annotation=inspect.signature(getattr(tools,name)).parameters['allow_automation_override'].annotation
    with pytest.raises(ValidationError): TypeAdapter(annotation).validate_python(invalid)


@pytest.mark.parametrize('kind',['regular','return','master'])
@pytest.mark.parametrize('field',['volume','panning'])
@pytest.mark.parametrize('state',[0,1,2,None])
def test_public_mixer_automation_state_read(kind,field,state):
    h=make_harness()
    track=h.song.tracks[0] if kind=='regular' else (h.song.return_tracks[0] if kind=='return' else h.song.master_track)
    param=getattr(track.mixer_device,field); param.automation_state=state; old=param.value
    result=h.mcp._get_parameter_automation(target={'kind':'mixer','track_type':kind,'parameter':field})
    assert result['automation_state']==state and result['supported'] is (state is not None)
    assert result['coverage']=='state_only' and result['points'] is None and param.value==old


@pytest.mark.parametrize('state',[0,1,2,None])
def test_public_send_and_chain_state_read(state):
    from fake_ableton import FakeRackDevice,FakeChain
    h=make_harness(); chain=FakeChain('A'); h.song.tracks[0].devices=[FakeRackDevice(chains=[chain])]
    h.song.tracks[0].mixer_device.sends[0].automation_state=state
    chain.mixer_device.panning.automation_state=state
    for target in ({'kind':'send','send_index':0}, {'kind':'chain','chain_index':0,'parameter':'panning'}):
        result=h.mcp._get_parameter_automation(target=target)
        assert result['automation_state']==state and result['overridden'] is (None if state is None else state==2)


@pytest.mark.parametrize('target',[
    {'kind':'unknown'}, {'kind':'mixer','parameter':'mute'},
    {'kind':'mixer','parameter':'volume','device_index':0},
    {'kind':'send','send_index':True}, {'kind':'send','send_index':-1},
    {'kind':'send','send_index':999}, {'kind':'send','send_index':0,'parameter':0},
    {'kind':'chain','chain_index':-1,'parameter':'volume'},
    {'kind':'device','parameter':0,'track_index':True},
])
def test_typed_automation_target_rejects_ambiguity(target):
    h=make_harness()
    with pytest.raises((ValueError,IndexError)): h.mcp._get_parameter_automation(target=target)


def test_explicit_device_target_retains_existing_read():
    h,p=parameter_fixture(2)
    assert h.mcp._get_parameter_automation(target={'kind':'device','parameter':0})==h.mcp._get_parameter_automation(target={'parameter':0})
