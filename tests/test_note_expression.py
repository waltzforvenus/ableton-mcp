"""Extended creation and in-place expression, isolated fake dependencies."""
from copy import deepcopy
from types import SimpleNamespace
import sys
import pytest
from test_note_edits import editable


def test_expression_selection_preserves_every_other_note():
    h,c = editable(); before = deepcopy(c._notes)
    assert hasattr(h.mcp, '_set_notes_probability'), 'missing expression edit'
    r = h.mcp._set_notes_probability(0,0,.25,-12,note_ids=[4],from_time=0)
    before[3].update(probability=.25, velocity_deviation=-12)
    assert c._notes == before
    assert r['verified_changed'] == 1


@pytest.mark.parametrize('kw', [{'probability': float('nan')}, {'velocity_deviation': float('inf')},
    {'probability': .2, 'note_ids': []}, {'probability': .2, 'note_ids': [1,1]},
    {'probability': .2, 'note_ids': [999]}, {'probability': .2,'note_ids':[True]}, {},
    {'probability': .2,'pitch':128}, {'probability': .2,'time_span':-1}])
def test_invalid_expression_selection_no_write(kw):
    h,c = editable(); before = deepcopy(c._notes)
    assert hasattr(h.mcp, '_set_notes_probability'), 'missing expression edit'
    with pytest.raises(ValueError): h.mcp._set_notes_probability(**kw)
    assert c._notes == before


@pytest.mark.parametrize('p,d,wantp,wantd', [(-1,-200,0,-127),(2,200,1,127)])
def test_expression_clamps(p,d,wantp,wantd):
    h,c=editable()
    assert hasattr(h.mcp, '_set_notes_probability'), 'missing expression edit'
    h.mcp._set_notes_probability(probability=p,velocity_deviation=d,note_ids=[4])
    assert c._notes[3]['probability'] == wantp
    assert c._notes[3]['velocity_deviation'] == wantd


def test_advanced_creation_and_readback(monkeypatch):
    h,c = editable()
    monkeypatch.setitem(sys.modules, 'Live', SimpleNamespace(Clip=SimpleNamespace(MidiNoteSpecification=lambda **kw: SimpleNamespace(**kw))))
    def add(notes):
        for n in notes:
            c._store_note(vars(n)); c._notes[-1].update(vars(n))
    c.add_new_notes = add
    r=h.mcp._add_notes_to_clip(notes=[{'pitch':72,'start_time':2,'duration':1,'probability':.25,'velocity_deviation':-12}],expect_count=1)
    assert c._notes[-1]['probability'] == .25
    assert c._notes[-1]['velocity_deviation'] == -12
    assert r['added'] == 1
    assert r['expression_verified'] is True


@pytest.mark.parametrize('note', [{'probability':1}, {'velocity_deviation':0}, {'probability':float('nan')}])
def test_unsupported_explicit_advanced_creation_refuses_even_defaults(note):
    h,c = editable(); before = deepcopy(c._notes)
    with pytest.raises(ValueError): h.mcp._add_notes_to_clip(notes=[note])
    assert c._notes == before


def test_arrangement_expression_and_name_guard():
    h,c=editable()
    h.song.tracks[0].arrangement_clips.append(c)
    before=deepcopy(c._notes)
    with pytest.raises(ValueError):
        h.mcp._set_notes_probability(probability=.3, arrangement=True, expect_track_name='wrong')
    assert c._notes==before
    r=h.mcp._set_notes_probability(probability=.3, arrangement=True, note_ids=[4])
    assert r['verified_changed']==1


def test_advanced_creation_ignored_api_not_verified(monkeypatch):
    h,c=editable()
    monkeypatch.setitem(sys.modules,'Live',SimpleNamespace(Clip=SimpleNamespace(MidiNoteSpecification=lambda **kw:SimpleNamespace(**kw))))
    c.add_new_notes=lambda notes: None
    r=h.mcp._add_notes_to_clip(notes=[{'probability':.25}])
    assert r['expression_verified'] is False
    assert r['added']==0


@pytest.mark.parametrize('missing',['get_notes_extended','get_all_notes_extended','apply_note_modifications'])
def test_expression_missing_native_api_refuses_without_write(missing):
    h,c=editable(); delattr(c,missing); before=deepcopy(c._notes)
    with pytest.raises((ValueError,KeyError)):
        h.mcp._set_notes_probability(probability=.5,note_ids=[4])
    assert c._notes==before


def test_exact_half_open_selection_does_not_round_neighbor():
    h,c=editable()
    c._notes[3]['start_time']=1.0001
    c._notes[4]['start_time']=1.0002
    h.mcp._set_notes_probability(probability=.2,from_time=1.0001,time_span=.0001)
    assert c._notes[3]['probability']==.2
    assert c._notes[4]['probability']==1


def test_advanced_truncated_expect_count_refuses_before_add(monkeypatch):
    h,c=editable(); before=deepcopy(c._notes)
    monkeypatch.setitem(sys.modules,'Live',SimpleNamespace(Clip=SimpleNamespace(MidiNoteSpecification=lambda **kw:SimpleNamespace(**kw))))
    c.add_new_notes=lambda notes: pytest.fail('must not mutate')
    with pytest.raises(ValueError,match='expect_count'):
        h.mcp._add_notes_to_clip(notes=[{'probability':.5}],expect_count=2)
    assert c._notes==before
