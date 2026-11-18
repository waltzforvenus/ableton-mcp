"""Lifecycle regressions with fake peers, no listeners."""
import socket
import threading
from fake_ableton import make_harness

class Peer:
    def __init__(self): self.eof=False; self.closed=False; self.shutdowns=0
    def recv(self,size,flags):
        assert flags == socket.MSG_PEEK | socket.MSG_DONTWAIT
        if self.eof: return b''
        raise BlockingIOError()
    def shutdown(self,how): self.shutdowns+=1; self.eof=True
    def close(self): self.closed=True

def test_pending_eof_prevents_mutation():
    h=make_harness(); p=Peer()
    assert not h.mcp._client_disconnected(p)
    req=h.mcp._enqueue_command({'type':'set_tempo','params':{'tempo':99}},p)
    p.eof=True; h.mcp.update_display()
    assert h.song.tempo==120 and 'not executed' in req.response['message']

def test_shutdown_cancels_and_rejects_admission():
    h=make_harness(); p=Peer(); assert h.mcp._register_client(p)
    server=Peer(); h.mcp.server=server
    pending=h.mcp._enqueue_command({'type':'set_tempo','params':{'tempo':99}},p)
    running=h.mcp._enqueue_command({'type':'get_session_info'},p); assert running.begin()
    h.mcp._shutdown_commands(); h.mcp._shutdown_commands()
    assert 'not executed' in pending.response['message']
    assert 'outcome is unknown' in running.response['message']
    assert p.closed and p.shutdowns==1 and server.closed
    rejected=h.mcp._enqueue_command({'type':'set_tempo','params':{'tempo':88}})
    assert 'not executed' in rejected.response['message']
    late=Peer(); assert not h.mcp._register_client(late); assert late.closed
    h.mcp.update_display(); assert h.song.tempo==120

def test_eof_after_start_returns_unknown():
    h=make_harness(); p=Peer(); admitted=threading.Event(); returned=threading.Event()
    put=h.mcp._command_queue.put_nowait
    h.mcp._command_queue.put_nowait=lambda req:(put(req),admitted.set())
    calls=[]; responses=[]
    def handler(tempo):
        calls.append(tempo); p.eof=True
        assert returned.wait(1)
        h.song.tempo=tempo
        return {}
    h.mcp._set_tempo=handler
    def worker():
        responses.append(h.mcp._process_command({'type':'set_tempo','params':{'tempo':99}},p))
        returned.set()
    t=threading.Thread(target=worker); t.start(); assert admitted.wait(1)
    h.mcp.update_display(); t.join(1)
    assert not t.is_alive() and 'outcome is unknown' in responses[0]['message']
    h.mcp.update_display(); assert calls==[99]

def test_shutdown_wakes_blocked_recv_without_worker_callbacks():
    h=make_harness(); h.mcp.running=True
    entered=threading.Event(); released=threading.Event()
    class Blocked(Peer):
        def settimeout(self,value): pass
        def recv(self,size):
            entered.set(); assert released.wait(1); return b''
        def shutdown(self,how): super().shutdown(how); released.set()
    p=Blocked()
    def forbidden(*args): raise AssertionError("worker touched ControlSurface")
    h.mcp.log_message=forbidden; h.mcp.show_message=forbidden
    t=threading.Thread(target=h.mcp._handle_client,args=(p,)); t.start()
    assert entered.wait(1); h.mcp._shutdown_commands(); t.join(1)
    assert not t.is_alive() and p.closed

def test_accepted_socket_racing_shutdown_is_closed_without_worker_start():
    h=make_harness(); h.mcp.running=True; p=Peer()
    class Listener(Peer):
        def settimeout(self,value): pass
        def accept(self):
            h.mcp._shutdown_commands()
            return p, ('127.0.0.1',1)
    h.mcp.server=Listener()
    h.mcp._server_thread()
    assert p.closed and not h.mcp._clients

def test_peek_preserves_buffered_bytes():
    h=make_harness()
    class Buffered(Peer):
        def recv(self,size,flags):
            assert flags & socket.MSG_PEEK and flags & socket.MSG_DONTWAIT
            return b'x'
    assert not h.mcp._client_disconnected(Buffered())

def test_shutdown_releases_pending_worker_without_execution():
    h=make_harness(); admitted=threading.Event(); responses=[]
    put=h.mcp._command_queue.put_nowait
    h.mcp._command_queue.put_nowait=lambda req:(put(req),admitted.set())
    t=threading.Thread(target=lambda:responses.append(h.mcp._process_command(
        {'type':'set_tempo','params':{'tempo':99}})))
    t.start(); assert admitted.wait(1)
    h.mcp._shutdown_commands(); t.join(1)
    assert not t.is_alive() and 'not executed' in responses[0]['message']
    h.mcp.update_display(); assert h.song.tempo==120

def test_disconnect_never_joins_accept_worker(monkeypatch):
    h=make_harness()
    class Worker:
        def is_alive(self): return True
        def join(self,*args): raise AssertionError('Live callback joined worker')
    h.mcp.server_thread=Worker(); h.mcp.client_threads=[Worker()]
    monkeypatch.setattr(h.module.ControlSurface,'disconnect',lambda self:None,raising=False)
    h.mcp.disconnect(); h.mcp.disconnect()
    assert h.mcp._commands_stopped
