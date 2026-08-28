# Changelog

Notable changes to this fork. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are the
`pyproject.toml` package version, with the Remote Script's own version
(`SCRIPT_VERSION`) called out where it changes — the two halves upgrade
separately, and a script change means **re-run
`ableton-mcp-install-script` and restart Ableton Live**.

## [Unreleased]

Nothing yet.

## [1.7.0] - 2026-08-28

Ships Remote Script **1.13.0**. **Re-run `ableton-mcp-install-script` and
restart Live after upgrading** — the trim rework and the locator race fix
live in the script half, and the server refuses `trim_arrangement_clip`
against older scripts (their implementation could never trim on the Live
builds we can verify).

### Fixed (user-facing)

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
