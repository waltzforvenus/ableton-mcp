"""Model layer: ``AbletonService``, the domain service between the tool
controllers and the wire (docs/REFACTOR_PLAN.md §3.2).

One method per wire command, section-commented in the same domain order as
tools.py. Almost every method is honestly small — build the command's param
dict, hand it to ``_send`` — and ``_send`` consults the commands.py registry,
so capability/version gating happens in exactly one place instead of the
seven hand-written blocks the tool bodies used to carry. Multi-step
orchestration (decisions between sends) is Model logic and lives here too:
``load_drum_kit``'s three-call sequence and ``get_remote_script_info``'s
handshake/cache interplay.

Methods return raw dicts. No string formatting here — every model-facing
string, success and error alike, belongs to the View (presenters.py).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from .commands import COMMANDS
from .handshake import ScriptHandshake
from .remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION


def _guarded(params: Dict[str, Any],
             expect_track_name: Optional[str]) -> Dict[str, Any]:
    """Add the caller's track-name guard to ``params`` — only if they gave one.

    Which is the whole point of the helper: the key is omitted entirely
    rather than sent as null. The Remote Script dispatches handlers as
    ``**params``, so a script older than the guard raises a bare TypeError
    on a keyword it has never heard of. Omitting it keeps every ordinary
    call working against those scripts, and leaves the guard as something
    a caller opts into.

    Be precise about what that opt-in costs on an old script: only the
    commands independently floored at 1.15.0 turn it into the friendly
    re-run-the-installer message. The rest — set_track_volume, set_clip_gain,
    move_arrangement_clip and the other long-standing rows — carry no floor,
    because flooring them would refuse every ORDINARY call on a 1.14.0 script
    to protect an optional argument. On those, passing expect_track_name to a
    pre-1.15.0 script surfaces as a TypeError from the script's ``**params``
    dispatch. Omitting it, which is the default, is always safe.

    Same shape as the optional criteria in ``jump_to_locator`` and
    ``trim_arrangement_clip``: presence, not value, is what the script
    branches on. The rule across this module is that a command's ordinary
    call shape is always sent (an explicit ``arrangement`` beats inheriting
    whichever default the installed script happens to have), while
    assertions, overrides and alternative addressing are sent only when
    used.
    """
    if expect_track_name is not None:
        params["expect_track_name"] = expect_track_name
    return params


class AbletonService:
    """The Model: constructed once, in the composition root (app.build_deps).

    ``client`` is anything satisfying the ``AbletonClientProtocol`` seam
    (``send_command``); the Protocol itself lives in app.py, which imports
    this module — naming it here would be an import cycle for the sake of a
    type hint.
    """

    def __init__(self, client: Any, handshake: ScriptHandshake) -> None:
        self._client = client
        self._handshake = handshake

    def _send(self, name: str,
              params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send one wire command, applying the registry's gate first.

        A ``COMMANDS[name].gated`` row routes through
        ``ScriptHandshake.require`` — which raises :class:`CapabilityError`
        carrying the friendly "re-run ``ableton-mcp-install-script``" text —
        before anything touches the wire, with the registry's
        ``min_script_version`` floor applied (capability names alone cannot
        tell a repaired script from a broken 1.7.0 that already advertises
        them, plan §4).
        """
        spec = COMMANDS[name]
        if spec.gated:
            self._handshake.require(name, spec.min_script_version,
                                    send_command=self._client.send_command)
        return self._client.send_command(name, params or {})

    # Core commands

    def get_session_info(self) -> Dict[str, Any]:
        return self._send("get_session_info")

    def get_remote_script_info(self) -> Dict[str, Any]:
        """The handshake/cache interplay behind the get_remote_script_info
        tool. Touch the connection first, exactly where
        get_ableton_connection() used to: an unreachable Live must surface
        as the tool's error string, not be absorbed into the handshake info
        (perform() deliberately swallows send failures)."""
        self._client.ensure_connected()
        info = self._handshake.perform(self._client.send_command)
        cached = dict(self._handshake.info() or info)
        cached["expected_version"] = EXPECTED_REMOTE_SCRIPT_VERSION
        return cached

    def get_track_info(self, track_index: int) -> Dict[str, Any]:
        return self._send("get_track_info", {"track_index": track_index})

    def get_clip_notes(self, track_index: int, clip_index: int,
                       arrangement: bool = False) -> Dict[str, Any]:
        return self._send(
            "get_clip_notes",
            {
                "track_index": track_index,
                "clip_index": clip_index,
                "arrangement": arrangement,
            },
        )

    def get_session_snapshot(
        self,
        include_notes: bool,
        include_params: bool,
        include_warp_markers: bool = False,
        include_rack_chains: bool = False,
        include_empty_slots: bool = False,
        tracks: Optional[List[Union[int, str]]] = None,
    ) -> Dict[str, Any]:
        # Every flag goes on the wire explicitly, none left to the script's
        # own default: schema v3 changed three of those defaults, and a
        # payload whose size depends on which script happens to be installed
        # is the opposite of what a scoped snapshot is for. ``tracks`` is
        # the exception — no filter is its absence, not a false.
        params: Dict[str, Any] = {
            "include_notes": include_notes,
            "include_params": include_params,
            "include_warp_markers": include_warp_markers,
            "include_rack_chains": include_rack_chains,
            "include_empty_slots": include_empty_slots,
        }
        if tracks is not None:
            params["tracks"] = tracks
        return self._send("get_session_snapshot", params)

    def create_midi_track(self, index: int) -> Dict[str, Any]:
        return self._send("create_midi_track", {"index": index})

    def create_audio_track(self, index: int) -> Dict[str, Any]:
        return self._send("create_audio_track", {"index": index})

    def duplicate_track(self, track_index: int,
                        expect_track_name: Optional[str] = None
                        ) -> Dict[str, Any]:
        return self._send(
            "duplicate_track",
            _guarded({"track_index": track_index}, expect_track_name))

    def set_track_name(self, track_index: int, name: str,
                       expect_track_name: Optional[str] = None
                       ) -> Dict[str, Any]:
        return self._send("set_track_name",
                          _guarded({"track_index": track_index, "name": name},
                                   expect_track_name))

    def create_clip(self, track_index: int, clip_index: int, length: float,
                    expect_track_name: Optional[str] = None) -> Dict[str, Any]:
        return self._send("create_clip", _guarded({
            "track_index": track_index,
            "clip_index": clip_index,
            "length": length
        }, expect_track_name))

    def set_clip_gain(self, track_index: int, clip_index: int, gain: float,
                      arrangement: bool,
                      expect_track_name: Optional[str] = None
                      ) -> Dict[str, Any]:
        return self._send("set_clip_gain", _guarded({
            "track_index": track_index,
            "clip_index": clip_index,
            "gain": gain,
            "arrangement": arrangement
        }, expect_track_name))

    def set_clip_warp(self, track_index: int, clip_index: int, warping: bool,
                      warp_mode: Optional[int], arrangement: bool,
                      expect_track_name: Optional[str] = None
                      ) -> Dict[str, Any]:
        return self._send("set_clip_warp", _guarded({
            "track_index": track_index,
            "clip_index": clip_index,
            "warping": warping,
            "warp_mode": warp_mode,
            "arrangement": arrangement
        }, expect_track_name))

    def back_to_arrangement(self) -> Dict[str, Any]:
        return self._send("back_to_arrangement", {})

    def get_track_routing(self, track_index: int) -> Dict[str, Any]:
        return self._send("get_track_routing", {"track_index": track_index})

    def set_track_routing(self, track_index: int, target: str, field: str,
                          expect_track_name: Optional[str] = None
                          ) -> Dict[str, Any]:
        return self._send("set_track_routing", _guarded({
            "track_index": track_index,
            "field": field,
            "target": target
        }, expect_track_name))

    def set_count_in(self, bars: int, metronome: bool) -> Dict[str, Any]:
        return self._send("set_count_in", {
            "bars": bars,
            "metronome": metronome
        })

    def set_track_send(self, track_index: int, send_index: int, value: float,
                       expect_track_name: Optional[str] = None
                       ) -> Dict[str, Any]:
        return self._send("set_track_send", _guarded({
            "track_index": track_index,
            "send_index": send_index,
            "value": value
        }, expect_track_name))

    def save_set(self) -> Dict[str, Any]:
        return self._send("save_set", {})

    def create_return_track(self) -> Dict[str, Any]:
        return self._send("create_return_track", {})

    def set_track_arm(self, track_index: int, armed: bool,
                      expect_track_name: Optional[str] = None
                      ) -> Dict[str, Any]:
        return self._send("set_track_arm", _guarded({
            "track_index": track_index,
            "value": armed
        }, expect_track_name))

    def set_track_monitoring(self, track_index: int, state: str,
                             expect_track_name: Optional[str] = None
                             ) -> Dict[str, Any]:
        return self._send("set_track_monitoring", _guarded({
            "track_index": track_index,
            "value": state
        }, expect_track_name))

    def get_device_parameters(self, track_index: int, device_index: int,
                              track_type: str) -> Dict[str, Any]:
        return self._send("get_device_parameters", {
            "track_index": track_index,
            "device_index": device_index,
            "track_type": track_type
        })

    def set_device_parameter(self, track_index: int, device_index: int,
                             parameter: Union[str, int], value: float,
                             track_type: str,
                             expect_track_name: Optional[str] = None
                             ) -> Dict[str, Any]:
        # ``parameter`` arrives already coerced (a name, or an int index) —
        # the string-to-int parse is boundary work and stays in the
        # controller.
        return self._send("set_device_parameter", _guarded({
            "track_index": track_index,
            "device_index": device_index,
            "parameter": parameter,
            "value": value,
            "track_type": track_type
        }, expect_track_name))

    def set_device_parameters(self, track_index: int, device_index: int,
                              parameters: Dict[str, Any], track_type: str,
                              expect_track_name: Optional[str] = None
                              ) -> Dict[str, Any]:
        # The plural of the method above, and deliberately not a loop over
        # it: the whole batch is one wire command so the writes land inside
        # a single main-thread task.
        return self._send("set_device_parameters", _guarded({
            "track_index": track_index,
            "device_index": device_index,
            "parameters": parameters,
            "track_type": track_type
        }, expect_track_name))

    def delete_device(self, track_index: int, device_index: int,
                      track_type: str,
                      expect_track_name: Optional[str] = None
                      ) -> Dict[str, Any]:
        return self._send("delete_device", _guarded({
            "track_index": track_index,
            "device_index": device_index,
            "track_type": track_type
        }, expect_track_name))

    def set_track_volume(self, track_index: int, value: float,
                         track_type: str,
                         expect_track_name: Optional[str] = None
                         ) -> Dict[str, Any]:
        return self._send("set_track_volume", _guarded({
            "track_index": track_index,
            "value": value,
            "track_type": track_type
        }, expect_track_name))

    def set_track_pan(self, track_index: int, value: float, track_type: str,
                      expect_track_name: Optional[str] = None
                      ) -> Dict[str, Any]:
        return self._send("set_track_pan", _guarded({
            "track_index": track_index,
            "value": value,
            "track_type": track_type
        }, expect_track_name))

    def set_track_mute(self, track_index: int, mute: bool,
                       expect_track_name: Optional[str] = None
                       ) -> Dict[str, Any]:
        return self._send("set_track_mute", _guarded({
            "track_index": track_index,
            "value": mute
        }, expect_track_name))

    def delete_track(self, track_index: int,
                     expect_track_name: Optional[str] = None
                     ) -> Dict[str, Any]:
        return self._send("delete_track", _guarded({
            "track_index": track_index
        }, expect_track_name))

    def create_audio_clip(self, track_index: int, clip_index: int, path: str,
                          expected_beats: Optional[float] = None,
                          expect_track_name: Optional[str] = None
                          ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "track_index": track_index,
            "clip_index": clip_index,
            "path": path
        }
        # An assertion about the file, not part of the import: sent only
        # when the caller has one to make.
        if expected_beats is not None:
            params["expected_beats"] = expected_beats
        return self._send("create_audio_clip",
                          _guarded(params, expect_track_name))

    def add_notes_to_clip(
        self,
        track_index: int,
        clip_index: int,
        notes: List[Dict[str, Union[int, float, bool]]],
        expect_count: Optional[int] = None,
        arrangement: bool = False,
        expect_track_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "track_index": track_index,
            "clip_index": clip_index,
            "notes": notes,
            "arrangement": arrangement
        }
        # The caller's stated total. It only means anything if it can
        # disagree with what arrived, so it is sent exactly as given and
        # never defaulted to len(notes) here — that would compare the list
        # against itself and always pass.
        if expect_count is not None:
            params["expect_count"] = expect_count
        return self._send("add_notes_to_clip",
                          _guarded(params, expect_track_name))

    def clear_notes_from_clip(self, track_index: int, clip_index: int,
                              arrangement: bool = False,
                              expect_track_name: Optional[str] = None
                              ) -> Dict[str, Any]:
        return self._send("clear_notes_from_clip", _guarded({
            "track_index": track_index,
            "clip_index": clip_index,
            "arrangement": arrangement
        }, expect_track_name))

    def set_clip_name(self, track_index: int, clip_index: int, name: str,
                      expect_track_name: Optional[str] = None
                      ) -> Dict[str, Any]:
        return self._send("set_clip_name", _guarded({
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        }, expect_track_name))

    def set_arrangement_clip_name(self, track_index: int, clip_index: int,
                                  name: str,
                                  expect_track_name: Optional[str] = None
                                  ) -> Dict[str, Any]:
        return self._send("set_arrangement_clip_name", _guarded({
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        }, expect_track_name))

    def set_tempo(self, tempo: float) -> Dict[str, Any]:
        return self._send("set_tempo", {"tempo": tempo})

    def load_browser_item(self, track_index: int, item_uri: str,
                          track_type: Optional[str] = None,
                          expect_track_name: Optional[str] = None
                          ) -> Dict[str, Any]:
        """The wire command behind all three load tools. The drum-kit
        orchestration has never sent a track_type, so the params dict only
        carries the key when the caller gives one — the wire exchange stays
        exactly what each tool always sent."""
        params: Dict[str, Any] = {
            "track_index": track_index,
            "item_uri": item_uri,
        }
        if track_type is not None:
            params["track_type"] = track_type
        return self._send("load_browser_item",
                          _guarded(params, expect_track_name))

    def fire_clip(self, track_index: int, clip_index: int,
                  expect_track_name: Optional[str] = None) -> Dict[str, Any]:
        return self._send("fire_clip", _guarded({
            "track_index": track_index,
            "clip_index": clip_index
        }, expect_track_name))

    def stop_clip(self, track_index: int, clip_index: int,
                  expect_track_name: Optional[str] = None) -> Dict[str, Any]:
        return self._send("stop_clip", _guarded({
            "track_index": track_index,
            "clip_index": clip_index
        }, expect_track_name))

    def delete_clip(self, track_index: int, clip_index: int,
                    expect_track_name: Optional[str] = None) -> Dict[str, Any]:
        return self._send("delete_clip", _guarded({
            "track_index": track_index,
            "clip_index": clip_index,
        }, expect_track_name))

    def start_playback(self) -> Dict[str, Any]:
        return self._send("start_playback")

    def stop_playback(self) -> Dict[str, Any]:
        return self._send("stop_playback")

    def get_browser_tree(self, category_type: str) -> Dict[str, Any]:
        return self._send("get_browser_tree", {
            "category_type": category_type
        })

    def get_browser_items_at_path(self, path: str) -> Dict[str, Any]:
        return self._send("get_browser_items_at_path", {
            "path": path
        })

    def load_drum_kit(self, track_index: int, rack_uri: str, kit_path: str,
                      expect_track_name: Optional[str] = None
                      ) -> Dict[str, Any]:
        """The one true multi-step orchestration: three wire calls with
        decisions between them (docs/REFACTOR_PLAN.md §3.2). Returns a
        stage-tagged plain dict — the presenter owns every string, the
        intermediate-failure ones included:

        - ``{"stage": "rack_failed", "rack_uri": ...}``
        - ``{"stage": "kit_lookup_failed", "error": ...}``
        - ``{"stage": "no_loadable", "kit_path": ...}``
        - ``{"stage": "ok", "kit_name": ..., "track_index": ...}``
        """
        # Step 1: Load the drum rack. The guard rides on both loads — they
        # target the same track, and the second one lands a kit inside
        # whatever rack the first one found.
        result = self.load_browser_item(track_index, rack_uri,
                                        expect_track_name=expect_track_name)
        if not result.get("loaded", False):
            return {"stage": "rack_failed", "rack_uri": rack_uri}

        # Step 2: Get the drum kit items at the specified path
        kit_result = self.get_browser_items_at_path(kit_path)
        if "error" in kit_result:
            return {"stage": "kit_lookup_failed",
                    "error": kit_result.get("error")}

        # Step 3: Find a loadable drum kit. kit_path may name a folder (load
        # its first loadable kit) or point directly at a kit file such as
        # "drums/808 Core Kit.adg" — then the node itself is the loadable item.
        kit_items = kit_result.get("items", [])
        loadable_kits = [item for item in kit_items
                         if item.get("is_loadable", False)]
        if not loadable_kits and kit_result.get("is_loadable") \
                and kit_result.get("uri"):
            loadable_kits = [kit_result]
        if not loadable_kits:
            return {"stage": "no_loadable", "kit_path": kit_path}

        # Step 4: Load the first loadable kit
        self.load_browser_item(track_index, loadable_kits[0].get("uri"),
                               expect_track_name=expect_track_name)
        return {"stage": "ok", "kit_name": loadable_kits[0].get("name"),
                "track_index": track_index}

    # ── Arrangement view commands ─────────────────────────────────────────────

    def switch_to_arrangement_view(self) -> Dict[str, Any]:
        return self._send("switch_to_arrangement_view")

    def set_current_song_time(self, time: float) -> Dict[str, Any]:
        return self._send("set_current_song_time", {"time": time})

    def get_arrangement_clips(self, track_index: int) -> Dict[str, Any]:
        return self._send("get_arrangement_clips",
                          {"track_index": track_index})

    def duplicate_session_clip_to_arrangement(
        self,
        track_index: int,
        clip_index: int,
        destination_time: Optional[float] = None,
        destination_times: Optional[List[float]] = None,
        allow_loop_phase_reset: bool = False,
        expect_track_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "track_index": track_index,
            "clip_index": clip_index,
        }
        # One key or the other, never both: the script branches on
        # destination_times being present at all, so a scalar sent
        # alongside a list would look placed and be silently ignored.
        if destination_times is not None:
            params["destination_times"] = destination_times
        else:
            params["destination_time"] = destination_time
        # An override of a refusal, not part of an ordinary stamp — sent
        # only when the caller has actually accepted the re-phasing, so an
        # ordinary call cannot carry it by accident.
        if allow_loop_phase_reset:
            params["allow_loop_phase_reset"] = True
        return self._send("duplicate_session_clip_to_arrangement",
                          _guarded(params, expect_track_name))

    def create_locator(self, name: str, time: float) -> Dict[str, Any]:
        return self._send(
            "create_locator",
            {"name": name, "time": time}
        )

    def delete_locator(self, name: str = "",
                       time: Optional[float] = None) -> Dict[str, Any]:
        # Presence-driven, exactly like jump_to_locator below: the script
        # matches by name first and falls back to the beat, so sending a
        # criterion the caller never gave would change which cue is found —
        # and this command then TOGGLES at whatever it found.
        params: Dict[str, Any] = {}
        if name:
            params["name"] = name
        if time is not None:
            params["time"] = time
        return self._send("delete_locator", params)

    def jump_to_locator(self, name: str = "",
                        time: Optional[float] = None) -> Dict[str, Any]:
        # Only the criteria actually given go on the wire, so the script's
        # name-first/time-second matching order is driven by presence.
        params: Dict[str, Any] = {}
        if name:
            params["name"] = name
        if time is not None:
            params["time"] = time
        return self._send("jump_to_locator", params)

    def trim_arrangement_clip(self, track_index: int, clip_index: int,
                              start_time: Optional[float] = None,
                              end_time: Optional[float] = None,
                              expect_track_name: Optional[str] = None
                              ) -> Dict[str, Any]:
        # An omitted edge stays where it is; only requested edges go on the
        # wire so the script can tell "leave alone" from "trim to".
        params: Dict[str, Any] = {
            "track_index": track_index,
            "clip_index": clip_index,
        }
        if start_time is not None:
            params["start_time"] = start_time
        if end_time is not None:
            params["end_time"] = end_time
        return self._send("trim_arrangement_clip",
                          _guarded(params, expect_track_name))

    def delete_arrangement_clip(self, track_index: int,
                                clip_index: Optional[int] = None,
                                start_time: Optional[float] = None,
                                start_times: Optional[List[float]] = None,
                                expect_track_name: Optional[str] = None
                                ) -> Dict[str, Any]:
        # Exactly one way of saying which clip goes on the wire, in the
        # script's own order of preference. Sending a leftover ordinal
        # beside a beat position would leave the choice of which one wins
        # to the handler's argument order rather than to the caller.
        params: Dict[str, Any] = {"track_index": track_index}
        if start_times is not None:
            params["start_times"] = start_times
        elif start_time is not None:
            params["start_time"] = start_time
        elif clip_index is not None:
            params["clip_index"] = clip_index
        # Nothing at all is a legitimate send: the script answers with the
        # track's actual clip positions, which is a better prompt than
        # anything this layer could compose without reading Live.
        return self._send("delete_arrangement_clip",
                          _guarded(params, expect_track_name))

    def move_arrangement_clip(self, track_index: int, clip_index: int,
                              destination_time: float,
                              expect_track_name: Optional[str] = None
                              ) -> Dict[str, Any]:
        return self._send(
            "move_arrangement_clip",
            _guarded({
                "track_index": track_index,
                "clip_index": clip_index,
                "destination_time": destination_time,
            }, expect_track_name)
        )

    def duplicate_arrangement_clip(self, track_index: int, clip_index: int,
                                   destination_time: float,
                                   allow_loop_phase_reset: bool = False,
                                   expect_track_name: Optional[str] = None
                                   ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "track_index": track_index,
            "clip_index": clip_index,
            "destination_time": destination_time,
        }
        # Same override, same reason as the session stamp above.
        if allow_loop_phase_reset:
            params["allow_loop_phase_reset"] = True
        return self._send("duplicate_arrangement_clip",
                          _guarded(params, expect_track_name))
