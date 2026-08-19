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


# Every command is gated (plan §6 PR10) so a user on an old Remote Script
# gets the friendly "re-run `ableton-mcp-install-script`" message instead of
# a raw "Unknown command" socket error. Two kinds of row stay ungated:
# get_script_info (the handshake's own probe — the gate cannot answer before
# it runs), and the commands in handshake.LEGACY_CAPABILITIES — that set is a
# floor for every script version, so their gate would always pass and
# gating them is a semantic no-op. test_cross_half_contract.py pins this:
# a new row cannot be added ungated without being in the floor.
COMMANDS: dict[str, CommandSpec] = {
    # ── Read-only commands (10 s socket timeout) ─────────────────────────
    # get_script_info is sent by the handshake (handshake.py), not by a tool;
    # it is the probe the gate itself answers from, so it stays ungated.
    "get_script_info": CommandSpec(),
    "get_session_info": CommandSpec(),  # LEGACY floor — gate is a no-op
    "get_track_info": CommandSpec(),  # LEGACY floor — gate is a no-op
    "get_track_routing": CommandSpec(gated=True),
    # 1.7.0 scripts advertise the device-parameter pair but serve broken
    # handlers (duplicate class-body definitions), hence the version floor.
    "get_device_parameters": CommandSpec(gated=True, min_script_version="1.8.0"),
    "get_arrangement_clips": CommandSpec(gated=True),
    "get_clip_notes": CommandSpec(gated=True),
    "get_session_snapshot": CommandSpec(gated=True),
    "get_browser_tree": CommandSpec(),  # LEGACY floor — gate is a no-op
    "get_browser_items_at_path": CommandSpec(),  # LEGACY floor — gate is a no-op
    # ── State-modifying commands (15 s socket timeout) ───────────────────
    "create_midi_track": CommandSpec(modifying=True),  # LEGACY floor — gate is a no-op
    "create_audio_track": CommandSpec(modifying=True, gated=True),
    "set_track_name": CommandSpec(modifying=True),  # LEGACY floor — gate is a no-op
    "create_clip": CommandSpec(modifying=True),  # LEGACY floor — gate is a no-op
    # Importing/decoding a large audio file happens on Live's main thread and
    # can far outlast the default budget; the Remote Script's own queue
    # timeout for it is 60 s, and the server must outlast that (+5 s
    # headroom) or it gives up while Live is still working.
    "create_audio_clip": CommandSpec(modifying=True, timeout=65.0, gated=True),
    "add_notes_to_clip": CommandSpec(modifying=True),  # LEGACY floor — gate is a no-op
    "clear_notes_from_clip": CommandSpec(modifying=True, gated=True),
    "set_clip_name": CommandSpec(modifying=True),  # LEGACY floor — gate is a no-op
    "set_arrangement_clip_name": CommandSpec(modifying=True, gated=True),
    "set_tempo": CommandSpec(modifying=True),  # LEGACY floor — gate is a no-op
    "fire_clip": CommandSpec(modifying=True),  # LEGACY floor — gate is a no-op
    "stop_clip": CommandSpec(modifying=True),  # LEGACY floor — gate is a no-op
    "delete_clip": CommandSpec(modifying=True, gated=True),
    "delete_track": CommandSpec(modifying=True, gated=True),
    "delete_device": CommandSpec(modifying=True, gated=True),
    "set_device_parameter": CommandSpec(
        modifying=True, gated=True, min_script_version="1.8.0"
    ),
    "set_track_volume": CommandSpec(modifying=True, gated=True),
    "set_track_pan": CommandSpec(modifying=True, gated=True),
    "set_track_mute": CommandSpec(modifying=True, gated=True),
    "create_return_track": CommandSpec(modifying=True, gated=True),
    "set_track_arm": CommandSpec(modifying=True, gated=True),
    "set_track_monitoring": CommandSpec(modifying=True, gated=True),
    "save_set": CommandSpec(modifying=True, gated=True),
    "set_track_send": CommandSpec(modifying=True, gated=True),
    "set_count_in": CommandSpec(modifying=True, gated=True),
    "back_to_arrangement": CommandSpec(modifying=True, gated=True),
    "set_track_routing": CommandSpec(modifying=True, gated=True),
    "set_clip_gain": CommandSpec(modifying=True, gated=True),
    "start_playback": CommandSpec(modifying=True),  # LEGACY floor — gate is a no-op
    "stop_playback": CommandSpec(modifying=True),  # LEGACY floor — gate is a no-op
    # The load_* tools actually send load_browser_item; without a modifying
    # row it got the short read-only socket timeout and appeared to fail
    # while Live was in fact still loading the device. Every historical
    # script dispatches it (advertised only from 1.8.0), so it sits in the
    # LEGACY floor and stays ungated like the rest of the floor.
    "load_browser_item": CommandSpec(modifying=True),
    # Dispatchable on the Remote Script but never sent by any current tool
    # (the tool of that name sends load_browser_item); kept for third-party
    # clients (plan §9) and for the cross-half modifying-set equality.
    # Also in the LEGACY floor, so it stays ungated.
    "load_instrument_or_effect": CommandSpec(modifying=True),
    "switch_to_arrangement_view": CommandSpec(modifying=True, gated=True),
    "set_current_song_time": CommandSpec(modifying=True, gated=True),
    "duplicate_session_clip_to_arrangement": CommandSpec(modifying=True, gated=True),
    "create_locator": CommandSpec(modifying=True, gated=True),
}
