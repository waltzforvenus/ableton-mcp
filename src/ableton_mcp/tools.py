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
from typing import TYPE_CHECKING, Dict, Any, List, Optional, Union

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


def _one_value(canonical: str, primary: Any, **aliases: Any) -> Any:
    """Fold a parameter's accepted spellings down to one value.

    Three arguments on this surface get guessed by the name a neighbouring
    tool uses: ``duplicate_to_arrangement``'s ``destination_time`` was
    written as ``arrangement_time`` seven times across four days of one
    production session, and every one of those was a round-trip that did no
    work at all. Accepting both spellings costs a keyword argument.

    What it must not do is guess between two DIFFERENT values — that would
    silently discard half of what the caller said, on tools that place
    audio on a timeline. Disagreement is refused, with both spellings named
    (presenters owns the wording), before anything reaches the wire.

    Deliberately NOT a pydantic alias: the ``tool`` decorator hands FastMCP
    an argument model built without ``populate_by_name``, so an alias there
    would REPLACE the canonical spelling instead of adding to it — turning
    a fix for a guessed name into a broken canonical one.
    """
    value, chosen = primary, canonical
    for name, alternative in aliases.items():
        if alternative is None:
            continue
        if value is None:
            value, chosen = alternative, name
        elif alternative != value:
            raise ValueError(
                presenters.alias_conflict(chosen, value, name, alternative))
    return value


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
    arrangement: bool = False,
) -> str:
    """
    Read all MIDI notes from a clip, in either view.

    Returns pitch, start_time, duration, velocity, mute (and extended fields when available).

    Note times are the clip's own beats in both views — an arrangement clip
    sitting at bar 60 still reports its first note at 0.0 — so what comes
    back can be edited and handed straight to add_notes_to_clip. Read the
    arrangement clip itself (arrangement=True) when you are about to edit
    one bar in place; read the Session clip when it is the pattern you are
    about to re-stamp.

    Parameters:
    - track_index: Track that owns the clip
    - clip_index: Session clip slot index, or — with arrangement=True — the
      index into the track's Arrangement clips in start-time order, as
      get_arrangement_clips returns them
    - arrangement: True to read a clip on the timeline, False (default) for
      a Session slot
    """
    result = _deps(ctx).service.get_clip_notes(track_index, clip_index,
                                               arrangement)
    return presenters.get_clip_notes(result)


@tool
def get_session_snapshot(
    ctx: Context,
    include_notes: bool = True,
    include_params: bool = True,
    include_warp_markers: bool = False,
    include_rack_chains: bool = False,
    include_empty_slots: bool = False,
    tracks: Optional[Union[List[Union[int, str]], int, str]] = None,
) -> str:
    """
    Read project state in one call: session metadata, tracks (mixer, devices,
    session clips, arrangement clips), returns, master, scenes and locators.

    SCOPE IT. A whole real arrangement does not fit in one response — in one
    production session 21 of 24 unscoped calls blew the output cap even with
    notes and parameters both off, and every one of them actually wanted a
    handful of tracks. Pass `tracks` with the names or indices you care
    about ("DRUMS", ["GUITARS", 3]) and the answer stays readable. Every
    track keeps its real session index, so an index read out of a filtered
    snapshot still addresses the same track everywhere else.

    Prefer this over walking the set with get_track_info when you need the
    picture before planning an edit; prefer get_track_info or
    get_arrangement_clips when you already know the one track you mean.

    The three include_* flags below default OFF because they are almost all
    of the bytes (warp markers alone were 73-81% of an early snapshot, and a
    drum rack's chains ~9.5 KB). Counts are always present —
    warp_marker_count, chain_count, clip_slot_count — so nothing omitted is
    invisible.

    Parameters:
    - include_notes: MIDI note arrays inside clips (default True)
    - include_params: every device's parameter values (default True)
    - include_warp_markers: every clip's warp-marker pairs (default False)
    - include_rack_chains: a rack's nested chains and their devices (default False)
    - include_empty_slots: clip slots with nothing in them (default False)
    - tracks: restrict to these song tracks — indices, names, or a mix; one
      selector or a list. Names match case-insensitively. A selector that
      matches nothing is refused rather than quietly dropped
    """
    # A bare selector is the common case ("just the DRUMS bus"), so accept
    # one without a list around it and normalise here — the wire form is a
    # list, and boundary coercion is the controller's job.
    selection: Optional[List[Union[int, str]]]
    if tracks is None or isinstance(tracks, list):
        selection = tracks
    else:
        selection = [tracks]
    result = _deps(ctx).service.get_session_snapshot(
        include_notes, include_params, include_warp_markers,
        include_rack_chains, include_empty_slots, selection)
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
def duplicate_track(ctx: Context, track_index: int,
                    expect_track_name: Optional[str] = None) -> str:
    """
    Duplicate a whole track — devices, mixer, routing and every clip.

    Live's Ctrl-D on a track header. Prefer this over create_midi_track /
    create_audio_track whenever the new track should start out like an
    existing one: double-tracking a part used to mean creating a track,
    re-routing it to the same bus, re-panning it and reloading each device
    by hand, and this is one call instead. Follow with set_track_name (Live
    names the copy itself, usually the source name plus a number).

    The copy lands immediately BELOW the source, so the renumbering is
    knowable in advance instead of needing a re-read: the copy is
    track_index + 1, and every track that was at or below that index has
    moved down by one.

    Live's Intro and Lite editions cap the track count, and the LOM exposes
    no way to read that cap beforehand — a refusal comes back as itself,
    saying nothing was created. Read the reply rather than assuming.

    Parameters:
    - track_index: The track to duplicate
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    result = _deps(ctx).service.duplicate_track(track_index, expect_track_name)
    return presenters.duplicate_track(result)


@tool
def set_track_name(ctx: Context, track_index: int, name: str,
                   expect_track_name: Optional[str] = None) -> str:
    """
    Set the name of a track.

    Parameters:
    - track_index: The index of the track to rename
    - name: The new name for the track
    - expect_track_name: Optional safety net — refuse unless the track at
      track_index currently carries this name (its name BEFORE the rename)
    """
    result = _deps(ctx).service.set_track_name(track_index, name,
                                               expect_track_name)
    return presenters.set_track_name(result, name)


@tool
def create_clip(ctx: Context, track_index: int, clip_index: int, length: float = 4.0,
                expect_track_name: Optional[str] = None) -> str:
    """
    Create a new MIDI clip in the specified track and clip slot.

    Parameters:
    - track_index: The index of the track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - length: The length of the clip in beats (default: 4.0)
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    _deps(ctx).service.create_clip(track_index, clip_index, length,
                                   expect_track_name)
    return presenters.create_clip(track_index, clip_index, length)


