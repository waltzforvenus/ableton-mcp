# ableton_mcp_server.py
"""Controllers: the MCP tool functions (docs/REFACTOR_PLAN.md §3.2, §3.3).

Tools reach their dependencies through ``_deps(ctx)`` — the one
FastMCP-specific hop to the ``Deps`` the composition root's lifespan yielded.
Registration is decoupled from definition: the ``tool`` decorator only
appends to ``TOOLS``; ``app.build_app`` feeds that list to FastMCP. No env
reads, no logging configuration, no sockets — importing this module has zero
side effects.
"""
from mcp.server.fastmcp import Context
import json
import logging
from typing import TYPE_CHECKING, Dict, Any, List, Union

from .handshake import CapabilityError

if TYPE_CHECKING:  # annotation-only: app.py imports this module at runtime
    from .app import Deps

logger = logging.getLogger("AbletonMCPServer")

# Every function the app registers as an MCP tool, in definition order.
TOOLS: list = []


def tool(fn):
    """Mark ``fn`` as an MCP tool. Deliberately minimal: registration only
    (``app.build_app`` hands TOOLS to ``FastMCP.add_tool``); the shared
    error-handling wrapper arrives with the MVC split (plan PR9)."""
    TOOLS.append(fn)
    return fn


def _deps(ctx: Context) -> "Deps":
    """The Deps the composition root's lifespan yielded for this session."""
    return ctx.request_context.lifespan_context


# Core Tool endpoints

