"""Golden characterization harness (docs/REFACTOR_PLAN.md section 5 Level 1,
section 6 PR3).

Two pieces:

- ``FakeWireClient`` — a scripted stand-in for the Ableton client. It is
  constructed with the *ordered* wire exchange a tool is expected to perform
  (command name AND params are asserted on every send), and returns canned
  responses. This freezes the wire protocol: which commands, with which
  params, in which order.

- ``call_tool(name, args, fake)`` — migration adapter #1 from the plan, in
  its post-refactor (PR8) shape: it builds a ``Deps`` around the fake client
  and an all-capabilities handshake, and hands the tool a two-line stub ctx.
  No module attribute is swapped and nothing outlives a call — every
  ``call_tool`` gets a fresh ``ScriptHandshake``, so get_remote_script_info's
  real handshake cannot leak state into later cases the way the old
  module-global cache could.
"""

# `import ableton_mcp.server` resolves through the editable install (uv sync)
# both under pytest and when tests/record_goldens.py runs as a script.
from types import SimpleNamespace

import ableton_mcp.server as server
from ableton_mcp.app import Deps
from ableton_mcp.handshake import ScriptHandshake


class WireMismatch(BaseException):
    """A tool sent something other than the scripted wire exchange.

    Deliberately a BaseException, NOT an Exception: every tool body wraps its
    work in ``except Exception`` and would otherwise swallow the mismatch and
    return it as its own "Error ...:" string — the replay test would still
    fail on byte-equality, but the recorder would silently freeze the broken
    exchange into a golden. Deriving from BaseException makes wire drift (and
    mis-defined cases) fail loudly at the point of mismatch instead.
    """


class AllCapabilitiesHandshake(ScriptHandshake):
    """A ScriptHandshake whose gate always passes and never touches the wire.

    ``require`` is the only override: gated tools must run their bodies (and
    their scripted wire exchanges) rather than the missing-capability early
    return, and the lazy-handshake attempt must not inject a get_script_info
    exchange the case never scripted. ``perform``/``info`` stay real, so
    get_remote_script_info exercises the true handshake logic against its
    canned wire response.
    """

    def require(self, name, min_version=None, *, send_command=None):
        return None


class FakeWireClient:
    """Scripted Ableton client double (satisfies the AbletonClient seam).

    ``script`` is an ordered list of steps, each a dict:

        {"command": <name>, "params": <dict>, "response": <dict>}
    or  {"command": <name>, "params": <dict>, "raise": <message>}

    ``send_command`` asserts the next expected command and params match, then
    returns the canned response — or raises ``RuntimeError(message)`` for a
    "raise" step. ``assert_consumed()`` verifies the tool performed the whole
    exchange (no step skipped, none left over).
    """

    def __init__(self, script):
        self.script = list(script)
        self.cursor = 0
        self.sent = []  # list of (command_type, params) actually sent

    def ensure_connected(self):
        """The real client's pre-flight connect (AbletonClient.ensure_connected)
        is a no-op here: the scripted peer is always "reachable" — per-step
        failures are simulated with {"raise": ...} instead."""

    def send_command(self, command_type, params=None):
        params = params or {}
        self.sent.append((command_type, params))

        step_index = self.cursor
        if step_index >= len(self.script):
            raise WireMismatch(
                "unexpected extra wire call #%d: %r with params %r "
                "(the script had only %d step(s))"
                % (step_index, command_type, params, len(self.script))
            )
        step = self.script[step_index]
        self.cursor += 1

        expected_command = step["command"]
        expected_params = step.get("params") or {}
        if command_type != expected_command:
            raise WireMismatch(
                "wire call #%d: expected command %r, got %r (params %r)"
                % (step_index, expected_command, command_type, params)
            )
        if params != expected_params:
            raise WireMismatch(
                "wire call #%d (%r): params mismatch\n  expected: %r\n  got:      %r"
                % (step_index, command_type, expected_params, params)
            )

        if "raise" in step:
            raise RuntimeError(step["raise"])
        return step.get("response", {})

    @property
    def fully_consumed(self):
        return self.cursor == len(self.script)

    def assert_consumed(self):
        if not self.fully_consumed:
            remaining = [s["command"] for s in self.script[self.cursor :]]
            raise WireMismatch(
                "wire script not fully consumed: %d step(s) never sent: %r"
                % (len(remaining), remaining)
            )


def make_ctx(deps):
    """The two-line stub ctx: exactly the attribute path _deps() reads."""
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=deps))


def call_tool(name, args, fake):
    """Call the MCP tool ``name`` with ``args`` against ``fake``, isolated.

    Every call builds a fresh Deps (fake client + fresh all-capabilities
    handshake), so every call sees the same world regardless of what ran
    before it.
    """
    tool = getattr(server, name)
    deps = Deps(client=fake, handshake=AllCapabilitiesHandshake())
    return tool(make_ctx(deps), **args)
