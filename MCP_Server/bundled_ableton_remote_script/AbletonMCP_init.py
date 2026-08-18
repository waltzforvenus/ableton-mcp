# AbletonMCP/init.py
from __future__ import absolute_import, print_function, unicode_literals

from _Framework.ControlSurface import ControlSurface
import os
import socket
import json
import threading
import time
import traceback

# Change queue import for Python 2
try:
    import Queue as queue  # Python 2
except ImportError:
    import queue  # Python 3

# Constants for socket communication
DEFAULT_PORT = 9877
# Bind to loopback only. The upstream default of "0.0.0.0" exposes Live's
# control socket to every host on the local network, and that socket accepts
# arbitrary commands with no authentication. The MCP server always connects
# from localhost, so loopback costs nothing.
HOST = "127.0.0.1"

# Bumped whenever the TCP command surface changes; the MCP server compares
# this to EXPECTED_REMOTE_SCRIPT_VERSION.
SCRIPT_VERSION = "1.8.0"
PROTOCOL_VERSION = 1

# Wire-command dispatch table: every command _process_command accepts, in one
# place. Each row is
#
#     command name: (handler method, main_thread, queue_timeout, advertise)
#
# - handler method: dispatched as getattr(self, method)(**params), so the wire
#   parameter names ARE the handler's keyword arguments and Python itself
#   enforces arity — the check whose absence let the 2026-08 merge's duplicate
#   definitions ship silently.
# - main_thread: True means the command modifies Live's state and must run on
#   Live's main thread (scheduled via schedule_message); False means read-only,
#   run directly on the socket client thread.
# - queue_timeout: seconds to wait for the main-thread task's response queue;
#   None means the default (10.0). create_audio_clip decodes/imports the file
#   on the main thread and needs more than the default headroom.
# - advertise: True puts the command in SCRIPT_CAPABILITIES, the capability
#   list get_script_info reports to the MCP server. The dispatchable set is
#   deliberately wider than the advertised one (legacy upstream commands and
#   the two rack orphans stay reachable but unadvertised), so this flag — not
#   naive derivation from the table's keys — is what keeps the wire response
#   stable. A guardrail test pins the derived list against an explicit
#   snapshot.
#
# The table must stay a pure literal (ast.literal_eval-able — no lambdas, no
# adapter callables): the guardrail tests read it with ast.parse, because this
# module imports _Framework and cannot be imported outside Live.
COMMANDS = {
    # Read-only commands — run directly on the socket client thread.
    "get_script_info":            ("_get_script_info",            False, None, True),
    "get_session_info":           ("_get_session_info",           False, None, True),
    "get_track_info":             ("_get_track_info",             False, None, True),
    "get_track_routing":          ("_get_track_routing",          False, None, True),
    "get_device_parameters":      ("_get_device_parameters",      False, None, True),
    "get_arrangement_clips":      ("_get_arrangement_clips",      False, None, True),
    "get_clip_notes":             ("_get_clip_notes",             False, None, True),
    "get_session_snapshot":       ("_get_session_snapshot",       False, None, True),
    "get_browser_item":           ("_get_browser_item",           False, None, False),
    "get_browser_tree":           ("get_browser_tree",            False, None, True),
    "get_browser_items_at_path":  ("get_browser_items_at_path",   False, None, True),
    # State-modifying commands — scheduled onto Live's main thread.
    "create_midi_track":          ("_create_midi_track",          True,  None, True),
    "create_audio_track":         ("_create_audio_track",         True,  None, True),
    "set_track_name":             ("_set_track_name",             True,  None, False),
    "create_clip":                ("_create_clip",                True,  None, True),
    "create_audio_clip":          ("_create_audio_clip",          True,  60.0, True),
    "add_notes_to_clip":          ("_add_notes_to_clip",          True,  None, True),
    "clear_notes_from_clip":      ("_clear_notes_from_clip",      True,  None, True),
    "set_clip_name":              ("_set_clip_name",              True,  None, False),
    "set_arrangement_clip_name":  ("_set_arrangement_clip_name",  True,  None, True),
    "set_tempo":                  ("_set_tempo",                  True,  None, False),
    "fire_clip":                  ("_fire_clip",                  True,  None, False),
    "stop_clip":                  ("_stop_clip",                  True,  None, False),
    "delete_clip":                ("_delete_clip",                True,  None, True),
    "delete_track":               ("_delete_track",               True,  None, True),
    "delete_device":              ("_delete_device",              True,  None, True),
    "set_device_parameter":       ("_set_device_parameter",       True,  None, True),
    "set_track_volume":           ("_set_track_volume",           True,  None, True),
    "set_track_pan":              ("_set_track_pan",              True,  None, True),
    "set_track_mute":             ("_set_track_mute",             True,  None, True),
    "create_return_track":        ("_create_return_track",        True,  None, True),
    "set_track_arm":              ("_set_track_arm",              True,  None, True),
    "set_track_monitoring":       ("_set_track_monitoring",       True,  None, True),
    "save_set":                   ("_save_set",                   True,  None, True),
    "set_track_send":             ("_set_track_send",             True,  None, True),
    "set_count_in":               ("_set_count_in",               True,  None, True),
    "back_to_arrangement":        ("_back_to_arrangement",        True,  None, True),
    "set_track_routing":          ("_set_track_routing",          True,  None, True),
    "set_clip_gain":              ("_set_clip_gain",              True,  None, True),
    "start_playback":             ("_start_playback",             True,  None, False),
    "stop_playback":              ("_stop_playback",              True,  None, False),
    "load_browser_item":          ("_load_browser_item",          True,  None, True),
    "load_instrument_or_effect":  ("_load_instrument_or_effect",  True,  None, True),
    "switch_to_arrangement_view": ("_switch_to_arrangement_view", True,  None, True),
    "set_current_song_time":      ("_set_current_song_time",      True,  None, True),
    "duplicate_session_clip_to_arrangement":
        ("_duplicate_session_clip_to_arrangement",                True,  None, True),
    "map_rack_magnitude":         ("_map_rack_magnitude",         True,  None, False),
    "inspect_rack":               ("_inspect_rack",               True,  None, False),
    "create_locator":             ("_create_locator",             True,  None, True),
}

# Derived, never hand-edited: the advertised subset of COMMANDS, in sorted
# order. Content is pinned by a guardrail test against the explicit pre-table
# list, so the derivation can never silently widen or shrink the wire response.
SCRIPT_CAPABILITIES = sorted(
    name for name, row in COMMANDS.items() if row[3]
)

def create_instance(c_instance):
    """Create and return the AbletonMCP script instance"""
    return AbletonMCP(c_instance)