@tool
def get_session_info(ctx: Context) -> str:
    """Get detailed information about the current Ableton session
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("get_session_info")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting session info from Ableton: {str(e)}")
        return f"Error getting session info: {str(e)}"


@tool
def get_remote_script_info(ctx: Context) -> str:
    """
    Report Ableton Remote Script version and capabilities (handshake).

    Use this to verify the Live-side bridge matches this MCP server package.
    """
    try:
        from .remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION

        d = _deps(ctx)
        # Touch the connection first, exactly where get_ableton_connection()
        # used to: an unreachable Live must surface as this tool's error
        # string, not be absorbed into the handshake info (perform()
        # deliberately swallows send failures).
        d.client.ensure_connected()
        info = d.handshake.perform(d.client.send_command)
        cached = dict(d.handshake.info() or info)
        cached["expected_version"] = EXPECTED_REMOTE_SCRIPT_VERSION
        return json.dumps(cached, indent=2)
    except Exception as e:
        logger.error(f"Error getting remote script info: {str(e)}")
        return f"Error getting remote script info: {str(e)}"

@tool
def get_track_info(ctx: Context, track_index: int) -> str:
    """
    Get detailed information about a specific track in Ableton.

    Parameters:
    - track_index: The index of the track to get information about
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("get_track_info", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting track info from Ableton: {str(e)}")
        return f"Error getting track info: {str(e)}"


@tool
def get_clip_notes(
    ctx: Context,
    track_index: int,
    clip_index: int,
) -> str:
    """
    Read all MIDI notes from a Session-view clip.

    Returns pitch, start_time, duration, velocity, mute (and extended fields when available).

    Parameters:
    - track_index: Track that owns the clip
    - clip_index: Session clip slot index
    """
    try:
        d = _deps(ctx)
        try:
            d.handshake.require("get_clip_notes",
                                send_command=d.client.send_command)
        except CapabilityError as e:
            return str(e)
        result = d.client.send_command(
            "get_clip_notes",
            {"track_index": track_index, "clip_index": clip_index},
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting clip notes: {str(e)}")
        return f"Error getting clip notes: {str(e)}"


@tool
def get_session_snapshot(
    ctx: Context,
    include_notes: bool = True,
    include_params: bool = True,
) -> str:
    """
    Read the whole project state in one call.

    Includes session metadata, every track (mixer, devices, session clips,
    arrangement clips), optional MIDI notes, and optional device parameters.
    Cheaper than walking the session with get_track_info track by track when
    you need the full picture before planning an edit.

    Parameters:
    - include_notes: Include MIDI note arrays in clips (default True)
    - include_params: Include device parameter values (default True)
    """
    try:
        d = _deps(ctx)
        try:
            d.handshake.require("get_session_snapshot",
                                send_command=d.client.send_command)
        except CapabilityError as e:
            return str(e)
        result = d.client.send_command(
            "get_session_snapshot",
            {
                "include_notes": include_notes,
                "include_params": include_params,
            },
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting session snapshot: {str(e)}")
        return f"Error getting session snapshot: {str(e)}"


@tool
def create_midi_track(ctx: Context, index: int = -1) -> str:
    """
    Create a new MIDI track in the Ableton session.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("create_midi_track", {"index": index})
        return f"Created new MIDI track: {result.get('name', 'unknown')}"
    except Exception as e:
        logger.error(f"Error creating MIDI track: {str(e)}")
        return f"Error creating MIDI track: {str(e)}"


@tool
def create_audio_track(ctx: Context, index: int = -1) -> str:
    """
    Create a new audio track in the Ableton session.

    Use this for recorded or imported audio (samples, stems, vocals). For MIDI
    instruments use create_midi_track instead.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("create_audio_track", {"index": index})
        return f"Created new audio track: {result.get('name', 'unknown')}"
    except Exception as e:
        logger.error(f"Error creating audio track: {str(e)}")
        return f"Error creating audio track: {str(e)}"


@tool
def set_track_name(ctx: Context, track_index: int, name: str) -> str:
    """
    Set the name of a track.

    Parameters:
    - track_index: The index of the track to rename
    - name: The new name for the track
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("set_track_name", {"track_index": track_index, "name": name})
        return f"Renamed track to: {result.get('name', name)}"
    except Exception as e:
        logger.error(f"Error setting track name: {str(e)}")
        return f"Error setting track name: {str(e)}"

@tool
def create_clip(ctx: Context, track_index: int, clip_index: int, length: float = 4.0) -> str:
    """
    Create a new MIDI clip in the specified track and clip slot.

    Parameters:
    - track_index: The index of the track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - length: The length of the clip in beats (default: 4.0)
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("create_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "length": length
        })
        return f"Created new clip at track {track_index}, slot {clip_index} with length {length} beats"
    except Exception as e:
        logger.error(f"Error creating clip: {str(e)}")
        return f"Error creating clip: {str(e)}"

@tool
def set_clip_gain(ctx: Context, track_index: int, clip_index: int, gain: float,
                  arrangement: bool = True) -> str:
    """
    Set one audio clip's gain, leaving every other clip on the track untouched.

    This is the right fix for a single section performed too loud or too quiet.
    Lowering the track fader would bury the whole performance, and compressing
    harder squashes the dynamics everywhere; clip gain changes only that take.

    gain is Live's normalized 0.0-1.0 scale where 0.5 is roughly unity, NOT
    decibels. Call get_arrangement_clips first — it reports each clip's current
    gain and the dB Live displays for it.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: Index of the clip, ordered by start time as get_arrangement_clips
      returns them (or the clip slot index when arrangement is False)
    - gain: 0.0 to 1.0, where 0.5 is approximately unity
    - arrangement: True for a clip on the timeline (default), False for a Session slot
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("set_clip_gain", {
            "track_index": track_index,
            "clip_index": clip_index,
            "gain": gain,
            "arrangement": arrangement
        })
        return (f"Set '{result.get('clip_name')}' on '{result.get('track_name')}' "
                f"to {result.get('gain_display') or result.get('gain')}")
    except Exception as e:
        logger.error(f"Error setting clip gain: {str(e)}")
        return f"Error setting clip gain: {str(e)}"

@tool
def back_to_arrangement(ctx: Context) -> str:
    """
    Return every track to Arrangement playback — Live's "Back to Arrangement" button.

    Launching a Session clip overrides that track's timeline, and STOPPING the
    clip does not undo it: the track falls silent instead of reverting. Until
    this is called, arrangement edits on an overridden track are inaudible.
    """
    try:
        d = _deps(ctx)
        d.client.send_command("back_to_arrangement", {})
        return "All tracks returned to Arrangement playback"
    except Exception as e:
        logger.error(f"Error returning to arrangement: {str(e)}")
        return f"Error returning to arrangement: {str(e)}"

@tool
def get_track_routing(ctx: Context, track_index: int) -> str:
    """
    Show a track's input and output routing, plus every option available to it.

    Use this to find the exact name to pass to set_track_routing — for example
    the master is called "Main" in Live 12, not "Master", and a bus track's name
    only appears in the list once that track can accept input.

    Parameters:
    - track_index: The index of the track
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("get_track_routing", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting track routing: {str(e)}")
        return f"Error getting track routing: {str(e)}"

@tool
def set_track_routing(ctx: Context, track_index: int, target: str,
                      field: str = "output_routing_type") -> str:
    """
    Route a track's output (or input) somewhere else, by display name.

    This is how you build a submix bus without Live's grouping, which the API
    does not expose: point several tracks' outputs at one audio track and put
    the shared effects on it. Tracks you leave pointing at "Main" bypass it.

    Parameters:
    - track_index: The index of the track to re-route
    - target: The destination's display name exactly as get_track_routing lists
      it (e.g. "Main", or the name of a bus track)
    - field: Which routing to set — "output_routing_type" (default),
      "input_routing_type", "output_routing_channel", "input_routing_channel"
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("set_track_routing", {
            "track_index": track_index,
            "field": field,
            "target": target
        })
        return (f"Set '{result.get('track_name')}' {result.get('field')} "
                f"to {result.get('value')}")
    except Exception as e:
        logger.error(f"Error setting track routing: {str(e)}")
        return f"Error setting track routing: {str(e)}"

@tool
def set_count_in(ctx: Context, bars: int = 1, metronome: bool = True) -> str:
    """
    Set the record count-in, giving a performer a lead-in before punching in.

    This is the right way to get a count-in: it applies only when recording, so
    it needs no empty bar inserted at the front of the arrangement.

    Parameters:
    - bars: 0 = none, 1 = 1 bar, 2 = 2 bars, 3 = 4 bars (Live's own indices)
    - metronome: Turn the metronome on, so the count-in is audible
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("set_count_in", {
            "bars": bars,
            "metronome": metronome
        })
        return (f"Count-in set to {result.get('count_in')}; "
                f"metronome {'on' if result.get('metronome') else 'off'}")
    except Exception as e:
        logger.error(f"Error setting count-in: {str(e)}")
        return f"Error setting count-in: {str(e)}"

@tool
def set_track_send(ctx: Context, track_index: int, send_index: int, value: float) -> str:
    """
    Set how much of a track is sent to a return track.

    Live starts every send at -inf, so a newly created return track receives
    nothing until this is raised — a shared reverb bus is silent without it.
    The scale matches Live's send knob: 0.0 is -inf, around 0.6-0.7 is a modest
    send, 1.0 is unity.

    Parameters:
    - track_index: The index of the track sending
    - send_index: Which return to send to (0 = Return A, 1 = Return B, ...)
    - value: Send amount from 0.0 to 1.0
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("set_track_send", {
            "track_index": track_index,
            "send_index": send_index,
            "value": value
        })
        return (f"Set '{result.get('track_name')}' send {send_index} to "
                f"{result.get('display_value') or result.get('value')}")
    except Exception as e:
        logger.error(f"Error setting track send: {str(e)}")
        return f"Error setting track send: {str(e)}"

@tool
def save_set(ctx: Context) -> str:
    """
    Save the open Live Set, if this Live build exposes a save through its API.

    Live has never officially documented a save in the Python API, so this tries
    the known candidates and reports exactly which one worked — or reports that
    none exist rather than claiming a success that did not happen. Check the
    result before assuming the set is safe on disk.
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("save_set", {})
        if result.get("saved"):
            return f"Saved the Live Set via {result.get('method')}"
        return ("Could NOT save — this Live build exposes no callable save through the "
                f"Python API. Tried: {result.get('attempts')}. The set must be saved from Live's UI.")
    except Exception as e:
        logger.error(f"Error saving set: {str(e)}")
        return f"Error saving set: {str(e)}"

@tool
def create_return_track(ctx: Context) -> str:
    """
    Create a new return track — a shared effects bus that any track can send to.

    This is how you get one reverb shared across many tracks instead of a
    separate reverb on each, which is both cheaper and sounds more coherent.
    Address it afterwards by passing track_type="return" to the device and
    mixer tools.
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("create_return_track", {})
        return (f"Created return track '{result.get('name')}' at return index "
                f"{result.get('return_index')}")
    except Exception as e:
        logger.error(f"Error creating return track: {str(e)}")
        return f"Error creating return track: {str(e)}"

@tool
def set_track_arm(ctx: Context, track_index: int, armed: bool = True) -> str:
    """
    Arm or disarm a track for recording.

    Parameters:
    - track_index: The index of the track
    - armed: True to arm, False to disarm
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("set_track_arm", {
            "track_index": track_index,
            "value": armed
        })
        state = "armed" if result.get("arm") else "disarmed"
        return f"Track {track_index} ('{result.get('track_name')}') {state}"
    except Exception as e:
        logger.error(f"Error arming track: {str(e)}")
        return f"Error arming track: {str(e)}"

@tool
def set_track_monitoring(ctx: Context, track_index: int, state: str = "auto") -> str:
    """
    Set a track's input monitoring, so the performer can hear themselves.

    Parameters:
    - track_index: The index of the track
    - state: "in" (always monitor input), "auto" (monitor when armed), or "off"
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("set_track_monitoring", {
            "track_index": track_index,
            "value": state
        })
        return (f"Track {track_index} ('{result.get('track_name')}') monitoring set to "
                f"{result.get('monitoring')}")
    except Exception as e:
        logger.error(f"Error setting monitoring: {str(e)}")
        return f"Error setting monitoring: {str(e)}"

@tool
def get_device_parameters(ctx: Context, track_index: int, device_index: int,
                          track_type: str = "regular") -> str:
    """
    List every parameter on a device, with its current value, range and the
    value as Live displays it (e.g. "-6.0 dB", "35 %").

    Call this before set_device_parameter so you know the parameter names and
    what range each one accepts — a reverb's dry/wet, a delay's feedback, a
    compressor's threshold are all reachable this way.

    Parameters:
    - track_index: The index of the track containing the device
    - device_index: The index of the device in that track's chain (0 = first)
    """
    try:
        d = _deps(ctx)
        # 1.7.0 scripts advertise this capability but serve a broken handler
        # (duplicate class-body definitions), so gate on version, not name.
        try:
            d.handshake.require("get_device_parameters", min_version="1.8.0",
                                send_command=d.client.send_command)
        except CapabilityError as e:
            return str(e)
        result = d.client.send_command("get_device_parameters", {
            "track_index": track_index,
            "device_index": device_index,
            "track_type": track_type
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting device parameters: {str(e)}")
        return f"Error getting device parameters: {str(e)}"

@tool
def set_device_parameter(ctx: Context, track_index: int, device_index: int,
                         parameter: str, value: float,
                         track_type: str = "regular") -> str:
    """
    Set one parameter on a device. This is how you actually mix: pull a reverb's
    Dry/Wet down, set a delay's feedback, change a filter cutoff.

    The value is clamped into the parameter's own range rather than erroring, so
    passing 0 always means "as low as this goes".

    Parameters:
    - track_index: The index of the track containing the device
    - device_index: The index of the device in that track's chain (0 = first)
    - parameter: The parameter's name as shown by get_device_parameters (e.g.
      "Dry/Wet"), or its integer index passed as a string (e.g. "3")
    - value: The value to set, in the parameter's own units
    """
    try:
        d = _deps(ctx)
        # 1.7.0 scripts advertise this capability but serve a broken handler
        # (duplicate class-body definitions), so gate on version, not name.
        try:
            d.handshake.require("set_device_parameter", min_version="1.8.0",
                                send_command=d.client.send_command)
        except CapabilityError as e:
            return str(e)
        # Accept an integer index passed as a string without making the caller care.
        param: Any = parameter
        try:
            param = int(str(parameter).strip())
        except (TypeError, ValueError):
            pass
        result = d.client.send_command("set_device_parameter", {
            "track_index": track_index,
            "device_index": device_index,
            "parameter": param,
            "value": value,
            "track_type": track_type
        })
        note = " (clamped)" if result.get("clamped") else ""
        return (f"Set {result.get('device_name')} '{result.get('parameter_name')}' "
                f"to {result.get('display_value') or result.get('value')}{note}")
    except Exception as e:
        logger.error(f"Error setting device parameter: {str(e)}")
        return f"Error setting device parameter: {str(e)}"

@tool
def delete_device(ctx: Context, track_index: int, device_index: int,
                  track_type: str = "regular") -> str:
    """
    Remove a device from a track's chain.

    Deleting a device shifts the index of every device after it down by one, so
    when removing several, work from the highest index downwards.

    Parameters:
    - track_index: The index of the track containing the device
    - device_index: The index of the device to remove (0 = first in the chain)
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("delete_device", {
            "track_index": track_index,
            "device_index": device_index,
            "track_type": track_type
        })
        return (f"Deleted '{result.get('deleted_device_name')}' from track {track_index}; "
                f"{result.get('remaining_device_count')} devices remain")
    except Exception as e:
        logger.error(f"Error deleting device: {str(e)}")
        return f"Error deleting device: {str(e)}"

@tool
def set_track_volume(ctx: Context, track_index: int, value: float,
                     track_type: str = "regular") -> str:
    """
    Set a track's mixer volume.

    The scale is Live's own 0.0-1.0 fader position, NOT decibels: 0.85 is unity
    (0 dB), 0.0 is silence, 1.0 is +6 dB. The returned display_value gives the
    resulting level in dB so you can check it landed where you meant.

    Parameters:
    - track_index: The index of the track
    - value: Fader position from 0.0 to 1.0 (0.85 = 0 dB)
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("set_track_volume", {
            "track_index": track_index,
            "value": value,
            "track_type": track_type
        })
        return (f"Set '{result.get('track_name')}' volume to "
                f"{result.get('display_value') or result.get('value')}")
    except Exception as e:
        logger.error(f"Error setting track volume: {str(e)}")
        return f"Error setting track volume: {str(e)}"

@tool
def set_track_pan(ctx: Context, track_index: int, value: float,
                  track_type: str = "regular") -> str:
    """
    Set a track's stereo panning.

    Parameters:
    - track_index: The index of the track
    - value: -1.0 is hard left, 0.0 is centre, 1.0 is hard right
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("set_track_pan", {
            "track_index": track_index,
            "value": value,
            "track_type": track_type
        })
        return (f"Set '{result.get('track_name')}' pan to "
                f"{result.get('display_value') or result.get('value')}")
    except Exception as e:
        logger.error(f"Error setting track pan: {str(e)}")
        return f"Error setting track pan: {str(e)}"

@tool
def set_track_mute(ctx: Context, track_index: int, mute: bool) -> str:
    """
    Mute or unmute a track. Useful for auditioning parts in isolation.

    Parameters:
    - track_index: The index of the track
    - mute: True to mute, False to unmute
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("set_track_mute", {
            "track_index": track_index,
            "value": mute
        })
        state = "muted" if result.get("mute") else "unmuted"
        return f"Track {track_index} ('{result.get('track_name')}') {state}"
    except Exception as e:
        logger.error(f"Error setting track mute: {str(e)}")
        return f"Error setting track mute: {str(e)}"

@tool
def delete_track(ctx: Context, track_index: int) -> str:
    """
    Delete a track from the Ableton session, along with all clips on it.

    Note that deleting a track shifts the index of every track after it down by
    one. When deleting several tracks, work from the highest index downwards so
    the remaining indices stay valid.

    Parameters:
    - track_index: The index of the track to delete
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("delete_track", {
            "track_index": track_index
        })
        deleted_name = result.get("deleted_track_name", "")
        remaining = result.get("remaining_track_count", "unknown")
        return f"Deleted track {track_index} ('{deleted_name}'); {remaining} tracks remain"
    except Exception as e:
        logger.error(f"Error deleting track: {str(e)}")
        return f"Error deleting track: {str(e)}"

@tool
def create_audio_clip(ctx: Context, track_index: int, clip_index: int, path: str) -> str:
    """
    Create a new audio clip in an audio track's clip slot by importing a file.

    Requires Ableton Live 12.0.5 or newer — the underlying
    ClipSlot.create_audio_clip Live API was introduced in 12.0.5 and is not
    available in earlier 12.0.x releases.

    Parameters:
    - track_index: The index of the audio track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - path: Absolute path to a supported audio file (e.g. a .wav). The target
      track must be an audio track and the clip slot must be empty.
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("create_audio_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "path": path
        })
        return f"Created audio clip '{result.get('name', 'clip')}' at track {track_index}, slot {clip_index} (length {result.get('length', '?')} beats)"
    except Exception as e:
        logger.error(f"Error creating audio clip: {str(e)}")
        return f"Error creating audio clip: {str(e)}"

@tool
def add_notes_to_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    notes: List[Dict[str, Union[int, float, bool]]],
) -> str:
    """
    Add MIDI notes to a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - notes: List of note dictionaries, each with pitch, start_time, duration, velocity, and mute
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("add_notes_to_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "notes": notes
        })
        return f"Added {len(notes)} notes to clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error adding notes to clip: {str(e)}")
        return f"Error adding notes to clip: {str(e)}"

@tool
def clear_notes_from_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
) -> str:
    """
    Remove all MIDI notes from a Session clip.

    Writes are additive (add_notes_to_clip only appends), so to truly *modify*
    a clip you clear it first, then add the new notes. Use this with
    get_clip_notes and add_notes_to_clip for a real read -> modify -> write
    loop: read the notes, edit the list, clear_notes_from_clip, then
    add_notes_to_clip the edited notes.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    """
    try:
        d = _deps(ctx)
        try:
            d.handshake.require("clear_notes_from_clip",
                                send_command=d.client.send_command)
        except CapabilityError as e:
            return str(e)
        result = d.client.send_command("clear_notes_from_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return "Cleared {n} note(s) from clip '{name}' (track {t}, slot {c})".format(
            n=result.get("cleared_count", "?"),
            name=result.get("clip_name", "clip"),
            t=track_index,
            c=clip_index,
        )
    except Exception as e:
        logger.error(f"Error clearing notes from clip: {str(e)}")
        return f"Error clearing notes from clip: {str(e)}"

