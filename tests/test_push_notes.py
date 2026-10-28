"""
Tests for ``scripts/push_notes.py``, the direct-socket bulk note client.

The script is not part of the package (``scripts/`` has no ``__init__``), so
it is loaded from its path. The peer is a loopback TCP server that behaves
like the Remote Script's ``_handle_client`` — accumulate until the buffer
parses as JSON, dispatch, answer with one JSON blob — and like its handlers
for the commands the client sends: ``get_script_info``,
``clear_notes_from_clip``, ``add_notes_to_clip``, ``set_clip_name`` and
``set_arrangement_clip_name``, including the ``expect_count`` and
``expect_track_name`` refusals in the script's own wording.

What has to hold, in order of how badly it fails otherwise:

- every chunk carries ``expect_count`` equal to its own length, and the
  guard the caller gave reaches the wire on every command — a guard that is
  quietly dropped is a safety net that is not there;
- a guard the caller did NOT give is absent, not sent as null, because the
  script's dispatch calls handlers as ``**params``;
- nothing is sent until every note in every file has been validated;
- the run stops at the first refusal and says what landed.

Runs anywhere — no Ableton, no network beyond loopback.
"""

import importlib.util
import io
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "push_notes.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("push_notes", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: dataclasses resolve the module's
    # postponed annotations through sys.modules, and an unregistered module
    # fails there on Python 3.13.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


push_notes = _load_script()

KICK_TOMS = "drums — kick + toms"
HATS_SNARE = "drums — hats + snare + cymbals"


# --------------------------------------------------------------------------
# The peer: a loopback stand-in for the Remote Script's socket
# --------------------------------------------------------------------------

class FakeLive:
    """Behaves like the Remote Script's socket server and its note handlers.

    ``commands`` records every command dict received, in order, across all
    connections. The knobs model the replies real scripts give:
    ``added_shortfall`` reports fewer notes added than sent (the condition
    the client's count check exists for), ``added_unknown`` reports
    ``added: null`` (a clip the script could not count), ``legacy_echo``
    answers with the pre-1.15.0 ``{"note_count": n}`` shape, and
    ``refuse`` makes one specific call fail. ``reply_writer`` replaces the
    single ``sendall`` so a test can split, delay or truncate a reply.
    """

    def __init__(self, track_names=(KICK_TOMS, HATS_SNARE),
                 script_version="1.16.0"):
        self.track_names = list(track_names)
        self.script_version = script_version
        self.commands = []
        self.connections = 0
        self.clip_counts = {}
        self.clip_names = {}
        self.added_shortfall = 0
        self.added_unknown = False
        self.legacy_echo = False
        self.refuse = None            # (command_type, nth call of it) -> error
        self.refuse_script_info = False
        self.reply_writer = None      # fn(conn, payload_bytes)
        self.raw_utf8 = False         # the real script escapes non-ASCII
        self._closing = False
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self.port = self._listener.getsockname()[1]
        self._conns = []
        threading.Thread(target=self._accept_loop, daemon=True).start()

    # -- socket half ---------------------------------------------------------

    def _accept_loop(self):
        while not self._closing:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            self.connections += 1
            self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        buffer = b""
        while not self._closing:
            try:
                chunk = conn.recv(8192)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            try:
                command = json.loads(buffer.decode("utf-8"))
            except ValueError:
                continue
            buffer = b""
            self.commands.append(command)
            payload = json.dumps(self._process(command),
                                 ensure_ascii=not self.raw_utf8).encode("utf-8")
            try:
                if self.reply_writer is not None:
                    self.reply_writer(conn, payload)
                else:
                    conn.sendall(payload)
            except OSError:
                return

    def close(self):
        self._closing = True
        for sock in [self._listener] + self._conns:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    # -- handler half --------------------------------------------------------

    def _process(self, command):
        ctype = command.get("type")
        params = command.get("params") or {}
        nth = sum(1 for c in self.commands if c.get("type") == ctype)
        if self.refuse == (ctype, nth):
            return {"status": "error", "message": "simulated refusal"}
        try:
            if ctype == "get_script_info":
                if self.refuse_script_info:
                    raise ValueError("Unknown command: get_script_info")
                result = {"script_version": self.script_version,
                          "capabilities": ["add_notes_to_clip"]}
            elif ctype == "add_notes_to_clip":
                result = self._add_notes(**params)
            elif ctype == "clear_notes_from_clip":
                result = self._clear_notes(**params)
            elif ctype in ("set_clip_name", "set_arrangement_clip_name"):
                result = self._set_clip_name(**params)
            else:
                raise ValueError("Unknown command: %s" % ctype)
        except Exception as e:
            return {"status": "error", "message": str(e)}
        return {"status": "success", "result": result}

    def _resolve_track(self, track_index, expect_track_name):
        if track_index < 0 or track_index >= len(self.track_names):
            raise IndexError("Track index out of range")
        actual = self.track_names[track_index]
        if expect_track_name is not None:
            if actual.strip().lower() != str(expect_track_name).strip().lower():
                raise ValueError(
                    "Track %s is '%s', not the expected '%s'; nothing was "
                    "changed." % (track_index, actual, expect_track_name))
        return actual

    def _add_notes(self, track_index=0, clip_index=0, notes=(),
                   expect_count=None, arrangement=False,
                   expect_track_name=None):
        track_name = self._resolve_track(track_index, expect_track_name)
        requested = len(notes)
        if expect_count is not None and int(expect_count) != requested:
            raise ValueError(
                "Refusing to write: expect_count is %d but %d notes arrived."
                % (int(expect_count), requested))
        if self.legacy_echo:
            return {"note_count": requested}
        key = (track_index, clip_index, bool(arrangement))
        self.clip_counts[key] = self.clip_counts.get(key, 0) + requested
        added = None if self.added_unknown else requested - self.added_shortfall
        return {
            "track_index": track_index, "track_name": track_name,
            "clip_index": clip_index,
            "clip_name": self.clip_names.get(key, ""),
            "arrangement": bool(arrangement), "requested": requested,
            "added": added, "clip_note_count": self.clip_counts[key],
        }

    def _clear_notes(self, track_index=0, clip_index=0, arrangement=False,
                     expect_track_name=None):
        self._resolve_track(track_index, expect_track_name)
        key = (track_index, clip_index, bool(arrangement))
        cleared = self.clip_counts.pop(key, 0)
        return {"cleared_count": cleared}

    def _set_clip_name(self, track_index=0, clip_index=0, name="",
                       expect_track_name=None):
        track_name = self._resolve_track(track_index, expect_track_name)
        arrangement = self.commands[-1]["type"] == "set_arrangement_clip_name"
        key = (track_index, clip_index, arrangement)
        previous = self.clip_names.get(key, "")
        self.clip_names[key] = name
        return {"track_index": track_index, "track_name": track_name,
                "clip_index": clip_index, "previous_name": previous,
                "name": name}


