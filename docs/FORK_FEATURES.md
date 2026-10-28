# What this fork adds

This is a permanent fork of [ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp),
not the upstream PyPI package. Upstream retains credit and its MIT licence;
this fork's changes are GPL-3.0-or-later OR AGPL-3.0-or-later.

## Scope and availability

This summary describes `dev` through PR #16 (Remote Script 1.18.0, 64 MCP
tools). It is a branch snapshot, not a release announcement or a claim that
all tools have been validated inside Live. See the [tool reference](../README.md#tool-reference)
for arguments and the [changelog](../CHANGELOG.md) for subsequent changes.

## Additions over the original foundation

| Area | What this fork provides |
|---|---|
| Mixing and routing | Volume, pan, mute, solo, arm and monitoring controls; return tracks, sends and input/output routing. Solo changes report all soloed tracks. |
| Device control | Parameter lookup by name or index, batch parameter writes, device deletion, return/master targeting where supported, and hosted-plugin factory preset listing/selection. Presets depend on what each plugin exposes to Live. |
| Arrangement editing | Bulk placement, arrangement move/copy/delete/trim, locator navigation/deletion and Back to Arrangement. Loop-phase guards refuse potentially destructive restamping by default. |
| Clip and MIDI workflows | Clip gain and warp controls, arrangement-aware MIDI reads/writes, expected-note-count validation and a direct bulk-note helper (`scripts/push_notes.py`). |
| Warp vision | Waveform/spectrogram and beat-grid images; transient, tempo and alignment measurements; dry-run warp suggestions; batch marker edits with readback. Requires the optional analysis dependencies for audio analysis. |
| Musical context | Live 12 song root/key, scale and Scale Mode reads/writes, also included in session snapshots where available. |
| Targeting safeguards | Optional expected-track-name guards prevent writes to a renumbered track; capability/version checks explain when the Remote Script needs upgrading. |

Warp suggestions are not a judgement of musical feel: quantising an already
well-played take can remove intentional timing. Suggestions default to dry run;
applying them requires an explicit tool argument.

## Privacy and maintenance differences

- Removed telemetry, dataset collection, prompt capture and related dependencies.
  Results still go to the MCP client you choose; this does not make a remote AI
  client local or change that client's data handling.
- Remote Script binds to loopback (`127.0.0.1`), not every network interface.
- Server split into controllers, services, presenters, transport and a command
  registry, with a fake-Live test harness and interface snapshots.
- Bundled script identity, version pairing, duplicate definitions, tool coverage
  and no-telemetry constraints have automated regression checks.
- CI builds wheels and source distributions; script upgrades require reinstalling
  the matching Remote Script and restarting Live.

## Selected upstream fixes retained or ported

The fork keeps the bundled installer, capability handshake, session snapshots
and upstream arrangement foundation. Recent ports include safe arm-state reads
for non-armable tracks (#128), browser recursion and byte-safe socket handling
(7e90cd6), per-command socket timeouts and failed-connect cleanup (#139), queued
command cancellation on timeout (#140), and expected-error logging (#141).
These are upstream contributions, not claimed as original fork inventions.
See [the upstream merge playbook](UPSTREAM.md) for integration rules.

## Validation and work not yet shipped in this snapshot

Offline tests and CI exercise fake Live objects and local audio; they do not
prove the real Live API behaves identically. In particular, the new warp-marker
constructors, movement semantics, units and readback timing still need real-Live
validation. The latest solo and song-scale additions also await the combined
Live check. No Live test or installation is implied by this documentation.

The audit backlog is not an implemented feature list. At this snapshot, do not
assume the repository provides enforced cross-task locking, verified save and
checkpoint recovery, automated export/bounce, or a transport authorization gate.
Those are follow-up work. The existing `save_set` API attempt is not a reliable
save path on Live builds that expose no save API.

## Installing the development snapshot

For contributors intentionally evaluating `dev` (not a stable-release recommendation):

```bash
git clone --branch dev https://github.com/waltzforvenus/ableton-mcp
cd ableton-mcp
uv sync --extra dev --extra analysis
```

This only prepares a checkout. Installing the Remote Script and restarting
Live are separate actions; follow the [installation guide](../README.md#installation)
when ready to update the running application. Use this fork's git URL or checkout,
not the unrelated upstream `ableton-mcp` PyPI build.
