"""Level-2 transport tests (docs/REFACTOR_PLAN.md §3.5, §5): AbletonClient /
AbletonConnection against a scripted in-process TCP peer.

The peer is a real listening socket on 127.0.0.1 — the same loopback-only
surface the production Remote Script socket uses — so the tests exercise the
genuine send/recv/timeout/reconnect machinery. No Ableton, no external
network.

Each of the three §3.5 hardenings is covered here:
1. reconnect-under-lock — concurrent senders stay serialized, no duplicate
   sockets;
2. drop-the-socket-after-timeout — a stale late reply can never be read as
   the next command's answer;
3. on_reconnect — establishing a replacement socket invalidates a wired
   ScriptHandshake cache.
"""

import json
import socket
import threading
import time

import pytest

from ableton_mcp.connection import AbletonClient
from ableton_mcp.handshake import ScriptHandshake


class ScriptedPeer:
    """A loopback TCP server standing in for the Remote Script's socket.

    ``handler(request, conn, conn_no)`` is called once per received command
    dict; returning a dict sends it back as one JSON blob, returning None
    means the handler wrote (or deliberately withheld) the bytes itself.
    ``conn_no`` counts accepted connections from 1, so a handler can behave
    differently before and after a reconnect.
    """

    def __init__(self, handler):
        self.handler = handler
        self.connections = []  # accepted sockets, in accept order
        self._closing = False
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        conn_no = 0
        while not self._closing:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            conn_no += 1
            self.connections.append(conn)
            threading.Thread(target=self._serve, args=(conn, conn_no),
                             daemon=True).start()

    def _serve(self, conn, conn_no):
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
                request = json.loads(buffer.decode("utf-8"))
            except ValueError:
                # Incomplete JSON — keep receiving. (Two interleaved requests
                # would concatenate here and never parse: a serialization
                # failure shows up as a hang, caught by the join timeouts.)
                continue
            buffer = b""
            try:
                response = self.handler(request, conn, conn_no)
            except OSError:
                return
            if response is not None:
                try:
                    conn.sendall(json.dumps(response).encode("utf-8"))
                except OSError:
                    return

    def kill_connection(self, index):
        """Force-close one accepted connection so the client sees a dead
        socket. shutdown() before close(): the serve thread is blocked in
        recv() on the same fd, and a bare close() would defer the FIN until
        that recv returns — which is never."""
        conn = self.connections[index]
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()

    def close(self):
        self._closing = True
        try:
            self._listener.close()
        except OSError:
            pass
        for conn in self.connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass


@pytest.fixture
def make_peer():
    peers = []

    def _make(handler):
        peer = ScriptedPeer(handler)
        peers.append(peer)
        return peer

    yield _make
    for peer in peers:
        peer.close()


def _success(result):
    return {"status": "success", "result": result}


# --------------------------------------------------------------------------
# Chunked JSON reassembly
# --------------------------------------------------------------------------

def test_chunked_json_response_is_reassembled(make_peer):
    payload = _success({"blob": "x" * 20000, "n": 7})
    encoded = json.dumps(payload).encode("utf-8")

    def handler(request, conn, conn_no):
        # Dribble the response out in many small writes so the client's recv
        # loop must reassemble it across multiple chunks.
        for i in range(0, len(encoded), 1024):
            conn.sendall(encoded[i:i + 1024])
            time.sleep(0.002)
        return None

    peer = make_peer(handler)
    client = AbletonClient("127.0.0.1", peer.port)
    try:
        assert client.send_command("get_session_info") == payload["result"]
    finally:
        client.close()


# --------------------------------------------------------------------------
# Timeout -> socket dropped, reconnect fresh, stale reply never read
# --------------------------------------------------------------------------

def test_timeout_drops_the_socket_and_the_stale_reply_is_never_read(make_peer):
    release_stale_reply = threading.Event()

    def handler(request, conn, conn_no):
        if conn_no == 1:
            # Withhold the first reply until AFTER the client has timed out
            # and sent its next command. Were the timed-out socket reused,
            # this stale reply would be read as that next command's answer —
            # the desync the drop-on-timeout hardening makes impossible.
            release_stale_reply.wait(timeout=5.0)
            try:
                conn.sendall(json.dumps(_success({"answer": "stale"})).encode("utf-8"))
            except OSError:
                pass
            return None
        return _success({"answer": "fresh", "echo": request["type"]})

    peer = make_peer(handler)
    client = AbletonClient("127.0.0.1", peer.port, receive_timeout=0.3)
    try:
        with pytest.raises(Exception, match="No data received"):
            client.send_command("get_session_info")

        # The timed-out socket was closed and discarded, not kept around.
        assert client._connection.sock is None

        release_stale_reply.set()
        time.sleep(0.1)  # let the stale reply be written (into the closed socket)

        result = client.send_command("get_track_info", {"track_index": 0})
        assert result == {"answer": "fresh", "echo": "get_track_info"}
        # The retry established a brand-new connection for the second call.
        assert len(peer.connections) == 2
    finally:
        client.close()