@pytest.fixture
def live():
    peer = FakeLive()
    yield peer
    peer.close()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def notes(count, pitch=36, start=0.0):
    return [{"pitch": pitch, "start_time": start + i * 0.25, "duration": 0.25,
             "velocity": 100, "mute": False} for i in range(count)]


def write_json(path, doc):
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def run(peer, *args, timeout="5"):
    """Run the CLI against ``peer`` (or a bare port), capturing both streams."""
    out, err = io.StringIO(), io.StringIO()
    port = peer.port if isinstance(peer, FakeLive) else peer
    argv = list(args) + ["--host", "127.0.0.1", "--port", str(port),
                         "--timeout", timeout]
    code = push_notes.main(argv, out=out, err=err, env={})
    return code, out.getvalue(), err.getvalue()


def adds(peer):
    return [c for c in peer.commands if c["type"] == "add_notes_to_clip"]


def _free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# --------------------------------------------------------------------------
# Chunking and the guards on the wire
# --------------------------------------------------------------------------

def test_note_file_is_chunked_with_both_guards_on_every_call(live, tmp_path):
    source = notes(275)
    path = write_json(tmp_path / "verse1.kt.0.json", source)

    code, out, err = run(live, str(path), "--track", "0", "--clip", "3",
                         "--expect-track-name", KICK_TOMS)

    assert code == 0, err
    sent = adds(live)
    assert [len(c["params"]["notes"]) for c in sent] == [110, 110, 55]
    for call in sent:
        params = call["params"]
        assert params["expect_count"] == len(params["notes"])
        assert params["expect_track_name"] == KICK_TOMS
        assert params["track_index"] == 0 and params["clip_index"] == 3
        # A Session-slot write says nothing about the view: only scripts
        # >= 1.15.0 know the keyword, and dispatch is **params.
        assert "arrangement" not in params
    # Nothing reordered, nothing dropped, nothing rewritten.
    assert [n for c in sent for n in c["params"]["notes"]] == source
    # The wire exchange is get_script_info, then the adds; no name, no clear.
    assert [c["type"] for c in live.commands] == \
        ["get_script_info"] + ["add_notes_to_clip"] * 3
    assert "Remote Script 1.16.0" in out
    assert "chunk 1/3: sent 110, Live added 110, clip now holds 110" in out
    assert "chunk 3/3: sent 55, Live added 55, clip now holds 275" in out
    assert "Done: 275 notes to 1 clip(s) in 3 add call(s)." in out
    assert err == ""