@tool
def set_clip_name(ctx: Context, track_index: int, clip_index: int, name: str) -> str:
    """
    Set the name of a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - name: The new name for the clip
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("set_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })
        return f"Renamed clip at track {track_index}, slot {clip_index} to '{name}'"
    except Exception as e:
        logger.error(f"Error setting clip name: {str(e)}")
        return f"Error setting clip name: {str(e)}"

@tool
def set_arrangement_clip_name(ctx: Context, track_index: int, clip_index: int, name: str) -> str:
    """
    Set the name of a clip placed in the Arrangement timeline.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip within track.arrangement_clips, in the
      same order returned by get_arrangement_clips (i.e. ordered by start_time)
    - name: The new name for the clip
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("set_arrangement_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })
        return f"Renamed arrangement clip at track {track_index}, index {clip_index} to '{name}'"
    except Exception as e:
        logger.error(f"Error setting arrangement clip name: {str(e)}")
        return f"Error setting arrangement clip name: {str(e)}"

@tool
def set_tempo(ctx: Context, tempo: float) -> str:
    """
    Set the tempo of the Ableton session.

    Parameters:
    - tempo: The new tempo in BPM
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("set_tempo", {"tempo": tempo})
        return f"Set tempo to {tempo} BPM"
    except Exception as e:
        logger.error(f"Error setting tempo: {str(e)}")
        return f"Error setting tempo: {str(e)}"


@tool
def load_instrument_or_effect(ctx: Context, track_index: int, uri: str,
                              track_type: str = "regular") -> str:
    """
    Load an instrument or effect onto a track using its URI.

    Parameters:
    - track_index: The index of the track to load the instrument on
    - uri: The URI of the instrument or effect to load (e.g., 'query:Synths#Instrument%20Rack:Bass:FileId_5116')
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": uri,
            "track_type": track_type
        })

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
    except Exception as e:
        logger.error(f"Error loading instrument by URI: {str(e)}")
        return f"Error loading instrument by URI: {str(e)}"

