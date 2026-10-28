# Changelog

Notable changes to this fork. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are the
`pyproject.toml` package version, with the Remote Script's own version
(`SCRIPT_VERSION`) called out where it changes — the two halves upgrade
separately, and a script change means **re-run
`ableton-mcp-install-script` and restart Ableton Live**.

## [Unreleased]

### Documentation

- Added a branch-scoped [fork comparison](docs/FORK_FEATURES.md), separating
  shipped additions, upstream ports, pending Live validation and planned work.
- Corrected the README tool count and installation examples to use this fork.

Ships Remote Script **1.26.0**. **Re-run `ableton-mcp-install-script` and
restart Live after upgrading** — the new commands live in the script half,
and Live caches an already-imported script module, so re-selecting the
control surface is not enough. This release is larger than usual on the
script side: several existing commands changed what they *return*, and a few
now refuse work they previously performed, so an out-of-date script is
refused with the re-run-the-installer message rather than served wrongly.

Everything here comes out of a four-day production session in which this fork
drove a complete track; the backlog it was mined from, with the measured
counts, is `docs/IMPROVEMENTS.md`.

### Added (user-facing)

- **Device placement and sidechain** (Remote Script **1.26.0**):
  `insert_device(track, device_name, index)` † (Live 12's
  `Track.insert_device` — a native device at a position, where browser loads
  always append), `move_device(track, device_index, to_index,
  to_track_index?)` † (`Song.move_device`, reorder or move across tracks),
  `get_device_sidechain` † / `set_device_sidechain(source, channel?)` † (a
  device's `input_routing_type` / `input_routing_channel` — key a bass
  compressor from the kick — matched by name or unique fragment, with the
  device's "S/C On" switch turned on). All verified by read-back; refused on
  a Live without the members.
- **Arrangement-native clips**: `create_arrangement_audio_clip(track, path,
  start_time)` † (Live 12's `Track.create_audio_clip(path, time)`) and
  `create_arrangement_midi_clip(track, start_time, length, notes?)` †
  (`Track.create_midi_clip`, notes written through the existing arrangement
  note path). No Session slot, no stamp, no delete. A start inside (audio)
  or a region overlapping (MIDI) an existing clip is refused unless
  `allow_overlap=true`, and whatever Live then cropped or replaced is
  reported.

- **Sound needs authorization — on by default.** `start_playback`,
  `fire_clip` and `set_track_arm(armed=true)` are refused, before anything
  is sent, unless the call passes `authorized=true`; disarming and
  `stop_playback` never are. The rule "never make sound without the user's
  OK" used to live only in agent prompts. `ABLETON_MCP_REQUIRE_AUDIO_AUTH=0`
  turns the gate off. (The audible commands carry `audible=True` in the
  registry; the check is in `AbletonService._send`, ahead of the lock.)
- `get_mix_state` † (Remote Script **1.25.0**): the mixer in a few KB — per
  track, return and the main track: volume and pan as Live displays them,
  mute, solo, arm, every send as displayed, output routing, and device names
  with "(off)" marks. Most snapshot calls were mixer checks.
- `get_session_snapshot` takes `max_chars` (default **40000**; 0 = no
  limit). Over budget, whole tracks are dropped from the end and listed in
  `omitted_tracks`, and `continuation` holds the `tracks` argument that
  fetches exactly them — instead of a reply the client cuts off mid-JSON.

- **Rendering** (Remote Script **1.24.0**). `export_audio` † drives Live's
  own *File › Export Audio/Video* — the exact offline render, no playback —
  over Accessibility, with consent (`allow_ui_automation` or
  `ABLETON_MCP_ALLOW_UI=1`) and the Live lock. It refuses while the user is
  active (HID idle < 30 s), while transport plays or records, or with a
  dialog open; takes the range from Live (`last_event_time` + `tail_beats`,
  default 8) instead of whatever the dialog last held; finds every field by
  its on-screen label, sets it and reads it back (any mismatch cancels the
  dialog and returns the diff — stale 22050 Hz and Normalize can no longer
  slip through); presses Export by its button; fills the save panel's
  folder and verifies the name; never replaces a file unless `overwrite`;
  waits on the filesystem; then measures each file. `dry_run` sets and reads
  back everything, screenshots it to `evidence_dir`, and cancels.
  `bounce_track` † runs *Edit › Freeze Track / Bounce to New Track / Bounce
  Track in Place* on a track selected and verified through the API (two new
  script commands, `select_track` and `get_track_render_state`), confirms
  the result through Live (frozen flag, track count, the new clip's file)
  and measures the bounced file; bounce-in-place needs
  `confirm_destructive=true` and checkpoints the set first.
  `analyze_audio` † / `verify_audio` † measure any file offline with
  ffmpeg/ffprobe (`ABLETON_MCP_FFMPEG`, PATH or Homebrew): duration, rate,
  bit depth, LUFS-I, LRA, true peak, sample peak, RMS, clipped samples, L/R
  balance and correlation, leading/trailing silence, silence %, and a cut
  tail (still above -60 dBFS in the last 50 ms); `verify_audio` adds
  pass/fail limits. Full-rate decoding reuses the warp-vision decoder
  (`audio_analysis.read_frames`), so there is one decoder, not two.

- **Rack chains** (Remote Script **1.23.0**): `get_rack_chains` †,
  `insert_rack_chain` †, `insert_chain_device` †, `set_chain_mixer` †.
  Read a rack's chains (name, volume/pan as value and display, mute, solo,
  devices with on/off state, a Drum Rack chain's `in_note`); add a chain
  (Live 12's `RackDevice.insert_chain`), put a native device into one chain
  at a position (`Chain.insert_device`), and set a chain's volume/pan (raw
  or display strings), mute, solo, name, and a Drum Rack pad's receiving
  note. Parallel amp blends and silent pads with the wrong `in_note` no
  longer need the UI or the set's XML. Each write is verified by reading
  the count or value back; on a Live without the members it refuses and
  changes nothing.

- `load_device_from_path(track_index, path)` † (Remote Script **1.22.0**):
  load a preset *file* — `.adv` / `.adg` / `.vstpreset` / `.aupreset` — from
  the User Library or a folder in Live's Places by absolute path. Live's
  BrowserItem carries no file path, so the path is mapped onto the browser's
  own tree (after `.../User Library/`, or after a Places folder's name),
  matching names with or without the extension. A miss names the level it
  stopped at and what was there (the usual cause: a file saved moments ago
  that Live has not indexed). A `.adv` saved from a hosted plugin holds its
  whole state — capture/model files, IRs, patches — so this is how a
  configured amp sim or synth comes back without its plugin window.
- `get_plugin_state(source_path, track?, plugin?)` †: offline decoder for
  hosted plugins' saved state in a `.als` / `.adv` / `.adg` (hex
  `ProcessorState` / `ControllerState` / `Buffer`) or a VST3 `.vstpreset`
  (`Comp` / `Cont` chunks): per plugin, its track, name, chain ordinal,
  state size and sha256, absolute file paths found in it, and readable
  strings (ASCII and UTF-16). Verifies which model a plugin really holds
  when its window still reads "Load model...". Never talks to Live, never
  writes. There is deliberately no `load_plugin_state`: Live's API has no
  way to write a running plugin's state (re-probed on 12.4.7); save the
  configured plugin as a preset and load that instead.

- **Display-string parameter values** (Remote Script **1.21.0** — re-run the
  installer): `set_device_parameter` and `set_device_parameters` accept what
  the device *shows* — `"3.6 s"`, `"28 ms"`, `"-1.5 dB"`, `"35 %"`,
  `"2.5 kHz"`, `"-inf dB"`, or a picker's label — as well as raw numbers.
  The script searches the parameter's own `str_for_value` rendering (labels
  for pickers; 64 samples then bisection otherwise, units folded so "800 ms"
  equals "0.8 s"), clamps out-of-range targets to the nearer end, and
  reports `requested_display` / `resolved_by` beside the value it landed on.
  A bare number, even as text, is still a raw value. Agents had been
  converting log-scaled decay times and curved gains by hand, and guessing.
- `load_devices(track_index, uris)` †: load a whole chain in one call, in
  order, stopping at the first failure; returns what landed, what failed
  and the track's final device list with indices.
- `get_track_info` and `get_session_snapshot` report `is_active` and
  `device_on` per device (None where unreadable), so a bypassed device —
  or one inside a bypassed rack — is visible without get_device_parameters.

- **`save_set` † now actually saves on Live 12** (macOS, with consent), and
  `checkpoint_set` † keeps labelled copies. Live's Python API has no save
  (every probe on 12.4.x returned an empty list), so after the API probe
  `save_set(allow_ui_automation=true)` clicks Live's own *File › Save Live
  Set* by title over Accessibility (never by position, never *Save As*),
  then reports a save only once Live's title has lost its unsaved `*` and
  the `.als` mtime has advanced. The file is the caller's `set_path` or the
  one Spotlight finds for the title's set name (ambiguity is reported, not
  guessed). An Untitled set's Save panel is cancelled and refused. Focus is
  handed back to the previous app. Consent is per call or server-wide with
  **`ABLETON_MCP_ALLOW_UI=1`**; without it nothing but the API is tried.
  `checkpoint_set(label)` saves, copies the set to
  `<set folder>/Checkpoints/<YYYYmmdd-HHMMSS>-<label>.als` and verifies the
  copy's sha256. `release_live_lock(save_first=true)` saves and releases in
  one call; `set_path` is optional there now (looked up from the title).
  Server-side only — no Remote Script change.

- **The shared Live lock — state-changing tools now need it by default.**
  `acquire_live_lock(holder, task)` †, `release_live_lock(holder, set_path)` †
  and `get_live_status` †. Every state-changing tool is checked against a
  lock file before anything is sent: held by someone else → refused, naming
  the holder and their task; held by nobody → refused until this server
  acquires it, **unless `ABLETON_MCP_REQUIRE_LOCK=0`** (the opt-out for
  single-user setups; someone else's lock is still honoured). The lock never
  expires; release succeeds only when the saved `.als` is at least as new as
  the holder's last edit and, on macOS, Live's window title shows that set
  without an unsaved `*` or an open Save/Export dialog. `stop_playback` is
  never refused, and `save_set` is checked but does not count as an edit.
  The file (`ABLETON_MCP_LOCK_FILE`, default `~/.ableton-mcp/live.lock`,
  JSON with an flock'd `.mutex`) is shared with command-line helpers; taking
  it there and calling `acquire_live_lock` with the same name adopts it.
  `get_live_status` is a one-call preflight: reachable, Live and script
  versions, transport and record state, soloed/muted/armed tracks,
  `last_event_time`, the lock, and on macOS — read passively from window
  titles and IOKit, never by driving the UI — the set name, unsaved marker,
  any modal dialog, and seconds since the user's last input.
  Remote Script **1.19.0** (new `get_live_status` command).
  **Re-run `ableton-mcp-install-script` and restart Live.**

- `get_song_scale` † and `set_song_scale` †: read and set the Live Set's
  key and scale (Live 12+) — `root_note` (0–11, 0 = C), `scale_name`, the
  Scale Mode toggle `scale_mode`, and the read-only `scale_intervals` where
  Live reports them — so generated parts can follow the key the Set is in.
  `root_note` also takes a note name (`"C"`, `"F#"`, `"Bb"`, `"c#"`;
  case-insensitive, sharps or flats, enharmonics wrap), coerced before the
  wire; anything else is refused with nothing sent. Only the fields given
  are written, all validation happens before the first write, and the
  scale name is written first so a name Live rejects leaves the key alone.
  Live's API offers no list of valid scale names, so the reply reports what
  Live reads back and whether it matched (`verified`, case-insensitive)
  rather than trusting the write. On a Live without `Song.root_note` /
  `Song.scale_name` both tools refuse with a "needs Live 12" message; a
  build without `Song.scale_mode` reports it as `null` and refuses only a
  Scale Mode write. `get_session_snapshot`'s `session` block now carries
  `root_note`, `scale_name` and `scale_mode` too (`null` without the API);
  the keys are additive, so the snapshot schema stays
  `ableton_mcp_snapshot_v3`. Idea from upstream PR #146 (MIT), written
  fresh.
- `set_track_solo` †: solo or unsolo a regular or return track, for
  auditioning a part in context without touching mutes or faders. Solos
  accumulate, and Live's Exclusive Solo preference may clear others, so
  the reply reads back and names *every* soloed track after the change,
  plus `exclusive_solo` as Live reports it (`null` on a build that does not
  expose `Song.exclusive_solo`). The master has no solo button in Live and
  is refused before anything is sent (`"master"` / `"main"`). Idea from
  upstream PRs #99 and #84 (MIT), written fresh on the fork's track
  resolver and name guard.

- **Warp vision** — an agent can now *see* an audio clip against its
  warping and fix it precisely, then verify. Four tools (all †):
  - `analyze_clip_audio`: reads the clip's audio file on this machine and
    returns an image — waveform with the song's bars/beats as the current
    warp maps them onto the audio, the warp markers, detected transients,
    a low/mid/high energy strip and a log-frequency spectrogram — plus
    JSON for text-only models: transients (file time, song beat, ms off the
    nearest 16th, strength, dominant band), a source tempo/beat-grid
    estimate, per-band energy over time, and `grid_alignment` (median/p90
    ms by which strong hits miss the grid — the before/after number).
  - `suggest_warp_markers`: proposes add/move/remove edits that put hits
    on the grid — `transients` (local snap), `rewarp` (left-to-right beat
    tracking for drifting takes or a wrong Auto-Warp tempo), `tempo`
    (constant-tempo anchors). Dry run by default with a before/after image;
    `apply=True` sends the batch and reports alignment MEASURED from Live's
    read-back.
  - `edit_clip_warp_markers`: batch add/move/remove in one main-thread
    task, then reads the markers back and reports `verified` per batch
    (Live's edit calls are "if possible": a refused edit can be silent).
  - `get_clip_warp_markers`: the raw markers with file path, sample rate,
    start/end/loop markers, warp mode, song tempo and meter.
  The two audio tools need the new optional extra: `uv pip install
  'ableton-mcp[analysis]'` (numpy, soundfile, Pillow — all GPL-compatible,
  no network clients; librosa was rejected for pulling in `pooch`, an HTTP
  downloader). Warp-marker editing needs Live 11.0.5 or later.
- `get_plugin_presets` † and `set_plugin_preset` †: list and select the
  factory presets of a hosted VST/VST3/AU plugin. Live exposes *no*
  parameters of an unconfigured VST3 or AU through its API — verified on
  12.4.3, `get_device_parameters` on TAL-NoiseMaker, Dexed and the u-he
  synths shows only "Device On" — so `set_device_parameter` cannot reach
  them at all. What the LOM does expose on those devices is
  `Live.PluginDevice.PluginDevice.presets` (a tuple of names, empty for a
  plugin that publishes none) and the writable `selected_preset_index`,
  which makes preset selection the one way to change what such a plugin
  sounds like from here. Names match exactly first (case-insensitively),
  then as a fragment — but a fragment that fits several presets is refused
  with the candidates listed rather than resolved to the first; an index
  out of range, a device that is not a plugin, and a plugin with an empty
  bank are refused too, with nothing changed. The reply reports what Live
  reads back and whether it matched (`verified`), without retrying: a
  same-tick read can lag the write, and an in-tick re-read returns the
  same stale value by construction.
- `set_clip_warp` †: turn an audio clip's warping on or off (and optionally
  set its warp mode). This is the fix for a problem that has no workaround
  from the API otherwise: Live's "Auto-Warp Long Samples" guesses a source
  tempo for every imported file, and on material without clear transients it
  guesses wrong — verified on 12.4.3, six byte-identical-length stems from
  one session each landed on a *different* guess (117.9, ~240 ×4, and 151
  BPM), so a multitrack that was perfectly aligned on disk played badly out
  of sync, with the ~240 BPM guesses running at double speed. Switching
  warping off plays each clip at its recorded rate, which is what keeps
  aligned stems aligned. The response reports the clip's resulting length,
  so a caller can confirm a set of stems now agree with each other rather
  than trusting the write.
- `duplicate_track` †: duplicate a whole track — devices, mixer, routing and
  every clip — the way Ctrl-D does on a track header. Double-tracking a part
  previously meant creating a track, re-routing it to the same bus,
  re-panning it and reloading each device by hand. The copy lands
  immediately below the source, so the renumbering is knowable in advance
  (the copy is `track_index + 1`) instead of needing a re-read. Live names
  the copy itself; follow with `set_track_name`.
- `set_device_parameters` †: set many parameters on one device in a single
  call, as a `{name: value}` map. A device is normally a handful of values
  that only make sense together, and sending them one at a time cost 257
  round-trips in one session. Each value is clamped into its own range
  exactly as the singular tool clamps, and one key that matches nothing
  comes back as its own not-found row rather than losing the writes around
  it. It buys round-trips, not freshness — read-back values are exactly as
  trustworthy as the singular tool's.
- `delete_locator` †: delete a locator by name or beat. Live's API has no
  "delete this cue" call, only a toggle at the playhead, so this parks the
  playhead on the locator, verifies it landed, and only then toggles — and
  refuses without toggling if the transport never settles. That refusal is
  the safety: toggling from the wrong position does not fail, it *creates* a
  stray locator there and leaves the intended one in place.
- `duplicate_to_arrangement` takes `destination_times`, placing the same
  source clip at a whole run of beats in one call. It was the single most
  used tool of the session — 465 calls and 63.5 minutes, arriving in runs of
  up to 76 identical calls with no reasoning between them. The run is capped
  (128 placements) because it all happens inside one main-thread task, where
  Live's UI and audio housekeeping also live.
- `add_notes_to_clip`, `clear_notes_from_clip` and `get_clip_notes` take
  `arrangement=True`, so notes can be edited in place on an arrangement clip
  instead of editing the Session clip and re-stamping every position it was
  placed at. This does not end the re-stamp treadmill — 15 placements are 15
  independent clips — but it removes the reason to stamp over live material
  for a one-bar fix.
- `delete_arrangement_clip` addresses clips by `start_time`, and
  `start_times` deletes a whole set in one all-or-nothing pass. `clip_index`
  is a positional ordinal that renumbers on every delete, which is what
  forced 63 `get_arrangement_clips` calls and a hand-maintained
  highest-index-first ritual across 19 delete runs. Live permits no overlaps
  on a track, so a start time is a genuine unique key; near-misses are
  listed in the error, since a hand-dragged clip can sit on any fraction.
- `create_audio_clip` takes `expected_beats` and cross-checks the imported
  clip's length against it.
- Every tool that writes to a track takes an optional `expect_track_name`:
  give it the name you believe is at `track_index` and the write is refused,
  naming what is actually there, if the two disagree. The out-of-range
  errors were the lucky half; the silent half is an in-range-but-wrong index
  after a mid-session delete, which is how an Auto Pan meant for the GUITARS
  bus landed on BASS with a success message.
- Three arguments accept a second spelling, because they get guessed by the
  name a neighbouring tool uses: `duplicate_to_arrangement`'s
  `destination_time` as `arrangement_time` or `time`,
  `set_device_parameter`'s `parameter` as `parameter_name`, and
  `create_audio_clip`'s `path` as `file_path`. `destination_time` was
  guessed wrong seven times across four days and never stuck. Passing two
  spellings with different values is refused rather than resolved.

### Fixed (user-facing)

- **`export_audio`'s dialog handling on Live 12.4.7 + macOS 26.** The
  dialog's AX shape differs from the fixture the driver was written
  against: toggles are nameless AXButtons reading On/Off, the
  bar/beat/sixteenth boxes are AXSliders that ignore written values
  (stepped instead), the MP3 toggle matches a label's ' (' suffix,
  Export/Cancel are the unnamed bottom pair, pop-up menus open as a
  separate nameless window of AXMenuItems, and the macOS 26 Save panel
  keeps its fields in splitter group 1 and opens a moment after the
  press (Go-to-folder resets the name). Verified against real Live
  12.4.7; smoke_live now also takes/adopts the shared Live lock and
  gates playback on --allow-audio and menu/dialog steps on --allow-ui.

- **A clean save stayed unreleasable after transport-only work.** Commands
  that take over Live but leave nothing to save - the transport
  (`start_playback`, `stop_clip`, `fire_clip`,
  `set_current_song_time`, `back_to_arrangement`, `jump_to_locator`),
  the view switch (`switch_to_arrangement_view`) and `select_track` -
  now meet the shared Live lock as `check`, not `write`: they still
  refuse it while someone else holds it, but no longer stamp
  `last_edit_ts`. Cmd-S on an unchanged set does not touch the file, so
  a stamp from them made a set saved after the last real edit impossible
  to release (seen after an export, 10-28). `save_set` keeps its
`check`; `stop_playback` stays exempt.

<<<<<<< HEAD
=======
- **A clean save stayed unreleasable after transport-only work.** Commands
  that take over Live but leave nothing to save - the transport
  (`start_playback`, `stop_clip`, `fire_clip`,
  `set_current_song_time`, `back_to_arrangement`, `jump_to_locator`),
  the view switch (`switch_to_arrangement_view`) and `select_track` -
  now meet the shared Live lock as `check`, not `write`: they still
  refuse it while someone else holds it, but no longer stamp
  `last_edit_ts`. Cmd-S on an unchanged set does not touch the file, so
  a stamp from them made a set saved after the last real edit impossible
  to release (seen after an export, 10-28). `save_set` keeps its
`check`; `stop_playback` stays exempt.
>>>>>>> origin/dev

- **`get_session_snapshot` after opening another set** (Remote Script
  **1.20.0** — re-run the installer). The script cached Live's Song once, at
  load; after a set switch unscoped snapshots failed with Boost.Python's
  "None.None(Song) did not match C++ signature: None(TPyHandle<ASong>)".
  `self._song` now re-resolves through `ControlSurface.song()` on every
  access. Independently, the snapshot reads each section (session, each
  track, scenes, returns, master, cue points) in isolation: a failure there
  becomes a `section_errors` entry (`{"section": "tracks[3]", "error":
  "TypeError: ..."}`) and a stub for that track instead of failing the whole
  call. Transport errors ("Could not connect to Ableton", "Connection to
  Ableton lost") now end with a hint to call `get_live_status`, and that the
  next call reconnects by itself.

- `get_track_info` on a group track, and `get_session_snapshot` on any Set
  that contains one, no longer fail with "Master and Return Tracks have no
  'Arm' state!". Live raises on *reading* `arm` for group tracks too, and
  the snapshot's `getattr(track, "arm", False)` never guarded it (the
  attribute exists; reading it is what throws). Both now report
  `arm: false` for any track that cannot be armed. Ported from upstream
  `4b707a2` (#128, fixing upstream #126); the passive arm listener that
  upstream also patched does not exist here.
- `get_browser_tree` now shows what is in each category: one level of
  children (up to 64 per folder, deeper folders and longer lists marked
  `[...]`) and a real folder count. Before, the script never recursed, so
  every category came back empty and the header always read "showing 0
  folders". Ported from upstream `7e90cd6`.
- The Remote Script's socket no longer desynchronises when a non-ASCII
  character (an accented clip or track name, an emoji) straddles two
  `recv()` chunks: it buffers raw bytes and decodes only a complete
  command. Previously the split raised, an error frame went into a stream
  the client was still reading a real reply from, and every later reply on
  that connection was out of step. An incomplete command is now capped at
  16 MB instead of buffering forever, and any framing error closes the
  connection (the server reconnects) instead of carrying on out of step.
  Ported from upstream `7e90cd6`; its `ABLETON_MCP_HOST` bind override was
  not taken, so the socket stays loopback-only.
- Long commands get the time they were budgeted: `create_audio_clip` (65 s)
  and `duplicate_session_clip_to_arrangement` (35 s) were silently cut off
  at 15 s, because the receive loop reset every exchange's socket timeout
  to the 15 s default after the per-command one had been set. A slow audio
  import was reported as a failure while Live was still doing it. Other
  commands keep the 15 s they always effectively had. A failed connect
  now closes its socket instead of leaking it. Server-side only. Ported
  from upstream PR #139 (open), rewritten for this fork's transport.
- A state-changing command that times out waiting for Live's main thread
  no longer runs later anyway. Before, the task stayed scheduled and could
  land on a later tick — a `delete_track` or `add_notes_to_clip` the caller
  had been told failed, editing the set behind its back. Now a task that
  had not started is cancelled and the error says "the command was not
  executed and will not be"; one that had started says its outcome is
  unknown and to read the state back before retrying. From upstream PR
  #140 (open); only this part was taken (see Internal).
- Routine refusals (bad index, missing clip, name guard) are now logged to
  Live's `Log.txt` on one line without a traceback, so real faults stand
  out. Faults, including Boost argument errors from the Live API, still log
  their traceback. From upstream PR #141 (open), adapted: its
  `set_device_parameter` range check was not taken, since this fork's
  handler already clamps into range.
- The device and mixer tools now say in their own descriptions that
  `track_type="master"` reaches the master track (`track_index` ignored),
  so the model knows it can put a limiter on the master bus. The Remote
  Script has resolved `"master"` / `"main"` all along; only the
  descriptions said "regular or return". This covers what upstream PR
  #129 (open, `track_index=-1` sentinel) adds, without a new convention.

### Changed (user-facing)

- **`duplicate_to_arrangement` and `duplicate_arrangement_clip` now REFUSE a
  stamp that would leave a looping clip re-phased.** This is the correctness
  fix the release is built around. Live crops whatever an arrangement stamp
  lands on; cropping a clip's tail is harmless, but cropping its *head* (or
  splitting it) leaves a right-hand survivor that still carries the clip's
  own `loop_start`, so from that beat on it replays the loop from the top.
  On a looping MIDI clip that is silent corruption that sounds plausible:
  three hat stamps scrambled bars 60-68, 156-164 and 204-208 in one session
  and invented a crash nobody played, caught only by diffing a snapshot days
  later. The refusal names the victim and the two safe shapes (end the stamp
  at the clip's end, or cover it exactly); `allow_loop_phase_reset=true`
  stamps anyway and reports which clips were re-phased, so taking the
  override is on the record. Batching makes the guard more load-bearing, not
  less — an unguarded run corrupts up to 128 positions per call instead of
  one. Some calls that previously succeeded will now be refused; that is the
  point of the change.
- `create_audio_clip` imports with **warping OFF** and reports it. This is
  the same six-stems problem `set_clip_warp` fixes, moved to where the
  damage happens: `set_clip_warp` cannot reach the clip until the import has
  already been reported as fine, and the tool reported success with a
  plausible-looking beat length either way.
- `add_notes_to_clip` reports what Live actually accepted — `{requested,
  added, clip_note_count}`, counted before and after the write — instead of
  printing back the length of the caller's own list. It previously said
  "Added 262 notes" whether Live took 262 or none. Pass `expect_count` to
  have a short list refused rather than silently accepted; that is the
  load-bearing half, because a truncated command never executes at all and
  the loss happens above the server where a before/after delta stays silent.
- `delete_device` verifies. It used to return `len(track.devices)` with no
  before-count, so a delete that did nothing still printed a plausible
  "deleted X; N devices remain" — and a highest-index-first sweep built on
  that lie then removed the wrong devices. It now compares the count and the
  device names before and after and returns `remaining_devices` with names,
  so a sweep can re-anchor on names instead of ordinals that renumber under
  it. Deliberately **no retry**: if the read was merely stale rather than
  the delete having failed, a blind second delete removes the *next* device
  and destroys a chain that was dialled in by hand.
- `get_session_snapshot` is scopeable and much smaller by default. 21 of 24
  calls in one session blew the output cap even with both existing flags
  off, then cost 30 follow-up shell calls to parse dump files, and every
  real use wanted one section of a handful of tracks. Warp markers, rack
  chains and empty slots are now behind `include_warp_markers`,
  `include_rack_chains` and `include_empty_slots`, all **default off**
  (warp markers were previously emitted gated by neither flag, and a drum
  rack's chains alone ran ~9.5 KB), and a `tracks` filter takes names or
  indices. This is a deliberate payload break: the advertised snapshot
  schema goes from v2 to **v3**.
- Every modifying result echoes the track name it actually resolved, so a
  caller who did not pass `expect_track_name` can still diff afterwards.

### Internal

- Remote Script **1.17.0 → 1.18.0**, with `EXPECTED_REMOTE_SCRIPT_VERSION`
  moved in the same commit, for the commands added after 1.17.0
  (`set_track_solo`, `get_song_scale`, `set_song_scale`). 1.17.0 already
  carried the dispatcher and framing ports (safe arm reads, browser tree,
  byte-safe framing, cancel-on-timeout, quiet refusals); 1.18.0 changes no
  existing command. No `min_script_version` floors: all three names are new,
  so a 1.17.0 script never advertises them and the capability gate answers
  with the installer message.
- `set_track_solo` adds one advertised, gated, main-thread command with no
  `min_script_version` floor: an older script never advertises the name,
  so the capability gate already answers with the installer message.
  Tool surface 57 → 58 (on top of warp vision's four: 61 → 62);
  `tests/data/tool_names.txt`, the pinned counts, `PR5_SCRIPT_CAPABILITIES`
  (50 → 51) and `GATED_POST_1_7_0` updated. `FakeLiveConfig` gained
  `exclusive_solo` (True / False / None = property absent) so both sides
  of the probe are tested.
- `get_song_scale` (read-only, socket thread) and `set_song_scale`
  (main thread) are advertised, gated rows with no version floor, on the
  same reasoning; still no `SCRIPT_VERSION` bump. Tool surface 58 → 60,
  `PR5_SCRIPT_CAPABILITIES` 51 → 53, `GATED_POST_1_7_0` and the pinned
  counts updated. `FakeSong` gained the Live 12 key/scale properties
  behind `FakeLiveConfig.song_scale_api` and `song_scale_mode_api` (their
  getters raise `AttributeError` when off, so `hasattr` is false as on an
  older Live), plus `unknown_scale_name` ("ignore" / "raise" / "accept")
  for what a write of a name Live may not know does.

- Remote Script 1.17.0 (`EXPECTED_REMOTE_SCRIPT_VERSION` moved with it):
  `get_clip_warp_markers` (read-only) and `edit_clip_warp_markers`
  (main-thread, ≤256 edits, validated before any runs, verified by
  read-back) — both new names, so gated without a version floor. Markers
  are built as `Live.Clip.WarpMarker(beat_time=, sample_time=)`, the only
  form the Python API takes (the dict is Max for Live's wrapper); move and
  remove resolve the caller's beat to the exact one Live holds (1e-3 beat
  tolerance). The fake LOM models the documented rules (monotonic in beat
  and sample time, inside the file, 5–999 BPM segments) and two toggles:
  `warp_marker_edit_api` (Live < 11.0.5) and `warp_edit_lands` (the silent
  "if possible" no-op). New `audio_analysis.py` / `warp_vision.py` hold the
  server-side analysis; tests run it on synthetic audio with sample-exact
  ground truth (clicks, drifting kick/hat takes, band splits, a log sweep)
  and drive the whole analyze → dry run → apply → verify loop through a
  real FastMCP session onto the real Remote Script. Image tools are
  annotated `Any` so FastMCP leaves their text+image content unstructured.
  Tool surface 57 → 61; goldens: four new files, and
  get_remote_script_info.json / set_track_volume.json re-recorded for the
  version string only.
- `tests/test_no_private_names.py`: a guardrail that scans every tracked
  file for private project details (song titles, collaborator names, track
  names from real sets, personal paths). The denylist itself is never
  committed: it is read from `$ABLETON_MCP_PRIVATE_DENYLIST` or the
  gitignored `tests/.private-names.txt`, and the scan skips where neither
  exists (CI). Failures name the file and line, never the matched entry.
  Example names in docs and goldens were replaced with generic ones.

- `scripts/push_notes.py`: a standard-library-only CLI that pushes MIDI
  notes from JSON files — a note list, or a manifest of files → track/clip
  targets — straight to the Remote Script socket, in chunks of at most 110
  notes, every chunk carrying `expect_count` and every command
  `expect_track_name`, printing what Live reported for each chunk. It
  exists because a whole-song drum part sent through the MCP round trip
  passes through the model's context once per tool call; 2,513 notes went
  in this way over ~45 calls in a few seconds, with no tokens spent. Safe
  beside a running server: the script accepts several clients and
  serialises writes on Live's main thread. Every note is validated before
  anything is sent, the run stops at the first refusal and names the
  half-written clip, and `--dry-run` connects to nothing.
  `tests/test_push_notes.py` drives it against a loopback peer that behaves
  like the script's handlers — both guards' refusals, replies split inside
  a multi-byte character, a reply that never comes, a connection closed
  mid-reply, and the pre-1.15.0 echoed count. The no-telemetry guardrail
  now scans `scripts/` too, with `push_notes.py` the one file there allowed
  to import `socket`.
- Remote Script **1.15.0 → 1.16.0** for the plugin-preset pair, with
  `EXPECTED_REMOTE_SCRIPT_VERSION` moved in the same commit. Neither new
  registry row carries a `min_script_version` floor: a brand-new command is
  never advertised by an older script, so the ordinary capability gate
  already answers with the installer message, and a floor would only cost
  a guardrail its exactness.
- Tool surface 55 → 57; `tests/data/tool_names.txt`, the pinned counts in
  four test modules, `PR5_SCRIPT_CAPABILITIES` (48 → 50) and
  `GATED_POST_1_7_0` updated to match.
- `tests/fake_ableton` gained `FakePluginDevice`: a hosted plugin whose
  only parameter is "Device On" (as Live really shows it) and whose
  `presets` / `selected_preset_index` are the interface — the only fake
  device carrying either attribute, which is what the script's `hasattr`
  probe branches on. `preset_select_lands=False` models the
  write-returns-but-reads-back-unchanged case the `verified` flag exists
  for.
- Goldens re-recorded: six new cases for the pair plus their error cases,
  and the `get_remote_script_info` cases that embed the expected script
  version. New behaviour tests cover listing (plugin, native device, AU and
  probe detection, empty bank), selection by index, exact name and unique
  fragment, and every refusal — ambiguous fragment, duplicated name,
  unknown name, out-of-range index, non-plugin, empty bank, nothing asked
  for, disagreeing index and name, `expect_track_name` mismatch — plus the
  unverified read-back and a return-track selection.
- Remote Script **1.14.0 → 1.15.0**, with `EXPECTED_REMOTE_SCRIPT_VERSION`
  moved in the same commit as always.
- Nine existing registry rows gained `min_script_version="1.15.0"`. They are
  all one species: a command whose *name* an older script advertises while
  its handler either cannot accept the new keyword (a raw `TypeError` out of
  the dispatch's `**params` inside Live) or serves the old, wrong behaviour
  under it — `add_notes_to_clip`'s echoed count, `create_audio_clip`'s
  warped import, `delete_device`'s unverified report, and the two stamps'
  missing loop-phase guard. Each entry costs an out-of-date user the command
  until they re-run the installer, which is the trade the friendly message
  exists to make.
- `add_notes_to_clip` is now `gated=True` despite sitting in the
  `LEGACY_CAPABILITIES` floor. The floor makes the capability half of the
  gate a no-op, but only a gated row consults `min_script_version`, and this
  is the one row that needs the floor without needing the capability check.
  `test_cross_half_contract` now permits exactly that combination and still
  rejects a floor row gated *without* a version floor, which would buy
  nothing.
- `duplicate_session_clip_to_arrangement`'s socket timeout is 35 s against
  the script's 30 s queue timeout, keeping the ≥ 5 s headroom the cross-half
  contract test enforces — a batched run of placements is one main-thread
  task and outlasts the default budget.
- Tool surface 52 → 55; `tests/data/tool_names.txt`, the pinned counts in
  four test modules, `PR5_SCRIPT_CAPABILITIES` (45 → 48 advertised commands)
  and `GATED_POST_1_7_0` all updated to match.
- Goldens re-recorded (169 cases across 55 tools), including new cases for
  the whole-call loop-phase refusal and its override on *both* stamps, the
  partial-success batch, and the plural device-parameter set. The refusal
  text is frozen deliberately: it names the victim, the beat the survivor
  would re-phase from and the two safe shapes, and a shortened version would
  be advice the model cannot act on.
- `tests/fake_ableton` gained a `device_delete_lands` toggle, modelling a
  `Track.delete_device` call that returns normally having changed nothing —
  the exact condition `delete_device`'s before/after check exists for, and
  indistinguishable from a stale read inside one tick.
- New behaviour tests cover the loop-phase guard from both sides (refused,
  overridden, and the two shapes that must *not* be refused — a non-looping
  victim and a tail-only crop), the batch partial-success path including
  that a refusal mid-run does not abort the placements after it, and
  `delete_device`'s verify in both outcomes.

## [1.7.0] - 2026-08-28

Ships Remote Script **1.13.0**. **Re-run `ableton-mcp-install-script` and
restart Live after upgrading** — the trim rework and the locator race fix
live in the script half, and the server refuses `trim_arrangement_clip`
against older scripts (their implementation could never trim on the Live
builds we can verify).

### Fixed (user-facing)

- **A clean save stayed unreleasable after transport-only work.** Commands
  that take over Live but leave nothing to save - the transport
  (`start_playback`, `stop_clip`, `fire_clip`,
  `set_current_song_time`, `back_to_arrangement`, `jump_to_locator`),
  the view switch (`switch_to_arrangement_view`) and `select_track` -
  now meet the shared Live lock as `check`, not `write`: they still
  refuse it while someone else holds it, but no longer stamp
  `last_edit_ts`. Cmd-S on an unchanged set does not touch the file, so
  a stamp from them made a set saved after the last real edit impossible
  to release (seen after an export, 10-28). `save_set` keeps its
`check`; `stop_playback` stays exempt.

- `create_locator` no longer misfires after a transport stop. It toggles
  Live's cue at the playhead, but a `current_song_time` write can be
  swallowed while the transport settles (verified on 12.4.3) — the toggle
  then fired at the OLD playhead position, leaving a stray auto-named
  locator there and reporting failure. The script now verifies the
  playhead landed (with one retry) before toggling, and refuses without
  toggling when it never settles — a wrong-position toggle is the
  corruption, so the refusal is the safety.

### Changed (user-facing)

- `trim_arrangement_clip` † actually trims now. The 1.9.0–1.11.0
  marker-write approach was verified inert on real Live 12.4.3: writing
  `Clip.start_marker`/`end_marker` never moves an Arrangement clip's
  footprint, so the readback guard refused every trim — safely, but
  uselessly. The rework trims the way the UI does, verified end to end on
  12.4.3: stamp a temporary silent clip over the region to remove (Live
  permanently crops whatever a stamp covers), delete the stamp, verify the
  take's new edge by readback. What follows from the mechanism:
  - The eraser's stamped footprint is measured in empty timeline space
    before the take is touched, so a build where the stamp machinery
    misbehaves gets an honest refusal with the take untouched — and an
    edge whose stamp cannot be placed safely (a micro-trim right against a
    neighbouring clip) is refused the same way.
  - Needs one empty Session slot on the track (the eraser is a temporary
    Session clip on the same track, since stamps only crop their own
    track) and Live 11+; trimming audio takes needs Live 12.0.5+
    (`ClipSlot.create_audio_clip`) and writes a tiny generated silent WAV
    to the system temp directory.
  - Looping clips are no longer refused — the crop is Live's own overlap
    handling, which treats them exactly as the UI does.
  - The trim is real editing, not a marker move: the audio file on disk is
    still never touched, but un-trimming is Edit > Undo in Live rather
    than dragging the edge back out.

### Internal

- `trim_arrangement_clip`'s registry row carries
  `min_script_version="1.12.0"`, so a 1.9.0–1.11.0 script (which
  advertises the command but serves the inert implementation) gets the
  friendly re-run-the-installer message instead of an endless refusal
  loop.
- A `fade_arrangement_clip` command (clip fades as clip-scoped volume
  automation) was built and then killed in review before release: the LOM
  documents `Clip.automation_envelope` as returning None for Arrangement
  clips on every Live generation, and arrangement automation lanes are not
  writable from the Python API — so programmatic clip fades are not
  possible, and shipping a command that can never succeed helps nobody.
  Recorded here so the next attempt starts from that fact.
- The fake Song's `current_song_time` is now a property with a
  `transport_write_lag` toggle, so the post-stop settle race has test
  coverage on both branches.
- `tests/fake_ableton` now models Live's arrangement overlap physics —
  `Track.duplicate_clip_to_arrangement` crops or splits whatever the copy
  lands on and keeps the clip list in start-time order — and derives an
  imported audio clip's length from the real WAV on disk. The
  `marker_trim` config toggle is gone: marker writes never move
  footprints, matching verified Live behavior.
- `scripts/smoke_live.py`'s arrangement-editing step now proves the
  overlap-stamp crop on real Live: trimmed edges are verified by
  `get_arrangement_clips` readback and the temporary eraser Session clip
  must be gone afterwards.

## [1.6.0] - 2026-08-27

Ships Remote Script **1.11.0**. **Re-run `ableton-mcp-install-script` and
restart Live after upgrading** — all four new commands live in the script
half.

### Added (user-facing)

Arrangement take editing — the cleanup and comping pass after recording:

- `trim_arrangement_clip` †: pull an Arrangement clip's edges inward, in
  beats — the fix for a take that overhangs its section. Live documents no
  arrangement resize, so the script works through the clip's content
  markers and then **verifies the edge actually moved by readback**; on a
  build where markers behave differently it restores them and refuses that
  edge honestly instead of mis-trimming a take. Unwarped audio (markers in
  seconds) is converted at the current tempo; looping clips are refused.
- `delete_arrangement_clip` †: delete a clip from the Arrangement timeline
  (Live 11+ `Track.delete_clip`) — stray record fragments, scrapped takes.
  The audio file on disk is never touched.
- `move_arrangement_clip` †: move a clip to a new start position. Live has
  no true move — `Clip.position` is the clip's LOOP position, not its
  Arrangement placement — so this duplicates the clip to the destination,
  verifies the copy landed, then deletes the original. A destination
  overlapping the clip's own span is refused.
- `duplicate_arrangement_clip` †: copy an Arrangement clip elsewhere on its
  track — reuse an already-recorded take at another section, then trim the
  copy to fit. Same Live 11+ API the session duplicate uses.

Clip *fades* are not exposed by Live's API at all; Live's own "Create Fades
on Clip Edges" preference supplies the anti-click edges on trimmed takes.

### Changed (internal)

- Package author contact switched to the maintainer's personal address.

## [1.5.0] - 2026-08-27

Ships Remote Script **1.10.0**. **Re-run `ableton-mcp-install-script` and
restart Live after upgrading** — the new command lives in the script half.

### Added (user-facing)

- `jump_to_locator` †: jump the Arrangement to an existing locator by name
  or beat time via `CuePoint.jump()`. While the transport is stopped this is
  the API equivalent of clicking the locator in the scrub area — the only
  way to move Live's **start marker**, so play/record launch from that spot.
  `set_arrangement_time` cannot do this: verified on Live 12.4.3, it moves
  only the visible playhead and record still launches from the old marker.
  While playing, `jump_to_locator` relocates playback instead, and the
  response says explicitly whether the start marker moved.

## [1.4.0] - 2026-08-19

Ships Remote Script **1.8.0**. **Re-run `ableton-mcp-install-script` and
restart Live after upgrading** — the repairs below live in the script half.

### Added (user-facing)

- Installable builds without a local toolchain: every CI run uploads the
  wheel + sdist as an `ableton-mcp-dist` workflow artifact, and every `v*`
  tag gets a GitHub Release with the same build attached. See the README's
  "Installing from CI builds and releases" section.

### Fixed (user-facing)

- **A clean save stayed unreleasable after transport-only work.** Commands
  that take over Live but leave nothing to save - the transport
  (`start_playback`, `stop_clip`, `fire_clip`,
  `set_current_song_time`, `back_to_arrangement`, `jump_to_locator`),
  the view switch (`switch_to_arrangement_view`) and `select_track` -
  now meet the shared Live lock as `check`, not `write`: they still
  refuse it while someone else holds it, but no longer stamp
  `last_edit_ts`. Cmd-S on an unchanged set does not touch the file, so
  a stamp from them made a set saved after the last real edit impossible
  to release (seen after an export, 10-28). `save_set` keeps its
`check`; `stop_playback` stays exempt.

- `get_device_parameters` and `set_device_parameter` work again. They had
  been broken at runtime since the 2026-08 upstream merge: the merged Remote
  Script defined their handlers twice, Python silently kept the later,
  incompatible definitions, and every call raised a `TypeError` inside Live.
  Script 1.8.0 restores this fork's handlers (parameter by name or index,
  clamping, display values, `track_type` for return tracks) with upstream's
  old-value echo.
- The `ableton-mcp` console script starts again. `main()` had been deleted by
  the telemetry-removal commit's end-of-file sweep, which broke every README
  client-config snippet, the Docker `CMD`, and smithery.
- `delete_clip` reports the deleted clip's name again.
- The browser `load_*` tools no longer time out while Live is still loading a
  device: `load_browser_item` was missing from the modifying-command list and
  got the short read-only socket timeout. `set_arrangement_clip_name` moved
  to the modifying timeout for the same reason.
- `ableton_mcp.__version__` reports the real package version (it claimed
  0.1.0); it is now read from the installed distribution's metadata.

### Changed (user-facing)

- Friendly capability gating: a user whose installed Remote Script is too old
  for a tool now gets a clear "re-run `ableton-mcp-install-script`, then
  restart Ableton Live" message instead of a raw socket error. The repaired
  device-parameter pair is additionally gated on script version 1.8.0, since
  the broken 1.7.0 script *advertises* those commands.
- Transport hardening: reconnection happens under the send lock (concurrent
  tools can no longer race into duplicate sockets); the socket is dropped
  after any timeout, so a late reply can never be misread as the answer to
  the next command; and the version handshake re-runs after any reconnect,
  so restarting Live with a different script is noticed immediately.

### Internal

- CI now publishes its build as a workflow artifact, and a tag-triggered
  Release workflow (`.github/workflows/release.yml`) attaches the wheel and
  sdist to GitHub Releases — the publish step of the release checklist in
  `docs/UPSTREAM.md`.
- The server half restructured from the `MCP_Server/server.py` monolith into
  the `src/ableton_mcp` package (src layout, PEP 8 name): a dependency-
  injection composition root (`app.py` — no module globals, no import-time
  side effects) and MVC layering (`tools.py` controllers, `services.py` /
  `connection.py` / `commands.py` / `handshake.py` model, `presenters.py`
  view). The import path is now `ableton_mcp`; the console-script names,
  tool names, docstrings, response text, and wire protocol are unchanged
  except for the fixes above.
- One command-metadata registry (`commands.py`) replaces the three
  disagreeing lists that used to decide timeouts and gating.
- Remote Script: both `elif` dispatch ladders collapsed into a single
  literal `COMMANDS` table; `SCRIPT_CAPABILITIES` is derived from the
  table's advertise flags; duplicate methods, unreachable branches, and
  branches calling nonexistent methods removed.
- Renames: `AbletonMCP_Remote_Script/` → `remote_script/`; the bundled copy
  is `remote_script_init.py`, regenerated only via
  `ableton-mcp-install-script --sync-bundle` and held byte-identical to the
  canonical script by test.
- A test suite that runs entirely without Ableton or network (355 tests) and
  GitHub Actions CI: guardrails for every failure class the fork has actually
  shipped (duplicate definitions, dead entry points, bundle drift, cross-half
  contract drift, loopback reversion, telemetry vocabulary and egress
  imports, tool-surface and README snapshots), a golden characterization
  suite (121 recorded cases across all 46 tools), a mock Ableton
  (`tests/fake_ableton/`) that executes the real Remote Script's handlers
  against a fake LOM, transport tests over an in-process socket, and
  full-stack tests through a real FastMCP session.
- New docs: `docs/REFACTOR_PLAN.md` (the architecture plan), `docs/UPSTREAM.md`
  (the upstream-merge and release playbook); CLAUDE.md rewritten for the new
  layout.

## [1.3.8] — 2026-08

The fork's baseline. The version number was inherited from upstream in the
2026-08 merge; under it, this fork removed all telemetry and dataset
collection (no analytics, no persistent identifiers, no prompt or MIDI
upload, no passive listeners on the user's UI actions), kept the Remote
Script socket bound to `127.0.0.1`, dual-licensed fork changes
GPL-3.0-or-later / AGPL-3.0-or-later, and merged upstream's Remote Script
installer, version/capability handshake, `create_locator`, and
`clear_notes_from_clip`. Not published to PyPI — the `ableton-mcp` name
there is upstream's build, which has telemetry; install this fork from git.
