"""Model: the shared Live lock — one agent edits Live at a time.

Several agents (and the human) drive one Live instance. Without a lock they
collide: a bounce job ran while the user was soloing and arming tracks, and
two agents handed the set back and forth in chat with nothing enforcing it.
This module is the server half of a lock that other tools share — a plain
JSON file, so a command-line helper, a second server process, or a person
with ``cat`` all see the same holder.

The file format and the mutex are a contract with those other users, so
they are deliberately boring:

- the lock file (``ABLETON_MCP_LOCK_FILE``, default
  ``~/.ableton-mcp/live.lock``) holds ``{"holder", "task", "acquired",
  "acquired_ts", "last_edit_ts"}``; written atomically (temp + rename);
- every read-modify-write happens under an exclusive ``flock`` on
  ``<lock file>.mutex``;
- a one-line history is appended to ``<lock file>.log`` (local only — the
  same file the helper appends to; nothing leaves the machine).

Semantics, chosen so a lock can never be lost silently:

- one holder; **it never expires** — a crashed agent's lock stays until it
  is released properly or the user removes it by hand;
- release happens only after a **verified save**: the ``.als`` must be at
  least as new as the holder's last edit (``last_edit_ts``), and — where the
  window titles can be read — Live must show that set, without an unsaved
  marker and without a Save/Export dialog open.

A process instance owns only records it acquired or resumed through an injected
trusted verifier. Holder names are labels, not credentials. Legacy records and
unknown save state fail closed; there is no timeout-based takeover. Transfer is
an atomic generation-checked replace, never a release/reacquire gap. The default
composition has no trusted identity/save verifier and cannot resume or transfer.

No string formatting for the model here — refusals are :class:`LockError`
carrying a reason code and data; presenters.py words them.
"""

from __future__ import annotations

import json
import os
import time
import uuid
import copy
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional

try:  # POSIX; the mutex degrades to atomic-rename-only where flock is absent
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


DEFAULT_LOCK_FILE = "~/.ableton-mcp/live.lock"


class LockError(Exception):
    """A lock refusal. ``reason`` is a stable code; ``details`` the data the
    presenter interpolates (the current lock record, mtimes, window titles).

    Reasons: ``held_by_other``, ``not_held`` (no lock and one is required),
    ``not_holder`` (release by someone who does not hold it),
    ``set_missing``, ``set_older_than_edit``, ``live_unsaved``,
    ``save_failed`` (release's own save_first failed), ``disabled`` (no
    lock configured in this process)."""

    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


def _now_iso(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), timezone.utc).isoformat(timespec="seconds")


