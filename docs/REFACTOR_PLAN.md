# Architecture refactor plan — dependency injection + MVC layering

Status: **proposal** (no production code changed yet).
Scope: both halves of the project — the MCP server package and the Ableton
Remote Script — plus tests, packaging, CI, and docs.

This plan was produced by a full survey of the codebase, its git history, and
the pinned `mcp` SDK, followed by three competing architecture designs scored
by independent review passes. Every factual claim below (bugs, line numbers,
SDK behavior) was verified against the repo at the commit this plan lands on.

---

## 1. Why now — what the current structure already cost us

The refactor is not cosmetic. The 2026-08-17 upstream merge (`4878234`) and
telemetry-removal commit (`e57c257`, PR #2) shipped four regressions, and
every one of them lived in a place **no test or import ever exercises** — a
class body only instantiated inside Live, a dispatch `elif` chain, a
console-script entry point:

1. **`set_device_parameter` and `get_device_parameters` are broken at
   runtime, continuously, since the merge.** The `AbletonMCP` class defines
   four methods twice; Python silently keeps the later definition. The merge
   commit message explicitly says the fork's device-parameter versions were
   kept ("upstream's would have failed on the wire") — but the resolution was
   executed only in the dispatch code. In the class body, upstream's later
   definitions win, with incompatible signatures:
   - `_get_device_parameters` — line 840 (fork: `track_type`, display values)
     loses to line 2515 (upstream: 2-arg). Dispatch passes 3 args → `TypeError`.
   - `_set_device_parameter` — line 876 (fork: name-or-index resolution,
     clamping, `track_type`) loses to line 2607 (upstream: `parameter_index`
     only). Dispatch passes 5 args → `TypeError`.
   - `_delete_clip` (782 vs 1403) and `_create_audio_track` (715 vs 1803) are
     also duplicated, benignly (compatible signatures).
2. **The `ableton-mcp` console script has been an `ImportError` since
   `e57c257`.** `main()` sat at the end of `server.py`, directly after the
   last telemetry tool; the deletion hunk swept through end-of-file and took
   the entry point with it. `pyproject.toml` still targets
   `MCP_Server.server:main`. This breaks every README client-config snippet,
   the Dockerfile `CMD`, and smithery.yaml.
3. **Dead code from the dispatch-table union:** unreachable
   `set_device_parameter` / `get_device_parameters` / `create_audio_track`
   branches; duplicate entries inside the modifying-command list; branches for
   `get_browser_categories` / `get_browser_items` that call methods which do
   not exist anywhere in the file.
4. **Silent contract drift between the halves:** the server's
   modifying-command list and the Remote Script's main-thread list disagree
   (`set_arrangement_clip_name` runs on Live's main thread but gets the short
   10 s server timeout); `SCRIPT_CAPABILITIES` advertises the two broken
   commands; `map_rack_magnitude` / `inspect_rack` are dispatchable but
   unreachable and uncapabilitied; and `load_browser_item` — the command
   three load tools actually send — appears in **neither**
   `SCRIPT_CAPABILITIES` nor the legacy set, so any future capability gate
   on it would fail even against a current install.

The bundled-copy `HOST = "0.0.0.0"` reversion was caught *manually* inside
the same merge. Manual vigilance is the only thing that has ever protected
any of these invariants. The architecture below exists to replace that
vigilance with structure and tests.

## 2. Goals and frozen contracts

**Goals**
- Real dependency injection: no module-global singletons, no service locator
  inside tool bodies; every dependency constructed in one composition root
  and injectable in tests without `monkeypatch`.
- An explicit MVC-style layering of the server half: controllers (MCP tool
  functions), model (domain services + transport + command metadata), view
  (presenters owning every model-facing string, including error text).
- A Pythonic package layout: `src/` layout, PEP 8 names, an importable
  package with zero import-time side effects.
- Mechanical guardrails for every failure class the fork has actually
  shipped, enforced by CI (the repo currently has none).
- A comprehensive test suite that runs entirely without Ableton — including
  a **mock Live instance** that lets the real Remote Script's handler logic
  execute and be tested for the first time — and that is written against
  behavior (wire protocol + response text), so it passes unchanged through
  every architectural step.
- Keep upstream merges tractable — this is a permanent fork that still runs
  `git merge upstream` for genuine features.

**Frozen contracts — must not change**
- Console-script **names**: `ableton-mcp`, `ableton-mcp-install-script`
  (referenced verbatim by users' MCP client configs, README, Docker,
  smithery, and the handshake's own error text). Entry-point *targets* are
  internal and will move.
- Distribution name `ableton-mcp` (the `uvx --from git+…` flow resolves
  through it).
- Env vars `ABLETON_HOST`, `ABLETON_PORT` (default stays `localhost`),
  `ABLETON_MCP_SKIP_SCRIPT_INSTALL`.
- The wire protocol (command names, param shapes, response envelope) — except
  the deliberate fixes called out in §6 PR5.
- Tool names, tool docstrings (they are the model-facing interface), and tool
  response text byte-for-byte — same exceptions.
- The installed artifact layout in Live:
  `User Remote Scripts/AbletonMCP/__init__.py` (+ `.py.bak` backups).
- All CLAUDE.md hard rules: no telemetry, loopback bind, licensing, tests
  never require Live or network.

**Non-goals (deferred, §9)** — protocol framing/request IDs, async tool
rewrite, exposing the orphan rack commands, DI-container libraries (rejected
outright: zero new runtime dependencies).

## 3. Target architecture

### 3.1 Package layout

```
ableton-mcp/
├── pyproject.toml                   # src layout; script NAMES unchanged, targets → ableton_mcp.app:main etc.
├── src/ableton_mcp/
│   ├── __init__.py                  # __version__ only — zero imports, zero side effects
│   ├── __main__.py                  # python -m ableton_mcp
│   ├── app.py                       # composition root: Settings, Deps, build_app(), lifespan, main()
│   ├── connection.py                # MODEL/transport: AbletonConnection/AbletonClient — the ONLY place `socket` is imported
│   ├── commands.py                  # MODEL/metadata: CommandSpec registry — one row per wire command
│   ├── handshake.py                 # MODEL: ScriptHandshake (instance state) + CapabilityError + LEGACY_CAPABILITIES
│   ├── services.py                  # MODEL/domain: AbletonService — one method per command + multi-step orchestrations
│   ├── presenters.py                # VIEW: per-tool success renderers + centralized error translation
│   ├── tools.py                     # CONTROLLERS: all 46 @tool functions, upstream-ordered; TOOLS registry
│   ├── remote_script_install.py     # installer (logic unchanged; paths updated)
│   └── bundled_ableton_remote_script/AbletonMCP_init.py   # generated copy (installer --sync-bundle)
├── AbletonMCP_Remote_Script/__init__.py   # canonical Remote Script — single file, dispatch-table driven (path unchanged)
├── tests/                           # see §5 — guardrails, goldens, unit, transport, fake-Ableton, full-stack
│   └── fake_ableton/                # the mock Live instance (fake LOM + _Framework stub)
├── docs/UPSTREAM.md                 # upstream-merge playbook (§7)
└── .github/workflows/ci.yml         # pytest + build on push/PR
```

### 3.2 The MVC layering of the server half

**Controllers — `tools.py`, deliberately one file.** All 46 `@tool` functions
in upstream's order with byte-identical docstrings, so upstream diffs stay
side-by-side diffable. A controller does exactly three things: coerce/validate
arguments (e.g. `set_device_parameter`'s int-parsing of `parameter` stays
here), delegate to the service, hand the result to a presenter. A single
`@tool` decorator supplies the mechanics every tool currently hand-rolls —
catch, log, delegate the *wording* to the View:

```python
def tool(fn):
    @functools.wraps(fn)          # verified: FastMCP 1.29 follows __wrapped__ —
    def wrapper(ctx, *a, **kw):   # name, docstring, schema and call_tool all round-trip
        try:
            return fn(ctx, *a, **kw)
        except CapabilityError as e:
            return str(e)                       # the friendly "re-run installer" text
        except Exception as e:
            logger.error(presenters.error_text(fn.__name__, e))
            return presenters.error_text(fn.__name__, e)
    TOOLS.append(wrapper); return wrapper

@tool
def set_device_parameter(ctx: Context, track_index: int, device_index: int,
                         parameter: str, value: float, track_type: str = "regular") -> str:
    """<byte-identical docstring>"""
    param: Any = parameter
    try: param = int(str(parameter).strip())
    except (TypeError, ValueError): pass
    result = _deps(ctx).service.set_device_parameter(track_index, device_index, param, value, track_type)
    return presenters.set_device_parameter(result)
```

**Model — `services.py`, one `AbletonService` class, not a package.** The
honest cohesion test: ~40 of 46 methods are "build a param dict, send" —
six domain files would average seven near-empty methods each and sextuple
the files an upstream port touches. One class, section-commented in the same
domain order as `tools.py`, ~450 lines. Its private `_send` consults the
`CommandSpec` registry, so capability/version gating happens in exactly one
place instead of today's five ad-hoc call sites:

```python
def _send(self, name, params=None):
    spec = COMMANDS[name]
    if spec.gated:
        self._handshake.require(name, spec.min_script_version)  # raises CapabilityError
    return self._client.send_command(name, params or {})
```

Multi-step orchestration is Model logic: `load_drum_kit`'s three wire calls
and the decisions between them (bail if the rack failed, filter
`is_loadable`, pick the first kit) move into a service method returning a
plain stage-tagged dict; the presenter owns every one of its strings,
including the intermediate-failure ones. Plain dicts, no result-type
hierarchy — this is the only tool that needs a structured outcome.

**View — `presenters.py`, one file.** Pure `dict -> str` functions, ~3 lines
each; the ~10 JSON-returning tools share literally one
(`json.dumps(result, indent=2)`). Error translation is centralized here —
`ERROR_PHRASES` maps tool name → the exact phrase in today's hand-rolled
strings ("setting device parameter", "arming track", …) so
`error_text(tool, e)` reproduces today's returns byte-for-byte;
`get_browser_items_at_path`'s three message-sniffing error formats become
the one custom renderer in `ERROR_RENDERERS`. This closes the seam review
flagged as unowned: error text was drifting per-tool; now it has a home.

**Anti-ceremony rules, stated up front so the layering never becomes its own
problem:** a presenter is allowed to be one f-string; a service method is
allowed to be one `_send` line; exactly one `Protocol` exists
(`AbletonClient` — the seam that carries the test suite); handshake,
services, presenters stay concrete; no ABCs, no plugin registries beyond the
`TOOLS` list and `COMMANDS` dict, no validators module; zero new
dependencies.

### 3.3 DI backbone — composition root + FastMCP lifespan injection

Verified against the pinned SDK (`mcp` 1.29.0 satisfies `>=1.9,<2.0`):
`FastMCP(lifespan=…)` threads the yielded value into
`ctx.request_context.lifespan_context`, including for **sync** tools; tools
can be registered programmatically with `mcp.add_tool(fn)` (docstring becomes
the description, preserving docstring-as-interface); bound methods and
closures register cleanly; `functools.partial` does **not** (fails in
`from_function`) and must not be used.

```python
# app.py — the ONLY module that reads env, configures logging, or wires objects
@dataclass(frozen=True)
class Settings:
    host: str
    port: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        return cls(env.get("ABLETON_HOST", "localhost"),
                   int(env.get("ABLETON_PORT", "9877")))

class AbletonClient(Protocol):
    def send_command(self, command_type: str, params: dict | None = None) -> dict: ...

@dataclass
class Deps:                      # what the lifespan yields; what tests fake
    client: AbletonClient
    handshake: ScriptHandshake
    service: AbletonService      # model layer, constructor-injected with client + handshake

def build_deps(settings: Settings) -> Deps: ...   # the one wiring function

def build_app(settings: Settings | None = None,
              deps: Deps | None = None) -> FastMCP:
    ...  # lifespan constructs (or adopts) Deps, performs best-effort handshake,
         # yields Deps; registers every controller via mcp.add_tool

def main() -> None:
    logging.basicConfig(level=logging.INFO)   # moved out of import time
    build_app().run()
```

Controllers reach dependencies through one helper,
`_deps(ctx) -> Deps` (reads `ctx.request_context.lifespan_context`). That hop
is the idiomatic FastMCP channel at this pin — the honest residue of context
plumbing — but everything behind it is constructor-injected, and tests build
a `Deps` full of fakes and hand controllers a two-line stub ctx. No
`monkeypatch`, no module globals anywhere: `_ableton_connection`, the
module-level `mcp`, and `script_handshake`'s `_lock`/`_script_info` globals
all cease to exist. `ableton_mcp/__init__.py` holds `__version__` only;
importing the package does nothing (this also retires the
`python -m` double-import warning the Dockerfile documents).

### 3.4 Command metadata — one registry instead of three lists

Today "which commands modify state" lives in a list inside
`AbletonConnection._send_command_locked`, again in the Remote Script's
membership list (the two already disagree), and timeout overrides live in two
more dicts. Replace the server side with one table the transport *consumes*:

```python
# commands.py — server-side single source of truth
@dataclass(frozen=True)
class CommandSpec:
    modifying: bool = False          # → 15 s socket timeout instead of 10 s
    timeout: float | None = None     # explicit override (create_audio_clip: 65 s)
    gated: bool = False              # require_capability gate (by command name)
    min_script_version: str | None = None   # see §4

COMMANDS: dict[str, CommandSpec] = {...}
```

The transport derives its timeout policy from this table, and the service's
`_send` derives gating from it (§3.2) — the registry is consumed, never
duplicated.

The Remote Script cannot import this package inside Live, so the two halves
are reconciled **by test, not by import**: a guardrail AST-extracts the
Remote Script's dispatch table and asserts the modifying/main-thread sets and
timeout headroom (server ≥ script + 5 s) match `COMMANDS` exactly.

### 3.5 Transport layer — moved verbatim, then hardened

`connection.py` absorbs today's `AbletonConnection` +
`get_ableton_connection()` logic (lazy connect, `MSG_PEEK` liveness probe,
3-attempt retry, request/response serialization under one `RLock`) as
instance state. Three deliberate hardenings ride along, each cheap and
server-side only:

1. **Reconnect under the lock.** FastMCP runs sync tools in worker threads;
   today's check-then-replace connection swap can race two threads into
   duplicate sockets. The liveness check + reconnect moves inside the same
   `RLock` that already serializes sends.
2. **Drop the socket after any send/receive timeout.** The wire protocol has
   no request IDs, so a late reply to a timed-out command would otherwise be
   read as the answer to the *next* command. Closing and reconnecting after
   every timeout makes desync structurally impossible without touching the
   protocol or the Remote Script.
3. **Invalidate the handshake cache on reconnect.** The capability/version
   cache currently lives as long as the process; if the user restarts Live
   with a different Remote Script mid-session, gating answers go stale. The
   client calls `handshake.invalidate()` whenever it establishes a new
   socket; the next gated call re-handshakes.

The transport becomes independently testable with a real `socketpair`/
in-process fake TCP server (no Ableton, no external network — loopback only,
same as the production socket), covering: chunked JSON reassembly, timeout →
socket drop, reconnect-then-retry, and the error envelope.

### 3.6 Remote Script — single file, dispatch table, derived capabilities

The Remote Script **stays one file**. A package split or a build-step
concatenator would turn every upstream merge into a port and complicate the
installer's upgrade path for zero testability gain (it still can only be
AST-checked outside Live). The structural fix is internal — both `elif`
ladders collapse into one literal table:

```python
COMMANDS = {
    # name:               (method,               main_thread, queue_timeout, advertise)
    "create_audio_clip":  ("_create_audio_clip", True,        60.0,          True),
    "get_clip_notes":     ("_get_clip_notes",    False,       None,          True),
    "inspect_rack":       ("_inspect_rack",      True,        None,          False),
    ...
}
SCRIPT_CAPABILITIES = sorted(n for n, row in COMMANDS.items() if row[3])
```

The `advertise` flag matters: the dispatchable set (51 commands today) is
deliberately larger than the advertised set (38), and `SCRIPT_CAPABILITIES`
is part of the `get_script_info` wire response §2 freezes. Naive
`sorted(COMMANDS)` derivation would grow the advertised list and resurrect
the orphan commands; the flag keeps the advertised set exactly equal to the
PR5 list while still making drift impossible (a test asserts the equality).

Dispatch becomes: look up → `getattr(self, method)(**params)` — Python then
enforces arity, which is precisely the check whose absence let the duplicate
definitions ship. Defaults move onto method signatures. The table is a dict
literal, so the guardrail tests read it with `ast.parse` without importing
`_Framework`. The installer, the bundled-copy mechanism (`--sync-bundle`
already exists in `remote_script_install.py` — it becomes the *documented*
regeneration path, backed by a byte-identity test), the backup behavior, and
the threading model are unchanged.

## 4. Version-skew and capability policy

Capability gating alone cannot protect users on the 1.7.0 script from the
two repaired commands: **1.7.0's `SCRIPT_CAPABILITIES` already advertises
`get_device_parameters` and `set_device_parameter`**, so a name-based gate
passes and the user still gets the raw in-Live `TypeError`. `CommandSpec`
therefore carries `min_script_version`, and the gate checks *both*:

- capability name present (catches legacy scripts), **and**
- `script_version >= min_script_version` (catches 1.7.0 for the repaired
  commands — they get the friendly "re-run `ableton-mcp-install-script`"
  message instead of a socket-level TypeError).

Skew policy, stated for all cases for the first time:
- **Script older than expected** → gated commands return the installer
  message; ungated legacy commands keep working.
- **Script newer than expected** (user ran the installer, then rolled the
  package back) → log a warning, proceed. The script's command surface is a
  superset; nothing the older server sends is missing. The handshake reports
  `up_to_date: false` either way, and `get_remote_script_info` shows both
  versions.
- **No handshake at all** (Ableton wasn't running at startup — the common
  case on a work machine): the gate answers only from a *successful*
  handshake. If none is cached, it attempts one lazily before gating; if
  Live is still unreachable, the gate **passes** and the send fails with the
  truthful connection error — never a misleading "re-run the installer"
  against a script nobody has seen. (Today's module-global cache gets this
  wrong: `None` cache falls back to legacy capabilities and mislabels
  up-to-date installs.)
- Rollback rule: `SCRIPT_VERSION` and `EXPECTED_REMOTE_SCRIPT_VERSION` bump
  **in the same commit** (now enforced by a guardrail test), so reverting the
  PR reverts both.

## 5. Testing architecture

Two requirements shape this section. First, **no test may ever require
Ableton, or any network beyond an in-process loopback socket** — development
happens on machines that cannot run Live, and CI has neither Live nor
egress. That is already a hard rule here and every level below honors it.
Second, **the suite must be invariant across the architectural migration**:
tests are written against *behavior* — the wire protocol (which commands,
with which params, in which order) and the response text the model sees —
never against module paths or internal structure. Every structural PR in §6
must pass the same suites byte-for-byte; the only code allowed to change
with the architecture is three named adapters (each small and reviewed as
such), listed below — nothing else in the test tree may be edited by a
structural PR.

The suite is a pyramid of five levels (0–4):

**Level 0 — Guardrails** (structure-independent; each annotated with the
commit hash of the shipped regression it would have caught; details in the
table below).

**Level 1 — Server unit tests with a fake client.** The existing 10 tests,
migrated fixture-only (build `Deps` around the existing `FakeConnection`,
hand tools a two-line stub ctx; assertions untouched), plus the
**characterization ("golden") suite**: for each of the 46 tools, checked-in
fixtures — `tests/goldens/<tool>.json` — recording, per case, the arguments,
the expected **ordered wire exchange** (`(command, canned_response)` pairs;
the fake asserts command names and order, so `load_drum_kit`'s three-call
sequence is itself frozen), and the exact response string, for success *and*
failure paths. Goldens are recorded against the **current monolith before
any restructuring**, so they guard every later PR. The golden runner's
`call_tool(name, args, fake) -> str` adapter is **migration adapter #1**:
pre-refactor it monkeypatches `get_ableton_connection` *and seeds the
handshake state* (the five gated tools consult the module-global cache, so a
fresh pytest process would hit the missing-capability early return — exactly
as today's suite neutralizes via `require_capability`); post-refactor it
builds `Deps` with an all-capabilities `ScriptHandshake`. Goldens change in
exactly two commits — the PR5 fixes and the PR10 gating flip — and only in
gated-failure cases (the repairs are Remote-Script-side; no success-path
string changes), with the diff quoted in those commit messages.

**Level 2 — Transport tests.** `connection.py` against a scripted in-process
TCP peer (`socketpair`/loopback): chunked JSON reassembly, timeout →
socket-drop-and-reconnect, reconnect-under-lock under concurrent senders,
handshake invalidation on reconnect, error envelope translation.

**Level 3 — The mock Ableton (`tests/fake_ableton/`).** The piece that makes
Remote Script *logic* testable for the first time. Feasibility is verified:
the script's only Live-bound import is `_Framework.ControlSurface`, so the
harness installs a stub `_Framework` package into `sys.modules`, imports the
**real canonical Remote Script**, constructs `AbletonMCP` without running
`__init__` (no socket, no threads), and wires three things onto it: a
`FakeSong`, an inline `schedule_message` (runs main-thread tasks
synchronously), and a captured `log_message`. Tests then call
`_process_command({...})` — the real dispatch, the real handlers — and
assert on the response envelope and on the fake's mutated state.

`FakeSong` is a plain-Python model of exactly the LOM surface the script
touches (the complete inventory is Appendix B): tracks with clip slots,
clips and notes, devices with parameters (`name`/`value`/`min`/`max`/
`str_for_value`), mixer devices with sends, routing, cue points, a browser
tree. Crucially it is **capability-configurable**: the script is full of
`hasattr` probes for older Lives (`remove_notes_extended` vs `remove_notes`,
`ClipSlot.create_audio_clip` only ≥ 12.0.5, the `_save_set` candidate chain,
routing attribute variants), and the fake can present or withhold each API
generation — so the fallback paths, which have never been executed outside
Live, get tests on both branches. First targets: the restored
`_set_device_parameter` (clamping, name-or-index, track_type),
`_get_device_parameters`, `_delete_clip` semantics, note read/write/clear
round-trips, and the snapshot serializers.

**Level 4 — Full-stack, still no Live.** Two compositions close the loop:
(a) a real FastMCP session over
`mcp.shared.memory.create_connected_server_and_client_session(build_app(deps=…))`
— proving registration, lifespan-context injection, and Context plumbing
survive SDK bumps (the one failure class direct calls cannot see); and
(b) **end-to-end against the mock Ableton**: the `Deps` client is a shim
(migration adapter #2) that feeds each command dict straight into the fake
Ableton's `_process_command` and returns its result — MCP client → tools →
service → registry → real Remote Script dispatch → real handlers → FakeSong,
with not a single component mocked except Live itself. A handful of
scenario tests run here (build a track, create a clip, write notes, read
them back, set a device parameter and see it clamped), because they cross
every contract the two halves share.

**Guardrail suite** — all runnable with `uv run pytest`, no Live, no
network:

| Test | Catches | Would have caught |
|---|---|---|
| `test_entrypoints` — import every `[project.scripts]` target, assert callable | dead console script | `e57c257` |
| `test_remote_script_ast` — no method defined twice in any class; every dispatch-table row names an existing method with a `**params`-compatible signature; no duplicate dispatch literals; `HOST == "127.0.0.1"`; `SCRIPT_VERSION == EXPECTED_REMOTE_SCRIPT_VERSION` | duplicate defs, arity breaks, loopback reversion, version-pair drift | `4878234` (all four duplicates) |
| `test_bundle_identity` — byte-equality canonical vs bundled | bundled-copy drift | the pre-fork drift episode |
| `test_no_telemetry` — CLAUDE.md's grep as a test, plus: `socket` may be imported **only** in `connection.py` and the Remote Script; no `urllib`/`http.client`/`requests` imports anywhere | telemetry reintroduction, novel egress paths | upstream's collection code |
| `test_cross_half_contract` — server `COMMANDS` vs RS table (modifying sets equal; timeouts: server ≥ script + 5 s); every `send_command("X", …)` literal in the server ∈ `SCRIPT_CAPABILITIES ∪ LEGACY`; derived-capabilities set tracked against an explicit snapshot (so `inspect_rack`/`map_rack_magnitude` are neither silently dropped nor resurrected) | cross-half drift | the `set_arrangement_clip_name` timeout gap |
| `test_tool_surface` — `list_tools()` returns exactly 46 unique names matching a checked-in snapshot; every docstring non-empty | silent tool loss/rename (FastMCP's later-definition-wins) | the hazard CLAUDE.md documents |
| `test_readme_sync` — README tool-reference table names ⊇ tool snapshot, † markers on fork-only tools | doc drift | standing CLAUDE.md convention |
| `test_installer` — run `install_remote_script` against a `tmp_path` "User Remote Scripts" dir; assert install / unchanged / backup-then-update behavior and that the installed bytes equal the bundle | packaging moves silently breaking the one command users must run | — (new protection) |

The two cross-half guardrails necessarily track the code's shape: their PR1
implementations parse **today's** structures (the `elif` ladders and
membership lists), and they are rewritten against the Remote Script table in
PR6 and the `commands.py` registry in PR8. Those two rewrites are
**migration adapter #3** — expected, named, and the only guardrail edits any
structural PR may contain.

Three guardrails from the MVC split keep a half-done upstream port from
compiling quietly: every `_send("X", …)` literal in `services.py` must have a
`COMMANDS` row; every `SCRIPT_CAPABILITIES` entry minus a checked-in
`KNOWN_ORPHANS` set must be reachable from some service method (catches
"merged their Remote Script, forgot our server side" — the likeliest miss,
since the Remote Script half merges automatically); every name in `TOOLS`
must have a presenter and an `ERROR_PHRASES` entry or custom renderer.

**CI** — `.github/workflows/ci.yml`: `uv sync --extra dev`, `uv run pytest`,
`uv build`, on push + PR. Without CI every guardrail above is advisory; this
is what turns them into enforcement. The repo has no `.github/` today; CI
lands in PR1 with the first tests.

## 6. Migration sequence

Four phases, eleven small PRs. Each PR leaves `uv run pytest` green and
observable behavior unchanged — except the deliberate fixes in Phase 2 and
the gating improvement in PR10, each called out in its own PR — and each is
independently revertable. Version-bump events: exactly one (PR5). The
ordering principle: **the entire safety net exists before anything
structural moves**, so goldens and the mock Ableton guard the rename, the
DI work, and the MVC split alike. (Phases can be collapsed into fewer PRs
if preferred; the internal ordering still holds.)

**Phase 1 — Safety net (test-only, plus one five-line fix)**

- **PR1 — CI + guardrail suite.** `.github/workflows/ci.yml` and the Level-0
  table above, implemented against today's structures (adapter #3 note in
  §5). The tests detecting the five known live defects — dead entry point,
  duplicate methods/arity, the two dead-branch classes, and
  `load_browser_item` missing from both capability lists — land as `xfail`
  with links to this plan, proving the suite *detects* each bug before
  anything fixes it. No production change.
- **PR2 — Restore `main()`.** Five lines in the current
  `MCP_Server/server.py` (pre-rename), matching the hunk `e57c257` deleted.
  Flips the entrypoint xfail. Fixes: console script, Docker, smithery, every
  README snippet.
- **PR3 — Golden characterization suite.** Recorded against the current
  monolith (Level 1): 46 tools × success + failure cases, wire order
  asserted. The runner uses the monkeypatch adapter for now.
- **PR4 — The mock Ableton.** `tests/fake_ableton/` (Level 3): `_Framework`
  stub, `FakeSong` per Appendix B, harness that imports the real Remote
  Script and drives `_process_command`. Behavior tests for the
  device-parameter pair land encoding the *intended* (fork) behavior and
  `xfail` against today's broken duplicates; tests for notes, clips,
  serializers, and the `hasattr` fallback branches land green.

**Phase 2 — Repair (the Remote Script surgery, under the net)**

- **PR5 — Deduplication + the single version bump (1.8.0).** Execute the
  2026-08 merge's *stated* intent: device-parameter pair → fork versions
  restored (`track_type`, name-or-index resolution, clamping, display
  values) with upstream's `old_value` echo grafted on; `_delete_clip` →
  upstream's no-op-on-empty semantics kept, fork's `deleted_clip_name` echo
  restored; `_create_audio_track` → keep one. Delete the unreachable
  branches, the nonexistent-method branches (`get_browser_categories`,
  `get_browser_items`), and the duplicate membership entries. Add
  `set_arrangement_clip_name` to the server's modifying list (10→15 s) and
  `load_browser_item` to `SCRIPT_CAPABILITIES` (it ships in 1.8.0 either
  way, so this is additive, and it unblocks ever gating the load tools).
  Gate the repaired commands with `min_script_version="1.8.0"` (§4) — the
  vehicle at this point is an **interim version-compare added to the
  existing `script_handshake.py`** (string-returning, like today's
  `require_capability`, including §4's lazy-handshake-when-unknown rule);
  PR8 migrates it into `ScriptHandshake.require` + `CapabilityError`, and
  that migration is part of adapter #3's review scope. Bump
  `SCRIPT_VERSION` + `EXPECTED_REMOTE_SCRIPT_VERSION` together; regenerate
  the bundle via `--sync-bundle`. Flip the remaining xfails (guardrails
  *and* the PR4 behavior tests). Correct the README's device-parameter
  claims. **Deliberate behavior change: two commands go from guaranteed
  TypeError to working.** Mid-migration risks: a 1.7.0-script user with the
  new server gets the friendly min-version message — strictly better than
  today's TypeError; and until PR8's reconnect-invalidation lands, a
  handshake cached from a pre-upgrade Live session can gate stale — the
  lazy-handshake rule bounds this to "restart the MCP server after
  upgrading the script", which the installer's output already tells users
  to do. Reverting this PR reverts both version constants together.
- **PR6 — Dispatch table.** Both `elif` ladders → the literal table with the
  per-row `advertise` flag (§3.6); `SCRIPT_CAPABILITIES` becomes derived
  from the advertised rows, with a test asserting the derived set equals
  PR5's explicit list exactly — the flag is what keeps the derivation from
  widening the wire response or resurrecting the orphans. The cross-half
  guardrail's parser is rewritten for the table (adapter #3). The PR4
  behavior tests must pass unchanged — they call `_process_command`, not
  the ladder. No wire change, no bump. Revertable without touching the PR5
  fixes.

**Phase 3 — Restructure (behavior frozen by Phases 1–2's net)**

- **PR7 — Mechanical rename.** `git mv MCP_Server src/ableton_mcp`;
  pyproject entry-point targets + `[tool.setuptools]` stanzas updated;
  tests import `ableton_mcp`; `conftest.py` sys-path hack deleted (editable
  install covers it); docs sweep (README component paths, CLAUDE.md greps
  and checklists, Dockerfile comment, `.gitignore`'s `MCP_Server/secrets.py`
  line). No `MCP_Server` compatibility shim: verified nothing outside the
  repo imports the package — the console-script names are the real
  contract. New modules created from PR7 onward carry no per-file license
  headers — repo convention keeps licensing in `pyproject.toml` and the
  LICENSE files, and new fork work is dual GPL-3.0/AGPL-3.0 under that
  declaration. Behavior identical; goldens byte-identical.
- **PR8 — DI composition root + Model extraction.** `app.py`
  (Settings/Deps/build_app/main + `__main__.py`), `connection.py` with the
  three §3.5 hardenings + Level-2 socketpair tests, `commands.py` registry,
  `handshake.py` `ScriptHandshake` class (absorbing PR5's interim
  version-compare; the string-returning gate becomes `CapabilityError`).
  Module globals deleted; fixtures migrate off `monkeypatch` (golden
  adapter #1 flips to `Deps`; the registry-side guardrail parser rewrite
  completes adapter #3). Level-4 full-stack tests land here, including
  end-to-end against the mock Ableton. Behavior identical (the hardenings
  change failure-mode behavior only, each covered by a new Level-2 test).
- **PR9 — MVC extraction.** `tools.py` / `services.py` / `presenters.py`
  split per §3.2. Goldens must pass byte-identical; the three port-guardrail
  tests (services↔registry↔capabilities↔presenters coverage) land here.
  Behavior identical to the byte.

**Phase 4 — Polish**

- **PR10 — Gating data flip.** Turn on `gated=True` for the sendable
  non-legacy commands (Appendix A; includes `load_browser_item`, gateable
  only because PR5 added it to `SCRIPT_CAPABILITIES`) — now a pure
  registry-row change.
  **Deliberate behavior improvement for legacy-script users only:** friendly
  "re-run the installer" text instead of raw "Unknown command" socket
  errors. Golden cases added for the gated path.
- **PR11 — `docs/UPSTREAM.md` + CLAUDE.md rewrite + release checklist.**
  §7's playbook; CLAUDE.md's "adding a command touches four places" section
  rewritten for the new layout (§7 lists the new places); CHANGELOG
  introduced, with the 1.8.0 entry telling users a script reinstall is
  required and why.

## 7. Upstream-merge playbook (`docs/UPSTREAM.md`)

Upstream is two single files. After this refactor, their Remote Script still
maps 1:1 onto ours (single file, table-driven dispatch); their `server.py`
maps onto our `tools.py` (controllers, deliberately one file and
upstream-ordered) plus a short mechanical port into services/presenters for
each changed tool. The playbook records:

- The standing per-feature resolutions the last merge only kept in a commit
  message: device parameters — *ours wins, delete theirs entirely, including
  the class body* (the lesson of `4878234`); `delete_clip` — theirs + name
  echo; `HOST` — ours, always; bundled copy — never merge, regenerate;
  telemetry tools — delete on sight, then check what shared a file tail with
  them (the lesson of `e57c257`).
- The port procedure for an upstream tool change (controller diff →
  service/presenter delta → registry row → RS table row → capability gate →
  regenerate bundle → bump pair if the wire surface changed).
- The mid-migration risk register (old script × new server, client configs,
  Docker/smithery, rollback semantics per PR).
- The closing step: **run the guardrail suite before the manual sweeps** —
  it mechanically catches all four historical failure classes.

## 8. Release mechanics

- Publish order for 1.8.0: merge Phases 1–2 (PRs 1–6) → tag/publish the
  package (the wheel now has a working entry point and the repaired script)
  → Docker/smithery images rebuild from the tag, not the branch.
- The handshake's version-mismatch warning is a server-side log line users
  driving the server through an MCP client never see; the
  `get_remote_script_info` tool remains the discoverable check, and the
  min-version gate (§4) is what actually reaches the model as text.
- CHANGELOG entry per release; the 1.8.0 entry is the user-facing "re-run
  `ableton-mcp-install-script`, restart Live" notice.

## 9. Deferred — scheduled debt, not silent omissions

- **Orphan commands** `inspect_rack` / `map_rack_magnitude`: decide expose
  (as tools, with the missing server-side modifying entries) or delete (from
  the RS table). Tracked by the capability-snapshot test either way.
- **Protocol request IDs / framing** — the real fix for reply desync; the
  §3.5 drop-on-timeout mitigation removes the urgency. Would require a
  version bump and both halves; design when a protocol change is next needed
  anyway.
- **`load_instrument_or_effect` wire command** — advertised and dispatched
  but never sent (the tool of that name sends `load_browser_item`); keep for
  third-party clients, note in UPSTREAM.md, revisit on the next protocol
  change.

## Appendix A — current-state findings inventory

*(verified in-session; line numbers at the planning commit)*

- Duplicate methods: `_create_audio_track` 715/1803, `_delete_clip` 782/1403,
  `_get_device_parameters` 840/2515, `_set_device_parameter` 876/2607;
  later definition wins in all four; dispatch calls the losing signatures for
  the device-parameter pair (TypeError at `_process_command` lines ~291 and
  ~389).
- Unreachable dispatch: `get_device_parameters` at 538, `set_device_parameter`
  at 549 (32-line dead block incl. its own queue), `create_audio_track` at
  401; membership-list duplicates `create_audio_track` (294, 299),
  `delete_clip` (296, 304); server-list duplicate `set_device_parameter`
  (server.py 131, 136).
- Branches calling nonexistent methods: `get_browser_categories` (515),
  `get_browser_items` (518).
- Missing `main()` in `MCP_Server/server.py`; pyproject targets it; verified
  `ImportError`.
- Import-time side effects: env reads (server.py 12–13), `logging.basicConfig`
  (16–17), FastMCP instance + 46 decorator registrations (228+), package
  `__init__` importing `.server`.
- Global state: `_ableton_connection` (server.py 234), handshake module
  globals (script_handshake.py 13–15).
- Gating: 5 tools gated, 24 sendable non-legacy commands ungated;
  `set_arrangement_clip_name` missing from the server modifying list;
  `create_audio_track` legacy-fragile; `load_browser_item` sent
  (server.py 1083, 1315, 1340) but present in neither `SCRIPT_CAPABILITIES`
  nor `_LEGACY_CAPABILITIES`; a handshake cache of `None` (Ableton not
  running at startup) silently falls back to legacy capabilities,
  mislabeling up-to-date installs.
- Baseline: 10/10 tests pass; bundled copy byte-identical; 46 tools, no
  duplicate tool names.

## Appendix B — the mock Ableton's API contract

The complete Live API surface the Remote Script actually touches (extracted
by full read; line numbers at the planning commit). `FakeSong` and friends
implement exactly this — nothing more — and every hasattr/getattr probe
below is a **toggle** the fake can flip to emulate different Live versions.

**Song** (`self._song`): `tempo` (r/w), `signature_numerator`/`_denominator`,
`tracks` (len/index/iterate/`.index()`), `return_tracks`, `master_track`,
`is_playing` / `current_song_time` (also written: 1467, 1641, 1649) /
`song_length` / `loop` / `loop_start` / `loop_length` (all via the
`_safe_song_property` getattr at 605–611), `create_midi_track(index)`,
`create_audio_track(index)` (−1 = append), `create_return_track()`,
`delete_track(index)`, `start_playing()`, `stop_playing()`,
`back_to_arranger` (w), `count_in_duration` (w — **read-only on Live
12.3.2**, so the fake's setter must be able to raise), `metronome` (w),
`cue_points` (iterate; cue: `.time` r, `.name` r/w), `set_or_delete_cue()`,
`scenes` (scene: `name`, `tempo`, `is_triggered` via safe_attr),
`view.selected_track` (w).

**Track**: `name` (r/w), `mute` (r/w), `solo`, `arm` (r/w; getattr at 2579),
`can_be_armed`, `current_monitoring_state` (int 0/1/2), `has_audio_input`,
`has_midi_input`, `clip_slots`, `devices`, `delete_device(i)`,
`arrangement_clips`, `duplicate_clip_to_arrangement(clip, beats)`,
`mixer_device.volume` / `.panning` (fetched via `getattr(mixer, field)`) /
`.sends` (all parameter-shaped: `value` r/w, `min`, `max`,
`str_for_value(v)`), routing fields (`{in,out}put_routing_{type,channel}` +
`available_*` lists, all getattr; option objects expose `display_name` with
`str(obj)` fallback). `_resolve_track` maps `track_type`
"master"/"main" → `master_track`, "return"/"send" → `return_tracks[i]`.

**ClipSlot**: `has_clip`, `clip`, `create_clip(length)`,
`create_audio_clip(path)` (hasattr-gated — the Live 12.0.5 probe),
`delete_clip()`, `fire()`, `stop()`. **Clip**: `name` (r/w), `length`,
`is_playing`, `is_recording`, `is_midi_clip`/`is_audio_clip`, `color`,
`start_time`/`end_time` (arrangement), `gain` (r/w, clamped 0–1) +
`gain_display_string`, safe-attr set (`looping`, `loop_start`, `loop_end`,
`warping`, `warp_mode`, `pitch_coarse`, `pitch_fine`, `launch_mode`,
`file_path`), `warp_markers` (marker: `beat_time`/`sample_time` → `.time`
fallback).

**Note APIs — the fake must offer BOTH generations, with an asymmetry that
must be preserved:** writes are **legacy-only** (`set_notes(tuples)` called
unconditionally at 1277; `add_new_notes` is never used); reads prefer
`get_notes_extended(from_pitch, pitch_span, from_time, time_span)` → note
objects (`pitch`, `start_time`, `duration`, `velocity`, `mute`, plus
per-field-hasattr optionals `probability`, `velocity_deviation`,
`release_velocity`, `note_id`, `pitch_bend_range`, `pressure`, `timbre`,
`slide`) falling back to legacy `get_notes(from_time, from_pitch,
time_span, pitch_span)` → tuples — **note the swapped argument order**;
deletes prefer `remove_notes_extended(...)` over `remove_notes(...)`. A
"new-API-only" fake configuration must break `add_notes_to_clip` — that is
faithful to the real constraint. The extended read path try/excepts into
legacy, so the fake should also support *raising* from the extended API.

**Device/parameter**: device `name`, `class_name`, `class_display_name`,
`can_have_drum_pads`, `can_have_chains` (getattr), `parameters`; parameter
`name` (r; w in the macro-rename fallback), `value` (r/w), `min`, `max`,
`is_quantized`, `is_enabled` (getattr, default True), `str_for_value(v)`
(try/except everywhere), `value_string` (hasattr), `automation_state`
(hasattr) / `is_automated` (hasattr fallback). Rack: `chains`,
`return_chains` (getattr), chain `devices`/`name`/`mute`/`solo`/`out_note` +
`mixer_device`; macro API behind hasattr: `macro_map`, `rename_macro`,
`add_macro`, `macros_mapped`, `visible_macro_count`. Rack recursion capped
at depth 4.

**Application/browser**: `application()` truthiness; `.view.show_view("Arranger")`;
`.browser` (hasattr + not-None); roots `instruments`, `sounds`, `drums`,
`audio_effects`, `midi_effects` + optional `plugins`, `max_for_live`,
`user_library`, `packs`, `samples` (hasattr); **`dir(app.browser)` is used
to enumerate categories** — the fake needs plausible non-underscore
attributes; `browser.load_item(item)`; item `name`, `uri`, `children`
(truthiness = folder), `is_loadable`, `is_device`, `is_folder`.
Save probes: getattr over {song, application} × {`save_set`, `save`,
`save_as`, `save_document`} with `callable()` checks, each call
try/excepted.

**ControlSurface stub contract**: `__init__(c_instance)`, `disconnect()`,
`song()`, `application()`, `log_message(str)`, `show_message(str)`,
`schedule_message(delay, callable)` — the last must be stubbable to **raise
`AssertionError`** to exercise the run-directly fallback at 492–495 and
566–569. Module entry point: `create_instance(c_instance)`.
