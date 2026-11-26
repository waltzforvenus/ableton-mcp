# CLAUDE.md

Guidance for Claude Code (and any other agent) working in this repository.

## What this project is

An MCP server that drives Ableton Live. Two halves that must stay in step:

- `src/ableton_mcp/` — the MCP server package. Speaks stdio to the client and
  JSON-over-TCP to Live, structured as a small MVC with one composition root:
  - `app.py` — composition root: `Settings`, `Deps`, `build_app()`, `main()`.
    The only module that reads the environment, configures logging, or wires
    objects together.
  - `tools.py` — **controllers**: all 102 `@tool` functions, kept in upstream's
    order; coerce arguments, call the service, hand the result to a presenter.
  - `services.py` — **model** (domain): `AbletonService`, one method per wire
    command; its `_send` consults the registry, so gating happens in one place.
  - `presenters.py` — **view**: every model-facing string, success and error
    alike (`ERROR_PHRASES` / `ERROR_RENDERERS`). No response text anywhere else.
  - `connection.py` — **model** (transport): the only module that imports
    `socket`; lazy connect, retry, drop-on-timeout, reconnect under the lock.
  - `commands.py` — **model** (metadata): the `CommandSpec` registry — one row
    per wire command: modifying flag, timeout, capability gate, version floor.
  - `handshake.py` — `ScriptHandshake` (cached script info + the capability/
    version gate, `CapabilityError`) and `LEGACY_CAPABILITIES`.
  - `remote_script_install.py` — the installer, including `--sync-bundle`.
  - `audio_analysis.py` / `warp_vision.py` — **model** (warp vision): read a
    clip's audio file in the server process (numpy STFT, transients, tempo,
    band energy, the warp-marker beat map, proposals, PNG rendering) and
    orchestrate analyze/propose over `get_clip_warp_markers`' state. Their
    deps are the optional `analysis` extra, imported lazily — every other
    tool works without them. Local file reads only; nothing leaves the
    machine.
- `remote_script/__init__.py` — a MIDI Remote Script that runs *inside*
  Ableton Live, hosting the socket server and touching the Live Object Model
  (LOM). Single file, dispatch-table driven (`COMMANDS`), with
  `SCRIPT_CAPABILITIES` derived from the table's advertise flags.
- `src/ableton_mcp/bundled_ableton_remote_script/remote_script_init.py` — the
  generated copy of the Remote Script that the installer ships. Never edited,
  always regenerated (see below).

This is a **permanent fork** of [ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp).
Upstream is merged in for genuine features; it is not a base we are trying to
get back onto. **Read `docs/UPSTREAM.md` before any merge or release** — it
holds the file mapping, the standing conflict resolutions, the port
checklist, and the release checklist.

## Hard rules

### 1. No telemetry. Ever.

This fork exists because upstream collects prompts, MIDI, and track and clip
names. Do not add, restore, or make it easy to re-add:

- analytics, usage statistics, crash or error reporting, "anonymous" metrics
- any persistent installation or user identifier
- any outbound network call that is not to the local Ableton socket
- LOM listeners that queue the user's manual UI actions for collection
- a `user_prompt` parameter (or similar) on tools, which exists only to route
  the human's prompt text somewhere

The server's only outbound connection is TCP to `ABLETON_HOST:ABLETON_PORT`.
Keep it that way. If a dependency would introduce a network client, that is a
reason to reject the dependency.

Verification, which should stay silent:

```bash
grep -ri "telemetry\|supabase\|analytics\|dataset\|trajectory" src/ableton_mcp/ remote_script/
```

This rule is mechanized in `tests/test_no_telemetry.py`: the grep above as a
test, plus no HTTP-client imports anywhere, plus `socket` importable only in
`connection.py`, the Remote Script, and `scripts/push_notes.py` (the direct
socket client; `scripts/` is scanned too). The test failing is the rule being
broken, not the test being wrong.

Reading Live's state and returning it *to the user's own MCP client* is fine —
that is what `get_session_snapshot` does. The line is whether data leaves the
user's machine to anywhere but their own client.

### 2. The socket binds to loopback