def test_chunk_size_is_adjustable(live, tmp_path):
    path = write_json(tmp_path / "n.json", notes(7))
    code, out, _ = run(live, str(path), "--track", "0", "--clip", "0",
                       "--chunk-size", "3")
    assert code == 0
    assert [len(c["params"]["notes"]) for c in adds(live)] == [3, 3, 1]


def test_several_note_files_go_to_one_clip_in_order(live, tmp_path):
    first = notes(3, pitch=36)
    second = notes(2, pitch=38, start=4.0)
    a = write_json(tmp_path / "a.json", first)
    b = write_json(tmp_path / "b.json", second)

    code, out, _ = run(live, str(a), str(b), "--track", "1", "--clip", "0",
                       "--expect-track-name", HATS_SNARE)

    assert code == 0
    assert len(adds(live)) == 1
    assert adds(live)[0]["params"]["notes"] == first + second
    assert "a.json, b.json -> track 1 clip 0" in out


def test_guard_not_given_is_absent_from_the_wire_and_called_out(live, tmp_path):
    path = write_json(tmp_path / "n.json", notes(2))
    code, out, _ = run(live, str(path), "--track", "0", "--clip", "0")
    assert code == 0
    assert "expect_track_name" not in adds(live)[0]["params"]
    assert "UNGUARDED" in out


def test_clear_then_notes_then_name_in_that_order(live, tmp_path):
    live.clip_counts[(0, 2, False)] = 9  # something already in the slot
    path = write_json(tmp_path / "n.json", notes(4))

    code, out, _ = run(live, str(path), "--track", "0", "--clip", "2",
                       "--expect-track-name", KICK_TOMS, "--clear",
                       "--clip-name", "verse1 kt")

    assert code == 0
    assert [c["type"] for c in live.commands] == [
        "get_script_info", "clear_notes_from_clip", "add_notes_to_clip",
        "set_clip_name"]
    clear, name = live.commands[1]["params"], live.commands[3]["params"]
    assert clear == {"track_index": 0, "clip_index": 2,
                     "expect_track_name": KICK_TOMS}
    assert name == {"track_index": 0, "clip_index": 2,
                    "expect_track_name": KICK_TOMS, "name": "verse1 kt"}
    assert "cleared 9 note(s)" in out
    assert "named clip 'verse1 kt'" in out
    assert live.clip_counts[(0, 2, False)] == 4


def test_arrangement_flag_reaches_every_command(live, tmp_path):
    path = write_json(tmp_path / "n.json", notes(2))
    code, _, _ = run(live, str(path), "--track", "0", "--clip", "5",
                     "--arrangement", "--clear", "--clip-name", "fixed bar")
    assert code == 0
    assert [c["type"] for c in live.commands] == [
        "get_script_info", "clear_notes_from_clip", "add_notes_to_clip",
        "set_arrangement_clip_name"]
    assert live.commands[1]["params"]["arrangement"] is True
    assert live.commands[2]["params"]["arrangement"] is True
    # set_arrangement_clip_name addresses the arrangement clip by its own
    # command name; the script's handler has no arrangement keyword.
    assert "arrangement" not in live.commands[3]["params"]


def test_empty_note_file_sends_no_notes_but_still_names(live, tmp_path):
    path = write_json(tmp_path / "empty.json", [])
    code, out, _ = run(live, str(path), "--track", "0", "--clip", "0",
                       "--clip-name", "silence")
    assert code == 0
    assert adds(live) == []
    assert live.commands[-1]["type"] == "set_clip_name"
    assert "no notes to send" in out


# --------------------------------------------------------------------------
# Manifests
# --------------------------------------------------------------------------

