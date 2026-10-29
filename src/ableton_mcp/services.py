"""Model layer: ``AbletonService``, the domain service between the tool
controllers and the wire (docs/REFACTOR_PLAN.md §3.2).

One method per wire command, section-commented in the same domain order as
tools.py. Almost every method is honestly small — build the command's param
dict, hand it to ``_send`` — and ``_send`` consults the commands.py registry,
so capability/version gating happens in exactly one place instead of the
seven hand-written blocks the tool bodies used to carry. Multi-step
orchestration (decisions between sends) is Model logic and lives here too:
``load_drum_kit``'s three-call sequence and ``get_remote_script_info``'s
handshake/cache interplay.

Methods return raw dicts. No string formatting here — every model-facing
string, success and error alike, belongs to the View (presenters.py).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from typing import Any, Dict, List, Optional, Union

from . import audio_metrics, plugin_state, preset_integrity, render, warp_vision
from .commands import COMMANDS
from .handshake import CapabilityError, ScriptHandshake
from .live_lock import LiveLock, LockError
from .remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION
from .ui_macos import MacUI, summarize_windows


def _guarded(params: Dict[str, Any],
             expect_track_name: Optional[str]) -> Dict[str, Any]:
    """Add the caller's track-name guard to ``params`` — only if they gave one.

    Which is the whole point of the helper: the key is omitted entirely
    rather than sent as null. The Remote Script dispatches handlers as
    ``**params``, so a script older than the guard raises a bare TypeError
    on a keyword it has never heard of. Omitting it keeps every ordinary
    call working against those scripts, and leaves the guard as something
    a caller opts into.

    Be precise about what that opt-in costs on an old script: only the
    commands independently floored at 1.15.0 turn it into the friendly
    re-run-the-installer message. The rest — set_track_volume, set_clip_gain,
    move_arrangement_clip and the other long-standing rows — carry no floor,
    because flooring them would refuse every ORDINARY call on a 1.14.0 script
    to protect an optional argument. On those, passing expect_track_name to a
    pre-1.15.0 script surfaces as a TypeError from the script's ``**params``
    dispatch. Omitting it, which is the default, is always safe.

    Same shape as the optional criteria in ``jump_to_locator`` and
    ``trim_arrangement_clip``: presence, not value, is what the script
    branches on. The rule across this module is that a command's ordinary
    call shape is always sent (an explicit ``arrangement`` beats inheriting
    whichever default the installed script happens to have), while
    assertions, overrides and alternative addressing are sent only when
    used.
    """
    if expect_track_name is not None:
        params["expect_track_name"] = expect_track_name
    return params


def _json_len(obj: Any) -> int:
    return len(json.dumps(obj, indent=2))


def _fit_snapshot(snapshot: Dict[str, Any], max_chars: int) -> Dict[str, Any]:
    """Trim a snapshot to ``max_chars`` of rendered JSON, by whole tracks.

    Scoped snapshots still overflowed the output cap ~50 times in one
    agent's runs (median 16.6k tokens), and an overflow is worse than a
    short answer: the client truncates mid-JSON. So the reply keeps every
    non-track section and as many whole tracks, in order, as fit; the rest
    are named in ``omitted_tracks`` with ``continuation`` — the ``tracks``
    argument that fetches exactly them next. Never trims inside a track.
    """
    if _json_len(snapshot) <= max_chars:
        return snapshot
    tracks = list(snapshot.get("tracks") or [])

    def build(k: int) -> Dict[str, Any]:
        out = dict(snapshot)
        out["tracks"] = tracks[:k]
        omitted = tracks[k:]
        if omitted:
            out["truncated"] = True
            out["max_chars"] = max_chars
            out["omitted_tracks"] = [{"index": t.get("index"), "name": t.get("name")}
                                     for t in omitted]
            out["continuation"] = {"tracks": [t.get("index") for t in omitted]}
        return out

    # The largest whole-track prefix whose reply (omission list included)
    # fits; at least one track, so the answer always makes progress.
    lo, hi = 1, len(tracks)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _json_len(build(mid)) <= max_chars:
            lo = mid
        else:
            hi = mid - 1
    return build(min(lo, len(tracks)))


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class AudioAuthRequired(Exception):
    """An audible command (playback, firing a clip, arming) without the
    caller's explicit authorized=true while the audio gate is on."""

    def __init__(self, command: str) -> None:
        super().__init__(command)
        self.command = command


