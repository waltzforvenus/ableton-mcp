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

    def get_clip_notes(self, track_index: int,
                       clip_index: int) -> Dict[str, Any]:
        return self._send(
            "get_clip_notes",
            {"track_index": track_index, "clip_index": clip_index},
        )

    def get_session_snapshot(self, include_notes: bool,
                             include_params: bool) -> Dict[str, Any]:
        return self._send(
            "get_session_snapshot",
            {
                "include_notes": include_notes,
                "include_params": include_params,
            },
        )

    def create_midi_track(self, index: int) -> Dict[str, Any]:
        return self._send("create_midi_track", {"index": index})

    def create_audio_track(self, index: int) -> Dict[str, Any]:
        return self._send("create_audio_track", {"index": index})

    def set_track_name(self, track_index: int, name: str) -> Dict[str, Any]:
        return self._send("set_track_name",
                          {"track_index": track_index, "name": name})

    def create_clip(self, track_index: int, clip_index: int,
                    length: float) -> Dict[str, Any]:
        return self._send("create_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "length": length
        })

    def set_clip_gain(self, track_index: int, clip_index: int, gain: float,
                      arrangement: bool) -> Dict[str, Any]:
        return self._send("set_clip_gain", {
            "track_index": track_index,
            "clip_index": clip_index,
            "gain": gain,
            "arrangement": arrangement
        })

    def back_to_arrangement(self) -> Dict[str, Any]:
        return self._send("back_to_arrangement", {})

    def get_track_routing(self, track_index: int) -> Dict[str, Any]:
        return self._send("get_track_routing", {"track_index": track_index})

    def set_track_routing(self, track_index: int, target: str,
                          field: str) -> Dict[str, Any]:
        return self._send("set_track_routing", {
            "track_index": track_index,
            "field": field,
            "target": target
        })

    def set_count_in(self, bars: int, metronome: bool) -> Dict[str, Any]:
        return self._send("set_count_in", {
            "bars": bars,
            "metronome": metronome
        })

    def set_track_send(self, track_index: int, send_index: int,
                       value: float) -> Dict[str, Any]:
        return self._send("set_track_send", {
            "track_index": track_index,
            "send_index": send_index,
            "value": value
        })

    def save_set(self) -> Dict[str, Any]:
        return self._send("save_set", {})

    def create_return_track(self) -> Dict[str, Any]:
        return self._send("create_return_track", {})

    def set_track_arm(self, track_index: int, armed: bool) -> Dict[str, Any]:
        return self._send("set_track_arm", {
            "track_index": track_index,
            "value": armed
        })

    def set_track_monitoring(self, track_index: int,
                             state: str) -> Dict[str, Any]:
        return self._send("set_track_monitoring", {
            "track_index": track_index,
            "value": state
        })

    def get_device_parameters(self, track_index: int, device_index: int,
                              track_type: str) -> Dict[str, Any]:
        return self._send("get_device_parameters", {
            "track_index": track_index,
            "device_index": device_index,
            "track_type": track_type
        })

    def set_device_parameter(self, track_index: int, device_index: int,
                             parameter: Union[str, int], value: float,
                             track_type: str) -> Dict[str, Any]:
        # ``parameter`` arrives already coerced (a name, or an int index) —
        # the string-to-int parse is boundary work and stays in the
        # controller.
        return self._send("set_device_parameter", {
            "track_index": track_index,
            "device_index": device_index,
            "parameter": parameter,
            "value": value,
            "track_type": track_type
        })

    def delete_device(self, track_index: int, device_index: int,
                      track_type: str) -> Dict[str, Any]:
        return self._send("delete_device", {
            "track_index": track_index,
            "device_index": device_index,
            "track_type": track_type
        })

    def set_track_volume(self, track_index: int, value: float,
                         track_type: str) -> Dict[str, Any]:
        return self._send("set_track_volume", {
            "track_index": track_index,
            "value": value,
            "track_type": track_type
        })

    def set_track_pan(self, track_index: int, value: float,
                      track_type: str) -> Dict[str, Any]:
        return self._send("set_track_pan", {
            "track_index": track_index,
            "value": value,
            "track_type": track_type
        })

    def set_track_mute(self, track_index: int, mute: bool) -> Dict[str, Any]:
        return self._send("set_track_mute", {
            "track_index": track_index,
            "value": mute
        })

    def delete_track(self, track_index: int) -> Dict[str, Any]:
        return self._send("delete_track", {
            "track_index": track_index
        })

    def create_audio_clip(self, track_index: int, clip_index: int,
                          path: str) -> Dict[str, Any]:
        return self._send("create_audio_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "path": path
        })

    def add_notes_to_clip(
        self,
        track_index: int,
        clip_index: int,
        notes: List[Dict[str, Union[int, float, bool]]],
    ) -> Dict[str, Any]:
        return self._send("add_notes_to_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "notes": notes
        })

    def clear_notes_from_clip(self, track_index: int,
                              clip_index: int) -> Dict[str, Any]:
        return self._send("clear_notes_from_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })

    def set_clip_name(self, track_index: int, clip_index: int,
                      name: str) -> Dict[str, Any]:
        return self._send("set_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })

    def set_arrangement_clip_name(self, track_index: int, clip_index: int,
                                  name: str) -> Dict[str, Any]:
        return self._send("set_arrangement_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })

    def set_tempo(self, tempo: float) -> Dict[str, Any]:
        return self._send("set_tempo", {"tempo": tempo})

    def load_browser_item(self, track_index: int, item_uri: str,
                          track_type: Optional[str] = None) -> Dict[str, Any]:
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
        return self._send("load_browser_item", params)

    def fire_clip(self, track_index: int, clip_index: int) -> Dict[str, Any]:
        return self._send("fire_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })

    def stop_clip(self, track_index: int, clip_index: int) -> Dict[str, Any]:
        return self._send("stop_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })

    def delete_clip(self, track_index: int, clip_index: int) -> Dict[str, Any]:
        return self._send("delete_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
        })

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

    def load_drum_kit(self, track_index: int, rack_uri: str,
                      kit_path: str) -> Dict[str, Any]:
        """The one true multi-step orchestration: three wire calls with
        decisions between them (docs/REFACTOR_PLAN.md §3.2). Returns a
        stage-tagged plain dict — the presenter owns every string, the
        intermediate-failure ones included:

        - ``{"stage": "rack_failed", "rack_uri": ...}``
        - ``{"stage": "kit_lookup_failed", "error": ...}``
        - ``{"stage": "no_loadable", "kit_path": ...}``
        - ``{"stage": "ok", "kit_name": ..., "track_index": ...}``
        """
        # Step 1: Load the drum rack
        result = self.load_browser_item(track_index, rack_uri)
        if not result.get("loaded", False):
            return {"stage": "rack_failed", "rack_uri": rack_uri}

        # Step 2: Get the drum kit items at the specified path
        kit_result = self.get_browser_items_at_path(kit_path)
        if "error" in kit_result:
            return {"stage": "kit_lookup_failed",
                    "error": kit_result.get("error")}

        # Step 3: Find a loadable drum kit
        kit_items = kit_result.get("items", [])
        loadable_kits = [item for item in kit_items
                         if item.get("is_loadable", False)]
        if not loadable_kits:
            return {"stage": "no_loadable", "kit_path": kit_path}

        # Step 4: Load the first loadable kit
        self.load_browser_item(track_index, loadable_kits[0].get("uri"))
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
        destination_time: float,
    ) -> Dict[str, Any]:
        return self._send(
            "duplicate_session_clip_to_arrangement",
            {
                "track_index": track_index,
                "clip_index": clip_index,
                "destination_time": destination_time
            }
        )

    def create_locator(self, name: str, time: float) -> Dict[str, Any]:
        return self._send(
            "create_locator",
            {"name": name, "time": time}
        )
