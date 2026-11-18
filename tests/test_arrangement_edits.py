"""Manifest guards use injected adapters and temporary source files only."""
import pytest
from copy import deepcopy

def test_manifest_binds_source_media_and_refuses_stale_before_checkpoint(tmp_path):
    from ableton_mcp.arrangement_edits import ArrangementEdits
    media=tmp_path/'sample.wav'; media.write_bytes(b'original')
    model=ArrangementEdits(); calls=[]
    native={'supported':True,'plan_id':'native','fingerprint':'state','source_media':[str(media)]}
    manifest=model.register(native,{'allow_ui_automation':False})
    media.write_bytes(b'changed')
    with pytest.raises(ValueError,match='media'):
        model.apply(manifest['manifest_id'],manifest['fingerprint'],lambda *a:calls.append('save'),lambda *a:calls.append('apply'))
    assert calls==[]


def test_native_region_copy_empty_span_is_additive_and_preserves_source():
    from fake_ableton import make_harness, FakeClip
    h=make_harness(); track=h.song.tracks[0]
    source=FakeClip('original',4,midi=True,start_time=0); track.arrangement_clips=[source]
    source.get_all_notes_extended=source._all_notes_extended
    source._config.extended_note_fields=True
    def clone(clip,start):
        new=deepcopy(clip); new.start_time=start; new.end_time=start+4
        track.arrangement_clips.append(new)
    track.duplicate_clip_to_arrangement=clone
    assert hasattr(h.mcp,'_plan_arrangement_region')
    plan=h.mcp._plan_arrangement_region(request={'track_index':0,'source_start':0,'source_end':4,'destination_start':4,'operation':'copy'})
    assert plan['supported'] and plan['predicted_cropped_clips']==[]
    result=h.mcp._apply_arrangement_region(plan_id=plan['plan_id'],fingerprint=plan['fingerprint'])
    assert result['applied'] and track.arrangement_clips[0] is source
    assert source.start_time==0 and source.end_time==4 and len(track.arrangement_clips)==2


def region_fixture():
    from fake_ableton import make_harness, FakeClip
    h=make_harness(); track=h.song.tracks[0]
    source=FakeClip('original',4,midi=True,start_time=8)
    source.get_all_notes_extended=source._all_notes_extended
    source._config.extended_note_fields=True
    source.start_marker=2; source.loop_start=2; source.loop_end=6
    track.arrangement_clips=[source]
    calls=[]
    def clone(clip,start):
        calls.append(start); new=deepcopy(clip); new.start_time=start; new.end_time=start+4
        track.arrangement_clips.append(new)
    track.duplicate_clip_to_arrangement=clone
    return h,track,source,calls

@pytest.mark.parametrize('destination,supported', [(4,True),(12,True),(6,False),(10,False),(8,False)])
def test_region_half_open_collision_and_offsets(destination,supported):
    h,track,source,calls=region_fixture()
    plan=h.mcp._plan_arrangement_region(request={'source_start':8,'source_end':12,'destination_start':destination})
    assert plan['supported'] is supported
    result=h.mcp._apply_arrangement_region(plan_id=plan['plan_id'],fingerprint=plan['fingerprint'])
    assert result['applied'] is supported
    assert source.start_time==8 and source.start_marker==2
    if supported: assert track.arrangement_clips[-1].start_marker==2
    else: assert calls==[]

@pytest.mark.parametrize('change', [{'source_start':9},{'operation':'replace'},{'fade_policy':'crossfade'},{'atomic':True}])
def test_unsupported_region_never_mutates(change):
    h,track,source,calls=region_fixture()
    request=dict(source_start=8,source_end=12,destination_start=16); request.update(change)
    plan=h.mcp._plan_arrangement_region(request=request)
    assert not plan['supported'] and plan['missing_capabilities']
    assert not h.mcp._apply_arrangement_region(plan_id=plan['plan_id'],fingerprint=plan['fingerprint'])['applied']
    assert calls==[] and track.arrangement_clips==[source]

@pytest.mark.parametrize('change', ['offset','identity','notes','topology'])
def test_stale_region_plan_zero_writes(change):
    h,track,source,calls=region_fixture()
    plan=h.mcp._plan_arrangement_region(request=dict(source_start=8,source_end=12,destination_start=16))
    if change=='offset': source.start_marker=3
    if change=='identity': track.arrangement_clips=[deepcopy(source)]
    if change=='notes': source._store_note({'pitch':60,'start_time':1,'duration':1,'velocity':100,'mute':False})
    if change=='topology': track.arrangement_clips.append(deepcopy(source))
    with pytest.raises(ValueError,match='stale'):
        h.mcp._apply_arrangement_region(plan_id=plan['plan_id'],fingerprint=plan['fingerprint'])
    assert calls==[]