@tool
def set_clip_gain(ctx: Context, track_index: int, clip_index: int, gain: float,
                  arrangement: bool = True,
                  expect_track_name: Optional[str] = None) -> str:
    """
    Set one audio clip's gain, leaving every other clip on the track untouched.

    This is the right fix for a single section performed too loud or too quiet.
    Lowering the track fader would bury the whole performance, and compressing
    harder squashes the dynamics everywhere; clip gain changes only that take.

    gain is Live's normalized 0.0-1.0 scale where 0.4 is unity (0.0 dB), NOT
    decibels. Call get_arrangement_clips first — it reports each clip's current
    gain and the dB Live displays for it.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: Index of the clip, ordered by start time as get_arrangement_clips
      returns them (or the clip slot index when arrangement is False)
    - gain: 0.0 to 1.0, where 0.4 is unity (0.0 dB)
    - arrangement: True for a clip on the timeline (default), False for a Session slot
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    result = _deps(ctx).service.set_clip_gain(track_index, clip_index, gain,
                                              arrangement, expect_track_name)
    return presenters.set_clip_gain(result)


@tool
def set_clip_warp(ctx: Context, track_index: int, clip_index: int, warping: bool,
                  warp_mode: Optional[int] = None, arrangement: bool = True,
                  expect_track_name: Optional[str] = None) -> str:
    """
    Turn an audio clip's warping on or off — the fix for stems that drift apart.

    Live's "Auto-Warp Long Samples" guesses a source tempo for every imported
    file, and on material without clear transients it guesses wrong. Stems
    captured together in one session can each land on a different guess, so a
    multitrack that was perfectly aligned on disk plays out of sync. Switching
    warping off makes a clip play at its recorded rate, which is what keeps
    aligned stems aligned; the response reports the clip's resulting length so
    you can confirm a set of them now agree.

    Leave warping on for material you actually want to follow the project
    tempo. warp_mode is only applied while warping is on.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: Index of the clip, ordered by start time as get_arrangement_clips
      returns them (or the clip slot index when arrangement is False)
    - warping: True to warp to project tempo, False to play at the native rate
    - warp_mode: Optional Live warp-mode index (0 Beats, 1 Tones, 2 Texture,
      3 Re-Pitch, 4 Complex, 5+ Complex Pro/REX); ignored when warping is False
    - arrangement: True for a clip on the timeline (default), False for a Session slot
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    result = _deps(ctx).service.set_clip_warp(
        track_index, clip_index, bool(warping),
        None if warp_mode is None else int(warp_mode), arrangement,
        expect_track_name)
    return presenters.set_clip_warp(result)


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
                      field: str = "output_routing_type",
                      expect_track_name: Optional[str] = None) -> str:
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
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    result = _deps(ctx).service.set_track_routing(track_index, target, field,
                                                  expect_track_name)
    return presenters.set_track_routing(result)


