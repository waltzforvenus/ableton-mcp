"""
Guardrail for ``expect_track_name``, the stale-index guard the Remote Script
grew on every handler that writes to a track.

The hazard it exists for is the silent half of renumbering: an index that is
still IN range but now points at a different track, which is how an Auto Pan
meant for the GUITARS bus once landed on BASS with a cheerful success
message. The loud half — an index past the end — always raised on its own.

Two things have to hold across ~30 tools, and both are the kind of thing a
hand-written per-tool test set gets 29 out of 30 right:

- a guard the caller gave REACHES the wire. A tool that quietly drops it
  offers a safety net that is not there, which is worse than not offering
  one;
- a guard the caller did NOT give is ABSENT from the wire, not sent as null.
  Dispatch calls the Remote Script handler as ``**params``, so a script older
  than the guard raises a bare TypeError on a keyword it has never heard of;
  omitting the key is what keeps ordinary calls working on those scripts.

Both are checked by walking the registered tool surface, so a tool added
later with the parameter is covered without touching this file.

Runs anywhere — no Ableton, no network.
"""

import inspect
from types import SimpleNamespace

import pytest

import ableton_mcp.tools as tools
from ableton_mcp.app import Deps
from ableton_mcp.services import AbletonService


GUARD = "expect_track_name"

# The three tools whose canonical argument is Optional only because it has an
# accepted second spelling (see test_parameter_aliases.py). Nothing else in
# the signature says they are required, so the probe below is told.
REQUIRED_EXTRAS = {
    "set_device_parameter": {"parameter": "Dry/Wet"},
    "create_audio_clip": {"path": "/tmp/loop.wav"},
    "duplicate_to_arrangement": {"destination_time": 16.0},
}


class RecordingClient:
    """Records every command; answers with an empty result dict."""

    def __init__(self):
        self.sent = []

    def send_command(self, command_type, params=None):
        self.sent.append((command_type, params or {}))
        return {}


class PermissiveHandshake:
    def require(self, name, min_version=None, *, send_command=None):
        return None


def _ctx_and_client():
    client = RecordingClient()
    handshake = PermissiveHandshake()
    ctx = SimpleNamespace(request_context=SimpleNamespace(
        lifespan_context=Deps(client=client, handshake=handshake,
                              service=AbletonService(client, handshake))))
    return ctx, client


def _placeholder(annotation):
    """A value of roughly the right shape for one required argument."""
    text = str(annotation)
    if "bool" in text:
        return False
    if "float" in text:
        return 0.0
    if "int" in text:
        return 0
    if "List" in text or "list" in text:
        return []
    if "Dict" in text or "dict" in text:
        return {}
    return "x"


def _guarded_tools():
    """Every registered tool that offers the guard, with a callable set of
    arguments — the decorator keeps the real signature on ``__wrapped__``."""
    out = []
    for fn in tools.TOOLS:
        signature = inspect.signature(fn.__wrapped__)
        if GUARD not in signature.parameters:
            continue
        kwargs = {
            name: _placeholder(param.annotation)
            for name, param in signature.parameters.items()
            if name != "ctx" and param.default is inspect.Parameter.empty
        }
        kwargs.update(REQUIRED_EXTRAS.get(fn.__name__, {}))
        out.append((fn, kwargs))
    return out


GUARDED = _guarded_tools()
IDS = [fn.__name__ for fn, _ in GUARDED]


def test_the_guard_is_offered_on_the_writing_tools():
    # A floor, not an exact pin: it fails loudly if a port drops the guard
    # wholesale, without fighting every future tool that adds one.
    assert len(GUARDED) >= 31, (
        "only %d tools offer %s: %r" % (len(GUARDED), GUARD, IDS)
    )
    # Reading is not the hazard: a snapshot that read the wrong track is a
    # wrong answer, not a wrong edit, and it costs nothing to re-read.
    assert "get_session_info" not in IDS


@pytest.mark.parametrize("fn,kwargs", GUARDED, ids=IDS)
def test_a_given_guard_reaches_the_wire(fn, kwargs):
    ctx, client = _ctx_and_client()
    out = fn(ctx, **dict(kwargs, expect_track_name="GUITARS"))
    assert client.sent, "%s sent nothing at all: %s" % (fn.__name__, out)
    command, params = client.sent[0]
    assert params.get(GUARD) == "GUITARS", (
        "%s sent %s without the caller's guard: %r"
        % (fn.__name__, command, params)
    )


@pytest.mark.parametrize("fn,kwargs", GUARDED, ids=IDS)
def test_an_absent_guard_is_absent_from_the_wire(fn, kwargs):
    ctx, client = _ctx_and_client()
    fn(ctx, **kwargs)
    assert client.sent
    for command, params in client.sent:
        assert GUARD not in params, (
            "%s put %s on the wire as %r without being asked — an older "
            "Remote Script raises TypeError on the unknown keyword"
            % (fn.__name__, command, params[GUARD])
        )