def test_partial_native_copy_never_replayed_or_undone():
    h,track,source,calls=region_fixture(); original=track.duplicate_clip_to_arrangement
    def partial(*args): original(*args); raise RuntimeError('after clone')
    track.duplicate_clip_to_arrangement=partial
    plan=h.mcp._plan_arrangement_region(request=dict(source_start=8,source_end=12,destination_start=16))
    result=h.mcp._apply_arrangement_region(plan_id=plan['plan_id'],fingerprint=plan['fingerprint'])
    assert result['applied'] is None and result['reconciliation_required']
    assert len(track.arrangement_clips)==2 and track.arrangement_clips[0] is source
    with pytest.raises(ValueError): h.mcp._apply_arrangement_region(plan_id=plan['plan_id'],fingerprint=plan['fingerprint'])
    assert calls==[16]


def test_checkpoint_failure_prevents_apply_and_success_preserves_media(tmp_path):
    from ableton_mcp.arrangement_edits import ArrangementEdits,media_hash
    model=ArrangementEdits(); path=tmp_path/'source'; path.write_bytes(b'immutable')
    checkpoint=tmp_path/'backup.als'; checkpoint.write_bytes(b'saved set')
    native={'supported':True,'plan_id':'native','fingerprint':'state','source_media':[str(path)]}
    calls=[]; manifest=model.register(native,{})
    result=model.apply(manifest['manifest_id'],manifest['fingerprint'],lambda r:{'checkpointed':False},lambda n:calls.append(n))
    assert result['applied'] is False and calls==[]
    manifest=model.register(native,{})
    result=model.apply(manifest['manifest_id'],manifest['fingerprint'],lambda r:{'checkpointed':True,'checkpoint_path':str(checkpoint),'sha256':media_hash(str(checkpoint)), 'save':{'saved':True,'verified':{'set_path':str(checkpoint),'document_before':str(checkpoint),'document_after':str(checkpoint),'unsaved_after':False,'mtime_advanced':True}}},lambda n:{'applied':True})
    assert result['applied'] is True and path.read_bytes()==b'immutable'


def test_native_clone_retains_notes_when_live_assigns_new_ids():
    h,track,source,calls=region_fixture()
    source._store_note((60,1,1,100,False))
    original=track.duplicate_clip_to_arrangement
    def clone(*args):
        original(*args)
        track.arrangement_clips[-1]._notes[0]['note_id']=999
    track.duplicate_clip_to_arrangement=clone
    plan=h.mcp._plan_arrangement_region(request=dict(source_start=8,source_end=12,destination_start=16))
    result=h.mcp._apply_arrangement_region(plan_id=plan['plan_id'],fingerprint=plan['fingerprint'])
    assert result['applied'] is True
    assert source._notes[0]['note_id']!=999


def test_audio_preview_refuses_unverifiable_fades_without_media_writes(tmp_path):
    from fake_ableton import FakeClip
    from ableton_mcp.arrangement_edits import ArrangementEdits
    h,track,source,calls=region_fixture()
    audio=FakeClip('sample',4,midi=False,start_time=8)
    media=tmp_path/'sample.wav'; media.write_bytes(b'unchanged')
    audio.file_path=str(media); audio.start_marker=1; audio.gain=.25
    track.arrangement_clips=[audio]
    native=h.mcp._plan_arrangement_region(request=dict(source_start=8,source_end=12,destination_start=16))
    manifest=ArrangementEdits().register(native,{})
    assert not manifest['supported'] and 'fade' in str(manifest['missing_capabilities'])
    result=h.mcp._apply_arrangement_region(plan_id=native['plan_id'],fingerprint=native['fingerprint'])
    assert result['applied'] is False and calls==[] and media.read_bytes()==b'unchanged'


def test_service_composes_checkpoint_and_native_apply(tmp_path):
    from ableton_mcp.arrangement_edits import ArrangementEdits, media_hash
    from ableton_mcp.services import AbletonService
    from golden_runner import AllCapabilitiesHandshake
    h,track,source,calls=region_fixture(); sent=[]
    class Client:
        def send_command(self,name,params):
            sent.append(name)
            return getattr(h.mcp,'_'+name)(**params)
    service=AbletonService(Client(),AllCapabilitiesHandshake(),arrangement_edits=ArrangementEdits())
    plan=service.plan_arrangement_region(dict(source_start=8,source_end=12,destination_start=16))
    assert plan['source_state'][0]['start_marker']==2
    backup=tmp_path/'copy.als'; backup.write_bytes(b'saved')
    def checkpoint(*args,**kwargs):
        sent.append('checkpoint')
        return dict(checkpointed=True,checkpoint_path=str(backup),sha256=media_hash(str(backup)),save={'saved':True,'verified':{'set_path':str(backup),'document_before':str(backup),'document_after':str(backup),'unsaved_after':False,'mtime_advanced':True}})
    service.checkpoint_set=checkpoint
    result=service.apply_arrangement_region(plan['manifest_id'],plan['fingerprint'])
    assert result['applied'] is True and sent==['plan_arrangement_region','checkpoint','apply_arrangement_region']
    assert track.arrangement_clips[0] is source
