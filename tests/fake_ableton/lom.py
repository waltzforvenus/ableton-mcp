"""Plain-Python fakes for the Live Object Model surface the Remote Script
touches — and nothing more (docs/REFACTOR_PLAN.md Appendix B is the contract).

Every hasattr/getattr probe the script performs against real Live is a
**toggle** here, carried by :class:`FakeLiveConfig`, so the fallback branches
that have only ever executed inside Live can be tested on both sides:

- ``note_api`` — which note-API generation a clip exposes. Writes in the
  script are ALWAYS legacy ``set_notes`` (never ``add_new_notes``), so the
  ``"extended_only"`` configuration faithfully breaks ``add_notes_to_clip``.
- ``extended_read_raises`` — ``get_notes_extended`` raises, exercising the
  script's try/except fallback into legacy ``get_notes``.
- ``create_audio_clip_api`` — ``ClipSlot.create_audio_clip`` present/absent
  (the Live 12.0.5 gate).
- ``count_in_read_only`` — ``Song.count_in_duration`` setter raises, as on
  Live 12.3.2 ("property of 'Song' object has no setter").
- ``save_owner`` — which object (song / application / neither) exposes a
  callable save candidate for the ``_save_set`` probe chain.
- ``warp_markers`` — audio clips carry warp markers.
- ``extended_note_fields`` — extended-read note objects carry the optional
  per-field-hasattr attributes (probability, note_id, ...).

All state mutations are observable through public attributes so tests can
assert on the fake's state after driving the real handlers.
"""

import os


_NOTE_API_GENERATIONS = ("legacy", "both", "extended_only")
_SAVE_OWNERS = ("song", "application", None)


class FakeLiveConfig(object):
    """Which Live-version-dependent APIs the fake presents (Appendix B)."""

    def __init__(self, note_api="both", extended_read_raises=False,
                 create_audio_clip_api=True, count_in_read_only=False,
                 save_owner="song", warp_markers=False,
                 extended_note_fields=False):
        if note_api not in _NOTE_API_GENERATIONS:
            raise ValueError("note_api must be one of %r" % (_NOTE_API_GENERATIONS,))
        if save_owner not in _SAVE_OWNERS:
            raise ValueError("save_owner must be one of %r" % (_SAVE_OWNERS,))
        self.note_api = note_api
        self.extended_read_raises = extended_read_raises
        self.create_audio_clip_api = create_audio_clip_api
        self.count_in_read_only = count_in_read_only
        self.save_owner = save_owner
        self.warp_markers = warp_markers
        self.extended_note_fields = extended_note_fields


# ── Parameters, devices, mixer ───────────────────────────────────────────────

class FakeParameter(object):
    """A DeviceParameter/mixer parameter: name/value/min/max/is_quantized/
    is_enabled/str_for_value/value_string/automation_state."""

    def __init__(self, name, value=0.0, min=0.0, max=1.0, is_quantized=False,
                 is_enabled=True, unit="", automation_state=0):
        self.name = name
        self.min = float(min)
        self.max = float(max)
        self._value = float(value)
        self.is_quantized = bool(is_quantized)
        self.is_enabled = bool(is_enabled)
        self.automation_state = int(automation_state)
        self._unit = unit

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, new_value):
        new_value = float(new_value)
        # Live throws on out-of-range assignment (the reason the fork's
        # _set_device_parameter clamps first), so the fake does too.
        if new_value < self.min or new_value > self.max:
            raise RuntimeError(
                "Invalid value %r for parameter '%s' (range %s to %s)"
                % (new_value, self.name, self.min, self.max))
        self._value = new_value

    def str_for_value(self, value):
        return ("%.2f %s" % (float(value), self._unit)).strip()

    @property
    def value_string(self):
        return self.str_for_value(self._value)


