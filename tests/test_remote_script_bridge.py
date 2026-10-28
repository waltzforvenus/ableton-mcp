"""The Remote Script's TCP framing and browser-tree walk, against the real
canonical script.

Adapted from upstream's tests/test_remote_script_bridge.py (7e90cd6, MIT) to
the fork's harness. Each test pins a defect that commit fixed:

* each recv() chunk was decoded on its own, so a multi-byte UTF-8 character
  split across a chunk boundary desynchronised the connection,
* an incomplete command grew the buffer without bound,
* get_browser_tree never recursed and never reported a folder count.

Upstream's ABLETON_MCP_HOST override is deliberately not ported: the bind
stays a loopback literal (CLAUDE.md hard rule 2,
test_remote_script_ast::test_socket_binds_to_loopback).

Runs anywhere: no Ableton, no network.
"""

import json
import threading

from fake_ableton import (
    FakeApplication,
    FakeBrowser,
    FakeBrowserItem,
    make_harness,
)


# ------------------------------------------------------------------ framing

class FakeClient(object):
    """Hands out prepared chunks, then EOF. Records what was written back."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.sent = []
        self.closed = False

    def settimeout(self, _timeout):
        pass

    def recv(self, _size):
        return self._chunks.pop(0) if self._chunks else b""

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True


def _serve(chunks, harness=None):
    """Run one client session through the real _handle_client, with
    _process_command recorded, and decode whatever it wrote back."""
    harness = harness if harness is not None else make_harness()
    mcp = harness.mcp
    mcp.running = True
    received = []

    def _process(command):
        received.append(command)
        return {"status": "success", "result": {"echo": command.get("type")}}

    mcp._process_command = _process
    client = FakeClient(chunks)
    mcp._handle_client(client)
    replies = [json.loads(frame.decode("utf-8")) for frame in client.sent]
    assert client.closed
    return received, replies


def test_command_split_mid_utf8_character_still_parses():
    """Decoding each chunk alone raised on the split, dropped the chunk, and
    wrote an error frame into a stream still awaiting the real response.
    ensure_ascii=False is what puts raw multi-byte on the wire."""
    payload = json.dumps(
        {"type": "set_clip_name", "params": {"name": "café été"}},
        ensure_ascii=False,
    ).encode("utf-8")
    split = payload.index(b"\xc3") + 1  # between the two bytes of 'é'
    assert payload[:split].endswith(b"\xc3")

    received, replies = _serve([payload[:split], payload[split:]])

    assert len(received) == 1
    assert received[0]["params"]["name"] == "café été"
    assert replies == [{"status": "success", "result": {"echo": "set_clip_name"}}]


def test_command_split_across_many_chunks_parses_once():
    payload = json.dumps({"type": "get_session_info", "params": {}}).encode("utf-8")
    chunks = [payload[i:i + 3] for i in range(0, len(payload), 3)]

    received, replies = _serve(chunks)

    assert len(received) == 1
    assert len(replies) == 1


def test_back_to_back_commands_each_get_one_reply():
    first = json.dumps({"type": "start_playback", "params": {}}).encode("utf-8")
    second = json.dumps({"type": "stop_playback", "params": {}}).encode("utf-8")

    received, replies = _serve([first, second])

    assert [c["type"] for c in received] == ["start_playback", "stop_playback"]
    assert len(replies) == 2


def test_oversized_request_is_refused_instead_of_buffered_forever(monkeypatch):
    """A client that never completes a command must not grow the buffer."""
    harness = make_harness()
    monkeypatch.setattr(harness.module, "MAX_REQUEST_BYTES", 1024)
    junk = b'{"type": "' + b"x" * 4096

    received, replies = _serve([junk, junk], harness=harness)

    assert received == []
    assert len(replies) == 1
    assert replies[0]["status"] == "error"


def test_a_real_command_round_trips_through_the_real_dispatch():
    """The framing change end to end: bytes in, the real _process_command,
    bytes out."""
    harness = make_harness()
    harness.mcp.running = True
    payload = json.dumps({"type": "get_session_info", "params": {}}).encode("utf-8")
    client = FakeClient([payload[:7], payload[7:]])
    harness.mcp._handle_client(client)
    assert len(client.sent) == 1
    reply = json.loads(client.sent[0].decode("utf-8"))
    assert reply["status"] == "success"
    assert reply["result"]["tempo"] == 120.0


# ------------------------------------------------------------- browser tree

def _tree(browser, category_type="instruments"):
    harness = make_harness(application=FakeApplication(browser=browser))
    response = harness.process({"type": "get_browser_tree",
                                "params": {"category_type": category_type}})
    assert response["status"] == "success", response
    return response["result"]


def test_browser_tree_returns_children_and_a_truthful_folder_count():
    """process_item never recursed (children was always []) and total_folders
    was never returned, so the server's header always read 'showing 0
    folders'."""
    kits = FakeBrowserItem("Kits", children=[
        FakeBrowserItem("808 Core Kit", is_loadable=True)])
    browser = FakeBrowser(instruments=FakeBrowserItem("Instruments",
                                                      children=[kits]))

    tree = _tree(browser)

    assert tree["total_folders"] == 2  # Instruments and Kits have children
    category = tree["categories"][0]
    assert [child["name"] for child in category["children"]] == ["Kits"]
    # Depth is capped one level below the category, so Kits reports more.
    assert category["children"][0]["has_more"] is True
    assert category["children"][0]["children"] == []


