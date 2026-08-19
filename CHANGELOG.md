# Changelog

Notable changes to this fork. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are the
`pyproject.toml` package version, with the Remote Script's own version
(`SCRIPT_VERSION`) called out where it changes — the two halves upgrade
separately, and a script change means **re-run
`ableton-mcp-install-script` and restart Ableton Live**.

## [Unreleased]

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