class FakeDevice(object):
    def __init__(self, name, class_name=None, class_display_name=None,
                 parameters=None):
        self.name = name
        self.class_name = class_name if class_name is not None else name
        self.class_display_name = (class_display_name
                                   if class_display_name is not None else name)
        self.can_have_drum_pads = False
        self.can_have_chains = False
        if parameters is None:
            parameters = [FakeParameter("Device On", 1.0, 0.0, 1.0,
                                        is_quantized=True)]
        self.parameters = list(parameters)


class FakeMixerDevice(object):
    def __init__(self, num_sends=0):
        self.volume = FakeParameter("Volume", 0.85, 0.0, 1.0, unit="dB")
        self.panning = FakeParameter("Pan", 0.0, -1.0, 1.0)
        self.sends = [
            FakeParameter("Send %s" % chr(ord("A") + i), 0.0, 0.0, 1.0, unit="dB")
            for i in range(num_sends)
        ]


# ── Routing, cues, scenes ────────────────────────────────────────────────────

class FakeRoutingOption(object):
    """A routing type/channel option; the script reads ``display_name``."""

    def __init__(self, display_name):
        self.display_name = display_name

    def __repr__(self):
        return "FakeRoutingOption(%r)" % (self.display_name,)


class FakeCuePoint(object):
    def __init__(self, time, name=""):
        self.time = float(time)
        self.name = name


class FakeScene(object):
    def __init__(self, name, tempo=None, is_triggered=False):
        self.name = name
        self.tempo = tempo
        self.is_triggered = bool(is_triggered)


class FakeWarpMarker(object):
    def __init__(self, beat_time, sample_time):
        self.beat_time = float(beat_time)
        self.sample_time = float(sample_time)


# ── Notes and clips ──────────────────────────────────────────────────────────

class FakeNote(object):
    """Extended-API note object: attributes only, built from a stored record."""

    def __init__(self, record):
        for key, value in record.items():
            setattr(self, key, value)


def _normalize_note(note):
    """Accept the script's legacy tuples or the wire's dicts; store dicts."""
    if isinstance(note, dict):
        return {
            "pitch": int(note.get("pitch", 60)),
            "start_time": float(note.get("start_time", 0.0)),
            "duration": float(note.get("duration", 0.25)),
            "velocity": float(note.get("velocity", 100)),
            "mute": bool(note.get("mute", False)),
        }
    return {
        "pitch": int(note[0]),
        "start_time": float(note[1]),
        "duration": float(note[2]),
        "velocity": float(note[3]),
        "mute": bool(note[4]) if len(note) > 4 else False,
    }


