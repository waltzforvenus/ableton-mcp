# CLAUDE.md

Guidance for Claude Code (and any other agent) working in this repository.

## What this project is

An MCP server that drives Ableton Live. Two halves that must stay in step:

- `src/ableton_mcp/` — the MCP server package. Speaks stdio to the client and
  JSON-over-TCP to Live, structured as a small MVC with one composition root:
  - `app.py` — composition root: `Settings`, `Deps`, `build_app()`, `main()`.
    The only module that reads the environment, configures logging, or wires
    objects together.
  - `tools.py` — **controllers**: all 46 `@tool` functions, kept in upstream's
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
`connection.py` and the Remote Script. The test failing is the rule being
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
`list_tools()` against the `tests/data/tool_names.txt` snapshot (46 unique
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
5. Remote Script: a `_your_command` handler with every parameter defaulted
   (dispatch calls `**params`), plus a `COMMANDS` table row
   `(method, main_thread, queue_timeout, advertise)`. State changes must run
   on Live's main thread (`main_thread=True`); the modifying sets of the two
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
