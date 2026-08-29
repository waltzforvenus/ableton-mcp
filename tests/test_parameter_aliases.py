"""
Server-side tests for the parameter-name aliases (docs/IMPROVEMENTS.md, "Build
first"): the three arguments callers reliably guess by a neighbouring tool's
name. ``destination_time`` was written as ``arrangement_time`` seven times
across four days of one production session, and every one of those was a
round-trip that did no work at all.

These live above the wire and below Live: no Ableton, no network. What they
pin is the part that has no Remote Script half to catch it —

- either spelling reaches the canonical wire parameter,
- two spellings with DIFFERENT values are refused before anything is sent
  (silently discarding half of what the caller said would be worse than the
  guessed name was),
- the wording of both refusals comes from presenters, per the layering rule.

Also covered here because it shares the shape: the ONE-position/RUN-of-
positions choice in duplicate_to_arrangement, where sending both keys would
leave the choice of which one wins to the Remote Script's argument order.
"""

from types import SimpleNamespace

import pytest

import ableton_mcp.tools as tools
from ableton_mcp.app import Deps
from ableton_mcp.services import AbletonService


class FakeConnection:
    """Records every command sent; answers with a canned result."""

    def __init__(self, response=None):
        self.response = {} if response is None else response
        self.sent = []  # (command_type, params)

    def send_command(self, command_type, params=None):
        self.sent.append((command_type, params or {}))
        return self.response


class PermissiveHandshake:
    """Every capability present, without touching the wire."""

    def require(self, name, min_version=None, *, send_command=None):
        return None


@pytest.fixture
def fake_conn():
    def _make(response=None):
        conn = FakeConnection(response=response)
        handshake = PermissiveHandshake()
        conn.ctx = SimpleNamespace(request_context=SimpleNamespace(
            lifespan_context=Deps(client=conn, handshake=handshake,
                                  service=AbletonService(conn, handshake))))
        return conn
    return _make


def _params(conn, command):
    return [sent for sent in conn.sent if sent[0] == command][0][1]


# --------------------------------------------------------------------------
# duplicate_to_arrangement: destination_time / arrangement_time / time
# --------------------------------------------------------------------------

STAMP = "duplicate_session_clip_to_arrangement"


@pytest.mark.parametrize("spelling", ["destination_time", "arrangement_time",
                                      "time"])
def test_every_spelling_of_destination_time_reaches_the_wire(fake_conn,
                                                             spelling):
    conn = fake_conn(response={"clip_name": "Hats", "track_name": "DRUMS"})
    tools.duplicate_to_arrangement(conn.ctx, track_index=1, clip_index=0,
                                   **{spelling: 8.0})
    assert _params(conn, STAMP)["destination_time"] == 8.0


def test_two_spellings_that_agree_are_accepted(fake_conn):
    conn = fake_conn(response={"clip_name": "Hats", "track_name": "DRUMS"})
    out = tools.duplicate_to_arrangement(conn.ctx, track_index=1, clip_index=0,
                                         destination_time=8.0,
                                         arrangement_time=8.0)
    assert not out.startswith("Error")
    assert _params(conn, STAMP)["destination_time"] == 8.0


def test_two_spellings_that_disagree_are_refused_before_the_wire(fake_conn):
    conn = fake_conn(response={"clip_name": "Hats"})
    out = tools.duplicate_to_arrangement(conn.ctx, track_index=1, clip_index=0,
                                         destination_time=8.0,
                                         arrangement_time=12.0)
    # Nothing was placed: the refusal happens in the controller, so the whole
    # exchange — gate included — never starts.
    assert conn.sent == []
    assert "destination_time" in out and "arrangement_time" in out
    assert "8.0" in out and "12.0" in out


def test_no_position_at_all_is_refused_with_both_spellings_named(fake_conn):
    conn = fake_conn()
    out = tools.duplicate_to_arrangement(conn.ctx, track_index=1, clip_index=0)
    assert conn.sent == []
    assert "destination_time" in out
    assert "arrangement_time" in out and "destination_times" in out


def test_a_run_of_positions_replaces_the_scalar_key(fake_conn):
    conn = fake_conn(response={
        "success": True, "clip_name": "Hats", "track_name": "DRUMS",
        "destination_times": [0.0, 4.0], "requested_count": 2,
        "placed_count": 2, "failed_count": 0,
        "placements": [{"destination_time": 0.0, "ok": True},
                       {"destination_time": 4.0, "ok": True}],
    })
    tools.duplicate_to_arrangement(conn.ctx, track_index=1, clip_index=0,
                                   destination_times=[0.0, 4.0])
    params = _params(conn, STAMP)
    assert params["destination_times"] == [0.0, 4.0]
    # Never both: the Remote Script branches on destination_times being
    # present, so a leftover scalar would look placed and be ignored.
    assert "destination_time" not in params


def test_one_position_and_a_run_together_are_refused(fake_conn):
    """The run form replaces the scalar on the wire, so honouring both is
    impossible — and dropping one silently loses a stamp the caller asked
    for."""
    conn = fake_conn()
    out = tools.duplicate_to_arrangement(conn.ctx, track_index=1, clip_index=0,
                                         destination_time=0.0,
                                         destination_times=[4.0, 8.0])
    assert conn.sent == []
    assert "destination_time" in out and "destination_times" in out


