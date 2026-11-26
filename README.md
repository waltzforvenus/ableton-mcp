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

Upstream exposes 37 MCP tools; this fork exposes 86. The mixer, device control
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
| **Rendering** | async export jobs (start/status/cancel), synchronous export_audio, bounce_track, analyze_audio / verify_audio |
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

### Sound needs authorization (by default)

`start_playback`, `fire_clip` and arming a track (`set_track_arm(armed=true)`)
are refused unless the call passes `authorized=true` — the agent must have the
user's OK before making sound or preparing to record. Disarming and
`stop_playback` never need it. `ABLETON_MCP_REQUIRE_AUDIO_AUTH=0` turns the
gate off for setups where nobody else is in the room.

### The Live lock (state-changing tools need it by default)

Live is often shared — several agents, plus the person at the keyboard. Every
tool that changes Live's state is therefore checked against a shared lock file
before anything is sent:

- **Held by someone else** → refused, naming the holder and their task.
- **Held by nobody** → refused until this server calls
  `acquire_live_lock(holder, task)` — unless the server runs with
  `ABLETON_MCP_REQUIRE_LOCK=0`, the opt-out for single-user setups (a lock
  someone else holds is still honoured then).
- The lock **never expires**. Release requires generation-bound observed save
  evidence, equal AXDocument paths before/after save and at release, a readable
  clean title, and file freshness after acquisition and edits. A newer same-name
  file alone cannot release an unknown baseline.
- `stop_playback` is never refused; reads never need the lock.

The file is plain JSON at `ABLETON_MCP_LOCK_FILE` (default
`~/.ableton-mcp/live.lock`) with an flock'd `<file>.mutex`, so command-line
helpers can inspect it. Name-only adoption is not supported. Older writers
must preserve identity/generation/reservations or are incompatible writers.
get_live_status shows who holds it.

### Tool reference

All 102 tools the server exposes. Tools marked **†** are added by this fork and
are not present upstream.

#### Session & info

| Tool | Arguments | Description |
|---|---|---|
| `get_session_info` | — | Get detailed information about the current Ableton session |
| `get_track_info` | `track_index` | Get detailed information about a specific track in Ableton |
| `get_session_snapshot` | `include_notes`?, `include_params`?, `include_warp_markers`?, `include_rack_chains`?, `include_empty_slots`?, `tracks`?, `max_chars`? | Read project state in one call — scope it with `tracks` (names or indices); the three detail flags default off |
| `get_remote_script_info` | — | Report Ableton Remote Script version and capabilities (handshake) |
| `get_mix_state` † | — | The mixer at a glance: per track/return/main — volume and pan as displayed, mute, solo, arm, sends, output routing, devices (with off state) — a few KB |
| `get_live_status` † | — | One-call preflight: reachable, versions, transport/record, soloed/muted/armed tracks, last event time, the Live lock, and (macOS) set name, unsaved marker, modal dialog and user idle time |
| `acquire_live_lock` † | `holder`, `task`? | Take the shared Live lock (required before state-changing tools unless `ABLETON_MCP_REQUIRE_LOCK=0`); never adopts another instance by holder name |
| `resume_live_lock` † | owner, owner_proof, expected_generation | Authenticated resume; default unsupported; active/uncertain operations refuse |
| `transfer_live_lock` † | from_owner, to_owner, save_proof, expected_generation | Atomic authenticated saved handoff; default unsupported; active/uncertain operations refuse |
| `release_live_lock` † | `holder`, `set_path`?, `save_first`?, `allow_ui_automation`? | Release only with generation-bound observed save and matching AXDocument/clean title; no active or uncertain operations |
| `set_tempo` | `tempo` | Set the tempo of the Ableton session |
| `get_song_scale` † | — | Read the Set's key and scale (Live 12+): root note, scale name, Scale Mode and intervals |
| `set_song_scale` † | `root_note`?, `scale_name`?, `scale_mode`? | Set the Set's root (0-11 or a note name like `"F#"` / `"Bb"`), scale name and Scale Mode (Live 12+); reports what Live reads back and whether it matched |
| `save_set` † | `allow_ui_automation`?, `set_path`? | Save the open set — Live's API if it has one, else File › Save Live Set over Accessibility (macOS, with consent); verified by the title's unsaved marker and the .als mtime |
| `checkpoint_set` † | `label`, `set_path`?, `allow_ui_automation`?, `checkpoint_dir`? | Save, then keep a sha256-verified copy at `<set folder>/Checkpoints/<timestamp>-<label>.als` |