@tool
def set_count_in(ctx: Context, bars: int = 1, metronome: bool = True) -> str:
    """
    Set the record count-in, giving a performer a lead-in before punching in.

    This is the right way to get a count-in: it applies only when recording, so
    it needs no empty bar inserted at the front of the arrangement.

    NOTE: Live 12.3+ exposes count-in as read-only to the API. On those builds
    the metronome half is still applied and the result says the count-in must
    be set in Live's UI.

    Parameters:
    - bars: 0 = none, 1 = 1 bar, 2 = 2 bars, 3 = 4 bars (Live's own indices)
    - metronome: Turn the metronome on, so the count-in is audible
    """
    result = _deps(ctx).service.set_count_in(bars, metronome)
    return presenters.set_count_in(result)


@tool
def set_track_send(ctx: Context, track_index: int, send_index: int, value: float,
                   expect_track_name: Optional[str] = None) -> str:
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
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    result = _deps(ctx).service.set_track_send(track_index, send_index, value,
                                               expect_track_name)
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
def set_track_arm(ctx: Context, track_index: int, armed: bool = True,
                  expect_track_name: Optional[str] = None) -> str:
    """
    Arm or disarm a track for recording.

    Parameters:
    - track_index: The index of the track
    - armed: True to arm, False to disarm
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    result = _deps(ctx).service.set_track_arm(track_index, armed,
                                              expect_track_name)
    return presenters.set_track_arm(result, track_index)


@tool
def set_track_monitoring(ctx: Context, track_index: int, state: str = "auto",
                         expect_track_name: Optional[str] = None) -> str:
    """
    Set a track's input monitoring, so the performer can hear themselves.

    Parameters:
    - track_index: The index of the track
    - state: "in" (always monitor input), "auto" (monitor when armed), or "off"
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    result = _deps(ctx).service.set_track_monitoring(track_index, state,
                                                     expect_track_name)
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
                         value: float,
                         parameter: Optional[str] = None,
                         track_type: str = "regular",
                         parameter_name: Optional[str] = None,
                         expect_track_name: Optional[str] = None) -> str:
    """
    Set one parameter on a device. This is how you actually mix: pull a reverb's
    Dry/Wet down, set a delay's feedback, change a filter cutoff.

    Setting several knobs on the same device? Use set_device_parameters
    (plural) — one call takes a whole {name: value} map, which is what
    dialling in a compressor or an EQ actually looks like.

    The value is clamped into the parameter's own range rather than erroring, so
    passing 0 always means "as low as this goes".

    Parameters:
    - track_index: The index of the track containing the device
    - device_index: The index of the device in that track's chain (0 = first)
    - parameter: The parameter's name as shown by get_device_parameters (e.g.
      "Dry/Wet"), or its integer index passed as a string (e.g. "3").
      Also accepted as parameter_name
    - value: The value to set, in the parameter's own units
    - track_type: "regular" (default) or "return"
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    param: Any = _one_value("parameter", parameter,
                            parameter_name=parameter_name)
    if param is None:
        raise ValueError(presenters.argument_required(
            "parameter", ("parameter_name",),
            "the parameter's name as get_device_parameters shows it "
            "(e.g. \"Dry/Wet\"), or its index as a string"))
    # Deliberately NOT coerced to int here. The script resolves a string by
    # NAME first and only then as an index, so that a parameter genuinely
    # named "3" stays reachable; coercing "3" to 3 in this layer would send
    # the singular tool down the index path while set_device_parameters sent
    # the same key down the name path — the two tools would write to
    # different parameters on the same device.
    result = _deps(ctx).service.set_device_parameter(
        track_index, device_index, param, value, track_type,
        expect_track_name)
    return presenters.set_device_parameter(result)


@tool
def set_device_parameters(ctx: Context, track_index: int, device_index: int,
                          parameters: Dict[str, float],
                          track_type: str = "regular",
                          expect_track_name: Optional[str] = None) -> str:
    """
    Set MANY parameters on ONE device in a single call — the batch form of
    set_device_parameter, and the one to reach for when dialling a device in.

    Send every knob at once as a {name: value} map:
    {"Dry/Wet": 0.25, "Decay Time": 3.2, "Predelay": 0.02}. A device is
    normally a handful of related values that only make sense together, and
    sending them one at a time cost 257 round-trips in a single production
    session. Prefer this over repeated set_device_parameter calls whenever
    you are setting more than one thing on the same device.

    Keys are parameter names exactly as get_device_parameters shows them, or
    an integer index written as a string ("3"); a name always wins over an
    index. Values are in the parameter's own units and each is clamped into
    its own range, exactly as the singular tool clamps. One key that matches
    nothing does NOT lose the others — it comes back as its own not-found
    row while every other write lands.

    What this does NOT buy is freshness. Every write happens inside one pass
    on Live's main thread, and a value read back in that same pass can still
    be the pre-write one — the reported values are exactly as trustworthy as
    the singular tool's, no more. Re-read with get_device_parameters when
    you need settled values.

    Parameters:
    - track_index: The index of the track containing the device
    - device_index: The index of the device in that track's chain (0 = first)
    - parameters: {parameter name or index: value} — everything to set
    - track_type: "regular" (default) or "return"
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    result = _deps(ctx).service.set_device_parameters(
        track_index, device_index, parameters, track_type, expect_track_name)
    return presenters.set_device_parameters(result)