def test_manifest_targets_files_relative_to_itself_and_inline_notes(live, tmp_path):
    folder = tmp_path / "drums"
    folder.mkdir()
    kt = notes(120, pitch=36)
    sh = notes(30, pitch=42)
    inline = notes(2, pitch=49)
    write_json(folder / "verse1.kt.0.json", kt)
    write_json(folder / "verse1.sh.0.json", sh)
    manifest = write_json(folder / "manifest.json", {"targets": [
        {"file": "verse1.kt.0.json", "track_index": 0, "clip_index": 3,
         "expect_track_name": KICK_TOMS, "clip_name": "verse1 kt",
         "clear": True},
        {"file": "verse1.sh.0.json", "track_index": 1, "clip_index": 3,
         "expect_track_name": HATS_SNARE},
        {"notes": inline, "track_index": 1, "clip_index": 4,
         "arrangement": True},
    ]})

    # Run from elsewhere: file paths resolve against the manifest, not cwd.
    code, out, err = run(live, str(manifest))

    assert code == 0, err
    types = [c["type"] for c in live.commands]
    assert types == ["get_script_info",
                     "clear_notes_from_clip", "add_notes_to_clip",
                     "add_notes_to_clip", "set_clip_name",
                     "add_notes_to_clip",
                     "add_notes_to_clip"]
    sent = adds(live)
    assert [n for c in sent[:2] for n in c["params"]["notes"]] == kt
    assert sent[2]["params"]["notes"] == sh
    assert sent[3]["params"]["notes"] == inline
    for call in sent[:2]:
        assert call["params"]["track_index"] == 0
        assert call["params"]["clip_index"] == 3
        assert call["params"]["expect_track_name"] == KICK_TOMS
    assert sent[2]["params"]["expect_track_name"] == HATS_SNARE
    assert "expect_track_name" not in sent[3]["params"]
    assert sent[3]["params"]["arrangement"] is True
    assert sent[3]["params"]["clip_index"] == 4
    assert "Plan: 152 notes to 3 clip(s)" in out
    assert "Done: 152 notes to 3 clip(s) in 4 add call(s)." in out


def test_manifest_as_a_bare_list_works_too(live, tmp_path):
    write_json(tmp_path / "a.json", notes(1))
    manifest = write_json(tmp_path / "m.json", [
        {"file": "a.json", "track_index": 0, "clip_index": 0},
    ])
    code, _, err = run(live, str(manifest))
    assert code == 0, err
    assert len(adds(live)) == 1


def test_dry_run_validates_prints_the_plan_and_connects_to_nothing(live, tmp_path):
    path = write_json(tmp_path / "n.json", notes(225))
    code, out, _ = run(live, str(path), "--track", "0", "--clip", "1",
                       "--expect-track-name", KICK_TOMS, "--dry-run")
    assert code == 0
    assert live.connections == 0
    assert "225 notes in 3 chunk(s)" in out
    assert "Dry run: nothing sent." in out


# --------------------------------------------------------------------------
# Refusing input before anything is sent
# --------------------------------------------------------------------------

def _note_with(**overrides):
    note = notes(1)[0]
    note.update(overrides)
    return [note]


BAD_NOTE_FILES = [
    ("missing-key", [{"pitch": 36, "start_time": 0, "duration": 1}], "missing velocity, mute"),
    ("pitch-high", _note_with(pitch=128), "pitch must be an integer 0–127"),
    ("pitch-float", _note_with(pitch=36.0), "pitch must be an integer 0–127"),
    ("pitch-bool", _note_with(pitch=True), "pitch must be an integer 0–127"),
    ("start-negative", _note_with(start_time=-0.5), "start_time must be a number ≥ 0"),
    ("duration-zero", _note_with(duration=0), "duration must be a number > 0"),
    ("velocity-high", _note_with(velocity=200), "velocity must be a number 0–127"),
    ("mute-string", _note_with(mute="no"), "mute must be true or false"),
    ("not-an-object", [36], "note 0: expected an object, got an integer"),
    ("object-not-manifest", {"pitch": 36}, "a JSON object must be a manifest"),
    ("scalar", 42, "expected a list of notes or a manifest"),
]


@pytest.mark.parametrize("name, doc, message", BAD_NOTE_FILES,
                         ids=[case[0] for case in BAD_NOTE_FILES])