`HOST = "127.0.0.1"` in the Remote Script. That socket executes arbitrary
commands with no authentication, so binding it to `0.0.0.0` exposes Live to the
whole local network. Upstream binds to `0.0.0.0`; when merging, check this has
not come back. `tests/test_remote_script_ast.py::test_socket_binds_to_loopback`
enforces it.

### 3. Licensing

- Upstream code stays MIT. `LICENSE.MIT` is verbatim and its copyright notice
  must never be stripped.
- New work in this fork is dual-licensed **GPL-3.0-or-later OR
  AGPL-3.0-or-later**. Do not add code under terms incompatible with that.
- Do not vendor third-party code without checking the licence is compatible
  with GPLv3/AGPLv3 and recording it.
- License texts are verbatim FSF originals. Never reformat, reflow, or edit
  them.

## Things that will bite you

### Duplicate definitions are silent

FastMCP registers tools by name: two tools with the same name means the later
one silently wins. The same trap exists inside the Remote Script's class
body: Python keeps the later `def`, which is how the 2026-08 merge shipped
broken device-parameter handlers while its commit message said ours were
kept. Both are now caught mechanically — `tests/test_tool_surface.py` pins
`list_tools()` against the `tests/data/tool_names.txt` snapshot (102 unique
names), and `tests/test_remote_script_ast.py` rejects any method defined
twice in any class. If you deliberately add or rename a tool, update the
snapshot (and the README) in the same commit.

### The Remote Script exists twice

`remote_script/__init__.py` is canonical.
`src/ableton_mcp/bundled_ableton_remote_script/remote_script_init.py` is the
copy that `ableton-mcp-install-script` actually installs into Live. **Never
edit the bundled copy by hand** — regenerate it after changing the canonical
one:

```bash
uv run ableton-mcp-install-script --sync-bundle
```

They drifted once already, and the bundled copy silently reverted the loopback
bind and dropped every tool this fork adds. `tests/test_bundle_identity.py`
now fails the suite on any byte difference, so a forgotten regeneration
cannot ship.

### Adding a command end to end

The layers a new tool touches, in order — each step has a guardrail that
fails if you skip it (`docs/UPSTREAM.md` §3 is the long form):

1. Controller in `tools.py`: an `@tool` function that coerces boundary
   arguments, calls one service method, returns via a presenter. Controllers
   never touch the wire (`test_mvc_contract` checks).
2. Service method in `services.py` calling `self._send("your_command", {...})`.
3. Presenter in `presenters.py` named after the tool (or an `as_json`
   alias), plus an `ERROR_PHRASES` entry or `ERROR_RENDERERS` renderer —
   `test_mvc_contract` requires both.
4. Registry row in `commands.py`: `modifying=True` if it changes Live's
   state (that is also what selects the longer socket timeout), an explicit
   `timeout` if the script side needs more than the default (keep ≥ 5 s
   headroom over the script's queue timeout — `test_cross_half_contract`
   checks), and `gated=True` unless the command is in the
   `LEGACY_CAPABILITIES` floor (ungated non-floor rows are rejected).
   A modifying row is also checked against the shared Live lock
   (`live_lock.py`) by `_send`; leave `lock="write"` (the default) unless
   the command must never stamp an edit (`"check"`, as `save_set`) or must
   never be refused (`"exempt"`, as `stop_playback`).
5. Remote Script: a `_your_command` handler with every parameter defaulted
   (dispatch calls `**params`), plus a `COMMANDS` table row
   `(method, modifying, queue_timeout, advertise)`. All handlers run on Live's main thread via the bounded update_display queue;
   state changes set modifying=True. The modifying sets of the two
   halves must agree (`test_cross_half_contract`). Set `advertise`
   deliberately — it feeds `SCRIPT_CAPABILITIES`, which is part of the
   `get_script_info` wire response and pinned by a snapshot test.
6. Regenerate the bundled copy (`--sync-bundle`).
7. If the wire surface changed, bump `SCRIPT_VERSION` (Remote Script) and
   `EXPECTED_REMOTE_SCRIPT_VERSION` (`remote_script_install.py`) **together,
   in the same commit** — they are compared at runtime and a guardrail
   enforces the pairing. Add `min_script_version` to the registry row when
   an older script would *advertise* the command but serve it wrongly, so
   users get the friendly "re-run `ableton-mcp-install-script`" message
   instead of an in-Live error.
