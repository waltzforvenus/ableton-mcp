"""Job lifecycle against local journals and injected executors; no native UI."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading
import pytest
from ableton_mcp.render_jobs import AtomicJobStore, RenderJobService


class ManualExecutor:
    def __init__(self):
        self.pending = []
    def submit(self, fn, *args):
        self.pending.append(lambda: fn(*args))
    def run(self):
        while self.pending:
            self.pending.pop(0)()


def jobs(tmp_path, runner, executor=None, clock=lambda: 0, owner=lambda: "owner"):
    store = AtomicJobStore(str(tmp_path / "jobs.json"))
    return RenderJobService(store, executor or ManualExecutor(), clock, runner, owner)


@contextmanager
def threaded_jobs(tmp_path, runner, **kwargs):
    # Terminal visibility is not executor quiescence. Always join the worker
    # before releasing its journal, including assertion/error cleanup paths.
    j = None
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            j = jobs(tmp_path, runner, executor, **kwargs)
            yield j
    finally:
        if j is not None:
            j.close()


def good(request, stage):
    stage("configuring")
    stage("rendering")
    stage("verifying")
    return {"ok": True, "files": [{"ok": True, "path": "fixture.wav"}]}


def test_idempotency_order_and_collision(tmp_path):
    calls = []
    ex = ManualExecutor()
    j = jobs(tmp_path, lambda r, s: (calls.append(r["n"]), good(r, s))[1], ex)
    a = j.start("a", {"n": 1})
    assert j.start("a", {"n": 1})["job_id"] == a["job_id"]
    with pytest.raises(ValueError, match="idempotency"):
        j.start("a", {"n": 2})
    b = j.start("b", {"n": 2})
    assert calls == []
    ex.run()
    assert calls == [1, 2]
    assert j.status(a["job_id"])["state"] == "succeeded"
    assert j.status(b["job_id"])["state"] == "succeeded"
    j.close()


def test_status_disconnect_long_job_and_cancel_after_admission(tmp_path):
    entered, release = threading.Event(), threading.Event()
    now = [0]
    def run(r, stage):
        stage("rendering")
        entered.set()
        assert release.wait(3)
        now[0] += 120
        stage("verifying")
        return {"ok": True, "files": [{"ok": True}]}
    with threaded_jobs(tmp_path, run, clock=lambda: now[0]) as j:
        a = j.start("long", {})
        try:
            assert entered.wait(1)
            assert j.status(a["job_id"])["state"] == "rendering"
            assert j.start("long", {})["job_id"] == a["job_id"]
            assert j.cancel(a["job_id"])["cancel_requested"]
            with pytest.raises(RuntimeError, match="active"):
                with j.idle_guard():
                    pass
        finally:
            release.set()
        end = j.wait(a["job_id"], 2)
        assert end["state"] == "needs_reconciliation"
        assert end["result"]["ok"] is True  # evidence not discarded
        assert end["updated_at"] == 120


def test_cancel_queued_and_before_render_prevents_native_start(tmp_path):
    calls = []
    ex = ManualExecutor()
    j = jobs(tmp_path, lambda r, s: calls.append(r), ex)
    a = j.start("a", {})
    assert j.cancel(a["job_id"])["state"] == "cancelled"
    ex.run()
    assert calls == []
    j.close()


def test_restart_nonterminal_never_replays_and_blocks_handoff(tmp_path):
    ex = ManualExecutor()
    j = jobs(tmp_path, good, ex)
    a = j.start("a", {})
    # Simulate loss of process: release only the local journal lease.
    j.store.close()
    newer = jobs(tmp_path, good)
    assert newer.status(a["job_id"])["state"] == "needs_reconciliation"
    assert newer.start("a", {})["job_id"] == a["job_id"]
    with pytest.raises(RuntimeError, match="active"):
        with newer.idle_guard():
            pass
    ex.run()  # stale owner must not execute
    assert newer.status(a["job_id"])["state"] == "needs_reconciliation"
    newer.close()


def test_stale_live_owner_refuses_before_runner(tmp_path):
    owner = ["first"]
    calls = []
    ex = ManualExecutor()
    j = jobs(tmp_path, lambda r, s: calls.append(r), ex, owner=lambda: owner[0])
    a = j.start("a", {})
    owner[0] = "second"
    ex.run()
    assert calls == []
    assert j.status(a["job_id"])["state"] == "failed"
    j.close()


def test_atomic_initial_write_failure_never_schedules(tmp_path, monkeypatch):
    ex = ManualExecutor()
    j = jobs(tmp_path, good, ex)
    monkeypatch.setattr(j.store, "save", lambda data: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        j.start("a", {})
    assert ex.pending == []
    j.close()


def test_concurrent_journal_owner_refused(tmp_path):
    first = jobs(tmp_path, good)
    first.start("a", {})
    second = jobs(tmp_path, good)
    with pytest.raises(RuntimeError, match="owner"):
        second.status("anything")
    first.store.close()


@pytest.mark.parametrize("result", [{"ok": True}, {"ok": False, "error": "timeout"}])
def test_unknown_result_never_claims_completed_render(tmp_path, result):
    ex = ManualExecutor()
    def run(r, stage):
        stage("rendering")
        return result
    j = jobs(tmp_path, run, ex)
    a = j.start("a", {})
    ex.run()
    assert j.status(a["job_id"])["state"] == "needs_reconciliation"
    j.close()


def test_service_submission_keeps_authority_and_blocks_handoff(tmp_path):
    from ableton_mcp.services import AbletonService
    from ableton_mcp.live_lock import LiveLock
    from golden_runner import AllCapabilitiesHandshake
    class UI:
        available = True
    lock = LiveLock(str(tmp_path / "live.lock"))
    lock.acquire("fixture-owner")
    ex = ManualExecutor()
    j = jobs(tmp_path, good, ex)
    service = AbletonService(None, AllCapabilitiesHandshake(), lock=lock, ui=UI(), render_jobs=j)
    with pytest.raises(Exception, match="not_allowed"):
        service.start_export_audio("no-consent", {"output_dir": str(tmp_path), "file_stem": "x"})
    a = service.start_export_audio("yes", {"output_dir": str(tmp_path), "file_stem": "x",
                                           "allow_ui_automation": True})
    with pytest.raises(RuntimeError, match="active"):
        service.release_live_lock("fixture-owner", set_path="unused")
    assert service.get_export_job(a["job_id"])["state"] == "queued"
    assert service.cancel_export_job(a["job_id"])["state"] == "cancelled"
    j.close()


def test_pre_render_cancel_during_configuring_stops_render(tmp_path):
    entered, release = threading.Event(), threading.Event()
    invoked = []
    def run(r, stage):
        stage("configuring")
        entered.set()
        assert release.wait(3)
        stage("rendering")
        invoked.append(True)
    with threaded_jobs(tmp_path, run) as j:
        a = j.start("cancel", {})
        try:
            assert entered.wait(1)
            j.cancel(a["job_id"])
        finally:
            release.set()
        assert j.wait(a["job_id"], 2)["state"] == "cancelled"
        assert invoked == []


def test_stage_journal_failure_stops_render_and_blocks_future_work(tmp_path, monkeypatch):
    ex = ManualExecutor()
    invoked = []
    j = jobs(tmp_path, lambda r, s: (s("rendering"), invoked.append(True)), ex)
    a = j.start("a", {})
    original = j.store.save
    def fail_render(data):
        if data[a["job_id"]]["state"] == "rendering":
            raise OSError("disk full at admission")
        original(data)
    monkeypatch.setattr(j.store, "save", fail_render)
    ex.run()
    assert invoked == []
    assert j.status(a["job_id"])["state"] == "needs_reconciliation"
    with pytest.raises(RuntimeError):
        j.start("b", {})
    j.close()


@pytest.mark.parametrize("duration", [1, 120])
def test_disconnected_caller_can_retrieve_same_completed_job(tmp_path, duration):
    now = [0]
    ex = ManualExecutor()
    calls = []
    def run(r, stage):
        calls.append(r)
        now[0] += duration
        return good(r, stage)
    j = jobs(tmp_path, run, ex, lambda: now[0])
    accepted = j.start("disconnect", {"mix": "fixture"})
    # The caller drops its reference/connection, not the server-owned executor.
    job_id = accepted["job_id"]
    del accepted
    ex.run()
    assert j.start("disconnect", {"mix": "fixture"})["job_id"] == job_id
    assert j.status(job_id)["state"] == "succeeded"
    assert j.status(job_id)["updated_at"] == duration
    assert len(calls) == 1
    j.close()


def test_public_tools_keep_one_job_and_return_structured_status(tmp_path):
    import json
    from types import SimpleNamespace
    from ableton_mcp import tools
    from ableton_mcp.services import AbletonService
    from golden_runner import AllCapabilitiesHandshake
    ex = ManualExecutor()
    j = jobs(tmp_path, good, ex)
    service = AbletonService(None, AllCapabilitiesHandshake(),
                             ui=SimpleNamespace(available=True), render_jobs=j)
    ctx = SimpleNamespace(request_context=SimpleNamespace(
        lifespan_context=SimpleNamespace(service=service)))
    args = {"output_dir": str(tmp_path), "file_stem": "fixture", "allow_ui_automation": True}
    first = json.loads(tools.start_export_audio(ctx, "public", args))
    second = json.loads(tools.start_export_audio(ctx, "public", dict(args, sample_rate=44100)))
    assert first["job_id"] == second["job_id"]  # canonical defaults
    ex.run()
    status = json.loads(tools.get_export_job(ctx, first["job_id"]))
    assert status["state"] == "succeeded"
    assert status["result"]["files"][0]["path"] == "fixture.wav"
    assert "native_readiness_probe_pending" in status["verification_policy"]
    assert json.loads(tools.cancel_export_job(ctx, first["job_id"]))["state"] == "succeeded"
    j.close()


def test_wait_reports_queue_blocked_by_uncertain_previous_job(tmp_path):
    ex = ManualExecutor()
    j = jobs(tmp_path, lambda r, s: {"ok": False}, ex)
    j.start("uncertain", {})
    b = j.start("queued", {})
    ex.run()
    result = j.wait(b["job_id"], timeout=0)
    assert result["state"] == "queued"
    assert result["blocked_by_reconciliation"] is True
    j.close()


def test_atomic_replace_failure_preserves_last_journal(tmp_path, monkeypatch):
    import os
    ex = ManualExecutor()
    j = jobs(tmp_path, good, ex)
    a = j.start("a", {})
    path = tmp_path / "jobs.json"
    before = path.read_bytes()
    def fail(*args):
        raise OSError("replace failed")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        j.cancel(a["job_id"])
    assert path.read_bytes() == before
    assert list(tmp_path.glob(".export-jobs-*")) == []
    assert j.status(a["job_id"])["state"] == "needs_reconciliation"
    j.close()


def test_real_renderer_stage_boundaries_and_cancel_cleanup(tmp_path):
    from ableton_mcp.render_jobs import JobCancelled
    from test_render import FakeExportUI, _export
    ui = FakeExportUI(tmp_path)
    stages = []
    def stage(name):
        stages.append(name)
        if name == "rendering":
            assert ui.open
            assert not any(call[:2] == ("panel", "save") for call in ui.calls)
            raise JobCancelled()
    with pytest.raises(JobCancelled):
        _export(tmp_path, ui, job_stage=stage)
    assert stages == ["configuring", "rendering"]
    assert not ui.open
    assert not (tmp_path / "renders" / "mix.wav").exists()


def test_production_composition_is_lazy_and_injects_job_service(tmp_path):
    from ableton_mcp.app import Settings, build_deps
    d = build_deps(Settings(lock_file=str(tmp_path / "live.lock")))
    try:
        assert d.service._render_jobs is d.render_jobs
        assert d.render_jobs.store.path == str(tmp_path / "live.lock.exports.json")
        assert list(tmp_path.iterdir()) == []  # no lock/socket/UI/config changes
    finally:
        d.render_executor.shutdown()
        d.render_jobs.close()


@pytest.mark.parametrize("field,value", [("allow_ui_automation", "false"), ("overwrite", "false"),
                                         ("mp3", 1), ("sample_rate", True), ("track", "1")])
def test_json_request_rejects_type_coercion_before_admission(tmp_path, field, value):
    from types import SimpleNamespace
    from ableton_mcp.services import AbletonService
    from golden_runner import AllCapabilitiesHandshake
    ex = ManualExecutor()
    j = jobs(tmp_path, good, ex)
    service = AbletonService(None, AllCapabilitiesHandshake(),
                             ui=SimpleNamespace(available=True), render_jobs=j)
    request = {"output_dir": str(tmp_path), "file_stem": "x", "allow_ui_automation": True,
               field: value}
    with pytest.raises(ValueError, match="type"):
        service.start_export_audio("bad", request)
    assert ex.pending == []
    assert not (tmp_path / "jobs.json").exists()
    j.close()


@pytest.mark.parametrize("operation", ["export", "bounce"])
def test_synchronous_service_delegates_and_waits_on_same_queue(tmp_path, monkeypatch, operation):
    from types import SimpleNamespace
    from ableton_mcp import render
    from ableton_mcp.services import AbletonService
    from golden_runner import AllCapabilitiesHandshake
    calls = []
    if operation == "export":
        # Preserve the real signature, because boundary defaults/types come
        # from it, while replacing only the native renderer dependency.
        import functools
        original = render.export_audio
        @functools.wraps(original)
        def fake(service, ui, **kwargs):
            calls.append(kwargs)
            return good({}, kwargs["job_stage"])
        monkeypatch.setattr(render, "export_audio", fake)
    else:
        def fake(service, ui, **kwargs):
            calls.append(kwargs)
            kwargs["job_stage"]("rendering")
            kwargs["job_stage"]("verifying")
            return {"ok": True, "mode": "freeze", "after": {"is_frozen": True}}
        monkeypatch.setattr(render, "bounce_track", fake)
    with threaded_jobs(tmp_path, lambda r, s: service.run_export_job(r, s)) as j:
        released, published = threading.Event(), threading.Event()
        condition = j._condition
        class HoldPublishedTerminal:
            """Force terminal visibility before worker-finally clears ownership."""
            def __getattr__(self, name):
                return getattr(condition, name)
            def __enter__(self):
                return condition.__enter__()
            def __exit__(self, *args):
                hold = (j._worker == threading.get_ident() and j._jobs and
                        any(job["state"] == "succeeded" for job in j._jobs.values()))
                result = condition.__exit__(*args)
                if hold:
                    published.set()
                    assert released.wait(3)
                return result
        monkeypatch.setattr(j, "_condition", HoldPublishedTerminal())
        service = AbletonService(None, AllCapabilitiesHandshake(), render_jobs=j,
                                 ui=SimpleNamespace(available=True))
        try:
            if operation == "export":
                result = service.export_audio(output_dir=str(tmp_path), file_stem="mix",
                                              allow_ui_automation=True)
            else:
                result = service.bounce_track(0, "freeze", allow_ui_automation=True)
            assert published.wait(1)
            assert result["ok"] is True
            assert result["job_state"] == "succeeded"
            assert service.get_export_job(result["job_id"])["state"] == "succeeded"
            assert len(calls) == 1
            # A terminal result must NOT weaken the active-worker close guard.
            with pytest.raises(RuntimeError, match="active export worker"):
                j.close()
        finally:
            released.set()
    assert j.store.closed
    assert j._worker is None


def test_old_result_does_not_gain_new_verification_proof(tmp_path):
    ex = ManualExecutor()
    j = jobs(tmp_path, good, ex)
    first = j.start('legacy', {})
    ex.run()
    status = j.status(first['job_id'])
    assert status['verification_scope'] == 'legacy_renderer_evidence_only'
    assert 'native_readiness_probe_pending' in status['verification_policy']
    j.close()