class AbletonService:
    """The Model: constructed once, in the composition root (app.build_deps).

    ``client`` is anything satisfying the ``AbletonClientProtocol`` seam
    (``send_command``); the Protocol itself lives in app.py, which imports
    this module — naming it here would be an import cycle for the sake of a
    type hint.
    """

    def __init__(self, client: Any, handshake: ScriptHandshake,
                 lock: Optional[LiveLock] = None,
                 ui: Optional[MacUI] = None,
                 allow_ui: bool = False,
                 evidence_dir: Optional[str] = None,
                 ffmpeg: Optional[str] = None,
                 require_audio_auth: bool = False) -> None:
        self._client = client
        self._handshake = handshake
        # Both optional so test doubles that predate them keep working: no
        # lock means no lock enforcement (the composition root always wires
        # one in production), no ui means every UI read is "unknown".
        self._lock = lock
        self._ui = ui
        # Server-wide consent to drive Live's UI (ABLETON_MCP_ALLOW_UI);
        # without it each UI-driving call must pass allow_ui_automation.
        self._allow_ui = allow_ui
        self._evidence_dir = evidence_dir
        self._ffmpeg = ffmpeg
        # Off unless the composition root turns it on, so test doubles and
        # goldens see the historical behaviour.
        self._require_audio_auth = require_audio_auth

    def _send(self, name: str,
              params: Optional[Dict[str, Any]] = None,
              authorized: bool = True) -> Dict[str, Any]:
        """Send one wire command, applying the registry's gate first.

        A ``COMMANDS[name].gated`` row routes through
        ``ScriptHandshake.require`` — which raises :class:`CapabilityError`
        carrying the friendly "re-run ``ableton-mcp-install-script``" text —
        before anything touches the wire, with the registry's
        ``min_script_version`` floor applied (capability names alone cannot
        tell a repaired script from a broken 1.7.0 that already advertises
        them, plan §4).
        """
        spec = COMMANDS[name]
        if spec.audible and self._require_audio_auth and not authorized:
            # First of all: making sound without the user's OK is the one
            # thing no lock or gate downstream can undo.
            raise AudioAuthRequired(name)
        guarded = (self._lock is not None and spec.modifying
                   and spec.lock != "exempt")
        if guarded:
            # Before the gate and the wire: a refusal here must leave Live
            # exactly as it was.
            self._lock.check_write(name)
        if spec.gated:
            self._handshake.require(name, spec.min_script_version,
                                    send_command=self._client.send_command)
        try:
            return self._client.send_command(name, params or {})
        finally:
            # Stamped even when the send raised: a timed-out main-thread task
            # may still have changed the set, and an over-eager stamp only
            # asks for one more save, while a missed one could let release
            # accept a file older than the last edit.
            if guarded and spec.lock == "write":
                self._lock.touch()

    def _require_lock(self) -> LiveLock:
        if self._lock is None:
            raise LockError("disabled")
        return self._lock

    # Core commands

    def get_session_info(self) -> Dict[str, Any]:
        return self._send("get_session_info")

    def get_remote_script_info(self) -> Dict[str, Any]:
        """The handshake/cache interplay behind the get_remote_script_info
        tool. Touch the connection first, exactly where
        get_ableton_connection() used to: an unreachable Live must surface
        as the tool's error string, not be absorbed into the handshake info
        (perform() deliberately swallows send failures)."""
        self._client.ensure_connected()
        info = self._handshake.perform(self._client.send_command)
        cached = dict(self._handshake.info() or info)
        cached["expected_version"] = EXPECTED_REMOTE_SCRIPT_VERSION
        return cached

    def get_track_info(self, track_index: int) -> Dict[str, Any]:
        return self._send("get_track_info", {"track_index": track_index})

    # Live lock and preflight

    def get_live_status(self) -> Dict[str, Any]:
        """One call answering "may I touch Live right now, and how is it?".

        Assembled from three independent sources so that any one failing
        still leaves the others: the Remote Script (transport, solos, arms),
        the lock file, and the macOS UI (window title → set name and unsaved
        marker, modal dialogs, user idle time). Nothing here changes state.
        """
        status: Dict[str, Any] = {"connected": False, "live": None,
                                  "live_error": None}
        try:
            status["live"] = self._send("get_live_status")
            status["connected"] = True
        except CapabilityError as e:
            # Reachable but too old to answer: say so, keep the rest.
            status["connected"] = True
            status["live_error"] = str(e)
        except Exception as e:
            status["live_error"] = str(e)
        info = self._handshake.info() or {}
        status["script_version"] = info.get("script_version")
        status["expected_script_version"] = EXPECTED_REMOTE_SCRIPT_VERSION
        status["lock"] = self._lock.status() if self._lock is not None else None
        if self._ui is not None and self._ui.available:
            status["ui"] = summarize_windows(self._ui.live_windows())
            status["user_idle_s"] = self._ui.user_idle_s()
        else:
            status["ui"] = summarize_windows(None)
            status["user_idle_s"] = None
        return status

    def acquire_live_lock(self, holder: str, task: str) -> Dict[str, Any]:
        return self._require_lock().acquire(holder, task)

    def release_live_lock(self, holder: str, set_path: Optional[str] = None,
                          save_first: bool = False,
                          allow_ui_automation: bool = False) -> Dict[str, Any]:
        """Release after a verified save; with ``save_first`` the save is
        done here (save_set's API-then-menu path) and its .als is the one
        checked. Without a ``set_path`` the window title's set is looked up
        (passively) — the lock still refuses unless that resolves."""
        lock = self._require_lock()
        save = None
        if save_first:
            save = self.save_set(allow_ui_automation=allow_ui_automation,
                                 set_path=set_path)
            if not save.get("saved"):
                raise LockError("save_failed", save=save)
            set_path = set_path or (save.get("verified") or {}).get("set_path")
        titles = None
        if self._ui is not None and self._ui.available:
            titles = self._ui.window_titles()
            if not set_path:
                windows = summarize_windows(self._ui.live_windows())
                set_path = self._resolve_set_path(windows, None)["set_path"]
        result = lock.release(holder, set_path or "", live_windows=titles)
        if save is not None:
            result["save"] = save
        return result

    def get_clip_notes(self, track_index: int, clip_index: int,
                       arrangement: bool = False) -> Dict[str, Any]:
        return self._send(
            "get_clip_notes",
            {
                "track_index": track_index,
                "clip_index": clip_index,
                "arrangement": arrangement,
            },
        )

    def get_session_snapshot(
        self,
        include_notes: bool,
        include_params: bool,
        include_warp_markers: bool = False,
        include_rack_chains: bool = False,
        include_empty_slots: bool = False,
        tracks: Optional[List[Union[int, str]]] = None,
        max_chars: Optional[int] = None,
    ) -> Dict[str, Any]:
        # Every flag goes on the wire explicitly, none left to the script's
        # own default: schema v3 changed three of those defaults, and a
        # payload whose size depends on which script happens to be installed
        # is the opposite of what a scoped snapshot is for. ``tracks`` is
        # the exception — no filter is its absence, not a false.
        params: Dict[str, Any] = {
            "include_notes": include_notes,
            "include_params": include_params,
            "include_warp_markers": include_warp_markers,
            "include_rack_chains": include_rack_chains,
            "include_empty_slots": include_empty_slots,
        }
        if tracks is not None:
            params["tracks"] = tracks
        result = self._send("get_session_snapshot", params)
        if max_chars:
            result = _fit_snapshot(result, int(max_chars))
        return result

    def get_mix_state(self) -> Dict[str, Any]:
        return self._send("get_mix_state")

    def create_midi_track(self, index: int) -> Dict[str, Any]:
        return self._send("create_midi_track", {"index": index})

    def create_audio_track(self, index: int) -> Dict[str, Any]:
        return self._send("create_audio_track", {"index": index})

    def duplicate_track(self, track_index: int,
                        expect_track_name: Optional[str] = None
                        ) -> Dict[str, Any]:
        return self._send(
            "duplicate_track",
            _guarded({"track_index": track_index}, expect_track_name))

    def set_track_name(self, track_index: int, name: str,
                       expect_track_name: Optional[str] = None
                       ) -> Dict[str, Any]:
        return self._send("set_track_name",
                          _guarded({"track_index": track_index, "name": name},
                                   expect_track_name))

    def create_clip(self, track_index: int, clip_index: int, length: float,
                    expect_track_name: Optional[str] = None) -> Dict[str, Any]:
        return self._send("create_clip", _guarded({
            "track_index": track_index,
            "clip_index": clip_index,
            "length": length
        }, expect_track_name))

    def set_clip_gain(self, track_index: int, clip_index: int, gain: float,
                      arrangement: bool,
                      expect_track_name: Optional[str] = None
                      ) -> Dict[str, Any]:
        return self._send("set_clip_gain", _guarded({
            "track_index": track_index,
            "clip_index": clip_index,
            "gain": gain,
            "arrangement": arrangement
        }, expect_track_name))

    def set_clip_warp(self, track_index: int, clip_index: int, warping: bool,
                      warp_mode: Optional[int], arrangement: bool,
                      expect_track_name: Optional[str] = None
                      ) -> Dict[str, Any]:
        return self._send("set_clip_warp", _guarded({
            "track_index": track_index,
            "clip_index": clip_index,
            "warping": warping,
            "warp_mode": warp_mode,
            "arrangement": arrangement
        }, expect_track_name))

    def get_clip_warp_markers(self, track_index: int, clip_index: int,
                              arrangement: bool,
                              expect_track_name: Optional[str] = None
                              ) -> Dict[str, Any]:
        return self._send("get_clip_warp_markers", _guarded({
            "track_index": track_index,
            "clip_index": clip_index,
            "arrangement": arrangement
        }, expect_track_name))

    def edit_clip_warp_markers(self, track_index: int, clip_index: int,
                               edits: List[Dict[str, Any]], arrangement: bool,
                               stop_on_error: bool = True,
                               expect_track_name: Optional[str] = None
                               ) -> Dict[str, Any]:
        return self._send("edit_clip_warp_markers", _guarded({
            "track_index": track_index,
            "clip_index": clip_index,
            "edits": edits,
            "arrangement": arrangement,
            "stop_on_error": stop_on_error
        }, expect_track_name))

    # Warp vision: one wire read, then analysis of the clip's audio file in
    # this process (warp_vision / audio_analysis). Orchestration, so Model.

    def analyze_clip_audio(self, track_index: int, clip_index: int,
                           arrangement: bool,
                           expect_track_name: Optional[str] = None,
                           **options: Any) -> Dict[str, Any]:
        state = self.get_clip_warp_markers(track_index, clip_index,
                                           arrangement, expect_track_name)
        return warp_vision.analyze(state, **options)

    def suggest_warp_markers(self, track_index: int, clip_index: int,
                             arrangement: bool, apply: bool = False,
                             include_image: bool = True,
                             image_width: int = warp_vision.DEFAULT_IMAGE_WIDTH,
                             expect_track_name: Optional[str] = None,
                             **options: Any) -> Dict[str, Any]:
        """Analyse, propose, and — only when ``apply`` — send the batch and
        measure the result on the markers Live reads back."""
        state = self.get_clip_warp_markers(track_index, clip_index,
                                           arrangement, expect_track_name)
        proposal = warp_vision.propose(state, **options)
        applied = None
        if apply and proposal.edits:
            applied = self.edit_clip_warp_markers(
                track_index, clip_index, warp_vision.wire_edits(proposal),
                arrangement, True, expect_track_name)
        return warp_vision.proposal_result(proposal, include_image,
                                           image_width, applied)

    def back_to_arrangement(self) -> Dict[str, Any]:
        return self._send("back_to_arrangement", {})

    def get_track_routing(self, track_index: int) -> Dict[str, Any]:
        return self._send("get_track_routing", {"track_index": track_index})

    def set_track_routing(self, track_index: int, target: str, field: str,
                          expect_track_name: Optional[str] = None
                          ) -> Dict[str, Any]:
        return self._send("set_track_routing", _guarded({
            "track_index": track_index,
            "field": field,
            "target": target
        }, expect_track_name))

    def set_count_in(self, bars: int, metronome: bool) -> Dict[str, Any]:
        return self._send("set_count_in", {
            "bars": bars,
            "metronome": metronome
        })

    def set_track_send(self, track_index: int, send_index: int, value: float,
                       expect_track_name: Optional[str] = None
                       ) -> Dict[str, Any]:
        return self._send("set_track_send", _guarded({
            "track_index": track_index,
            "send_index": send_index,
            "value": value
        }, expect_track_name))

    def _ui_allowed(self, allow_ui_automation: bool) -> bool:
        return bool(self._ui is not None and self._ui.available
                    and (allow_ui_automation or self._allow_ui))

    def _resolve_set_path(self, windows: Dict[str, Any],
                          set_path: Optional[str]) -> Dict[str, Any]:
        """The .als to verify against: the caller's, else the one Spotlight
        finds for the window title's set name (only if it is unambiguous)."""
        if set_path:
            path = os.path.expanduser(set_path)
            return {"set_path": path, "candidates": [path], "source": "caller"}
        shown = windows.get("set_path")
        if shown and os.path.isfile(shown):
            # The main window's AXDocument: the file Live itself has open.
            return {"set_path": shown, "candidates": [shown], "source": "ax_document"}
        candidates = None
        if self._ui is not None and windows.get("set_name"):
            candidates = self._ui.find_set_paths(windows["set_name"],
                                                 windows.get("project"))
        candidates = candidates or []
        project = windows.get("project")
        in_project = [c for c in candidates
                      if project and os.path.basename(os.path.dirname(c)) == project]
        pick = (in_project if len(in_project) == 1
                else candidates if len(candidates) == 1 else [])
        return {"set_path": pick[0] if pick else None, "candidates": candidates,
                "source": "spotlight" if pick else None}

    def save_set(self, allow_ui_automation: bool = False,
                 set_path: Optional[str] = None,
                 timeout_s: float = 30.0) -> Dict[str, Any]:
        """Save the open set: Live's API first, then File › Save Live Set.

        Live 12's Python API exposes no save (every probe has returned
        ``Tried: []`` on 12.4.x), so the reliable path is the menu item, clicked
        by title over Accessibility — only with the caller's (or the
        server's) consent, because it brings Live to the front. Success is
        not "the click happened": it is the window title losing its unsaved
        marker and, when the .als is known, its mtime advancing. A Save
        panel appearing means the set was never saved (Untitled) — that is
        cancelled with Escape and refused rather than saved somewhere
        arbitrary.
        """
        result = dict(self._send("save_set", {}))  # lock-checked ("check")
        result.setdefault("attempts", [])
        if result.get("saved"):
            result["verified"] = None  # the API said so; nothing to read back
            return result
        if not self._ui_allowed(allow_ui_automation):
            result["ui_path"] = ("not_macos" if self._ui is None or not self._ui.available
                                 else "not_allowed")
            return result
        return self._save_via_menu(set_path, timeout_s, result["attempts"])

    def _save_via_menu(self, set_path: Optional[str], timeout_s: float,
                       attempts: List[str]) -> Dict[str, Any]:
        ui = self._ui
        before = summarize_windows(ui.live_windows())
        out: Dict[str, Any] = {"saved": False, "method": None,
                               "attempts": attempts, "ui_path": "tried"}
        if not before["readable"]:
            out["error"] = "ui_unreadable"
            return out
        if before["modal_dialog"]:
            out.update(error="modal_open", modal_dialog=before["modal_dialog"])
            return out
        target = self._resolve_set_path(before, set_path)
        path = target["set_path"]
        mtime_before = (os.path.getmtime(path)
                        if path and os.path.isfile(path) else None)
        previous_app = ui.frontmost_app()
        click = ui.click_menu_item("File", "Save Live Set",
                                   exclude=("Save Live Set As",))
        out["menu"] = click
        if not click.get("ok"):
            clean = (before["unsaved"] is False
                     or (before["unsaved"] is None and path is not None))
            if click.get("error") == "item_disabled" and clean:
                # Nothing to save: Live greys the item out on a clean set.
                # Where the title shows no "*" (12.4.7), the greyed item on a
                # set with a file is itself the evidence.
                out.update(saved=True, method="none needed (no unsaved changes)")
            else:
                out["error"] = "menu_" + str(click.get("error"))
            out["verified"] = self._save_verification(before, before, target,
                                                      mtime_before)
            return out

        def settled():
            now = summarize_windows(ui.live_windows())
            if now["modal_dialog"]:
                return ("dialog", now)
            mtime = (os.path.getmtime(path)
                     if path and os.path.isfile(path) else None)
            advanced = path is None or (mtime is not None and (
                mtime_before is None or mtime > mtime_before))
            # No "*" to read (12.4.7): the file's mtime advancing is the proof.
            clean = now["unsaved"] is False or (now["unsaved"] is None
                                                and path is not None)
            if now["readable"] and clean and advanced:
                return ("saved", now)
            return None

        outcome = ui.wait_for(settled, timeout_s)
        after = summarize_windows(ui.live_windows())
        if outcome and outcome[0] == "dialog":
            # A Save panel: the set is Untitled. Never type a path into it
            # here — cancel and say so.
            ui.press_escape()
            out.update(error="save_panel_opened", modal_dialog=outcome[1]["modal_dialog"])
        verified = self._save_verification(before, after, target, mtime_before)
        out["verified"] = verified
        if outcome and outcome[0] == "saved":
            out.update(saved=True, method="ui: File > " + str(click.get("clicked")))
        elif "error" not in out:
            out["error"] = "not_confirmed"
        if previous_app and not previous_app.startswith("Live"):
            ui.activate(previous_app)
        return out

    @staticmethod
    def _save_verification(before: Dict[str, Any], after: Dict[str, Any],
                           target: Dict[str, Any],
                           mtime_before: Optional[float]) -> Dict[str, Any]:
        path = target.get("set_path")
        mtime_after = os.path.getmtime(path) if path and os.path.isfile(path) else None
        return {
            "set_name": after.get("set_name") or before.get("set_name"),
            "title_before": next(iter(before.get("titles") or []), None),
            "unsaved_before": before.get("unsaved"),
            "unsaved_after": after.get("unsaved"),
            "set_path": path,
            "set_path_source": target.get("source"),
            "set_path_candidates": target.get("candidates"),
            "mtime_before": mtime_before,
            "mtime_after": mtime_after,
            "mtime_advanced": (mtime_after is not None and (
                mtime_before is None or mtime_after > mtime_before)),
        }

    # Rendering (render.py drives the UI; these are its LOM/lock hooks)

    def check_ui_write(self, action: str) -> None:
        """The Live lock for a UI-driven action that is not a wire command:
        refused exactly as a state-changing command would be."""
        if self._lock is not None:
            self._lock.check_write(action)

    def live_status_raw(self) -> Dict[str, Any]:
        return self._send("get_live_status")

    def select_track(self, track_index: int, track_type: str = "regular",
                     expect_track_name: Optional[str] = None) -> Dict[str, Any]:
        return self._send("select_track", _guarded({
            "track_index": track_index, "track_type": track_type},
            expect_track_name))

    def track_render_state(self, track_index: int,
                           track_type: str = "regular") -> Dict[str, Any]:
        return self._send("get_track_render_state", {
            "track_index": track_index, "track_type": track_type})

    def _require_ui(self, allow_ui_automation: bool) -> MacUI:
        if not self._ui_allowed(allow_ui_automation):
            raise render.RenderRefused(
                "not_macos" if self._ui is None or not self._ui.available
                else "not_allowed")
        return self._ui

    def export_audio(self, allow_ui_automation: bool = False,
                     evidence_dir: Optional[str] = None,
                     **options: Any) -> Dict[str, Any]:
        ui = self._require_ui(allow_ui_automation)
        return render.export_audio(
            self, ui, evidence_dir=evidence_dir or self._evidence_dir,
            ffmpeg=self._ffmpeg, **options)

    def bounce_track(self, track_index: int, mode: str,
                     allow_ui_automation: bool = False,
                     expect_track_name: Optional[str] = None,
                     confirm_destructive: bool = False) -> Dict[str, Any]:
        ui = self._require_ui(allow_ui_automation)
        try:
            return render.bounce_track(
                self, ui, track_index=track_index, mode=mode,
                expect_track_name=expect_track_name,
                confirm_destructive=confirm_destructive, ffmpeg=self._ffmpeg)
        finally:
            # A freeze or bounce edits the set whether or not it was
            # confirmed in time; release must see a save after it.
            if self._lock is not None:
                self._lock.touch()

    def analyze_audio(self, path: str, start_s: Optional[float] = None,
                      end_s: Optional[float] = None) -> Dict[str, Any]:
        return audio_metrics.analyze(path, start_s=start_s, end_s=end_s,
                                     ffmpeg=self._ffmpeg)

    def verify_audio(self, path: str, **checks: Any) -> Dict[str, Any]:
        return audio_metrics.verify(path, ffmpeg=self._ffmpeg, **checks)

    def checkpoint_set(self, label: str, set_path: Optional[str] = None,
                       allow_ui_automation: bool = False,
                       checkpoint_dir: Optional[str] = None) -> Dict[str, Any]:
        """Save, then copy the .als to ``<set folder>/Checkpoints/`` as
        ``<timestamp>-<label>.als`` and prove the copy is byte-identical.

        "Put it back how it was" needs a file from before the change; Live's
        own Backup/ folder rotates and is named by time only. A labelled
        checkpoint is one save plus one copy, and the sha256 lets a later
        diff or restore be sure which state it holds."""
        save = self.save_set(allow_ui_automation=allow_ui_automation,
                             set_path=set_path)
        out: Dict[str, Any] = {"checkpointed": False, "save": save}
        if not save.get("saved"):
            out["error"] = "save_failed"
            return out
        verified = save.get("verified") or {}
        path = verified.get("set_path") or (os.path.expanduser(set_path)
                                            if set_path else None)
        if not path or not os.path.isfile(path):
            out.update(error="set_path_unknown",
                       candidates=verified.get("set_path_candidates"))
            return out
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", label or "").strip("-.")[:60] or "checkpoint"
        folder = os.path.expanduser(checkpoint_dir) if checkpoint_dir else os.path.join(
            os.path.dirname(path), "Checkpoints")
        os.makedirs(folder, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(folder, f"{stamp}-{slug}.als")
        n = 2
        while os.path.exists(dest):
            dest = os.path.join(folder, f"{stamp}-{slug}-{n}.als")
            n += 1
        shutil.copy2(path, dest)
        src_hash, dest_hash = _sha256(path), _sha256(dest)
        out.update(checkpointed=src_hash == dest_hash, checkpoint_path=dest,
                   set_path=path, sha256=dest_hash, bytes=os.path.getsize(dest))
        if src_hash != dest_hash:
            out["error"] = "copy_mismatch"
        return out

    def create_return_track(self) -> Dict[str, Any]:
        return self._send("create_return_track", {})

    def set_track_arm(self, track_index: int, armed: bool,
                      expect_track_name: Optional[str] = None,
                      authorized: bool = False) -> Dict[str, Any]:
        return self._send("set_track_arm", _guarded({
            "track_index": track_index,
            "value": armed
        }, expect_track_name), authorized=authorized or not armed)

    def set_track_monitoring(self, track_index: int, state: str,
                             expect_track_name: Optional[str] = None
                             ) -> Dict[str, Any]:
        return self._send("set_track_monitoring", _guarded({
            "track_index": track_index,
            "value": state
        }, expect_track_name))

    def get_device_parameters(self, track_index: int, device_index: int,
                              track_type: str) -> Dict[str, Any]:
        return self._send("get_device_parameters", {
            "track_index": track_index,
            "device_index": device_index,
            "track_type": track_type
        })

    def set_device_parameter(self, track_index: int, device_index: int,
                             parameter: Union[str, int], value: float,
                             track_type: str,
                             expect_track_name: Optional[str] = None
                             ) -> Dict[str, Any]:
        # ``parameter`` arrives already coerced (a name, or an int index) —
        # the string-to-int parse is boundary work and stays in the
        # controller.
        return self._send("set_device_parameter", _guarded({
            "track_index": track_index,
            "device_index": device_index,
            "parameter": parameter,
            "value": value,
            "track_type": track_type
        }, expect_track_name))

    def set_device_parameters(self, track_index: int, device_index: int,
                              parameters: Dict[str, Any], track_type: str,
                              expect_track_name: Optional[str] = None
                              ) -> Dict[str, Any]:
        # The plural of the method above, and deliberately not a loop over
        # it: the whole batch is one wire command so the writes land inside
        # a single main-thread task.
        return self._send("set_device_parameters", _guarded({
            "track_index": track_index,
            "device_index": device_index,
            "parameters": parameters,
            "track_type": track_type
        }, expect_track_name))

    def get_plugin_presets(self, track_index: int, device_index: int,
                           track_type: str) -> Dict[str, Any]:
        return self._send("get_plugin_presets", {
            "track_index": track_index,
            "device_index": device_index,
            "track_type": track_type
        })

    def set_plugin_preset(self, track_index: int, device_index: int,
                          preset_index: Optional[int] = None,
                          preset_name: Optional[str] = None,
                          track_type: str = "regular",
                          expect_track_name: Optional[str] = None
                          ) -> Dict[str, Any]:
        # Presence-driven, like jump_to_locator: only the criteria the
        # caller gave go on the wire, so the script's index / name /
        # both-must-agree resolution sees exactly what was asked and never
        # a null it has to explain away.
        params: Dict[str, Any] = {
            "track_index": track_index,
            "device_index": device_index,
            "track_type": track_type
        }
        if preset_index is not None:
            params["preset_index"] = preset_index
        if preset_name:
            params["preset_name"] = preset_name
        return self._send("set_plugin_preset", _guarded(params, expect_track_name))

    def delete_device(self, track_index: int, device_index: int,
                      track_type: str,
                      expect_track_name: Optional[str] = None
                      ) -> Dict[str, Any]:
        return self._send("delete_device", _guarded({
            "track_index": track_index,
            "device_index": device_index,
            "track_type": track_type
        }, expect_track_name))

    def set_track_volume(self, track_index: int, value: float,
                         track_type: str,
                         expect_track_name: Optional[str] = None
                         ) -> Dict[str, Any]:
        return self._send("set_track_volume", _guarded({
            "track_index": track_index,
            "value": value,
            "track_type": track_type
        }, expect_track_name))

    def set_track_pan(self, track_index: int, value: float, track_type: str,
                      expect_track_name: Optional[str] = None
                      ) -> Dict[str, Any]:
        return self._send("set_track_pan", _guarded({
            "track_index": track_index,
            "value": value,
            "track_type": track_type
        }, expect_track_name))

    def set_track_mute(self, track_index: int, mute: bool,
                       expect_track_name: Optional[str] = None
                       ) -> Dict[str, Any]:
        return self._send("set_track_mute", _guarded({
            "track_index": track_index,
            "value": mute
        }, expect_track_name))

    def set_track_solo(self, track_index: int, solo: bool, track_type: str,
                       expect_track_name: Optional[str] = None
                       ) -> Dict[str, Any]:
        return self._send("set_track_solo", _guarded({
            "track_index": track_index,
            "value": solo,
            "track_type": track_type
        }, expect_track_name))

    def delete_track(self, track_index: int,
                     expect_track_name: Optional[str] = None
                     ) -> Dict[str, Any]:
        return self._send("delete_track", _guarded({
            "track_index": track_index
        }, expect_track_name))

    def create_audio_clip(self, track_index: int, clip_index: int, path: str,
                          expected_beats: Optional[float] = None,
                          expect_track_name: Optional[str] = None
                          ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "track_index": track_index,
            "clip_index": clip_index,
            "path": path
        }
        # An assertion about the file, not part of the import: sent only
        # when the caller has one to make.
        if expected_beats is not None:
            params["expected_beats"] = expected_beats
        return self._send("create_audio_clip",
                          _guarded(params, expect_track_name))

    def add_notes_to_clip(
        self,
        track_index: int,
        clip_index: int,
        notes: List[Dict[str, Union[int, float, bool]]],
        expect_count: Optional[int] = None,
        arrangement: bool = False,
        expect_track_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "track_index": track_index,
            "clip_index": clip_index,
            "notes": notes,
            "arrangement": arrangement
        }
        # The caller's stated total. It only means anything if it can
        # disagree with what arrived, so it is sent exactly as given and
        # never defaulted to len(notes) here — that would compare the list
        # against itself and always pass.
        if expect_count is not None:
            params["expect_count"] = expect_count
        return self._send("add_notes_to_clip",
                          _guarded(params, expect_track_name))

    def clear_notes_from_clip(self, track_index: int, clip_index: int,
                              arrangement: bool = False,
                              expect_track_name: Optional[str] = None
                              ) -> Dict[str, Any]:
        return self._send("clear_notes_from_clip", _guarded({
            "track_index": track_index,
            "clip_index": clip_index,
            "arrangement": arrangement
        }, expect_track_name))

    def set_clip_name(self, track_index: int, clip_index: int, name: str,
                      expect_track_name: Optional[str] = None
                      ) -> Dict[str, Any]:
        return self._send("set_clip_name", _guarded({
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        }, expect_track_name))

    def set_arrangement_clip_name(self, track_index: int, clip_index: int,
                                  name: str,
                                  expect_track_name: Optional[str] = None
                                  ) -> Dict[str, Any]:
        return self._send("set_arrangement_clip_name", _guarded({
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        }, expect_track_name))

    def set_tempo(self, tempo: float) -> Dict[str, Any]:
        return self._send("set_tempo", {"tempo": tempo})

    def get_song_scale(self) -> Dict[str, Any]:
        return self._send("get_song_scale", {})

    def set_song_scale(self, root_note: Optional[int] = None,
                       scale_name: Optional[str] = None,
                       scale_mode: Optional[bool] = None) -> Dict[str, Any]:
        # Presence-driven: only what the caller asked to change goes on the
        # wire, so the script writes exactly that and leaves the rest alone.
        params: Dict[str, Any] = {}
        if root_note is not None:
            params["root_note"] = root_note
        if scale_name is not None:
            params["scale_name"] = scale_name
        if scale_mode is not None:
            params["scale_mode"] = scale_mode
        return self._send("set_song_scale", params)

    def load_browser_item(self, track_index: int, item_uri: str,
                          track_type: Optional[str] = None,
                          expect_track_name: Optional[str] = None
                          ) -> Dict[str, Any]:
        """The wire command behind all three load tools. The drum-kit
        orchestration has never sent a track_type, so the params dict only
        carries the key when the caller gives one — the wire exchange stays
        exactly what each tool always sent."""
        params: Dict[str, Any] = {
            "track_index": track_index,
            "item_uri": item_uri,
        }
        if track_type is not None:
            params["track_type"] = track_type
        return self._send("load_browser_item",
                          _guarded(params, expect_track_name))

    # Device placement and sidechain (Live 12)

    def insert_device(self, track_index: int, device_name: str, index: int = -1,
                      track_type: str = "regular",
                      expect_track_name: Optional[str] = None) -> Dict[str, Any]:
        return self._send("insert_device", _guarded({
            "track_index": track_index, "device_name": device_name,
            "index": index, "track_type": track_type}, expect_track_name))

    def move_device(self, track_index: int, device_index: int, to_index: int,
                    to_track_index: Optional[int] = None,
                    track_type: str = "regular",
                    to_track_type: Optional[str] = None,
                    expect_track_name: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"track_index": track_index,
                                  "device_index": device_index,
                                  "to_index": to_index, "track_type": track_type}
        if to_track_index is not None:
            params["to_track_index"] = to_track_index
        if to_track_type is not None:
            params["to_track_type"] = to_track_type
        return self._send("move_device", _guarded(params, expect_track_name))

    def get_device_sidechain(self, track_index: int, device_index: int,
                             track_type: str = "regular") -> Dict[str, Any]:
        return self._send("get_device_sidechain", {
            "track_index": track_index, "device_index": device_index,
            "track_type": track_type})

    def set_device_sidechain(self, track_index: int, device_index: int,
                             source: str, channel: Optional[str] = None,
                             enable: bool = True, track_type: str = "regular",
                             expect_track_name: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"track_index": track_index,
                                  "device_index": device_index, "source": source,
                                  "enable": enable, "track_type": track_type}
        if channel is not None:
            params["channel"] = channel
        return self._send("set_device_sidechain", _guarded(params, expect_track_name))

    # Arrangement-native clips (Live 12)

    def create_arrangement_audio_clip(self, track_index: int, path: str,
                                      start_time: float, allow_overlap: bool = False,
                                      name: Optional[str] = None,
                                      expect_track_name: Optional[str] = None
                                      ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"track_index": track_index,
                                  "path": os.path.expanduser(path),
                                  "start_time": start_time,
                                  "allow_overlap": allow_overlap}
        if name is not None:
            params["name"] = name
        return self._send("create_arrangement_audio_clip",
                          _guarded(params, expect_track_name))

    def create_arrangement_midi_clip(self, track_index: int, start_time: float,
                                     length: float,
                                     notes: Optional[List[Dict[str, Any]]] = None,
                                     name: Optional[str] = None,
                                     allow_overlap: bool = False,
                                     expect_track_name: Optional[str] = None
                                     ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"track_index": track_index,
                                  "start_time": start_time, "length": length,
                                  "allow_overlap": allow_overlap}
        if notes:
            params["notes"] = notes
        if name is not None:
            params["name"] = name
        return self._send("create_arrangement_midi_clip",
                          _guarded(params, expect_track_name))

    # Rack chains

    def get_rack_chains(self, track_index: int, device_index: int,
                        track_type: str = "regular") -> Dict[str, Any]:
        return self._send("get_rack_chains", {
            "track_index": track_index, "device_index": device_index,
            "track_type": track_type})

    def insert_rack_chain(self, track_index: int, device_index: int,
                          index: int = -1, name: Optional[str] = None,
                          track_type: str = "regular",
                          expect_track_name: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"track_index": track_index,
                                  "device_index": device_index, "index": index,
                                  "track_type": track_type}
        if name is not None:
            params["name"] = name
        return self._send("insert_rack_chain", _guarded(params, expect_track_name))

    def insert_chain_device(self, track_index: int, device_index: int,
                            chain_index: int, device_name: str, index: int = -1,
                            track_type: str = "regular",
                            expect_track_name: Optional[str] = None) -> Dict[str, Any]:
        return self._send("insert_chain_device", _guarded({
            "track_index": track_index, "device_index": device_index,
            "chain_index": chain_index, "device_name": device_name,
            "index": index, "track_type": track_type}, expect_track_name))

    def set_chain_mixer(self, track_index: int, device_index: int,
                        chain_index: int, settings: Dict[str, Any],
                        track_type: str = "regular",
                        expect_track_name: Optional[str] = None) -> Dict[str, Any]:
        # Only the fields the caller set go on the wire: the handler treats
        # an absent key as "leave it", and None would mean the same anyway.
        params: Dict[str, Any] = {"track_index": track_index,
                                  "device_index": device_index,
                                  "chain_index": chain_index,
                                  "track_type": track_type}
        params.update({k: v for k, v in settings.items() if v is not None})
        return self._send("set_chain_mixer", _guarded(params, expect_track_name))

    def _send_cue(self, command: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Send a cue command with one fresh-tick retry.

        The script parks the playhead and verifies before it toggles a cue,
        and right after a stop the read-back can stay stale for the rest of
        the tick even though the write landed (verified on Live 12.4.x - see
        _park_playhead). Its refusal says so - "Transport did not settle
        ... retry in a moment" - and is side-effect free. One retry on the
        next tick clears it; anything else is a real error and propagates.
        """
        try:
            return self._send(command, params)
        except LockError:
            raise
        except Exception as e:
            if "did not settle" not in str(e) or "retry" not in str(e):
                raise
            time.sleep(0.25)
            return self._send(command, params)

    def _send_with_alert_watchdog(self, command: str, params: Dict[str, Any],
                                  timeout_s: float = 45.0) -> Dict[str, Any]:
        """Send a load command while watching for a Live alert.

        A preset Live cannot load answers with a MODAL ALERT on Live's main
        thread: the Remote Script never replies, every later command times
        out, and Live stays wedged until someone clicks the alert away. The
        watchdog polls the UI while the send is in flight; when an alert
        that was not there before appears, it presses OK (dismissing only
        what this command itself opened) and either returns the reply that
        unblocked, or raises `live_alert` naming the alert's own text.
        """
        ui = getattr(self, "_ui", None)
        if ui is None or not getattr(ui, "available", False):
            return self._send(command, params)
        before = ui.modal_alerts()
        # None means the alerts cannot be read (no Accessibility permission
        # for this process): the load still goes, but an alert it opens
        # cannot be seen or dismissed, and the refusal below says so.
        blind = before is None
        before = before or []
        if before:
            raise render.RenderRefused("modal_open", modal_dialog=before[0])
        outcome: Dict[str, Any] = {}
        failure: List[Any] = []

        def _worker() -> None:
            try:
                outcome["value"] = self._send(command, params)
            except Exception as e:  # surfaced by the watchdog loop
                failure.append(e)

        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()
        alert = None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            worker.join(0.25)
            if not worker.is_alive():
                break
            alerts = ui.modal_alerts()
            if alerts is None:
                blind = True
                continue
            fresh = [a for a in alerts if a not in before]
            if fresh:
                alert = fresh[0]
                press = ui.dismiss_modal_alert("OK")
                if not press.get("ok"):
                    press = ui.dismiss_modal_alert("Cancel")
                if press.get("text") and not alert.get("text"):
                    alert["text"] = press["text"]
                worker.join(5.0)
                break
        if alert is not None:
            if worker.is_alive() or failure:
                raise render.RenderRefused(
                    "live_alert", alert_text=alert.get("text") or "",
                    buttons=alert.get("buttons") or [],
                    detail=("no reply from Live after the alert was "
                            "dismissed" if worker.is_alive()
                            else str(failure[0])[:200]))
            outcome["value"]["alert"] = alert.get("text") or ""
            return outcome["value"]
        worker.join(1.0)
        if worker.is_alive():
            raise render.RenderRefused(
                "live_alert", alert_text="", buttons=[],
                detail=("no reply from Live, and its dialogs cannot be read "
                        "(this process lacks the Accessibility permission), "
                        "so a 'cannot be loaded' alert may be open in Live: "
                        "dismiss it there before sending anything else"
                        if blind else
                        "no reply from Live and no alert seen"))
        if failure:
            raise failure[0]
        return outcome["value"]

    def load_device_from_path(self, track_index: int, path: str,
                              track_type: str = "regular",
                              expect_track_name: Optional[str] = None
                              ) -> Dict[str, Any]:
        path = os.path.expanduser(path)
        # A document Live would refuse is refused here instead: one kind
        # answers with a modal alert that wedges Live until it is clicked.
        broken = preset_integrity.check_preset_file(path)
        if broken:
            raise render.RenderRefused("preset_corrupt", path=path,
                                       summary=broken["summary"],
                                       problems=broken["problems"])
        return self._send_with_alert_watchdog("load_browser_item_by_path", _guarded({
            "track_index": track_index,
            "path": path,
            "track_type": track_type,
        }, expect_track_name))

    def get_plugin_state(self, source_path: str, track: Optional[str] = None,
                         plugin: Optional[str] = None,
                         include_strings: bool = True) -> Dict[str, Any]:
        """Offline: decode hosted plugins' saved state from a .als/.adv/.adg
        or .vstpreset (plugin_state.py). Never talks to Live."""
        result = plugin_state.read_plugin_states(source_path, track, plugin)
        if not include_strings:
            for entry in result.get("plugins", []):
                for state in (entry.get("states") or {}).values():
                    state.pop("strings", None)
        return result

    def load_devices(self, track_index: int, uris: List[str],
                     track_type: str = "regular",
                     expect_track_name: Optional[str] = None) -> Dict[str, Any]:
        """Load each URI in turn, stopping at the first failure. Each load is
        its own wire command (Live loads into the selected track and the
        device appears within that main-thread task, so there is no batch
        gain in one long task — only the round-trips to the model matter)."""
        if not uris:
            raise ValueError("uris must list at least one browser URI")
        loaded: List[Dict[str, Any]] = []
        failed = None
        devices_after: List[str] = []
        track_name = None
        for uri in uris:
            try:
                r = self.load_browser_item(track_index, uri, track_type,
                                           expect_track_name)
            except LockError:
                raise
            except Exception as e:
                failed = {"uri": uri, "error": str(e)}
                break
            loaded.append({"uri": uri, "item_name": r.get("item_name"),
                           "new_devices": r.get("new_devices")})
            devices_after = list(r.get("devices_after") or [])
            track_name = r.get("track_name", track_name)
        return {
            "track_index": track_index,
            "track_name": track_name,
            "requested_count": len(uris),
            "loaded_count": len(loaded),
            "loaded": loaded,
            "failed": failed,
            "not_attempted": list(uris[len(loaded) + (1 if failed else 0):]),
            "devices": [{"index": i, "name": n} for i, n in enumerate(devices_after)],
        }

    def fire_clip(self, track_index: int, clip_index: int,
                  expect_track_name: Optional[str] = None,
                  authorized: bool = False) -> Dict[str, Any]:
        return self._send("fire_clip", _guarded({
            "track_index": track_index,
            "clip_index": clip_index
        }, expect_track_name), authorized=authorized)

    def stop_clip(self, track_index: int, clip_index: int,
                  expect_track_name: Optional[str] = None) -> Dict[str, Any]:
        return self._send("stop_clip", _guarded({
            "track_index": track_index,
            "clip_index": clip_index
        }, expect_track_name))

    def delete_clip(self, track_index: int, clip_index: int,
                    expect_track_name: Optional[str] = None) -> Dict[str, Any]:
        return self._send("delete_clip", _guarded({
            "track_index": track_index,
            "clip_index": clip_index,
        }, expect_track_name))

    def start_playback(self, authorized: bool = False) -> Dict[str, Any]:
        return self._send("start_playback", authorized=authorized)

    def stop_playback(self) -> Dict[str, Any]:
        return self._send("stop_playback")

    def get_browser_tree(self, category_type: str) -> Dict[str, Any]:
        return self._send("get_browser_tree", {
            "category_type": category_type
        })

    def get_browser_items_at_path(self, path: str) -> Dict[str, Any]:
        return self._send("get_browser_items_at_path", {
            "path": path
        })

    def load_drum_kit(self, track_index: int, rack_uri: str, kit_path: str,
                      expect_track_name: Optional[str] = None
                      ) -> Dict[str, Any]:
        """The one true multi-step orchestration: three wire calls with
        decisions between them (docs/REFACTOR_PLAN.md §3.2). Returns a
        stage-tagged plain dict — the presenter owns every string, the
        intermediate-failure ones included:

        - ``{"stage": "rack_failed", "rack_uri": ...}``
        - ``{"stage": "kit_lookup_failed", "error": ...}``
        - ``{"stage": "no_loadable", "kit_path": ...}``
        - ``{"stage": "ok", "kit_name": ..., "track_index": ...}``
        """
        # Step 1: Load the drum rack. The guard rides on both loads — they
        # target the same track, and the second one lands a kit inside
        # whatever rack the first one found.
        result = self.load_browser_item(track_index, rack_uri,
                                        expect_track_name=expect_track_name)
        if not result.get("loaded", False):
            return {"stage": "rack_failed", "rack_uri": rack_uri}

        # Step 2: Get the drum kit items at the specified path
        kit_result = self.get_browser_items_at_path(kit_path)
        if "error" in kit_result:
            return {"stage": "kit_lookup_failed",
                    "error": kit_result.get("error")}

        # Step 3: Find a loadable drum kit. kit_path may name a folder (load
        # its first loadable kit) or point directly at a kit file such as
        # "drums/808 Core Kit.adg" — then the node itself is the loadable item.
        kit_items = kit_result.get("items", [])
        loadable_kits = [item for item in kit_items
                         if item.get("is_loadable", False)]
        if not loadable_kits and kit_result.get("is_loadable") \
                and kit_result.get("uri"):
            loadable_kits = [kit_result]
        if not loadable_kits:
            return {"stage": "no_loadable", "kit_path": kit_path}

        # Step 4: Load the first loadable kit
        self.load_browser_item(track_index, loadable_kits[0].get("uri"),
                               expect_track_name=expect_track_name)
        return {"stage": "ok", "kit_name": loadable_kits[0].get("name"),
                "track_index": track_index}

    # ── Arrangement view commands ─────────────────────────────────────────────

    def switch_to_arrangement_view(self) -> Dict[str, Any]:
        return self._send("switch_to_arrangement_view")

    def set_current_song_time(self, time: float) -> Dict[str, Any]:
        return self._send("set_current_song_time", {"time": time})

    def get_arrangement_clips(self, track_index: int) -> Dict[str, Any]:
        return self._send("get_arrangement_clips",
                          {"track_index": track_index})

    def duplicate_session_clip_to_arrangement(
        self,
        track_index: int,
        clip_index: int,
        destination_time: Optional[float] = None,
        destination_times: Optional[List[float]] = None,
        allow_loop_phase_reset: bool = False,
        expect_track_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "track_index": track_index,
            "clip_index": clip_index,
        }
        # One key or the other, never both: the script branches on
        # destination_times being present at all, so a scalar sent
        # alongside a list would look placed and be silently ignored.
        if destination_times is not None:
            params["destination_times"] = destination_times
        else:
            params["destination_time"] = destination_time
        # An override of a refusal, not part of an ordinary stamp — sent
        # only when the caller has actually accepted the re-phasing, so an
        # ordinary call cannot carry it by accident.
        if allow_loop_phase_reset:
            params["allow_loop_phase_reset"] = True
        return self._send("duplicate_session_clip_to_arrangement",
                          _guarded(params, expect_track_name))

    def create_locator(self, name: str, time: float) -> Dict[str, Any]:
        return self._send_cue("create_locator", {"name": name, "time": time})

    def delete_locator(self, name: str = "",
                       time: Optional[float] = None) -> Dict[str, Any]:
        # Presence-driven, exactly like jump_to_locator below: the script
        # matches by name first and falls back to the beat, so sending a
        # criterion the caller never gave would change which cue is found —
        # and this command then TOGGLES at whatever it found.
        params: Dict[str, Any] = {}
        if name:
            params["name"] = name
        if time is not None:
            params["time"] = time
        return self._send("delete_locator", params)

    def jump_to_locator(self, name: str = "",
                        time: Optional[float] = None) -> Dict[str, Any]:
        # Only the criteria actually given go on the wire, so the script's
        # name-first/time-second matching order is driven by presence.
        params: Dict[str, Any] = {}
        if name:
            params["name"] = name
        if time is not None:
            params["time"] = time
        return self._send_cue("jump_to_locator", params)

    def trim_arrangement_clip(self, track_index: int, clip_index: int,
                              start_time: Optional[float] = None,
                              end_time: Optional[float] = None,
                              expect_track_name: Optional[str] = None
                              ) -> Dict[str, Any]:
        # An omitted edge stays where it is; only requested edges go on the
        # wire so the script can tell "leave alone" from "trim to".
        params: Dict[str, Any] = {
            "track_index": track_index,
            "clip_index": clip_index,
        }
        if start_time is not None:
            params["start_time"] = start_time
        if end_time is not None:
            params["end_time"] = end_time
        return self._send("trim_arrangement_clip",
                          _guarded(params, expect_track_name))

    def delete_arrangement_clip(self, track_index: int,
                                clip_index: Optional[int] = None,
                                start_time: Optional[float] = None,
                                start_times: Optional[List[float]] = None,
                                expect_track_name: Optional[str] = None
                                ) -> Dict[str, Any]:
        # Exactly one way of saying which clip goes on the wire, in the
        # script's own order of preference. Sending a leftover ordinal
        # beside a beat position would leave the choice of which one wins
        # to the handler's argument order rather than to the caller.
        params: Dict[str, Any] = {"track_index": track_index}
        if start_times is not None:
            params["start_times"] = start_times
        elif start_time is not None:
            params["start_time"] = start_time
        elif clip_index is not None:
            params["clip_index"] = clip_index
        # Nothing at all is a legitimate send: the script answers with the
        # track's actual clip positions, which is a better prompt than
        # anything this layer could compose without reading Live.
        return self._send("delete_arrangement_clip",
                          _guarded(params, expect_track_name))

    def move_arrangement_clip(self, track_index: int, clip_index: int,
                              destination_time: float,
                              expect_track_name: Optional[str] = None
                              ) -> Dict[str, Any]:
        return self._send(
            "move_arrangement_clip",
            _guarded({
                "track_index": track_index,
                "clip_index": clip_index,
                "destination_time": destination_time,
            }, expect_track_name)
        )

    def duplicate_arrangement_clip(self, track_index: int, clip_index: int,
                                   destination_time: float,
                                   allow_loop_phase_reset: bool = False,
                                   expect_track_name: Optional[str] = None
                                   ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "track_index": track_index,
            "clip_index": clip_index,
            "destination_time": destination_time,
        }
        # Same override, same reason as the session stamp above.
        if allow_loop_phase_reset:
            params["allow_loop_phase_reset"] = True
        return self._send("duplicate_arrangement_clip",
                          _guarded(params, expect_track_name))