@tool
def fire_clip(ctx: Context, track_index: int, clip_index: int) -> str:
    """
    Start playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("fire_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Started playing clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error firing clip: {str(e)}")
        return f"Error firing clip: {str(e)}"

@tool
def stop_clip(ctx: Context, track_index: int, clip_index: int) -> str:
    """
    Stop playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("stop_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Stopped clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error stopping clip: {str(e)}")
        return f"Error stopping clip: {str(e)}"


@tool
def delete_clip(ctx: Context, track_index: int, clip_index: int) -> str:
    """
    Delete the clip in the given clip slot, freeing it for reuse.

    Use this before create_clip when you want to overwrite an existing clip
    (create_clip itself refuses to write into an occupied slot).

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot to clear
    """
    try:
        d = _deps(ctx)
        try:
            d.handshake.require("delete_clip",
                                send_command=d.client.send_command)
        except CapabilityError as e:
            return str(e)
        result = d.client.send_command("delete_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error deleting clip: {str(e)}")
        return f"Error deleting clip: {str(e)}"


@tool
def start_playback(ctx: Context) -> str:
    """Start playing the Ableton session.
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("start_playback")
        return "Started playback"
    except Exception as e:
        logger.error(f"Error starting playback: {str(e)}")
        return f"Error starting playback: {str(e)}"

@tool
def stop_playback(ctx: Context) -> str:
    """Stop playing the Ableton session.
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("stop_playback")
        return "Stopped playback"
    except Exception as e:
        logger.error(f"Error stopping playback: {str(e)}")
        return f"Error stopping playback: {str(e)}"