class FakeClip(object):
    """A Session or Arrangement clip.

    Which note-API generation exists is decided per Appendix B by binding
    instance attributes in ``__init__`` — a missing generation is genuinely
    absent, so the script's ``hasattr`` probes behave exactly as in Live.
    The extended and legacy signatures deliberately keep their **swapped
    argument order** (pitch-first vs time-first); a script that passed
    legacy-ordered arguments to the extended API would select the wrong notes
    here, just as it would in Live.
    """

    def __init__(self, name, length, midi=True, config=None, notes=None,
                 start_time=0.0, color=0):
        self._config = config if config is not None else FakeLiveConfig()
        self.name = name
        self.length = float(length)
        self.is_midi_clip = bool(midi)
        self.is_audio_clip = not self.is_midi_clip
        self.is_playing = False
        self.is_recording = False
        self.color = int(color)
        self.start_time = float(start_time)
        self.end_time = self.start_time + self.length
        self.looping = False
        self.loop_start = 0.0
        self.loop_end = self.length
        self.launch_mode = 0
        self._notes = []
        self._next_note_id = 1

        if self.is_audio_clip:
            self.gain = 0.5
            self.gain_display_string = "0.0 dB"
            self.warping = True
            self.warp_mode = 0
            self.pitch_coarse = 0
            self.pitch_fine = 0
            self.file_path = "/samples/%s.wav" % name.replace(" ", "_").lower()
            if self._config.warp_markers:
                self.warp_markers = [FakeWarpMarker(0.0, 0.0),
                                     FakeWarpMarker(4.0, 2.0)]

        api = self._config.note_api
        if api in ("legacy", "both"):
            self.get_notes = self._legacy_get_notes
            self.set_notes = self._legacy_set_notes
            self.remove_notes = self._legacy_remove_notes
        if api in ("both", "extended_only"):
            self.get_notes_extended = self._extended_get_notes
            self.remove_notes_extended = self._extended_remove_notes

        for note in (notes or []):
            self._store_note(note)

    # -- internal note store ------------------------------------------------

    def _store_note(self, note):
        record = _normalize_note(note)
        if self._config.extended_note_fields:
            record.update({
                "probability": 1.0,
                "velocity_deviation": 0.0,
                "release_velocity": 64.0,
                "note_id": self._next_note_id,
            })
            self._next_note_id += 1
        self._notes.append(record)

    @property
    def stored_notes(self):
        """The clip's note state, for test assertions (copies, not aliases)."""
        return [dict(record) for record in self._notes]

    @staticmethod
    def _in_range(record, from_pitch, pitch_span, from_time, time_span):
        return (from_pitch <= record["pitch"] < from_pitch + pitch_span
                and from_time <= record["start_time"] < from_time + time_span)

    # -- extended generation (Live 11+): pitch-first argument order ---------

    def _extended_get_notes(self, from_pitch, pitch_span, from_time, time_span):
        if self._config.extended_read_raises:
            raise RuntimeError("get_notes_extended raised (fake configuration)")
        return tuple(
            FakeNote(record) for record in self._notes
            if self._in_range(record, from_pitch, pitch_span, from_time, time_span)
        )

    def _extended_remove_notes(self, from_pitch, pitch_span, from_time, time_span):
        self._notes = [
            record for record in self._notes
            if not self._in_range(record, from_pitch, pitch_span, from_time, time_span)
        ]

    # -- legacy generation: time-first argument order, tuple notes ----------

    def _legacy_get_notes(self, from_time, from_pitch, time_span, pitch_span):
        return tuple(
            (record["pitch"], record["start_time"], record["duration"],
             record["velocity"], record["mute"])
            for record in self._notes
            if self._in_range(record, from_pitch, pitch_span, from_time, time_span)
        )

    def _legacy_set_notes(self, notes):
        for note in notes:
            self._store_note(note)

    def _legacy_remove_notes(self, from_time, from_pitch, time_span, pitch_span):
        self._notes = [
            record for record in self._notes
            if not self._in_range(record, from_pitch, pitch_span, from_time, time_span)
        ]

    # -- duplication (for duplicate_clip_to_arrangement) --------------------

    def copy(self):
        duplicate = FakeClip(self.name, self.length, midi=self.is_midi_clip,
                             config=self._config, start_time=self.start_time,
                             color=self.color)
        duplicate._notes = [dict(record) for record in self._notes]
        duplicate._next_note_id = self._next_note_id
        if self.is_audio_clip:
            for attr in ("gain", "gain_display_string", "warping", "warp_mode",
                         "pitch_coarse", "pitch_fine", "file_path"):
                setattr(duplicate, attr, getattr(self, attr))
            if hasattr(self, "warp_markers"):
                duplicate.warp_markers = list(self.warp_markers)
        return duplicate


