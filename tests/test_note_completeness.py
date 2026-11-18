"""Offline source enumeration tests; fake LOM only, no sockets or Live."""
import pytest
from fake_ableton import FakeClip, FakeLiveConfig, make_harness
from fake_ableton.lom import FakeNote


def fixture(all_notes=True):
    h = make_harness()
    c = FakeClip("Offset pattern", 8, config=FakeLiveConfig(extended_note_fields=True, all_notes_api=all_notes),
                 notes=[(60, i - 2, .5, 100, False) for i in range(49)] +
                       [(64, 10000, 1, 80, False)])
    c.loop_start, c.loop_end = 16, 24
    c.start_marker, c.end_marker = 16, 24
    c.looping = True
    h.song.tracks[0].clip_slots[0].clip = c
    return h, c


def read(h, **kw):
    return h.mcp._get_clip_notes(0, 0, **kw)


def test_all_notes_include_negative_trimmed_and_far_end_source():
    h, c = fixture()
    r = read(h)
    assert len(r["notes"]) == 50
    assert r["completeness"] == "complete"
    assert r["time_domain"] == "clip_source_beats"
    assert r["source_extent"] == {"start": -2.0, "end": 10001.0}
    assert [n["start_time"] for n in r["audible_notes"]] == list(range(16, 24))
    assert r["start_marker"] == r["loop_start"] == 16
    assert r["end_marker"] == r["loop_end"] == 24
    assert r["arrangement_start"] is None
    assert r["next_cursor"] is None
    assert r["notes"][0]["release_velocity"] == 64
    assert isinstance(h.mcp._notes_from_clip(c), list)


def test_legacy_reports_unknown_extent_even_when_empty():
    h, c = fixture(False)
    r = read(h)
    assert r["completeness"] == "partial"
    assert r["source_extent"] is None
    assert r["whole_clip_replace_supported"] is False
    c._notes = []
    assert read(h)["completeness"] != "complete"


def test_paging_union_is_lossless_and_ids_unique():
    h, _ = fixture()
    r = read(h, limit=7)
    notes = list(r["notes"])
    assert r["completeness"] == "partial"
    while r["next_cursor"]:
        r = read(h, limit=7, cursor=r["next_cursor"])
        notes.extend(r["notes"])
    assert len(notes) == len({n["note_id"] for n in notes}) == 50
    assert sorted(n["start_time"] for n in notes) == list(range(-2, 47)) + [10000]


@pytest.mark.parametrize("change", ["note", "clip", "markers"])
def test_stale_cursor_refuses_change(change):
    h, c = fixture()
    token = read(h, limit=3)["next_cursor"]
    if change == "note":
        c._notes[-1]["velocity"] = 11
    elif change == "markers":
        c.loop_end = 25
    else:
        h.song.tracks[0].clip_slots[0].clip = FakeClip("Offset pattern", 8)
    with pytest.raises(ValueError, match="cursor"):
        read(h, limit=3, cursor=token)


def test_source_selection_keeps_source_time_and_discloses_partial():
    h, _ = fixture()
    r = read(h, source_start=-2, source_end=0)
    assert [n["start_time"] for n in r["notes"]] == [-2, -1]
    assert r["completeness"] == "partial"


@pytest.mark.parametrize("kw", [{"limit":0}, {"limit":10001}, {"limit":1.5},
    {"source_start":float("nan")}, {"source_start":2,"source_end":1},
    {"cursor":"invented"}])
def test_invalid_read_arguments_refused(kw):
    h, _ = fixture()
    with pytest.raises(ValueError):
        read(h, **kw)


def test_legacy_explicit_offset_range_is_useful_but_never_complete():
    h, _ = fixture(False)
    r = read(h, source_start=16, source_end=24)
    assert [n["start_time"] for n in r["notes"]] == list(range(16, 24))
    assert r["source_completeness"] == "unknown"


@pytest.mark.parametrize("source_end", [-0.5, 0.0])
@pytest.mark.parametrize("legacy_only", [False, True])
def test_legacy_implicit_lower_bound_refuses_nonpositive_span(source_end, legacy_only):
    h, c = fixture(False)
    calls = []
    def extended(*args):
        calls.append(args)
        return ()
    def legacy(*args):
        calls.append(args)
        return ()
    c.get_notes = legacy
    if legacy_only:
        del c.get_notes_extended
    else:
        c.get_notes_extended = extended
    with pytest.raises(ValueError, match="source_start"):
        read(h, source_end=source_end)
    assert calls == []  # Reject before either API, not a swallowed API failure.


@pytest.mark.parametrize("legacy_only", [False, True])
def test_legacy_explicit_negative_range_preserves_notes_and_unknown_coverage(legacy_only):
    h, c = fixture(False)
    if legacy_only:
        del c.get_notes_extended
    r = read(h, source_start=-2, source_end=-0.5)
    assert [n["start_time"] for n in r["notes"]] == [-2, -1]
    assert r["source_completeness"] == "unknown"
    assert r["completeness"] == "partial"


def test_all_api_failure_does_not_fabricate_complete_empty_read():
    h, c = fixture()
    def failed():
        raise RuntimeError("API unavailable")
    c.get_all_notes_extended = failed
    with pytest.raises(RuntimeError):
        read(h)


def test_malformed_all_api_does_not_fallback_and_claim_complete():
    h, c = fixture()
    c.get_all_notes_extended = lambda: (object(),)
    with pytest.raises((ValueError, AttributeError)):
        read(h)


def test_arrangement_metadata_not_source_rebasing():
    h, c = fixture()
    c.start_time, c.end_time = 64, 72
    h.song.tracks[0].arrangement_clips = [c]
    r = read(h, arrangement=True)
    assert (r["arrangement_start"],r["arrangement_end"]) == (64,72)
    assert r["notes"][0]["start_time"] == -2