@tool
def delete_device(ctx: Context, track_index: int, device_index: int,
                  track_type: str = "regular",
                  expect_track_name: Optional[str] = None) -> str:
    """
    Remove a device from a track's chain.

    Deleting a device shifts the index of every device after it down by one, so
    when removing several, work from the highest index downwards.

    The reply names the whole surviving chain, index by index, and says
    whether the delete was actually verified. Re-anchor the next delete on
    those NAMES rather than on the indices you started with — and if it
    reports that nothing was removed, re-read instead of calling again: a
    blind second delete takes the NEXT device in the chain.

    Parameters:
    - track_index: The index of the track containing the device
    - device_index: The index of the device to remove (0 = first in the chain)
    - track_type: "regular" (default) or "return"
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    result = _deps(ctx).service.delete_device(track_index, device_index,
                                              track_type, expect_track_name)
    return presenters.delete_device(result, track_index)


@tool
def set_track_volume(ctx: Context, track_index: int, value: float,
                     track_type: str = "regular",
                     expect_track_name: Optional[str] = None) -> str:
    """
    Set a track's mixer volume.

    The scale is Live's own 0.0-1.0 fader position, NOT decibels: 0.85 is unity
    (0 dB), 0.0 is silence, 1.0 is +6 dB. The returned display_value gives the
    resulting level in dB so you can check it landed where you meant.

    Parameters:
    - track_index: The index of the track
    - value: Fader position from 0.0 to 1.0 (0.85 = 0 dB)
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    result = _deps(ctx).service.set_track_volume(track_index, value, track_type,
                                                 expect_track_name)
    return presenters.set_track_volume(result)


@tool
def set_track_pan(ctx: Context, track_index: int, value: float,
                  track_type: str = "regular",
                  expect_track_name: Optional[str] = None) -> str:
    """
    Set a track's stereo panning.

    Parameters:
    - track_index: The index of the track
    - value: -1.0 is hard left, 0.0 is centre, 1.0 is hard right
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    result = _deps(ctx).service.set_track_pan(track_index, value, track_type,
                                              expect_track_name)
    return presenters.set_track_pan(result)


@tool
def set_track_mute(ctx: Context, track_index: int, mute: bool,
                   expect_track_name: Optional[str] = None) -> str:
    """
    Mute or unmute a track. Useful for auditioning parts in isolation.

    Parameters:
    - track_index: The index of the track
    - mute: True to mute, False to unmute
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    result = _deps(ctx).service.set_track_mute(track_index, mute,
                                               expect_track_name)
    return presenters.set_track_mute(result, track_index)


@tool
def delete_track(ctx: Context, track_index: int,
                 expect_track_name: Optional[str] = None) -> str:
    """
    Delete a track from the Ableton session, along with all clips on it.

    Note that deleting a track shifts the index of every track after it down by
    one. When deleting several tracks, work from the highest index downwards so
    the remaining indices stay valid.

    Parameters:
    - track_index: The index of the track to delete
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    result = _deps(ctx).service.delete_track(track_index, expect_track_name)
    return presenters.delete_track(result, track_index)


@tool
def create_audio_clip(ctx: Context, track_index: int, clip_index: int,
                      path: Optional[str] = None,
                      expected_beats: Optional[float] = None,
                      file_path: Optional[str] = None,
                      expect_track_name: Optional[str] = None) -> str:
    """
    Create a new audio clip in an audio track's clip slot by importing a file.

    The clip is imported with WARPING OFF, so it plays at its recorded rate.
    Live's "Auto-Warp Long Samples" guesses a source tempo per file and
    guesses badly on material without clear transients: six identically-long
    stems from one render each landed on a different guess, which pulls an
    aligned multitrack apart while every import still reports a plausible
    length. Turn warping back on with set_clip_warp for material that really
    should follow the project tempo.

    Pass expected_beats when you know how long the file is — the reply says
    whether the clip Live produced actually matches, which is the check that
    catches a bad tempo guess at import instead of by ear hours later.

    Requires Ableton Live 12.0.5 or newer — the underlying
    ClipSlot.create_audio_clip Live API was introduced in 12.0.5 and is not
    available in earlier 12.0.x releases.

    Parameters:
    - track_index: The index of the audio track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - path: Absolute path to a supported audio file (e.g. a .wav). The target
      track must be an audio track and the clip slot must be empty.
      Also accepted as file_path
    - expected_beats: Optional — the length in beats you believe the file is
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    source = _one_value("path", path, file_path=file_path)
    if source is None:
        raise ValueError(presenters.argument_required(
            "path", ("file_path",), "an absolute path to an audio file"))
    result = _deps(ctx).service.create_audio_clip(
        track_index, clip_index, source, expected_beats, expect_track_name)
    return presenters.create_audio_clip(result, track_index, clip_index)


