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