class FakeClipSlot(object):
    def __init__(self, config=None):
        self._config = config if config is not None else FakeLiveConfig()
        self.clip = None
        # ClipSlot.create_audio_clip only exists on Live >= 12.0.5; the
        # script hasattr-gates on it, so absence must be genuine absence.
        if self._config.create_audio_clip_api:
            self.create_audio_clip = self._create_audio_clip_impl

    @property
    def has_clip(self):
        return self.clip is not None

    def create_clip(self, length):
        if self.clip is not None:
            raise RuntimeError("Clip slot already has a clip")
        # Live names a freshly created MIDI clip with an empty string.
        self.clip = FakeClip("", length, midi=True, config=self._config)

    def _create_audio_clip_impl(self, path):
        if self.clip is not None:
            raise RuntimeError("Clip slot already has a clip")
        name = os.path.splitext(os.path.basename(path))[0]
        clip = FakeClip(name, 4.0, midi=False, config=self._config)
        clip.file_path = path
        self.clip = clip

    def delete_clip(self):
        if self.clip is None:
            raise RuntimeError("Clip slot has no clip")
        self.clip = None

    def fire(self):
        if self.clip is not None:
            self.clip.is_playing = True

    def stop(self):
        if self.clip is not None:
            self.clip.is_playing = False


# ── Tracks ───────────────────────────────────────────────────────────────────

class FakeTrack(object):
    def __init__(self, name, midi=False, config=None, num_slots=4,
                 num_sends=0, kind="regular"):
        self._config = config if config is not None else FakeLiveConfig()
        self.name = name
        self.mute = False
        self.solo = False
        self.arm = False
        self.can_be_armed = kind == "regular"
        self.current_monitoring_state = 1  # Auto
        if kind == "regular":
            self.has_midi_input = bool(midi)
            self.has_audio_input = not midi
        else:
            # Returns and the master are audio-only and slotless.
            self.has_midi_input = False
            self.has_audio_input = True
            num_slots = 0
        self.clip_slots = [FakeClipSlot(self._config) for _ in range(num_slots)]
        self.devices = []
        self.arrangement_clips = []
        self.mixer_device = FakeMixerDevice(num_sends=num_sends)

        # Routing: current values are option objects; available_* lists hold
        # every option a display-name lookup may match.
        main_out = FakeRoutingOption("Main")
        self.available_output_routing_types = [main_out,
                                               FakeRoutingOption("Sends Only")]
        self.output_routing_type = main_out
        master_channel = FakeRoutingOption("Master")
        self.available_output_routing_channels = [master_channel]
        self.output_routing_channel = master_channel
        no_input = FakeRoutingOption("No Input")
        self.available_input_routing_types = [no_input,
                                              FakeRoutingOption("Resampling")]
        self.input_routing_type = no_input
        all_channels = FakeRoutingOption("All Channels")
        self.available_input_routing_channels = [all_channels]
        self.input_routing_channel = all_channels

    def delete_device(self, device_index):
        del self.devices[device_index]

    def duplicate_clip_to_arrangement(self, clip, destination_time):
        duplicate = clip.copy()
        duplicate.start_time = float(destination_time)
        duplicate.end_time = duplicate.start_time + duplicate.length
        self.arrangement_clips.append(duplicate)
        return duplicate


# ── Song ─────────────────────────────────────────────────────────────────────

class _FakeSongView(object):
    def __init__(self):
        self.selected_track = None