def test_malformed_note_input_is_refused_before_connecting(live, tmp_path, name, doc, message):
    path = write_json(tmp_path / f"{name}.json", doc)
    code, out, err = run(live, str(path), "--track", "0", "--clip", "0")
    assert code == 2
    assert message in err
    assert path.name in err
    assert live.connections == 0
    assert out == ""


def test_invalid_json_and_missing_file_are_refused(live, tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("[{", encoding="utf-8")
    code, _, err = run(live, str(broken), "--track", "0", "--clip", "0")
    assert code == 2 and "not valid JSON" in err

    code, _, err = run(live, str(tmp_path / "nope.json"), "--track", "0",
                       "--clip", "0")
    assert code == 2 and "nope.json" in err
    assert live.connections == 0


def test_a_bad_note_in_the_second_file_stops_the_whole_run(live, tmp_path):
    good = write_json(tmp_path / "good.json", notes(3))
    bad = write_json(tmp_path / "bad.json", _note_with(velocity=-1))
    code, _, err = run(live, str(good), str(bad), "--track", "0", "--clip", "0")
    assert code == 2
    assert "bad.json: note 0" in err
    assert live.connections == 0


def test_note_file_without_a_destination_is_refused(live, tmp_path):
    path = write_json(tmp_path / "n.json", notes(1))
    code, _, err = run(live, str(path), "--track", "0")
    assert code == 2 and "--track and --clip" in err
    code, _, err = run(live, str(path))
    assert code == 2 and "--track and --clip" in err
    assert live.connections == 0


BAD_MANIFESTS = [
    ("unknown-key", {"targets": [{"file": "a.json", "track_index": 0,
                                  "clip_index": 0, "expect_track": "x"}]},
     "unknown keys ['expect_track']"),
    ("file-and-notes", {"targets": [{"file": "a.json", "notes": [],
                                     "track_index": 0, "clip_index": 0}]},
     "exactly one of \"file\" or \"notes\""),
    ("neither", {"targets": [{"track_index": 0, "clip_index": 0}]},
     "exactly one of \"file\" or \"notes\""),
    ("missing-track", {"targets": [{"file": "a.json", "clip_index": 0}]},
     "missing track_index"),
    ("negative-clip", {"targets": [{"file": "a.json", "track_index": 0,
                                    "clip_index": -1}]},
     "clip_index must be an integer ≥ 0"),
    ("bool-track", {"targets": [{"file": "a.json", "track_index": True,
                                 "clip_index": 0}]},
     "track_index must be an integer ≥ 0"),
    ("guard-not-string", {"targets": [{"file": "a.json", "track_index": 0,
                                       "clip_index": 0,
                                       "expect_track_name": 3}]},
     "expect_track_name must be a string"),
    ("clear-not-bool", {"targets": [{"file": "a.json", "track_index": 0,
                                     "clip_index": 0, "clear": "yes"}]},
     "clear must be true or false"),
    ("targets-not-list", {"targets": {"file": "a.json"}},
     "\"targets\" must be a list"),
    ("extra-top-level", {"targets": [], "defaults": {}},
     "unknown top-level keys ['defaults']"),
    ("entry-not-object", {"targets": ["a.json"]},
     "target 0: expected an object, got a string"),
    ("bad-notes-inside", {"targets": [{"notes": _note_with(pitch=-1),
                                       "track_index": 0, "clip_index": 0}]},
     "target 0: notes: note 0: pitch must be an integer 0–127"),
    ("missing-file", {"targets": [{"file": "missing.json", "track_index": 0,
                                   "clip_index": 0}]},
     "missing.json"),
]


@pytest.mark.parametrize("name, doc, message", BAD_MANIFESTS,
                         ids=[case[0] for case in BAD_MANIFESTS])
def test_malformed_manifest_is_refused_before_connecting(live, tmp_path, name, doc, message):
    write_json(tmp_path / "a.json", notes(1))
    manifest = write_json(tmp_path / "m.json", doc)
    code, _, err = run(live, str(manifest))
    assert code == 2
    assert message in err
    assert live.connections == 0


def test_manifest_does_not_take_per_clip_flags_or_extra_files(live, tmp_path):
    write_json(tmp_path / "a.json", notes(1))
    manifest = write_json(tmp_path / "m.json", {"targets": [
        {"file": "a.json", "track_index": 0, "clip_index": 0}]})

    code, _, err = run(live, str(manifest), "--track", "0")
    assert code == 2 and "names its own targets" in err

    code, _, err = run(live, str(manifest), "--clear")
    assert code == 2 and "names its own targets" in err

    code, _, err = run(live, str(manifest), str(tmp_path / "a.json"))
    assert code == 2 and "not a mix" in err
    assert live.connections == 0


def test_chunk_size_must_be_positive(tmp_path, capsys):
    path = write_json(tmp_path / "n.json", notes(1))
    with pytest.raises(SystemExit) as exc:
        push_notes.main([str(path), "--track", "0", "--clip", "0",
                         "--chunk-size", "0"], env={})
    assert exc.value.code == 2
    assert "at least 1" in capsys.readouterr().err


def test_host_and_port_default_from_the_servers_environment_variables():
    parser = push_notes.build_parser(env={"ABLETON_HOST": "10.0.0.5",
                                          "ABLETON_PORT": "1234"})
    args = parser.parse_args(["x.json"])
    assert (args.host, args.port) == ("10.0.0.5", 1234)
    args = push_notes.build_parser(env={}).parse_args(["x.json"])
    assert (args.host, args.port) == ("127.0.0.1", 9877)


# --------------------------------------------------------------------------
# What Live says back
# --------------------------------------------------------------------------

def test_a_refusal_stops_the_run_and_reports_what_landed(live, tmp_path):
    live.refuse = ("add_notes_to_clip", 2)
    path = write_json(tmp_path / "n.json", notes(275))

    code, out, err = run(live, str(path), "--track", "0", "--clip", "0",
                         "--expect-track-name", KICK_TOMS)

    assert code == 1
    assert len(adds(live)) == 2            # the third chunk was never sent
    assert "chunk 1/3: sent 110, Live added 110" in out
    assert "FAILED: n.json -> track 0 clip 0, chunk 2/3 (add_notes_to_clip): simulated refusal" in err
    assert "Landed before that: 110 notes in 1 add call(s); 0 clip(s) complete." in err
    assert "half-written" in err


def test_a_stale_index_is_refused_by_the_script_with_nothing_written(live, tmp_path):
    path = write_json(tmp_path / "n.json", notes(5))
    code, _, err = run(live, str(path), "--track", "1", "--clip", "0",
                       "--expect-track-name", KICK_TOMS)  # track 1 is the hats
    assert code == 1
    assert "not the expected 'drums — kick + toms'" in err
    assert live.clip_counts == {}
    assert "Landed before that: 0 notes in 0 add call(s)" in err
    assert "half-written" not in err        # nothing landed, nothing to clear


def test_a_refusal_on_a_later_target_keeps_the_earlier_ones_complete(live, tmp_path):
    write_json(tmp_path / "a.json", notes(2))
    write_json(tmp_path / "b.json", notes(2))
    manifest = write_json(tmp_path / "m.json", {"targets": [
        {"file": "a.json", "track_index": 0, "clip_index": 0},
        {"file": "b.json", "track_index": 1, "clip_index": 0,
         "expect_track_name": "wrong name"},
    ]})
    code, out, err = run(live, str(manifest))
    assert code == 1
    assert "1 clip(s) complete" in err
    assert live.clip_counts == {(0, 0, False): 2}


def test_a_count_that_disagrees_is_flagged_and_fails_the_run(live, tmp_path):
    live.added_shortfall = 1
    path = write_json(tmp_path / "n.json", notes(3))
    code, out, err = run(live, str(path), "--track", "0", "--clip", "0")
    assert code == 1
    assert "sent 3, Live added 2, clip now holds 3" in out
    assert "** COUNT MISMATCH **" in out
    assert "Live's counts disagree with what was sent: n.json chunk 1/1: sent 3, added 2" in err


def test_an_uncountable_clip_is_reported_but_not_a_failure(live, tmp_path):
    live.added_unknown = True
    path = write_json(tmp_path / "n.json", notes(3))
    code, out, err = run(live, str(path), "--track", "0", "--clip", "0")
    assert code == 0
    assert "Live could not count the clip (added unknown)" in out
    assert "Not verified by Live's own count: n.json chunk 1/1" in out
    assert err == ""


def test_a_pre_1_15_script_that_echoes_the_count_is_named_as_such(live, tmp_path):
    live.legacy_echo = True
    live.script_version = "1.14.0"
    path = write_json(tmp_path / "n.json", notes(3))
    code, out, err = run(live, str(path), "--track", "0", "--clip", "0")
    assert code == 0
    assert "Remote Script 1.14.0" in out
    assert "Live echoed 3" in out
    assert "re-run ableton-mcp-install-script" in out
    assert "Not verified" in out


def test_a_script_too_old_for_get_script_info_still_gets_the_notes(live, tmp_path):
    live.refuse_script_info = True
    path = write_json(tmp_path / "n.json", notes(3))
    code, out, err = run(live, str(path), "--track", "0", "--clip", "0")
    assert code == 0, err
    assert "get_script_info was refused" in out
    assert len(adds(live)) == 1


def test_live_names_are_printed_from_the_reply(live, tmp_path):
    live.clip_names[(1, 0, False)] = "verse1 sh"
    path = write_json(tmp_path / "n.json", notes(1))
    code, out, _ = run(live, str(path), "--track", "1", "--clip", "0")
    assert code == 0
    assert f"clip 'verse1 sh' on '{HATS_SNARE}'" in out


# --------------------------------------------------------------------------
# The transport: replies in pieces, replies that never come
# --------------------------------------------------------------------------

def test_reply_is_reassembled_across_recv_calls(live, tmp_path):
    def dribble(conn, payload):
        third = len(payload) // 3
        for piece in (payload[:third], payload[third:2 * third],
                      payload[2 * third:]):
            conn.sendall(piece)
            time.sleep(0.02)

    live.reply_writer = dribble
    path = write_json(tmp_path / "n.json", notes(2))
    code, out, err = run(live, str(path), "--track", "0", "--clip", "0",
                         "--expect-track-name", KICK_TOMS)
    assert code == 0, err
    assert f"on '{KICK_TOMS}'" in out


def test_reply_split_inside_a_multibyte_char_is_still_reassembled(live, tmp_path):
    """The real script's json.dumps escapes non-ASCII, so this is the
    defensive branch: a peer that emits raw UTF-8 and happens to break a
    recv inside the em dash must not read as a lost connection."""
    def dribble(conn, payload):
        dash = "—".encode("utf-8")
        cut = payload.find(dash)
        if cut < 0:                      # get_script_info has no dash
            conn.sendall(payload)
            return
        cut += 1                         # one byte into the 3-byte dash
        for piece in (payload[:cut], payload[cut:cut + 1], payload[cut + 1:]):
            conn.sendall(piece)
            time.sleep(0.02)

    live.raw_utf8 = True
    live.reply_writer = dribble
    path = write_json(tmp_path / "n.json", notes(2))
    code, out, err = run(live, str(path), "--track", "0", "--clip", "0",
                         "--expect-track-name", KICK_TOMS)
    assert code == 0, err
    assert f"on '{KICK_TOMS}'" in out


def test_no_reply_within_the_timeout_is_reported(live, tmp_path):
    live.reply_writer = lambda conn, payload: None   # Live never answers
    path = write_json(tmp_path / "n.json", notes(2))
    code, _, err = run(live, str(path), "--track", "0", "--clip", "0",
                       timeout="0.3")
    assert code == 1
    assert "get_script_info: no reply from Live within 0.3 s" in err


def test_live_closing_mid_reply_is_reported(live, tmp_path):
    def truncate(conn, payload):
        conn.sendall(payload[:10])
        conn.shutdown(socket.SHUT_RDWR)
        conn.close()

    live.reply_writer = truncate
    path = write_json(tmp_path / "n.json", notes(2))
    code, _, err = run(live, str(path), "--track", "0", "--clip", "0")
    assert code == 1
    assert "Live closed the connection mid-reply" in err


def test_nothing_listening_is_reported_with_the_fix(tmp_path):
    path = write_json(tmp_path / "n.json", notes(2))
    code, _, err = run(_free_port(), str(path), "--track", "0", "--clip", "0")
    assert code == 1
    assert "FAILED: could not connect to 127.0.0.1:" in err
    assert "AbletonMCP control surface" in err


def test_the_script_is_standard_library_only():
    """It runs as ``python3 scripts/push_notes.py`` with nothing installed,
    and adds nothing to the server's dependency surface."""
    import ast
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    stdlib = set(sys.stdlib_module_names)
    assert imported <= stdlib, sorted(imported - stdlib)
