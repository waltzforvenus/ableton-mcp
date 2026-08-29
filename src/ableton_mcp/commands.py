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
    # 1.15.0 taught it to read arrangement clips (arrangement=True). An
    # older script advertises the name but its handler has no such keyword,
    # so the dispatch's **params call dies on a raw TypeError inside Live.
    "get_clip_notes": CommandSpec(gated=True, min_script_version="1.15.0"),
    # Snapshot schema v3: the scoping flags and the tracks filter are new
    # keywords (TypeError on an older handler), and v2 also emitted warp
    # markers and rack chains unconditionally — the same call returns a
    # materially different payload, which is the second half of the floor.
    "get_session_snapshot": CommandSpec(gated=True, min_script_version="1.15.0"),
    "get_browser_tree": CommandSpec(),  # LEGACY floor — gate is a no-op
    "get_browser_items_at_path": CommandSpec(),  # LEGACY floor — gate is a no-op
    # ── State-modifying commands (15 s socket timeout) ───────────────────
    "create_midi_track": CommandSpec(modifying=True),  # LEGACY floor — gate is a no-op
    "create_audio_track": CommandSpec(modifying=True, gated=True),
    "duplicate_track": CommandSpec(modifying=True, gated=True),
    "set_track_name": CommandSpec(modifying=True),  # LEGACY floor — gate is a no-op
    "create_clip": CommandSpec(modifying=True),  # LEGACY floor — gate is a no-op
    # Importing/decoding a large audio file happens on Live's main thread and
    # can far outlast the default budget; the Remote Script's own queue
    # timeout for it is 60 s, and the server must outlast that (+5 s
    # headroom) or it gives up while Live is still working.
    # 1.15.0 turns warping OFF inside the import task and reports it back
    # (six identically-long stems each got a different Auto-Warp tempo
    # guess); an older script warps on import and its reply carries no
    # `warping` at all, so the floor is about the DEFAULT behaviour, not
    # just the new expected_beats keyword.
    "create_audio_clip": CommandSpec(
        modifying=True, timeout=65.0, gated=True, min_script_version="1.15.0"
    ),
    # In the LEGACY floor (so the capability half of the gate is a no-op)
    # but gated anyway, because only a gated row consults
    # min_script_version — and this one needs it: 1.15.0 added the
    # arrangement=True write path and expect_count, and replaced the old
    # {"note_count": <what you sent>} echo with a measured before/after
    # delta. An older script serves it, but serves it in exactly the way
    # that made "Added 262 notes" print when Live took none.
    "add_notes_to_clip": CommandSpec(
        modifying=True, gated=True, min_script_version="1.15.0"
    ),
    "clear_notes_from_clip": CommandSpec(
        modifying=True, gated=True, min_script_version="1.15.0"
    ),
    "set_clip_name": CommandSpec(modifying=True),  # LEGACY floor — gate is a no-op
    "set_arrangement_clip_name": CommandSpec(modifying=True, gated=True),
    "set_tempo": CommandSpec(modifying=True),  # LEGACY floor — gate is a no-op
    "fire_clip": CommandSpec(modifying=True),  # LEGACY floor — gate is a no-op
    "stop_clip": CommandSpec(modifying=True),  # LEGACY floor — gate is a no-op
    "delete_clip": CommandSpec(modifying=True, gated=True),
    "delete_track": CommandSpec(modifying=True, gated=True),
    # Up to 1.14.0 the handler returned len(track.devices) with no
    # before-count, so a delete that did nothing still reported "deleted X;
    # N devices remain" — a lie a highest-index-first sweep then builds on.
    # 1.15.0 verifies and returns the surviving device names.
    "delete_device": CommandSpec(
        modifying=True, gated=True, min_script_version="1.15.0"
    ),
    "set_device_parameter": CommandSpec(
        modifying=True, gated=True, min_script_version="1.8.0"
    ),
    "set_device_parameters": CommandSpec(modifying=True, gated=True),
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
    "set_clip_warp": CommandSpec(modifying=True, gated=True),
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
    # 35 s, not the modifying default: the Remote Script's queue timeout for
    # this row is 30 s because destination_times places a whole run of
    # stamps inside one main-thread task, and the socket must outlast it by
    # the 5 s headroom test_cross_half_contract enforces. The floor covers
    # both new keywords AND the loop-phase guard, which now REFUSES stamps
    # an older script performs happily (and silently re-phases a looping
    # survivor doing it — the worst correctness incident of the session).
    "duplicate_session_clip_to_arrangement": CommandSpec(
        modifying=True, timeout=35.0, gated=True, min_script_version="1.15.0"
    ),
    "create_locator": CommandSpec(modifying=True, gated=True),
    "jump_to_locator": CommandSpec(modifying=True, gated=True),
    "delete_locator": CommandSpec(modifying=True, gated=True),
    # Scripts up to 1.11.0 advertise trim_arrangement_clip but serve the
    # marker-write implementation, which is verifiably inert on real Live
    # (12.4.3: every trim self-refuses); the floor turns that endless
    # refusal loop into installer advice. 1.12.0 trims via overlap-stamping.
    "trim_arrangement_clip": CommandSpec(
        modifying=True, gated=True, min_script_version="1.12.0"
    ),
    # 1.15.0 addresses clips by start_time (and a plural start_times that
    # resolves every position before deleting any). Older scripts take only
    # the positional clip_index, so a start_time request would be dispatched
    # into a handler that has no such keyword.
    "delete_arrangement_clip": CommandSpec(
        modifying=True, gated=True, min_script_version="1.15.0"
    ),
    "move_arrangement_clip": CommandSpec(modifying=True, gated=True),
    # Same loop-phase guard as the session stamp, and the same reason for a
    # floor: an older script makes the re-phasing stamp without refusing.
    "duplicate_arrangement_clip": CommandSpec(
        modifying=True, gated=True, min_script_version="1.15.0"
    ),
}