| `plan_arrangement_region` † | request | Preview a server-held whole MIDI clip copy manifest; crop/audio fades/replacement/atomic batches unsupported |
| `apply_arrangement_region` † | manifest_id, fingerprint | Checkpoint, revalidate and apply a single native clone into empty space; uncertain outcomes require reconciliation |

| `get_parameter_automation` | target, range | Device automation state or saved-copy events; overrides unknown in saved copies |
| `plan_automation_edit` | target, points, operation | Non-mutating saved-copy envelope diff; live CRUD unsupported |
| `apply_automation_edit` | manifest_id, fingerprint | Validates freshness then refuses unsupported apply; never reloads |

| `plan_automation_reload` | copy_path, targets | Read-only selected-target override risk baseline; never reloads |
| `reconcile_automation_reload` | plan_id, fingerprint | Fresh state comparison; never authorizes reload or repairs overrides |

| `inspect_automation_curve` | saved_path, pointee_id, sample_times | Inspect serialized curve fields and exact stored points; no playback evaluation |

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
| `set_device_parameters` † | `track_index`, `device_index`, `parameters`, `track_type`? | Set MANY parameters on one device in a single call — the batch form, preferred when dialling a device in. Values may be raw numbers or display strings (`"3.6 s"`, `"-1.5 dB"`, `"35 %"`, a picker label) |
| `get_plugin_presets` † | `track_index`, `device_index`, `track_type`? | List a hosted VST/VST3/AU plugin's factory presets and which one is selected — the way in to a plugin whose parameters Live does not expose |
| `set_plugin_preset` † | `track_index`, `device_index`, `preset_index`?, `preset_name`?, `track_type`? | Select a plugin's factory preset by index or by name (exact, then an unambiguous fragment); refuses ambiguous names, out-of-range indices and devices without presets |
| `delete_device` † | `track_index`, `device_index`, `track_type`? | Remove a device from a track's chain |
| `load_devices` † | `track_index`, `uris`, `track_type`?, `expect_track_name`? | Load several browser items onto one track in order (a whole chain); stops at failure or state reconciliation; reports per-load arm/monitor evidence |
| `start_export_audio` † | idempotency_key, request | Queue an idempotent export without a UI wait. |
| `get_export_job` † | job_id | Read journal status and retained evidence. |
| `cancel_export_job` † | job_id | Cooperative stop; no native abort claim. |
| `export_audio` † | `output_dir`, `file_stem`, `scope`?, `track`?, `start_beat`?, `end_beat`?, `tail_beats`?, `sample_rate`?, `bit_depth`?, `file_type`?, … `allow_ui_automation`, `dry_run`? | Live's own offline render via File › Export Audio/Video, driven over Accessibility: range from the LOM, every field set by label and read back, Export by its button, files measured. No playback |
| `bounce_track` † | `track_index`, `mode`?, `allow_ui_automation`, `expect_track_name`?, `confirm_destructive`? | Freeze / Bounce to New Track / Bounce Track in Place through the Edit menu, verified through the LOM (frozen flag, track count, the new clip's file) |
| `analyze_audio` † | `path`, `start_s`?, `end_s`? | Measure a file offline: duration, rate, LUFS-I, LRA, true peak, sample peak, RMS, clipping, L/R balance/correlation, silence, cut tail (ffmpeg) |
| `verify_audio` † | `path`, `expected_duration_s`?, `tolerance_s`?, `sample_rate`?, `bit_depth`?, `max_true_peak_dbtp`?, `min_lufs`?, `max_lufs`?, `allow_tail_cut`? | analyze_audio plus pass/fail checks; fails on silence, clipping, a cut tail, or any limit |
| `insert_device` † | `track_index`, `device_name`, `index`?, `track_type`? | Insert a native device at a position in the chain (Live 12 `Track.insert_device`) |
| `move_device` † | `track_index`, `device_index`, `to_index`, `to_track_index`? | Reorder a chain or move a device to another track (`Song.move_device`) |
| `get_device_sidechain` † | `track_index`, `device_index` | A device's sidechain source/channel, the choices Live offers, and the S/C switch |
| `set_device_sidechain` † | `track_index`, `device_index`, `source`, `channel`?, `enable`? | Key a Compressor/Gate/… from another track and switch sidechain on; read back |
| `create_arrangement_audio_clip` † | track_index, path, start_time, warping?, source_offset_seconds?, duration_seconds?, destination_length_beats?, allow_overlap?, name? | Whole-file import only on an empty audio arrangement track; measured native extent, no tempo conversion. Occupied/trimmed/beat-span requests refuse; uncertain writes require reconciliation |
| `create_arrangement_midi_clip` † | `track_index`, `start_time`, `length`?, `notes`?, `allow_overlap`? | Create (and fill) a MIDI clip directly on the arrangement (Live 12) |
| `get_rack_chains` † | `track_index`, `device_index`, `track_type`? | A rack's chains: name, volume/pan (value + display), mute, solo, Drum Rack `in_note`, devices |
| `insert_rack_chain` † | `track_index`, `device_index`, `index`?, `name`? | Add a chain to a rack (Live 12 `RackDevice.insert_chain`) — parallel blends |
| `insert_chain_device` † | `track_index`, `device_index`, `chain_index`, `device_name`, `index`? | Put a native device into one chain at a position (`Chain.insert_device`) |
| `set_chain_mixer` † | `track_index`, `device_index`, `chain_index`, `volume`?, `panning`?, `mute`?, `solo`?, `name`?, `in_note`? | Set a chain's mixer (raw or display strings), name, and a Drum Rack pad's receiving note |
| `load_device_from_path` † | `track_index`, `path`, `track_type`?, `expect_track_name`? | Load a preset file (.adv/.adg/.vstpreset/.aupreset) from the User Library or a Places folder by absolute path — how a configured plugin (amp capture, IR, patch) comes back exactly |
| `get_plugin_state` † | `source_path`, `track`?, `plugin`?, `include_strings`? | Offline: decode hosted plugins' saved state from a .als/.adv/.adg/.vstpreset — size, sha256, file paths and readable strings — to verify which model/IR/patch a plugin holds |
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
| `plan_note_edits` † | snapshot, edits | Trusted complete-read token + full fingerprint; per-note-ID patches |
| `apply_note_edits` † | plan | Single-use native modifications, stale identity refusal and measured verification |
| `set_notes_probability` † | track_index, clip_index, probability?, velocity_deviation?, note_ids?, pitch?, from_time?, time_span?, arrangement?, expect_track_name? | Same guarded primitives; exact ID/range intersection |
| `get_clip_notes` | `track_index`, `clip_index`, `arrangement`?, `source_start`?, `source_end`?, `cursor`?, `limit`? | Source-beat MIDI pages with explicit completeness, offsets and audible-window projection |
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

### Lossless MIDI patches (script 1.29.0)

| Tool | Parameters | Purpose |
|---|---|---|
| `plan_note_edits` † | snapshot, edits | Trusted complete-read token + full fingerprint; per-note-ID patches |
| `apply_note_edits` † | plan | Single-use native modifications, stale identity refusal and measured verification |
| `set_notes_probability` † | track_index, clip_index, probability?, velocity_deviation?, note_ids?, pitch?, from_time?, time_span?, arrangement?, expect_track_name? | Same guarded primitives; exact ID/range intersection |

Times are source beats, never arrangement positions or rebased loop markers.
Read with limit sufficient for a complete snapshot (maximum 10000); partial
pages never authorize edits. Existing expression and groove stay on native notes.
Lossless full replacement is explicitly unsupported: no clear/re-add fallback.
Legacy clear is destructive deletion, not a replacement protocol.
Advanced add_notes_to_clip accepts probability/deviation only with supported
MidiNoteSpecification/add_new_notes and reliable extended readback; explicit
default values also refuse on unsupported versions. Check expression_verified.
Live acceptance remains pending; these paths are verified with unit fakes only.

MIDI verification scope: verified measures serialized note fields, markers and
association with the groove; opaque per-note expression envelopes are neither
serialized nor fingerprinted. Opaque-only intervening edits are not detected.
The native apply_note_modifications guarantee preserves per-note events; this is
not measured envelope verification. Results explicitly report verification_scope,
fingerprint_scope and opaque_expression_verified=false. Full replacement remains
unsupported. Setter/apply/readback failures consume the plan and require explicit
reconciliation; do not retry. Advanced-add failures report partial measured IDs
or unknown counts after one best-effort readback, never retry or roll back.


### Load-state safety (Remote Script 1.32.0)
URI/preset/batch/kit loads snapshot track identities and arm/monitor states before
selection and report immediate readback. Native state changes or uncertain loads
require reconciliation and block further loads in that script instance; no blind
restoration or global Exclusive Arm preference change is performed. Consecutive
batch/kit loads validate the prior identity/state token before the next mutation.

Automatic restoration of Exclusive Arm transfers and normalization of unexpected
new tracks remain unsupported: native browser events do not identify whether a
loader, plugin or human caused a change. Existing unarmed tracks are never armed
by this code; unexpected native arming is reported, not silently overwritten.
Unchanged-at-return is not proof that asynchronous plugin loading has finished.
Inspect/reconcile manually before deliberately restarting the script; a restart
alone is not evidence of reconciliation. Live acceptance remains pending.

Load failure reporting (1.33.0) retains completed rack/batch evidence across lookup
errors and later preflight refusals. Read-only lookup failure does not imply a
new mutation. URI/path post-dispatch uncertainty is reported with loaded=null,
retry_safe=false and watchdog diagnostics when available; proven preflight
lock/capability/preset/modal refusals retain their existing refusal behavior.

**Follow-up A4-HUMAN-ACK (owner: retained Ableton implementation owner):** replace
restart-only latch recovery with explicit human-confirmed incident acknowledgement
bound to a fresh state/identity fingerprint. Clear only the application-owned
latch/candidate, invalidate stale tokens, and perform no arm/monitor/load writes.
Changed snapshots must refuse; quiet time is not asynchronous-completion proof.
This follow-up is not implemented in the current detection/non-clobber increment.

### Durable export jobs (server-local, native acceptance pending)

| Tool | Purpose |
|---|---|
| start_export_audio † | Enqueue export_audio options with an idempotency key; return without waiting on UI. |
| get_export_job † | Read journal status/results without waiting on the UI worker. |
| cancel_export_job † | Stop queued/pre-render admission; no native render cancellation guarantee. |

Production synchronous export_audio and bounce_track delegate to the same
single worker and wait; their responses include job_id/job_state. For long
exports prefer start_export_audio and poll. Reuse the exact key/request after
connector loss, never blindly start again. Status distinguishes queued,
preflight, configuring, rendering (admitted, not proof of native progress),
verifying, succeeded, failed, cancelled and needs_reconciliation.

The atomic local journal beside the configured Live lock (.exports.json)
retains requests/results. One process owns its non-expiring OS lease; another
active process refuses. Restart marks interrupted work needs_reconciliation,
never resumes/re-exports. Uncertainty and persistence failure block new jobs
and lock release, including save-first handoff. No journal entries are
silently pruned. There is intentionally no automated reconciliation/reset
endpoint yet: preserve journal/output evidence for human review; a restart
alone does not clear an uncertain job. Existing lock ownership checks are
not the stronger authenticated transfer protocol planned separately.

Cancellation before render admission is cooperative; an already configuring
UI worker must reach its next boundary/cleanup first. After admission,
cancellation only records intent and yields reconciliation, retaining output
evidence without deleting files, claiming native cancellation or retrying.
The worker retains ownership through cleanup/verification. Succeeded means
the renderer evidence passed (or explicit dry-run), not real Live acceptance
or proven engine/device readiness. See the verification scope below. GUI interaction
by humans/other helpers is outside this server's queue; do not edit during a job.


### Export verification scope

Exports now require an exact non-null Save filename and destination readback,
then fresh, stable requested PCM/MP3 artifacts, a complete error-free decoder
pass, decoded duration within max(one sample, 0.01 seconds), requested rate and
known PCM depth. Each requested artifact must independently match its probed
container and codec (including PCM subtype precision); an MP3 suffix is not
MP3 evidence. Lossy depth is not applicable only for the requested, verified
MP3 artifact. Unknown or mismatched encoding fails without re-export. Header duration or
file existence alone cannot pass. Verification retains the job/UI owner lease.
UI subprocesses and decoder passes share bounded remaining operation budgets;
expiry after admission requires reconciliation, never an automatic retry.
Only exact quoted requested filenames authorize Replace sheets (PCM and MP3
separately); unrelated/ambiguous sheets remain untouched.

Native limitations remain explicit: engine/device readiness has no verified
probe in this adapter and is reported unknown, not ready. A capable injected
adapter can report engine-off and rate mismatch without changing settings.
Save destination uses the standard AXPathControl AXURL when exposed; otherwise
export refuses before Save. Localized/unrecognized overwrite prompts and
renamed per-track stem overwrite prompts refuse safely. Multi-stem enumeration
still uses existing output discovery, not a proven complete track manifest.
Real UI/short/long export acceptance remains pending; mock tests are not proof
of native readiness, AX layout, or timing. No Remote Script/version change.

### Verified lock handoff (A7)
| Tool | Purpose |
|---|---|
| resume_live_lock † | Resume an existing owner only with trusted proof and generation comparison. |
| transfer_live_lock † | Atomically transfer a save-verified lock, with no unlocked gap. |

The default deployment has no trusted authorization/save-proof provider: these
operations explicitly refuse as unsupported. An embedding may inject
lock_authorization_verifier into app.build_deps; that trusted callback must verify
owner and recipient authorization plus fresh actual current-set save evidence,
bound to the exact record generation. Returning true on caller assertions is NOT
a valid verifier. The verifier must be bounded, must not mutate Live, and must
fail closed on unknown, dirty, mismatched-set or stale proof. No secret is embedded
or accepted as a holder label. Recipient must separately authenticate resume.
Active/reconciliation export jobs block both operations via the existing idle guard.

Acquisition no longer stamps last_edit_ts. It starts with save_state=unknown;
actual modifying commands stamp dirty and advance generation. Old/helper records
remain held and cannot be adopted by matching a name; trusted migration/recovery
is not available by default. Never delete a stale lock to bypass this boundary.
All admitted guarded wire commands and service UI save/render/bounce workflows
reserve the lock record before dispatch. Musical-write admission invalidates save
provenance and advances generation. Transfer, resume (including another process)
and release refuse active or uncertain reservations, even with trusted proof.
Save observation is exclusive against other admitted operations. Timeout, crash
or structured native uncertainty retains a reservation; no automatic clearing,
retry, rollback, expiry or restart-based recovery is provided. A separate trusted
reconciliation mechanism remains deferred. Proven pre-dispatch cancellation and
the existing native no-cue-toggle refusal do not manufacture uncertainty.

Release requires internal save-workflow provenance: advanced file mtime,
explicitly clean after-title, no modal, and equal AXDocument paths before/after
save and again at release. One current structured window observation must also
be readable, explicitly clean (not unknown), and modal-free. Evidence is bound to generation; a new edit invalidates
it. Acquisition time is a separate freshness floor, never an edit stamp.
Caller paths, Spotlight/title matches, native saved=True/verified=None,
greyed-save/no advancement, and unknown dirty state (including bare-title builds)
are insufficient to release. This is bounded UI/file evidence, not an invented
native save-attestation API. Human/external edits outside this cooperating protocol
cannot be serialized; actual Live acceptance remains held.

Default resume/transfer identity authorization is still absent. A7 is a useful
safety subset, NOT full native authenticated handoff/recovery completion.

Pure UI consent/platform, render signature/type and shared local filename/timeout/bounce-mode checks precede mutation reservations. Refusal before admission does not create or clear any reservation; admitted errors retain the existing uncertainty rules.

### Region manifests (1.36.0)
A bounded supported operation is one whole existing MIDI Arrangement clip copied
on its own track into an empty half-open span. Exact adjacency is allowed. Native
Track.duplicate_clip_to_arrangement clones the clip; notes are never cleared or
reconstructed. Manifest records source metadata/offsets, destination, policy,
predicted victims and checkpoint requirement. Copy readback compares serialized
notes (excluding new native note IDs), markers, loops, colour and footprint;
opaque groove/expression are carried by native cloning, not independently proven.

Partial crops, replacement, cross-track/multi-clip atomic edits and requested fade
changes are unsupported. Audio previews refuse because complete fade/warp-envelope
preservation cannot be verified; there is no destructive emulation. Supported
source media hashes are bounded to256MiB per file; unsupported previews do not
claim hashes were verified. Server-held manifests expire on restart/eviction
(max64). Apply requires an observed saved-set checkpoint and fresh source identity,
metadata and destination topology before clone. Checkpoint files are retained;
there is no global undo, rollback claim or replay of a consumed plan. Uncertain
native outcomes latch further region writes pending explicit reconciliation.
Default checkpoints need actual AXDocument and explicitly clean saved-state
observations; unknown state refuses. Real clone/fidelity/rollback acceptance remains
pending. Native version pair and generated bundle are1.36.0; no installation or
Live restart is performed by planning or these unit checks.

### Automation visibility (1.37.0 limited increment)
Device targets use track_index/device_index/parameter and optional track_type /
expect_track_name. Live automation_state distinguishes none, playing and overridden;
missing/unknown probes are not evidence of no envelope. Coverage is state_only,
not curve enumeration and not a requested-range read. Singular/plural device
parameter setters refuse automated values unless allow_automation_override=true;
unknown states always refuse. Plural guards run before any writes. Override consent
never deletes envelopes, reenables automation or authorizes a reload. Older scripts
are version-gated so they cannot silently bypass these guards.

Saved targets use saved_path plus a unique pointee_id. Only unambiguous FloatEvent
Time/Value/optional Id schemas are projected; unknown curves/types refuse. Points
retain beat positions (including sentinel/pre-roll); interpolation is not inferred.
Optional start/end filters events, not evaluated envelope values. Plans show
insert or explicit whole-envelope replace/delete diffs with original file and
unrelated envelope hashes. No saved file is edited. Apply always refuses: native
lossless CRUD/backup/apply and approved reload remain unimplemented, not simulated.
Reload may reenable overrides, whose current Live state a saved file cannot know.
XML parsing is capped at8MiB decompressed; file hashing256MiB. Manifests max64,
process-local. Track/return/master volume/pan, track sends and chain volume/pan now share these
guards; chain parameters are preflighted before any field write. Other direct
setters/non-DeviceParameter controls are not newly classified by this increment;
full A5 scope, evaluated interpolation and Live acceptance remain open.

A5 repair: override intent is strict JSON boolean at MCP ingress and checked
without truthy coercion at controller/service/native boundaries. Saved FloatEvent
nodes must be leaves; unknown nested data refuses rather than disappearing from
preview. Mixer/send/chain guards are implemented, not a native capability deferral.

### Expanded automation state targets (1.38.0)
get_parameter_automation accepts explicit target.kind: device (legacy default),
mixer, send, or chain. Common selectors are track_index, track_type and optional
expect_track_name. Mixer/chain parameter is volume or panning; send requires
send_index; chain also uses device_index/chain_index. Device parameter names/indices
retain existing resolution. Mixed/unknown selectors and negative/boolean indices
refuse. Regular/return/master state reads use the existing track resolvers.
These are read-only automation-state observations, not envelope points, evaluated
values or override reconciliation proof. Unknown state stays unknown. Existing
commands are version-gated at1.38.0 for expanded target semantics; no new tools.

### Output-only saved automation value edits
The optional plan target fields output_path, backup_path and
schema="leaf-float-values-v1" enable a narrowly qualified new-copy writer.
Supply an existing independent byte-identical backup and a NEW absolute output
path. Only operation="replace" with exactly the original point times/count is
supported. Plain UTF-8 XML, self-closing leaf FloatEvent Time/Value/optional numeric
Id only; compressed sets, DTD/entities, changed times, insertion/deletion and
unknown selected curves refuse. This schema is synthetic-fixture qualified, NOT
Live-version certification; output loading/playback remains unverified.

Only selected Value attribute payload bytes change. All other bytes—including
unknown unrelated XML, comments, CDATA and other envelopes—remain unchanged.
Apply revalidates held source/backup/output binding, exclusively creates a new
file and verifies bytes/hash, projected points and unrelated envelopes. Publication
failure retains any artifact and requires reconciliation; consumed plans never
retry or clean up uncertain output. Source/backup originals are never overwritten.
Preview-only plans without writer fields still refuse apply. No live envelope
mutation, interpolation evaluation, reload or override reenable is performed.
The earlier blanket no-file-write description applies to default preview mode;
this explicit output-only mode is the sole exception. Next: reload dry-run/fresh
state reconciliation; evaluated curves need documented schema/semantics fixtures.

### Reload risk dry-run and observation comparison
plan_automation_reload hashes a candidate copy and reads1..64 live automation
state targets through the existing gated read command. Its server-held plan expires
in300seconds; max64plans, lost on restart/eviction. reconcile_automation_reload
rehashes the copy and freshly reads the same targets. Changed/unknown observations
are flagged; prior overridden=True becomingFalse is a POSSIBLE reenable risk, not
proof that a reload occurred. Caller data cannot replace held observations.

Neither tool loads, saves, edits, reenables or authorizes anything. Scope is selected
non-atomic state observations, not all parameters, stable native identities, set
identity, saved-file compatibility or a reload safety certificate. Even unchanged
states return reload_authorized=false and identity_verified=false. Hash/expiry
mismatches refuse before/after reads; unreadable/inconsistent observations remain
unknown, never equivalent to safe. Real reload execution and native acceptance
remain separately gated; this protocol supplies evidence, not the missing loader.

### Saved curve schema inspection (not playback evaluation)
inspect_automation_curve retains FloatEvent Time/Value/Id and complete sets of
CurveControl1X/Y,CurveControl2X/Y. Unknown attributes, partial control sets, nested
event content, ambiguous mappings, nonfinite values and duplicate/unordered times
refuse. Max10000events,1024sample requests; existing8MiB XML/hash bounds apply.
Root version fields are reported verbatim as provenance, NEVER native certification.

At an exact nonnegative stored timestamp, stored_value is returned. evaluated_value
is always null: between-point interpolation, extrapolation and negative sentinel
interpretation are unsupported. No absence-of-controls=linear assumption. Source
bytes remain unchanged; no writes, Live probes or override claims. This qualifies
lexical fields for inspection, not audible playback or the source's entire format.

Evidence: dawtool's Ableton parser observes four curve-control attributes but does
not establish their normalization/segment ownership; its tempo sentinel discussion
is explicitly uncertain. See https://github.com/offlinemark/dawtool/blob/master/dawtool/daw/ableton.py .
A real version-qualified fixture with known sampled envelope values/timing semantics
is still required before implementing playback-equivalent evaluation.

### MCP structured output (A6 transport increment)
The default server now returns one canonical object in structuredContent and no
duplicated text block. Existing JSON-object presenters become that object directly,
not a JSON string under result. Legacy prose-only successes use {message:...};
this compatibility adapter does not invent raw model fields that those controllers
currently discard. Lists/scalars use explicit items/value object adapters.

ABLETON_MCP_OUTPUT_MODE=legacy retains exact historical text behavior; both emits
compact JSON text of the SAME structured object for clients requiring text fallback.
Programmatic build_app(output_mode=...) selects the same modes. Direct Python tool
calls remain legacy, keeping text goldens stable. The default is structured.
Controller/service exceptions return isError=true with {code,message,details};
codes distinguish capability, lock, audio authority, render refusal, invalid argument
and operation failure. FastMCP argument-validation errors occurring before controller
invocation remain SDK-owned in this increment. No successful error-text guessing.

This delivers transport structure, not full A6: names/compact filters, projections,
return/master parity, cursor continuity and final serialized UTF-8 byte budgets
remain next implementation work. Unicode transport is preserved; no new budget
promise yet. Existing max_chars snapshot estimates must not be treated as final
encoded MCP byte limits. Native script/tool inventory unchanged.

### Parameter read projections

The parameter-list tool accepts optional names (exact, case-sensitive names;
null selects all, an empty list selects none) and compact (boolean).
Duplicate native names remain separate rows in native order. Opt-in responses
report requested scope, missing names, returned count and omitted parameter fields;
the original parameter count remains the total. Compact rows retain index, name,
raw value and display value (unknown fields are null). Default responses retain
the legacy shape. Projection is server-side, not a reduction in native read cost.
This increment does not yet enforce final MCP encoded snapshot budgets or cursors.

### Snapshot UTF-8 budgets and cursors

Structured/both snapshot calls can opt into max_bytes, a positive byte cap on
serialized CallToolResult JSON (including content/structuredContent/isError;
excluding the outer JSON-RPC id envelope). This overrides legacy max_chars.
Whole-track pages expose requested_scope, omitted_fields, omitted_tracks and
next_cursor. Repeat the same scope and budget with that cursor. Any observed
snapshot/scope change invalidates it; cursors do not certify native identity or
atomic capture. Both mode budgets include duplicated compatibility text.
An envelope or single track too large is an explicit error, never an oversized
successful page. Refusal errors can exceed an impossibly small requested cap.
Returns/master remain included and honor existing parameter flags. Pagination
bounds response bytes, not native read cost. Legacy max_chars remains a historical
character heuristic, not a strict transport guarantee. This opt-in increment does
not yet add missing native device-active/monitoring/clip-mute/note-count fields.

Remote Script 1.39 adds snapshot monitoring_state on regular/return/master rows
and muted plus note_count on clip metadata. Unreadable monitoring/mute is null;
notes excluded means note_count is null, not a fabricated zero or a hidden
full-note read. Device is_active already uses the shared nullable state reader.
These fields are additive; older scripts remain callable but may omit them.
No new native arguments/capabilities or raised historical command floor are needed.

Remote Script 1.40 distinguishes snapshot notes_status: read, unavailable,
not_requested, or not_applicable. Failed or interrupted native MIDI note reads
return notes=null and note_count=null with notes_error; successful empty reads
return [] and 0. Snapshot strict reads do not silently downgrade a failed extended
reader to legacy data. The established query window remains 0..clip.length+1;
read means that query succeeded, not lossless enumeration outside the window.

Byte-budget snapshot projection also normalizes older-script optional state:
missing/unrecognized monitoring or boolean state becomes null, never false.
Missing device/clip collections on returns/master stay null, not fabricated empty
lists; these serializers may not expose clips at all. Requested notes/parameters
missing from older replies remain unknown. Notes/parameter exclusion applies to
regular/return/master rows and nested rack devices, even if an older reply included
them. Unrelated fields are preserved; malformed collection layouts refuse rather
than claiming completeness. This normalization is opt-in with max_bytes; legacy
and unbudgeted response shapes remain unchanged.