class FakeSong(object):
    def __init__(self, config=None):
        self.config = config if config is not None else FakeLiveConfig()
        self.tempo = 120.0
        self.signature_numerator = 4
        self.signature_denominator = 4
        self.tracks = []
        self.return_tracks = []
        self.master_track = FakeTrack("Master", config=self.config, kind="master")
        self.is_playing = False
        self.current_song_time = 0.0
        self.song_length = 32.0
        self.loop = False
        self.loop_start = 0.0
        self.loop_length = 16.0
        self.back_to_arranger = True
        self.metronome = False
        self._count_in_duration = 1
        self.cue_points = []
        self.scenes = []
        self.view = _FakeSongView()
        if self.config.save_owner == "song":
            self.save_calls = []
            self.save_set = lambda: self.save_calls.append("save_set")

    # count_in_duration is a property so the Live 12.3.2 read-only behavior
    # ("property of 'Song' object has no setter") can be emulated faithfully.
    @property
    def count_in_duration(self):
        return self._count_in_duration

    @count_in_duration.setter
    def count_in_duration(self, value):
        if self.config.count_in_read_only:
            raise AttributeError("property 'count_in_duration' of 'Song' object "
                                 "has no setter")
        self._count_in_duration = int(value)

    # -- track management ---------------------------------------------------

    def _new_track_name(self, suffix):
        return "%d %s" % (len(self.tracks) + 1, suffix)

    def create_midi_track(self, index=-1):
        track = FakeTrack(self._new_track_name("MIDI"), midi=True,
                          config=self.config,
                          num_slots=len(self.scenes) or 4,
                          num_sends=len(self.return_tracks))
        if index == -1:
            self.tracks.append(track)
        else:
            self.tracks.insert(index, track)

    def create_audio_track(self, index=-1):
        track = FakeTrack(self._new_track_name("Audio"), midi=False,
                          config=self.config,
                          num_slots=len(self.scenes) or 4,
                          num_sends=len(self.return_tracks))
        if index == -1:
            self.tracks.append(track)
        else:
            self.tracks.insert(index, track)

    def create_return_track(self):
        name = "%s Return" % chr(ord("A") + len(self.return_tracks))
        self.return_tracks.append(
            FakeTrack(name, config=self.config, kind="return"))

    def delete_track(self, index):
        del self.tracks[index]

    # -- transport ----------------------------------------------------------

    def start_playing(self):
        self.is_playing = True

    def stop_playing(self):
        self.is_playing = False

    def set_or_delete_cue(self):
        """Toggle a cue point at the current playhead, as Live does."""
        playhead = self.current_song_time
        for cue in list(self.cue_points):
            if abs(cue.time - playhead) < 1e-6:
                self.cue_points.remove(cue)
                return
        self.cue_points.append(
            FakeCuePoint(playhead, "Locator %d" % (len(self.cue_points) + 1)))


# ── Application and browser ──────────────────────────────────────────────────

class FakeBrowserItem(object):
    def __init__(self, name, uri=None, children=None, is_loadable=False,
                 is_device=False, is_folder=None):
        self.name = name
        self.uri = uri
        self.children = list(children or [])
        self.is_loadable = bool(is_loadable)
        self.is_device = bool(is_device)
        self.is_folder = bool(self.children) if is_folder is None else is_folder


class FakeBrowser(object):
    """Browser with the five roots the script requires. ``dir()`` over an
    instance yields the root names as the script's category enumeration
    expects. ``load_item`` records each load and drops a device onto the
    song's selected track — the observable effect loading has in Live."""

    def __init__(self, song=None, instruments=None, sounds=None, drums=None,
                 audio_effects=None, midi_effects=None):
        self._song = song
        self.loads = []
        empty = lambda name, uri: FakeBrowserItem(name, uri=uri)
        self.instruments = instruments or empty("Instruments", "query:Instruments")
        self.sounds = sounds or empty("Sounds", "query:Sounds")
        self.drums = drums or empty("Drums", "query:Drums")
        self.audio_effects = audio_effects or empty("Audio Effects", "query:AudioFx")
        self.midi_effects = midi_effects or empty("MIDI Effects", "query:MidiFx")

    def load_item(self, item):
        if not item.is_loadable:
            raise RuntimeError("Browser item '%s' is not loadable" % item.name)
        selected = None
        if self._song is not None:
            selected = self._song.view.selected_track
        self.loads.append((item, selected))
        if selected is not None:
            selected.devices.append(FakeDevice(item.name))


class _FakeAppView(object):
    def __init__(self):
        self.shown = []

    def show_view(self, name):
        self.shown.append(name)


class FakeApplication(object):
    def __init__(self, song=None, config=None, browser=None):
        self._config = config if config is not None else FakeLiveConfig()
        self.view = _FakeAppView()
        self.browser = browser
        if self.browser is None:
            self.browser = FakeBrowser(song=song)
        if self._config.save_owner == "application":
            self.save_calls = []
            self.save_set = lambda: self.save_calls.append("save_set")
