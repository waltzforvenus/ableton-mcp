"""Command metadata registry — the server-side single source of truth
(docs/REFACTOR_PLAN.md §3.4).

One row per wire command the server sends to the Remote Script. The transport
(connection.py) derives its socket-timeout policy from this table, and the
capability/version gate data lives here so the eventual service layer can
consult one place instead of five ad-hoc call sites. The registry is
consumed, never duplicated: the modifying-command list that used to be
embedded in ``AbletonConnection._send_command_locked`` and the
``long_running_commands`` dict beside it are both gone.

The Remote Script cannot import this module inside Live, so the two halves
are reconciled by test, not by import: tests/test_cross_half_contract.py
asserts this table's modifying set matches the Remote Script's main-thread
dispatch set, that every command literal the server sends has a row here,
and that the timeout overrides keep headroom over the script's own queue
timeouts.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandSpec:
    """Metadata for one wire command.

    - ``modifying``: the Remote Script runs it on Live's main thread, so the
      transport gives it the longer 15 s socket timeout instead of 10 s.
    - ``timeout``: explicit socket-timeout override in seconds (wins over the
      modifying/read-only default).
    - ``gated``: the tool must pass the Remote Script capability gate
      (``ScriptHandshake.require``) before sending.
    - ``min_script_version``: the gate additionally requires at least this
      script version — capability names alone cannot tell a repaired install
      from a broken one that already advertises the name (plan §4).
    """

    modifying: bool = False
    timeout: float | None = None
    gated: bool = False
    min_script_version: str | None = None


# gated=True is deliberately limited to the commands that were already gated
# before this registry existed; flipping it on for the rest of the
# non-legacy surface is plan PR10's data-only change, not this module's job.
COMMANDS: dict[str, CommandSpec] = {
    # ── Read-only commands (10 s socket timeout) ─────────────────────────
    # get_script_info is sent by the handshake (handshake.py), not by a tool.
    "get_script_info": CommandSpec(),
    "get_session_info": CommandSpec(),
    "get_track_info": CommandSpec(),
    "get_track_routing": CommandSpec(),
    # 1.7.0 scripts advertise the device-parameter pair but serve broken
    # handlers (duplicate class-body definitions), hence the version floor.
    "get_device_parameters": CommandSpec(gated=True, min_script_version="1.8.0"),
    "get_arrangement_clips": CommandSpec(),
    "get_clip_notes": CommandSpec(gated=True),
    "get_session_snapshot": CommandSpec(gated=True),
    "get_browser_tree": CommandSpec(),
    "get_browser_items_at_path": CommandSpec(),
    # ── State-modifying commands (15 s socket timeout) ───────────────────
    "create_midi_track": CommandSpec(modifying=True),
    "create_audio_track": CommandSpec(modifying=True),
    "set_track_name": CommandSpec(modifying=True),
    "create_clip": CommandSpec(modifying=True),
    # Importing/decoding a large audio file happens on Live's main thread and
    # can far outlast the default budget; the Remote Script's own queue
    # timeout for it is 60 s, and the server must outlast that (+5 s
    # headroom) or it gives up while Live is still working.
    "create_audio_clip": CommandSpec(modifying=True, timeout=65.0),
    "add_notes_to_clip": CommandSpec(modifying=True),
    "clear_notes_from_clip": CommandSpec(modifying=True, gated=True),
    "set_clip_name": CommandSpec(modifying=True),
    "set_arrangement_clip_name": CommandSpec(modifying=True),
    "set_tempo": CommandSpec(modifying=True),
    "fire_clip": CommandSpec(modifying=True),
    "stop_clip": CommandSpec(modifying=True),
    "delete_clip": CommandSpec(modifying=True, gated=True),
    "delete_track": CommandSpec(modifying=True),
    "delete_device": CommandSpec(modifying=True),
    "set_device_parameter": CommandSpec(
        modifying=True, gated=True, min_script_version="1.8.0"
    ),
    "set_track_volume": CommandSpec(modifying=True),
    "set_track_pan": CommandSpec(modifying=True),
    "set_track_mute": CommandSpec(modifying=True),
    "create_return_track": CommandSpec(modifying=True),
    "set_track_arm": CommandSpec(modifying=True),
    "set_track_monitoring": CommandSpec(modifying=True),
    "save_set": CommandSpec(modifying=True),
    "set_track_send": CommandSpec(modifying=True),
    "set_count_in": CommandSpec(modifying=True),
    "back_to_arrangement": CommandSpec(modifying=True),
    "set_track_routing": CommandSpec(modifying=True),
    "set_clip_gain": CommandSpec(modifying=True),
    "start_playback": CommandSpec(modifying=True),
    "stop_playback": CommandSpec(modifying=True),
    # The load_* tools actually send load_browser_item; without a modifying
    # row it got the short read-only socket timeout and appeared to fail
    # while Live was in fact still loading the device.
    "load_browser_item": CommandSpec(modifying=True),
    # Dispatchable on the Remote Script but never sent by any current tool
    # (the tool of that name sends load_browser_item); kept for third-party
    # clients (plan §9) and for the cross-half modifying-set equality.
    "load_instrument_or_effect": CommandSpec(modifying=True),
    "switch_to_arrangement_view": CommandSpec(modifying=True),
    "set_current_song_time": CommandSpec(modifying=True),
    "duplicate_session_clip_to_arrangement": CommandSpec(modifying=True),
    "create_locator": CommandSpec(modifying=True, gated=True),
}
