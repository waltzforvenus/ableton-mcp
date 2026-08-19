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
- [Troubleshooting](#troubleshooting)
- [Technical Details](#technical-details)
- [Limitations & Security Considerations](#limitations--security-considerations)
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

Everything upstream does, this fork also does — it is merged, not diverged from.
On top of that:

### Added tools

Upstream exposes 37 MCP tools; this fork exposes 46. The mixer, device control
and routing tools are new here:

| Area | Tools |
|---|---|
| **Mixer** | `set_track_volume`, `set_track_pan`, `set_track_mute`, `set_track_arm`, `set_track_monitoring` |
| **Devices** | `delete_device` — plus `get_device_parameters` / `set_device_parameter` extended to address parameters *by name* and to reach return tracks via `track_type` |
| **Sends & buses** | `create_return_track`, `set_track_send` — one shared reverb instead of a copy per track |
| **Routing** | `set_track_routing`, `get_track_routing` |
| **Clips & tracks** | `set_clip_gain`, `delete_track` |
| **Transport & session** | `back_to_arrangement`, `set_count_in`, `save_set` |

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

Then **restart Ableton** (or re-select the AbletonMCP control surface) so Live
loads it. Re-run the command after upgrading — the server logs a warning when
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
<summary><b>Manual fallback locations (User Remote Scripts)</b></summary>

- **macOS:** `/Users/[Username]/Library/Preferences/Ableton/Live XX/User Remote Scripts/AbletonMCP/`
- **Windows:** `C:\Users\[Username]\AppData\Roaming\Ableton\Live x.x.x\Preferences\User Remote Scripts\AbletonMCP\`
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

### Tool reference

All 46 tools the server exposes. Tools marked **†** are added by this fork and
are not present upstream.

#### Session & info

| Tool | Arguments | Description |
|---|---|---|
| `get_session_info` | — | Get detailed information about the current Ableton session |
| `get_track_info` | `track_index` | Get detailed information about a specific track in Ableton |
| `get_session_snapshot` | `include_notes`?, `include_params`? | Read the whole project state in one call |
| `get_remote_script_info` | — | Report Ableton Remote Script version and capabilities (handshake) |
| `set_tempo` | `tempo` | Set the tempo of the Ableton session |
| `save_set` † | — | Save the open Live Set, if this Live build exposes a save through its API |

#### Tracks

| Tool | Arguments | Description |
|---|---|---|
| `create_midi_track` | `index`? | Create a new MIDI track in the Ableton session |
| `create_audio_track` | `index`? | Create a new audio track in the Ableton session |
| `create_return_track` † | — | Create a new return track — a shared effects bus that any track can send to |
| `delete_track` † | `track_index` | Delete a track from the Ableton session, along with all clips on it |
| `set_track_name` | `track_index`, `name` | Set the name of a track |

#### Mixer

| Tool | Arguments | Description |
|---|---|---|
| `set_track_volume` † | `track_index`, `value`, `track_type`? | Set a track's mixer volume |
| `set_track_pan` † | `track_index`, `value`, `track_type`? | Set a track's stereo panning |
| `set_track_mute` † | `track_index`, `mute` | Mute or unmute a track. Useful for auditioning parts in isolation |
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
| `create_audio_clip` | `track_index`, `clip_index`, `path` | Create a new audio clip in an audio track's clip slot by importing a file |
| `delete_clip` | `track_index`, `clip_index` | Delete the clip in the given clip slot, freeing it for reuse |
| `set_clip_name` | `track_index`, `clip_index`, `name` | Set the name of a clip |
| `set_clip_gain` † | `track_index`, `clip_index`, `gain`, `arrangement`? | Set one audio clip's gain, leaving every other clip on the track untouched |
| `get_clip_notes` | `track_index`, `clip_index` | Read all MIDI notes from a Session-view clip |
| `add_notes_to_clip` | `track_index`, `clip_index`, `notes` | Add MIDI notes to a clip |
| `clear_notes_from_clip` | `track_index`, `clip_index` | Remove all MIDI notes from a Session clip |

#### Arrangement

| Tool | Arguments | Description |
|---|---|---|
| `switch_to_arrangement_view` | — | Switch Ableton's main window to the Arrangement view |
| `get_arrangement_clips` | `track_index` | List all clips placed in the Arrangement timeline for a track |
| `duplicate_to_arrangement` | `track_index`, `clip_index`, `destination_time` | Copy a Session-view clip into the Arrangement timeline |
| `set_arrangement_clip_name` | `track_index`, `clip_index`, `name` | Set the name of a clip placed in the Arrangement timeline |
| `set_arrangement_time` | `time` | Move the arrangement playhead to a specific position |
| `create_locator` | `name`, `time` | Create a named locator (cue point) in the Arrangement at a beat position |
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

### Example Commands

| Prompt |
|---|
| *"Create an 80s synthwave track"* |
| *"Create a Metro Boomin style hip-hop beat"* |
| *"Create a full arrangement with an intro, buildup, drop, breakdown, and outro"* |
| *"Create a new MIDI track with a synth bass instrument"* |
| *"Add a reverb return track and send the drums to it at 20%"* |
| *"Pull the Dry/Wet on the reverb down to 15%"* |
| *"Create a 4-bar MIDI clip with a simple melody"* |
| *"Add a jazz chord progression to the clip in track 1"* |
| *"Set the tempo to 120 BPM"* |
| *"Save the set"* |

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

## Limitations & Security Considerations

- Complex musical arrangements may need to be broken into smaller steps
- Designed to work with Ableton's default devices and browser items
- The Remote Script socket executes commands without authentication. It is bound
  to loopback, so only processes on your own machine can reach it — do not
  forward or rebind that port
- Always save your work before extensive experimentation

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
