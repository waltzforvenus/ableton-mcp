"""Lossless edit authority; fake LOM, no sockets or Live."""
from copy import deepcopy
import pytest
from test_note_completeness import fixture, read


def editable():
    h, c = fixture()
    c.groove = object()
    for n in c._notes:
        n['pressure'] = .3
    def apply(notes):
        for note in notes:
            for stored in c._notes:
                if stored['note_id'] == note.note_id:
                    stored.update(vars(note))
    c.apply_note_modifications = apply
    return h, c


def plan(h, snapshot, edits):
    assert hasattr(h.mcp, '_plan_note_edits'), 'missing trusted note planner'
    return h.mcp._plan_note_edits(snapshot=snapshot, edits=edits)


def test_duration_patch_preserves_hidden_notes_expression_and_groove():
    h, c = editable()
    before, groove = deepcopy(c._notes), c.groove
    p = plan(h, read(h), [{'note_id': 1, 'duration': 2.0}])
    result = h.mcp._apply_note_edits(plan=p)
    before[0]['duration'] = 2.0
    assert c._notes == before
    assert c.groove is groove
    assert result['verified_changed'] == 1


@pytest.mark.parametrize('bad', ['invalid', 1.5, True, None, -1])
def test_malformed_native_ids_never_authorize_edits(bad):
    h, c = editable()
    c._notes[0]['note_id'] = bad
    with pytest.raises(ValueError, match='note.*ID|note_id'):
        read(h)


def test_duplicate_native_ids_refused():
    h, c = editable()
    c._notes[1]['note_id'] = 1
    with pytest.raises(ValueError, match='ID|note_id'):
        read(h)


@pytest.mark.parametrize('change', ['note', 'clip', 'token', 'fingerprint'])
def test_stale_or_forged_authority_zero_mutation(change):
    h, c = editable()
    s = read(h)
    if change == 'token': s['read_token'] = 'forged'
    if change == 'fingerprint': s['fingerprint'] = 'forged'
    if change in ('token', 'fingerprint'):
        with pytest.raises(ValueError): plan(h, s, [{'note_id': 1, 'duration': 2}])
        return
    p = plan(h, s, [{'note_id': 1, 'duration': 2}])
    if change == 'note': c._notes[-1]['velocity'] = 2
    else: h.song.tracks[0].clip_slots[0].clip = fixture()[1]
    before = deepcopy(c._notes)
    with pytest.raises(ValueError): h.mcp._apply_note_edits(plan=p)
    assert c._notes == before


@pytest.mark.parametrize('edits', [[{'note_id': True, 'duration': 1}],
    [{'note_id': 1.5, 'duration': 1}], [{'note_id': 999, 'duration': 1}],
    [{'note_id': 1, 'duration': -1}], [{'note_id': 1, 'duration': float('nan')}],
    [{'note_id': 1, 'pressure': .9}], [{'note_id': 1, 'duration': 1}]*2, []])
def test_invalid_patch_zero_mutation(edits):
    h, c = editable(); before = deepcopy(c._notes)
    with pytest.raises(ValueError): plan(h, read(h), edits)
    assert c._notes == before


@pytest.mark.parametrize('complete', [True, False])
def test_replacement_never_clears_unsupported_lossless_state(complete):
    h, c = editable()
    if not complete: del c.get_all_notes_extended
    s = read(h); s['completeness'] = 'complete'
    before = deepcopy(c._notes)
    with pytest.raises(ValueError): plan(h, s, [{'operation': 'replace', 'notes': []}])
    assert c._notes == before


def test_missing_apply_refuses_before_modifying_native_objects():
    h, c = editable(); del c.apply_note_modifications
    before = deepcopy(c._notes)
    with pytest.raises(ValueError): plan(h, read(h), [{'note_id': 1, 'duration': 2}])
    assert c._notes == before


def test_ignored_apply_reports_unverified():
    h, c = editable(); c.apply_note_modifications = lambda notes: None
    p = plan(h, read(h), [{'note_id': 1, 'duration': 2}])
    r = h.mcp._apply_note_edits(plan=p)
    assert r['verified_changed'] == 0
    assert r['verified'] is False


def test_returned_plan_cannot_mutate_server_held_patch():
    h,c=editable()
    p=plan(h,read(h),[{'note_id':1,'duration':2}])
    p['patches'][0]['duration']=99
    h.mcp._apply_note_edits(p)
    assert c._notes[0]['duration']==2


def test_verified_changed_counts_changes_not_already_matching_values():
    h,c=editable()
    p=plan(h,read(h),[{'note_id':1,'duration':.5}])
    r=h.mcp._apply_note_edits(p)
    assert r['verified'] is True
    assert r['verified_changed']==0


def test_apply_failure_reports_reconciliation_without_replay():
    h,c=editable()
    def fail(notes):
        c._notes[0]['duration']=2
        raise RuntimeError('native partial write')
    c.apply_note_modifications=fail
    p=plan(h,read(h),[{'note_id':1,'duration':2}])
    r=h.mcp._apply_note_edits(p)
    assert r['reconciliation_required'] is True
    assert r['verified'] is False
    with pytest.raises(ValueError): h.mcp._apply_note_edits(p)


def test_unrelated_native_write_change_fails_preservation_verification():
    h,c=editable(); apply=c.apply_note_modifications
    def corrupt(notes):
        apply(notes); c._notes[-1]['velocity']=1
    c.apply_note_modifications=corrupt
    p=plan(h,read(h),[{'note_id':1,'duration':2}])
    r=h.mcp._apply_note_edits(p)
    assert r['verified'] is False
    assert r['reconciliation_required'] is True


def test_groove_change_between_read_and_apply_refuses():
    h,c=editable()
    p=plan(h,read(h),[{'note_id':1,'duration':2}])
    c.groove=object()
    with pytest.raises(ValueError): h.mcp._apply_note_edits(p)
    assert c._notes[0]['duration']==.5
