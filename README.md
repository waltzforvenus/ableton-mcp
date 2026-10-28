<div align="center">

# Ableton MCP

**Connect Ableton Live to an AI assistant**

Prompt-assisted music production, end-to-end track creation, and Live session and arrangement manipulation.

[![License](https://img.shields.io/badge/License-GPLv3%20%2F%20AGPLv3-blue.svg)](LICENSE)
[![No telemetry](https://img.shields.io/badge/telemetry-none-brightgreen.svg)](#telemetry-removed)

*A permanent, non-commercial fork of [ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp) with all telemetry removed.*

</div>

---

## About this fork

This is a **permanent fork**, not a temporary patch set. It tracks upstream for
genuine features and takes none of the data collection. Three things make it
different:

**No telemetry, at all.** Upstream ships two collection tiers, both enabled by
default, and the second one uploads your prompts, your MIDI, and your track and
clip names. Every line of it is gone from this fork — see
[Telemetry, removed](#telemetry-removed).

**Non-commercial.** Nothing here is monetised, and no dataset is gathered from
your sessions to be sold, published, or trained on. There is no product behind
it and no company collecting anything. Your musical work stays on your machine.

**Copyleft licensing.** Changes made in this fork are dual-licensed under
GPLv3 and AGPLv3 so that this cannot quietly become a data-collection product
again downstream. See [Licensing](#licensing).

> **Installing:** the `ableton-mcp` package on PyPI is *upstream's* build, and it
> has telemetry. Install this fork from git — the commands below already do.

---

## Quickstart

Three steps: install `uv`, point your MCP client at the server, install the Ableton Remote Script.

**1. Install uv**

```bash
# macOS
brew install uv
```

Otherwise, install from [uv's official website](https://docs.astral.sh/uv/getting-started/installation/).

> **Warning:** Do not proceed before installing uv.

**2. Add the MCP server to your client**

<details open>
<summary><b>Claude Desktop</b> — Settings → Developer → Edit Config</summary>

```json
{
    "mcpServers": {
        "AbletonMCP": {
            "command": "uvx",
            "args": [
                "--from",
                "git+https://github.com/waltzforvenus/ableton-mcp",
                "ableton-mcp"
            ]
        }
    }
}
```
</details>

<details>
<summary><b>Cursor</b> — Settings → MCP</summary>

Paste this as a command:

```
uvx --from git+https://github.com/waltzforvenus/ableton-mcp ableton-mcp
```
</details>

<details>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add AbletonMCP -- uvx --from git+https://github.com/waltzforvenus/ableton-mcp ableton-mcp
```
</details>

> **Warning:** Only run one instance of the MCP server, not one per client.

**3. Install the Ableton Remote Script**

```bash
# preview the target folders first
uvx --from git+https://github.com/waltzforvenus/ableton-mcp ableton-mcp-install-script --list-targets

uvx --from git+https://github.com/waltzforvenus/ableton-mcp ableton-mcp-install-script
```

**4. Connect**

1. Launch Ableton Live
2. Go to **Settings/Preferences → Link, Tempo & MIDI**
3. In the **Control Surface** dropdown, select **AbletonMCP**
4. Set **Input** and **Output** to **None**

That's it — ask your assistant to build something.

---

## Table of Contents

- [About this fork](#about-this-fork)
- [Quickstart](#quickstart)
- [Telemetry, removed](#telemetry-removed)
- [What this fork changes](#what-this-fork-changes)
- [Features](#features)
- [Components](#components)
- [Installation](#installation)
- [Usage](#usage)
  - [Tool reference](#tool-reference)
  - [Pushing notes in bulk](#pushing-notes-in-bulk)
- [Troubleshooting](#troubleshooting)
- [Technical Details](#technical-details)
- [Limitations & Security Considerations](#limitations--security-considerations)
  - [Dead ends](#dead-ends)
- [Licensing](#licensing)
- [Contributing](#contributing)
- [Credits](#credits)
- [Disclaimer](#disclaimer)

---

## Telemetry, removed

**This fork sends nothing, anywhere. There is no opt-out because there is
nothing to opt out of.**

Upstream collects usage data through two tiers, *both on by default*. Anonymous
telemetry covers tool names, timings, success and failure, plus a persistent
installation ID. Dataset recording goes considerably further: your prompts,
your MIDI notes, session structure, track and clip names, preference labels and
browser auditions, all uploaded to the maintainer's Supabase project. Upstream
also binds LOM listeners inside Live that queue your manual UI actions — tempo
changes, mixer moves, clip edits — for the server to drain and upload.

All of it has been deleted from this fork:

| Removed | What it was |
|---|---|
| `MCP_Server/telemetry.py` | Supabase event pipeline, persistent install UUID, consent flags |
| `MCP_Server/telemetry_decorator.py` | Decorators wrapping every tool call, including MIDI note capture |
| `MCP_Server/dataset/` | Trajectory recorder, consent store, Supabase client, passive poller, snapshot uploader |
| `TERMS.md` | Data-use terms, describing collection that no longer happens |
| 6 MCP tools | `set_dataset_consent`, `submit_intent`, `rate_last_action`, `prefer_candidate`, `reject_last_action`, `record_audition` — these only wrote preference rows upstream and had no local effect |
| `user_prompt` parameter | Present on every tool upstream, purely to ship your prompt text to the collector |
| Passive LOM listeners | Remote Script hooks on tempo, time signature, playback, track add/remove, mixer volume/pan and clip slots, queued for upload |
| `supabase` dependency | Along with 30 transitive packages |

The startup event, the recorder lifecycle, and the network client are gone
too — not disabled behind a flag, deleted. You can confirm it yourself:

```bash
grep -ri "telemetry\|supabase\|dataset\|trajectory" src/ableton_mcp/ remote_script/
# no matches
```

`get_session_snapshot` is kept. It reads your project state and returns it to
**your own MCP client** so the model can see the session before planning an
edit. It uploads nothing.

---

## What this fork changes

This fork retains the upstream music-control foundation, removes its data
collection, and selectively ports later fixes. It is not a promise of parity
with every future upstream change.

See **[Fork features and upstream comparison](docs/FORK_FEATURES.md)** for a
summary of additions, reliability work, and what still awaits Live validation.
The list below describes this branch; `dev` additions are not necessarily
available on `main` or in a released package.

### Added tools

Upstream exposes 37 MCP tools; this fork exposes 68. The mixer, device control
and routing tools are new here:

| Area | Tools |
|---|---|
| **Mixer** | `set_track_volume`, `set_track_pan`, `set_track_mute`, `set_track_solo`, `set_track_arm`, `set_track_monitoring` |
| **Devices** | `delete_device`, `set_device_parameters` (the batch form), `get_plugin_presets` / `set_plugin_preset` (factory presets on hosted VST/VST3/AU plugins, whose parameters Live does not expose) — plus `get_device_parameters` / `set_device_parameter` extended to address parameters *by name* and to reach return tracks and the master track via `track_type` (`"return"` / `"master"`) |
| **Sends & buses** | `create_return_track`, `set_track_send` — one shared reverb instead of a copy per track |
| **Routing** | `set_track_routing`, `get_track_routing` |
| **Clips & tracks** | `set_clip_gain`, `set_clip_warp`, `duplicate_track`, `delete_track` |
| **Warp vision** | `analyze_clip_audio` (spectrogram + waveform image with the clip's warp markers, transients and the song grid, plus JSON), `suggest_warp_markers` (dry-run proposals with a before/after image; `apply=True` executes and verifies), `edit_clip_warp_markers` (batch add/move/remove, read back and verified), `get_clip_warp_markers` — needs the optional `analysis` extra for the two audio tools |
| **Transport & session** | `back_to_arrangement`, `set_count_in`, `save_set`, `get_song_scale` / `set_song_scale` (Live 12's song-wide key and scale) |
| **Saving** | `save_set` (API, else Live's File menu with consent; verified), `checkpoint_set` (labelled, hash-verified copies) |
| **Shared-Live safety** | `get_live_status` (one-call preflight: transport/record, soloed/muted/armed tracks, set name and unsaved marker, modal dialogs, user idle, lock holder), `acquire_live_lock` / `release_live_lock` (one editor at a time; release only after a verified save) |

### Security fix

Upstream's Remote Script binds its control socket to `0.0.0.0`, exposing Live to
every host on the local network — and that socket executes arbitrary commands
with no authentication. This fork binds to `127.0.0.1`. The MCP server always
connects from localhost, so nothing is lost.

This fix was previously undone by upstream's bundled-script installer, which
shipped its own copy of the Remote Script. The bundled copy is now generated
from the canonical one, so installing can no longer silently reopen the socket.

### Merged from upstream

Brought in and kept: the bundled Remote Script installer
(`ableton-mcp-install-script`), the script version and capability handshake,
`create_locator`, `clear_notes_from_clip`, `get_clip_notes`,
`get_session_snapshot`, and the arrangement tooling.

Where both projects had independently written the same tool, one of each was
kept — `clear_clip_notes` folded into upstream's `clear_notes_from_clip`, and
this fork's parameter-by-name device tools over upstream's index-only
versions. Duplicates were not harmless: the MCP framework registers tools by
name, so the loser was silently overwritten depending on file order — and the
merge initially left both device-tool handlers defined twice inside the Remote
Script itself, which broke them at runtime until script v1.8.0 removed the
duplicates. Run `ableton-mcp-install-script` after updating so Live loads the
repaired script.

---

## Features

| | |
|---|---|
| **Two-way communication** | Socket-based server connecting your assistant to Ableton Live |
| **Track manipulation** | Create, modify, and manipulate MIDI and audio tracks |
| **Instrument and effect selection** | Load the right instruments, effects and sounds from Ableton's library |
| **Clip creation** | Create and edit MIDI clips with notes |
| **Mixing** | Volume, pan, mute, sends, return tracks and device parameters by name |
| **Arrangement view composition** | Build full songs, including intro, buildup, drop, breakdown, and outro |
| **Session control** | Playback, clip firing, and transport across Session and Arrangement View |
| **No telemetry** | Nothing is collected, stored, or transmitted |

## Components

1. **Ableton Remote Script** (`remote_script/__init__.py`) — a MIDI Remote Script for Ableton Live that creates a socket server to receive and execute commands
2. **MCP Server** (`src/ableton_mcp/`) — a Python package that implements the Model Context Protocol and connects to the Remote Script

---

## Installation

### Prerequisites

- **Ableton Live** 10 or newer
- **Python** 3.10 or newer
- **uv** package manager

### Installing the Ableton Remote Script

```bash
# preview the target folders first
uvx --from git+https://github.com/waltzforvenus/ableton-mcp ableton-mcp-install-script --list-targets

uvx --from git+https://github.com/waltzforvenus/ableton-mcp ableton-mcp-install-script
```

This copies the matching Remote Script into Ableton's **User Remote Scripts**
folder. If a different version of the script is already there, the existing file
is backed up to `__init__.py.bak` before being replaced.


This copies the matching Remote Script into your Ableton **User Library**'s `Remote Scripts` folder — the location Live (10.1.13+) scans for third-party control surface scripts. The installer reads the User Library location from Live's `Library.cfg`, falling back to the default (`~/Music/Ableton/User Library` on macOS, `Documents\Ableton\User Library` on Windows). If a different version of the script is already there, the existing file is backed up to `__init__.py.bak` before being replaced.

If your User Library lives somewhere non-standard and isn't detected, point the installer at it directly:

```bash
uvx --from git+https://github.com/waltzforvenus/ableton-mcp ableton-mcp-install-script --target "/path/to/User Library/Remote Scripts"
```

> The legacy `Preferences/User Remote Scripts` folder (used for instant-mapping configs, not Python control surfaces) is no longer targeted by default; pass `--legacy` if you need it for an old Live version.

Then **restart Ableton** so Live loads it. (Re-selecting the AbletonMCP
control surface is only enough the *first* time, when the script is newly
detected — Live caches an already-imported script module, so an updated
script needs a full restart.) Re-run the command after upgrading — the server logs a warning when
the loaded script version doesn't match what it expects.

> **Note:** The server does **not** install the script on startup. Writing into
> Ableton's preferences directory is an explicit action, not a side effect of
> launching a server.

**First-time Ableton setup:**

1. Run the install command above
2. Launch Ableton Live
3. Go to **Settings/Preferences → Link, Tempo & MIDI**
4. In the **Control Surface** dropdown, select **AbletonMCP**
5. Set **Input** and **Output** to **None**

<details>
<summary><b>Manual fallback locations (User Library → Remote Scripts)</b></summary>

- **macOS:** `~/Music/Ableton/User Library/Remote Scripts/AbletonMCP/`
- **Windows:** `C:\Users\[Username]\Documents\Ableton\User Library\Remote Scripts\AbletonMCP\`

If you've moved your User Library, use its actual location (shown in Live under **Preferences → Library → Location of User Library**), and create the `Remote Scripts` folder inside it if it doesn't exist yet.
</details>

The MCP server and Remote Script share a version handshake
(`get_remote_script_info`). If they diverge, newer tools degrade gracefully
until Live is restarted.

### Running from a local checkout

```bash
git clone https://github.com/waltzforvenus/ableton-mcp
cd ableton-mcp
uv sync --extra dev
uv run ableton-mcp
uv run pytest        # tests run without Ableton and without network
```

`scripts/` holds two helpers that are deliberately not tests:
`smoke_live.py` drives a real Live session through the production stack and
reports per step, and `push_notes.py` pushes note JSON files straight to the
Remote Script socket without going through the model at all (see
[Pushing notes in bulk](#pushing-notes-in-bulk)).

### Installing from CI builds and releases

Every push runs the [CI workflow](.github/workflows/ci.yml): the full test
suite (no Ableton, no network), then a package build whose wheel and sdist
are uploaded as a workflow artifact. Every tag starting with `v` runs the
[Release workflow](.github/workflows/release.yml), which re-runs the suite
and attaches the same build to a GitHub Release.

**From a release (stable, no login needed):**

1. Open the [Releases page](https://github.com/waltzforvenus/ableton-mcp/releases)
   and download the `.whl` from the release you want.
2. Install or run it:

```bash
# install the command onto your PATH
uv tool install ./ableton_mcp-*.whl        # or: pip install ./ableton_mcp-*.whl

# or run it without installing
uvx --from ./ableton_mcp-*.whl ableton-mcp
```

**From a CI run (any branch or PR, before it's released):**

1. Open the [Actions tab](https://github.com/waltzforvenus/ableton-mcp/actions),
   pick the run for the commit you want, and download the
   **`ableton-mcp-dist`** artifact from the run's summary page (downloading
   artifacts requires being signed in to GitHub; artifacts expire after
   90 days — releases don't).
2. Unzip it and install the `.whl` exactly as above.

A few notes that apply to every install method:

- The wheel is pure Python — there are no compiled platform binaries, so the
  same file works on macOS, Windows, and Linux (Python ≥ 3.10).
- The console-script names never change: your MCP client config points at
  `ableton-mcp` whether it came from `uvx --from git+…`, a release wheel, or
  a CI artifact.
- The wheel bundles the matching Remote Script. After installing or
  upgrading, run `ableton-mcp-install-script` and restart Ableton so the
  Live side matches — the version handshake will tell you if you forget.

---

## Usage

### Starting the Connection

1. Ensure the Ableton Remote Script is loaded in Ableton Live
2. Make sure the MCP server is configured in your client
3. The connection is established automatically when you interact with the assistant

### The Live lock (state-changing tools need it by default)

Live is often shared — several agents, plus the person at the keyboard. Every
tool that changes Live's state is therefore checked against a shared lock file
before anything is sent:

- **Held by someone else** → refused, naming the holder and their task.
- **Held by nobody** → refused until this server calls
  `acquire_live_lock(holder, task)` — unless the server runs with
  `ABLETON_MCP_REQUIRE_LOCK=0`, the opt-out for single-user setups (a lock
  someone else holds is still honoured then).
- The lock **never expires**. `release_live_lock(holder, set_path)` succeeds
  only once the saved `.als` is at least as new as the holder's last edit and,
  on macOS, Live's window shows that set with no unsaved `*` and no Save or
  Export dialog.
- `stop_playback` is never refused; reads never need the lock.

The file is plain JSON at `ABLETON_MCP_LOCK_FILE` (default
`~/.ableton-mcp/live.lock`) with an flock'd `<file>.mutex`, so command-line
helpers can share it: take the lock there under a name, then call
`acquire_live_lock` with the same name to adopt it. `get_live_status` shows
who holds it.

### Tool reference

All 68 tools the server exposes. Tools marked **†** are added by this fork and
are not present upstream.

#### Session & info

| Tool | Arguments | Description |
|---|---|---|
| `get_session_info` | — | Get detailed information about the current Ableton session |
| `get_track_info` | `track_index` | Get detailed information about a specific track in Ableton |
| `get_session_snapshot` | `include_notes`?, `include_params`?, `include_warp_markers`?, `include_rack_chains`?, `include_empty_slots`?, `tracks`? | Read project state in one call — scope it with `tracks` (names or indices); the three detail flags default off |
| `get_remote_script_info` | — | Report Ableton Remote Script version and capabilities (handshake) |
| `get_live_status` † | — | One-call preflight: reachable, versions, transport/record, soloed/muted/armed tracks, last event time, the Live lock, and (macOS) set name, unsaved marker, modal dialog and user idle time |
| `acquire_live_lock` † | `holder`, `task`? | Take the shared Live lock (required before state-changing tools unless `ABLETON_MCP_REQUIRE_LOCK=0`); adopts a lock already held under the same name |
| `release_live_lock` † | `holder`, `set_path`?, `save_first`?, `allow_ui_automation`? | Release the lock only after a verified save (.als newer than the last edit; no unsaved marker or dialog) |
| `set_tempo` | `tempo` | Set the tempo of the Ableton session |
| `get_song_scale` † | — | Read the Set's key and scale (Live 12+): root note, scale name, Scale Mode and intervals |
| `set_song_scale` † | `root_note`?, `scale_name`?, `scale_mode`? | Set the Set's root (0-11 or a note name like `"F#"` / `"Bb"`), scale name and Scale Mode (Live 12+); reports what Live reads back and whether it matched |
| `save_set` † | `allow_ui_automation`?, `set_path`? | Save the open set — Live's API if it has one, else File › Save Live Set over Accessibility (macOS, with consent); verified by the title's unsaved marker and the .als mtime |
| `checkpoint_set` † | `label`, `set_path`?, `allow_ui_automation`?, `checkpoint_dir`? | Save, then keep a sha256-verified copy at `<set folder>/Checkpoints/<timestamp>-<label>.als` |

#### Tracks

| Tool | Arguments | Description |
|---|---|---|
| `create_midi_track` | `index`? | Create a new MIDI track in the Ableton session |
| `create_audio_track` | `index`? | Create a new audio track in the Ableton session |
| `create_return_track` † | — | Create a new return track — a shared effects bus that any track can send to |
| `duplicate_track` † | `track_index` | Duplicate a whole track (devices, mixer, routing, clips); the copy lands directly below the source |
| `delete_track` † | `track_index` | Delete a track from the Ableton session, along with all clips on it |
| `set_track_name` | `track_index`, `name` | Set the name of a track |

#### Mixer

| Tool | Arguments | Description |
|---|---|---|
| `set_track_volume` † | `track_index`, `value`, `track_type`? | Set a track's mixer volume |
| `set_track_pan` † | `track_index`, `value`, `track_type`? | Set a track's stereo panning |
| `set_track_mute` † | `track_index`, `mute` | Mute or unmute a track. Useful for auditioning parts in isolation |
| `set_track_solo` † | `track_index`, `solo`, `track_type`? | Solo or unsolo a regular or return track, to audition a part in context; the reply names every soloed track and Live's Exclusive Solo setting. The master cannot be soloed |
| `set_track_arm` † | `track_index`, `armed`? | Arm or disarm a track for recording |
| `set_track_monitoring` † | `track_index`, `state`? | Set a track's input monitoring, so the performer can hear themselves |
| `set_track_send` † | `track_index`, `send_index`, `value` | Set how much of a track is sent to a return track |

#### Routing

| Tool | Arguments | Description |
|---|---|---|
| `get_track_routing` † | `track_index` | Show a track's input and output routing, plus every option available to it |
| `set_track_routing` † | `track_index`, `target`, `field`? | Route a track's output (or input) somewhere else, by display name |

#### Devices

| Tool | Arguments | Description |
|---|---|---|
| `get_device_parameters` | `track_index`, `device_index`, `track_type`? | List every parameter on a device, with its current value, range and the |
| `set_device_parameter` | `track_index`, `device_index`, `parameter`, `value`, `track_type`? | Set one parameter on a device. This is how you actually mix: pull a reverb's |
| `set_device_parameters` † | `track_index`, `device_index`, `parameters`, `track_type`? | Set MANY parameters on one device in a single call — the batch form, preferred when dialling a device in |
| `get_plugin_presets` † | `track_index`, `device_index`, `track_type`? | List a hosted VST/VST3/AU plugin's factory presets and which one is selected — the way in to a plugin whose parameters Live does not expose |
| `set_plugin_preset` † | `track_index`, `device_index`, `preset_index`?, `preset_name`?, `track_type`? | Select a plugin's factory preset by index or by name (exact, then an unambiguous fragment); refuses ambiguous names, out-of-range indices and devices without presets |
| `delete_device` † | `track_index`, `device_index`, `track_type`? | Remove a device from a track's chain |
| `load_instrument_or_effect` | `track_index`, `uri`, `track_type`? | Load an instrument or effect onto a track using its URI |
| `load_drum_kit` | `track_index`, `rack_uri`, `kit_path` | Load a drum rack and then load a specific drum kit into it |

#### Browser

| Tool | Arguments | Description |
|---|---|---|
| `get_browser_tree` | `category_type`? | Get a hierarchical tree of browser categories from Ableton |
| `get_browser_items_at_path` | `path` | Get browser items at a specific path in Ableton's browser |

#### Clips (Session)

| Tool | Arguments | Description |
|---|---|---|
| `create_clip` | `track_index`, `clip_index`, `length`? | Create a new MIDI clip in the specified track and clip slot |
| `create_audio_clip` | `track_index`, `clip_index`, `path`, `expected_beats`? | Import an audio file into a clip slot, warping OFF, and cross-check its length against `expected_beats` |
| `delete_clip` | `track_index`, `clip_index` | Delete the clip in the given clip slot, freeing it for reuse |
| `set_clip_name` | `track_index`, `clip_index`, `name` | Set the name of a clip |
| `set_clip_gain` † | `track_index`, `clip_index`, `gain`, `arrangement`? | Set one audio clip's gain, leaving every other clip on the track untouched |
| `set_clip_warp` † | `track_index`, `clip_index`, `warping`, `warp_mode`?, `arrangement`? | Turn a clip's warping on or off — the fix for stems Live auto-warped to different tempos |
| `get_clip_notes` | `track_index`, `clip_index`, `arrangement`? | Read all MIDI notes from a clip, in either view |
| `add_notes_to_clip` | `track_index`, `clip_index`, `notes`, `expect_count`?, `arrangement`? | Add MIDI notes to a clip in either view; reports what Live actually took, and `expect_count` refuses a truncated list |
| `clear_notes_from_clip` | `track_index`, `clip_index`, `arrangement`? | Remove all MIDI notes from a clip, in either view |

#### Warp vision

Look at an audio clip, fix its warping, verify. The two audio tools read the
clip's file on this machine (nothing is uploaded) and need the optional
extra: run `uv sync --extra analysis` from this fork's checkout
(numpy, soundfile, Pillow).
Workflow: `analyze_clip_audio` → `suggest_warp_markers` (dry run) → apply →
`analyze_clip_audio` again; `grid_alignment` (median/p90 ms off the grid) is
the before/after number. Warp-marker editing needs Live 11.0.5 or later.

| Tool | Arguments | Description |
|---|---|---|
| `analyze_clip_audio` † | `track_index`, `clip_index`, `arrangement`?, `start_beat`?, `end_beat`?, `start_seconds`?, `end_seconds`?, `include_image`?, `image_width`?, `sensitivity`?, `max_transients`? | Image (waveform + log-frequency spectrogram with warp markers, transients, song bars/beats, estimated source beats, low/mid/high energy strip) plus JSON: transients, tempo estimate, band energy, grid alignment |
| `suggest_warp_markers` † | `track_index`, `clip_index`, `arrangement`?, `mode`?, `subdivision`?, `tolerance`?, `min_strength`?, `max_edits`?, window args?, `apply`?, `include_image`? | Propose marker edits that put hits on the grid (`transients` local fix, `rewarp` drift tracking, `tempo` constant tempo); dry run with a before/after image by default, `apply=True` sends and verifies |
| `edit_clip_warp_markers` † | `track_index`, `clip_index`, `edits`, `arrangement`?, `stop_on_error`? | Batch add/move/remove warp markers in one main-thread task; reads them back and reports `verified` |
| `get_clip_warp_markers` † | `track_index`, `clip_index`, `arrangement`? | Raw warp markers (beat_time, sample_time in file seconds) with file path, sample rate, start/end/loop markers, warp mode and song tempo |

#### Arrangement

| Tool | Arguments | Description |
|---|---|---|
| `switch_to_arrangement_view` | — | Switch Ableton's main window to the Arrangement view |
| `get_arrangement_clips` | `track_index` | List all clips placed in the Arrangement timeline for a track |
| `duplicate_to_arrangement` | `track_index`, `clip_index`, `destination_time`, `destination_times`?, `allow_loop_phase_reset`? | Copy a Session-view clip into the Arrangement — pass `destination_times` to place a whole run in one call; refuses stamps that would re-phase a looping clip |
| `set_arrangement_clip_name` | `track_index`, `clip_index`, `name` | Set the name of a clip placed in the Arrangement timeline |
| `set_arrangement_time` | `time` | Move the arrangement playhead to a specific position |
| `create_locator` | `name`, `time` | Create a named locator (cue point) in the Arrangement at a beat position |
| `jump_to_locator` † | `name`?, `time`? | Jump to an existing locator by name or beat time; while stopped this plants the start marker, so play/record launch from it |
| `delete_locator` † | `name`?, `time`? | Delete a locator, matched by name or beat — parks the playhead on it first, and refuses rather than toggling from the wrong position |
| `trim_arrangement_clip` † | `track_index`, `clip_index`, `start_time`?, `end_time`? | Trim an Arrangement clip's edges inward (take cleanup) — crops UI-style via a temporary silent stamp, each edge verified by readback |
| `delete_arrangement_clip` † | `track_index`, `start_times`?, `start_time`?, `clip_index`? | Delete Arrangement clips, addressed by beat position — `start_times` removes a whole set in one pass, all-or-nothing |
| `move_arrangement_clip` † | `track_index`, `clip_index`, `destination_time` | Move an Arrangement clip to a new start position (duplicate + delete under the hood; refuses a self-overlapping destination) |
| `duplicate_arrangement_clip` † | `track_index`, `clip_index`, `destination_time`, `allow_loop_phase_reset`? | Copy an Arrangement clip elsewhere on its track — reuse a recorded take at another section; refuses stamps that would re-phase a looping clip |
| `back_to_arrangement` † | — | Return every track to Arrangement playback — Live's "Back to Arrangement" button |

#### Transport

| Tool | Arguments | Description |
|---|---|---|
| `start_playback` | — | Start playing the Ableton session |
| `stop_playback` | — | Stop playing the Ableton session |
| `fire_clip` | `track_index`, `clip_index` | Start playing a clip |
| `stop_clip` | `track_index`, `clip_index` | Stop playing a clip |
| `set_count_in` † | `bars`?, `metronome`? | Set the record count-in, giving a performer a lead-in before punching in |

Arguments marked `?` are optional. `track_type` accepts `"regular"` or
`"return"`, so the device and mixer tools reach return tracks as well as
ordinary ones. `set_device_parameter` takes a parameter *name* as shown by
`get_device_parameters` (e.g. `"Dry/Wet"`), or an index passed as a string.

Every tool that writes to a track also accepts an optional
`expect_track_name`: give it the name you believe is at `track_index` and the
write is refused, naming what is actually there, if the two disagree. Track
indices renumber whenever a track is created or deleted, and an index that is
still in range but now points at a different track fails silently otherwise.

A few arguments accept a second spelling, because they get guessed by the
name a neighbouring tool uses: `duplicate_to_arrangement`'s
`destination_time` is also accepted as `arrangement_time` or `time`,
`set_device_parameter`'s `parameter` as `parameter_name`, and
`create_audio_clip`'s `path` as `file_path`. Passing both spellings with
different values is refused rather than resolved.

Third-party plugins are a special case. Live exposes no parameters of an
unconfigured VST3 or AU through its API — `get_device_parameters` on
TAL-NoiseMaker, Dexed or a u-he synth lists only "Device On" — so
`get_plugin_presets` / `set_plugin_preset` work through the one thing it does
expose: the plugin's factory preset list. A plugin that publishes no presets
to Live can only be set up from its own window.

### Example Commands

| Prompt |
|---|
| *"Create an 80s synthwave track"* |
| *"Create a Metro Boomin style hip-hop beat"* |
| *"Create a full arrangement with an intro, buildup, drop, breakdown, and outro"* |
| *"Create a new MIDI track with a synth bass instrument"* |
| *"Add a reverb return track and send the drums to it at 20%"* |
| *"Pull the Dry/Wet on the reverb down to 15%"* |
| *"Put the NoiseMaker on a warm pad preset"* |
| *"Create a 4-bar MIDI clip with a simple melody"* |
| *"Add a jazz chord progression to the clip in track 1"* |
| *"Set the tempo to 120 BPM"* |
| *"Save the set"* |

### Pushing notes in bulk

`add_notes_to_clip` is the right tool for a phrase and the wrong transport
for a whole-song drum part: every note the model sends passes through its
context twice, once read and once re-emitted per call, and a few thousand
notes is a couple of hundred kilobytes of JSON. When a generator has already
written the notes to disk, push the files straight to the Remote Script's
socket instead:

```bash
python3 scripts/push_notes.py verse1.kt.0.json verse1.kt.1.json \
    --track 0 --clip 3 --expect-track-name "drums — kick + toms" --clip-name "verse1 kt"
```

or describe many clips in one manifest, with file paths relative to it:

```json
{"targets": [
  {"file": "verse1.kt.0.json", "track_index": 0, "clip_index": 3,
   "expect_track_name": "drums — kick + toms", "clip_name": "verse1 kt", "clear": true},
  {"file": "verse1.sh.0.json", "track_index": 1, "clip_index": 3,
   "expect_track_name": "drums — hats + snare + cymbals"},
  {"notes": [{"pitch": 49, "start_time": 0.0, "duration": 0.5, "velocity": 110, "mute": false}],
   "track_index": 1, "clip_index": 0, "arrangement": true}
]}
```

```bash
python3 scripts/push_notes.py drums-manifest.json
```

A note file is a JSON list of `{pitch, start_time, duration, velocity, mute}`
objects, `start_time` in beats from the clip's own start. The script needs
nothing installed (standard library only). It validates every note before it
sends anything, writes in chunks of at most 110 notes with `expect_count` on
each chunk and `expect_track_name` on every command, and prints what Live
reported for each chunk: the count Live took, not the count sent. It stops at
the first refusal and says which clip is half-written. Writes are additive,
so `--clear` (or `"clear": true` on a manifest entry) empties the clip first;
`--arrangement` addresses a clip on the timeline; `--dry-run` prints the plan
and connects to nothing. It is safe to run while the MCP server is
connected: the Remote Script takes any number of clients and serialises
writes on Live's main thread.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| **Connection issues** | Make sure the Remote Script is loaded in Live and the MCP server is configured in your client |
| **Timeout errors** | Simplify your requests or break them into smaller steps |
| **A tool reports a missing capability** | Your installed Remote Script is older than the package. Re-run the install command, then restart Ableton |
| **Still stuck** | Restart both your MCP client and Ableton Live |

## Technical Details

### Communication Protocol

A JSON-based protocol over a TCP socket bound to `127.0.0.1`:

- **Commands** are sent as JSON objects with a `type` and optional `params`
- **Responses** are JSON objects with a `status` and `result` or `message`
- There is no length framing and no request id: a reply is complete when the
  bytes so far parse as JSON, and it answers the last command sent on that
  connection. The script accepts several connections and serialises
  state-changing commands on Live's main thread. `scripts/push_notes.py` is
  a standard-library client for this protocol, small enough to read as its
  specification

## Limitations & Security Considerations

- Complex musical arrangements may need to be broken into smaller steps
- Designed to work with Ableton's default devices and browser items
- The Remote Script socket executes commands without authentication. It is bound
  to loopback, so only processes on your own machine can reach it — do not
  forward or rebind that port
- Always save your work before extensive experimentation

### Dead ends

Things that look like they should work through the Live Object Model and do
not. Each was attempted against real Live (12.4.3) and verified impossible —
recorded here so nobody spends another afternoon rebuilding one.
`fade_arrangement_clip` was written once, passed 424 tests against the fake,
and had to be deleted in review; that is the cost this list exists to avoid.
The long form, with line references, is in `docs/IMPROVEMENTS.md`.

| Dead end | Why |
|---|---|
| **Clip fades and arrangement automation lanes** | `Clip.automation_envelope` returns `None` for arrangement clips and `create_automation_envelope` raises there. Fades stay a baked-render job. |
| **Any in-tick retry or same-tick readback** | A write and a re-read inside one Live callback return the same stale value by construction — the read can stay stale for the whole tick even though the write landed. There is no yield primitive, and sleeping on Live's main thread blocks audio and UI. Only a later tick can clear. |
| **Moving an Arrangement clip's footprint by writing markers** | `start_marker` / `end_marker` writes land but never move the footprint. They do change what plays, which is why marker writes are still useful on *Session* clips — but nothing resizes an arrangement clip except the stamp path. |
| **Creating group tracks** | No LOM function creates one and `Track.group_track` is read-only. Buses built with `set_track_routing` are the only path. |
| **Render, export, and saving through the LOM (12.4.x)** | The Python API has no save, render or export entry point. Saving now goes through Live's File menu instead (`save_set(allow_ui_automation=true)`, macOS); render/export stay UI-only. |
| **Detecting Live's edition by probing** | The LOM exposes no edition or product property, so Intro's track / return / scene caps cannot be read ahead of time. They surface only as a raise, which is why every tool that can hit a cap catches and reports it instead of pre-checking. |
| **A gapless `split_arrangement_clip`** | There is no LOM split and no zero-width stamp, so any eraser-based split leaves a gap of the eraser's own width. |

---

## Licensing

This fork is licensed differently from upstream, deliberately and permanently.

- **Upstream code** remains under the MIT license, preserved verbatim in
  [`LICENSE.MIT`](LICENSE.MIT) with its copyright notice intact.
- **This fork's changes** are dual-licensed: you may use them under **either**
  [GPL-3.0-or-later](LICENSE.GPL-3.0) **or**
  [AGPL-3.0-or-later](LICENSE.AGPL-3.0), your choice as the recipient.

MIT permits sublicensing, which is what makes the combination lawful. The
practical effect is that the repository as a whole is copyleft: distribute it,
modified or not, under whichever license you chose, keep the MIT notice, and
make corresponding source available. The AGPL option additionally covers running
a modified version as a network service. Running the MCP server locally is not
distribution and carries no obligation.

Full detail in [`LICENSE`](LICENSE). It is not legal advice.

## Contributing

Contributions are welcome. By opening a pull request you offer your changes
under GPL-3.0-or-later and AGPL-3.0-or-later.

Pull requests that add telemetry, analytics, crash reporting, "anonymous" usage
statistics, or any other form of phoning home will not be accepted.

## Credits

The original AbletonMCP was created by [Siddharth Ahuja](https://x.com/sidahuj),
and the upstream project remains at
[ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp). This fork
exists to keep that work usable without the data collection attached to it, and
retains its MIT notice with thanks.

## Disclaimer

This is a third-party integration and is not made by or affiliated with Ableton.

---

<div align="center">

**Your music stays on your machine.**

</div>
