# CLAUDE.md

Guidance for Claude Code (and any other agent) working in this repository.

## What this project is

An MCP server that drives Ableton Live. Two halves that must stay in step:

- `MCP_Server/server.py` — the MCP server. Defines the tools, speaks stdio to
  the client and JSON-over-TCP to Live.
- `AbletonMCP_Remote_Script/__init__.py` — a MIDI Remote Script that runs
  *inside* Ableton Live, hosting the socket server and touching the Live Object
  Model (LOM).

This is a **permanent fork** of [ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp).
Upstream is merged in for genuine features; it is not a base we are trying to
get back onto.

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
grep -ri "telemetry\|supabase\|analytics\|dataset\|trajectory" MCP_Server/ AbletonMCP_Remote_Script/
```

Reading Live's state and returning it *to the user's own MCP client* is fine —
that is what `get_session_snapshot` does. The line is whether data leaves the
user's machine to anywhere but their own client.

### 2. The socket binds to loopback

`HOST = "127.0.0.1"` in the Remote Script. That socket executes arbitrary
commands with no authentication, so binding it to `0.0.0.0` exposes Live to the
whole local network. Upstream binds to `0.0.0.0`; when merging, check this has
not come back.

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

### Duplicate tool names are silent

FastMCP registers tools by name. Two `@mcp.tool()` functions with the same name
means the later definition wins and the earlier one vanishes — no error, and
which one survives depends on file order. After merging upstream, check:

```bash
uv run python -c "
import asyncio, MCP_Server.server as s, collections
n=[t.name for t in asyncio.run(s.mcp.list_tools())]
print(len(n), [k for k,v in collections.Counter(n).items() if v>1])"
```

### The Remote Script exists twice

`AbletonMCP_Remote_Script/__init__.py` is canonical.
`MCP_Server/bundled_ableton_remote_script/AbletonMCP_init.py` is the copy that
`ableton-mcp-install-script` actually installs into Live. **Never edit the
bundled copy by hand** — regenerate it after changing the canonical one:

```bash
cp AbletonMCP_Remote_Script/__init__.py MCP_Server/bundled_ableton_remote_script/AbletonMCP_init.py
```

They drifted once already, and the bundled copy silently reverted the loopback
bind and dropped every tool this fork adds.

### Adding a command touches four places

To add a tool end to end:

1. `@mcp.tool()` function in `MCP_Server/server.py` that calls
   `send_command("your_command", {...})`.
2. If it changes Live's state, add the command name to the modifying-command
   list in `server.py` (`_send_command_locked`) so it gets the longer socket
   timeout, **and** to the main-thread dispatch list in the Remote Script.
   State changes must run on Live's main thread.
3. Dispatch branch plus a `_your_command` implementation in the Remote Script.
4. Add the name to `SCRIPT_CAPABILITIES` in the Remote Script, then regenerate
   the bundled copy.

Bump `SCRIPT_VERSION` in the Remote Script and `EXPECTED_REMOTE_SCRIPT_VERSION`
in `MCP_Server/remote_script_install.py` together — they are compared at
runtime. Gate genuinely new commands with
`require_capability("your_command")` so a user running an older installed
script gets a clear "re-run the installer" message instead of a socket error.

### The Remote Script runs on Ableton's Python

It imports `_Framework` and cannot be imported, linted, or tested outside Live.
Check it with `python -c "import ast; ast.parse(open(...).read())"`. Assume an
older Python and a restricted environment: prefer `hasattr` probes and
fall-backs over assuming a modern Live API, as the existing code does with
`remove_notes_extended` versus `remove_notes`.

## Working in the repo

```bash
uv sync --extra dev
uv run pytest          # no Ableton and no network required
uv run ableton-mcp     # starts the server on stdio
uv build               # must succeed; needs setuptools>=77 for PEP 639
```

Tests mock the Ableton socket, so they run anywhere. Keep it that way — a test
that needs Live running is a test that never runs.

## Conventions

- Docstrings on tools are read by the model at runtime, so they are part of the
  interface. Say what the tool is *for* and when to prefer it over a
  neighbouring tool, not just what its arguments are.
- Comments explain *why*, particularly where Live's API forced the shape of the
  code. Match the surrounding density; the existing code is well commented
  about Live's quirks and that is worth preserving.
- Prefer clamping into a valid range over erroring, where a musician would
  reasonably expect "as low as this goes" — see `set_device_parameter`.
- Commit messages explain the reasoning, not just the change. When resolving a
  merge, record which side won and why.
- Keep the README's tool reference in step when adding or removing a tool, and
  mark fork-only tools with †.
- When merging upstream, run through the hard rules above before committing:
  telemetry, loopback bind, duplicate tools, bundled script.