@tool
def get_browser_tree(ctx: Context, category_type: str = "all") -> str:
    """
    Get a hierarchical tree of browser categories from Ableton.

    Parameters:
    - category_type: Type of categories to get ('all', 'instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects')
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("get_browser_tree", {
            "category_type": category_type
        })

        # Check if we got any categories
        if "available_categories" in result and len(result.get("categories", [])) == 0:
            available_cats = result.get("available_categories", [])
            return (f"No categories found for '{category_type}'. "
                   f"Available browser categories: {', '.join(available_cats)}")

        # Format the tree in a more readable way
        total_folders = result.get("total_folders", 0)
        formatted_output = f"Browser tree for '{category_type}' (showing {total_folders} folders):\n\n"

        def format_tree(item, indent=0):
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

        # Format each category
        for category in result.get("categories", []):
            formatted_output += format_tree(category)
            formatted_output += "\n"

        return formatted_output
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        else:
            logger.error(f"Error getting browser tree: {error_msg}")
            return f"Error getting browser tree: {error_msg}"

@tool
def get_browser_items_at_path(ctx: Context, path: str) -> str:
    """
    Get browser items at a specific path in Ableton's browser.

    Parameters:
    - path: Path in the format "category/folder/subfolder"
            where category is one of the available browser categories in Ableton
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("get_browser_items_at_path", {
            "path": path
        })

        # Check if there was an error with available categories
        if "error" in result and "available_categories" in result:
            error = result.get("error", "")
            available_cats = result.get("available_categories", [])
            return (f"Error: {error}\n"
                   f"Available browser categories: {', '.join(available_cats)}")

        return json.dumps(result, indent=2)
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        elif "Unknown or unavailable category" in error_msg:
            logger.error(f"Invalid browser category: {error_msg}")
            return f"Error: {error_msg}. Please check the available categories using get_browser_tree."
        elif "Path part" in error_msg and "not found" in error_msg:
            logger.error(f"Path not found: {error_msg}")
            return f"Error: {error_msg}. Please check the path and try again."
        else:
            logger.error(f"Error getting browser items at path: {error_msg}")
            return f"Error getting browser items at path: {error_msg}"

