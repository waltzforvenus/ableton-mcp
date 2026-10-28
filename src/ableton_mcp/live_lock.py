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

A server process holds the lock *as* the holder it acquired with. Another
process that acquired under the same holder name (the command-line helper,
typically) is adopted by calling ``acquire`` with that name. Every
state-changing wire command is checked against the file before it is sent:
held by someone else means refused, naming the holder; not held at all
means refused too unless the lock is optional (``require=False``).

No string formatting for the model here — refusals are :class:`LockError`
carrying a reason code and data; presenters.py words them.
"""

from __future__ import annotations

import json
import os
import time
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
                 clock: Callable[[], float] = time.time) -> None:
        self.path = os.path.expanduser(path)
        self.require = require
        self.holder: Optional[str] = None
        self._clock = clock

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
        return data if isinstance(data, dict) else None

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
                and data.get("holder") == self.holder),
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
            adopted = data is not None
            t = self._clock()
            data = data or {"holder": holder, "task": task,
                            "acquired": _now_iso(self._clock), "acquired_ts": t}
            data["last_edit_ts"] = data.get("last_edit_ts", t)
            self._write(data)
            self._log(f"acquire {holder} {task!r}"
                      + (" (adopted by ableton-mcp)" if adopted else " (ableton-mcp)"))
        self.holder = holder
        return {"acquired": True, "adopted": adopted, "lock": data}

    def check_write(self, command: str) -> None:
        """Raise unless this process may change Live's state right now."""
        data = self.read()
        if data is None:
            if self.require:
                raise LockError("not_held", command=command)
            return
        if self.holder is None or data.get("holder") != self.holder:
            raise LockError("held_by_other", lock=data, command=command)

    def touch(self) -> None:
        """Record an edit (``last_edit_ts``) — after every state change this
        process sends, so release can tell a save from a stale file."""
        if self.holder is None:
            return
        with self._mutex():
            data = self.read()
            if not data or data.get("holder") != self.holder:
                return  # released or taken over meanwhile; nothing of ours to stamp
            data["last_edit_ts"] = self._clock()
            self._write(data)

    def release(self, holder: str, set_path: str,
                live_windows: Optional[List[str]] = None) -> Dict[str, Any]:
        """Release after a verified save. ``live_windows`` is Live's window
        titles when they could be read (None = unknown, check skipped —
        the same rule the helper applies off macOS or without AX)."""
        with self._mutex():
            data = self.read()
            if not data:
                if holder == self.holder:
                    self.holder = None
                return {"released": False, "note": "no lock held"}
            if data.get("holder") != holder:
                raise LockError("not_holder", lock=data)
            path = os.path.expanduser(set_path or "")
            if not path or not os.path.isfile(path):
                raise LockError("set_missing", set_path=set_path, lock=data)
            mtime = os.path.getmtime(path)
            last_edit = data.get("last_edit_ts") or 0
            if mtime < last_edit:
                raise LockError("set_older_than_edit", set_path=path,
                                set_mtime=mtime, last_edit_ts=last_edit, lock=data)
            if live_windows is not None:
                problem = unsaved_problem(live_windows, path)
                if problem:
                    raise LockError("live_unsaved", problem=problem,
                                    live_windows=live_windows, set_path=path,
                                    lock=data)
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
    mains = [w for w in titles if name in w]
    if not mains:
        return "different_set"
    if any("*" in w for w in mains):
        return "unsaved_marker"
    if any("Export" in w or "Save" in w for w in titles):
        return "dialog_open"
    return None