@tool
def add_notes_to_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    notes: List[Dict[str, Union[int, float, bool]]],
    expect_count: Optional[int] = None,
    arrangement: bool = False,
    expect_track_name: Optional[str] = None,
) -> str:
    """
    Add MIDI notes to a clip, in either view. Writes are ADDITIVE — to
    replace a clip's contents, clear_notes_from_clip first.

    STATE YOUR TOTAL with expect_count whenever the pattern matters, and
    especially when sending it in chunks: a note list that arrives short
    arrives short ABOVE this server, where a before/after count stays silent
    because Live faithfully added everything it was handed. expect_count
    refuses the write outright when the list disagrees, before half a drum
    pattern lands. The reply reports what Live actually took, not what you
    sent.

    arrangement=True edits a clip already on the timeline instead of a
    Session slot — that is how a single arranged bar gets fixed in place,
    rather than editing the Session clip and re-stamping every copy of it.
    It changes only that one clip: copies stamped elsewhere are independent.

    In both views a note's start_time is measured from the clip's own start,
    not the song's — an arrangement clip at bar 60 still takes notes at 0.0.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The clip slot index, or — with arrangement=True — the index
      into the track's Arrangement clips in start-time order
    - notes: List of note dictionaries, each with pitch, start_time, duration, velocity, and mute
    - expect_count: How many notes you intend to send; the write is refused
      if the list that arrived is a different length
    - arrangement: True to edit a clip on the timeline, False (default) for
      a Session slot
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    result = _deps(ctx).service.add_notes_to_clip(
        track_index, clip_index, notes, expect_count, arrangement,
        expect_track_name)
    return presenters.add_notes_to_clip(result, track_index, clip_index)


@tool
def clear_notes_from_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    arrangement: bool = False,
    expect_track_name: Optional[str] = None,
) -> str:
    """
    Remove all MIDI notes from a clip, in either view.

    Writes are additive (add_notes_to_clip only appends), so to truly *modify*
    a clip you clear it first, then add the new notes. Use this with
    get_clip_notes and add_notes_to_clip for a real read -> modify -> write
    loop: read the notes, edit the list, clear_notes_from_clip, then
    add_notes_to_clip the edited notes — and keep `arrangement` the same
    across all three, or you will read one clip and rewrite another.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The clip slot index, or — with arrangement=True — the index
      into the track's Arrangement clips in start-time order
    - arrangement: True to clear a clip on the timeline, False (default) for
      a Session slot
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    result = _deps(ctx).service.clear_notes_from_clip(
        track_index, clip_index, arrangement, expect_track_name)
    return presenters.clear_notes_from_clip(result, track_index, clip_index)


@tool
def set_clip_name(ctx: Context, track_index: int, clip_index: int, name: str,
                  expect_track_name: Optional[str] = None) -> str:
    """
    Set the name of a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - name: The new name for the clip
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    _deps(ctx).service.set_clip_name(track_index, clip_index, name,
                                     expect_track_name)
    return presenters.set_clip_name(track_index, clip_index, name)


@tool
def set_arrangement_clip_name(ctx: Context, track_index: int, clip_index: int,
                              name: str,
                              expect_track_name: Optional[str] = None) -> str:
    """
    Set the name of a clip placed in the Arrangement timeline.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip within track.arrangement_clips, in the
      same order returned by get_arrangement_clips (i.e. ordered by start_time)
    - name: The new name for the clip
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    _deps(ctx).service.set_arrangement_clip_name(track_index, clip_index, name,
                                                 expect_track_name)
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
                              track_type: str = "regular",
                              expect_track_name: Optional[str] = None) -> str:
    """
    Load an instrument or effect onto a track using its URI.

    Parameters:
    - track_index: The index of the track to load the instrument on
    - uri: The URI of the instrument or effect to load (e.g., 'query:Synths#Instrument%20Rack:Bass:FileId_5116')
    """
    result = _deps(ctx).service.load_browser_item(track_index, uri, track_type,
                                                 expect_track_name)
    return presenters.load_instrument_or_effect(result, track_index, uri)