@tool
def load_drum_kit(ctx: Context, track_index: int, rack_uri: str, kit_path: str) -> str:
    """
    Load a drum rack and then load a specific drum kit into it.

    Parameters:
    - track_index: The index of the track to load on
    - rack_uri: The URI of the drum rack to load (e.g., 'Drums/Drum Rack')
    - kit_path: Path to the drum kit inside the browser (e.g., 'drums/acoustic/kit1')
    """
    try:
        d = _deps(ctx)

        # Step 1: Load the drum rack
        result = d.client.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": rack_uri
        })

        if not result.get("loaded", False):
            return f"Failed to load drum rack with URI '{rack_uri}'"

        # Step 2: Get the drum kit items at the specified path
        kit_result = d.client.send_command("get_browser_items_at_path", {
            "path": kit_path
        })

        if "error" in kit_result:
            return f"Loaded drum rack but failed to find drum kit: {kit_result.get('error')}"

        # Step 3: Find a loadable drum kit
        kit_items = kit_result.get("items", [])
        loadable_kits = [item for item in kit_items if item.get("is_loadable", False)]

        if not loadable_kits:
            return f"Loaded drum rack but no loadable drum kits found at '{kit_path}'"

        # Step 4: Load the first loadable kit
        kit_uri = loadable_kits[0].get("uri")
        load_result = d.client.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": kit_uri
        })

        return f"Loaded drum rack and kit '{loadable_kits[0].get('name')}' on track {track_index}"
    except Exception as e:
        logger.error(f"Error loading drum kit: {str(e)}")
        return f"Error loading drum kit: {str(e)}"