8. README tool-reference row († if fork-only), `tests/data/tool_names.txt`,
   and golden cases for the new tool.

### The Remote Script runs on Ableton's Python

It imports `_Framework` and cannot be imported or linted outside Live as-is.
Assume an older Python and a restricted environment: prefer `hasattr` probes
and fall-backs over assuming a modern Live API, as the existing code does with
`remove_notes_extended` versus `remove_notes`.

Its handler *logic* is testable now: `tests/fake_ableton/` stubs
`_Framework`, imports the real canonical script, and drives the real
`_process_command` dispatch against a fake LOM (`FakeSong` and friends). The
fakes are capability-configurable (`FakeLiveConfig`), so both branches of
every `hasattr` fallback can be exercised — when you add a probe for an
older Live, add a fake toggle and test both sides
(`tests/test_remote_script_behavior.py` has the patterns).

## Working in the repo

```bash
uv sync --extra dev
uv run pytest          # no Ableton and no network required
uv run ableton-mcp     # starts the server on stdio
uv build               # must succeed; needs setuptools>=77 for PEP 639
```

Tests mock the Ableton socket, so they run anywhere. Keep it that way — a test
that needs Live running is a test that never runs. CI
(`.github/workflows/ci.yml`) runs the suite and the build on every push and
PR; the guardrails only guard if they run.

`scripts/` holds two things that are deliberately not tests: `smoke_live.py`
drives a real Live through the production stack, and `push_notes.py` pushes
note JSON files straight to the Remote Script socket in guarded chunks (for
bulk MIDI that should not pass through the model's context; a generator
writes the files, the script moves them). `push_notes.py` is standard
library only so it runs with nothing installed, and
`tests/test_push_notes.py` covers it against a fake socket peer. Both
scripts are scanned by the no-telemetry guardrail.

**Goldens are the frozen interface.** `tests/goldens/*.json` record, per
tool, the wire exchange and the exact response string. They change only when
tool behavior deliberately changes: edit `tests/golden_cases.py` if needed,
run `uv run python tests/record_goldens.py`, and put the goldens diff in that
commit's message — the diff *is* the behavior change. Never hand-edit a
golden to make a test pass.

**The version pair moves together.** `SCRIPT_VERSION` and
`EXPECTED_REMOTE_SCRIPT_VERSION` bump in the same commit, so any revert keeps
them consistent. `__version__` in `src/ableton_mcp/__init__.py` is read from
package metadata — the only version to bump by hand is `pyproject.toml`'s, at
release time.

**Merges and releases** follow `docs/UPSTREAM.md`: the standing conflict
resolutions, the port checklist, the version-skew policy, and the release
steps (including telling users to re-run the installer when the script
version changes — see `CHANGELOG.md`).

## Conventions

- Docstrings on tools are read by the model at runtime, so they are part of
  the interface. Say what the tool is *for* and when to prefer it over a
  neighbouring tool, not just what its arguments are. Ported upstream tools
  keep upstream's docstrings byte-identical, to keep merge diffs aligned.
- Response text lives **only** in `presenters.py` — success strings and error
  wording alike. A controller or service that formats a user-visible string
  is a layering bug, and the contract tests treat it as one.
- `commands.py` is the single source of command metadata. Never introduce a
  second list of "modifying" or "long-running" commands anywhere; the
  transport and the gate both consume the registry.
- Comments explain *why*, particularly where Live's API forced the shape of
  the code. Match the surrounding density; the existing code is well
  commented about Live's quirks and that is worth preserving.
- Prefer clamping into a valid range over erroring, where a musician would
  reasonably expect "as low as this goes" — see `set_device_parameter`.
- Commit messages explain the reasoning, not just the change. When resolving
  a merge, record which side won and why. When goldens change, quote the
  diff.
- Keep the README's tool reference in step when adding or removing a tool,
  and mark fork-only tools with † (`test_readme_sync` checks presence).
- Keep `CHANGELOG.md` current: user-facing changes and internal changes
  separated, and a loud note whenever an entry requires users to re-run
  `ableton-mcp-install-script`.
