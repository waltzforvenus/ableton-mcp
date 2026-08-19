"""Controllers: the MCP tool functions (docs/REFACTOR_PLAN.md §3.2, §3.3).

Each tool does exactly three things: coerce arguments that belong at the
boundary, delegate to the Model (``AbletonService``, reached through
``_deps(ctx)`` — the one FastMCP-specific hop to the ``Deps`` the composition
root's lifespan yielded), and hand the outcome to its View renderer in
presenters.py. The shared ``tool`` decorator supplies the mechanics every
body used to hand-roll: catch, log, and let the View word the failure.

Registration stays decoupled from definition: the decorator only appends to
``TOOLS``; ``app.build_app`` feeds that list to FastMCP. No env reads, no
logging configuration, no sockets — importing this module has zero side
effects, and nothing here ever touches the wire directly (that is the
service layer's job; a guardrail test asserts it).
"""
from mcp.server.fastmcp import Context
import functools
import logging
from typing import TYPE_CHECKING, Dict, Any, List, Union

from . import presenters
from .handshake import CapabilityError

if TYPE_CHECKING:  # annotation-only: app.py imports this module at runtime
    from .app import Deps

logger = logging.getLogger("AbletonMCPServer")

# Every function the app registers as an MCP tool, in definition order.
TOOLS: list = []


def tool(fn):
    """Mark ``fn`` as an MCP tool and wrap it in the shared mechanics.

    Registration: the wrapper is appended to ``TOOLS`` for ``app.build_app``
    to hand to ``FastMCP.add_tool``. ``functools.wraps`` carries the name and
    docstring, and — via ``__wrapped__``, which both ``inspect.signature``
    and FastMCP's schema builder follow — the original signature, so the
    model-facing interface is byte-identical to the unwrapped function's.

    Error handling, formerly hand-rolled in every body:

    - ``CapabilityError`` (raised by the registry-driven gate in
      ``AbletonService._send``): its ``str()`` IS the friendly "re-run
      ``ableton-mcp-install-script``" message — return it verbatim.
    - any other ``Exception``: log, then return the View's wording for this
      tool's failure (``presenters.error_text``, which routes the two
      bespoke browser tools through ``ERROR_RENDERERS``).
    """
    @functools.wraps(fn)
    def wrapper(ctx: Context, *args: Any, **kwargs: Any) -> str:
        try:
            return fn(ctx, *args, **kwargs)
        except CapabilityError as e:
            return str(e)
        except Exception as e:
            text = presenters.error_text(fn.__name__, e)
            logger.error(text)
            return text

    TOOLS.append(wrapper)
    return wrapper


def _deps(ctx: Context) -> "Deps":
    """The Deps the composition root's lifespan yielded for this session."""
    return ctx.request_context.lifespan_context


# Core Tool endpoints

@tool
def get_session_info(ctx: Context) -> str:
    """Get detailed information about the current Ableton session
    """
    result = _deps(ctx).service.get_session_info()
    return presenters.get_session_info(result)


@tool
def get_remote_script_info(ctx: Context) -> str:
    """
    Report Ableton Remote Script version and capabilities (handshake).

    Use this to verify the Live-side bridge matches this MCP server package.
    """
    result = _deps(ctx).service.get_remote_script_info()
    return presenters.get_remote_script_info(result)


@tool
def get_track_info(ctx: Context, track_index: int) -> str:
    """
    Get detailed information about a specific track in Ableton.

    Parameters:
    - track_index: The index of the track to get information about
    """
    result = _deps(ctx).service.get_track_info(track_index)
    return presenters.get_track_info(result)


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
    result = _deps(ctx).service.get_clip_notes(track_index, clip_index)
    return presenters.get_clip_notes(result)


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
    result = _deps(ctx).service.get_session_snapshot(include_notes, include_params)
    return presenters.get_session_snapshot(result)


@tool
def create_midi_track(ctx: Context, index: int = -1) -> str:
    """
    Create a new MIDI track in the Ableton session.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    """
    result = _deps(ctx).service.create_midi_track(index)
    return presenters.create_midi_track(result)


@tool
def create_audio_track(ctx: Context, index: int = -1) -> str:
    """
    Create a new audio track in the Ableton session.

    Use this for recorded or imported audio (samples, stems, vocals). For MIDI
    instruments use create_midi_track instead.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    """
    result = _deps(ctx).service.create_audio_track(index)
    return presenters.create_audio_track(result)


@tool
def set_track_name(ctx: Context, track_index: int, name: str) -> str:
    """
    Set the name of a track.

    Parameters:
    - track_index: The index of the track to rename
    - name: The new name for the track
    """
    result = _deps(ctx).service.set_track_name(track_index, name)
    return presenters.set_track_name(result, name)


