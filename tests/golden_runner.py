"""Golden characterization harness (docs/REFACTOR_PLAN.md section 5 Level 1,
section 6 PR3).

Two pieces:

- ``FakeWireClient`` — a scripted stand-in for ``AbletonConnection``. It is
  constructed with the *ordered* wire exchange a tool is expected to perform
  (command name AND params are asserted on every send), and returns canned
  responses. This freezes the wire protocol: which commands, with which
  params, in which order.

- ``call_tool(name, args, fake)`` — migration adapter #1 from the plan.
  Pre-refactor it monkeypatches ``MCP_Server.server.get_ableton_connection``
  and ``MCP_Server.script_handshake.require_capability`` (the gated tools
  import the latter inside the function body, so the module attribute is what
  resolves at call time), and snapshots/restores the handshake module state
  (``_script_info`` / ``_handshake_done``) around each call, because
  ``get_remote_script_info`` runs a real handshake that mutates them.
  Post-refactor this adapter is rewritten to build ``Deps``; nothing else in
  the golden suite may change.

Tools are called with ``ctx=None`` exactly as tests/test_clip_notes.py does.
"""

import os
import sys

# Make `import MCP_Server.server` work both under pytest (conftest.py already
# handles it) and when imported by tests/record_goldens.py run as a script.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import MCP_Server.server as server
import MCP_Server.script_handshake as script_handshake


class WireMismatch(BaseException):
    """A tool sent something other than the scripted wire exchange.

    Deliberately a BaseException, NOT an Exception: every tool body wraps its
    work in ``except Exception`` and would otherwise swallow the mismatch and
    return it as its own "Error ...:" string — the replay test would still
    fail on byte-equality, but the recorder would silently freeze the broken
    exchange into a golden. Deriving from BaseException makes wire drift (and
    mis-defined cases) fail loudly at the point of mismatch instead.
    """


class FakeWireClient:
    """Scripted AbletonConnection double.

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


def call_tool(name, args, fake):
    """Call the MCP tool ``name`` with ``args`` against ``fake``, isolated.

    Swaps in the fake connection and a permissive require_capability, and
    snapshots/restores the handshake module state, so every call sees the
    same world regardless of what ran before it (get_remote_script_info's
    real handshake would otherwise leak `_script_info` into later calls).
    """
    tool = getattr(server, name)

    orig_get_conn = server.get_ableton_connection
    orig_require = script_handshake.require_capability
    with script_handshake._lock:
        orig_info = script_handshake._script_info
        orig_done = script_handshake._handshake_done

    server.get_ableton_connection = lambda: fake
    script_handshake.require_capability = lambda name: None
    try:
        return tool(None, **args)
    finally:
        server.get_ableton_connection = orig_get_conn
        script_handshake.require_capability = orig_require
        with script_handshake._lock:
            script_handshake._script_info = orig_info
            script_handshake._handshake_done = orig_done
