"""The mock Ableton (docs/REFACTOR_PLAN.md section 5 Level 3, Appendix B).

A plain-Python model of exactly the Live Object Model surface the Remote
Script touches, plus a harness that imports the REAL canonical Remote Script
(with a stubbed ``_Framework``) and drives its ``_process_command`` dispatch.
Runs anywhere: no Ableton, no network, no threads, no sockets.

Typical use::

    from fake_ableton import FakeLiveConfig, make_harness

    harness = make_harness(config=FakeLiveConfig(note_api="legacy"))
    response = harness.process({"type": "get_session_info", "params": {}})
"""

from .framework_stub import install_framework_stub
from .lom import (
    FakeApplication,
    FakeBrowser,
    FakeBrowserItem,
    FakeClip,
    FakeClipSlot,
    FakeCuePoint,
    FakeDevice,
    FakeLiveConfig,
    FakeMixerDevice,
    FakeNote,
    FakeParameter,
    FakeRoutingOption,
    FakeScene,
    FakeSong,
    FakeTrack,
    FakeWarpMarker,
)
from .harness import RemoteScriptHarness, load_remote_script

__all__ = [
    "FakeApplication",
    "FakeBrowser",
    "FakeBrowserItem",
    "FakeClip",
    "FakeClipSlot",
    "FakeCuePoint",
    "FakeDevice",
    "FakeLiveConfig",
    "FakeMixerDevice",
    "FakeNote",
    "FakeParameter",
    "FakeRoutingOption",
    "FakeScene",
    "FakeSong",
    "FakeTrack",
    "FakeWarpMarker",
    "RemoteScriptHarness",
    "install_framework_stub",
    "load_remote_script",
    "make_harness",
    "make_standard_browser",
    "make_standard_song",
]


# ── Standard fixtures ────────────────────────────────────────────────────────

# The Lead clip's notes, shared with tests that assert on them.
LEAD_RIFF_NOTES = [
    {"pitch": 60, "start_time": 0.0, "duration": 0.5, "velocity": 100, "mute": False},
    {"pitch": 64, "start_time": 1.0, "duration": 0.5, "velocity": 90, "mute": False},
    {"pitch": 67, "start_time": 2.0, "duration": 1.0, "velocity": 80, "mute": True},
]

DRUM_LOOP_NOTES = [
    {"pitch": 36, "start_time": 0.0, "duration": 0.25, "velocity": 127, "mute": False},
    {"pitch": 38, "start_time": 1.0, "duration": 0.25, "velocity": 110, "mute": False},
]


