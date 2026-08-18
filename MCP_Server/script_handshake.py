"""Remote Script version handshake and capability checks."""

from __future__ import annotations

import logging
import threading
from typing import Any

from .remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION

logger = logging.getLogger("AbletonMCPServer")

_lock = threading.Lock()
_script_info: dict[str, Any] | None = None
_handshake_done = False

# Commands present on Remote Scripts before get_script_info existed
_LEGACY_CAPABILITIES = frozenset({
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
})


def get_cached_script_info() -> dict[str, Any] | None:
    with _lock:
        return dict(_script_info) if _script_info else None


def script_has_capability(name: str) -> bool:
    info = get_cached_script_info()
    if not info or info.get("script_version") in (None, "legacy"):
        return name in _LEGACY_CAPABILITIES
    caps = info.get("capabilities")
    if isinstance(caps, list):
        return name in caps
    return script_version_ok()


def script_version_ok() -> bool:
    info = get_cached_script_info()
    if not info:
        return False
    return str(info.get("script_version") or "") == EXPECTED_REMOTE_SCRIPT_VERSION


def handshake(send_command) -> dict[str, Any]:
    """Query Live for script info. On old scripts, get_script_info is unknown."""
    global _script_info, _handshake_done
    info: dict[str, Any] = {
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

    with _lock:
        _script_info = info
        _handshake_done = True

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


def _parse_version(text: Any) -> tuple[int, ...] | None:
    """"1.8.0" -> (1, 8, 0); None for anything that is not dotted integers."""
    try:
        return tuple(int(part) for part in str(text).strip().split("."))
    except (AttributeError, ValueError):
        return None


def require_capability(name: str, min_version: str | None = None) -> str | None:
    """Return an error message if the loaded Remote Script cannot serve
    ``name``, else None (docs/REFACTOR_PLAN.md §4; interim PR5 shape — PR8
    absorbs this into ``ScriptHandshake.require``).

    ``min_version`` additionally requires the script to be at least that
    version. Capability names alone cannot gate the commands repaired in
    1.8.0: the broken 1.7.0 script already *advertises* them, so only a
    version compare tells a repaired install from a broken one.

    The gate answers only from a successful handshake. If none is cached
    (Ableton was not running when the server started), it attempts one
    lazily; if Live is still unreachable, the gate PASSES and the actual
    send fails with the truthful connection error — never a misleading
    "re-run the installer" about a script nobody has seen.
    """
    info = get_cached_script_info()
    if not info or info.get("script_version") is None:
        # No successful handshake yet — a cached script_version of None means
        # the startup handshake itself failed (e.g. Live unreachable). Try
        # once, lazily. Imported here to avoid a module import cycle.
        try:
            from .server import get_ableton_connection

            handshake(get_ableton_connection().send_command)
        except Exception:
            pass
        info = get_cached_script_info()
    if not info or info.get("script_version") is None:
        return None

    if not script_has_capability(name):
        return (
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
            return (
                f"Ableton Remote Script v{loaded} is older than v{min_version}, "
                f"which '{name}' requires. "
                f"Run `ableton-mcp-install-script` to update the User Remote "
                f"Script, then restart Ableton Live."
            )

    return None