class AbletonMCP(ControlSurface):
    """AbletonMCP Remote Script for Ableton Live"""
    
    def __init__(self, c_instance):
        """Initialize the control surface"""
        ControlSurface.__init__(self, c_instance)
        self.log_message(
            "AbletonMCP Remote Script initializing... (script v%s)"
            % SCRIPT_VERSION
        )
        
        # Socket server for communication
        self.server = None
        self.client_threads = []
        self.server_thread = None
        self.running = False
        
        # Cache the song reference for easier access
        self._song = self.song()
        
        # Start the socket server
        self.start_server()
        
        self.log_message("AbletonMCP initialized")
        
        # Show a message in Ableton
        self.show_message("AbletonMCP: Listening for commands on port " + str(DEFAULT_PORT))
    
    def disconnect(self):
        """Called when Ableton closes or the control surface is removed"""
        self.log_message("AbletonMCP disconnecting...")
        self.running = False
        
        # Stop the server
        if self.server:
            try:
                self.server.close()
            except:
                pass
        
        # Wait for the server thread to exit
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(1.0)
            
        # Clean up any client threads
        for client_thread in self.client_threads[:]:
            if client_thread.is_alive():
                # We don't join them as they might be stuck
                self.log_message("Client thread still alive during disconnect")
        
        ControlSurface.disconnect(self)
        self.log_message("AbletonMCP disconnected")
    
    def start_server(self):
        """Start the socket server in a separate thread"""
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind((HOST, DEFAULT_PORT))
            self.server.listen(5)  # Allow up to 5 pending connections
            
            self.running = True
            self.server_thread = threading.Thread(target=self._server_thread)
            self.server_thread.daemon = True
            self.server_thread.start()
            
            self.log_message("Server started on port " + str(DEFAULT_PORT))
        except Exception as e:
            self.log_message("Error starting server: " + str(e))
            self.show_message("AbletonMCP: Error starting server - " + str(e))
    
    def _server_thread(self):
        """Server thread implementation - handles client connections"""
        try:
            self.log_message("Server thread started")
            # Set a timeout to allow regular checking of running flag
            self.server.settimeout(1.0)
            
            while self.running:
                try:
                    # Accept connections with timeout
                    client, address = self.server.accept()
                    self.log_message("Connection accepted from " + str(address))
                    self.show_message("AbletonMCP: Client connected")
                    
                    # Handle client in a separate thread
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client,)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                    
                    # Keep track of client threads
                    self.client_threads.append(client_thread)
                    
                    # Clean up finished client threads
                    self.client_threads = [t for t in self.client_threads if t.is_alive()]
                    
                except socket.timeout:
                    # No connection yet, just continue
                    continue
                except Exception as e:
                    if self.running:  # Only log if still running
                        self.log_message("Server accept error: " + str(e))
                    time.sleep(0.5)
            
            self.log_message("Server thread stopped")
        except Exception as e:
            self.log_message("Server thread error: " + str(e))
    
    def _handle_client(self, client):
        """Handle communication with a connected client"""
        self.log_message("Client handler started")
        client.settimeout(None)  # No timeout for client socket
        buffer = ''  # Changed from b'' to '' for Python 2
        
        try:
            while self.running:
                try:
                    # Receive data
                    data = client.recv(8192)
                    
                    if not data:
                        # Client disconnected
                        self.log_message("Client disconnected")
                        break
                    
                    # Accumulate data in buffer with explicit encoding/decoding
                    try:
                        # Python 3: data is bytes, decode to string
                        buffer += data.decode('utf-8')
                    except AttributeError:
                        # Python 2: data is already string
                        buffer += data
                    
                    try:
                        # Try to parse command from buffer
                        command = json.loads(buffer)  # Removed decode('utf-8')
                        buffer = ''  # Clear buffer after successful parse
                        
                        self.log_message("Received command: " + str(command.get("type", "unknown")))
                        
                        # Process the command and get response
                        response = self._process_command(command)
                        
                        # Send the response with explicit encoding
                        try:
                            # Python 3: encode string to bytes
                            client.sendall(json.dumps(response).encode('utf-8'))
                        except AttributeError:
                            # Python 2: string is already bytes
                            client.sendall(json.dumps(response))
                    except ValueError:
                        # Incomplete data, wait for more
                        continue
                        
                except Exception as e:
                    self.log_message("Error handling client data: " + str(e))
                    self.log_message(traceback.format_exc())
                    
                    # Send error response if possible
                    error_response = {
                        "status": "error",
                        "message": str(e)
                    }
                    try:
                        # Python 3: encode string to bytes
                        client.sendall(json.dumps(error_response).encode('utf-8'))
                    except AttributeError:
                        # Python 2: string is already bytes
                        client.sendall(json.dumps(error_response))
                    except:
                        # If we can't send the error, the connection is probably dead
                        break
                    
                    # For serious errors, break the loop
                    if not isinstance(e, ValueError):
                        break
        except Exception as e:
            self.log_message("Error in client handler: " + str(e))
        finally:
            try:
                client.close()
            except:
                pass
            self.log_message("Client handler stopped")
    
    def _process_command(self, command):
        """Process a command from the client and return a response"""
        command_type = command.get("type", "")
        params = command.get("params", {})
        
        # Initialize response
        response = {
            "status": "success",
            "result": {}
        }
        
        try:
            # Route the command through the COMMANDS table. Wire parameter
            # names are the handler's keyword arguments, so **params both
            # dispatches and enforces arity; per-command defaults live on the
            # handler signatures.
            spec = COMMANDS.get(command_type)
            if spec is None:
                response["status"] = "error"
                response["message"] = "Unknown command: " + command_type
                return response

            method_name, main_thread, queue_timeout, _advertise = spec
            handler = getattr(self, method_name)

            if not main_thread:
                # Read-only commands run directly on this client thread.
                response["result"] = handler(**params)
                return response

            # Commands that modify Live's state must run on Live's main
            # thread. Use a thread-safe approach with a response queue.
            response_queue = queue.Queue()

            # Define a function to execute on the main thread
            def main_thread_task():
                try:
                    # The handler call happens inside the task so a bad
                    # parameter set (TypeError) reports through the same
                    # error envelope as any other handler failure.
                    result = handler(**params)
                    # Put the result in the queue
                    response_queue.put({"status": "success", "result": result})
                except Exception as e:
                    self.log_message("Error in main thread task: " + str(e))
                    self.log_message(traceback.format_exc())
                    response_queue.put({"status": "error", "message": str(e)})

            # Schedule the task to run on the main thread
            try:
                self.schedule_message(0, main_thread_task)
            except AssertionError:
                # If we're already on the main thread, execute directly
                main_thread_task()

            # queue_timeout comes from the COMMANDS row; None means the
            # default budget (create_audio_clip's 60.0 is the one override —
            # it decodes/imports the file on the main thread).
            if queue_timeout is None:
                queue_timeout = 10.0
            try:
                task_response = response_queue.get(timeout=queue_timeout)
                if task_response.get("status") == "error":
                    response["status"] = "error"
                    response["message"] = task_response.get("message", "Unknown error")
                else:
                    response["result"] = task_response.get("result", {})
            except queue.Empty:
                response["status"] = "error"
                response["message"] = "Timeout waiting for operation to complete"
        except Exception as e:
            self.log_message("Error processing command: " + str(e))
            self.log_message(traceback.format_exc())
            response["status"] = "error"
            response["message"] = str(e)
        
        return response
    
    # Command implementations

    def _get_script_info(self):
        """Handshake payload for MCP server version / capability checks."""
        return {
            "name": "AbletonMCP",
            "script_version": SCRIPT_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "port": DEFAULT_PORT,
            "capabilities": list(SCRIPT_CAPABILITIES),
            "snapshot_schema": "ableton_mcp_snapshot_v2",
        }
    
    def _safe_song_property(self, attr, cast, default):
        """Read self._song.<attr> with cast, returning default on common failures.
        Catches only narrow exceptions so genuine bugs still surface."""
        try:
            return cast(getattr(self._song, attr))
        except (AttributeError, TypeError, ValueError):
            return default

    def _get_session_info(self):
        """Get information about the current session"""
        try:
            result = {
                "tempo": self._song.tempo,
                "signature_numerator": self._song.signature_numerator,
                "signature_denominator": self._song.signature_denominator,
                "track_count": len(self._song.tracks),
                "return_track_count": len(self._song.return_tracks),
                "master_track": {
                    "name": "Master",
                    "volume": self._song.master_track.mixer_device.volume.value,
                    "panning": self._song.master_track.mixer_device.panning.value
                },
                # Read via _safe_song_property so an attribute missing on a
                # given Live version falls back to its default.
                "is_playing":        self._safe_song_property("is_playing",        bool,  False),
                "current_song_time": self._safe_song_property("current_song_time", float, 0.0),
                "song_length":       self._safe_song_property("song_length",       float, 0.0),
                "loop":              self._safe_song_property("loop",              bool,  False),
                "loop_start":        self._safe_song_property("loop_start",        float, 0.0),
                "loop_length":       self._safe_song_property("loop_length",       float, 0.0),
            }
            return result
        except Exception as e:
            self.log_message("Error getting session info: " + str(e))
            raise
    
    def _get_track_info(self, track_index=0):
        """Get information about a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            # Get clip slots
            clip_slots = []
            for slot_index, slot in enumerate(track.clip_slots):
                clip_info = None
                if slot.has_clip:
                    clip = slot.clip
                    clip_info = {
                        "name": clip.name,
                        "length": clip.length,
                        "is_playing": clip.is_playing,
                        "is_recording": clip.is_recording
                    }
                
                clip_slots.append({
                    "index": slot_index,
                    "has_clip": slot.has_clip,
                    "clip": clip_info
                })
            
            # Get devices
            devices = []
            for device_index, device in enumerate(track.devices):
                devices.append({
                    "index": device_index,
                    "name": device.name,
                    "class_name": device.class_name,
                    "type": self._get_device_type(device)
                })
            
            result = {
                "index": track_index,
                "name": track.name,
                "is_audio_track": track.has_audio_input,
                "is_midi_track": track.has_midi_input,
                "mute": track.mute,
                "solo": track.solo,
                "arm": track.arm,
                "volume": track.mixer_device.volume.value,
                "panning": track.mixer_device.panning.value,
                "clip_slots": clip_slots,
                "devices": devices
            }
            return result
        except Exception as e:
            self.log_message("Error getting track info: " + str(e))
            raise
    
    def _create_midi_track(self, index=-1):
        """Create a new MIDI track at the specified index"""
        try:
            # Create the track
            self._song.create_midi_track(index)
            
            # Get the new track
            new_track_index = len(self._song.tracks) - 1 if index == -1 else index
            new_track = self._song.tracks[new_track_index]
            
            result = {
                "index": new_track_index,
                "name": new_track.name
            }
            return result
        except Exception as e:
            self.log_message("Error creating MIDI track: " + str(e))
            raise

    def _set_track_name(self, track_index=0, name=""):
        """Set the name of a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            # Set the name
            track = self._song.tracks[track_index]
            track.name = name
            
            result = {
                "name": track.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting track name: " + str(e))
            raise
    
    def _create_clip(self, track_index=0, clip_index=0, length=4.0):
        """Create a new MIDI clip in the specified track and clip slot"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            # Check if the clip slot already has a clip
            if clip_slot.has_clip:
                raise Exception("Clip slot already has a clip")
            
            # Create the clip
            clip_slot.create_clip(length)
            
            result = {
                "name": clip_slot.clip.name,
                "length": clip_slot.clip.length
            }
            return result
        except Exception as e:
            self.log_message("Error creating clip: " + str(e))
            raise

    def _resolve_track(self, track_index, track_type="regular"):
        """Resolve a regular track, a return track, or the master track.

        track_type is "regular" (default), "return", or "master"; master ignores
        track_index. song.tracks contains neither the returns nor the master, so
        this is what lets the mixer and device commands reach them at all.
        """
        kind = (track_type or "regular").strip().lower()
        if kind in ("master", "main"):
            return self._song.master_track
        if kind in ("return", "send"):
            returns = self._song.return_tracks
            if track_index < 0 or track_index >= len(returns):
                raise IndexError("Return track index out of range (%d return tracks)"
                                 % len(returns))
            return returns[track_index]
        if track_index < 0 or track_index >= len(self._song.tracks):
            raise IndexError("Track index out of range")
        return self._song.tracks[track_index]

    def _resolve_device(self, track_index, device_index, track_type="regular"):
        track = self._resolve_track(track_index, track_type)
        if device_index < 0 or device_index >= len(track.devices):
            raise IndexError("Device index out of range (track has %d devices)"
                             % len(track.devices))
        return track, track.devices[device_index]

    def _get_device_parameters(self, track_index=0, device_index=0, track_type="regular"):
        """List every automatable parameter on a device, with its current value"""
        try:
            track, device = self._resolve_device(track_index, device_index, track_type)

            parameters = []
            for i, p in enumerate(device.parameters):
                entry = {
                    "index": i,
                    "name": p.name,
                    "value": p.value,
                    "min": p.min,
                    "max": p.max,
                    "is_quantized": bool(p.is_quantized),
                }
                # display_value is what Live shows in the UI (e.g. "-6.0 dB"),
                # which is far more useful than the raw float when deciding
                # what to set something to.
                try:
                    entry["display_value"] = str(p.str_for_value(p.value))
                except Exception:
                    entry["display_value"] = ""
                parameters.append(entry)

            return {
                "track_index": track_index,
                "track_name": track.name,
                "device_index": device_index,
                "device_name": device.name,
                "parameter_count": len(parameters),
                "parameters": parameters,
            }
        except Exception as e:
            self.log_message("Error getting device parameters: " + str(e))
            raise

    def _set_device_parameter(self, track_index=0, device_index=0, parameter=None,
                              value=0.0, track_type="regular"):
        """Set one device parameter, addressed by integer index or by name"""
        try:
            track, device = self._resolve_device(track_index, device_index, track_type)

            target = None
            if isinstance(parameter, bool):
                raise ValueError("parameter must be an index or a name, not a bool")
            elif isinstance(parameter, int):
                if parameter < 0 or parameter >= len(device.parameters):
                    raise IndexError("Parameter index out of range (device has %d)"
                                     % len(device.parameters))
                target = device.parameters[parameter]
            else:
                wanted = str(parameter).strip().lower()
                for p in device.parameters:
                    if p.name.strip().lower() == wanted:
                        target = p
                        break
                if target is None:
                    names = ", ".join([p.name for p in device.parameters])
                    raise ValueError("No parameter named '%s'. Available: %s"
                                     % (parameter, names))

            value = float(value)
            # Read before writing so the result can report what the caller
            # just overwrote (upstream's one good idea in its version).
            old_value = float(target.value)
            # Clamp rather than raise: Live throws on out-of-range assignment,
            # and a caller asking for "as low as it goes" should just get the min.
            clamped = max(target.min, min(target.max, value))
            target.value = clamped

            try:
                shown = str(target.str_for_value(target.value))
            except Exception:
                shown = ""

            return {
                "track_index": track_index,
                "device_index": device_index,
                "device_name": device.name,
                "parameter_name": target.name,
                "requested": value,
                "old_value": old_value,
                "value": target.value,
                "display_value": shown,
                "clamped": clamped != value,
                "min": target.min,
                "max": target.max,
            }
        except Exception as e:
            self.log_message("Error setting device parameter: " + str(e))
            raise

    def _delete_device(self, track_index=0, device_index=0, track_type="regular"):
        """Remove a device from a track's chain"""
        try:
            track, device = self._resolve_device(track_index, device_index, track_type)
            name = device.name
            track.delete_device(device_index)
            return {
                "track_index": track_index,
                "deleted_device_index": device_index,
                "deleted_device_name": name,
                "remaining_device_count": len(track.devices),
            }
        except Exception as e:
            self.log_message("Error deleting device: " + str(e))
            raise

    def _set_track_mixer(self, track_index, field, value, track_type="regular"):
        """Set mixer volume (0.0-1.0, 0.85 = 0 dB) or panning (-1.0 to 1.0).

        Works on regular tracks, return tracks and the master.
        """
        try:
            track = self._resolve_track(track_index, track_type)
            param = getattr(track.mixer_device, field)

            value = float(value)
            clamped = max(param.min, min(param.max, value))
            param.value = clamped

            try:
                shown = str(param.str_for_value(param.value))
            except Exception:
                shown = ""

            return {
                "track_index": track_index,
                "track_name": track.name,
                "field": field,
                "requested": value,
                "value": param.value,
                "display_value": shown,
                "clamped": clamped != value,
            }
        except Exception as e:
            self.log_message("Error setting track " + field + ": " + str(e))
            raise

    # Thin wire adapters: the set_track_volume / set_track_pan commands share
    # one implementation (_set_track_mixer) that also needs the mixer field
    # name, which is not a wire parameter. The COMMANDS table stays a pure
    # literal, so the field is injected here rather than by an adapter row.

    def _set_track_volume(self, track_index=0, value=0.85, track_type="regular"):
        """Set a track's volume fader (0.0-1.0, 0.85 = 0 dB)."""
        return self._set_track_mixer(track_index, "volume", value, track_type)

    def _set_track_pan(self, track_index=0, value=0.0, track_type="regular"):
        """Set a track's pan (-1.0 hard left to 1.0 hard right)."""
        return self._set_track_mixer(track_index, "panning", value, track_type)

    def _set_clip_gain(self, track_index=0, clip_index=0, gain=0.5, arrangement=True):
        """Set one audio clip's gain, without touching the track fader.

        This is what fixes a single section sung too loud: it changes that clip
        alone, where lowering the track would bury every other section and
        compressing harder squashes the whole performance.

        gain is Live's normalized 0.0-1.0, where 0.5 is roughly unity.
        """
        try:
            track = self._resolve_track(track_index)

            if arrangement:
                clips = list(track.arrangement_clips)
                if clip_index < 0 or clip_index >= len(clips):
                    raise IndexError("Arrangement clip index out of range (track has %d)"
                                     % len(clips))
                clip = clips[clip_index]
            else:
                if clip_index < 0 or clip_index >= len(track.clip_slots):
                    raise IndexError("Clip slot index out of range")
                slot = track.clip_slots[clip_index]
                if not slot.has_clip:
                    raise Exception("No clip in that slot")
                clip = slot.clip

            if clip.is_midi_clip:
                raise ValueError("Clip gain applies to audio clips only; this is a MIDI clip")

            value = float(gain)
            clip.gain = max(0.0, min(1.0, value))

            return {
                "track_index": track_index,
                "track_name": track.name,
                "clip_index": clip_index,
                "clip_name": clip.name,
                "arrangement": bool(arrangement),
                "gain": clip.gain,
                "gain_display": str(getattr(clip, "gain_display_string", "")),
                "clamped": clip.gain != value,
            }
        except Exception as e:
            self.log_message("Error setting clip gain: " + str(e))
            raise

    def _back_to_arrangement(self):
        """Hand every overridden track back to the Arrangement.

        Stopping a Session clip does NOT return its track to the timeline — the
        track goes silent until this is triggered. Without it, arrangement edits
        are inaudible while any Session clip has ever been launched.
        """
        try:
            self._song.back_to_arranger = False
            return {"back_to_arranger": bool(self._song.back_to_arranger),
                    "message": "All tracks returned to Arrangement playback"}
        except Exception as e:
            self.log_message("Error returning to arrangement: " + str(e))
            raise

    def _routing_name(self, obj):
        if obj is None:
            return None
        return getattr(obj, "display_name", str(obj))

    def _get_track_routing(self, track_index=0):
        """Report a track's input/output routing and every option available to it"""
        try:
            track = self._resolve_track(track_index)
            result = {"track_index": track_index, "track_name": track.name}
            for attr in ("output_routing_type", "output_routing_channel",
                         "input_routing_type", "input_routing_channel"):
                result[attr] = self._routing_name(getattr(track, attr, None))
            for attr in ("available_output_routing_types",
                         "available_output_routing_channels",
                         "available_input_routing_types",
                         "available_input_routing_channels"):
                options = getattr(track, attr, None)
                result[attr] = [self._routing_name(o) for o in options] if options else []
            return result
        except Exception as e:
            self.log_message("Error getting track routing: " + str(e))
            raise

    def _set_track_routing(self, track_index=0, field="output_routing_type",
                           target="Main"):
        """Set one routing field by its display name, e.g. output type 'Main'"""
        try:
            track = self._resolve_track(track_index)
            available_attr = "available_" + field + "s"
            options = getattr(track, available_attr, None)
            if not options:
                raise ValueError("Track exposes no %s" % available_attr)

            wanted = str(target).strip().lower()
            for option in options:
                if self._routing_name(option).strip().lower() == wanted:
                    setattr(track, field, option)
                    return {
                        "track_index": track_index,
                        "track_name": track.name,
                        "field": field,
                        "value": self._routing_name(getattr(track, field, None)),
                    }
            names = ", ".join([self._routing_name(o) for o in options])
            raise ValueError("No %s named '%s'. Available: %s" % (field, target, names))
        except Exception as e:
            self.log_message("Error setting track routing: " + str(e))
            raise

    def _set_count_in(self, bars=1, metronome=None):
        """Set the record count-in, so a performer gets a lead-in before punching in.

        This is the correct way to get a count-in: it happens only when
        recording, and it does not require shifting every clip in the
        arrangement to make room at the front.

        bars: 0 = None, 1 = 1 Bar, 2 = 2 Bars, 3 = 4 Bars (Live's own indices).

        NOTE: verified against Live 12.3.2 — `Song.count_in_duration` is exposed
        but READ-ONLY ("property of 'Song' object has no setter"). The same is
        true of any save. Both are reported as failures rather than silently
        swallowed, so a caller learns the API route is closed and reaches for
        the UI instead of assuming it worked.
        """
        try:
            mapping = {0: "None", 1: "1 Bar", 2: "2 Bars", 3: "4 Bars"}
            if isinstance(bars, str):
                lookup = {"none": 0, "0": 0, "1": 1, "1 bar": 1,
                          "2": 2, "2 bars": 2, "4": 3, "4 bars": 3}
                key = bars.strip().lower()
                if key not in lookup:
                    raise ValueError("count-in must be none, 1, 2 or 4 bars")
                value = lookup[key]
            else:
                value = int(bars)
            if value not in mapping:
                raise ValueError("count-in index must be 0 (None), 1, 2 or 3 (4 Bars)")

            self._song.count_in_duration = value

            # A count-in you cannot hear is useless, so allow turning the
            # metronome on in the same call.
            if metronome is not None:
                self._song.metronome = bool(metronome)

            return {
                "count_in_duration": int(self._song.count_in_duration),
                "count_in": mapping.get(int(self._song.count_in_duration), "?"),
                "metronome": bool(self._song.metronome),
            }
        except Exception as e:
            self.log_message("Error setting count-in: " + str(e))
            raise

    def _set_track_send(self, track_index=0, send_index=0, value=0.0):
        """Set how much of a track is sent to a return track (0.0-1.0).

        A newly created return track receives nothing until this is raised —
        Live starts every send at -inf — so a shared reverb bus is silent
        without it.
        """
        try:
            track = self._resolve_track(track_index)
            sends = track.mixer_device.sends
            if send_index < 0 or send_index >= len(sends):
                raise IndexError("Send index out of range (track has %d sends)" % len(sends))

            param = sends[send_index]
            value = float(value)
            clamped = max(param.min, min(param.max, value))
            param.value = clamped

            try:
                shown = str(param.str_for_value(param.value))
            except Exception:
                shown = ""

            return {
                "track_index": track_index,
                "track_name": track.name,
                "send_index": send_index,
                "value": param.value,
                "display_value": shown,
                "clamped": clamped != value,
            }
        except Exception as e:
            self.log_message("Error setting track send: " + str(e))
            raise

    def _set_track_mute(self, track_index=0, value=False):
        """Mute or unmute a track"""
        try:
            track = self._resolve_track(track_index)
            track.mute = bool(value)
            return {
                "track_index": track_index,
                "track_name": track.name,
                "mute": bool(track.mute),
            }
        except Exception as e:
            self.log_message("Error setting track mute: " + str(e))
            raise

    def _delete_track(self, track_index=0):
        """Delete a track from the song"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            deleted_name = self._song.tracks[track_index].name

            self._song.delete_track(track_index)

            result = {
                "deleted_track_index": track_index,
                "deleted_track_name": deleted_name,
                "remaining_track_count": len(self._song.tracks)
            }
            return result
        except Exception as e:
            self.log_message("Error deleting track: " + str(e))
            raise

    def _create_audio_clip(self, track_index=0, clip_index=0, path=""):
        """Create an audio clip in the specified audio track clip slot by importing a file.

        Requires Ableton Live 12.0.5 or newer (the underlying
        ClipSlot.create_audio_clip Live API was introduced in 12.0.5 — it is
        not available in earlier 12.0.x releases).
        """
        try:
            if not path:
                raise ValueError("Audio file path is required")

            if not os.path.isabs(path):
                raise ValueError("Audio file path must be absolute (got: %s)" % path)

            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            if getattr(track, "has_midi_input", False) or not getattr(track, "has_audio_input", True):
                raise ValueError("Track %d is not an audio track" % track_index)

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            if clip_slot.has_clip:
                raise Exception("Clip slot already has a clip")

            if not hasattr(clip_slot, "create_audio_clip"):
                raise Exception(
                    "ClipSlot.create_audio_clip is unavailable in this Ableton Live "
                    "version. Requires Live 12.0.5 or newer."
                )

            clip_slot.create_audio_clip(path)

            result = {
                "name": clip_slot.clip.name,
                "length": clip_slot.clip.length,
                "is_audio_clip": clip_slot.clip.is_audio_clip
            }
            return result
        except Exception as e:
            self.log_message("Error creating audio clip: " + str(e))
            raise

    # notes defaults to an empty tuple, not [] — same "no notes" wire default
    # the dispatcher used to supply, without a mutable default argument.
    def _add_notes_to_clip(self, track_index=0, clip_index=0, notes=()):
        """Add MIDI notes to a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip = clip_slot.clip
            
            # Convert note data to Live's format
            live_notes = []
            for note in notes:
                pitch = note.get("pitch", 60)
                start_time = note.get("start_time", 0.0)
                duration = note.get("duration", 0.25)
                velocity = note.get("velocity", 100)
                mute = note.get("mute", False)
                
                live_notes.append((pitch, start_time, duration, velocity, mute))
            
            # Add the notes
            clip.set_notes(tuple(live_notes))
            
            result = {
                "note_count": len(notes)
            }
            return result
        except Exception as e:
            self.log_message("Error adding notes to clip: " + str(e))
            raise
    
    def _set_clip_name(self, track_index=0, clip_index=0, name=""):
        """Set the name of a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip = clip_slot.clip
            clip.name = name
            
            result = {
                "name": clip.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting clip name: " + str(e))
            raise

    def _set_arrangement_clip_name(self, track_index=0, clip_index=0, name=""):
        """Set the name of a clip placed in the Arrangement timeline.

        clip_index indexes into track.arrangement_clips, in the same order
        as returned by _get_arrangement_clips (i.e. ordered by start_time).
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]
            arrangement_clips = list(track.arrangement_clips)

            if clip_index < 0 or clip_index >= len(arrangement_clips):
                raise IndexError("Clip index out of range")

            clip = arrangement_clips[clip_index]
            clip.name = name

            result = {
                "name": clip.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting arrangement clip name: " + str(e))
            raise

    def _set_tempo(self, tempo=120.0):
        """Set the tempo of the session"""
        try:
            self._song.tempo = tempo
            
            result = {
                "tempo": self._song.tempo
            }
            return result
        except Exception as e:
            self.log_message("Error setting tempo: " + str(e))
            raise
    
    def _fire_clip(self, track_index=0, clip_index=0):
        """Fire a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip_slot.fire()
            
            result = {
                "fired": True
            }
            return result
        except Exception as e:
            self.log_message("Error firing clip: " + str(e))
            raise
    
    def _stop_clip(self, track_index=0, clip_index=0):
        """Stop a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            clip_slot.stop()
            
            result = {
                "stopped": True
            }
            return result
        except Exception as e:
            self.log_message("Error stopping clip: " + str(e))
            raise

    def _delete_clip(self, track_index=0, clip_index=0):
        """Delete the clip in the given clip slot, freeing the slot for reuse."""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            if not clip_slot.has_clip:
                return {"deleted": False, "reason": "Clip slot was already empty"}

            # Read the name before deleting — the clip object is gone afterwards
            deleted_name = clip_slot.clip.name

            clip_slot.delete_clip()

            return {"deleted": True, "deleted_clip_name": deleted_name}
        except Exception as e:
            self.log_message("Error deleting clip: " + str(e))
            raise


    def _start_playback(self):
        """Start playing the session"""
        try:
            self._song.start_playing()
            
            result = {
                "playing": self._song.is_playing
            }
            return result
        except Exception as e:
            self.log_message("Error starting playback: " + str(e))
            raise
    
    def _stop_playback(self):
        """Stop playing the session"""
        try:
            self._song.stop_playing()
            
            result = {
                "playing": self._song.is_playing
            }
            return result
        except Exception as e:
            self.log_message("Error stopping playback: " + str(e))
            raise
    
    # ── Arrangement view implementations ──────────────────────────────────────

    def _switch_to_arrangement_view(self):
        """Switch Ableton's main window to the Arrangement view"""
        try:
            self.application().view.show_view("Arranger")
            return {"view": "Arranger"}
        except Exception as e:
            self.log_message("Error switching to arrangement view: " + str(e))
            raise

    def _set_current_song_time(self, time=0.0):
        """Move the arrangement playhead to a position in beats.

        The parameter is named for the wire ("time"); it shadows the time
        module inside this method only, and the body never uses the module.
        """
        try:
            self._song.current_song_time = float(time)
            return {"current_song_time": self._song.current_song_time}
        except Exception as e:
            self.log_message("Error setting current song time: " + str(e))
            raise

    def _get_arrangement_clips(self, track_index=0):
        """Return all clips placed in the Arrangement timeline for a track.

        Each clip dict contains:
          name, start_time, end_time, length, color,
          is_midi_clip, is_audio_clip, is_playing
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]
            clips = []

            # track.arrangement_clips is available in Live 11 / 12
            for clip in track.arrangement_clips:
                clips.append({
                    "name": clip.name,
                    "start_time": clip.start_time,
                    "end_time": clip.end_time,
                    "length": clip.length,
                    "color": clip.color,
                    "is_midi_clip": clip.is_midi_clip,
                    "is_audio_clip": clip.is_audio_clip,
                    # Report gain so a too-loud section can be found and fixed
                    # without guessing at its current level.
                    "gain": getattr(clip, "gain", None) if clip.is_audio_clip else None,
                    "gain_display": (str(getattr(clip, "gain_display_string", ""))
                                     if clip.is_audio_clip else ""),
                    "is_playing": clip.is_playing
                })

            return {
                "track_index": track_index,
                "track_name": track.name,
                "clip_count": len(clips),
                "clips": clips
            }
        except Exception as e:
            self.log_message("Error getting arrangement clips: " + str(e))
            raise

    def _clear_notes_from_clip(self, track_index=0, clip_index=0):
        """Remove all MIDI notes from a Session clip.

        Pairs with _add_notes_to_clip to make a real replace (clear, then add),
        which the write-only API otherwise can't do. Counts notes first so the
        result can report how many were removed.
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            if not clip_slot.has_clip:
                raise Exception("No clip in slot")

            clip = clip_slot.clip

            if not clip.is_midi_clip:
                raise Exception("Clip is not a MIDI clip; no notes to clear")

            length = clip.length

            # Count existing notes for the report (best-effort; never fatal).
            cleared = 0
            try:
                getter = getattr(clip, "get_notes_extended", None)
                if getter is not None:
                    cleared = len(list(getter(0, 128, 0.0, length)))
                else:
                    cleared = len(list(clip.get_notes(0.0, 0, length, 128)))
            except Exception:
                cleared = 0

            # Remove every note across the full pitch/time range. Prefer the
            # modern API (Live 11+); fall back to the legacy signature. Argument
            # order mirrors the get/remove _extended family:
            #   remove_notes_extended(from_pitch, pitch_span, from_time, time_span)
            # vs the legacy remove_notes(from_time, from_pitch, time_span, pitch_span).
            remover = getattr(clip, "remove_notes_extended", None)
            if remover is not None:
                remover(0, 128, 0.0, length)
            else:
                clip.remove_notes(0.0, 0, length, 128)

            return {
                "track_index": track_index,
                "clip_index": clip_index,
                "clip_name": clip.name,
                "cleared_count": cleared,
            }
        except Exception as e:
            self.log_message("Error clearing notes from clip: " + str(e))
            raise

    def _duplicate_session_clip_to_arrangement(self, track_index=0, clip_index=0,
                                               destination_time=0.0):
        """Copy a Session-view clip into the Arrangement timeline.

        Uses the real Live API:
          track.duplicate_clip_to_arrangement(clip, destination_time)

        Available in Live 11 / 12.  destination_time is in beats from the
        start of the arrangement.
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip slot index out of range")

            clip_slot = track.clip_slots[clip_index]

            if not clip_slot.has_clip:
                raise Exception(
                    "No clip in slot " + str(clip_index) +
                    " on track " + str(track_index)
                )

            clip = clip_slot.clip

            # Duplicate to arrangement at the requested beat position
            track.duplicate_clip_to_arrangement(clip, float(destination_time))

            return {
                "success": True,
                "track_index": track_index,
                "track_name": track.name,
                "clip_name": clip.name,
                "destination_time": destination_time
            }
        except Exception as e:
            self.log_message("Error duplicating clip to arrangement: " + str(e))
            raise

    def _create_locator(self, name="", time=0.0):
        """Create (or rename) a named locator at the given beat position.

        Uses Live's Song.set_or_delete_cue(), which toggles a cue at the
        current_song_time. We temporarily move the playhead, toggle, then
        restore. If a cue already exists at that time we just rename it
        instead of toggling (which would delete it).

        The second parameter is named for the wire ("time"); it shadows the
        time module inside this method only, and the body never uses the
        module.
        """
        try:
            song = self._song
            target_time = float(time)
            tolerance = 1e-3

            # See if a cue already exists at (or near) the target time
            existing = None
            for cue in song.cue_points:
                if abs(cue.time - target_time) < tolerance:
                    existing = cue
                    break

            original_time = song.current_song_time

            if existing is None:
                # Move playhead, toggle to create, then locate the new cue
                song.current_song_time = target_time
                song.set_or_delete_cue()
                for cue in song.cue_points:
                    if abs(cue.time - target_time) < tolerance:
                        existing = cue
                        break
                # Restore playhead
                try:
                    song.current_song_time = original_time
                except Exception:
                    pass

            if existing is None:
                raise Exception("Failed to create cue at time " + str(target_time))

            if name:
                try:
                    existing.name = str(name)
                except Exception as e:
                    self.log_message("Could not rename locator: " + str(e))

            return {
                "success": True,
                "time": existing.time,
                "name": existing.name,
            }
        except Exception as e:
            self.log_message("Error creating locator: " + str(e))
            raise

    # ── Browser implementations ───────────────────────────────────────────────

    def _get_browser_item(self, uri=None, path=None):
        """Get a browser item by URI or path"""
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            result = {
                "uri": uri,
                "path": path,
                "found": False
            }
            
            # Try to find by URI first if provided
            if uri:
                item = self._find_browser_item_by_uri(app.browser, uri)
                if item:
                    result["found"] = True
                    result["item"] = {
                        "name": item.name,
                        "is_folder": item.is_folder,
                        "is_device": item.is_device,
                        "is_loadable": item.is_loadable,
                        "uri": item.uri
                    }
                    return result
            
            # If URI not provided or not found, try by path
            if path:
                # Parse the path and navigate to the specified item
                path_parts = path.split("/")
                
                # Determine the root based on the first part
                current_item = None
                if path_parts[0].lower() == "instruments":
                    current_item = app.browser.instruments
                elif path_parts[0].lower() == "sounds":
                    current_item = app.browser.sounds
                elif path_parts[0].lower() == "drums":
                    current_item = app.browser.drums
                elif path_parts[0].lower() == "audio_effects":
                    current_item = app.browser.audio_effects
                elif path_parts[0].lower() == "midi_effects":
                    current_item = app.browser.midi_effects
                else:
                    # Default to instruments if not specified
                    current_item = app.browser.instruments
                    # Don't skip the first part in this case
                    path_parts = ["instruments"] + path_parts
                
                # Navigate through the path
                for i in range(1, len(path_parts)):
                    part = path_parts[i]
                    if not part:  # Skip empty parts
                        continue
                    
                    found = False
                    for child in current_item.children:
                        if child.name.lower() == part.lower():
                            current_item = child
                            found = True
                            break
                    
                    if not found:
                        result["error"] = "Path part '{0}' not found".format(part)
                        return result
                
                # Found the item
                result["found"] = True
                result["item"] = {
                    "name": current_item.name,
                    "is_folder": current_item.is_folder,
                    "is_device": current_item.is_device,
                    "is_loadable": current_item.is_loadable,
                    "uri": current_item.uri
                }
            
            return result
        except Exception as e:
            self.log_message("Error getting browser item: " + str(e))
            self.log_message(traceback.format_exc())
            raise   
    
    
    
    def _save_set(self):
        """Save the open Live Set, if this Live build exposes a way to do it.

        Live's Python API has never officially documented a save, but some
        builds expose Song.save_set or an equivalent on the Application. Try
        each candidate and report precisely which one worked — or report that
        none exist, so the caller knows to stop asking rather than assuming a
        silent success.
        """
        try:
            attempts = []

            for owner_name, owner in (("song", self._song),
                                      ("application", self.application())):
                if owner is None:
                    continue
                for attr in ("save_set", "save", "save_as", "save_document"):
                    fn = getattr(owner, attr, None)
                    if fn is None:
                        continue
                    if not callable(fn):
                        attempts.append("%s.%s exists but is not callable" % (owner_name, attr))
                        continue
                    try:
                        fn()
                        return {
                            "saved": True,
                            "method": "%s.%s()" % (owner_name, attr),
                            "attempts": attempts,
                        }
                    except Exception as inner:
                        attempts.append("%s.%s() raised %s" % (owner_name, attr, inner))

            return {
                "saved": False,
                "method": None,
                "attempts": attempts,
                "message": ("This Live build exposes no callable save through the "
                            "Python API; the set must be saved from the UI."),
            }
        except Exception as e:
            self.log_message("Error saving set: " + str(e))
            raise

    def _create_audio_track(self, index=-1):
        """Create a new audio track"""
        try:
            self._song.create_audio_track(index)
            new_track = self._song.tracks[index if index >= 0 else len(self._song.tracks) - 1]
            return {"index": list(self._song.tracks).index(new_track), "name": new_track.name}
        except Exception as e:
            self.log_message("Error creating audio track: " + str(e))
            raise

    def _create_return_track(self):
        """Create a new return track, for shared send effects"""
        try:
            self._song.create_return_track()
            returns = self._song.return_tracks
            t = returns[-1]
            return {"return_index": len(returns) - 1, "name": t.name,
                    "return_track_count": len(returns)}
        except Exception as e:
            self.log_message("Error creating return track: " + str(e))
            raise

    def _set_track_arm(self, track_index=0, value=True):
        """Arm or disarm a track for recording"""
        try:
            track = self._resolve_track(track_index)
            if not track.can_be_armed:
                raise ValueError("Track %d cannot be armed" % track_index)
            track.arm = bool(value)
            return {"track_index": track_index, "track_name": track.name,
                    "arm": bool(track.arm)}
        except Exception as e:
            self.log_message("Error arming track: " + str(e))
            raise

    def _set_track_monitoring(self, track_index=0, value="auto"):
        """Set input monitoring. 0 = In, 1 = Auto, 2 = Off (Live's own ordering)."""
        try:
            track = self._resolve_track(track_index)
            names = {"in": 0, "auto": 1, "off": 2}
            if isinstance(value, str):
                key = value.strip().lower()
                if key not in names:
                    raise ValueError("monitoring must be 'in', 'auto' or 'off'")
                state = names[key]
            else:
                state = int(value)
            if state not in (0, 1, 2):
                raise ValueError("monitoring state must be 0 (In), 1 (Auto) or 2 (Off)")
            track.current_monitoring_state = state
            inverse = {0: "in", 1: "auto", 2: "off"}
            return {"track_index": track_index, "track_name": track.name,
                    "monitoring": inverse[int(track.current_monitoring_state)]}
        except Exception as e:
            self.log_message("Error setting monitoring: " + str(e))
            raise

    def _load_instrument_or_effect(self, track_index=0, uri="", track_type="regular"):
        """Load an instrument or effect onto a track by its browser URI.

        The command dispatcher above calls this method, but it was never
        defined — and "load_instrument_or_effect" was missing from the list of
        main-thread commands as well, so the command fell through to the final
        "Unknown command" branch. Loading a device is exactly what
        _load_browser_item does, so delegate to it; the only difference is the
        parameter name the MCP server uses ("uri" vs "item_uri").
        """
        return self._load_browser_item(track_index, uri, track_type)

    def _load_browser_item(self, track_index=0, item_uri="", track_type="regular"):
        """Load a browser item onto a track by its URI.

        track_type accepts "regular", "return" or "master", so effects can be
        placed on the master bus and on send returns, not just regular tracks.
        """
        try:
            track = self._resolve_track(track_index, track_type)
            
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            
            # Find the browser item by URI
            item = self._find_browser_item_by_uri(app.browser, item_uri)
            
            if not item:
                raise ValueError("Browser item with URI '{0}' not found".format(item_uri))
            
            # Select the track
            self._song.view.selected_track = track
            
            # Load the item
            app.browser.load_item(item)
            
            result = {
                "loaded": True,
                "item_name": item.name,
                "track_name": track.name,
                "uri": item_uri
            }
            return result
        except Exception as e:
            self.log_message("Error loading browser item: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
    
    # Substring markers that point a URI at a likely root. Unmatched URIs fall
    # back to the default search order.
    _URI_ROOT_HINTS = (
        ('plugins',       ('vst:', 'vst3:', 'au:', 'query:plugins', 'plugin#')),
        ('max_for_live',  ('max for live', 'maxforlive', 'm4l', 'query:max')),
        ('user_library',  ('user library', 'userlibrary', 'query:user library', 'query:user-library')),
        ('packs',         ('query:packs', '/packs/')),
        ('samples',       ('query:samples', 'sample:', '/samples/')),
        ('drums',         ('query:drums', '/drums/')),
        ('instruments',   ('query:instruments', '/instruments/')),
        ('sounds',        ('query:sounds', '/sounds/')),
        ('audio_effects', ('query:audio effects', 'audioeffects', '/audio_effects/')),
        ('midi_effects',  ('query:midi effects', 'midieffects', '/midi_effects/')),
    )

    def _order_roots_by_uri(self, roots, uri):
        """Reorder ``roots`` so the URI's likely root is walked first."""
        if not isinstance(uri, (bytes, str)) or not uri:
            return roots
        lowered = uri.lower()
        for attr, markers in self._URI_ROOT_HINTS:
            if any(m in lowered for m in markers):
                head = [(a, r) for (a, r) in roots if a == attr]
                tail = [(a, r) for (a, r) in roots if a != attr]
                return head + tail
        return roots

    def _find_browser_item_by_uri(self, browser_or_item, uri, max_depth=10, current_depth=0):
        """Find a browser item by its URI.

        Top-level lookups are memoised on ``self._uri_cache`` so repeated
        loads of the same URI don't re-walk the entire browser tree.
        """
        if current_depth == 0:
            cache = getattr(self, '_uri_cache', None)
            if cache is None:
                self._uri_cache = cache = {}
            if uri in cache:
                return cache[uri]
            result = self._walk_browser_for_uri(browser_or_item, uri, max_depth, 0)
            if result is not None:
                cache[uri] = result
            return result
        return self._walk_browser_for_uri(browser_or_item, uri, max_depth, current_depth)

    def _walk_browser_for_uri(self, browser_or_item, uri, max_depth, current_depth):
        """Recursive walk used by :py:meth:`_find_browser_item_by_uri`."""
        try:
            # Check if this is the item we're looking for
            if hasattr(browser_or_item, 'uri') and browser_or_item.uri == uri:
                return browser_or_item

            # Stop recursion if we've reached max depth
            if current_depth >= max_depth:
                return None

            # Check if this is a browser with root categories
            if hasattr(browser_or_item, 'instruments'):
                roots = [
                    ('instruments', browser_or_item.instruments),
                    ('sounds', browser_or_item.sounds),
                    ('drums', browser_or_item.drums),
                    ('audio_effects', browser_or_item.audio_effects),
                    ('midi_effects', browser_or_item.midi_effects),
                ]
                for extra_attr in ('plugins', 'max_for_live', 'user_library', 'packs', 'samples'):
                    if hasattr(browser_or_item, extra_attr):
                        try:
                            roots.append((extra_attr, getattr(browser_or_item, extra_attr)))
                        except (AttributeError, RuntimeError) as e:
                            self.log_message("Could not access browser.{0}: {1}".format(extra_attr, str(e)))

                for _attr, category in self._order_roots_by_uri(roots, uri):
                    item = self._find_browser_item_by_uri(category, uri, max_depth, current_depth + 1)
                    if item:
                        return item

                return None

            # Check if this item has children
            if hasattr(browser_or_item, 'children') and browser_or_item.children:
                for child in browser_or_item.children:
                    item = self._find_browser_item_by_uri(child, uri, max_depth, current_depth + 1)
                    if item:
                        return item

            return None
        except Exception as e:
            self.log_message("Error finding browser item by URI: {0}".format(str(e)))
            return None
    
    # Helper methods

    def _find_blend_parameter(self, device):
        """Find Dry/Wet, Mix, or Amount on a device for Magnitude mapping."""
        preferred = ("Dry/Wet", "Dry Wet", "Mix", "Amount")
        by_name = {}
        for param in device.parameters:
            try:
                by_name[param.name] = param
            except Exception:
                continue
        for name in preferred:
            if name in by_name:
                return by_name[name], name
        # Case-insensitive fallback
        lowered = dict((k.lower(), (v, k)) for k, v in by_name.items())
        for name in preferred:
            hit = lowered.get(name.lower())
            if hit:
                return hit[0], hit[1]
        return None, None

    def _inspect_rack(self, track_index=0, device_index=0):
        """Inspect a rack's nested devices and blend parameters."""
        if track_index < 0 or track_index >= len(self._song.tracks):
            raise IndexError("Track index out of range")
        track = self._song.tracks[track_index]
        if device_index < 0 or device_index >= len(track.devices):
            raise IndexError("Device index out of range")
        rack = track.devices[device_index]
        if not getattr(rack, "can_have_chains", False):
            raise ValueError("Device '{0}' is not a rack".format(rack.name))

        devices_info = []
        for chain_index, chain in enumerate(rack.chains):
            for nested in chain.devices:
                blend, blend_name = self._find_blend_parameter(nested)
                param_names = []
                try:
                    param_names = [p.name for p in nested.parameters]
                except Exception:
                    pass
                devices_info.append({
                    "chain_index": chain_index,
                    "name": nested.name,
                    "class_name": nested.class_name,
                    "blend_param": blend_name,
                    "parameters": param_names,
                })

        return {
            "track_index": track_index,
            "device_index": device_index,
            "rack_name": rack.name,
            "has_macro_map": hasattr(rack, "macro_map"),
            "has_rename_macro": hasattr(rack, "rename_macro"),
            "macros_mapped": list(getattr(rack, "macros_mapped", [])),
            "devices": devices_info,
        }

    def _map_rack_magnitude(self, track_index=0, device_index=0, macro_name="Magnitude"):
        """Rename Macro 1 and map nested Dry/Wet (or Mix/Amount) params to it."""
        if track_index < 0 or track_index >= len(self._song.tracks):
            raise IndexError("Track index out of range")
        track = self._song.tracks[track_index]
        if device_index < 0 or device_index >= len(track.devices):
            raise IndexError("Device index out of range")
        rack = track.devices[device_index]
        if not getattr(rack, "can_have_chains", False):
            raise ValueError("Device '{0}' is not a rack".format(rack.name))
        if not hasattr(rack, "macro_map"):
            raise RuntimeError(
                "RackDevice.macro_map is unavailable in this Live version")

        # Ensure at least one macro is visible
        try:
            visible = int(getattr(rack, "visible_macro_count", 1) or 1)
            while visible < 1 and hasattr(rack, "add_macro"):
                rack.add_macro()
                visible = int(rack.visible_macro_count)
        except Exception as e:
            self.log_message("Could not adjust visible macros: {0}".format(e))

        if hasattr(rack, "rename_macro"):
            rack.rename_macro(0, macro_name)
        else:
            # Fallback: Macro 1 is usually parameters[1] (0 = Device On)
            try:
                if len(rack.parameters) > 1:
                    rack.parameters[1].name = macro_name
            except Exception:
                pass

        mapped = []
        skipped = []
        for chain_index, chain in enumerate(rack.chains):
            for nested in chain.devices:
                blend, blend_name = self._find_blend_parameter(nested)
                if not blend:
                    skipped.append({
                        "device": nested.name,
                        "reason": "no Dry/Wet, Mix, or Amount parameter",
                    })
                    continue
                try:
                    rack.macro_map(0, blend)
                    mapped.append({
                        "device": nested.name,
                        "parameter": blend_name,
                        "chain_index": chain_index,
                    })
                except Exception as e:
                    skipped.append({
                        "device": nested.name,
                        "parameter": blend_name,
                        "reason": str(e),
                    })

        return {
            "rack_name": rack.name,
            "macro_name": macro_name,
            "macro_index": 0,
            "mapped": mapped,
            "skipped": skipped,
            "macros_mapped": list(getattr(rack, "macros_mapped", [])),
        }
    
    def _get_device_type(self, device):
        """Get the type of a device"""
        try:
            # Simple heuristic - in a real implementation you'd look at the device class
            if device.can_have_drum_pads:
                return "drum_machine"
            elif device.can_have_chains:
                return "rack"
            elif "instrument" in device.class_display_name.lower():
                return "instrument"
            elif "audio_effect" in device.class_name.lower():
                return "audio_effect"
            elif "midi_effect" in device.class_name.lower():
                return "midi_effect"
            else:
                return "unknown"
        except:
            return "unknown"

    # ── Session snapshot helpers ──────────────────────────────────────────────

    def _safe_attr(self, obj, attr, cast=None, default=None):
        try:
            val = getattr(obj, attr)
            if callable(val):
                return default
            if cast is not None:
                return cast(val)
            return val
        except Exception:
            return default

    def _notes_from_clip(self, clip):
        """Extract MIDI notes from a clip (incl. MPE/expression when available)."""
        notes = []
        if not clip or not getattr(clip, "is_midi_clip", False):
            return notes

        if hasattr(clip, "get_notes_extended"):
            try:
                raw = clip.get_notes_extended(0, 128, 0.0, float(clip.length) + 1.0)
                for n in raw:
                    entry = {
                        "pitch": int(getattr(n, "pitch", 0)),
                        "start_time": float(getattr(n, "start_time", 0.0)),
                        "duration": float(getattr(n, "duration", 0.0)),
                        "velocity": float(getattr(n, "velocity", 0)),
                        "mute": bool(getattr(n, "mute", False)),
                    }
                    for opt, caster in [
                        ("probability", float),
                        ("velocity_deviation", float),
                        ("release_velocity", float),
                        ("note_id", int),
                    ]:
                        if hasattr(n, opt):
                            try:
                                entry[opt] = caster(getattr(n, opt))
                            except Exception:
                                pass
                    for opt in ("pitch_bend_range", "pressure", "timbre", "slide"):
                        if hasattr(n, opt):
                            try:
                                entry[opt] = float(getattr(n, opt))
                            except Exception:
                                pass
                    notes.append(entry)
                return notes
            except Exception as e:
                self.log_message("get_notes_extended failed, falling back: " + str(e))

        if hasattr(clip, "get_notes"):
            try:
                raw = clip.get_notes(0.0, 0, float(clip.length) + 1.0, 128)
                for n in raw:
                    notes.append({
                        "pitch": int(n[0]),
                        "start_time": float(n[1]),
                        "duration": float(n[2]),
                        "velocity": float(n[3]),
                        "mute": bool(n[4]) if len(n) > 4 else False,
                    })
            except Exception as e:
                self.log_message("get_notes failed: " + str(e))
        return notes

    def _warp_markers_from_clip(self, clip):
        markers = []
        try:
            raw = getattr(clip, "warp_markers", None)
            if not raw:
                return markers
            for m in raw:
                markers.append({
                    "beat_time": float(getattr(m, "beat_time", getattr(m, "time", 0.0))),
                    "sample_time": float(
                        getattr(m, "sample_time", getattr(m, "time", 0.0))
                    ),
                })
        except Exception as e:
            self.log_message("warp_markers read failed: " + str(e))
        return markers

    def _automated_params_for_device(self, device):
        automated = []
        try:
            for param in device.parameters:
                is_auto = False
                try:
                    if hasattr(param, "automation_state"):
                        is_auto = int(param.automation_state) != 0
                    elif hasattr(param, "is_automated"):
                        is_auto = bool(param.is_automated)
                except Exception:
                    continue
                if is_auto:
                    automated.append(param.name)
        except Exception:
            pass
        return automated

    # Racks nest, and a pathological project could nest deeply. Cap the walk so
    # a snapshot can never blow the stack or the payload size.
    _MAX_CHAIN_DEPTH = 4

    def _serialize_device(self, device, device_index, include_params=True, depth=0):
        info = {
            "index": device_index,
            "name": device.name,
            "class_name": device.class_name,
            "type": self._get_device_type(device),
        }
        automated = self._automated_params_for_device(device)
        if automated:
            info["automated_parameters"] = automated
            info["automation_enabled"] = True
        else:
            info["automation_enabled"] = False

        if include_params:
            params = []
            try:
                for p_index, param in enumerate(device.parameters):
                    try:
                        entry = {
                            "index": p_index,
                            "name": param.name,
                            "value": float(param.value),
                            "min": float(param.min),
                            "max": float(param.max),
                            "is_enabled": bool(getattr(param, "is_enabled", True)),
                            "is_quantized": bool(getattr(param, "is_quantized", False)),
                        }
                        if hasattr(param, "value_string"):
                            entry["value_string"] = str(param.value_string)
                        if hasattr(param, "automation_state"):
                            try:
                                entry["automation_state"] = int(param.automation_state)
                            except Exception:
                                pass
                        params.append(entry)
                    except Exception:
                        continue
            except Exception as e:
                self.log_message("Error reading device parameters: " + str(e))
            info["parameters"] = params

        # Devices inside a rack carry the actual sound design — a drum rack's
        # nested Operator, an instrument rack's filter. Without this walk a rack
        # contributes only its 8 macros and the timbral state is invisible.
        if getattr(device, "can_have_chains", False):
            if depth >= self._MAX_CHAIN_DEPTH:
                info["chains_truncated"] = True
            else:
                info["chains"] = self._serialize_chains(
                    device, include_params=include_params, depth=depth
                )
        return info

    def _serialize_chains(self, rack, include_params=True, depth=0):
        chains = []
        try:
            chain_lists = [("chains", getattr(rack, "chains", []))]
            returns = getattr(rack, "return_chains", None)
            if returns:
                chain_lists.append(("return_chains", returns))

            for kind, chain_list in chain_lists:
                for chain_index, chain in enumerate(chain_list):
                    entry = {
                        "index": chain_index,
                        "kind": kind,
                        "chain_name": self._safe_attr(chain, "name", str, ""),
                        "mute": bool(self._safe_attr(chain, "mute", bool, False)),
                        "solo": bool(self._safe_attr(chain, "solo", bool, False)),
                    }
                    try:
                        mixer = chain.mixer_device
                        entry["volume"] = float(mixer.volume.value)
                        entry["panning"] = float(mixer.panning.value)
                    except Exception:
                        pass

                    # Drum racks expose the pad's note, which is what ties a
                    # nested device back to the kick/snare/hat it voices.
                    note = self._safe_attr(chain, "out_note", int, None)
                    if note is not None:
                        entry["out_note"] = note

                    nested = []
                    try:
                        for d_i, dev in enumerate(chain.devices):
                            nested.append(
                                self._serialize_device(
                                    dev,
                                    d_i,
                                    include_params=include_params,
                                    depth=depth + 1,
                                )
                            )
                    except Exception as e:
                        self.log_message("Error reading chain devices: " + str(e))
                    entry["devices"] = nested
                    chains.append(entry)
        except Exception as e:
            self.log_message("Error serializing rack chains: " + str(e))
        return chains

    def _serialize_clip_common(self, clip):
        info = {
            "looping": bool(self._safe_attr(clip, "looping", bool, False)),
            "loop_start": self._safe_attr(clip, "loop_start", float, None),
            "loop_end": self._safe_attr(clip, "loop_end", float, None),
            "warping": bool(self._safe_attr(clip, "warping", bool, False)),
            "warp_mode": self._safe_attr(clip, "warp_mode", int, None),
            "gain": self._safe_attr(clip, "gain", float, None),
            "pitch_coarse": self._safe_attr(clip, "pitch_coarse", int, None),
            "pitch_fine": self._safe_attr(clip, "pitch_fine", int, None),
            "launch_mode": self._safe_attr(clip, "launch_mode", int, None),
        }
        for attr in ("file_path", "file_path_relative"):
            path = self._safe_attr(clip, attr, str, None)
            if path:
                info["file_path"] = path
                break
        markers = self._warp_markers_from_clip(clip)
        if markers:
            info["warp_markers"] = markers
            info["warp_marker_count"] = len(markers)
        return dict((k, v) for k, v in info.items() if v is not None)

    def _serialize_session_clip(self, clip, include_notes=True):
        info = {
            "name": clip.name,
            "length": float(clip.length),
            "is_playing": bool(clip.is_playing),
            "is_recording": bool(getattr(clip, "is_recording", False)),
            "is_midi_clip": bool(getattr(clip, "is_midi_clip", False)),
            "is_audio_clip": bool(getattr(clip, "is_audio_clip", False)),
            "color": int(getattr(clip, "color", 0)),
        }
        info.update(self._serialize_clip_common(clip))
        if include_notes and info["is_midi_clip"]:
            info["notes"] = self._notes_from_clip(clip)
            info["note_count"] = len(info["notes"])
        return info

    def _serialize_arrangement_clip(self, clip, include_notes=True):
        info = {
            "name": clip.name,
            "start_time": float(clip.start_time),
            "end_time": float(clip.end_time),
            "length": float(clip.length),
            "color": int(getattr(clip, "color", 0)),
            "is_midi_clip": bool(getattr(clip, "is_midi_clip", False)),
            "is_audio_clip": bool(getattr(clip, "is_audio_clip", False)),
            "is_playing": bool(getattr(clip, "is_playing", False)),
        }
        info.update(self._serialize_clip_common(clip))
        if include_notes and info["is_midi_clip"]:
            info["notes"] = self._notes_from_clip(clip)
            info["note_count"] = len(info["notes"])
        return info

    def _serialize_sends(self, track):
        sends = []
        try:
            for i, send in enumerate(track.mixer_device.sends):
                sends.append({
                    "index": i,
                    "value": float(send.value),
                    "name": str(getattr(send, "name", "Send %d" % i)),
                })
        except Exception:
            pass
        return sends

    def _serialize_scenes(self):
        scenes = []
        try:
            for i, scene in enumerate(self._song.scenes):
                scenes.append({
                    "index": i,
                    "name": str(scene.name),
                    "tempo": self._safe_attr(scene, "tempo", float, None),
                    "is_triggered": bool(self._safe_attr(scene, "is_triggered", bool, False)),
                })
        except Exception as e:
            self.log_message("scenes serialize failed: " + str(e))
        return scenes

    def _serialize_cue_points(self):
        cues = []
        try:
            for cue in self._song.cue_points:
                cues.append({
                    "name": str(getattr(cue, "name", "")),
                    "time": float(getattr(cue, "time", 0.0)),
                })
        except Exception as e:
            self.log_message("cue_points serialize failed: " + str(e))
        return cues

    def _serialize_return_tracks(self, include_params=True):
        returns = []
        try:
            for i, track in enumerate(self._song.return_tracks):
                devices = []
                for d_i, device in enumerate(track.devices):
                    devices.append(
                        self._serialize_device(device, d_i, include_params=include_params)
                    )
                returns.append({
                    "index": i,
                    "name": track.name,
                    "mute": bool(track.mute),
                    "solo": bool(track.solo),
                    "volume": float(track.mixer_device.volume.value),
                    "panning": float(track.mixer_device.panning.value),
                    "devices": devices,
                })
        except Exception as e:
            self.log_message("return_tracks serialize failed: " + str(e))
        return returns

    def _serialize_master_track(self, include_params=True):
        """Master chain — the bus compressor/limiter that shapes the final sound."""
        try:
            track = self._song.master_track
            devices = []
            for d_i, device in enumerate(track.devices):
                devices.append(
                    self._serialize_device(device, d_i, include_params=include_params)
                )
            return {
                "volume": float(track.mixer_device.volume.value),
                "panning": float(track.mixer_device.panning.value),
                "devices": devices,
            }
        except Exception as e:
            self.log_message("master_track serialize failed: " + str(e))
            return None

    def _get_clip_notes(self, track_index=0, clip_index=0):
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            track = self._song.tracks[track_index]
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            slot = track.clip_slots[clip_index]
            if not slot.has_clip:
                raise Exception("No clip in slot")
            clip = slot.clip
            if not getattr(clip, "is_midi_clip", False):
                raise Exception("Clip is not a MIDI clip")
            notes = self._notes_from_clip(clip)
            return {
                "track_index": track_index,
                "clip_index": clip_index,
                "clip_name": clip.name,
                "length": float(clip.length),
                "note_count": len(notes),
                "notes": notes,
            }
        except Exception as e:
            self.log_message("Error getting clip notes: " + str(e))
            raise

    def _get_session_snapshot(self, include_notes=True, include_params=True):
        """Full v2 project state dump, returned to the caller."""
        try:
            session = self._get_session_info()
            tracks = []
            for track_index, track in enumerate(self._song.tracks):
                clip_slots = []
                for slot_index, slot in enumerate(track.clip_slots):
                    clip_info = None
                    if slot.has_clip:
                        clip_info = self._serialize_session_clip(
                            slot.clip, include_notes=include_notes
                        )
                    clip_slots.append({
                        "index": slot_index,
                        "has_clip": bool(slot.has_clip),
                        "clip": clip_info,
                    })

                devices = []
                for device_index, device in enumerate(track.devices):
                    devices.append(
                        self._serialize_device(
                            device, device_index, include_params=include_params
                        )
                    )

                arrangement_clips = []
                try:
                    for clip in track.arrangement_clips:
                        arrangement_clips.append(
                            self._serialize_arrangement_clip(
                                clip, include_notes=include_notes
                            )
                        )
                except Exception as e:
                    self.log_message(
                        "arrangement_clips unavailable on track %d: %s"
                        % (track_index, str(e))
                    )

                tracks.append({
                    "index": track_index,
                    "name": track.name,
                    "is_audio_track": bool(track.has_audio_input),
                    "is_midi_track": bool(track.has_midi_input),
                    "mute": bool(track.mute),
                    "solo": bool(track.solo),
                    "arm": bool(getattr(track, "arm", False)),
                    "volume": float(track.mixer_device.volume.value),
                    "panning": float(track.mixer_device.panning.value),
                    "sends": self._serialize_sends(track),
                    "clip_slots": clip_slots,
                    "devices": devices,
                    "arrangement_clips": arrangement_clips,
                })

            return {
                "schema": "ableton_mcp_snapshot_v2",
                "session": session,
                "tracks": tracks,
                "scenes": self._serialize_scenes(),
                "return_tracks": self._serialize_return_tracks(
                    include_params=include_params
                ),
                "master_track": self._serialize_master_track(
                    include_params=include_params
                ),
                "cue_points": self._serialize_cue_points(),
                "include_notes": bool(include_notes),
                "include_params": bool(include_params),
            }
        except Exception as e:
            self.log_message("Error getting session snapshot: " + str(e))
            raise

    def get_browser_tree(self, category_type="all"):
        """
        Get a simplified tree of browser categories.
        
        Args:
            category_type: Type of categories to get ('all', 'instruments', 'sounds', etc.)
            
        Returns:
            Dictionary with the browser tree structure
        """
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            # Check if browser is available
            if not hasattr(app, 'browser') or app.browser is None:
                raise RuntimeError("Browser is not available in the Live application")
            
            # Log available browser attributes to help diagnose issues
            browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith('_')]
            self.log_message("Available browser attributes: {0}".format(browser_attrs))
            
            result = {
                "type": category_type,
                "categories": [],
                "available_categories": browser_attrs
            }
            
            # Helper function to process a browser item and its children
            def process_item(item, depth=0):
                if not item:
                    return None
                
                result = {
                    "name": item.name if hasattr(item, 'name') else "Unknown",
                    "is_folder": hasattr(item, 'children') and bool(item.children),
                    "is_device": hasattr(item, 'is_device') and item.is_device,
                    "is_loadable": hasattr(item, 'is_loadable') and item.is_loadable,
                    "uri": item.uri if hasattr(item, 'uri') else None,
                    "children": []
                }
                
                
                return result
            
            # Process based on category type and available attributes
            if (category_type == "all" or category_type == "instruments") and hasattr(app.browser, 'instruments'):
                try:
                    instruments = process_item(app.browser.instruments)
                    if instruments:
                        instruments["name"] = "Instruments"  # Ensure consistent naming
                        result["categories"].append(instruments)
                except Exception as e:
                    self.log_message("Error processing instruments: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "sounds") and hasattr(app.browser, 'sounds'):
                try:
                    sounds = process_item(app.browser.sounds)
                    if sounds:
                        sounds["name"] = "Sounds"  # Ensure consistent naming
                        result["categories"].append(sounds)
                except Exception as e:
                    self.log_message("Error processing sounds: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "drums") and hasattr(app.browser, 'drums'):
                try:
                    drums = process_item(app.browser.drums)
                    if drums:
                        drums["name"] = "Drums"  # Ensure consistent naming
                        result["categories"].append(drums)
                except Exception as e:
                    self.log_message("Error processing drums: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "audio_effects") and hasattr(app.browser, 'audio_effects'):
                try:
                    audio_effects = process_item(app.browser.audio_effects)
                    if audio_effects:
                        audio_effects["name"] = "Audio Effects"  # Ensure consistent naming
                        result["categories"].append(audio_effects)
                except Exception as e:
                    self.log_message("Error processing audio_effects: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "midi_effects") and hasattr(app.browser, 'midi_effects'):
                try:
                    midi_effects = process_item(app.browser.midi_effects)
                    if midi_effects:
                        midi_effects["name"] = "MIDI Effects"
                        result["categories"].append(midi_effects)
                except Exception as e:
                    self.log_message("Error processing midi_effects: {0}".format(str(e)))
            
            # Try to process other potentially available categories
            for attr in browser_attrs:
                if attr not in ['instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects'] and \
                   (category_type == "all" or category_type == attr):
                    try:
                        item = getattr(app.browser, attr)
                        if hasattr(item, 'children') or hasattr(item, 'name'):
                            category = process_item(item)
                            if category:
                                category["name"] = attr.capitalize()
                                result["categories"].append(category)
                    except Exception as e:
                        self.log_message("Error processing {0}: {1}".format(attr, str(e)))
            
            self.log_message("Browser tree generated for {0} with {1} root categories".format(
                category_type, len(result['categories'])))
            return result
            
        except Exception as e:
            self.log_message("Error getting browser tree: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
    
    def get_browser_items_at_path(self, path=""):
        """
        Get browser items at a specific path.
        
        Args:
            path: Path in the format "category/folder/subfolder"
                 where category is one of: instruments, sounds, drums, audio_effects, midi_effects
                 or any other available browser category
                 
        Returns:
            Dictionary with items at the specified path
        """
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            # Check if browser is available
            if not hasattr(app, 'browser') or app.browser is None:
                raise RuntimeError("Browser is not available in the Live application")
            
            # Log available browser attributes to help diagnose issues
            browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith('_')]
            self.log_message("Available browser attributes: {0}".format(browser_attrs))
                
            # Parse the path
            path_parts = path.split("/")
            if not path_parts:
                raise ValueError("Invalid path")
            
            # Determine the root category
            root_category = path_parts[0].lower()
            current_item = None
            
            # Check standard categories first
            if root_category == "instruments" and hasattr(app.browser, 'instruments'):
                current_item = app.browser.instruments
            elif root_category == "sounds" and hasattr(app.browser, 'sounds'):
                current_item = app.browser.sounds
            elif root_category == "drums" and hasattr(app.browser, 'drums'):
                current_item = app.browser.drums
            elif root_category == "audio_effects" and hasattr(app.browser, 'audio_effects'):
                current_item = app.browser.audio_effects
            elif root_category == "midi_effects" and hasattr(app.browser, 'midi_effects'):
                current_item = app.browser.midi_effects
            else:
                # Try to find the category in other browser attributes
                found = False
                for attr in browser_attrs:
                    if attr.lower() == root_category:
                        try:
                            current_item = getattr(app.browser, attr)
                            found = True
                            break
                        except Exception as e:
                            self.log_message("Error accessing browser attribute {0}: {1}".format(attr, str(e)))
                
                if not found:
                    # If we still haven't found the category, return available categories
                    return {
                        "path": path,
                        "error": "Unknown or unavailable category: {0}".format(root_category),
                        "available_categories": browser_attrs,
                        "items": []
                    }
            
            # Navigate through the path
            for i in range(1, len(path_parts)):
                part = path_parts[i]
                if not part:  # Skip empty parts
                    continue
                
                if not hasattr(current_item, 'children'):
                    return {
                        "path": path,
                        "error": "Item at '{0}' has no children".format('/'.join(path_parts[:i])),
                        "items": []
                    }
                
                found = False
                for child in current_item.children:
                    if hasattr(child, 'name') and child.name.lower() == part.lower():
                        current_item = child
                        found = True
                        break
                
                if not found:
                    return {
                        "path": path,
                        "error": "Path part '{0}' not found".format(part),
                        "items": []
                    }
            
            # Get items at the current path
            items = []
            if hasattr(current_item, 'children'):
                for child in current_item.children:
                    item_info = {
                        "name": child.name if hasattr(child, 'name') else "Unknown",
                        "is_folder": hasattr(child, 'children') and bool(child.children),
                        "is_device": hasattr(child, 'is_device') and child.is_device,
                        "is_loadable": hasattr(child, 'is_loadable') and child.is_loadable,
                        "uri": child.uri if hasattr(child, 'uri') else None
                    }
                    items.append(item_info)
            
            result = {
                "path": path,
                "name": current_item.name if hasattr(current_item, 'name') else "Unknown",
                "uri": current_item.uri if hasattr(current_item, 'uri') else None,
                "is_folder": hasattr(current_item, 'children') and bool(current_item.children),
                "is_device": hasattr(current_item, 'is_device') and current_item.is_device,
                "is_loadable": hasattr(current_item, 'is_loadable') and current_item.is_loadable,
                "items": items
            }
            
            self.log_message("Retrieved {0} items at path: {1}".format(len(items), path))
            return result
            
        except Exception as e:
            self.log_message("Error getting browser items at path: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