class LiveLock:
    """The lock file, as one server process sees it.

    ``holder`` is the name this process acquired (or adopted) the lock
    under; ``None`` until ``acquire`` succeeds. ``clock`` is injectable so
    the save-versus-edit ordering is testable without sleeping.
    """

    def __init__(self, path: str = DEFAULT_LOCK_FILE, require: bool = True,
                 clock: Callable[[], float] = time.time,
                 authorization_verifier: Optional[Callable[..., bool]] = None) -> None:
        self.path = os.path.expanduser(path)
        self.require = require
        self.holder: Optional[str] = None
        self._clock = clock
        self._instance_id = uuid.uuid4().hex
        self._authorization_verifier = authorization_verifier
        self._operation_local = threading.local()

    # ── file primitives (the shared contract) ────────────────────────────

    def read(self) -> Optional[Dict[str, Any]]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except ValueError:
            # A torn or hand-mangled file must not read as "unlocked" — that
            # would let two agents in. Surface it as held by an unknown party.
            return {"holder": "<unreadable lock file>", "task": "",
                    "acquired": None, "acquired_ts": None,
                    "last_edit_ts": None}
        return data if isinstance(data, dict) and data else {"holder": "<unreadable lock file>"}

    def _write(self, data: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)

    def _log(self, message: str) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path + ".log", "a", encoding="utf-8") as f:
                f.write(f"{_now_iso(self._clock)} {message}\n")
        except OSError:
            pass  # the history is a courtesy; never fail a lock op over it

    @contextmanager
    def _mutex(self) -> Iterator[None]:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path + ".mutex", "w") as m:
            if fcntl is not None:
                fcntl.flock(m, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(m, fcntl.LOCK_UN)

    # ── operations ───────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        data = self.read()
        if data and isinstance(data.get("acquired_ts"), (int, float)):
            data = dict(data)
            data["age_min"] = round((self._clock() - data["acquired_ts"]) / 60, 1)
        return {
            "lock": data,
            "held_by_this_server": bool(
                data and self.holder is not None
                and self._owns(data)),
            "this_server_holder": self.holder,
            "require_lock": self.require,
            "lock_file": self.path,
        }

    def acquire(self, holder: str, task: str = "") -> Dict[str, Any]:
        holder = (holder or "").strip()
        if not holder:
            raise ValueError("holder must be a non-empty name (session key or task)")
        with self._mutex():
            data = self.read()
            if data and data.get("holder") != holder:
                raise LockError("held_by_other", lock=data)
            if data and not self._owns(data):
                raise LockError("owner_proof_required", lock=data)
            adopted = data is not None
            t = self._clock()
            data = data or {"holder": holder, "task": task,
                            "acquired": _now_iso(self._clock), "acquired_ts": t,
                            "instance_id": self._instance_id,
                            "generation": uuid.uuid4().hex, "save_state": "unknown"}
            data["last_edit_ts"] = data.get("last_edit_ts")
            self._write(data)
            self._log(f"acquire {holder} {task!r}"
                      + (" (adopted by ableton-mcp)" if adopted else " (ableton-mcp)"))
        self.holder = holder
        return {"acquired": True, "adopted": adopted, "lock": data}

    def _owns(self, data):
        return (self.holder is not None and data.get("holder") == self.holder
                and data.get("instance_id") == self._instance_id)

    def _authorize(self, action, data, proof, **context):
        if self._authorization_verifier is None:
            raise LockError("transfer_unsupported", action=action)
        # Trusted injected boundary verifies identity and fresh current-set/save
        # evidence. Caller-supplied assertions are not themselves evidence.
        context["lock"] = copy.deepcopy(data)
        if self._authorization_verifier(action, context, proof) is not True:
            raise LockError("proof_rejected", action=action)

    def _generation(self, data, expected):
        if not data or not expected or data.get("generation") != expected:
            raise LockError("generation_conflict", lock=data)

    @staticmethod
    def _quiescent(data):
        if data.get("operations"):
            raise LockError("operations_unresolved", lock=data)

    @contextmanager
    def operation(self, command, edit=False, exclusive=False):
        """Persist admission before dispatch. Crashes/errors never clear it.

        Nested same-thread service calls share a reservation. Other instances
        see the reservation under the same file mutex as ownership changes.
        A successful transport response is not proof of an uncertain native
        operation's completion; callers must mark structured uncertainty too.
        """
        token = getattr(self._operation_local, "token", None)
        outer = token is None
        token = token or uuid.uuid4().hex
        state = getattr(self._operation_local, "state", None) or {"uncertain": False}
        with self._mutex():
            self.check_write(command)
            data = self.read()
            if data:
                operations = data.setdefault("operations", {})
                if outer:
                    # Unresolved prior attempts are never implicitly reconciled.
                    if ((exclusive and operations)
                            or any(v.get("uncertain") or v.get("exclusive")
                                   for v in operations.values())):
                        raise LockError("operations_unresolved", lock=data)
                    operations[token] = {"command": command, "uncertain": False,
                                         "exclusive": exclusive}
                if edit:
                    data.update(last_edit_ts=self._clock(), save_state="dirty",
                                generation=uuid.uuid4().hex)
                    data.pop("save_observation", None)
                self._write(data)
        if outer:
            self._operation_local.token = token
            self._operation_local.state = state
        try:
            yield state
        except BaseException:
            if not state.pop("proven_refusal", False):
                state["uncertain"] = True
            raise
        finally:
            if outer:
                try:
                    with self._mutex():
                        data = self.read()
                        if data and self._owns(data):
                            operations = data.get("operations", {})
                            if state["uncertain"]:
                                operations[token] = {"command": command, "uncertain": True}
                                data["save_state"] = "uncertain"
                                data.pop("save_observation", None)
                            else:
                                operations.pop(token, None)
                            self._write(data)
                finally:
                    self._operation_local.token = None
                    self._operation_local.state = None

    def observe_save(self, result, generation):
        """Internal workflow provenance, never an attestation of current path."""
        with self._mutex():
            data = self.read()
            if not data or not self._owns(data) or data.get("generation") != generation:
                return
            token = getattr(self._operation_local, "token", None)
            if any(k != token for k in data.get("operations", {})):
                return
            verified = result.get("verified")
            if result.get("saved") is not True or not isinstance(verified, dict):
                return
            if (not verified.get("mtime_advanced") or verified.get("unsaved_after") is not False
                    or not verified.get("set_path")
                    or verified.get("document_before") != verified.get("set_path")
                    or verified.get("document_after") != verified.get("set_path")
                    or verified.get("modal_after")):
                return
            data["save_observation"] = {"generation": generation,
                                        "observed": self._clock(),
                                        "evidence": copy.deepcopy(verified)}
            self._write(data)

    def resume(self, owner, owner_proof, expected_generation):
        with self._mutex():
            data = self.read()
            self._generation(data, expected_generation)
            self._quiescent(data)
            if data.get("holder") != owner:
                raise LockError("not_holder", lock=data)
            self._authorize("resume", data, owner_proof, owner=owner)
            data = dict(data, instance_id=self._instance_id,
                        generation=uuid.uuid4().hex)
            self._write(data)
        self.holder = owner
        return {"resumed": True, "lock": data}

    def transfer(self, from_owner, to_owner, save_proof, expected_generation):
        if not isinstance(to_owner, str) or not to_owner.strip():
            raise ValueError("to_owner must be nonempty")
        with self._mutex():
            data = self.read()
            self._generation(data, expected_generation)
            self._quiescent(data)
            if from_owner != self.holder or not self._owns(data):
                raise LockError("not_holder", lock=data)
            self._authorize("transfer", data, save_proof,
                            from_owner=from_owner, to_owner=to_owner)
            data = dict(data, holder=to_owner, instance_id=None,
                        generation=uuid.uuid4().hex, save_state="verified")
            self._write(data)
        self.holder = None
        return {"transferred": True, "lock": data}

    def check_write(self, command: str) -> None:
        """Raise unless this process may change Live's state right now."""
        data = self.read()
        if data is None:
            if self.require:
                raise LockError("not_held", command=command)
            return
        if not self._owns(data):
            raise LockError("held_by_other", lock=data, command=command)

    def touch(self) -> None:
        """Record an edit (``last_edit_ts``) — after every state change this
        process sends, so release can tell a save from a stale file."""
        if self.holder is None:
            return
        with self._mutex():
            data = self.read()
            if not data or not self._owns(data):
                return  # released or taken over meanwhile; nothing of ours to stamp
            data["last_edit_ts"] = self._clock()
            data["save_state"] = "dirty"
            data["generation"] = uuid.uuid4().hex
            data.pop("save_observation", None)
            self._write(data)

    def release(self, holder: str, set_path: str,
                live_windows: Optional[List[str]] = None,
                current_set_path: Optional[str] = None) -> Dict[str, Any]:
        """Release after a verified save. ``live_windows`` is Live's window
        titles when they could be read (None = unknown, release refused)."""
        with self._mutex():
            data = self.read()
            if not data:
                if holder == self.holder:
                    self.holder = None
                return {"released": False, "note": "no lock held"}
            if data.get("holder") != holder or not self._owns(data):
                raise LockError("not_holder", lock=data)
            self._quiescent(data)
            path = os.path.expanduser(set_path or "")
            if not path or not os.path.isfile(path):
                raise LockError("set_missing", set_path=set_path, lock=data)
            mtime = os.path.getmtime(path)
            last_edit = max(data.get("last_edit_ts") or 0, data.get("acquired_ts") or 0)
            if mtime < last_edit:
                raise LockError("set_older_than_edit", set_path=path,
                                set_mtime=mtime, last_edit_ts=last_edit, lock=data)
            if live_windows is None:
                raise LockError("save_evidence_unknown", lock=data)
            if live_windows is not None:
                problem = unsaved_problem(live_windows, path)
                if problem:
                    raise LockError("live_unsaved", problem=problem,
                                    live_windows=live_windows, set_path=path,
                                    lock=data)
            observation = data.get("save_observation") or {}
            evidence = observation.get("evidence") or {}
            if (observation.get("generation") != data.get("generation")
                    or evidence.get("set_path") != path
                    or evidence.get("mtime_after") != mtime
                    or current_set_path != path
                    or observation.get("observed", 0) < last_edit):
                raise LockError("save_evidence_unknown", lock=data)
            # Server-observed AXDocument before/after save and again at release
            # binds the path. Titles, Spotlight and caller paths alone cannot.
            os.remove(self.path)
            self._log(f"release {holder} saved={path}")
        if self.holder == holder:
            self.holder = None
        return {"released": True, "saved_set": path, "set_mtime": mtime,
                "live_windows": live_windows}


def unsaved_problem(windows: List[str], set_path: str) -> Optional[str]:
    """Why Live's window titles say the set is not safely saved, or None.

    Same rule as the command-line helper, so both refuse the same states:
    a main window naming the set must exist, carry no ``*`` unsaved marker,
    and no Export or Save dialog may be open."""
    name = os.path.splitext(os.path.basename(set_path))[0]
    titles = [w.strip() for w in windows if w and w.strip()]
    mains = [w for w in titles
             if w.split(" - Ableton")[0].split(" [")[0].rstrip("* ") == name]
    if not mains:
        return "different_set"
    if any("*" in w for w in mains):
        return "unsaved_marker"
    if any("Export" in w or "Save" in w for w in titles):
        return "dialog_open"
    return None