# --------------------------------------------------------------------------
# Concurrent senders stay serialized (reconnect and exchange under one lock)
# --------------------------------------------------------------------------

def test_concurrent_senders_are_serialized_on_one_socket(make_peer):
    def handler(request, conn, conn_no):
        # Linger inside the exchange so an unserialized second sender would
        # interleave its request bytes with this one's reply window.
        time.sleep(0.05)
        return _success({"echo": request["params"]["tag"]})

    peer = make_peer(handler)
    client = AbletonClient("127.0.0.1", peer.port)
    results = {}
    errors = []

    def call(tag):
        try:
            results[tag] = client.send_command("get_track_info", {"tag": tag})
        except Exception as e:  # pragma: no cover - failure detail
            errors.append((tag, e))

    threads = [threading.Thread(target=call, args=("t%d" % i,)) for i in range(4)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        assert not any(t.is_alive() for t in threads), (
            "senders deadlocked/interleaved — requests never parsed as JSON"
        )
        assert errors == []
        # Every caller read ITS OWN reply, and nobody raced the lazy connect
        # into a duplicate socket.
        assert results == {"t%d" % i: {"echo": "t%d" % i} for i in range(4)}
        assert len(peer.connections) == 1
    finally:
        client.close()


# --------------------------------------------------------------------------
# on_reconnect fires for replacement sockets and invalidates the handshake
# --------------------------------------------------------------------------

def test_reconnect_invalidates_a_wired_handshake_cache(make_peer):
    def handler(request, conn, conn_no):
        if request["type"] == "get_script_info":
            return _success({"script_version": "9.9.9", "capabilities": ["x"]})
        return _success({"conn_no": conn_no})

    peer = make_peer(handler)
    handshake = ScriptHandshake()
    # Exactly the wiring build_deps performs.
    client = AbletonClient("127.0.0.1", peer.port,
                           on_reconnect=handshake.invalidate)
    try:
        handshake.perform(client.send_command)
        assert handshake.info()["script_version"] == "9.9.9"
        # The FIRST connect is not a REconnect: the cache survives it (it was
        # earned over this very socket).
        assert handshake.info() is not None

        # "Live restarts": the peer closes its end; the client's liveness
        # probe must notice, build a replacement socket, and invalidate.
        peer.kill_connection(0)
        time.sleep(0.2)

        assert client.send_command("ping")["conn_no"] == 2
        assert handshake.info() is None, (
            "handshake cache survived a reconnect — gating answers would be "
            "stale after a Live restart"
        )
    finally:
        client.close()


def test_on_reconnect_does_not_fire_on_first_connect(make_peer):
    def handler(request, conn, conn_no):
        return _success({"ok": True})

    peer = make_peer(handler)
    fired = []
    client = AbletonClient("127.0.0.1", peer.port,
                           on_reconnect=lambda: fired.append(1))
    try:
        client.send_command("get_session_info")
        assert fired == []
        peer.kill_connection(0)
        time.sleep(0.2)
        client.send_command("get_session_info")
        assert fired == [1]
    finally:
        client.close()


# --------------------------------------------------------------------------
# Ableton error envelope
# --------------------------------------------------------------------------

def test_ableton_error_envelope_raises_with_the_message(make_peer):
    def handler(request, conn, conn_no):
        return {"status": "error", "message": "Track index out of range"}

    peer = make_peer(handler)
    client = AbletonClient("127.0.0.1", peer.port)
    try:
        with pytest.raises(Exception) as excinfo:
            client.send_command("get_track_info", {"track_index": 99})
        # The exact wrapped shape AbletonConnection has always produced (the
        # envelope raise is re-caught by its generic communication handler).
        assert str(excinfo.value) == (
            "Communication error with Ableton: Track index out of range"
        )
    finally:
        client.close()