@tool
def fire_clip(ctx: Context, track_index: int, clip_index: int,
              expect_track_name: Optional[str] = None) -> str:
    """
    Start playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    _deps(ctx).service.fire_clip(track_index, clip_index, expect_track_name)
    return presenters.fire_clip(track_index, clip_index)


@tool
def stop_clip(ctx: Context, track_index: int, clip_index: int,
              expect_track_name: Optional[str] = None) -> str:
    """
    Stop playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    _deps(ctx).service.stop_clip(track_index, clip_index, expect_track_name)
    return presenters.stop_clip(track_index, clip_index)


@tool
def delete_clip(ctx: Context, track_index: int, clip_index: int,
                expect_track_name: Optional[str] = None) -> str:
    """
    Delete the clip in the given clip slot, freeing it for reuse.

    Use this before create_clip when you want to overwrite an existing clip
    (create_clip itself refuses to write into an occupied slot).

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot to clear
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    result = _deps(ctx).service.delete_clip(track_index, clip_index,
                                            expect_track_name)
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
def load_drum_kit(ctx: Context, track_index: int, rack_uri: str, kit_path: str,
                  expect_track_name: Optional[str] = None) -> str:
    """
    Load a drum rack and then load a specific drum kit into it.

    Parameters:
    - track_index: The index of the track to load on
    - rack_uri: The URI of the drum rack to load (e.g., 'Drums/Drum Rack')
    - kit_path: Browser path to the kit — either a folder (its first loadable
      kit is used) or a kit file itself (e.g. 'drums/808 Core Kit.adg')
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    result = _deps(ctx).service.load_drum_kit(track_index, rack_uri, kit_path,
                                              expect_track_name)
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
    destination_time: Optional[float] = None,
    destination_times: Optional[List[float]] = None,
    allow_loop_phase_reset: bool = False,
    arrangement_time: Optional[float] = None,
    time: Optional[float] = None,
    expect_track_name: Optional[str] = None,
) -> str:
    """
    Copy a Session-view clip into the Arrangement timeline.

    USE destination_times FOR MORE THAN ONE POSITION. Laying a pattern
    across an arrangement is the single most expensive thing this server
    does — 465 calls and over an hour of round-trips in one session,
    arriving in runs of up to 76 identical stamps — and a list places all of
    them in one call:

        destination_times=[0, 4, 8, 12, 16]

    Every placement is guarded and reported individually, and one refusal
    never erases the placements that already landed; read placed_count in
    the reply rather than assuming a reply means all of them. Live still
    does one stamp per position on its main thread, so the run is capped
    (128 placements) — split a longer sweep into several calls.

    A stamp is REFUSED when it would leave a looping Arrangement clip
    surviving to its right: that survivor silently restarts its loop from
    the top, which is how three hat stamps scrambled three sections of a
    real arrangement without a single error message. The refusal names the
    victim and the safe shapes. allow_loop_phase_reset=True stamps anyway,
    for a caller who has decided the re-phasing is what they want.

    Typical workflow:
      1. create_clip / add_notes_to_clip to build a Session clip
      2. Call duplicate_to_arrangement ONCE with every bar position
      3. Call switch_to_arrangement_view to confirm the result in Live

    Parameters:
    - track_index:       Index of the track that owns the Session clip
    - clip_index:        Index of the clip slot in that track (Session view)
    - destination_time:  Beat position in the arrangement to place the clip
                         (e.g. 0.0 = start, 8.0 = bar 3 in 4/4). Also
                         accepted as arrangement_time or time
    - destination_times: A list of beat positions — place the same clip at
                         all of them in one call. Preferred over repeating
                         this tool
    - allow_loop_phase_reset: Stamp even where a looping clip to the right
                         would be re-phased (default False, which refuses)
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    # destination_time is the argument this surface gets wrong most often —
    # guessed as arrangement_time or time, seven times in four days.
    beat = _one_value("destination_time", destination_time,
                      arrangement_time=arrangement_time, time=time)
    if beat is None and destination_times is None:
        raise ValueError(presenters.argument_required(
            "destination_time", ("arrangement_time", "time"),
            "a beat position — or destination_times for a run of them"))
    # The run form REPLACES the single position on the wire rather than
    # adding to it, so accepting both would drop a stamp the caller thinks
    # they asked for.
    if beat is not None and destination_times is not None:
        raise ValueError(presenters.conflicting_forms(
            "destination_time", beat, "destination_times", destination_times,
            "Put every position in destination_times."))
    result = _deps(ctx).service.duplicate_session_clip_to_arrangement(
        track_index, clip_index, beat, destination_times,
        allow_loop_phase_reset, expect_track_name)
    return presenters.duplicate_to_arrangement(
        result, track_index, clip_index, beat)


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


@tool
def jump_to_locator(
    ctx: Context,
    name: str = "",
    time: Optional[float] = None,
) -> str:
    """
    Jump the Arrangement to an existing locator — and, while the transport
    is stopped, plant Live's start marker there so play and record launch
    from that spot.

    This is the API equivalent of clicking the locator in the scrub area,
    which is the only way to move the start marker: set_arrangement_time
    moves just the visible playhead, so record still launches from the old
    marker. Call this while STOPPED to re-aim recording; while playing it
    relocates playback instead and the start marker stays put (the reply
    says which happened). If no locator exists at the target yet, create
    one first with create_locator.

    Parameters:
    - name: Locator label to match exactly (e.g. "Chorus", "REC start")
    - time: Beat position to match (~1e-3 tolerance), used when no name
      is given or the name finds nothing
    """
    result = _deps(ctx).service.jump_to_locator(name, time)
    return presenters.jump_to_locator(result, name, time)


@tool
def delete_locator(
    ctx: Context,
    name: str = "",
    time: Optional[float] = None,
) -> str:
    """
    Delete an existing locator (cue point) from the Arrangement.

    The counterpart to create_locator, for section markers that were placed
    at the wrong beat or belong to an arrangement that has moved on. Live's
    API has no "delete this cue" call — there is only a toggle at the
    playhead — so this parks the playhead exactly on the locator, verifies
    it landed, and only then toggles. If the transport has not settled it
    refuses and changes nothing, because toggling from the wrong position
    does not fail: it CREATES a stray locator there and leaves the one you
    meant to delete in place.

    The playhead is moved and put back, which is audible while playing —
    prefer doing this stopped, as with create_locator.

    Matching is by exact name first, then by beat. Names are not unique in
    Live, so a name match takes the first locator with that name; pass time
    when you need to be exact about which one goes.

    Parameters:
    - name: Locator label to match exactly (e.g. "Chorus")
    - time: Beat position to match (~1e-3 tolerance), used when no name is
      given or the name finds nothing
    """
    result = _deps(ctx).service.delete_locator(name, time)
    return presenters.delete_locator(result, name, time)


@tool
def trim_arrangement_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    expect_track_name: Optional[str] = None,
) -> str:
    """
    Trim an Arrangement clip's edges inward — the cleanup after recording,
    when a take overhangs its section into the neighbouring one.

    Give the new edge position(s) in arrangement beats; omit an edge to
    leave it alone. Only shrinking is supported. Live has no arrangement
    resize API (marker writes verifiably don't move the footprint), so the
    script trims the way the UI does: it stamps a temporary silent clip
    over the region to remove — Live permanently crops whatever a stamp
    covers — then deletes the stamp and VERIFIES the take's new edge by
    readback. Each edge is checked independently: an edge whose stamp
    cannot be placed safely (e.g. a micro-trim right against a neighbouring
    clip) is refused with the take untouched, so a both-edge request can
    land partially, and the reply says exactly which edges changed. The
    audio file on disk is never touched, but the crop is real editing —
    restoring a trimmed edge is Edit > Undo in Live, not a drag. Needs one
    empty Session slot on the track for the temporary eraser clip, and
    Live 11+ (audio takes: Live 12.0.5+). Looping clips trim like any
    other. Use get_arrangement_clips to find clip_index, which counts
    clips on the track in start-time order.

    Parameters:
    - track_index: The track holding the clip
    - clip_index: Index into the track's Arrangement clips (start-time order)
    - start_time: New left edge in beats (omit to keep)
    - end_time: New right edge in beats (omit to keep)
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    result = _deps(ctx).service.trim_arrangement_clip(
        track_index, clip_index, start_time, end_time, expect_track_name)
    return presenters.trim_arrangement_clip(
        result, track_index, clip_index, start_time, end_time)


@tool
def delete_arrangement_clip(
    ctx: Context,
    track_index: int,
    clip_index: Optional[int] = None,
    start_time: Optional[float] = None,
    start_times: Optional[List[float]] = None,
    expect_track_name: Optional[str] = None,
) -> str:
    """
    Delete one or more clips from the Arrangement timeline — stray record
    fragments, scrapped takes, replaced sections.

    ADDRESS CLIPS BY BEAT, NOT BY INDEX, and delete them together:

        start_times=[64, 96, 128]

    A clip's start_time is a stable key (Live allows no overlaps on a
    track), while clip_index is a positional ordinal that renumbers on every
    delete — which is what forces the highest-index-first ritual and a
    re-read of the clip list between deletes. The plural form resolves every
    position BEFORE deleting anything, then deletes in a safe order itself.
    If any position matches nothing, nothing at all is deleted and the
    nearby clip starts are listed: a position that has moved usually means
    your picture of the timeline is stale, and the rest of the list is then
    no more trustworthy than the bad one.

    Deletion removes the clip from the timeline but never the audio file on
    disk, so a deleted take is not lost audio. Read get_arrangement_clips
    first for the start times.

    This is the Arrangement twin of delete_clip, which deletes Session-slot
    clips.

    Parameters:
    - track_index: The track holding the clip
    - start_times: Beat positions of every clip to delete, in one pass
      (preferred)
    - start_time: Beat position of a single clip to delete (~1e-3 tolerance)
    - clip_index: Legacy addressing — index into the track's Arrangement
      clips in start-time order. Works, but renumbers under you
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    # Exactly one way of saying which clip. The script has a precedence
    # order and the service honours it, but a caller who gave two is telling
    # us they are not sure which clip they mean — and this deletes takes.
    given = [(name, value) for name, value in (("start_times", start_times),
                                               ("start_time", start_time),
                                               ("clip_index", clip_index))
             if value is not None]
    if len(given) > 1:
        raise ValueError(presenters.conflicting_forms(
            given[0][0], given[0][1], given[1][0], given[1][1],
            "Say which clips to delete exactly once — start_times for a set, "
            "start_time for one, clip_index only when you have neither."))
    result = _deps(ctx).service.delete_arrangement_clip(
        track_index, clip_index, start_time, start_times, expect_track_name)
    return presenters.delete_arrangement_clip(result, track_index, clip_index)


@tool
def move_arrangement_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    destination_time: float,
    expect_track_name: Optional[str] = None,
) -> str:
    """
    Move an Arrangement clip so it starts at a new beat position — sliding
    a take into place, or clearing room to comp another one in.

    Live has no true move API, so under the hood this duplicates the clip
    to the destination, verifies the copy landed, then deletes the
    original — the audio and its timing inside the clip are preserved. A
    destination overlapping the clip's own current span is refused (move
    in two hops via a clear stretch). An occupied destination is resolved
    by Live itself (typically the overlapped region of the existing clip
    is replaced — unverified on every build), so read get_arrangement_clips
    first when the target might be occupied, and re-read it AFTER the move:
    clip indices are start-time ordered and will have shifted.

    Parameters:
    - track_index: The track holding the clip
    - clip_index: Index into the track's Arrangement clips (start-time order)
    - destination_time: New start position in beats
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    result = _deps(ctx).service.move_arrangement_clip(
        track_index, clip_index, destination_time, expect_track_name)
    return presenters.move_arrangement_clip(
        result, track_index, clip_index, destination_time)