@tool
def create_clip(ctx: Context, track_index: int, clip_index: int, length: float = 4.0) -> str:
    """
    Create a new MIDI clip in the specified track and clip slot.

    Parameters:
    - track_index: The index of the track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - length: The length of the clip in beats (default: 4.0)
    """
    _deps(ctx).service.create_clip(track_index, clip_index, length)
    return presenters.create_clip(track_index, clip_index, length)


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
    result = _deps(ctx).service.set_clip_gain(track_index, clip_index, gain, arrangement)
    return presenters.set_clip_gain(result)


@tool
def back_to_arrangement(ctx: Context) -> str:
    """
    Return every track to Arrangement playback — Live's "Back to Arrangement" button.

    Launching a Session clip overrides that track's timeline, and STOPPING the
    clip does not undo it: the track falls silent instead of reverting. Until
    this is called, arrangement edits on an overridden track are inaudible.
    """
    _deps(ctx).service.back_to_arrangement()
    return presenters.back_to_arrangement()


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
    result = _deps(ctx).service.get_track_routing(track_index)
    return presenters.get_track_routing(result)


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
    result = _deps(ctx).service.set_track_routing(track_index, target, field)
    return presenters.set_track_routing(result)


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
    result = _deps(ctx).service.set_count_in(bars, metronome)
    return presenters.set_count_in(result)


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
    result = _deps(ctx).service.set_track_send(track_index, send_index, value)
    return presenters.set_track_send(result, send_index)


@tool
def save_set(ctx: Context) -> str:
    """
    Save the open Live Set, if this Live build exposes a save through its API.

    Live has never officially documented a save in the Python API, so this tries
    the known candidates and reports exactly which one worked — or reports that
    none exist rather than claiming a success that did not happen. Check the
    result before assuming the set is safe on disk.
    """
    result = _deps(ctx).service.save_set()
    return presenters.save_set(result)


@tool
def create_return_track(ctx: Context) -> str:
    """
    Create a new return track — a shared effects bus that any track can send to.

    This is how you get one reverb shared across many tracks instead of a
    separate reverb on each, which is both cheaper and sounds more coherent.
    Address it afterwards by passing track_type="return" to the device and
    mixer tools.
    """
    result = _deps(ctx).service.create_return_track()
    return presenters.create_return_track(result)


@tool
def set_track_arm(ctx: Context, track_index: int, armed: bool = True) -> str:
    """
    Arm or disarm a track for recording.

    Parameters:
    - track_index: The index of the track
    - armed: True to arm, False to disarm
    """
    result = _deps(ctx).service.set_track_arm(track_index, armed)
    return presenters.set_track_arm(result, track_index)


@tool
def set_track_monitoring(ctx: Context, track_index: int, state: str = "auto") -> str:
    """
    Set a track's input monitoring, so the performer can hear themselves.

    Parameters:
    - track_index: The index of the track
    - state: "in" (always monitor input), "auto" (monitor when armed), or "off"
    """
    result = _deps(ctx).service.set_track_monitoring(track_index, state)
    return presenters.set_track_monitoring(result, track_index)


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
    result = _deps(ctx).service.get_device_parameters(track_index, device_index, track_type)
    return presenters.get_device_parameters(result)


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
    # Accept an integer index passed as a string without making the caller care.
    param: Any = parameter
    try:
        param = int(str(parameter).strip())
    except (TypeError, ValueError):
        pass
    result = _deps(ctx).service.set_device_parameter(
        track_index, device_index, param, value, track_type)
    return presenters.set_device_parameter(result)


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
    result = _deps(ctx).service.delete_device(track_index, device_index, track_type)
    return presenters.delete_device(result, track_index)


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
    result = _deps(ctx).service.set_track_volume(track_index, value, track_type)
    return presenters.set_track_volume(result)


@tool
def set_track_pan(ctx: Context, track_index: int, value: float,
                  track_type: str = "regular") -> str:
    """
    Set a track's stereo panning.

    Parameters:
    - track_index: The index of the track
    - value: -1.0 is hard left, 0.0 is centre, 1.0 is hard right
    """
    result = _deps(ctx).service.set_track_pan(track_index, value, track_type)
    return presenters.set_track_pan(result)


@tool
def set_track_mute(ctx: Context, track_index: int, mute: bool) -> str:
    """
    Mute or unmute a track. Useful for auditioning parts in isolation.

    Parameters:
    - track_index: The index of the track
    - mute: True to mute, False to unmute
    """
    result = _deps(ctx).service.set_track_mute(track_index, mute)
    return presenters.set_track_mute(result, track_index)


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
    result = _deps(ctx).service.delete_track(track_index)
    return presenters.delete_track(result, track_index)


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
    result = _deps(ctx).service.create_audio_clip(track_index, clip_index, path)
    return presenters.create_audio_clip(result, track_index, clip_index)


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
    _deps(ctx).service.add_notes_to_clip(track_index, clip_index, notes)
    return presenters.add_notes_to_clip(track_index, clip_index, notes)


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
    result = _deps(ctx).service.clear_notes_from_clip(track_index, clip_index)
    return presenters.clear_notes_from_clip(result, track_index, clip_index)