def test_browser_tree_caps_a_huge_folder_and_flags_it():
    many = [FakeBrowserItem("Preset %d" % i, is_loadable=True)
            for i in range(200)]
    browser = FakeBrowser(instruments=FakeBrowserItem("Instruments",
                                                      children=many))

    category = _tree(browser)["categories"][0]

    assert len(category["children"]) == 64
    assert category["has_more"] is True


def test_browser_tree_standard_fixture_lists_first_level_items():
    harness = make_harness()
    result = harness.process({"type": "get_browser_tree",
                              "params": {"category_type": "all"}})["result"]
    instruments = result["categories"][0]
    assert [c["name"] for c in instruments["children"]] == ["Synths"]
    assert instruments["children"][0]["has_more"] is True
    drums = result["categories"][2]
    assert [c["name"] for c in drums["children"]] == ["808 Core Kit"]
    assert "has_more" not in drums
    # Five roots with children, plus Synths one level down.
    assert result["total_folders"] == 6


# ------------------------------------------------- main-thread dispatch safety

def _deferred_harness(monkeypatch, command="delete_track", queue_timeout=0.05):
    """A harness whose schedule_message queues tasks instead of running them,
    standing in for a Live tick that has not come round yet, with the
    command's queue timeout shrunk so the wait expires at once."""
    harness = make_harness()
    pending = []
    harness.mcp.schedule_message = lambda delay, task: pending.append(task)
    method, main_thread, _timeout, advertise = harness.module.COMMANDS[command]
    monkeypatch.setitem(harness.module.COMMANDS, command,
                        (method, main_thread, queue_timeout, advertise))
    return harness, pending


def test_a_timed_out_write_never_lands_later(monkeypatch):
    """Upstream #140: a task still queued when the client is told 'timeout'
    used to run on the next tick anyway, editing the set behind the
    client's back."""
    harness, pending = _deferred_harness(monkeypatch)
    track_count = len(harness.song.tracks)

    response = harness.process({"type": "delete_track",
                                "params": {"track_index": 0}})
    assert response["status"] == "error"
    assert "not executed" in response["message"]

    assert len(pending) == 1
    pending[0]()  # Live's tick finally comes round
    assert len(harness.song.tracks) == track_count
    assert any("already timed out" in line for line in harness.logs)


def test_a_write_that_started_before_the_timeout_reports_unknown(monkeypatch):
    """A task already inside its handler when the wait expires cannot be
    called back; the reply must say the outcome is unknown, not that the
    command did not run."""
    harness, _pending = _deferred_harness(monkeypatch)
    real_delete = harness.mcp._delete_track
    release = threading.Event()

    def held_delete(**params):
        release.wait(timeout=2.0)  # still working when the wait expires
        return real_delete(**params)

    harness.mcp._delete_track = held_delete
    harness.mcp.schedule_message = (
        lambda delay, task: threading.Thread(target=task, daemon=True).start())

    response = harness.process({"type": "delete_track",
                                "params": {"track_index": 0}})
    release.set()
    assert response["status"] == "error"
    assert "outcome is unknown" in response["message"]


def test_a_write_inside_its_budget_is_unaffected():
    harness = make_harness()
    response = harness.process({"type": "set_tempo", "params": {"tempo": 99.0}})
    assert response["status"] == "success"
    assert harness.song.tempo == 99.0


# ----------------------------------------------------- expected-error logging

def test_an_expected_refusal_logs_one_line_and_no_traceback():
    """Upstream #141: a bad index is normal use, not a fault; a traceback
    per refusal buried the real failures in Live's Log.txt."""
    harness = make_harness()
    response = harness.process({"type": "set_tempo_nonexistent", "params": {}})
    assert response["status"] == "error"  # unknown command: no log at all

    response = harness.process({"type": "delete_track",
                                "params": {"track_index": 999}})
    assert response["status"] == "error"
    assert not any("Traceback" in line for line in harness.logs)
    assert any(line.startswith("Error in main thread task")
               for line in harness.logs)


def test_a_real_fault_still_logs_its_traceback():
    harness = make_harness()

    def broken(**params):
        raise RuntimeError("LOM went away")

    harness.mcp._get_session_info = broken
    response = harness.process({"type": "get_session_info", "params": {}})
    assert response["status"] == "error"
    assert response["message"] == "LOM went away"
    assert any("Traceback" in line for line in harness.logs)


def test_a_boost_argument_error_is_a_fault_not_a_refusal():
    """Boost.Python's ArgumentError subclasses TypeError; it must keep its
    traceback."""
    harness = make_harness()

    class ArgumentError(TypeError):
        pass

    def broken(**params):
        raise ArgumentError("Python argument types did not match C++ signature")

    harness.mcp._get_session_info = broken
    harness.process({"type": "get_session_info", "params": {}})
    assert any("Traceback" in line for line in harness.logs)