def test_two_ways_of_naming_a_clip_to_delete_are_refused(fake_conn):
    conn = fake_conn()
    out = tools.delete_arrangement_clip(conn.ctx, track_index=1, clip_index=2,
                                        start_time=64.0)
    assert conn.sent == []
    assert "start_time" in out and "clip_index" in out


@pytest.mark.parametrize("addressing, expected", [
    ({"start_times": [64.0, 96.0]}, {"start_times": [64.0, 96.0]}),
    ({"start_time": 64.0}, {"start_time": 64.0}),
    ({"clip_index": 3}, {"clip_index": 3}),
])
def test_each_way_of_naming_a_clip_reaches_the_wire_alone(fake_conn,
                                                          addressing,
                                                          expected):
    conn = fake_conn(response={"deleted_clip_name": "Take 3",
                               "deletions": None})
    tools.delete_arrangement_clip(conn.ctx, track_index=1, **addressing)
    params = _params(conn, "delete_arrangement_clip")
    assert params == dict({"track_index": 1}, **expected)


# --------------------------------------------------------------------------
# set_device_parameter: parameter / parameter_name
# --------------------------------------------------------------------------

def test_parameter_name_is_accepted_for_parameter(fake_conn):
    conn = fake_conn(response={"device_name": "Reverb",
                               "parameter_name": "Dry/Wet", "value": 0.25})
    tools.set_device_parameter(conn.ctx, track_index=0, device_index=0,
                               value=0.25, parameter_name="Dry/Wet")
    assert _params(conn, "set_device_parameter")["parameter"] == "Dry/Wet"


def test_a_digit_string_is_passed_through_not_coerced(fake_conn):
    """Both device-parameter tools must resolve a key the SAME way.

    This layer used to coerce "3" to the int 3, which sent the singular tool
    down the script's INDEX path while set_device_parameters sent the same key
    down its NAME path — the script tries the name first precisely so that a
    parameter genuinely named "3" stays reachable. On a device with such a
    parameter the two tools wrote to different targets. The string is now
    passed through so the script's one resolver decides for both.
    """
    conn = fake_conn(response={"device_name": "Reverb",
                               "parameter_name": "3", "value": 0.25})
    tools.set_device_parameter(conn.ctx, track_index=0, device_index=0,
                               value=0.25, parameter_name="3")
    assert _params(conn, "set_device_parameter")["parameter"] == "3"


def test_an_int_index_is_still_sent_as_an_int(fake_conn):
    """Passing a real int is unchanged — only the string coercion is gone."""
    conn = fake_conn(response={"device_name": "Reverb",
                               "parameter_name": "Dry/Wet", "value": 0.25})
    tools.set_device_parameter(conn.ctx, track_index=0, device_index=0,
                               value=0.25, parameter=3)
    assert _params(conn, "set_device_parameter")["parameter"] == 3


def test_conflicting_parameter_spellings_are_refused(fake_conn):
    conn = fake_conn()
    out = tools.set_device_parameter(conn.ctx, track_index=0, device_index=0,
                                     value=0.25, parameter="Dry/Wet",
                                     parameter_name="Decay Time")
    assert conn.sent == []
    assert "Dry/Wet" in out and "Decay Time" in out


def test_no_parameter_at_all_is_refused(fake_conn):
    conn = fake_conn()
    out = tools.set_device_parameter(conn.ctx, track_index=0, device_index=0,
                                     value=0.25)
    assert conn.sent == []
    assert "parameter" in out and "parameter_name" in out


# --------------------------------------------------------------------------
# create_audio_clip: path / file_path
# --------------------------------------------------------------------------

def test_file_path_is_accepted_for_path(fake_conn):
    conn = fake_conn(response={"name": "Stem", "length": 64.0,
                               "warping": False})
    tools.create_audio_clip(conn.ctx, track_index=2, clip_index=0,
                            file_path="/tmp/stem.wav")
    assert _params(conn, "create_audio_clip")["path"] == "/tmp/stem.wav"


def test_conflicting_path_spellings_are_refused(fake_conn):
    conn = fake_conn()
    out = tools.create_audio_clip(conn.ctx, track_index=2, clip_index=0,
                                  path="/tmp/a.wav", file_path="/tmp/b.wav")
    assert conn.sent == []
    assert "/tmp/a.wav" in out and "/tmp/b.wav" in out


def test_no_path_at_all_is_refused(fake_conn):
    conn = fake_conn()
    out = tools.create_audio_clip(conn.ctx, track_index=2, clip_index=0)
    assert conn.sent == []
    assert "path" in out and "file_path" in out


# --------------------------------------------------------------------------
# The refusals are the View's words, not a controller's
# --------------------------------------------------------------------------

def test_refusal_wording_comes_from_the_presenter(fake_conn):
    from ableton_mcp import presenters

    conn = fake_conn()
    out = tools.create_audio_clip(conn.ctx, track_index=2, clip_index=0,
                                  path="/tmp/a.wav", file_path="/tmp/b.wav")
    # The decorator wraps a controller refusal in the tool's own error
    # phrase, so the presenter's sentence is what sits inside it.
    assert presenters.alias_conflict("path", "/tmp/a.wav",
                                     "file_path", "/tmp/b.wav") in out
