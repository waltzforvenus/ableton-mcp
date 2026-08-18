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
from typing import Any, Callable, Dict, List, Union


def as_json(result: Dict[str, Any]) -> str:
    """The shared renderer for the JSON-returning tools."""
    return json.dumps(result, indent=2)


# ── Success renderers, in tools.py order ─────────────────────────────────────

get_session_info = as_json
get_remote_script_info = as_json
get_track_info = as_json
get_clip_notes = as_json
get_session_snapshot = as_json


def create_midi_track(result: Dict[str, Any]) -> str:
    return f"Created new MIDI track: {result.get('name', 'unknown')}"


def create_audio_track(result: Dict[str, Any]) -> str:
    return f"Created new audio track: {result.get('name', 'unknown')}"


def set_track_name(result: Dict[str, Any], name: str) -> str:
    return f"Renamed track to: {result.get('name', name)}"


def create_clip(track_index: int, clip_index: int, length: float) -> str:
    return f"Created new clip at track {track_index}, slot {clip_index} with length {length} beats"


def set_clip_gain(result: Dict[str, Any]) -> str:
    return (f"Set '{result.get('clip_name')}' on '{result.get('track_name')}' "
            f"to {result.get('gain_display') or result.get('gain')}")


def back_to_arrangement() -> str:
    return "All tracks returned to Arrangement playback"


get_track_routing = as_json


def set_track_routing(result: Dict[str, Any]) -> str:
    return (f"Set '{result.get('track_name')}' {result.get('field')} "
            f"to {result.get('value')}")


def set_count_in(result: Dict[str, Any]) -> str:
    return (f"Count-in set to {result.get('count_in')}; "
            f"metronome {'on' if result.get('metronome') else 'off'}")


def set_track_send(result: Dict[str, Any], send_index: int) -> str:
    return (f"Set '{result.get('track_name')}' send {send_index} to "
            f"{result.get('display_value') or result.get('value')}")


def save_set(result: Dict[str, Any]) -> str:
    if result.get("saved"):
        return f"Saved the Live Set via {result.get('method')}"
    return ("Could NOT save — this Live build exposes no callable save through the "
            f"Python API. Tried: {result.get('attempts')}. The set must be saved from Live's UI.")


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


def set_device_parameter(result: Dict[str, Any]) -> str:
    note = " (clamped)" if result.get("clamped") else ""
    return (f"Set {result.get('device_name')} '{result.get('parameter_name')}' "
            f"to {result.get('display_value') or result.get('value')}{note}")


def delete_device(result: Dict[str, Any], track_index: int) -> str:
    return (f"Deleted '{result.get('deleted_device_name')}' from track {track_index}; "
            f"{result.get('remaining_device_count')} devices remain")


def set_track_volume(result: Dict[str, Any]) -> str:
    return (f"Set '{result.get('track_name')}' volume to "
            f"{result.get('display_value') or result.get('value')}")


def set_track_pan(result: Dict[str, Any]) -> str:
    return (f"Set '{result.get('track_name')}' pan to "
            f"{result.get('display_value') or result.get('value')}")


def set_track_mute(result: Dict[str, Any], track_index: int) -> str:
    state = "muted" if result.get("mute") else "unmuted"
    return f"Track {track_index} ('{result.get('track_name')}') {state}"


def delete_track(result: Dict[str, Any], track_index: int) -> str:
    deleted_name = result.get("deleted_track_name", "")
    remaining = result.get("remaining_track_count", "unknown")
    return f"Deleted track {track_index} ('{deleted_name}'); {remaining} tracks remain"


def create_audio_clip(result: Dict[str, Any], track_index: int,
                      clip_index: int) -> str:
    return f"Created audio clip '{result.get('name', 'clip')}' at track {track_index}, slot {clip_index} (length {result.get('length', '?')} beats)"


def add_notes_to_clip(track_index: int, clip_index: int,
                      notes: List[Dict[str, Union[int, float, bool]]]) -> str:
    return f"Added {len(notes)} notes to clip at track {track_index}, slot {clip_index}"


def clear_notes_from_clip(result: Dict[str, Any], track_index: int,
                          clip_index: int) -> str:
    return "Cleared {n} note(s) from clip '{name}' (track {t}, slot {c})".format(
        n=result.get("cleared_count", "?"),
        name=result.get("clip_name", "clip"),
        t=track_index,
        c=clip_index,
    )


def set_clip_name(track_index: int, clip_index: int, name: str) -> str:
    return f"Renamed clip at track {track_index}, slot {clip_index} to '{name}'"


def set_arrangement_clip_name(track_index: int, clip_index: int,
                              name: str) -> str:
    return f"Renamed arrangement clip at track {track_index}, index {clip_index} to '{name}'"


def set_tempo(tempo: float) -> str:
    return f"Set tempo to {tempo} BPM"


def load_instrument_or_effect(result: Dict[str, Any], track_index: int,
                              uri: str) -> str:
    # Check if the instrument was loaded successfully
    if result.get("loaded", False):
        new_devices = result.get("new_devices", [])
        if new_devices:
            return f"Loaded instrument with URI '{uri}' on track {track_index}. New devices: {', '.join(new_devices)}"
        else:
            devices = result.get("devices_after", [])
            return f"Loaded instrument with URI '{uri}' on track {track_index}. Devices on track: {', '.join(devices)}"
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
    return f"Playhead moved to beat {result.get('current_song_time', time)}"


get_arrangement_clips = as_json


def duplicate_to_arrangement(result: Dict[str, Any], track_index: int,
                             clip_index: int, destination_time: float) -> str:
    clip_name = result.get("clip_name", "clip")
    track_name = result.get("track_name", f"track {track_index}")
    return (
        f"Duplicated '{clip_name}' from Session slot {clip_index} "
        f"on '{track_name}' to arrangement at beat {destination_time}"
    )


def create_locator(result: Dict[str, Any], name: str, time: float) -> str:
    return (
        f"Locator '{result.get('name', name)}' set at beat "
        f"{result.get('time', time)}"
    )


# ── Error translation ────────────────────────────────────────────────────────

# Tool name → the phrase inside its "Error {phrase}: {e}" return string,
# extracted verbatim from the pre-split tool bodies. The goldens replay every
# one of these byte-for-byte, so a slipped phrase fails loudly.
ERROR_PHRASES: Dict[str, str] = {
    "get_session_info": "getting session info",
    "get_remote_script_info": "getting remote script info",
    "get_track_info": "getting track info",
    "get_clip_notes": "getting clip notes",
    "get_session_snapshot": "getting session snapshot",
    "create_midi_track": "creating MIDI track",
    "create_audio_track": "creating audio track",
    "set_track_name": "setting track name",
    "create_clip": "creating clip",
    "set_clip_gain": "setting clip gain",
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
    "delete_device": "deleting device",
    "set_track_volume": "setting track volume",
    "set_track_pan": "setting track pan",
    "set_track_mute": "setting track mute",
    "delete_track": "deleting track",
    "create_audio_clip": "creating audio clip",
    "add_notes_to_clip": "adding notes to clip",
    "clear_notes_from_clip": "clearing notes from clip",
    "set_clip_name": "setting clip name",
    "set_arrangement_clip_name": "setting arrangement clip name",
    "set_tempo": "setting tempo",
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


def error_text(tool_name: str, e: Exception) -> str:
    """The exact string tool ``tool_name`` returns when ``e`` escapes it."""
    renderer = ERROR_RENDERERS.get(tool_name)
    if renderer is not None:
        return renderer(e)
    return f"Error {ERROR_PHRASES[tool_name]}: {e}"
