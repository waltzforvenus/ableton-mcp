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
grep -ri "telemetry\|supabase\|dataset\|trajectory" MCP_Server/ AbletonMCP_Remote_Script/
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

Where both projects had independently written the same tool, the merge kept one
of each — `clear_clip_notes` folded into upstream's `clear_notes_from_clip`,
and this fork's parameter-by-name device tools kept over upstream's
index-only versions. Duplicates were not harmless: the MCP framework registers
tools by name, so the loser was silently overwritten depending on file order.

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

1. **Ableton Remote Script** (`AbletonMCP_Remote_Script/__init__.py`) — a MIDI Remote Script for Ableton Live that creates a socket server to receive and execute commands
2. **MCP Server** (`MCP_Server/server.py`) — a Python server that implements the Model Context Protocol and connects to the Remote Script

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

---

## Usage

### Starting the Connection

1. Ensure the Ableton Remote Script is loaded in Ableton Live
2. Make sure the MCP server is configured in your client
3. The connection is established automatically when you interact with the assistant

### Capabilities

- Get session, track, clip and device information
- Create and modify MIDI and audio tracks
- Create full song arrangements in Arrangement View
- Create, edit, clear and trigger clips
- Mix: volume, pan, mute, sends, return tracks, device parameters by name
- Route tracks and set up shared effect buses
- Control playback, count-in and tempo
- Load instruments and effects from Ableton's browser
- Save the set

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