@tool
def duplicate_arrangement_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    destination_time: float,
    allow_loop_phase_reset: bool = False,
    expect_track_name: Optional[str] = None,
) -> str:
    """
    Copy an Arrangement clip to another beat position on the same track —
    reusing an already-recorded take at another section (a verse take
    repeated, a chorus doubled at the outro), then trim_arrangement_clip
    the copy to fit.

    The Arrangement twin of duplicate_to_arrangement (whose source is a
    Session slot), and it carries the same loop-phase refusal: a copy that
    would leave a LOOPING clip surviving to its right is refused, because
    that survivor silently restarts its loop from the top and quietly
    rewrites bars nobody named. An occupied destination is resolved by Live itself
    (typically the overlapped region of the existing clip is replaced —
    including the source's own span, which truncates the source), so check
    get_arrangement_clips first when the target might be occupied, and
    re-read it AFTER the copy to confirm the result and find the copy's
    index: indices are start-time ordered and will have shifted.

    Parameters:
    - track_index: The track holding the clip
    - clip_index: Index into the track's Arrangement clips (start-time order)
    - destination_time: Where the copy should start, in beats
    - allow_loop_phase_reset: Copy even where a looping clip to the right
      would be re-phased (default False, which refuses and names the victim)
    - expect_track_name: Optional safety net — refuse and change nothing
      unless the track at track_index is really named this. Track indices
      renumber whenever a track is added or deleted, and an index that is
      still in range but now points elsewhere fails silently otherwise
    """
    result = _deps(ctx).service.duplicate_arrangement_clip(
        track_index, clip_index, destination_time, allow_loop_phase_reset,
        expect_track_name)
    return presenters.duplicate_arrangement_clip(
        result, track_index, clip_index, destination_time)