# ── Arrangement view tools ────────────────────────────────────────────────────

@tool
def switch_to_arrangement_view(ctx: Context) -> str:
    """Switch Ableton's main window to the Arrangement view.
    """
    try:
        d = _deps(ctx)
        d.client.send_command("switch_to_arrangement_view")
        return "Switched to Arrangement view"
    except Exception as e:
        logger.error(f"Error switching to arrangement view: {str(e)}")
        return f"Error switching to arrangement view: {str(e)}"


@tool
def set_arrangement_time(ctx: Context, time: float) -> str:
    """
    Move the arrangement playhead to a specific position.

    Parameters:
    - time: Position in beats from the start of the arrangement (e.g. 8.0 = bar 3 in 4/4)
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("set_current_song_time", {"time": time})
        return f"Playhead moved to beat {result.get('current_song_time', time)}"
    except Exception as e:
        logger.error(f"Error setting arrangement time: {str(e)}")
        return f"Error setting arrangement time: {str(e)}"


@tool
def get_arrangement_clips(ctx: Context, track_index: int) -> str:
    """
    List all clips placed in the Arrangement timeline for a track.

    Returns each clip's name, start_time, end_time, length, and type.

    Parameters:
    - track_index: The index of the track to inspect
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command("get_arrangement_clips", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting arrangement clips: {str(e)}")
        return f"Error getting arrangement clips: {str(e)}"


@tool
def duplicate_to_arrangement(
    ctx: Context,
    track_index: int,
    clip_index: int,
    destination_time: float,
) -> str:
    """
    Copy a Session-view clip into the Arrangement timeline.

    Uses Live's track.duplicate_clip_to_arrangement() API (Live 11 / 12).
    The clip is placed at destination_time beats from the start of the
    arrangement on the same track it lives in.

    Typical workflow:
      1. create_clip / add_notes_to_clip to build a Session clip
      2. Call duplicate_to_arrangement once per bar/section you need
      3. Call switch_to_arrangement_view to confirm the result in Live

    Parameters:
    - track_index:       Index of the track that owns the Session clip
    - clip_index:        Index of the clip slot in that track (Session view)
    - destination_time:  Beat position in the arrangement to place the clip
                         (e.g. 0.0 = start, 8.0 = bar 3 in 4/4)
    """
    try:
        d = _deps(ctx)
        result = d.client.send_command(
            "duplicate_session_clip_to_arrangement",
            {
                "track_index": track_index,
                "clip_index": clip_index,
                "destination_time": destination_time
            }
        )
        clip_name = result.get("clip_name", "clip")
        track_name = result.get("track_name", f"track {track_index}")
        return (
            f"Duplicated '{clip_name}' from Session slot {clip_index} "
            f"on '{track_name}' to arrangement at beat {destination_time}"
        )
    except Exception as e:
        logger.error(f"Error duplicating clip to arrangement: {str(e)}")
        return f"Error duplicating clip to arrangement: {str(e)}"


@tool
def create_locator(
    ctx: Context,
    name: str,
    time: float,
) -> str:
    """
    Create a named locator (cue point) in the Arrangement at a beat position.

    If a locator already exists at that beat (within ~1e-3 tolerance) it is
    renamed instead of toggled off. Time is in beats from the start of the
    arrangement (e.g. 0.0 = start, 16.0 = bar 5 in 4/4).

    Parameters:
    - name: The locator label (e.g. "Chorus", "Verse 1", "Drop")
    - time: Beat position where the locator should sit
    """
    try:
        d = _deps(ctx)
        try:
            d.handshake.require("create_locator",
                                send_command=d.client.send_command)
        except CapabilityError as e:
            return str(e)
        result = d.client.send_command(
            "create_locator",
            {"name": name, "time": time}
        )
        return (
            f"Locator '{result.get('name', name)}' set at beat "
            f"{result.get('time', time)}"
        )
    except Exception as e:
        logger.error(f"Error creating locator: {str(e)}")
        return f"Error creating locator: {str(e)}"
