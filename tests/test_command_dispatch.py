"""Real dispatcher, fake main tick and deterministic deadlines; no sockets."""
import threading
import pytest
from fake_ableton import make_harness


def setup_dispatch():
    h = make_harness()
    now = [0.0]
    h.mcp._initialize_command_dispatch(clock=lambda: now[0])
    return h, now


def test_every_registered_handler_runs_only_on_main_tick():
    h, now = setup_dispatch()
    main = threading.get_ident()
    seen = []
    for name, (method, _, _, _) in h.module.COMMANDS.items():
        setattr(h.mcp, method, lambda **kw: seen.append(threading.get_ident()) or {})
        before = len(seen)
        admitted = threading.Event()
        put = h.mcp._command_queue.put_nowait
        h.mcp._command_queue.put_nowait = lambda req: (put(req), admitted.set())
        responses = []
        t = threading.Thread(target=lambda: responses.append(h.mcp._process_command({'type':name})))
        t.start()
        assert admitted.wait(1)
        assert len(seen) == before
        h.mcp.update_display()
        t.join(1)
        assert not t.is_alive()
        assert responses[0]['status'] == 'success'
        h.mcp._command_queue.put_nowait = put
    assert seen == [main] * len(h.module.COMMANDS)
    assert h.scheduled == []


def test_queue_capacity_and_tick_budget():
    h, now = setup_dispatch()
    seen = []
    h.mcp._get_session_info = lambda: seen.append(1) or {}
    requests = [h.mcp._enqueue_command({'type':'get_session_info'}) for _ in range(65)]
    assert 'not executed' in requests[-1].response['message']
    h.mcp.update_display()
    assert len(seen) == 8
    for _ in range(7): h.mcp.update_display()
    assert len(seen) == 64
    assert all(r.response['status'] == 'success' for r in requests[:-1])


def test_exact_deadline_prevents_late_write():
    h, now = setup_dispatch()
    req = h.mcp._enqueue_command({'type':'set_tempo','params':{'tempo':99}})
    now[0] = 10
    h.mcp.update_display()
    assert h.song.tempo == 120
    assert 'not executed' in req.response['message']


@pytest.mark.parametrize('name,budget', [('get_script_info',10),('get_session_info',10),
 ('duplicate_session_clip_to_arrangement',30),('create_audio_clip',60),('create_arrangement_audio_clip',60)])
def test_preserves_enqueue_budgets(name,budget):
    h, now = setup_dispatch()
    now[0] = 7
    req = h.mcp._enqueue_command({'type':name})
    assert req.deadline == 7+budget


def test_cancel_wins_start_and_completion_cannot_overwrite_uncertainty():
    h, now = setup_dispatch()
    req = h.mcp._enqueue_command({'type':'set_tempo','params':{'tempo':99}})
    response = req.cancel('Cancelled')
    h.mcp.update_display()
    assert h.song.tempo == 120
    assert req.response == response
    req2 = h.mcp._enqueue_command({'type':'get_session_info'})
    assert req2.begin()
    unknown = req2.cancel('Timeout')
    assert 'outcome is unknown' in unknown['message']
    req2.complete({'status':'success','result':{}})
    assert req2.response == unknown


def test_main_thread_handoff_refuses_and_foreign_direct_execution_refuses():
    h, now = setup_dispatch()
    response = h.mcp._process_command({'type':'set_tempo','params':{'tempo':99}})
    assert response['status'] == 'error'
    assert h.song.tempo == 120
    errors = []
    def foreign():
        try: h.mcp._execute_command({'type':'set_tempo','params':{'tempo':99}})
        except RuntimeError as e: errors.append(str(e))
    t = threading.Thread(target=foreign); t.start(); t.join(1)
    assert errors and h.song.tempo == 120


def test_worker_pending_timeout_never_mutates_later(monkeypatch):
    h, now = setup_dispatch()
    method, modifying, _, advertise = h.module.COMMANDS['set_tempo']
    monkeypatch.setitem(h.module.COMMANDS, 'set_tempo', (method, modifying, .01, advertise))
    responses = []
    t = threading.Thread(target=lambda: responses.append(h.mcp._process_command(
        {'type':'set_tempo','params':{'tempo':99}})))
    t.start(); t.join(1)
    assert not t.is_alive()
    assert 'not executed' in responses[0]['message']
    h.mcp.update_display()
    assert h.song.tempo == 120


def test_worker_running_timeout_is_unknown_and_not_replayed(monkeypatch):
    h, now = setup_dispatch()
    method, modifying, _, advertise = h.module.COMMANDS['set_tempo']
    monkeypatch.setitem(h.module.COMMANDS, 'set_tempo', (method, modifying, .05, advertise))
    admitted = threading.Event()
    returned = threading.Event()
    put = h.mcp._command_queue.put_nowait
    h.mcp._command_queue.put_nowait = lambda req: (put(req), admitted.set())
    calls = []
    def held(tempo):
        calls.append(threading.get_ident())
        assert returned.wait(1)
        h.song.tempo = tempo
        return {'tempo':tempo}
    h.mcp._set_tempo = held
    responses = []
    def worker():
        responses.append(h.mcp._process_command({'type':'set_tempo','params':{'tempo':99}}))
        returned.set()
    t = threading.Thread(target=worker); t.start()
    assert admitted.wait(1)
    h.mcp.update_display(); t.join(1)
    assert 'outcome is unknown' in responses[0]['message']
    h.mcp.update_display()
    assert calls == [threading.get_ident()]
    assert h.song.tempo == 99


def test_completion_wins_cancellation_and_cancel_start_race():
    h, now = setup_dispatch()
    req = h.mcp._enqueue_command({'type':'get_session_info'})
    h.mcp.update_display()
    assert req.cancel('late timeout')['status'] == 'success'
    for _ in range(30):
        req = h.module._CommandRequest({}, 10, lambda:0)
        barrier = threading.Barrier(2)
        outcomes = []
        def cancel():
            barrier.wait()
            outcomes.append(req.cancel('timeout'))
        t = threading.Thread(target=cancel); t.start()
        barrier.wait()
        started = req.begin()
        t.join(1)
        assert not t.is_alive()
        assert ('outcome is unknown' in outcomes[0]['message']) is started
        assert req.begin() is False
