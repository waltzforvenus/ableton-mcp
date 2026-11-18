"""Fake sockets only: total response deadlines, no replay."""
import pytest
from ableton_mcp.connection import AbletonConnection

class Wire:
    def __init__(self, now, chunks):
        self.now=now; self.chunks=list(chunks)
        self.timeouts=[]; self.sent=[]; self.closed=False
    def settimeout(self, value): self.timeouts.append(value)
    def sendall(self, data): self.sent.append(data)
    def recv(self, size):
        advance, chunk=self.chunks.pop(0)
        self.now[0]+=advance
        return chunk
    def close(self): self.closed=True

def test_trickle_cannot_extend_deadline():
    now=[0.]
    wire=Wire(now, [(4,b'{'), (4,b' '), (4,b' '), (0,b'"status":"success"}')])
    c=AbletonConnection('localhost',1,wire,receive_timeout=10,clock=lambda:now[0])
    with pytest.raises(Exception, match='Timeout'):
        c.send_command('set_tempo', {'tempo':99})
    assert wire.timeouts==[10,10,6,2]
    assert wire.closed and c.sock is None and len(wire.sent)==1
    assert len(wire.chunks)==1

def test_registry_budget_not_default_budget():
    now=[0.]
    wire=Wire(now, [(20,b'{'),(20,b'"status":"success","result":{"ok":true}}')])
    c=AbletonConnection('localhost',1,wire,clock=lambda:now[0])
    assert c.send_command('create_audio_clip')=={'ok':True}
    assert wire.timeouts==[65,65,45]

@pytest.mark.parametrize('chunk',[b'',b'[]',b'null',b'\xff'])
def test_invalid_frame_discards_socket(chunk):
    now=[0.]; wire=Wire(now,[(1,chunk)])
    c=AbletonConnection('localhost',1,wire,clock=lambda:now[0])
    with pytest.raises(Exception): c.send_command('get_session_info')
    assert wire.closed and c.sock is None and len(wire.sent)==1

def test_reconnect_invalidates_handshake_before_new_command(monkeypatch):
    from ableton_mcp.connection import AbletonClient
    now=[0.]; old=Wire(now,[(0,b'')]); new=Wire(now,[(0,b'{"status":"success","result":{}}')])
    client=AbletonClient('localhost',1)
    client._connection=AbletonConnection('localhost',1,old)
    client._ever_connected=True
    with pytest.raises(Exception): client._connection.send_command('set_tempo')
    events=[]; client._on_reconnect=lambda:events.append('invalidate')
    def connect(c): c.sock=new; return True
    monkeypatch.setattr(AbletonConnection,'connect',connect)
    send=new.sendall
    def send_new(data): events.append('send'); send(data)
    new.sendall=send_new
    assert client.send_command('get_script_info')=={}
    assert events==['invalidate','send'] and len(old.sent)==1 and len(new.sent)==1
