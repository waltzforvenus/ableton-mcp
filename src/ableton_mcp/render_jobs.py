"""Server-owned export queue. No UI adapters or process configuration here.

The journal lease is process-lifetime, non-expiring and local. A new owner
reconciles interrupted jobs, never replays them. Stage writes precede side
effects. Cancellation stops admission, not a render already admitted.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
try:
    import fcntl
except ImportError:  # no unsafe atomic-rename-only ownership fallback
    fcntl = None
import json
import os
import tempfile
import threading
import uuid

TERMINAL = {"succeeded", "failed", "cancelled", "needs_reconciliation"}
STAGES = ("queued", "preflight", "configuring", "rendering", "verifying")


class AtomicJobStore:
    def __init__(self, path):
        self.path = os.path.abspath(os.path.expanduser(path))
        self._lease = None
        self.closed = False

    def load(self):
        if self.closed:
            raise RuntimeError("stale journal owner")
        if self._lease is None:
            if fcntl is None:
                raise RuntimeError("export journal owner locking unsupported")
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            lease = open(self.path + ".owner", "a+")
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                lease.close()
                raise RuntimeError("export journal has an active owner") from None
            self._lease = lease
        try:
            with open(self.path, encoding="utf8") as stream:
                data = json.load(stream)
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict) or any(
                not isinstance(v, dict) or v.get("job_id") != k
                or v.get("state") not in set(STAGES) | TERMINAL
                for k, v in data.items()):
            raise ValueError("invalid export journal; manual reconciliation required")
        return data

    def save(self, data):
        if self.closed or self._lease is None:
            raise RuntimeError("stale journal owner")
        payload = json.dumps(data, allow_nan=False, sort_keys=True)
        fd, path = tempfile.mkstemp(prefix=".export-jobs-", dir=os.path.dirname(self.path))
        try:
            with os.fdopen(fd, "w", encoding="utf8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, self.path)
            directory = os.open(os.path.dirname(self.path), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def close(self):
        self.closed = True
        if self._lease is not None:
            self._lease.close()
            self._lease = None


class JobCancelled(Exception):
    """Internal cooperative stop before render admission, not native cancel."""


class RenderJobService:
    def __init__(self, store, executor, clock, runner, owner):
        self.store, self.executor, self.clock = store, executor, clock
        self.runner, self.owner = runner, owner
        self._condition = threading.Condition(threading.RLock())
        self._jobs = None
        self._fault = None
        self._worker = None
        self._scheduled = False
        self._closed = False

    def _load(self):
        if self.store.closed:
            raise RuntimeError("stale journal owner")
        if self._jobs is None:
            data = self.store.load()
            interrupted = False
            for job in data.values():
                if job["state"] not in TERMINAL:
                    job.update(state="needs_reconciliation", reason="process_interrupted",
                               updated_at=self.clock())
                    interrupted = True
            if interrupted:
                self.store.save(data)
            self._jobs = data

    def _save(self, data):
        try:
            self.store.save(data)
        except Exception as exc:
            self._fault = str(exc)
            # Retain observed results in memory, explicitly non-durable. Disk
            # stays on its last atomic revision and restart reconciles it.
            self._jobs = data
            self._condition.notify_all()
            raise
        self._jobs = data
        self._condition.notify_all()

    def _update(self, job_id, **fields):
        data = copy.deepcopy(self._jobs)
        data[job_id].update(fields, updated_at=self.clock())
        self._save(data)

    def _status(self, job_id):
        result = copy.deepcopy(self._jobs[job_id])
        if self._fault:
            result.update(state="needs_reconciliation", journal_error=self._fault)
        result["blocked_by_reconciliation"] = result["state"] == "queued" and any(
            j["state"] == "needs_reconciliation" for j in self._jobs.values())
        result["retry_safe"] = False
        result["verification_scope"] = "existing_renderer_checks; A1b_pending"
        return result

    def status(self, job_id):
        with self._condition:
            self._load()
            return self._status(job_id)

    def start(self, key, request):
        if not isinstance(key, str) or not key.strip() or len(key) > 256:
            raise ValueError("idempotency_key must contain 1..256 characters")
        canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if not isinstance(request, dict) or len(canonical) > 65536:
            raise ValueError("request must be a bounded JSON object")
        with self._condition:
            self._load()
            for job in self._jobs.values():
                if job["idempotency_key"] == key:
                    if job["canonical_request"] != canonical:
                        raise ValueError("idempotency key already names a different request")
                    return self._status(job["job_id"])
            if self._closed or self._fault:
                raise RuntimeError("export queue unavailable; reconciliation required")
            if any(j["state"] == "needs_reconciliation" for j in self._jobs.values()):
                raise RuntimeError("active reconciliation blocks new exports")
            if sum(j["state"] not in TERMINAL for j in self._jobs.values()) >= 64:
                raise RuntimeError("export queue full")
            job_id = str(uuid.uuid4())
            data = copy.deepcopy(self._jobs)
            data[job_id] = dict(job_id=job_id, state="queued", idempotency_key=key,
                               canonical_request=canonical, request=json.loads(canonical),
                               owner=self.owner(), cancel_requested=False,
                               created_at=self.clock(), updated_at=self.clock())
            self._save(data)  # accepted only after durable write
            if not self._scheduled:
                self._scheduled = True
                try:
                    self.executor.submit(self._drain)
                except Exception:
                    self._scheduled = False
                    self._update(job_id, state="failed", reason="executor_refused")
                    raise
            return self._status(job_id)

    def cancel(self, job_id):
        with self._condition:
            self._load()
            job = self._jobs[job_id]
            if job["state"] not in TERMINAL:
                fields = {"cancel_requested": True}
                if job["state"] == "queued":
                    fields["state"] = "cancelled"
                self._update(job_id, **fields)
            return self._status(job_id)

    def _stage(self, job_id, stage):
        with self._condition:
            self._load()
            job = self._jobs[job_id]
            if self._fault:
                raise RuntimeError("journal unavailable")
            if stage not in STAGES or STAGES.index(stage) < STAGES.index(job["state"]):
                raise ValueError("invalid export stage transition")
            if job["state"] in ("preflight", "configuring") and job["cancel_requested"]:
                raise JobCancelled()
            if self.owner() != job["owner"]:
                raise RuntimeError("stale Live lock owner")
            self._update(job_id, state=stage)

    def _drain(self):
        try:
            while True:
                with self._condition:
                    self._load()
                    if self._fault or self._closed or any(
                            j["state"] == "needs_reconciliation" for j in self._jobs.values()):
                        return
                    pending = next((j for j in self._jobs.values() if j["state"] == "queued"), None)
                    if pending is None:
                        return
                    job_id = pending["job_id"]
                    self._worker = threading.get_ident()
                    self._update(job_id, state="preflight")
                result = None
                error = None
                cancelled = False
                try:
                    self._stage(job_id, "preflight")
                    result = self.runner(copy.deepcopy(pending["request"]),
                                         lambda stage: self._stage(job_id, stage))
                except JobCancelled:
                    cancelled = True
                except Exception as exc:
                    error = {"type": type(exc).__name__, "reason": str(exc),
                             "details": getattr(exc, "details", {})}
                with self._condition:
                    job = self._jobs[job_id]
                    phase = job["state"]
                    if cancelled:
                        state = "cancelled"
                    elif error:
                        state = "failed" if phase == "preflight" else "needs_reconciliation"
                    elif job["cancel_requested"]:
                        state = "needs_reconciliation"
                    elif isinstance(result, dict) and result.get("ok") is True and (
                            result.get("dry_run") is True
                            or (phase == "verifying" and result.get("files")
                                and all(f.get("ok") is True for f in result["files"]))
                            or (result.get("mode") and result.get("after"))):
                        state = "succeeded"
                    else:
                        state = "needs_reconciliation"
                    self._update(job_id, state=state, result=result, error=error)
        except Exception as exc:
            # Journal/lease loss: never proceed with another native action.
            with self._condition:
                self._fault = str(exc)
        finally:
            with self._condition:
                self._worker = None
                self._scheduled = False
                # A caller may enqueue between the empty-queue observation
                # and this finally block. Do not strand that accepted job.
                if not self._fault and not self._closed and self._jobs and not any(
                        j["state"] == "needs_reconciliation" for j in self._jobs.values()
                ) and any(j["state"] == "queued" for j in self._jobs.values()):
                    self._scheduled = True
                    try:
                        self.executor.submit(self._drain)
                    except Exception as exc:
                        self._scheduled = False
                        self._fault = str(exc)
                self._condition.notify_all()

    def wait(self, job_id, timeout=None):
        with self._condition:
            self._load()
            self._condition.wait_for(lambda: self._fault or self._jobs[job_id]["state"] in TERMINAL
                                     or self._status(job_id)["blocked_by_reconciliation"],
                                     timeout=timeout)
            return self._status(job_id)  # caller timeout never cancels

    def check_access(self):
        with self._condition:
            self._load()
            if self._worker == threading.get_ident():
                return
            if self._fault or any(j["state"] not in {"succeeded", "failed", "cancelled"}
                                  for j in self._jobs.values()):
                raise RuntimeError("active export job requires completion or reconciliation")

    @contextmanager
    def idle_guard(self):
        # Keep admission serialized through a save-verified handoff.
        if self._worker == threading.get_ident():
            # Job-owned checkpoint/save must not hold the journal mutex over
            # UI work: status/cancel remain responsive during that stage.
            yield
            return
        with self._condition:
            self.check_access()
            yield

    def close(self):
        with self._condition:
            if self._worker is not None:
                raise RuntimeError("active export worker cannot be closed")
            self._closed = True
            self.store.close()
