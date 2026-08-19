"""Remote Script version handshake and capability checks.

Instance-stateful successor to the retired ``script_handshake`` module
(docs/REFACTOR_PLAN.md §3.3, §4): the cached handshake info and its lock are
``ScriptHandshake`` instance state, constructed once in the composition root
and injected everywhere else — no module globals, so tests build isolated
instances and the transport can invalidate the cache on reconnect. The
string-returning ``require_capability`` gate became ``require``, which
raises :class:`CapabilityError` carrying the identical message text.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Optional

from .remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION

logger = logging.getLogger("AbletonMCPServer")

# Commands every AbletonMCP Remote Script that has ever existed can serve —
# the dispatch surface of the oldest script, from before get_script_info
# existed. The set plays two roles in the gate: it is the assumed capability
# list for legacy handshakes (scripts too old to answer get_script_info), and
# it is a FLOOR for every script version — a command in this set is always
# considered available, because historical scripts did not advertise
# everything they dispatched (1.7.0 dispatched load_browser_item but never
# listed it in SCRIPT_CAPABILITIES), so the advertised list under-reports and
# gating on it alone would falsely block commands that work.
LEGACY_CAPABILITIES = frozenset({
    "get_session_info",
    "get_track_info",
    "create_midi_track",
    "create_clip",
    "add_notes_to_clip",
    "set_tempo",
    "set_track_name",
    "set_clip_name",
    "fire_clip",
    "stop_clip",
    "start_playback",
    "stop_playback",
    "load_instrument_or_effect",
    "get_browser_tree",
    "get_browser_items_at_path",
    # Dispatched by every historical script (it is the wire command all three
    # load_* tools actually send) but only advertised in SCRIPT_CAPABILITIES
    # from 1.8.0 — without the floor, gating it would falsely block a 1.7.0
    # user from a command their script serves fine.
    "load_browser_item",
})


class CapabilityError(Exception):
    """The loaded Remote Script cannot serve a command.

    ``str()`` of the instance is the user-facing message — the friendly
    "re-run ``ableton-mcp-install-script``" text that gated tools return to
    the model verbatim instead of a raw socket error.
    """


def _parse_version(text: Any) -> Optional[tuple]:
    """"1.8.0" -> (1, 8, 0); None for anything that is not dotted integers."""
    try:
        return tuple(int(part) for part in str(text).strip().split("."))
    except (AttributeError, ValueError):
        return None


class ScriptHandshake:
    """Cached knowledge about the Remote Script loaded in Live.

    ``perform`` runs the handshake and caches its info; ``require`` is the
    capability/version gate over that cache; ``invalidate`` empties it (wired
    as the transport's ``on_reconnect`` callback, because a new socket can
    mean a restarted Live with a different script).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._info: Optional[Dict[str, Any]] = None

    def info(self) -> Optional[Dict[str, Any]]:
        """A copy of the most recent handshake's info, or None if none ran."""
        with self._lock:
            return dict(self._info) if self._info else None

    def invalidate(self) -> None:
        """Forget the cached handshake; the next gated call re-handshakes."""
        with self._lock:
            self._info = None

    def perform(self, send_command: Callable[..., Dict[str, Any]]) -> Dict[str, Any]:
        """Query Live for script info. On old scripts, get_script_info is unknown."""
        info: Dict[str, Any] = {
            "script_version": None,
            "capabilities": [],
            "expected_version": EXPECTED_REMOTE_SCRIPT_VERSION,
            "up_to_date": False,
            "error": None,
        }
        try:
            result = send_command("get_script_info")
            if isinstance(result, dict):
                info.update(result)
                info["expected_version"] = EXPECTED_REMOTE_SCRIPT_VERSION
                info["up_to_date"] = (
                    str(result.get("script_version") or "") == EXPECTED_REMOTE_SCRIPT_VERSION
                )
        except Exception as e:
            msg = str(e)
            info["error"] = msg
            # Old Remote Scripts don't know get_script_info
            if "Unknown command" in msg or "unknown command" in msg.lower():
                info["script_version"] = "legacy"
                info["capabilities"] = []
                info["up_to_date"] = False
                logger.warning(
                    "Ableton Remote Script is outdated (no get_script_info). "
                    "Expected %s. Run `ableton-mcp-install-script`, then restart "
                    "Live.",
                    EXPECTED_REMOTE_SCRIPT_VERSION,
                )
            else:
                logger.warning("Remote Script handshake failed: %s", e)

        with self._lock:
            self._info = info

        if info.get("up_to_date"):
            logger.info(
                "Remote Script handshake OK (v%s, %d capabilities)",
                info.get("script_version"),
                len(info.get("capabilities") or []),
            )
        elif info.get("script_version") and info.get("script_version") != "legacy":
            logger.warning(
                "Remote Script v%s loaded, package expects v%s — run "
                "`ableton-mcp-install-script`, then restart Ableton.",
                info.get("script_version"),
                EXPECTED_REMOTE_SCRIPT_VERSION,
            )

        return info

    def _has_capability(self, info: Dict[str, Any], name: str) -> bool:
        # The floor applies to EVERY script version, not just legacy
        # handshakes: every script that has ever existed dispatches these
        # commands, but not every version advertises them all (see the
        # LEGACY_CAPABILITIES comment), so "advertised" under-reports.
        if name in LEGACY_CAPABILITIES:
            return True
        if info.get("script_version") in (None, "legacy"):
            return False
        caps = info.get("capabilities")
        if isinstance(caps, list):
            return name in caps
        # No capability list in the reply: fall back to "is this exactly the
        # expected version", the pre-capability-list probe.
        return str(info.get("script_version") or "") == EXPECTED_REMOTE_SCRIPT_VERSION

    def require(self, name: str, min_version: Optional[str] = None, *,
                send_command: Optional[Callable[..., Dict[str, Any]]] = None) -> None:
        """Raise :class:`CapabilityError` if the loaded Remote Script cannot
        serve ``name``, else return None (docs/REFACTOR_PLAN.md §4).

        ``min_version`` additionally requires the script to be at least that
        version. Capability names alone cannot gate the commands repaired in
        1.8.0: the broken 1.7.0 script already *advertises* them, so only a
        version compare tells a repaired install from a broken one.

        The gate answers only from a successful handshake. If none is cached
        (Ableton was not running when the server started), it attempts one
        lazily through ``send_command``; if Live is still unreachable, the
        gate PASSES and the actual send fails with the truthful connection
        error — never a misleading "re-run the installer" about a script
        nobody has seen.
        """
        info = self.info()
        if not info or info.get("script_version") is None:
            # No successful handshake yet — a cached script_version of None
            # means the startup handshake itself failed (e.g. Live
            # unreachable). Try once, lazily, through the caller-provided
            # transport; perform() absorbs send failures into the info dict.
            if send_command is not None:
                try:
                    self.perform(send_command)
                except Exception:
                    pass
            info = self.info()
        if not info or info.get("script_version") is None:
            return

        if not self._has_capability(info, name):
            raise CapabilityError(
                f"Ableton Remote Script missing capability '{name}' "
                f"(loaded={info.get('script_version')!r}, "
                f"expected={EXPECTED_REMOTE_SCRIPT_VERSION}). "
                f"Run `ableton-mcp-install-script` to update the User Remote Script, "
                f"then restart Ableton Live."
            )

        if min_version is not None:
            loaded = info.get("script_version")
            required = _parse_version(min_version)
            parsed = _parse_version(loaded)
            # "legacy" and anything unparseable count as older than min_version.
            if required is not None and (parsed is None or parsed < required):
                raise CapabilityError(
                    f"Ableton Remote Script v{loaded} is older than v{min_version}, "
                    f"which '{name}' requires. "
                    f"Run `ableton-mcp-install-script` to update the User Remote "
                    f"Script, then restart Ableton Live."
                )
