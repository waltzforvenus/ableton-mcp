"""View layer: every model-facing string (docs/REFACTOR_PLAN.md §3.2).

One success renderer per tool, named exactly after it — the JSON-returning
tools are aliases of the shared ``as_json`` — so tests/test_mvc_contract.py
can assert the tool → presenter mapping by attribute lookup. Renderers are
pure: they take the service's raw result dict (plus whichever call arguments
the text interpolates) and return the exact string the tool has always
returned. A renderer is allowed to be one f-string.

Error translation is centralized here too. ``ERROR_PHRASES`` maps each tool
to the phrase inside its "Error {phrase}: {e}" string; the two browser tools
whose error handling sniffs the failure message instead live in
``ERROR_RENDERERS``. ``error_text`` is the one entry point the ``tool``
decorator calls — the wording seam that used to drift per-tool now has a
single owner.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Sequence


def as_json(result: Dict[str, Any]) -> str:
    """The shared renderer for the JSON-returning tools."""
    return json.dumps(result, indent=2)


# ── Success renderers, in tools.py order ─────────────────────────────────────

get_session_info = as_json
get_remote_script_info = as_json
get_track_info = as_json
get_live_status = as_json
get_mix_state = as_json

_AUDIBLE_WHAT = {
    "start_playback": "starts playback",
    "fire_clip": "fires a clip (it plays)",
    "set_track_arm": "arms a track for recording",
}


def audio_auth_required(e: Any) -> str:
    command = getattr(e, "command", str(e))
    what = _AUDIBLE_WHAT.get(command, "makes sound")
    return (f"Refused: this {what}, which needs the user's explicit OK. Ask them; "
            f"if they agree, call again with authorized=true. Nothing was sent. "
            f"(The server-wide gate is ABLETON_MCP_REQUIRE_AUDIO_AUTH.)")


def _holder_text(lock: Any) -> str:
    if not isinstance(lock, dict):
        return "someone"
    text = f"'{lock.get('holder')}'"
    if lock.get("task"):
        text += f" (task: {lock.get('task')})"
    if lock.get("acquired"):
        text += f", since {lock.get('acquired')}"
    return text


def acquire_live_lock(result: Dict[str, Any]) -> str:
    lock = result.get("lock") or {}
    if result.get("adopted"):
        return (f"You already held the Live lock as '{lock.get('holder')}'; this "
                f"server now writes under it. Release it with release_live_lock "
                f"after saving.")
    return (f"Acquired the Live lock as '{lock.get('holder')}'. It never "
            f"expires: save, then release it with release_live_lock.")


def release_live_lock(result: Dict[str, Any]) -> str:
    if not result.get("released"):
        return "No Live lock was held; nothing to release."
    saved = " (saved first)" if (result.get("save") or {}).get("saved") else ""
    return (f"Released the Live lock; the set is saved at "
            f"{result.get('saved_set')}{saved}.")


_UNSAVED_PROBLEMS = {
    "different_set": "Live's window does not show that set — a different set is open, or the path is wrong",
    "unsaved_marker": "Live's title still shows unsaved changes (*)",
    "dialog_open": "a Save or Export dialog is open in Live",
}


def lock_refusal(e: Any) -> str:
    """The wording of every ``LockError`` (live_lock.py)."""
    d = getattr(e, "details", {}) or {}
    reason = getattr(e, "reason", str(e))
    command = d.get("command")
    doing = f" ({command} was not sent; nothing changed)" if command else ""
    if reason == "held_by_other":
        return (f"Refused: Live is locked by {_holder_text(d.get('lock'))}. Do not "
                f"touch Live until they release it — ask them or the user; the lock "
                f"never expires and is not yours to take over{doing}.")
    if reason == "not_held":
        return (f"Refused: this server does not hold the Live lock. Call "
                f"get_live_status to check nobody else is working in Live, then "
                f"acquire_live_lock(holder=...) before editing{doing}. (Single-user "
                f"setups can set ABLETON_MCP_REQUIRE_LOCK=0.)")
    if reason == "not_holder":
        return (f"Refused: the Live lock is held by {_holder_text(d.get('lock'))}, "
                f"not by you; it stays held.")
    if reason == "set_missing":
        return (f"Refused: no saved set at {d.get('set_path')!r}. Save the set and "
                f"pass the absolute .als path; the lock stays held.")
    if reason == "set_older_than_edit":
        return (f"Refused: {d.get('set_path')} is older than your last edit "
                f"(file {d.get('set_mtime'):.0f}, last edit {d.get('last_edit_ts'):.0f}). "
                f"Save first; the lock stays held.")
    if reason == "live_unsaved":
        problem = _UNSAVED_PROBLEMS.get(d.get("problem"), d.get("problem"))
        return (f"Refused: {problem}. Live's windows: {d.get('live_windows')}. "
                f"The lock stays held.")
    if reason == "save_failed":
        return ("Not released — the save failed, so the lock stays held. "
                + save_set(d.get("save") or {}))
    if reason == "disabled":
        return "The Live lock is not configured in this server process."
    return f"Refused by the Live lock: {reason}"
get_clip_notes = as_json
get_session_snapshot = as_json


def create_midi_track(result: Dict[str, Any]) -> str:
    return f"Created new MIDI track: {result.get('name', 'unknown')}"


def create_audio_track(result: Dict[str, Any]) -> str:
    return f"Created new audio track: {result.get('name', 'unknown')}"


def duplicate_track(result: Dict[str, Any]) -> str:
    source = result.get("source_track_name")
    if not result.get("duplicated"):
        # The count did not move. Say so plainly rather than reporting an
        # index the caller would then address something else through.
        return (f"Live did not duplicate track "
                f"{result.get('source_track_index')} ('{source}') — the "
                f"session still has {result.get('track_count_after')} tracks "
                f"and no copy was made")
    return (f"Duplicated '{source}' with its devices, mixer, routing and "
            f"clips — the copy is track {result.get('index')} "
            f"('{result.get('name')}'), directly below the source. Every "
            f"track index from {result.get('index')} down has shifted by one; "
            f"rename the copy with set_track_name")


def set_track_name(result: Dict[str, Any], name: str) -> str:
    return f"Renamed track to: {result.get('name', name)}"


def create_clip(track_index: int, clip_index: int, length: float) -> str:
    return f"Created new clip at track {track_index}, slot {clip_index} with length {length} beats"


def set_clip_gain(result: Dict[str, Any]) -> str:
    return (f"Set '{result.get('clip_name')}' on '{result.get('track_name')}' "
            f"to {result.get('gain_display') or result.get('gain')}")


def set_clip_warp(result: Dict[str, Any]) -> str:
    state = "on" if result.get("warping") else "off"
    span = result.get("length")
    span_text = f"; clip is now {span:.3f} beats long" if isinstance(span, (int, float)) else ""
    return (f"Warp {state} for '{result.get('clip_name')}' on "
            f"'{result.get('track_name')}'{span_text}")


# ── Warp vision ──────────────────────────────────────────────────────────────

get_clip_warp_markers = as_json

_IMAGE_KEY = (
    "Image key: yellow lines = warp markers (label = clip beat), cyan ticks = "
    "detected transients (taller = stronger), grey lines = song bars (number "
    "on top) and beats, green dots on the time axis = the estimated source "
    "beat grid, magenta = proposed/after markers. Bottom: low/mid/high "
    "energy strip and a log-frequency spectrogram (kick at the bottom, hats "
    "at the top)."
)


def _with_image(text: str, result: Dict[str, Any], fmt: str = "png") -> Any:
    """Text plus, when one was rendered, the image as MCP image content —
    the JSON alone must still be enough for a text-only model."""
    image = result.pop("image", None)
    body = text + "\n" + json.dumps(result, indent=1)
    if image is None:
        return body
    # Imported here so a text-only code path never needs the image type.
    from mcp.server.fastmcp import Image
    return [body + "\n\n" + _IMAGE_KEY, Image(data=image, format=fmt)]


def _alignment_phrase(a: Dict[str, Any]) -> str:
    if not a or not a.get("count"):
        return "no strong on-grid hits to measure"
    return (f"median {a['median_abs_ms']} ms / p90 {a['p90_abs_ms']} ms off "
            f"the grid over {a['count']} strong hits")


def analyze_clip_audio(result: Dict[str, Any]) -> Any:
    clip, win = result.get("clip", {}), result.get("window", {})
    tempo = result.get("source_tempo_estimate")
    tempo_text = (f"source ≈ {tempo['bpm']} BPM (confidence "
                  f"{tempo['confidence']}; candidates {tempo['candidates_bpm']})"
                  if tempo else "no clear source tempo")
    align = result.get("grid_alignment", {}).get("beat", {})
    text = (f"Analysed '{clip.get('clip_name')}' on '{clip.get('track_name')}' "
            f"{win.get('start_seconds')}-{win.get('end_seconds')} s of the file "
            f"(grid beats {win.get('start_beat')}-{win.get('end_beat')}): "
            f"{tempo_text}; the current warping plays it at "
            f"{result.get('warped_bpm_at_window_middle')} BPM here; "
            f"{result.get('transients_total')} transients; beat-grid alignment: "
            f"{_alignment_phrase(align)}. Next: suggest_warp_markers (dry run) "
            f"to see proposed fixes, or zoom with start_beat/end_beat.")
    return _with_image(text, result)


def edit_clip_warp_markers(result: Dict[str, Any]) -> str:
    applied, requested = result.get("applied"), result.get("requested")
    who = f"'{result.get('clip_name')}' on '{result.get('track_name')}'"
    if result.get("verified"):
        head = (f"Applied {applied}/{requested} warp edits to {who} — every "
                f"edit verified against Live's read-back; "
                f"{len(result.get('warp_markers_after') or [])} markers now")
    else:
        head = (f"Warp edits on {who} NOT fully verified: {applied}/{requested} "
                f"reported applied, {result.get('failed')} failed, "
                f"{result.get('not_attempted')} not attempted, "
                f"{len(result.get('mismatches') or [])} read-back mismatches. "
                f"warp_markers_before below is the state to restore if needed")
    return head + "\n" + json.dumps(result, indent=1)


def suggest_warp_markers(result: Dict[str, Any]) -> Any:
    before = _alignment_phrase(result.get("alignment_before"))
    after = _alignment_phrase(result.get("alignment_after"))
    n = result.get("edit_count")
    mode = result.get("mode")
    sub = result.get("subdivision_beats")
    applied = result.get("applied")
    if applied is not None:
        state = ("verified" if applied.get("verified")
                 else "NOT verified — see applied.failures / mismatches")
        text = (f"Applied {applied.get('applied')}/{applied.get('requested')} "
                f"warp edits ({mode}, {sub}-beat grid), {state}. Measured on "
                f"Live's read-back: before {before}; after {after}. Re-run "
                f"analyze_clip_audio on the same window to look again.")
    elif not n:
        text = (f"No edits proposed ({mode}, {sub}-beat grid): {before}. "
                f"Skipped: {result.get('skipped')}. Try another mode, a "
                f"finer/coarser subdivision, a larger tolerance, or a "
                f"narrower window.")
    else:
        text = (f"Dry run — nothing changed. {n} edits ({mode}, {sub}-beat "
                f"grid) would take the clip from {before} to {after} "
                f"(predicted). Look at the before/after image; to apply, call "
                f"again with apply=True, or send `edits` (edited as you see "
                f"fit) to edit_clip_warp_markers.")
    return _with_image(text, result)


def back_to_arrangement() -> str:
    return "All tracks returned to Arrangement playback"


get_track_routing = as_json


def set_track_routing(result: Dict[str, Any]) -> str:
    return (f"Set '{result.get('track_name')}' {result.get('field')} "
            f"to {result.get('value')}")


def set_count_in(result: Dict[str, Any]) -> str:
    metronome = f"metronome {'on' if result.get('metronome') else 'off'}"
    if result.get("count_in_writable") is False:
        return (f"Live's API exposes count-in as read-only on this build, so "
                f"it is still {result.get('count_in')} — set it in Live's UI "
                f"(Record button context menu). Applied the rest: {metronome}")
    return f"Count-in set to {result.get('count_in')}; {metronome}"


def set_track_send(result: Dict[str, Any], send_index: int) -> str:
    return (f"Set '{result.get('track_name')}' send {send_index} to "
            f"{result.get('display_value') or result.get('value')}")


_SAVE_ERRORS = {
    "ui_unreadable": "Live's windows could not be read (is Live running, and does this process have the Accessibility permission?)",
    "modal_open": "a dialog is open in Live",
    "save_panel_opened": "Live opened a Save panel, so this set has never been saved (Untitled); the panel was cancelled. Save it once by hand, or open a saved set",
    "not_confirmed": "the menu item was clicked but the save was not confirmed in time (title still unsaved, or the .als did not change)",
    "menu_item_not_found": "File > Save Live Set was not found in Live's menu",
    "menu_item_disabled": "File > Save Live Set is disabled",
    "menu_live_not_running": "Live is not running",
}


def _save_detail(result: Dict[str, Any]) -> str:
    v = result.get("verified") or {}
    bits = []
    if v.get("set_path"):
        bits.append(f"file {v.get('set_path')}"
                    + (" (modified time advanced)" if v.get("mtime_advanced") else ""))
    elif v.get("set_path_candidates"):
        bits.append(f"file not identified; candidates: {v.get('set_path_candidates')}")
    if v.get("unsaved_after") is False:
        bits.append("title shows no unsaved changes")
    return "; ".join(bits)


def save_set(result: Dict[str, Any]) -> str:
    if result.get("saved"):
        detail = _save_detail(result)
        return (f"Saved the Live Set via {result.get('method')}"
                + (f" — {detail}" if detail else ""))
    if result.get("ui_path") in (None, "not_allowed", "not_macos"):
        tail = {
            "not_allowed": " Pass allow_ui_automation=true (or run the server with "
                           "ABLETON_MCP_ALLOW_UI=1) to save through Live's File menu.",
            "not_macos": " The File-menu fallback needs macOS.",
        }.get(result.get("ui_path"), " The set must be saved from Live's UI.")
        return ("Could NOT save — this Live build exposes no callable save through the "
                f"Python API. Tried: {result.get('attempts')}.{tail}")
    error = result.get("error")
    why = _SAVE_ERRORS.get(error, error)
    if error == "modal_open":
        why += f" ({result.get('modal_dialog')!r}); close it first"
    menu = result.get("menu") or {}
    if menu.get("items"):
        why += f". File menu items: {menu.get('items')}"
    detail = _save_detail(result)
    return f"Could NOT save: {why}." + (f" ({detail})" if detail else "")


def checkpoint_set(result: Dict[str, Any]) -> str:
    if result.get("checkpointed"):
        return (f"Checkpoint saved: {result.get('checkpoint_path')} "
                f"({result.get('bytes')} bytes, sha256 {str(result.get('sha256'))[:12]}…), "
                f"a verified copy of {result.get('set_path')}")
    error = result.get("error")
    if error == "save_failed":
        return "No checkpoint: " + save_set(result.get("save") or {})
    if error == "set_path_unknown":
        return ("Saved, but no checkpoint: the open set's .als could not be identified "
                f"(candidates: {result.get('candidates')}). Pass set_path.")
    return (f"Checkpoint copy at {result.get('checkpoint_path')} does NOT match the "
            f"saved set; do not rely on it.")


def create_return_track(result: Dict[str, Any]) -> str:
    return (f"Created return track '{result.get('name')}' at return index "
            f"{result.get('return_index')}")


def set_track_arm(result: Dict[str, Any], track_index: int) -> str:
    state = "armed" if result.get("arm") else "disarmed"
    return f"Track {track_index} ('{result.get('track_name')}') {state}"


def set_track_monitoring(result: Dict[str, Any], track_index: int) -> str:
    return (f"Track {track_index} ('{result.get('track_name')}') monitoring set to "
            f"{result.get('monitoring')}")


get_device_parameters = as_json


def _asked(row: Dict[str, Any]) -> str:
    """" (asked '3.6 s')" on a display-string write, so a nearest-match or
    clamped landing is visible next to what was requested."""
    asked = row.get("requested_display")
    return f" (asked '{asked}')" if asked is not None else ""


def set_device_parameter(result: Dict[str, Any]) -> str:
    note = " (clamped)" if result.get("clamped") else ""
    return (f"Set {result.get('device_name')} '{result.get('parameter_name')}' "
            f"to {result.get('display_value') or result.get('value')}{note}"
            f"{_asked(result)}")


def set_device_parameters(result: Dict[str, Any]) -> str:
    landed, problems = [], []
    for row in result.get("parameters") or []:
        if row.get("error"):
            # The key as the caller wrote it, which is what makes a typo
            # findable — a resolved name would hide it.
            problems.append(f"{row.get('name')} ({row.get('error')})")
            continue
        shown = row.get("display_value") or row.get("value")
        landed.append(f"{row.get('name')} = {shown}"
                      + (" (clamped)" if row.get("clamped") else "")
                      + _asked(row))
    head = (f"Set {result.get('applied_count')} of "
            f"{result.get('requested_count')} parameters on "
            f"{result.get('device_name')}")
    body = f": {', '.join(landed)}" if landed else ""
    tail = f". NOT applied: {'; '.join(problems)}" if problems else ""
    return head + body + tail


get_rack_chains = as_json
analyze_audio = as_json
verify_audio = as_json
export_audio = as_json
bounce_track = as_json

_RENDER_REASONS = {
    "not_allowed": "this drives Live's UI; pass allow_ui_automation=true (or run the server with ABLETON_MCP_ALLOW_UI=1)",
    "not_macos": "UI-driven rendering needs macOS",
    "user_active": "the user is using the machine (idle {idle_s} s < {required_s} s); ask them, or wait",
    "ui_unreadable": "Live's windows cannot be read (is Live running, and does this process have the Accessibility permission?)",
    "modal_open": "a dialog is open in Live ({modal_dialog!r}); close it first",
    "transport_busy": "Live is playing or recording (playing={is_playing}, record={record_mode}); stop first",
    "soloed": "tracks are soloed ({soloed}) and strict_solo is on",
    "select_failed": "could not select the track ({selection})",
    "no_end": "{note}",
    "exists": "{path} already exists; pass overwrite=true to replace it",
    "menu": "the menu item could not be clicked ({menu})",
    "dialog_did_not_open": "the Export Audio/Video dialog did not open",
    "labels_missing": "the dialog has no field labelled {missing} (a Live update may have renamed it); nothing was set",
    "readback_mismatch": "the dialog does not show what was requested, so the export was cancelled: {diffs} {problems}",
    "export_button": "the Export button could not be pressed ({detail})",
    "save_panel": "the save panel could not be filled ({detail}); cancelled",
    "save_panel_name": "the save panel shows {shows!r}, not {wanted!r}; cancelled",
    "destructive": "{mode} replaces the track's clips and devices; pass confirm_destructive=true",
    "checkpoint_failed": "the safety checkpoint before a destructive bounce failed ({checkpoint})",
    "cannot_freeze": "Live says '{track}' cannot be frozen",
}


def render_refusal(e: Any) -> str:
    reason = getattr(e, "reason", str(e))
    details = getattr(e, "details", {}) or {}
    template = _RENDER_REASONS.get(reason, reason)
    try:
        why = template.format(**details)
    except (KeyError, IndexError, ValueError):
        why = f"{template} ({details})"
    return f"Refused, nothing rendered: {why}."


def _chain_line(chain: Dict[str, Any]) -> str:
    devices = ", ".join(d.get("name") for d in chain.get("devices") or []) or "no devices"
    vol = chain.get("volume_display") or chain.get("volume")
    return (f"chain {chain.get('index')} '{chain.get('name')}' (volume {vol}; "
            f"{devices})")


def insert_rack_chain(result: Dict[str, Any]) -> str:
    return (f"Added {_chain_line(result.get('chain') or {})} to "
            f"'{result.get('device_name')}' on '{result.get('track_name')}'; the "
            f"rack now has {result.get('chain_count')} chains")


def insert_chain_device(result: Dict[str, Any]) -> str:
    return (f"Inserted into '{result.get('device_name')}' "
            f"{_chain_line(result.get('chain') or {})}")


def set_chain_mixer(result: Dict[str, Any]) -> str:
    parts = []
    for key, change in (result.get("changes") or {}).items():
        if isinstance(change, dict):
            shown = change.get("display_value") or change.get("value")
            parts.append(f"{key} = {shown}" + (" (clamped)" if change.get("clamped") else "")
                         + _asked(change))
        else:
            parts.append(f"{key} = {change}")
    return (f"Set '{result.get('device_name')}' chain {result.get('chain_index')}: "
            + ", ".join(parts))


def load_device_from_path(result: Dict[str, Any]) -> str:
    new = result.get("new_devices") or []
    added = f"added {', '.join(new)}" if new else "no new device appeared"
    chain = ", ".join(f"{i} {n}" for i, n in enumerate(result.get("devices_after") or []))
    return (f"Loaded '{result.get('item_name')}' from {result.get('path')} onto "
            f"'{result.get('track_name')}': {added}. Devices now: {chain}")


get_plugin_state = as_json


def load_devices(result: Dict[str, Any]) -> str:
    chain = ", ".join(f"{d.get('index')} {d.get('name')}"
                      for d in result.get("devices") or [])
    head = (f"Loaded {result.get('loaded_count')} of {result.get('requested_count')} "
            f"items onto " + (f"'{result.get('track_name')}'" if result.get("track_name")
                          else f"track {result.get('track_index')}"))
    failed = result.get("failed")
    tail = ""
    if failed:
        tail = f". STOPPED at {failed.get('uri')}: {failed.get('error')}"
        if result.get("not_attempted"):
            tail += f"; not attempted: {result.get('not_attempted')}"
    return head + (f". Devices now: {chain}" if chain else "") + tail


get_plugin_presets = as_json


def _preset_label(index: Any, name: Any) -> str:
    """"12 'Warm Pad'", or the bare index when the list does not cover it
    (Live reports -1 while no preset is selected)."""
    if name is None:
        return f"index {index} (no preset)"
    return f"{index} '{name}'"


def set_plugin_preset(result: Dict[str, Any]) -> str:
    where = (f"{result.get('device')} (track {result.get('track_index')} "
             f"'{result.get('track_name')}')")
    requested = _preset_label(result.get("requested_index"),
                              result.get("requested_name"))
    previous = _preset_label(result.get("previous_index"),
                             result.get("previous_name"))
    if not result.get("verified"):
        # A read in the same tick as the write can be stale, so this is not
        # proof the select failed — and a blind second select in reply
        # cannot learn more than the first did. Say what Live reads back
        # and let a later call confirm.
        read_back = _preset_label(result.get("selected_index"),
                                  result.get("selected_name"))
        return (f"Asked {where} to select preset {requested}, but Live still "
                f"reads back {read_back} (was {previous}). The read-back can "
                f"lag the write within one tick, so re-read with "
                f"get_plugin_presets before selecting again.")
    return f"Selected preset {requested} on {where}; was {previous}"


def delete_device(result: Dict[str, Any], track_index: int) -> str:
    name = result.get("deleted_device_name")
    remaining = result.get("remaining_devices") or []
    chain = ", ".join(f"{d.get('index')} {d.get('name')}"
                      for d in remaining) or "nothing"
    if not result.get("deleted"):
        # The before/after counts disagreed. Never suggest simply calling
        # again: if the readback was merely stale, a second delete takes the
        # NEXT device and destroys a chain that was dialled in by hand.
        return (f"Did NOT delete '{name}' from track {track_index} — the "
                f"device count did not drop "
                f"({result.get('device_count_before')} before, "
                f"{result.get('remaining_device_count')} after). Nothing was "
                f"deleted twice on purpose; re-read the chain before trying "
                f"again. The track now reads: {chain}")
    warning = ("" if result.get("remaining_matches_expected", True) else
               " — but that is NOT the chain removing this device alone "
               "would leave, so re-read before deleting anything else")
    return (f"Deleted '{name}' from track {track_index}; "
            f"{result.get('remaining_device_count')} devices remain: "
            f"{chain}{warning}")


def set_track_volume(result: Dict[str, Any]) -> str:
    return (f"Set '{result.get('track_name')}' volume to "
            f"{result.get('display_value') or result.get('value')}")


def set_track_pan(result: Dict[str, Any]) -> str:
    return (f"Set '{result.get('track_name')}' pan to "
            f"{result.get('display_value') or result.get('value')}")


def set_track_mute(result: Dict[str, Any], track_index: int) -> str:
    state = "muted" if result.get("mute") else "unmuted"
    return f"Track {track_index} ('{result.get('track_name')}') {state}"


def set_track_solo(result: Dict[str, Any], track_index: int,
                   solo: bool) -> str:
    kind = "return track" if result.get("track_type") == "return" else "track"
    where = f"{kind} {track_index} ('{result.get('track_name')}')"
    if bool(result.get("solo")) != bool(solo):
        # Read back after the write, so this is Live's answer, not ours.
        wanted = "solo" if solo else "unsolo"
        now = "soloed" if result.get("solo") else "not soloed"
        text = f"Asked to {wanted} {where}, but Live reads it back as {now}"
    else:
        text = f"{where[0].upper()}{where[1:]} {'soloed' if solo else 'unsoloed'}"
    soloed = result.get("soloed_tracks") or []
    if soloed:
        text += f". Soloed now: {', '.join(soloed)}"
    else:
        text += ". Nothing is soloed now"
    exclusive = result.get("exclusive_solo")
    if exclusive is True:
        text += " (Live's Exclusive Solo preference is on)"
    elif exclusive is False:
        text += " (Exclusive Solo is off in Live's preferences, so solos add up)"
    return text


def delete_track(result: Dict[str, Any], track_index: int) -> str:
    deleted_name = result.get("deleted_track_name", "")
    remaining = result.get("remaining_track_count", "unknown")
    return f"Deleted track {track_index} ('{deleted_name}'); {remaining} tracks remain"


def create_audio_clip(result: Dict[str, Any], track_index: int,
                      clip_index: int) -> str:
    text = (f"Created audio clip '{result.get('name', 'clip')}' at track "
            f"{track_index}, slot {clip_index} (length "
            f"{result.get('length', '?')} beats)")
    if result.get("warp_error"):
        text += (f"; warping could NOT be turned off ({result['warp_error']}) "
                 f"— Live's tempo guess is still in play, so check the length")
    elif result.get("warping"):
        text += ("; warping is still ON, so this length is Live's tempo guess "
                 "rather than the file's own rate")
    else:
        text += "; imported unwarped, so it plays at its recorded rate"
    matches = result.get("length_matches_expected")
    if matches is False:
        # Hedged deliberately: the length was read in the same tick that
        # cleared warping, and unwarping is what changes a clip's beat
        # length, so a stale read can report a mismatch for a fine import.
        # Telling the user to delete and re-import on that evidence would
        # destroy good work.
        text += (f". That does NOT match the {result.get('expected_beats')} "
                 f"beats you expected. Most likely Live's import guessed a "
                 f"wrong source tempo — but this length was read in the same "
                 f"tick the warp was cleared, so re-read the clip before "
                 f"acting; if it really is wrong, set_clip_warp it off")
    elif matches is True:
        text += f", matching the expected {result.get('expected_beats')} beats"
    return text


def _clip_location(result: Dict[str, Any], track_index: int,
                   clip_index: int) -> str:
    """Where a note edit landed, in the view it actually happened in."""
    where = ("arrangement clip" if result.get("arrangement") else "slot")
    return f"track {track_index}, {where} {clip_index}"


def _clip_phrase(result: Dict[str, Any], track_index: int,
                 clip_index: int) -> str:
    """The clip a note edit hit, named if it has a name.

    Live leaves a freshly created clip's name empty, and "notes added to ''"
    reads like a bug rather than like an unnamed clip.
    """
    where = _clip_location(result, track_index, clip_index)
    name = result.get("clip_name")
    return f"'{name}' ({where})" if name else f"the clip at {where}"


def add_notes_to_clip(result: Dict[str, Any], track_index: int,
                      clip_index: int) -> str:
    where = _clip_phrase(result, track_index, clip_index)
    requested = result.get("requested")
    added = result.get("added")
    total = result.get("clip_note_count")
    if added is None:
        # The clip could not be counted. "I could not check" and "none
        # arrived" are different answers and must not be conflated.
        return (f"Sent {requested} notes to {where} — this clip's notes could "
                f"not be counted, so how many Live took is unverified; read "
                f"it back with get_clip_notes")
    if added != requested:
        return (f"Live took {added} of the {requested} notes sent to {where}; "
                f"the clip now holds {total}. Notes written past the clip's "
                f"marker window are stored but never sound — read it back "
                f"with get_clip_notes")
    return (f"Added {added} notes to {where}; the clip now holds {total}")


def clear_notes_from_clip(result: Dict[str, Any], track_index: int,
                          clip_index: int) -> str:
    remaining = result.get("clip_note_count")
    tail = ("" if remaining in (0, None)
            else f" — {remaining} note(s) remain, which should not happen; "
                 f"read the clip back")
    return "Cleared {n} note(s) from clip '{name}' ({where}){tail}".format(
        n=result.get("cleared_count", "?"),
        name=result.get("clip_name", "clip"),
        where=_clip_location(result, track_index, clip_index),
        tail=tail,
    )


def set_clip_name(track_index: int, clip_index: int, name: str) -> str:
    return f"Renamed clip at track {track_index}, slot {clip_index} to '{name}'"


def set_arrangement_clip_name(track_index: int, clip_index: int,
                              name: str) -> str:
    return f"Renamed arrangement clip at track {track_index}, index {clip_index} to '{name}'"


def set_tempo(tempo: float) -> str:
    return f"Set tempo to {tempo} BPM"


get_song_scale = as_json


def _scale_label(state: Dict[str, Any]) -> str:
    """"D Dorian, Scale Mode on" — Scale Mode only when Live reports it."""
    label = f"{state.get('root_name')} {state.get('scale_name')}"
    mode = state.get("scale_mode")
    if mode is not None:
        label += f", Scale Mode {'on' if mode else 'off'}"
    return label


def set_song_scale(result: Dict[str, Any]) -> str:
    now = _scale_label(result)
    was = _scale_label(result.get("previous") or {})
    if result.get("verified"):
        return f"Song scale is now {now}; was {was}"
    requested = result.get("requested") or {}
    asked = []
    if "root_name" in requested:
        asked.append(f"root {requested['root_name']}")
    if "scale_name" in requested:
        asked.append(f"scale '{requested['scale_name']}'")
    if "scale_mode" in requested:
        asked.append(f"Scale Mode {'on' if requested['scale_mode'] else 'off'}")
    mismatched = ", ".join(result.get("mismatched") or [])
    text = (f"Asked for {', '.join(asked)}, but Live reads back {now} "
            f"(was {was}) — {mismatched} did not take")
    if "scale_name" in (result.get("mismatched") or []):
        # Live's API has no list of scale names to validate against, so a
        # name it does not know is only visible here, on the read-back.
        text += (". Live most likely does not know that scale name; use it "
                 "exactly as get_song_scale or Live's Scale chooser spells "
                 "it (e.g. 'Major', 'Minor', 'Dorian')")
    return text


def load_instrument_or_effect(result: Dict[str, Any], track_index: int,
                              uri: str) -> str:
    # Check if the instrument was loaded successfully
    if result.get("loaded", False):
        new_devices = result.get("new_devices", [])
        if new_devices:
            return f"Loaded instrument with URI '{uri}' on track {track_index}. New devices: {', '.join(new_devices)}"
        else:
            devices = result.get("devices_after", [])
            if devices:
                return f"Loaded instrument with URI '{uri}' on track {track_index}. Devices on track: {', '.join(devices)}"
            item = result.get("item_name") or uri
            return f"Loaded '{item}' on track {track_index}"
    else:
        return f"Failed to load instrument with URI '{uri}'"


def fire_clip(track_index: int, clip_index: int) -> str:
    return f"Started playing clip at track {track_index}, slot {clip_index}"


def stop_clip(track_index: int, clip_index: int) -> str:
    return f"Stopped clip at track {track_index}, slot {clip_index}"


delete_clip = as_json


def start_playback() -> str:
    return "Started playback"


def stop_playback() -> str:
    return "Stopped playback"


def format_tree(item: Dict[str, Any], indent: int = 0) -> str:
    """Render one browser-tree node (and its children) as indented bullets."""
    output = ""
    if item:
        prefix = "  " * indent
        name = item.get("name", "Unknown")
        path = item.get("path", "")
        has_more = item.get("has_more", False)

        # Add this item
        output += f"{prefix}• {name}"
        if path:
            output += f" (path: {path})"
        if has_more:
            output += " [...]"
        output += "\n"

        # Add children
        for child in item.get("children", []):
            output += format_tree(child, indent + 1)
    return output


def get_browser_tree(result: Dict[str, Any], category_type: str) -> str:
    # Check if we got any categories
    if "available_categories" in result and len(result.get("categories", [])) == 0:
        available_cats = result.get("available_categories", [])
        return (f"No categories found for '{category_type}'. "
                f"Available browser categories: {', '.join(available_cats)}")

    # Format the tree in a more readable way
    total_folders = result.get("total_folders", 0)
    formatted_output = f"Browser tree for '{category_type}' (showing {total_folders} folders):\n\n"

    # Format each category
    for category in result.get("categories", []):
        formatted_output += format_tree(category)
        formatted_output += "\n"

    return formatted_output


def get_browser_items_at_path(result: Dict[str, Any]) -> str:
    # Check if there was an error with available categories
    if "error" in result and "available_categories" in result:
        error = result.get("error", "")
        available_cats = result.get("available_categories", [])
        return (f"Error: {error}\n"
                f"Available browser categories: {', '.join(available_cats)}")

    return as_json(result)


def load_drum_kit(result: Dict[str, Any]) -> str:
    """Render the service's stage-tagged outcome dict. Only the exception
    path is an "Error ..." string (the decorator's job); the intermediate
    failures below have always been success-path strings."""
    stage = result["stage"]
    if stage == "rack_failed":
        return f"Failed to load drum rack with URI '{result['rack_uri']}'"
    if stage == "kit_lookup_failed":
        return f"Loaded drum rack but failed to find drum kit: {result['error']}"
    if stage == "no_loadable":
        return f"Loaded drum rack but no loadable drum kits found at '{result['kit_path']}'"
    return f"Loaded drum rack and kit '{result['kit_name']}' on track {result['track_index']}"


def switch_to_arrangement_view() -> str:
    return "Switched to Arrangement view"


def set_arrangement_time(result: Dict[str, Any], time: float) -> str:
    if result.get("settled") is False:
        return (f"Playhead set to beat {result.get('requested', time)} — Live "
                f"still reported {result.get('current_song_time')} while the "
                f"transport was settling; the write lands on the next tick "
                f"(re-read transport state to confirm)")
    return f"Playhead moved to beat {result.get('current_song_time', time)}"


get_arrangement_clips = as_json


def _loop_phase_note(result: Dict[str, Any]) -> str:
    """The override's footnote: which looping clips were re-phased.

    Only ever present when the caller passed allow_loop_phase_reset, so its
    absence is silence and its presence is a decision on the record.
    """
    victims = result.get("loop_phase_reset")
    if not victims:
        return ""
    named = ", ".join(f"'{name}'" for name in victims)
    return (f" — loop phase was RESET on {named}, which now replay from the "
            f"top of their loop; check those bars")


def duplicate_to_arrangement(result: Dict[str, Any], track_index: int,
                             clip_index: int,
                             destination_time: Any) -> str:
    clip_name = result.get("clip_name", "clip")
    track_name = result.get("track_name", f"track {track_index}")
    times = result.get("destination_times")
    if times is None:
        return (
            f"Duplicated '{clip_name}' from Session slot {clip_index} "
            f"on '{track_name}' to arrangement at beat {destination_time}"
            + _loop_phase_note(result)
        )

    placements = result.get("placements") or []
    placed = result.get("placed_count")
    requested = result.get("requested_count")
    if result.get("success"):
        beats = ", ".join(str(row.get("destination_time"))
                          for row in placements)
        return (
            f"Stamped '{clip_name}' from Session slot {clip_index} on "
            f"'{track_name}' at {placed} positions: beats {beats}"
            + _loop_phase_note(result)
        )
    # A partial run is the case this whole shape exists for: the placements
    # that landed are REAL and stay on the timeline, so the text has to name
    # both halves rather than reading as a failure.
    failures = "; ".join(
        f"beat {row.get('destination_time')}: {row.get('error')}"
        for row in placements if not row.get("ok")
    )
    landed = ", ".join(str(row.get("destination_time"))
                       for row in placements if row.get("ok")) or "none"
    return (
        f"Stamped '{clip_name}' at {placed} of {requested} positions on "
        f"'{track_name}' — the rest were refused and nothing was placed for "
        f"them. Landed at beats: {landed}. Refused — {failures}"
        + _loop_phase_note(result)
    )


def create_locator(result: Dict[str, Any], name: str, time: float) -> str:
    return (
        f"Locator '{result.get('name', name)}' set at beat "
        f"{result.get('time', time)}"
    )


def jump_to_locator(result: Dict[str, Any], name: str, time: Any) -> str:
    cue_name = result.get("name", name)
    cue_time = result.get("time", time)
    if result.get("start_marker_set"):
        return (
            f"Jumped to locator '{cue_name}' at beat {cue_time} — the "
            f"transport was stopped, so the start marker is planted there: "
            f"play and record now launch from that beat"
        )
    return (
        f"Sent the jump to locator '{cue_name}' at beat {cue_time} while the "
        f"transport was playing — Live applies it at the song's quantization, "
        f"and the start marker is not re-aimed; stop and jump again to make "
        f"play/record launch from it"
    )


def delete_locator(result: Dict[str, Any], name: str, time: Any) -> str:
    cue_name = result.get("name", name)
    cue_time = result.get("time", time)
    if result.get("success"):
        return (f"Deleted locator '{cue_name}' at beat {cue_time}; "
                f"{result.get('cue_point_count')} locators remain")
    if result.get("deleted"):
        # The beat is clear but the count did not fall by exactly one, which
        # is what a toggle looks like when it also CREATED something.
        return (f"Beat {cue_time} is clear of locators, but the count went "
                f"from {result.get('cue_point_count_before')} to "
                f"{result.get('cue_point_count')} rather than down by one — "
                f"read the locators before deleting another")
    return (f"Locator '{cue_name}' is STILL at beat {cue_time} — the toggle "
            f"did not remove it and there are now "
            f"{result.get('cue_point_count')} locators. Re-read them before "
            f"retrying")


def trim_arrangement_clip(result: Dict[str, Any], track_index: int,
                          clip_index: int, start_time: Any,
                          end_time: Any) -> str:
    spans = (f"clip spans beats {result.get('start_time')} to "
             f"{result.get('end_time')}")
    refusals = result.get("refusals") or []
    trimmed = result.get("trimmed_head") or result.get("trimmed_tail")
    if refusals:
        # Each refusal string from the script says what happened to its
        # edge, so no blanket "untouched" claim is added here.
        prefix = "Partial trim" if trimmed else "Trim refused"
        return f"{prefix}: {'; '.join(refusals)} — the {spans}"
    if not trimmed:
        return f"Nothing to trim — the {spans} already"
    return f"Trimmed arrangement clip {clip_index}: the {spans}"


def delete_arrangement_clip(result: Dict[str, Any], track_index: int,
                            clip_index: Any) -> str:
    deletions = result.get("deletions")
    if deletions is None:
        name = result.get("deleted_clip_name") or f"clip {clip_index}"
        # The handler verifies with a before/after count. Saying "Deleted" when
        # it reports otherwise is the same lie the plural branch and
        # delete_device already refuse to tell — and a highest-index-first
        # sweep built on that lie removes the wrong clips next.
        if result.get("deleted") is False:
            return (
                f"Did NOT delete arrangement clip '{name}' on track "
                f"{track_index} — the clip count did not change, so nothing "
                f"was removed and no indices shifted. Re-read "
                f"get_arrangement_clips and retry; do not assume a shift"
            )
        return (
            f"Deleted arrangement clip '{name}' (beats "
            f"{result.get('start_time')} to {result.get('end_time')}) from "
            f"track {track_index} — the audio file on disk is untouched. "
            f"Remaining arrangement clip indices on this track have shifted; "
            f"re-read get_arrangement_clips before the next arrangement edit"
        )

    gone = ", ".join(f"'{row.get('deleted_clip_name')}' at {row.get('start_time')}"
                     for row in deletions if row.get("ok")) or "none"
    tail = (f" {result.get('arrangement_clip_count')} clips remain on the "
            f"track; the audio files on disk are untouched")
    if result.get("success"):
        return (f"Deleted {result.get('deleted_count')} arrangement clips "
                f"from track {track_index}: {gone}.{tail}")
    # Beats, not indices, so the caller can retry exactly what failed
    # without re-deriving anything from a list that has now shifted.
    failures = "; ".join(f"beat {row.get('start_time')}: {row.get('error')}"
                         for row in deletions if not row.get("ok"))
    return (f"Deleted {result.get('deleted_count')} of "
            f"{result.get('requested_count')} arrangement clips from track "
            f"{track_index}: {gone}. FAILED — {failures}.{tail}")


def move_arrangement_clip(result: Dict[str, Any], track_index: int,
                          clip_index: int, destination_time: float) -> str:
    name = result.get("clip_name") or f"clip {clip_index}"
    if result.get("moved") is False:
        return (
            f"Arrangement clip '{name}' already starts at beat "
            f"{result.get('start_time')} — nothing moved"
        )
    return (
        f"Moved arrangement clip '{name}' — it now spans beats "
        f"{result.get('start_time')} to {result.get('end_time')} on track "
        f"{track_index}. Clip indices on this track have shifted "
        f"(start-time order); re-read get_arrangement_clips before the "
        f"next arrangement edit"
    )


def duplicate_arrangement_clip(result: Dict[str, Any], track_index: int,
                               clip_index: int,
                               destination_time: float) -> str:
    name = result.get("clip_name") or f"clip {clip_index}"
    return (
        f"Duplicated arrangement clip '{name}' (source beats "
        f"{result.get('source_start_time')} to "
        f"{result.get('source_end_time')}) to beat "
        f"{result.get('destination_time', destination_time)} on track "
        f"{track_index}. Clip indices on this track have shifted "
        f"(start-time order); re-read get_arrangement_clips to confirm the "
        f"copy and find its index"
        + _loop_phase_note(result)
    )


# ── Boundary refusals (raised by controllers, worded here) ───────────────────

def invalid_root_note(value: Any) -> str:
    """set_song_scale's root_note was neither 0-11 nor a note name."""
    return (f"root_note={value!r} is not a key root. Give 0-11 (0 = C, "
            f"11 = B) or a note name such as 'C', 'F#', 'Bb' or 'c#'; "
            f"nothing was sent to Ableton.")


def song_scale_empty_name() -> str:
    return ("scale_name is empty — give a scale name as Live spells it "
            "(e.g. 'Major', 'Minor', 'Dorian'), or leave it out to keep the "
            "current scale. Nothing was sent to Ableton.")


def song_scale_nothing_to_set() -> str:
    return ("set_song_scale needs at least one of root_note, scale_name or "
            "scale_mode; nothing was sent to Ableton. Use get_song_scale to "
            "read the current key and scale.")


def master_cannot_be_soloed() -> str:
    """set_track_solo aimed at the master, which has no solo button."""
    return ("The master track cannot be soloed in Live — it has no solo "
            "button. Solo a regular or return track instead; nothing was "
            "sent to Ableton.")


def alias_conflict(first_name: str, first_value: Any,
                   second_name: str, second_value: Any) -> str:
    """Two spellings of one argument, given two different values."""
    return (
        f"{first_name}={first_value!r} and {second_name}={second_value!r} are "
        f"two names for the SAME argument but were given different values, so "
        f"nothing was sent to Ableton. Pass just one of them."
    )


def conflicting_forms(first_name: str, first_value: Any,
                      second_name: str, second_value: Any,
                      advice: str) -> str:
    """Two ways of saying the same thing, where only one can be honoured.

    Distinct from :func:`alias_conflict`: these are not two spellings of one
    argument but two different forms of a request (one position or a run of
    them, an ordinal or a beat), and the wire carries exactly one. Ignoring
    the loser silently is what turns "stamp here and at these" into a
    placement the caller believes landed and never did.
    """
    return (
        f"{first_name}={first_value!r} and {second_name}={second_value!r} were "
        f"both given, and only one of them can be honoured — so nothing was "
        f"sent to Ableton. {advice}"
    )


def argument_required(canonical: str, aliases: Sequence[str],
                      what: str) -> str:
    """A required argument arrived under none of its accepted spellings."""
    spellings = (f" (also accepted as {' or '.join(aliases)})"
                 if aliases else "")
    return (f"{canonical}{spellings} is required — give {what}. Nothing was "
            f"sent to Ableton.")


# ── Error translation ────────────────────────────────────────────────────────

# Tool name → the phrase inside its "Error {phrase}: {e}" return string,
# extracted verbatim from the pre-split tool bodies. The goldens replay every
# one of these byte-for-byte, so a slipped phrase fails loudly.
ERROR_PHRASES: Dict[str, str] = {
    "get_session_info": "getting session info",
    "get_remote_script_info": "getting remote script info",
    "get_track_info": "getting track info",
    "get_live_status": "getting Live status",
    "get_mix_state": "getting the mix state",
    "acquire_live_lock": "acquiring the Live lock",
    "release_live_lock": "releasing the Live lock",
    "checkpoint_set": "checkpointing the set",
    "load_devices": "loading devices",
    "load_device_from_path": "loading the preset file",
    "get_rack_chains": "reading rack chains",
    "analyze_audio": "analysing audio",
    "verify_audio": "verifying audio",
    "export_audio": "exporting audio",
    "bounce_track": "bouncing the track",
    "insert_rack_chain": "adding a rack chain",
    "insert_chain_device": "inserting a device into the chain",
    "set_chain_mixer": "setting the chain mixer",
    "get_plugin_state": "reading plugin state",
    "get_clip_notes": "getting clip notes",
    "get_session_snapshot": "getting session snapshot",
    "create_midi_track": "creating MIDI track",
    "create_audio_track": "creating audio track",
    "duplicate_track": "duplicating track",
    "set_track_name": "setting track name",
    "create_clip": "creating clip",
    "set_clip_gain": "setting clip gain",
    "set_clip_warp": "setting clip warp",
    "get_clip_warp_markers": "reading warp markers",
    "edit_clip_warp_markers": "editing warp markers",
    "analyze_clip_audio": "analyzing clip audio",
    "suggest_warp_markers": "suggesting warp markers",
    "back_to_arrangement": "returning to arrangement",
    "get_track_routing": "getting track routing",
    "set_track_routing": "setting track routing",
    "set_count_in": "setting count-in",
    "set_track_send": "setting track send",
    "save_set": "saving set",
    "create_return_track": "creating return track",
    "set_track_arm": "arming track",
    "set_track_monitoring": "setting monitoring",
    "get_device_parameters": "getting device parameters",
    "set_device_parameter": "setting device parameter",
    "set_device_parameters": "setting device parameters",
    "get_plugin_presets": "getting plugin presets",
    "set_plugin_preset": "setting plugin preset",
    "delete_device": "deleting device",
    "set_track_volume": "setting track volume",
    "set_track_pan": "setting track pan",
    "set_track_mute": "setting track mute",
    "set_track_solo": "setting track solo",
    "delete_track": "deleting track",
    "create_audio_clip": "creating audio clip",
    "add_notes_to_clip": "adding notes to clip",
    "clear_notes_from_clip": "clearing notes from clip",
    "set_clip_name": "setting clip name",
    "set_arrangement_clip_name": "setting arrangement clip name",
    "set_tempo": "setting tempo",
    "get_song_scale": "getting song scale",
    "set_song_scale": "setting song scale",
    "load_instrument_or_effect": "loading instrument by URI",
    "fire_clip": "firing clip",
    "stop_clip": "stopping clip",
    "delete_clip": "deleting clip",
    "start_playback": "starting playback",
    "stop_playback": "stopping playback",
    "load_drum_kit": "loading drum kit",
    "switch_to_arrangement_view": "switching to arrangement view",
    "set_arrangement_time": "setting arrangement time",
    "get_arrangement_clips": "getting arrangement clips",
    "duplicate_to_arrangement": "duplicating clip to arrangement",
    "create_locator": "creating locator",
    "jump_to_locator": "jumping to locator",
    "delete_locator": "deleting locator",
    "trim_arrangement_clip": "trimming arrangement clip",
    "delete_arrangement_clip": "deleting arrangement clip",
    "move_arrangement_clip": "moving arrangement clip",
    "duplicate_arrangement_clip": "duplicating arrangement clip",
}


def _get_browser_tree_error(e: Exception) -> str:
    error_msg = str(e)
    if "Browser is not available" in error_msg:
        return ("Error: The Ableton browser is not available. "
                "Make sure Ableton Live is fully loaded and try again.")
    elif "Could not access Live application" in error_msg:
        return ("Error: Could not access the Ableton Live application. "
                "Make sure Ableton Live is running and the Remote Script is loaded.")
    else:
        return f"Error getting browser tree: {error_msg}"


def _get_browser_items_at_path_error(e: Exception) -> str:
    error_msg = str(e)
    if "Browser is not available" in error_msg:
        return ("Error: The Ableton browser is not available. "
                "Make sure Ableton Live is fully loaded and try again.")
    elif "Could not access Live application" in error_msg:
        return ("Error: Could not access the Ableton Live application. "
                "Make sure Ableton Live is running and the Remote Script is loaded.")
    elif "Unknown or unavailable category" in error_msg:
        return f"Error: {error_msg}. Please check the available categories using get_browser_tree."
    elif "Path part" in error_msg and "not found" in error_msg:
        return f"Error: {error_msg}. Please check the path and try again."
    else:
        return f"Error getting browser items at path: {error_msg}"


# The tools whose error handling is bespoke: they sniff the failure message
# and answer with guidance instead of the uniform "Error {phrase}: {e}".
# Ported branch-for-branch from the pre-split bodies.
ERROR_RENDERERS: Dict[str, Callable[[Exception], str]] = {
    "get_browser_tree": _get_browser_tree_error,
    "get_browser_items_at_path": _get_browser_items_at_path_error,
}


# Transport failures that usually mean Live is not answering *right now* —
# closed, still starting, opening another set, or blocked by a dialog — not
# that the call was wrong. Agents used to retry blindly or give up; the hint
# names the one call that tells them which, and that no restart is needed
# (the transport reconnects on the next call by itself).
_UNREACHABLE_MARKERS = ("Could not connect to Ableton", "Connection to Ableton lost",
                        "Not connected to Ableton")
UNREACHABLE_HINT = (" (Live may be closed, starting up, opening a set or showing a "
                    "dialog; get_live_status shows which. The next call reconnects "
                    "automatically.)")


def error_text(tool_name: str, e: Exception) -> str:
    """The exact string tool ``tool_name`` returns when ``e`` escapes it."""
    renderer = ERROR_RENDERERS.get(tool_name)
    if renderer is not None:
        text = renderer(e)
    else:
        text = f"Error {ERROR_PHRASES[tool_name]}: {e}"
    if any(marker in str(e) for marker in _UNREACHABLE_MARKERS):
        text += UNREACHABLE_HINT
    return text
