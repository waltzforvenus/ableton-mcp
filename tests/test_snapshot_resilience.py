"""The Song handle is re-resolved per access, and one failing snapshot
section no longer costs the whole dump (backlog R4); the transport-error
hint (R5).

The real failure — Boost.Python's "None.None(Song) did not match C++
signature" after opening another set — can only be reproduced in Live; here
the fake raises the same TypeError from chosen members so both halves of the
fix are pinned. Runs anywhere — no Ableton, no network.
"""

from fake_ableton import FakeSong, make_harness
from ableton_mcp import presenters

BOOST_ERROR = ("Python argument types in None.None(Song) did not match C++ "
               "signature: None(TPyHandle<ASong>)")


class _Raising(object):
    """A LOM sequence/attribute that raises the stale-handle error when read."""

    def __iter__(self):
        raise TypeError(BOOST_ERROR)

    def __len__(self):
        raise TypeError(BOOST_ERROR)


def _snapshot(harness, **flags):
    params = {"include_notes": False, "include_params": False}
    params.update(flags)
    resp = harness.process({"type": "get_session_snapshot", "params": params})
    assert resp["status"] == "success", resp
    return resp["result"]


def test_song_is_resolved_on_every_access():
    harness = make_harness()
    first = harness.song
    assert harness.mcp._song is first  # the harness fallback, song() is None
    other = FakeSong(harness.config)
    other.tempo = 87.0
    # Live's ControlSurface.song() answers for whichever set is open now.
    harness.mcp.song = lambda: other
    assert harness.mcp._song is other
    resp = harness.process({"type": "get_session_info", "params": {}})
    assert resp["result"]["tempo"] == 87.0


def test_a_failing_track_is_isolated_and_named():
    harness = make_harness()
    harness.song.tracks[1].devices = _Raising()
    result = _snapshot(harness)
    errors = result["section_errors"]
    assert errors == [{"section": "tracks[1]", "error": "TypeError: " + BOOST_ERROR}]
    by_index = {t["index"]: t for t in result["tracks"]}
    assert by_index[1] == {"index": 1, "name": harness.song.tracks[1].name,
                           "error": True}
    assert "devices" in by_index[0]  # the other tracks are whole


def test_a_failing_top_level_section_keeps_the_rest():
    harness = make_harness()
    # get_session_info counts the returns; make that read raise.
    harness.song.return_tracks = _Raising()
    result = _snapshot(harness)
    sections = [e["section"] for e in result["section_errors"]]
    assert sections[0] == "session" and result["session"] == {}
    assert result["tracks"] and all("error" not in t for t in result["tracks"])


def test_a_clean_snapshot_has_no_section_errors_key():
    assert "section_errors" not in _snapshot(make_harness())


def test_unreachable_errors_carry_the_status_hint():
    text = presenters.error_text(
        "set_tempo",
        Exception("Could not connect to Ableton. Make sure the Remote Script is running."))
    assert text.endswith(presenters.UNREACHABLE_HINT)
    assert "get_live_status" in text
    plain = presenters.error_text("set_tempo", Exception("Track index out of range"))
    assert presenters.UNREACHABLE_HINT not in plain