def make_standard_song(config=None):
    """A representative Live set:

    - track 0 "Lead"  — MIDI; clip "Lead Riff" in slot 0 (3 notes); Operator
      device with parameters
    - track 1 "Drums" — MIDI; clip "Drum Loop" in slot 1 (2 notes); Impulse
      device with parameters
    - track 2 "Audio" — audio; empty slots; one Arrangement clip "Vox Take"
    - return track "A Reverb" with a Reverb device; every regular track has
      one send
    - master track, 4 scenes, routing options on every track, one cue point
      ("Verse" at beat 8)
    """
    config = config if config is not None else FakeLiveConfig()
    song = FakeSong(config)
    song.scenes = [FakeScene("Scene %d" % (i + 1)) for i in range(4)]

    reverb_return = FakeTrack("A Reverb", config=config, kind="return")
    reverb_return.devices.append(FakeDevice(
        "Reverb",
        class_name="Reverb",
        class_display_name="Reverb",
        parameters=[
            FakeParameter("Device On", 1.0, 0.0, 1.0, is_quantized=True),
            FakeParameter("Dry/Wet", 0.35, 0.0, 1.0, unit="%"),
            FakeParameter("Decay Time", 2.5, 0.1, 60.0, unit="s"),
        ],
    ))
    song.return_tracks.append(reverb_return)

    lead = FakeTrack("Lead", midi=True, config=config, num_slots=4, num_sends=1)
    lead.devices.append(FakeDevice(
        "Operator",
        class_name="Operator",
        class_display_name="Operator",
        parameters=[
            FakeParameter("Device On", 1.0, 0.0, 1.0, is_quantized=True),
            FakeParameter("Filter Freq", 60.0, 0.0, 127.0, unit="Hz"),
            FakeParameter("Resonance", 0.2, 0.0, 1.0),
        ],
    ))
    lead.clip_slots[0].clip = FakeClip("Lead Riff", 4.0, midi=True,
                                       config=config, notes=LEAD_RIFF_NOTES)

    drums = FakeTrack("Drums", midi=True, config=config, num_slots=4, num_sends=1)
    drums.devices.append(FakeDevice(
        "Impulse",
        class_name="InstrumentImpulse",
        class_display_name="Impulse",
        parameters=[
            FakeParameter("Device On", 1.0, 0.0, 1.0, is_quantized=True),
            FakeParameter("Global Volume", 0.8, 0.0, 1.0, unit="dB"),
        ],
    ))
    drums.clip_slots[1].clip = FakeClip("Drum Loop", 8.0, midi=True,
                                        config=config, notes=DRUM_LOOP_NOTES)

    audio = FakeTrack("Audio", midi=False, config=config, num_slots=4, num_sends=1)
    audio.arrangement_clips.append(
        FakeClip("Vox Take", 8.0, midi=False, config=config, start_time=0.0))

    song.tracks = [lead, drums, audio]
    song.cue_points.append(FakeCuePoint(8.0, "Verse"))
    return song


def make_standard_browser(song=None):
    """A small but plausible browser tree under the five required roots."""
    operator = FakeBrowserItem("Operator", uri="query:Synths#Operator",
                               is_loadable=True, is_device=True)
    wavetable = FakeBrowserItem("Wavetable", uri="query:Synths#Wavetable",
                                is_loadable=True, is_device=True)
    synths = FakeBrowserItem("Synths", uri="query:Synths",
                             children=[operator, wavetable])
    return FakeBrowser(
        song=song,
        instruments=FakeBrowserItem("Instruments", uri="query:Instruments",
                                    children=[synths]),
        sounds=FakeBrowserItem("Sounds", uri="query:Sounds", children=[
            FakeBrowserItem("Pad Ambient", uri="query:Sounds#PadAmbient",
                            is_loadable=True),
        ]),
        drums=FakeBrowserItem("Drums", uri="query:Drums", children=[
            FakeBrowserItem("808 Core Kit", uri="query:Drums#808CoreKit",
                            is_loadable=True, is_device=True),
        ]),
        audio_effects=FakeBrowserItem("Audio Effects", uri="query:AudioFx",
                                      children=[
            FakeBrowserItem("Reverb", uri="query:AudioFx#Reverb",
                            is_loadable=True, is_device=True),
        ]),
        midi_effects=FakeBrowserItem("MIDI Effects", uri="query:MidiFx",
                                     children=[
            FakeBrowserItem("Arpeggiator", uri="query:MidiFx#Arpeggiator",
                            is_loadable=True, is_device=True),
        ]),
    )


def make_harness(config=None, song=None, application=None,
                 schedule_raises=False):
    """Build a :class:`RemoteScriptHarness` around the standard song and a
    populated browser. Pass ``config`` to flip the Live-version toggles,
    ``schedule_raises=True`` to make ``schedule_message`` raise
    ``AssertionError`` (exercising the script's direct-execution fallback)."""
    config = config if config is not None else FakeLiveConfig()
    if song is None:
        song = make_standard_song(config)
    if application is None:
        application = FakeApplication(song=song, config=config,
                                      browser=make_standard_browser(song))
    return RemoteScriptHarness(song=song, application=application,
                               config=config, schedule_raises=schedule_raises)