- When merging upstream, run through the hard rules above before committing:
  telemetry, loopback bind, duplicate tools, bundled script — and run the
  suite *before* the manual sweeps, per `docs/UPSTREAM.md`.


## Consolidated delivery guidance (26 November 2026)

Overnight implementation is authorized. One retained implementation owner,
independent exact-head reviewer and serial landing owner coordinate the same
canonical task list; this is a public-safe mapping snapshot, not a second queue.
Original A/F/R integrity and delivery work precedes dependent extensions.
Reviewed/unit-tested implementation is not native acceptance or production
completion. Required CI and actual native acceptance on safe disposable fixtures
must precede authorized main promotion. Save-verified serial Live custody,
rollback, original music preservation and explicit audio consent remain required.
Never infer permission to alter unrelated production work, expose the network,
upload data, install models or incur spend. Preserve all hard rules above.

Existing A5 primitives cover the SYN08 implementation obligation; native curve
qualification and broader acceptance remain open. A6 structured transport and
projection increments are implemented/reviewed in stages; prose migration and
native partial-read fidelity remain incomplete. No expanded ID below is newly
native-qualified by this documentation. Record exact revision, tests, dependency
and native evidence in the canonical task list before advancing a status.

| ID | Status | Canonical action or precise dependency |
|---|---|---|
| SYN01 | Queued | Shared nested device resolver |
| SYN02 | Queued | Typed Wavetable adapter; SYN01 |
| SYN03 | Queued | Modulation matrix; SYN01/native proof |
| SYN04 | Queued | A8 ordered verified transactions |
| SYN05 | Queued | A9/A10 recall; SYN04 |
| SYN06 | Partly covered / queued | A6 projections covered in reviewed increments; metadata/search delta remains |
| SYN07 | Queued | Typed Simpler/sample adapter |
| SYN08 | Covered obligation / acceptance open | Consolidated into A5; no separate engine |
| B01 | Queued | F2 scene extension |
| B02 | Queued | Recording/capture; input/audio custody |
| B03 | Queued | GroovePool/clip associations |
| B04 | Queued | Local MIDI interchange over A2 |
| B05 | Queued | Transport extension; external Link decision separate |
| B06 | Queued | F4/A3 pitch/warp controls |
| B07 | Queued | Bounded opt-in lifecycle observations |
| B08 | Partly covered / queued | Existing registry/A6; typed capability reasons delta |
| B09 | Queued / proof dependency | Take lanes; full comp requires API proof |
| B10 | Queued | Shared mixer crossfader/cue |
| B11 | Queued | Typed track/view conveniences |
| B12 | Queued | ONE shared C08 recipe engine |
| C01 | Queued / dependency | A8/SYN04/B07 foundations |
| C02 | Product dependency | Named hardware/port/workflow needed; no hardware purchase or visual-editor clone assumed |
| C03 | Queued / dependency | SYN04/05+A5 morph semantics/timing proof |
| C04 | Queued / dependency | A9/A10 local index first; embeddings require model/license/resources decision |
| C05 | Queued | Existing metrics/reference composition |
| C06 | Product dependency | Select transcription/stem task plus licensed model/resources/quality target; no install assumed |
| C07 | Product dependency | Select frontend/performance need; local MIDI versus LAN exposure requires explicit decision |
| C08 | Queued | Shared B12 recipes using existing host |
| C09 | Queued / platform dependency | Existing native matrix; Windows requires provisioned host/license/tester |

Use one implementation per capability: A2 underpins B04/B12/C08, A3 underpins
B09/arrangement recipes, A4 protections underpin B02, A5 underpins SYN08/C03,
A6 underpins SYN06/B08, A7/R1/R2 underpin B07/C01, A8/SYN04 underpin ordered
transactions/B03/C01/C03, A9/A10 underpin SYN05/C04, F2 underpins B01,
F4 underpins B06, F1 underpins B05, and F6/R3 underpin B10. SYN01 is the shared
nested resolver. B12/C08 share one bounded deterministic recipe engine using
the existing host. C05 reuses existing analysis/render metrics; hardware and
frontend work are adapters over this bridge, never duplicate servers.
Preserve public aliases, legacy adapters, focused readers, safety layers and
bundle mirrors; no source deletion is justified by duplication suspicion alone.