@tool
def set_clip_name(ctx: Context, track_index: int, clip_index: int, name: str) -> str:
    """
    Set the name of a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - name: The new name for the clip
    """
    _deps(ctx).service.set_clip_name(track_index, clip_index, name)
    return presenters.set_clip_name(track_index, clip_index, name)


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
    _deps(ctx).service.set_arrangement_clip_name(track_index, clip_index, name)
    return presenters.set_arrangement_clip_name(track_index, clip_index, name)


@tool
def set_tempo(ctx: Context, tempo: float) -> str:
    """
    Set the tempo of the Ableton session.

    Parameters:
    - tempo: The new tempo in BPM
    """
    _deps(ctx).service.set_tempo(tempo)
    return presenters.set_tempo(tempo)


@tool
def load_instrument_or_effect(ctx: Context, track_index: int, uri: str,
                              track_type: str = "regular") -> str:
    """
    Load an instrument or effect onto a track using its URI.

    Parameters:
    - track_index: The index of the track to load the instrument on
    - uri: The URI of the instrument or effect to load (e.g., 'query:Synths#Instrument%20Rack:Bass:FileId_5116')
    """
    result = _deps(ctx).service.load_browser_item(track_index, uri, track_type)
    return presenters.load_instrument_or_effect(result, track_index, uri)


@tool
def fire_clip(ctx: Context, track_index: int, clip_index: int) -> str:
    """
    Start playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    """
    _deps(ctx).service.fire_clip(track_index, clip_index)
    return presenters.fire_clip(track_index, clip_index)


@tool
def stop_clip(ctx: Context, track_index: int, clip_index: int) -> str:
    """
    Stop playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    """
    _deps(ctx).service.stop_clip(track_index, clip_index)
    return presenters.stop_clip(track_index, clip_index)


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
    result = _deps(ctx).service.delete_clip(track_index, clip_index)
    return presenters.delete_clip(result)


@tool
def start_playback(ctx: Context) -> str:
    """Start playing the Ableton session.
    """
    _deps(ctx).service.start_playback()
    return presenters.start_playback()


@tool
def stop_playback(ctx: Context) -> str:
    """Stop playing the Ableton session.
    """
    _deps(ctx).service.stop_playback()
    return presenters.stop_playback()


@tool
def get_browser_tree(ctx: Context, category_type: str = "all") -> str:
    """
    Get a hierarchical tree of browser categories from Ableton.

    Parameters:
    - category_type: Type of categories to get ('all', 'instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects')
    """
    result = _deps(ctx).service.get_browser_tree(category_type)
    return presenters.get_browser_tree(result, category_type)


@tool
def get_browser_items_at_path(ctx: Context, path: str) -> str:
    """
    Get browser items at a specific path in Ableton's browser.

    Parameters:
    - path: Path in the format "category/folder/subfolder"
            where category is one of the available browser categories in Ableton
    """
    result = _deps(ctx).service.get_browser_items_at_path(path)
    return presenters.get_browser_items_at_path(result)


@tool
def load_drum_kit(ctx: Context, track_index: int, rack_uri: str, kit_path: str) -> str:
    """
    Load a drum rack and then load a specific drum kit into it.

    Parameters:
    - track_index: The index of the track to load on
    - rack_uri: The URI of the drum rack to load (e.g., 'Drums/Drum Rack')
    - kit_path: Path to the drum kit inside the browser (e.g., 'drums/acoustic/kit1')
    """
    result = _deps(ctx).service.load_drum_kit(track_index, rack_uri, kit_path)
    return presenters.load_drum_kit(result)


# ── Arrangement view tools ────────────────────────────────────────────────────

@tool
def switch_to_arrangement_view(ctx: Context) -> str:
    """Switch Ableton's main window to the Arrangement view.
    """
    _deps(ctx).service.switch_to_arrangement_view()
    return presenters.switch_to_arrangement_view()


@tool
def set_arrangement_time(ctx: Context, time: float) -> str:
    """
    Move the arrangement playhead to a specific position.

    Parameters:
    - time: Position in beats from the start of the arrangement (e.g. 8.0 = bar 3 in 4/4)
    """
    result = _deps(ctx).service.set_current_song_time(time)
    return presenters.set_arrangement_time(result, time)


@tool
def get_arrangement_clips(ctx: Context, track_index: int) -> str:
    """
    List all clips placed in the Arrangement timeline for a track.

    Returns each clip's name, start_time, end_time, length, and type.

    Parameters:
    - track_index: The index of the track to inspect
    """
    result = _deps(ctx).service.get_arrangement_clips(track_index)
    return presenters.get_arrangement_clips(result)


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
    result = _deps(ctx).service.duplicate_session_clip_to_arrangement(
        track_index, clip_index, destination_time)
    return presenters.duplicate_to_arrangement(
        result, track_index, clip_index, destination_time)


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
    result = _deps(ctx).service.create_locator(name, time)
    return presenters.create_locator(result, name, time)
