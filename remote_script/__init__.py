# AbletonMCP/__init__.py
from __future__ import absolute_import, print_function, unicode_literals

from _Framework.ControlSurface import ControlSurface
import os
import socket
import json
import struct
import tempfile
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
SCRIPT_VERSION = "1.15.0"
PROTOCOL_VERSION = 1

# The trim eraser (see _trim_arrangement_clip): a temporary Session clip
# stamped over an Arrangement region so Live's own overlap handling crops
# what it covers. Audio tracks need a real audio file behind such a clip;
# the script generates this much 16-bit mono silence on demand. Short on
# purpose — the stamp's footprint only ever has to fit INSIDE the region
# being removed (or overhang into timeline verified empty first), never to
# match its length.
TRIM_ERASER_NAME = "MCP trim eraser"
TRIM_ERASER_WAV_SECONDS = 0.05

# Ceiling on how many placements one batched stamp may make (see
# _duplicate_session_clip_to_arrangement's destination_times). The whole run
# happens inside a single main-thread task, and Live's main thread is where
# the UI and the audio engine's housekeeping live: a loop long enough to be
# convenient is also long enough to stall both. The longest real run measured
# in a production session was 76 placements, so this leaves headroom without
# letting a mistyped range block Live for minutes.
MAX_STAMP_PLACEMENTS = 128

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
#   None means the default (10.0). Two rows need more: create_audio_clip
#   decodes/imports the file on the main thread, and the session stamp can be
#   handed a whole run of destination_times to place in one task. Every
#   override here must be matched by a longer socket timeout on the server
#   side (>= queue_timeout + 5 s), which test_cross_half_contract enforces.
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
    "duplicate_track":            ("_duplicate_track",            True,  None, True),
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
    "set_device_parameters":      ("_set_device_parameters",      True,  None, True),
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
    "set_clip_warp":              ("_set_clip_warp",              True,  None, True),
    "start_playback":             ("_start_playback",             True,  None, False),
    "stop_playback":              ("_stop_playback",              True,  None, False),
    "load_browser_item":          ("_load_browser_item",          True,  None, True),
    "load_instrument_or_effect":  ("_load_instrument_or_effect",  True,  None, True),
    "switch_to_arrangement_view": ("_switch_to_arrangement_view", True,  None, True),
    "set_current_song_time":      ("_set_current_song_time",      True,  None, True),
    # 30 s, not the 10 s default: destination_times stamps a whole run of
    # placements inside ONE main-thread task, and a client that gives up
    # mid-run leaves Live still stamping with nobody reading the result.
    # The server's socket timeout for this row must stay >= 35 s.
    "duplicate_session_clip_to_arrangement":
        ("_duplicate_session_clip_to_arrangement",                True,  30.0, True),
    "map_rack_magnitude":         ("_map_rack_magnitude",         True,  None, False),
    "inspect_rack":               ("_inspect_rack",               True,  None, False),
    "create_locator":             ("_create_locator",             True,  None, True),
    "jump_to_locator":            ("_jump_to_locator",            True,  None, True),
    "delete_locator":             ("_delete_locator",             True,  None, True),
    "trim_arrangement_clip":      ("_trim_arrangement_clip",      True,  None, True),
    "delete_arrangement_clip":    ("_delete_arrangement_clip",    True,  None, True),
    "move_arrangement_clip":      ("_move_arrangement_clip",      True,  None, True),
    "duplicate_arrangement_clip": ("_duplicate_arrangement_clip", True,  None, True),
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
            # default budget. The overrides are the two commands that do
            # bulk work on the main thread (create_audio_clip's import, the
            # session stamp's placement run).
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
            # v3 scopes get_session_snapshot: warp markers, rack chains and
            # empty clip slots moved behind default-off flags and a `tracks`
            # filter arrived, so a v3 payload is a strict subset of v2 unless
            # the caller asks for more. Bumped deliberately — a client that
            # diffs snapshots across script versions must see the break.
            "snapshot_schema": "ableton_mcp_snapshot_v3",
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

    def _set_track_name(self, track_index=0, name="", expect_track_name=None):
        """Set the name of a track"""
        try:
            # expect_track_name is checked against the name the track has
            # NOW, which is the point: after a rename the result echoes the
            # name the caller asked for either way, so previous_name below is
            # the only field that can ever expose a rename aimed one track
            # off.
            track = self._resolve_track(track_index,
                                        expect_track_name=expect_track_name)
            previous_name = track.name

            # Set the name
            track.name = name

            result = {
                "track_index": track_index,
                "previous_name": previous_name,
                "name": track.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting track name: " + str(e))
            raise
    
    def _create_clip(self, track_index=0, clip_index=0, length=4.0,
                     expect_track_name=None):
        """Create a new MIDI clip in the specified track and clip slot"""
        try:
            track = self._resolve_track(track_index,
                                        expect_track_name=expect_track_name)

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            # Check if the clip slot already has a clip
            if clip_slot.has_clip:
                raise Exception("Clip slot already has a clip")

            # Create the clip
            clip_slot.create_clip(length)

            result = {
                "track_index": track_index,
                "track_name": track.name,
                "clip_index": clip_index,
                "name": clip_slot.clip.name,
                "length": clip_slot.clip.length
            }
            return result
        except Exception as e:
            self.log_message("Error creating clip: " + str(e))
            raise

    def _resolve_track(self, track_index, track_type="regular",
                       expect_track_name=None):
        """Resolve a regular track, a return track, or the master track.

        track_type is "regular" (default), "return", or "master"; master ignores
        track_index. song.tracks contains neither the returns nor the master, so
        this is what lets the mixer and device commands reach them at all.

        expect_track_name is the caller's guard against a stale index. Track
        indices renumber whenever a track is created or deleted, and the
        loud half of that hazard — an index past the end — raises on its
        own. The silent half is an index that is still IN range but now
        points somewhere else, which is how an Auto Pan meant for the
        GUITARS bus once landed on BASS with a cheerful success message.
        Naming the track the caller believes it is addressing turns that
        into a refusal before the write, reporting what is actually there.
        Compared case-insensitively after stripping: Live's own names pick
        up trailing spaces more often than anyone expects, and a rename is
        not what the guard is for.
        """
        kind = (track_type or "regular").strip().lower()
        if kind in ("master", "main"):
            track = self._song.master_track
        elif kind in ("return", "send"):
            returns = self._song.return_tracks
            if track_index < 0 or track_index >= len(returns):
                raise IndexError("Return track index out of range (%d return tracks)"
                                 % len(returns))
            track = returns[track_index]
        else:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            track = self._song.tracks[track_index]

        if expect_track_name is not None:
            actual = str(track.name)
            if actual.strip().lower() != str(expect_track_name).strip().lower():
                raise ValueError(
                    "Track %s is '%s', not the expected '%s'; nothing was "
                    "changed. Track indices renumber when tracks are created "
                    "or deleted — re-read get_session_info or "
                    "get_session_snapshot and retry with the right index."
                    % (track_index, actual, expect_track_name))
        return track

    def _resolve_device(self, track_index, device_index, track_type="regular",
                        expect_track_name=None):
        track = self._resolve_track(track_index, track_type,
                                    expect_track_name=expect_track_name)
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
                # The scale is mixed WITHIN a single device — Limiter's Input
                # Gain is normalised 0..1 while its Lookahead is a raw
                # quantized index — so a bare min/max pair says nothing about
                # what the numbers mean. Rendering both ends through
                # str_for_value is what makes the scale legible BEFORE a
                # write instead of discoverable after one ("Input Gain 0.625"
                # is +6.0 dB, and there is no way to know that from 0.0/1.0).
                # Not every parameter implements str_for_value for arbitrary
                # values, hence the per-end guard.
                for key, bound in (("display_min", p.min),
                                   ("display_max", p.max)):
                    try:
                        entry[key] = str(p.str_for_value(bound))
                    except Exception:
                        entry[key] = ""
                # Quantized parameters are pickers, not ranges: the index is
                # meaningless without the labels it indexes. value_items is
                # absent on older Live builds and on non-quantized params, so
                # it is emitted only when it is really there.
                if entry["is_quantized"]:
                    try:
                        items = getattr(p, "value_items", None)
                        if items:
                            entry["value_items"] = [str(v) for v in items]
                    except Exception:
                        pass
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

    def _resolve_device_parameter(self, device, parameter):
        """One DeviceParameter, addressed by integer index or by name.

        Shared by the singular and plural setters so the two can never
        disagree about what "parameter" means — a device dialled through
        set_device_parameters must land on exactly the parameter
        set_device_parameter would have found.
        """
        if isinstance(parameter, bool):
            raise ValueError("parameter must be an index or a name, not a bool")
        elif isinstance(parameter, int):
            if parameter < 0 or parameter >= len(device.parameters):
                raise IndexError("Parameter index out of range (device has %d)"
                                 % len(device.parameters))
            return device.parameters[parameter]
        # No str() coercion when the wire already handed us text: on Live
        # 10.1's Python 2 str() of a non-ASCII unicode name raises before
        # the comparison can run (the trap _jump_to_locator documents).
        wanted = parameter if hasattr(parameter, "strip") else str(parameter)
        wanted = wanted.strip().lower()
        for p in device.parameters:
            if p.name.strip().lower() == wanted:
                return p
        names = ", ".join([p.name for p in device.parameters])
        raise ValueError("No parameter named '%s'. Available: %s"
                         % (parameter, names))

    def _write_device_parameter(self, target, value):
        """Clamp value into a parameter's range, write it, describe the write.

        The clamp and the read-back live here, not in the callers, for the
        same reason the resolver does: two copies of Live's range rules
        would drift.
        """
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
            "name": target.name,
            "requested": value,
            "old_value": old_value,
            "value": target.value,
            "display_value": shown,
            "clamped": clamped != value,
            "min": target.min,
            "max": target.max,
        }

    def _set_device_parameter(self, track_index=0, device_index=0, parameter=None,
                              value=0.0, track_type="regular",
                              expect_track_name=None):
        """Set one device parameter, addressed by integer index or by name"""
        try:
            track, device = self._resolve_device(
                track_index, device_index, track_type,
                expect_track_name=expect_track_name)

            target = self._resolve_device_parameter(device, parameter)
            written = self._write_device_parameter(target, value)

            return {
                "track_index": track_index,
                "track_name": track.name,
                "device_index": device_index,
                "device_name": device.name,
                "parameter_name": written["name"],
                "requested": written["requested"],
                "old_value": written["old_value"],
                "value": written["value"],
                "display_value": written["display_value"],
                "clamped": written["clamped"],
                "min": written["min"],
                "max": written["max"],
            }
        except Exception as e:
            self.log_message("Error setting device parameter: " + str(e))
            raise

    def _parameter_key_as_index(self, key):
        """A dict key that is really an ordinal, as an int — or None.

        JSON object keys are always strings, so a caller addressing the
        third parameter in a `parameters` dict sends "3", not 3. Only the
        plural setter needs this: the singular's `parameter` is a JSON
        value and keeps its type.

        Never raises — it runs inside the plural setter's fallback path,
        where anything thrown would cost the caller the whole batch.
        """
        try:
            if isinstance(key, bool):
                return None
            if isinstance(key, int):
                return key
            text = key.strip() if hasattr(key, "strip") else key
            if not text or not text.isdigit():
                return None
            return int(text)
        except Exception:
            return None

    def _set_device_parameters(self, track_index=0, device_index=0,
                               parameters=None, track_type="regular",
                               expect_track_name=None):
        """Set MANY parameters on ONE device in a single round-trip.

        `parameters` is a dict of {name_or_index: value}, each entry
        resolved and clamped exactly as set_device_parameter does — this
        calls the same resolver and the same writer.

        What this buys and what it does NOT: the win is round-trips, not
        freshness. Every write happens inside ONE main-thread task, and the
        read-back at the end of that task is still the SAME tick as the
        writes — and a read-back can stay stale for the rest of a tick even
        though the write landed (verified on Live 12.4.3; see
        _set_current_song_time, which is where this fork first measured it).
        So `value` and `display_value` here are exactly as trustworthy as
        the singular setter's, no more: one call's worth of staleness
        instead of one per parameter. A caller that needs a settled read
        must re-read with get_device_parameters, which is a later tick.

        One failure never erases the successes: an unresolvable key is
        reported as found=false in its own row while every other write
        still lands. Nothing here can half-write a single parameter — the
        clamp happens before the assignment.

        Order: JSON objects have no guaranteed order on Live's Python, so
        the writes happen in whatever order the dict yields and the rows
        come back in that same order. A sequence that genuinely depends on
        order (set a mode, then a value that means something different in
        that mode) still needs separate calls.
        """
        try:
            track, device = self._resolve_device(
                track_index, device_index, track_type,
                expect_track_name=expect_track_name)

            if not parameters:
                raise ValueError(
                    "parameters must be a non-empty {name_or_index: value} "
                    "map; nothing was changed")
            if not hasattr(parameters, "items"):
                raise ValueError(
                    "parameters must be a {name_or_index: value} map, not %s"
                    % type(parameters).__name__)

            # Resolve everything BEFORE writing anything, so a typo is known
            # up front and the writes themselves run as one uninterrupted
            # block — the main thread is Live's audio thread's neighbour and
            # is not a good place to be doing name lookups between writes.
            plan = []
            for key, value in parameters.items():
                target = None
                error = None
                try:
                    target = self._resolve_device_parameter(device, key)
                except Exception as name_error:
                    # Names are tried first — a parameter genuinely named
                    # "3" must stay reachable — and only a key that answered
                    # to no name is retried as an ordinal.
                    index = self._parameter_key_as_index(key)
                    if index is None:
                        error = str(name_error)
                    else:
                        try:
                            target = self._resolve_device_parameter(device, index)
                        except Exception as index_error:
                            error = str(index_error)
                plan.append((key, value, target, error))

            rows = []
            applied = 0
            for key, value, target, error in plan:
                if target is None:
                    # The key as the caller wrote it — there is no resolved
                    # parameter name to report, and echoing the key is what
                    # lets them find the typo.
                    rows.append({
                        "name": key,
                        "requested": value,
                        "value": None,
                        "display_value": "",
                        "clamped": False,
                        "found": False,
                        "error": error,
                    })
                    continue
                try:
                    row = self._write_device_parameter(target, value)
                except Exception as write_error:
                    # A single parameter Live refuses (a value that is not a
                    # number, a parameter that is not automatable) must not
                    # cost the caller the rest of the batch.
                    rows.append({
                        "name": target.name,
                        "requested": value,
                        # Whatever the parameter reads as now — read
                        # defensively, because this row exists precisely
                        # because something about this parameter misbehaved.
                        "value": self._safe_attr(target, "value", float, None),
                        "display_value": "",
                        "clamped": False,
                        "found": True,
                        "error": str(write_error),
                    })
                    continue
                row["found"] = True
                rows.append(row)
                applied += 1

            return {
                "track_index": track_index,
                "track_name": track.name,
                "device_index": device_index,
                "device_name": device.name,
                "requested_count": len(rows),
                "applied_count": applied,
                "not_found": [r["name"] for r in rows if not r["found"]],
                "parameters": rows,
            }
        except Exception as e:
            self.log_message("Error setting device parameters: " + str(e))
            raise

    def _delete_device(self, track_index=0, device_index=0, track_type="regular",
                       expect_track_name=None):
        """Remove a device from a track's chain, and verify it actually went.

        This used to report len(track.devices) with no before-count, so a
        delete that did nothing still printed a plausible "deleted X; N
        devices remain" — which happened for real: a device had to be
        deleted twice because the first call lied. The fix is a before/after
        comparison of both the count and the device NAMES, so a sweep that
        walks a chain highest-index-first can re-anchor on names instead of
        on ordinals that renumber under it.

        Deliberately NO retry. If the readback was merely stale rather than
        the delete having failed, a blind second delete removes the NEXT
        device and destroys a chain that was dialled in by hand. Reporting
        deleted=False and letting the caller re-read is the honest move; an
        automatic retry can only be safe once verification happens on a
        later tick.
        """
        try:
            track, device = self._resolve_device(
                track_index, device_index, track_type,
                expect_track_name=expect_track_name)
            name = device.name
            before = [d.name for d in track.devices]
            # What the chain should look like if exactly the addressed device
            # went. Names repeat (two Auto Pans on one bus is normal), so
            # this is a corroborating check, not a proof of identity.
            expected_after = before[:device_index] + before[device_index + 1:]

            track.delete_device(device_index)

            after = [d.name for d in track.devices]
            return {
                "track_index": track_index,
                "track_name": track.name,
                "deleted": len(after) == len(before) - 1,
                "deleted_device_index": device_index,
                "deleted_device_name": name,
                "device_count_before": len(before),
                "remaining_matches_expected": after == expected_after,
                "remaining_devices": [{"index": i, "name": n}
                                      for i, n in enumerate(after)],
                "remaining_device_count": len(after),
            }
        except Exception as e:
            self.log_message("Error deleting device: " + str(e))
            raise

    def _set_track_mixer(self, track_index, field, value, track_type="regular",
                         expect_track_name=None):
        """Set mixer volume (0.0-1.0, 0.85 = 0 dB) or panning (-1.0 to 1.0).

        Works on regular tracks, return tracks and the master.
        """
        try:
            track = self._resolve_track(track_index, track_type,
                                        expect_track_name=expect_track_name)
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

    def _set_track_volume(self, track_index=0, value=0.85, track_type="regular",
                          expect_track_name=None):
        """Set a track's volume fader (0.0-1.0, 0.85 = 0 dB)."""
        return self._set_track_mixer(track_index, "volume", value, track_type,
                                     expect_track_name=expect_track_name)

    def _set_track_pan(self, track_index=0, value=0.0, track_type="regular",
                       expect_track_name=None):
        """Set a track's pan (-1.0 hard left to 1.0 hard right)."""
        return self._set_track_mixer(track_index, "panning", value, track_type,
                                     expect_track_name=expect_track_name)

    def _set_clip_gain(self, track_index=0, clip_index=0, gain=0.5, arrangement=True,
                       expect_track_name=None):
        """Set one audio clip's gain, without touching the track fader.

        This is what fixes a single section sung too loud: it changes that clip
        alone, where lowering the track would bury every other section and
        compressing harder squashes the whole performance.

        gain is Live's normalized 0.0-1.0, where 0.4 is unity (0.0 dB).
        """
        try:
            track = self._resolve_track(track_index,
                                        expect_track_name=expect_track_name)

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

    def _set_clip_warp(self, track_index=0, clip_index=0, warping=False,
                       warp_mode=None, arrangement=True,
                       expect_track_name=None):
        """Turn an audio clip's warping on or off, and optionally set its mode.

        Live's "Auto-Warp Long Samples" guesses a source tempo per imported
        file. On material without clear transients it guesses wrong, and stems
        captured in one session can each land on a *different* guess — which
        silently pulls an aligned multitrack apart. Switching warping off plays
        the file at its recorded rate, which is what keeps stems together.

        warp_mode is Live's own index (0 Beats, 1 Tones, 2 Texture,
        3 Re-Pitch, 4 Complex, 5+ Complex Pro/REX depending on build) and is
        only applied while warping is on; it is ignored otherwise.
        """
        try:
            if arrangement:
                track, clip = self._resolve_arrangement_clip(
                    track_index, clip_index,
                    expect_track_name=expect_track_name)
            else:
                track = self._resolve_track(
                    track_index, expect_track_name=expect_track_name)
                if clip_index < 0 or clip_index >= len(track.clip_slots):
                    raise IndexError("Clip slot index out of range")
                slot = track.clip_slots[clip_index]
                if not slot.has_clip:
                    raise Exception("No clip in that slot")
                clip = slot.clip

            if clip.is_midi_clip:
                raise ValueError(
                    "Warping applies to audio clips only; this is a MIDI clip")

            want = bool(warping)
            clip.warping = want

            applied_mode = None
            if want and warp_mode is not None:
                try:
                    clip.warp_mode = int(warp_mode)
                    applied_mode = int(clip.warp_mode)
                except Exception as e:
                    # An unsupported mode index should not lose the warp toggle
                    # the caller actually asked for.
                    self.log_message("Could not set warp mode: " + str(e))

            if applied_mode is None:
                try:
                    applied_mode = int(clip.warp_mode)
                except Exception:
                    applied_mode = -1

            # Turning warping off restores the file's native length, so report
            # the resulting span: that readback is how a caller confirms a set
            # of stems now agree with each other.
            return {
                "track_index": track_index,
                "track_name": track.name,
                "clip_index": clip_index,
                "clip_name": clip.name,
                "arrangement": bool(arrangement),
                "warping": bool(clip.warping),
                "warp_mode": applied_mode,
                "start_time": float(getattr(clip, "start_time", 0.0)),
                "end_time": float(getattr(clip, "end_time", 0.0)),
                "length": float(getattr(clip, "length", 0.0)),
            }
        except Exception as e:
            self.log_message("Error setting clip warp: " + str(e))
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
                           target="Main", expect_track_name=None):
        """Set one routing field by its display name, e.g. output type 'Main'"""
        try:
            track = self._resolve_track(track_index,
                                        expect_track_name=expect_track_name)
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

        NOTE: verified against Live 12.3.2 and 12.4.3 — `Song.count_in_duration`
        is exposed but READ-ONLY ("property of 'Song' object has no setter").
        When that happens the metronome half (which IS writable) is still
        applied, and the result reports count_in_writable=False with the
        unchanged current value, so a caller gets the honest partial outcome
        instead of an all-or-nothing failure.
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

            count_in_writable = True
            try:
                self._song.count_in_duration = value
            except Exception as e:
                count_in_writable = False
                self.log_message("count_in_duration is read-only on this "
                                 "Live build: " + str(e))

            # A count-in you cannot hear is useless, so allow turning the
            # metronome on in the same call — even when the count-in itself
            # could not be written.
            if metronome is not None:
                self._song.metronome = bool(metronome)

            return {
                "count_in_writable": count_in_writable,
                "requested": mapping[value],
                "count_in_duration": int(self._song.count_in_duration),
                "count_in": mapping.get(int(self._song.count_in_duration), "?"),
                "metronome": bool(self._song.metronome),
            }
        except Exception as e:
            self.log_message("Error setting count-in: " + str(e))
            raise

    def _set_track_send(self, track_index=0, send_index=0, value=0.0,
                        expect_track_name=None):
        """Set how much of a track is sent to a return track (0.0-1.0).

        A newly created return track receives nothing until this is raised —
        Live starts every send at -inf — so a shared reverb bus is silent
        without it.
        """
        try:
            track = self._resolve_track(track_index,
                                        expect_track_name=expect_track_name)
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

    def _set_track_mute(self, track_index=0, value=False, expect_track_name=None):
        """Mute or unmute a track"""
        try:
            track = self._resolve_track(track_index,
                                        expect_track_name=expect_track_name)
            track.mute = bool(value)
            return {
                "track_index": track_index,
                "track_name": track.name,
                "mute": bool(track.mute),
            }
        except Exception as e:
            self.log_message("Error setting track mute: " + str(e))
            raise

    def _delete_track(self, track_index=0, expect_track_name=None):
        """Delete a track from the song"""
        try:
            # Resolved through the shared resolver so expect_track_name can
            # refuse first: of every mis-indexed write in this script, this
            # is the one with no cheap way back.
            track = self._resolve_track(track_index,
                                        expect_track_name=expect_track_name)
            deleted_name = track.name

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

    def _create_audio_clip(self, track_index=0, clip_index=0, path="",
                           expected_beats=None, expect_track_name=None):
        """Create an audio clip in the specified audio track clip slot by importing a file.

        Requires Ableton Live 12.0.5 or newer (the underlying
        ClipSlot.create_audio_clip Live API was introduced in 12.0.5 — it is
        not available in earlier 12.0.x releases).

        Warping is turned OFF in the same main-thread task as the import.
        Live's "Auto-Warp Long Samples" guesses a source tempo per file, and
        it guesses per FILE, not per session: six byte-identical-length stems
        from one render each landed on a different guess (117.9, four at
        ~240 = double speed, 151), which pulls an aligned multitrack apart
        while every import reports a plausible-looking beat length. Unwarped
        playback is the file at its recorded rate, which is what keeps stems
        together; call set_clip_warp afterwards for material that really
        should follow the tempo.

        expected_beats, when given, is the length the caller believes the
        file is. It is compared against the clip Live actually produced and
        reported as length_matches_expected — the check that would have
        caught the six stems at import instead of by ear, hours later.
        """
        try:
            if not path:
                raise ValueError("Audio file path is required")

            if not os.path.isabs(path):
                raise ValueError("Audio file path must be absolute (got: %s)" % path)

            track = self._resolve_track(track_index,
                                        expect_track_name=expect_track_name)

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

            clip = clip_slot.clip
            # Same task, same tick: any later call is a separate round trip,
            # by which point the import has already been reported as fine and
            # a wrong tempo guess has already been believed.
            warp_error = None
            try:
                clip.warping = False
            except Exception as e:
                # A build (or a file type) that refuses the write must not
                # lose the import — say so instead of raising over a clip
                # that now exists.
                warp_error = str(e)
                self.log_message("Could not disable warping on import: " + warp_error)

            length = float(clip.length)
            result = {
                "track_index": track_index,
                "track_name": track.name,
                "clip_index": clip_index,
                "name": clip.name,
                "length": length,
                "is_audio_clip": clip.is_audio_clip,
                # Read back rather than assumed: this is the field that says
                # whether the stems will line up.
                "warping": bool(getattr(clip, "warping", False)),
            }
            if warp_error is not None:
                result["warp_error"] = warp_error
            if expected_beats is not None:
                expected = float(expected_beats)
                result["expected_beats"] = expected
                # A 1/256-note tolerance: an honest import lands exactly, and
                # a wrong tempo guess is off by whole beats, never by a
                # rounding hair.
                result["length_matches_expected"] = abs(length - expected) <= 1e-3
            return result
        except Exception as e:
            self.log_message("Error creating audio clip: " + str(e))
            raise

    def _count_clip_notes(self, clip, time_span=None):
        """How many notes a clip holds over a window, or None if unreadable.

        Prefers the modern reader and falls back to the legacy signature the
        same way _clear_notes_from_clip does — note the deliberately swapped
        argument orders:
          get_notes_extended(from_pitch, pitch_span, from_time, time_span)
          get_notes(from_time, from_pitch, time_span, pitch_span)

        time_span defaults to the clip's own length; callers that may be
        writing past the clip's end must pass a window wide enough to cover
        those notes, and must use the SAME window before and after, or the
        delta is meaningless.

        Returns None (not 0) when neither reader is available or one raises:
        "I could not count" and "there were none" are different answers, and
        conflating them is exactly the dishonesty this is here to remove.
        """
        try:
            if time_span is None:
                time_span = float(clip.length)
            time_span = max(float(time_span), 0.0)
            getter = getattr(clip, "get_notes_extended", None)
            if getter is not None:
                try:
                    return len(list(getter(0, 128, 0.0, time_span)))
                except Exception:
                    pass  # fall through to the legacy reader
            legacy = getattr(clip, "get_notes", None)
            if legacy is not None:
                return len(list(legacy(0.0, 0, time_span, 128)))
        except Exception:
            pass
        return None

    # notes defaults to an empty tuple, not [] — same "no notes" wire default
    # the dispatcher used to supply, without a mutable default argument.
    def _add_notes_to_clip(self, track_index=0, clip_index=0, notes=(),
                           expect_count=None, arrangement=False,
                           expect_track_name=None):
        """Add MIDI notes to a clip, reporting what Live actually took.

        The old result was {"note_count": len(notes)} — the caller's own
        argument handed back, so "added 262 notes" printed whether Live took
        262 or none. This counts the clip before and after the write and
        reports the delta.

        expect_count is the load-bearing half. A chunked write is emitted as
        several calls, and a list that arrives short arrives short ABOVE this
        script, where a before/after delta is silent because Live faithfully
        added everything it was handed. Stating the intended total refuses
        the write outright when the list disagrees, before anything lands.

        arrangement=True edits a clip already on the timeline instead of a
        Session slot — Clip.set_notes is the same call there, and the
        arrangement clip is addressed by clip_index into
        track.arrangement_clips, the order get_arrangement_clips reports.
        This is what makes a one-bar fix a one-call edit instead of "clear
        the Session clip, re-add, re-stamp fifteen positions". It does NOT
        end the re-stamp treadmill: fifteen stamped copies are fifteen
        independent clips, and editing one changes only that one.

        In BOTH views a note's start_time is measured from the clip's own
        start, not from the song's — an arrangement clip at bar 60 still
        takes notes at 0.0.
        """
        try:
            track, clip = self._resolve_clip_in_view(
                track_index, clip_index, arrangement=arrangement,
                expect_track_name=expect_track_name)

            requested = len(notes)
            if expect_count is not None and int(expect_count) != requested:
                # Refuse BEFORE writing: half a drum pattern in a clip is
                # worse than no drum pattern, because it looks finished.
                raise ValueError(
                    "Refusing to write: expect_count is %d but %d notes "
                    "arrived. Nothing was added — the note list was "
                    "truncated somewhere above this script; re-send the "
                    "chunk." % (int(expect_count), requested))

            # Convert note data to Live's format
            live_notes = []
            for note in notes:
                pitch = note.get("pitch", 60)
                start_time = note.get("start_time", 0.0)
                duration = note.get("duration", 0.25)
                velocity = note.get("velocity", 100)
                mute = note.get("mute", False)

                live_notes.append((pitch, start_time, duration, velocity, mute))

            # One window, used for both counts, wide enough to include notes
            # written past the clip's current end — the readers select by
            # start time, so a narrow window would hide exactly the notes a
            # caller is most likely to have got wrong.
            #
            # Which is also what happens to those notes: Live STORES a note
            # written past the clip's marker window (that is why these counts
            # find them) but plays only what falls inside start_marker to
            # end_marker. On a Session clip the window can be widened later.
            # On an ARRANGEMENT clip it cannot be widened from here — marker
            # writes land but never move an arrangement clip's footprint on
            # Live 12.4.3, which is why trim had to be rebuilt around
            # overlap-stamping (see the dead-end registry in
            # docs/IMPROVEMENTS.md). So an out-of-window note on an
            # arrangement clip is stored, silent and invisible in the
            # timeline until the clip is resized by hand or re-stamped.
            # UNVERIFIED against real Live for the arrangement case; the
            # counts below at least make the discrepancy visible in the
            # reply — a caller whose clip_note_count grows while nothing
            # sounds different has written outside the window.
            window = float(clip.length)
            for pitch, start_time, duration, velocity, mute in live_notes:
                window = max(window, float(start_time) + float(duration))
            window += 1.0

            before = self._count_clip_notes(clip, window)

            # Add the notes
            clip.set_notes(tuple(live_notes))

            after = self._count_clip_notes(clip, window)
            result = {
                "track_index": track_index,
                "track_name": track.name,
                "clip_index": clip_index,
                "clip_name": clip.name,
                "arrangement": bool(arrangement),
                "requested": requested,
                # None when the clip could not be counted, rather than a
                # confident zero.
                "added": (None if before is None or after is None
                          else after - before),
                "clip_note_count": after,
            }
            return result
        except Exception as e:
            self.log_message("Error adding notes to clip: " + str(e))
            raise

    def _set_clip_name(self, track_index=0, clip_index=0, name="",
                       expect_track_name=None):
        """Set the name of a clip"""
        try:
            track = self._resolve_track(track_index,
                                        expect_track_name=expect_track_name)

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            if not clip_slot.has_clip:
                raise Exception("No clip in slot")

            clip = clip_slot.clip
            previous_name = clip.name
            clip.name = name

            result = {
                "track_index": track_index,
                "track_name": track.name,
                "clip_index": clip_index,
                # The name that was there before: the only field that can
                # expose a rename aimed at the wrong slot.
                "previous_name": previous_name,
                "name": clip.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting clip name: " + str(e))
            raise

    def _set_arrangement_clip_name(self, track_index=0, clip_index=0, name="",
                                   expect_track_name=None):
        """Set the name of a clip placed in the Arrangement timeline.

        clip_index indexes into track.arrangement_clips, in the same order
        as returned by _get_arrangement_clips (i.e. ordered by start_time).
        """
        try:
            track, clip = self._resolve_arrangement_clip(
                track_index, clip_index, expect_track_name=expect_track_name)

            previous_name = clip.name
            start_time = float(getattr(clip, "start_time", 0.0))
            clip.name = name

            result = {
                "track_index": track_index,
                "track_name": track.name,
                "clip_index": clip_index,
                # start_time pins WHICH clip was renamed: arrangement clip
                # indices are positional ordinals that renumber on every
                # delete, so the ordinal alone identifies nothing later.
                "start_time": start_time,
                "previous_name": previous_name,
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
    
    def _fire_clip(self, track_index=0, clip_index=0, expect_track_name=None):
        """Fire a clip"""
        try:
            track = self._resolve_track(track_index,
                                        expect_track_name=expect_track_name)

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            if not clip_slot.has_clip:
                raise Exception("No clip in slot")

            clip_name = clip_slot.clip.name
            clip_slot.fire()

            result = {
                "track_index": track_index,
                "track_name": track.name,
                "clip_index": clip_index,
                "clip_name": clip_name,
                "fired": True
            }
            return result
        except Exception as e:
            self.log_message("Error firing clip: " + str(e))
            raise

    def _stop_clip(self, track_index=0, clip_index=0, expect_track_name=None):
        """Stop a clip"""
        try:
            track = self._resolve_track(track_index,
                                        expect_track_name=expect_track_name)

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            clip_slot.stop()

            result = {
                "track_index": track_index,
                "track_name": track.name,
                "clip_index": clip_index,
                "stopped": True
            }
            return result
        except Exception as e:
            self.log_message("Error stopping clip: " + str(e))
            raise

    def _delete_clip(self, track_index=0, clip_index=0, expect_track_name=None):
        """Delete the clip in the given clip slot, freeing the slot for reuse."""
        try:
            track = self._resolve_track(track_index,
                                        expect_track_name=expect_track_name)

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            if not clip_slot.has_clip:
                return {"track_index": track_index,
                        "track_name": track.name,
                        "clip_index": clip_index,
                        "deleted": False,
                        "reason": "Clip slot was already empty"}

            # Read the name before deleting — the clip object is gone afterwards
            deleted_name = clip_slot.clip.name

            clip_slot.delete_clip()

            return {"track_index": track_index,
                    "track_name": track.name,
                    "clip_index": clip_index,
                    # Verified, not assumed: the slot is what says it went.
                    "deleted": not clip_slot.has_clip,
                    "deleted_clip_name": deleted_name}
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
            requested = float(time)
            self._song.current_song_time = requested
            actual = self._song.current_song_time
            if abs(actual - requested) > 1e-3:
                # Right after stop_playback the transport is still settling:
                # a write can be swallowed (the retry covers that), and —
                # verified against Live 12.4.3 — read-backs can stay stale
                # for the rest of the tick even though the write landed.
                self._song.current_song_time = requested
                actual = self._song.current_song_time
            return {
                "current_song_time": actual,
                "requested": requested,
                "settled": abs(actual - requested) <= 1e-3,
            }
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

    def _clear_notes_from_clip(self, track_index=0, clip_index=0,
                               arrangement=False, expect_track_name=None):
        """Remove all MIDI notes from a clip.

        Pairs with _add_notes_to_clip to make a real replace (clear, then add),
        which the write-only API otherwise can't do. Counts notes first so the
        result can report how many were removed.

        arrangement=True clears a clip on the timeline instead of a Session
        slot; the removal window is the clip's own length, in the clip's own
        beats, exactly as for a Session clip.
        """
        try:
            track, clip = self._resolve_clip_in_view(
                track_index, clip_index, arrangement=arrangement,
                expect_track_name=expect_track_name)

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
                "track_name": track.name,
                "clip_index": clip_index,
                "clip_name": clip.name,
                "arrangement": bool(arrangement),
                "cleared_count": cleared,
                # What the clip holds now — the honest half of "cleared N",
                # which is otherwise a before-count with nothing behind it.
                "clip_note_count": self._count_clip_notes(clip, length),
            }
        except Exception as e:
            self.log_message("Error clearing notes from clip: " + str(e))
            raise

    def _stamp_session_clip_once(self, track, clip, destination_time,
                                 allow_loop_phase_reset):
        """One guarded, measured duplicate_clip_to_arrangement.

        Returns a placement row and NEVER raises: the caller decides whether
        a failure is fatal (the single-destination form, which has no
        successes to lose) or one row among many (the batched form, where
        raising would throw away every placement that already landed —
        placement 40 of 76 failing must not erase the 39 before it).

        Everything here is recomputed per placement on purpose. The stamp
        before this one has already changed the timeline, so guard and
        overlap answers from the top of the run would be describing an
        arrangement that no longer exists.
        """
        row = {
            "destination_time": destination_time,
            "ok": False,
            "error": None,
        }
        try:
            # The footprint is PREDICTED from the source clip's length —
            # there is no way to measure a stamp without making it. It is
            # exact for MIDI and for warped audio; an unwarped audio source
            # under tempo automation can stamp longer, which widens the real
            # overlap beyond what was checked here.
            stamp_start = float(destination_time)
            stamp_end = stamp_start + float(clip.length)
            row["stamp_start_time"] = stamp_start
            row["stamp_end_time"] = stamp_end

            victims = self._loop_phase_victims(track, stamp_start, stamp_end)
            if victims and not allow_loop_phase_reset:
                row["error"] = self._loop_phase_refusal(
                    track, victims, stamp_start, stamp_end)
                return row
            # Named before the stamp: afterwards these objects may be split
            # or gone.
            victim_names = [(self._clip_geometry(v) or {}).get("name")
                            for v in victims]

            overlapped, before = self._stamp_overlap_before(
                track, stamp_start, stamp_end)

            # Duplicate to arrangement at the requested beat position
            track.duplicate_clip_to_arrangement(clip, stamp_start)

            rows, span_after = self._stamp_overlap_after(
                track, overlapped, before, stamp_start, stamp_end)

            row["ok"] = True
            row["overlapped_clips"] = rows
            row["clips_in_span_after"] = span_after
            if victim_names:
                # Only present when the guard was overridden: name what was
                # re-phased, so the decision is on the record.
                row["loop_phase_reset"] = victim_names
        except Exception as e:
            row["ok"] = False
            row["error"] = str(e)
        return row

    def _duplicate_session_clip_to_arrangement(self, track_index=0, clip_index=0,
                                               destination_time=0.0,
                                               destination_times=None,
                                               allow_loop_phase_reset=False,
                                               expect_track_name=None):
        """Copy a Session-view clip into the Arrangement timeline.

        Uses the real Live API:
          track.duplicate_clip_to_arrangement(clip, destination_time)

        Available in Live 11 / 12.  destination_time is in beats from the
        start of the arrangement.

        destination_times places the SAME source clip at every beat in a
        list, inside one main-thread task. That is the whole point: laying
        a drum pattern across an arrangement was 465 round-trips and 63.5
        minutes in one session, arriving in runs of up to 76 identical
        calls. It saves round-trips, not main-thread work — Live still does
        one stamp per placement — so the run is capped at
        MAX_STAMP_PLACEMENTS.

        The two forms differ in how a failure is reported, and only there.
        With a single destination_time a refusal raises, as it always has:
        nothing landed, so there is nothing to report. With
        destination_times NOTHING raises per placement — every placement
        gets a {destination_time, ok, error} row and they all come back,
        because a run that stops at placement 40 must still tell the caller
        about the 39 that landed. Read `placed_count`, not just the reply
        arriving.

        Every placement is guarded independently: a stamp is REFUSED when it
        would leave a looping Arrangement clip surviving to its right,
        because that survivor silently restarts its loop from the top — see
        _loop_phase_victims for the incident this exists for. Batching makes
        that guard more load-bearing, not less: an unguarded run corrupts up
        to 128 positions per call instead of one.
        allow_loop_phase_reset=True stamps anyway, for a caller who has
        decided the re-phasing is what they want, and it applies to every
        placement in the run.

        Whatever the stamp lands on is reported before and after, so an
        overlap that edited clips the caller never named is visible in the
        reply rather than days later in a snapshot diff.
        """
        try:
            track = self._resolve_track(track_index,
                                        expect_track_name=expect_track_name)

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip slot index out of range")

            clip_slot = track.clip_slots[clip_index]

            if not clip_slot.has_clip:
                raise Exception(
                    "No clip in slot " + str(clip_index) +
                    " on track " + str(track_index)
                )

            clip = clip_slot.clip

            batched = destination_times is not None
            if batched:
                # Coerce the whole list up front: a bad entry found halfway
                # through would otherwise be discovered with clips already
                # on the timeline.
                try:
                    times = [float(t) for t in destination_times]
                except (TypeError, ValueError) as e:
                    raise ValueError(
                        "destination_times must be a list of beat positions "
                        "(%s); nothing was stamped" % e)
                if not times:
                    raise ValueError(
                        "destination_times was empty; nothing was stamped")
                if len(times) > MAX_STAMP_PLACEMENTS:
                    raise ValueError(
                        "Refusing %d placements in one call (limit %d): the "
                        "whole run happens on Live's main thread, where a "
                        "loop that long stalls the UI and the audio engine. "
                        "Nothing was stamped — split it into several calls."
                        % (len(times), MAX_STAMP_PLACEMENTS))
            else:
                times = [float(destination_time)]

            placements = []
            placed = 0
            for target_time in times:
                row = self._stamp_session_clip_once(
                    track, clip, target_time, allow_loop_phase_reset)
                placements.append(row)
                if row["ok"]:
                    placed += 1
                elif not batched:
                    # Single-destination form: preserve the raise it has
                    # always had, with the guard's own message intact.
                    raise Exception(row["error"]
                                    or "The stamp failed without a message")

            if batched:
                return {
                    # False whenever any placement failed — a batch that
                    # half-landed is not a success, and the caller has to be
                    # able to see that without reading every row.
                    "success": placed == len(times),
                    "track_index": track_index,
                    "track_name": track.name,
                    "clip_name": clip.name,
                    "destination_times": times,
                    "requested_count": len(times),
                    "placed_count": placed,
                    "failed_count": len(times) - placed,
                    "placements": placements,
                }

            row = placements[0]
            result = {
                "success": True,
                "track_index": track_index,
                "track_name": track.name,
                "clip_name": clip.name,
                "destination_time": destination_time,
                "stamp_start_time": row["stamp_start_time"],
                "stamp_end_time": row["stamp_end_time"],
                "overlapped_clips": row["overlapped_clips"],
                "clips_in_span_after": row["clips_in_span_after"],
                # The one-element batch shape, so a caller (or a presenter)
                # can read placements without branching on which form was used.
                "placements": placements,
            }
            if "loop_phase_reset" in row:
                result["loop_phase_reset"] = row["loop_phase_reset"]
            return result
        except Exception as e:
            self.log_message("Error duplicating clip to arrangement: " + str(e))
            raise

    def _park_playhead(self, target_time, tolerance=1e-3):
        """Move the playhead to target_time; report whether it verifiably landed.

        Both cue commands have to park the playhead before toggling, because
        Song.set_or_delete_cue() acts at current_song_time and nowhere else.
        Right after a stop the transport can swallow the first write and keep
        reading stale for the rest of the tick (verified on Live 12.4.3, the
        same race _set_current_song_time guards against), so the write is
        retried once and then VERIFIED. A toggle at the wrong position is the
        corruption itself — it creates a stray cue where the playhead really
        is, or deletes one that was there — which is why callers refuse
        instead of toggling on a False here.

        Returns (landed, read_back). Never raises: a caller that is about to
        refuse needs to name the stale value it actually read.
        """
        song = self._song
        song.current_song_time = target_time
        if abs(song.current_song_time - target_time) > tolerance:
            song.current_song_time = target_time
        read_back = song.current_song_time
        return abs(read_back - target_time) <= tolerance, read_back

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
                # Park the playhead, VERIFY it settled, then toggle — see
                # _park_playhead for the race and why a toggle at the wrong
                # position is worse than no toggle at all.
                landed, stale_read = self._park_playhead(target_time, tolerance)
                if not landed:
                    # Undo the (possibly landed) move before refusing, so a
                    # refusal has no side effect either.
                    try:
                        song.current_song_time = original_time
                    except Exception:
                        pass
                    raise Exception(
                        "Transport did not settle on beat %s (still reads "
                        "%s); no cue was toggled — retry in a moment"
                        % (target_time, stale_read))
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

    def _jump_to_locator(self, name="", time=None):
        """Jump the arrangement to an existing locator (cue point).

        CuePoint.jump() is the API twin of clicking the locator in the scrub
        area, and that click is the only way to move Live's start marker —
        the position play/record actually launch from. Writing
        current_song_time moves the visible playhead but leaves the start
        marker behind (verified on Live 12.4.3: record still launched from
        the old marker). While the transport is playing, jump() relocates
        playback instead (Live applies it at the song's quantization) and
        does not re-aim the start marker; the result's start_marker_set
        flag reports which of the two happened.

        Matches by exact name first, then by beat time (~1e-3 tolerance).

        The second parameter is named for the wire ("time"); it shadows the
        time module inside this method only, and the body never uses the
        module.
        """
        try:
            song = self._song
            available = ", ".join(
                "'%s' at %s" % (cue.name, cue.time)
                for cue in song.cue_points) or "none"
            if not name and time is None:
                raise Exception(
                    "Give a locator name or a beat time to jump to "
                    "(locators: %s)" % available)
            target = None
            if name:
                # No str() coercion: the wire hands us text already, and on
                # Live 10.1's Python 2 str() of a non-ASCII unicode name
                # raises before the time fallback could run.
                for cue in song.cue_points:
                    if cue.name == name:
                        target = cue
                        break
            if target is None and time is not None:
                target_time = float(time)
                for cue in song.cue_points:
                    if abs(cue.time - target_time) < 1e-3:
                        target = cue
                        break
            if target is None:
                raise Exception(
                    "No locator matches name=%r time=%r (locators: %s)"
                    % (name, time, available))

            was_playing = bool(song.is_playing)
            target.jump()
            return {
                "success": True,
                "name": target.name,
                "time": target.time,
                "was_playing": was_playing,
                "start_marker_set": not was_playing,
            }
        except Exception as e:
            self.log_message("Error jumping to locator: " + str(e))
            raise

    def _delete_locator(self, name="", time=None):
        """Delete an existing locator (cue point), matched by name or beat.

        The LOM has no "delete this cue" call. Song.set_or_delete_cue() is a
        TOGGLE at the playhead, and that is the entire hazard here: fired
        from the wrong position it does not fail, it CREATES a stray locator
        where the playhead really is — turning a delete into an extra
        locator plus the one you meant to remove. So this parks the playhead
        at the cue's OWN position, verifies it landed (see _park_playhead),
        and refuses without toggling when the transport has not settled.

        Matches by exact name first, then by beat time (~1e-3). Names are not
        unique in Live, so a name match takes the first cue with that name;
        pass time to be exact.

        The playhead is moved and then put back. While the transport is
        playing that move is audible — the same caveat create_locator
        carries.

        The second parameter is named for the wire ("time"); it shadows the
        time module inside this method only, and the body never uses the
        module.
        """
        try:
            song = self._song
            tolerance = 1e-3
            available = ", ".join(
                "'%s' at %s" % (cue.name, cue.time)
                for cue in song.cue_points) or "none"
            if not name and time is None:
                raise Exception(
                    "Give a locator name or a beat time to delete "
                    "(locators: %s)" % available)

            target = None
            if name:
                # No str() coercion: the wire hands us text already, and on
                # Live 10.1's Python 2 str() of a non-ASCII unicode name
                # raises before the time fallback could run.
                for cue in song.cue_points:
                    if cue.name == name:
                        target = cue
                        break
            if target is None and time is not None:
                wanted_time = float(time)
                for cue in song.cue_points:
                    if abs(cue.time - wanted_time) < tolerance:
                        target = cue
                        break
            if target is None:
                raise Exception(
                    "No locator matches name=%r time=%r (locators: %s)"
                    % (name, time, available))

            # Read the identity BEFORE the toggle — afterwards the CuePoint
            # is gone — and toggle at the cue's OWN time, never the caller's:
            # a name match came with no time at all, and a time match is only
            # good to a tolerance. Toggling one tolerance-width off the cue is
            # precisely how a stray gets created.
            cue_time = float(target.time)
            cue_name = target.name
            count_before = len(list(song.cue_points))

            original_time = song.current_song_time
            landed, stale_read = self._park_playhead(cue_time, tolerance)
            if not landed:
                # Undo the (possibly landed) move before refusing, so a
                # refusal has no side effect either.
                try:
                    song.current_song_time = original_time
                except Exception:
                    pass
                raise Exception(
                    "Transport did not settle on beat %s (still reads %s); "
                    "no cue was toggled and '%s' is untouched — retry in a "
                    "moment. Toggling from the wrong position would have "
                    "CREATED a locator there instead of deleting this one."
                    % (cue_time, stale_read, cue_name))

            # try/finally: the playhead was moved to the cue to toggle it, so
            # it must come back even if the toggle itself raises. Without this
            # a failed delete silently leaves the transport parked on the cue,
            # which the next create_locator would then toggle against.
            try:
                song.set_or_delete_cue()

                still_there = False
                for cue in song.cue_points:
                    if abs(cue.time - cue_time) < tolerance:
                        still_there = True
                        break
                count_after = len(list(song.cue_points))
            finally:
                try:
                    song.current_song_time = original_time
                except Exception:
                    pass

            return {
                # Two independent checks, because the failure mode this
                # command is shaped around ADDS a cue rather than failing:
                # the beat is clear AND the list got shorter by one. A toggle
                # that created something reads back as success False with a
                # grown count, not as a cheerful delete.
                "success": (not still_there
                            and count_after == count_before - 1),
                "deleted": not still_there,
                "name": cue_name,
                "time": cue_time,
                "cue_point_count_before": count_before,
                "cue_point_count": count_after,
            }
        except Exception as e:
            self.log_message("Error deleting locator: " + str(e))
            raise

    def _resolve_arrangement_clip(self, track_index, clip_index,
                                  expect_track_name=None):
        """Resolve (track, clip) for an Arrangement clip, or raise.

        clip_index indexes track.arrangement_clips in the same start-time
        order _get_arrangement_clips reports, so a caller can read the list
        and address what it saw.
        """
        track = self._resolve_track(track_index,
                                    expect_track_name=expect_track_name)
        clips = list(track.arrangement_clips)
        if clip_index < 0 or clip_index >= len(clips):
            raise IndexError(
                "Arrangement clip index out of range (track has %d)"
                % len(clips))
        return track, clips[clip_index]

    def _resolve_clip_in_view(self, track_index, clip_index, arrangement=False,
                              expect_track_name=None):
        """Resolve (track, clip) in either view — the switch _set_clip_gain
        already uses, shared so the note tools cannot drift from it.

        arrangement=False indexes track.clip_slots and is the default
        everywhere, because a Session slot is what every caller of these
        tools meant before the arrangement path existed. True indexes
        track.arrangement_clips in the same start-time order
        _get_arrangement_clips reports.
        """
        if arrangement:
            return self._resolve_arrangement_clip(
                track_index, clip_index, expect_track_name=expect_track_name)
        track = self._resolve_track(track_index,
                                    expect_track_name=expect_track_name)
        if clip_index < 0 or clip_index >= len(track.clip_slots):
            raise IndexError("Clip index out of range")
        clip_slot = track.clip_slots[clip_index]
        if not clip_slot.has_clip:
            raise Exception("No clip in slot")
        return track, clip_slot.clip

    def _trim_silence_wav_path(self):
        """Path to a tiny generated silent WAV, written on first use.

        The trim eraser must be a real Session clip, and on an audio track
        that takes a real audio file (ClipSlot.create_audio_clip). Built
        by hand from struct — Live's Python is assumed minimal — and left
        in the system temp directory between calls; Live's .asd sidecar
        lands next to it there too.
        """
        path = os.path.join(tempfile.gettempdir(),
                            "ableton_mcp_trim_silence.wav")
        if not os.path.exists(path):
            rate = 44100
            data = b"\x00\x00" * int(rate * TRIM_ERASER_WAV_SECONDS)
            header = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
                      + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate,
                                              rate * 2, 2, 16)
                      + b"data" + struct.pack("<I", len(data)))
            handle = open(path, "wb")
            try:
                handle.write(header + data)
            finally:
                handle.close()
        return path

    def _find_trim_stamp(self, track, position):
        """The Arrangement copy of the trim eraser starting at position."""
        for candidate in track.arrangement_clips:
            if (candidate.name == TRIM_ERASER_NAME
                    and abs(candidate.start_time - position) <= 1e-3):
                return candidate
        return None

    def _overlapping_arrangement_clips(self, track, region_start, region_end):
        """Every Arrangement clip on the track whose span overlaps the region.

        The one piece of overlap geometry in this file: the loop-phase guard
        and the trim's safety checks must agree about what "overlaps" means,
        and two copies of a comparison like this would drift.
        """
        hits = []
        for candidate in track.arrangement_clips:
            if (candidate.start_time < region_end - 1e-3
                    and candidate.end_time > region_start + 1e-3):
                hits.append(candidate)
        return hits

    def _region_occupied(self, track, region_start, region_end):
        """True if any Arrangement clip on the track overlaps the region.

        An inverted or empty region (start >= end) is trivially free.
        """
        return bool(self._overlapping_arrangement_clips(
            track, region_start, region_end))

    def _clip_geometry(self, clip):
        """One Arrangement clip's placement and loop window, or None.

        Read defensively on purpose: this is called again AFTER a stamp, and
        a clip Live fully covered no longer exists — reading a dead LOM
        object raises rather than returning anything.
        """
        try:
            return {
                "name": str(clip.name),
                "start_time": float(clip.start_time),
                "end_time": float(clip.end_time),
                "loop_start": self._safe_attr(clip, "loop_start", float, None),
                "loop_end": self._safe_attr(clip, "loop_end", float, None),
                "looping": bool(self._safe_attr(clip, "looping", bool, False)),
            }
        except Exception:
            return None

    def _loop_phase_victims(self, track, stamp_start, stamp_end):
        """Looping clips a stamp would leave a RIGHT-hand survivor of.

        Live resolves an Arrangement overlap by cropping whatever the
        incoming copy lands on. Cropping a clip's TAIL is harmless: the
        survivor keeps its start, so it keeps its phase. Cropping its HEAD —
        or splitting it in two — is not. The surviving right-hand piece
        keeps the clip's own loop_start, so from that beat on it replays the
        loop from the top instead of from where the music was.

        On a looping MIDI clip that is silent corruption, and it sounds
        plausible: three hat stamps scrambled bars 60-68, 156-164 and
        204-208 in one session, invented a crash nobody played, and were
        caught only by diffing a snapshot days later. Hence a refusal rather
        than a warning.

        Both damaging shapes reduce to one test — the stamp ends strictly
        before the clip does, so something survives on its right.
        """
        tol = 1e-3
        victims = []
        for candidate in self._overlapping_arrangement_clips(
                track, stamp_start, stamp_end):
            if not self._safe_attr(candidate, "looping", bool, False):
                continue
            if candidate.end_time > stamp_end + tol:
                victims.append(candidate)
        return victims

    def _loop_phase_refusal(self, track, victims, stamp_start, stamp_end):
        """The refusal message for _loop_phase_victims, naming the safe shapes."""
        stamp_length = stamp_end - stamp_start
        clauses = []
        for victim in victims:
            geometry = self._clip_geometry(victim) or {}
            start = geometry.get("start_time")
            end = geometry.get("end_time")
            # The two shapes that leave nothing to re-phase: cover the clip
            # exactly, or land so the stamp's END meets the clip's end.
            aligned = "?" if end is None else end - stamp_length
            covering = ("?" if end is None or start is None else end - start)
            clauses.append(
                "'%s' spans beats %s to %s (loop_start %s) and would survive "
                "from beat %s on with its loop restarted from the top; to "
                "end this stamp at that clip's end instead use "
                "destination_time %s, or cover the clip exactly with "
                "destination_time %s and a %s-beat source"
                % (geometry.get("name", "?"), start, end,
                   geometry.get("loop_start"), stamp_end, aligned, start,
                   covering))
        return (
            "Refusing this stamp at beat %s (footprint %s to %s on '%s'): it "
            "would re-phase %d looping clip(s). Live crops whatever a stamp "
            "lands on, and a survivor to the RIGHT of the stamp keeps its own "
            "loop_start, so it replays the loop from the top from that beat "
            "on — silently, and it sounds plausible. %s. Nothing was "
            "changed. Delete or unloop the clip first, use one of the safe "
            "shapes above, or pass allow_loop_phase_reset=true to accept the "
            "re-phasing."
            % (stamp_start, stamp_start, stamp_end, track.name, len(victims),
               "; ".join(clauses)))

    def _stamp_overlap_before(self, track, stamp_start, stamp_end):
        """(clips, geometries) for everything a stamp is about to land on.

        A stamp edits clips the caller never named, so a result that reports
        only the copy it made hides the damage by construction. The clip
        objects come back too, so the same ones can be re-read afterwards.
        """
        clips = self._overlapping_arrangement_clips(track, stamp_start,
                                                    stamp_end)
        return clips, [self._clip_geometry(c) for c in clips]

    def _stamp_overlap_after(self, track, clips, before, stamp_start, stamp_end):
        """Pair each pre-stamp geometry with how that clip reads back now.

        Live may have deleted a fully covered clip or split one in two, so
        this also reports every clip now sitting in the affected span —
        including pieces that did not exist before the stamp.
        """
        present = list(track.arrangement_clips)
        rows = []
        span_start = stamp_start
        span_end = stamp_end
        for geometry in before:
            if geometry is not None:
                span_start = min(span_start, geometry["start_time"])
                span_end = max(span_end, geometry["end_time"])

        for clip, geometry in zip(clips, before):
            after = self._clip_geometry(clip)
            still = False
            for candidate in present:
                if candidate is clip:
                    still = True
                    break
            if not still and after is not None:
                # LOM proxies are not guaranteed to compare identical across
                # two reads of arrangement_clips, so fall back to matching
                # the geometry the object itself just reported.
                for candidate in present:
                    other = self._clip_geometry(candidate)
                    if (other is not None
                            and other["name"] == after["name"]
                            and abs(other["start_time"]
                                    - after["start_time"]) <= 1e-3
                            and abs(other["end_time"]
                                    - after["end_time"]) <= 1e-3):
                        still = True
                        break
            rows.append({
                "name": (geometry or {}).get("name"),
                "before": geometry,
                # None means the clip no longer reads back at all — Live
                # covered it completely.
                "after": after if still else None,
                "still_on_timeline": still,
            })

        span = [self._clip_geometry(c)
                for c in self._overlapping_arrangement_clips(
                    track, span_start, span_end)]
        return rows, [g for g in span if g is not None]

    def _trim_arrangement_clip(self, track_index=0, clip_index=0,
                               start_time=None, end_time=None,
                               expect_track_name=None):
        """Trim an Arrangement clip's edges inward, in arrangement beats.

        Writing the clip's content markers verifiably does NOT move an
        Arrangement clip's footprint (Live 12.4.3: the write lands, the
        footprint stays — the approach this handler shipped with refused
        every trim there). What Live DOES do, same as in the UI, is
        permanently crop an existing Arrangement clip when another clip is
        stamped over it via Track.duplicate_clip_to_arrangement, and
        Track.delete_clip removes the stamp cleanly afterwards. Verified
        end to end on 12.4.3; this handler trims through that mechanism.

        The recipe, per requested edge:

        1. create a temporary "eraser" Session clip on the same track (a
           stamp only crops clips on its own track): a short MIDI clip, or
           on audio tracks a clip from a generated silent WAV;
        2. stamp it once in empty timeline space beyond every clip on the
           track and read the footprint back — the stamped length is
           MEASURED, not inferred from tempo/warp assumptions, and the
           probe proves the crop machinery exists before the take is at
           risk;
        3. stamp it with its cut-side edge exactly at the requested edge —
           the far side stays inside the region being removed, or overhangs
           only into timeline verified empty first (else the edge is
           refused with the take untouched);
        4. delete the stamp and any shard the stamp split off inside the
           removed region, then VERIFY the take's new edge by readback.

        Only shrinking is supported. The crop is real editing: the audio
        file on disk is never touched, but the way back is Edit > Undo in
        Live, not dragging the edge out. The start_time/end_time parameters
        are named for the wire; the time module is never used in this body.
        """
        try:
            track, clip = self._resolve_arrangement_clip(
                track_index, clip_index, expect_track_name=expect_track_name)
            if (not hasattr(track, "duplicate_clip_to_arrangement")
                    or not hasattr(track, "delete_clip")):
                raise Exception(
                    "This Live build does not expose the Live 11+ clip "
                    "duplicate/delete APIs the trim is built on; trim the "
                    "clip in the UI instead")

            take_name = clip.name
            old_start = clip.start_time
            old_end = clip.end_time
            new_start = old_start if start_time is None else float(start_time)
            new_end = old_end if end_time is None else float(end_time)
            tol = 1e-3

            if new_start < old_start - tol or new_end > old_end + tol:
                raise Exception(
                    "Can only trim inward: clip spans %s to %s, requested %s to %s"
                    % (old_start, old_end, new_start, new_end))
            # 2x tolerance, so the readback cleanup below can always tell
            # the surviving remainder from the stamp and shards beside it.
            if new_end - new_start < 2e-3:
                raise Exception("Trim would leave nothing of the clip")

            want_tail = new_end < old_end - tol
            want_head = new_start > old_start + tol
            if not want_tail and not want_head:
                return {
                    "track_index": track_index,
                    "track_name": track.name,
                    "clip_name": take_name,
                    "start_time": old_start,
                    "end_time": old_end,
                    "requested_start_time": new_start,
                    "requested_end_time": new_end,
                    "trimmed_head": False,
                    "trimmed_tail": False,
                    "refusals": [],
                }

            # The eraser Session clip, in the track's first empty slot.
            slot = None
            for candidate in track.clip_slots:
                if not candidate.has_clip:
                    slot = candidate
                    break
            if slot is None:
                raise Exception(
                    "Every Session slot on this track holds a clip; the trim "
                    "needs one empty slot for its temporary eraser clip — "
                    "clear a slot (or add a scene) and retry")
            if getattr(track, "has_midi_input", False):
                # A fresh MIDI clip is a content-free container: the ideal
                # eraser. Sized to sit inside the smaller requested region
                # where the regions allow it.
                regions = []
                if want_tail:
                    regions.append(old_end - new_end)
                if want_head:
                    regions.append(new_start - old_start)
                slot.create_clip(max(0.0625, min([1.0] + regions)))
            else:
                if not hasattr(slot, "create_audio_clip"):
                    raise Exception(
                        "Trimming an audio take needs "
                        "ClipSlot.create_audio_clip (Live 12.0.5+) for the "
                        "temporary eraser clip; trim the clip in the UI "
                        "instead")
                slot.create_audio_clip(self._trim_silence_wav_path())
            eraser = slot.clip
            eraser.name = TRIM_ERASER_NAME

            refusals = []
            trimmed_head = False
            trimmed_tail = False
            cur_start = old_start
            cur_end = old_end
            aborted = False
            try:
                # Measure the eraser's true stamped footprint in empty
                # timeline space right of everything on the track.
                clear_pos = max([c.end_time
                                 for c in track.arrangement_clips]) + 8.0
                track.duplicate_clip_to_arrangement(eraser, clear_pos)
                probe = self._find_trim_stamp(track, clear_pos)
                if probe is None:
                    raise Exception(
                        "the measuring stamp of the eraser clip never "
                        "appeared on the timeline, so no trim was attempted "
                        "(the take is untouched)")
                eraser_len = probe.end_time - probe.start_time
                track.delete_clip(probe)
                if eraser_len <= tol:
                    raise Exception(
                        "the eraser clip stamped to a zero-length footprint, "
                        "so no trim was attempted (the take is untouched)")

                if want_tail:
                    cut = new_end
                    # A stamp's START is exact (it is the destination
                    # argument), but its length can exceed the measured one
                    # (an unwarped audio eraser under tempo automation), so
                    # everything up to twice the measured footprint past the
                    # cut — where the stamp's far edge could land beyond the
                    # take — must be empty. For any region wider than that,
                    # this check is inert.
                    if self._region_occupied(track, cur_end,
                                             cut + 2.0 * eraser_len):
                        refusals.append(
                            "end: a clip sits within the stamp's safety "
                            "zone right of the take (2x the eraser's %.4f-"
                            "beat footprint past the cut), so stamping "
                            "risks cropping it; this edge is untouched — "
                            "trim it in the UI" % eraser_len)
                    else:
                        track.duplicate_clip_to_arrangement(eraser, cut)
                        # Everything now inside [cut, safety zone] is ours:
                        # the stamp, plus the shard the stamp split off the
                        # take when it was shorter than the region. The
                        # remainder starts at cur_start, left of the scan.
                        shard_hi = max(cur_end, cut + 2.0 * eraser_len)
                        for shard in [c for c in list(track.arrangement_clips)
                                      if c.start_time >= cut - tol
                                      and c.end_time <= shard_hi + tol]:
                            track.delete_clip(shard)
                        remainder = None
                        for c in track.arrangement_clips:
                            if (c.name == take_name
                                    and abs(c.start_time - cur_start) <= tol):
                                remainder = c
                                break
                        if remainder is not None and abs(
                                remainder.end_time - cut) <= tol:
                            trimmed_tail = True
                            cur_end = remainder.end_time
                        elif remainder is not None and abs(
                                remainder.end_time - cur_end) <= tol:
                            refusals.append(
                                "end: stamping over the region did not crop "
                                "the take (this Live build's overlap "
                                "behavior differs); the clip is unchanged")
                        else:
                            aborted = True

                if want_head and not aborted:
                    # Head-trimming a LOOPING take re-phases what survives,
                    # the same physics _loop_phase_victims refuses for a
                    # stamp. It is not refused here on purpose: moving this
                    # clip's start edge is precisely what the caller asked
                    # for, where a stamp's damage is collateral to a clip
                    # nobody named. Re-check the loop window afterwards on a
                    # looping take.
                    cut = new_start
                    dest = cut - eraser_len
                    if dest < -tol:
                        refusals.append(
                            "start: the eraser's %.4f-beat footprint is "
                            "longer than the %.4f-beat region to remove and "
                            "its stamp would start before beat 0; this edge "
                            "is untouched — trim it in the UI"
                            % (eraser_len, cut - cur_start))
                    elif self._region_occupied(track, dest, cur_start):
                        # Overhang left of the take. Unlike the tail, no 2x
                        # margin: the stamp's start is exactly dest, so only
                        # [dest, take start] can be hit.
                        refusals.append(
                            "start: the eraser's %.4f-beat footprint is "
                            "longer than the %.4f-beat region to remove and "
                            "a clip sits in the overhang left of the take, "
                            "so stamping risks cropping it; this edge is "
                            "untouched — trim it in the UI"
                            % (eraser_len, cut - cur_start))
                    else:
                        track.duplicate_clip_to_arrangement(
                            eraser, max(dest, 0.0))
                        shard_lo = min(dest, cur_start)
                        for shard in [c for c in list(track.arrangement_clips)
                                      if c.start_time >= shard_lo - tol
                                      and c.end_time <= cut + tol]:
                            track.delete_clip(shard)
                        remainder = None
                        for c in track.arrangement_clips:
                            if (c.name == take_name
                                    and abs(c.end_time - cur_end) <= tol):
                                remainder = c
                                break
                        if remainder is not None and abs(
                                remainder.start_time - cut) <= tol:
                            trimmed_head = True
                            cur_start = remainder.start_time
                        elif remainder is not None and abs(
                                remainder.start_time - cur_start) <= tol:
                            refusals.append(
                                "start: stamping over the region did not "
                                "crop the take (this Live build's overlap "
                                "behavior differs); the clip is unchanged")
                        else:
                            aborted = True

                if aborted:
                    # A stamp landed but the readback found no take where
                    # one was expected. Report whatever overlaps the take's
                    # old span, honestly — Edit > Undo in Live is the way
                    # back from a crop that went somewhere wrong.
                    actual = None
                    for c in track.arrangement_clips:
                        if (c.name == take_name
                                and c.end_time > old_start + tol
                                and c.start_time < old_end - tol):
                            actual = c
                            break
                    if actual is not None:
                        cur_start = actual.start_time
                        cur_end = actual.end_time
                    refusals.append(
                        "after a stamp the take was not at the span the "
                        "readback expected; reporting what was found "
                        "instead, and no further edge was touched — "
                        "Edit > Undo in Live rewinds the edit if it is "
                        "wrong")
            finally:
                # The eraser Session clip never outlives the call, whatever
                # happened above.
                try:
                    slot.delete_clip()
                except Exception as cleanup_error:
                    self.log_message(
                        "Trim cleanup: could not delete the temporary "
                        "eraser Session clip: " + str(cleanup_error))

            return {
                "track_index": track_index,
                "track_name": track.name,
                "clip_name": take_name,
                "start_time": cur_start,
                "end_time": cur_end,
                "requested_start_time": new_start,
                "requested_end_time": new_end,
                "trimmed_head": trimmed_head,
                "trimmed_tail": trimmed_tail,
                "refusals": refusals,
            }
        except Exception as e:
            self.log_message("Error trimming arrangement clip: " + str(e))
            raise

    def _find_arrangement_clip_at(self, track, start_time, tolerance=1e-3):
        """(index, clip) for the Arrangement clip starting at start_time, or None.

        Live permits no overlapping clips on one track, so a start_time is a
        genuine unique key — unlike clip_index, which is a positional ordinal
        that renumbers under the caller on every delete. That renumbering is
        what forced a hand-maintained highest-index-first ritual (and 63
        re-reads of the clip list in one session); addressing by beat needs
        neither.

        The match is by value within a tolerance because a hand-dragged clip
        can sit on an arbitrary fraction of a beat — the same shape
        _find_trim_stamp uses.
        """
        for index, candidate in enumerate(track.arrangement_clips):
            try:
                if abs(float(candidate.start_time) - start_time) <= tolerance:
                    return index, candidate
            except Exception:
                continue
        return None

    def _nearby_arrangement_starts(self, track, wanted, limit=8):
        """The clips whose start_time is nearest `wanted`, as a phrase.

        The near misses, not the whole track: _jump_to_locator can list every
        cue point because a set holds a handful, but a busy take track
        carries dozens of clips and a wall of them is not a hint. Sorted by
        distance so the intended clip is nearly always first.
        """
        rows = []
        for index, clip in enumerate(track.arrangement_clips):
            try:
                start = float(clip.start_time)
            except Exception:
                continue
            rows.append((abs(start - wanted), index, start, clip.name))
        if not rows:
            return "none"
        rows.sort()
        shown = rows[:limit]
        phrase = ", ".join("'%s' at %s (index %d)" % (name, start, index)
                           for _distance, index, start, name in shown)
        if len(rows) > len(shown):
            phrase += ", ... (%d more)" % (len(rows) - len(shown))
        return phrase

    def _delete_arrangement_clip(self, track_index=0, clip_index=None,
                                 start_time=None, start_times=None,
                                 expect_track_name=None):
        """Delete one or more clips from the Arrangement timeline.

        Track.delete_clip has existed since Live 11; older builds get an
        honest refusal instead of an AttributeError. Session clips keep
        their own tool (delete_clip) — this one exists because take cleanup
        (stray record fragments, replaced sections) happens in the
        Arrangement.

        Three ways to say which clip, in order of preference:

        - start_times: a list of beat positions. This is the one that
          removes the hazard rather than documenting it. Every position is
          resolved BEFORE anything is deleted, and the deletes then run
          highest-index-first internally, so the caller never has to
          maintain that ritual — and never has to re-read the clip list
          between deletes to survive the renumbering.
        - start_time: one beat position, matched within 1e-3.
        - clip_index: the positional ordinal into track.arrangement_clips.
          It works and it stays, but it renumbers on every delete, which is
          exactly what makes a multi-clip cleanup error-prone.

        A start_time that matches nothing is refused with the nearby
        start_times listed, the way jump_to_locator lists the cue points it
        knows. For the plural form that refusal happens before ANY delete:
        an unmatched position usually means the caller's picture of the
        timeline is stale, and the other positions in the same list are then
        no more trustworthy than the bad one.
        """
        try:
            track = self._resolve_track(track_index,
                                        expect_track_name=expect_track_name)
            if not hasattr(track, "delete_clip"):
                raise Exception(
                    "This Live build does not expose Track.delete_clip "
                    "(Live 11+); delete the clip in the UI instead")

            if start_times is not None:
                return self._delete_arrangement_clips_at(
                    track, track_index, start_times)

            if start_time is not None:
                wanted = float(start_time)
                found = self._find_arrangement_clip_at(track, wanted)
                if found is None:
                    raise Exception(
                        "No arrangement clip starts at beat %s on '%s'; "
                        "nothing was deleted. Nearest: %s"
                        % (wanted, track.name,
                           self._nearby_arrangement_starts(track, wanted)))
                index, clip = found
                matched_by = "start_time"
            elif clip_index is not None:
                index = clip_index
                clips = list(track.arrangement_clips)
                if index < 0 or index >= len(clips):
                    raise IndexError(
                        "Arrangement clip index out of range (track has %d)"
                        % len(clips))
                clip = clips[index]
                matched_by = "clip_index"
            else:
                raise ValueError(
                    "Give clip_index, start_time or start_times to say which "
                    "clip to delete (clips: %s)"
                    % self._nearby_arrangement_starts(track, 0.0))

            deleted_name = clip.name
            deleted_start = clip.start_time
            deleted_end = clip.end_time
            count_before = len(list(track.arrangement_clips))
            track.delete_clip(clip)
            count_after = len(list(track.arrangement_clips))
            return {
                "track_index": track_index,
                "track_name": track.name,
                # Verified by count, not assumed from the call returning:
                # a delete that quietly did nothing renumbers nothing, and a
                # highest-index-first sweep built on that lie removes the
                # wrong clips next.
                "deleted": count_after == count_before - 1,
                "deleted_clip_name": deleted_name,
                "clip_index": index,
                "matched_by": matched_by,
                "start_time": deleted_start,
                "end_time": deleted_end,
                "arrangement_clip_count": count_after,
            }
        except Exception as e:
            self.log_message("Error deleting arrangement clip: " + str(e))
            raise

    def _delete_arrangement_clips_at(self, track, track_index, start_times):
        """Delete every Arrangement clip at the given beats, in one pass."""
        try:
            wanted = [float(t) for t in start_times]
        except (TypeError, ValueError) as e:
            raise ValueError(
                "start_times must be a list of beat positions (%s); nothing "
                "was deleted" % e)
        if not wanted:
            raise ValueError("start_times was empty; nothing was deleted")

        # Resolve everything first. A delete cannot be undone from here, so
        # an unresolvable position refuses the whole batch while it is still
        # free to do so: nothing has been deleted yet, and the caller can
        # re-read and re-issue. (Contrast the batched stamp, where a failure
        # mid-run must NOT erase the placements that already landed — there
        # the successes exist and hiding them is the harm.)
        resolved = []
        missing = []
        for target in wanted:
            found = self._find_arrangement_clip_at(track, target)
            if found is None:
                missing.append(target)
            else:
                resolved.append((target, found[0], found[1]))
        if missing:
            raise Exception(
                "No arrangement clip starts at beat(s) %s on '%s'; NOTHING "
                "was deleted (all %d requested positions were abandoned, so "
                "the timeline is exactly as you last read it). Nearest to "
                "%s: %s"
                % (", ".join(str(m) for m in missing), track.name,
                   len(wanted), missing[0],
                   self._nearby_arrangement_starts(track, missing[0])))

        seen = {}
        for target, index, _clip in resolved:
            if index in seen:
                raise ValueError(
                    "Beats %s and %s both resolve to arrangement clip index "
                    "%d; nothing was deleted. Remove the duplicate — the "
                    "second delete would either fail or take a clip you did "
                    "not name." % (seen[index], target, index))
            seen[index] = target

        # Highest index first, so the ordinals of the clips still to be
        # deleted cannot shift under the loop. The caller supplied beats and
        # never has to know this happened — which is the point.
        order = sorted(range(len(resolved)),
                       key=lambda i: resolved[i][1], reverse=True)

        rows = [None] * len(resolved)
        deleted = 0
        for position in order:
            target, index, clip = resolved[position]
            row = {
                "start_time": target,
                "clip_index": index,
                "ok": False,
                "error": None,
            }
            try:
                row["deleted_clip_name"] = clip.name
                row["end_time"] = float(clip.end_time)
                count_before = len(list(track.arrangement_clips))
                track.delete_clip(clip)
                count_after = len(list(track.arrangement_clips))
                row["ok"] = count_after == count_before - 1
                if not row["ok"]:
                    row["error"] = (
                        "Track.delete_clip returned but the clip count did "
                        "not drop (%d before, %d after)"
                        % (count_before, count_after))
            except Exception as e:
                # One clip Live refuses must not abandon the rest — and must
                # not be reported as gone.
                row["error"] = str(e)
            if row["ok"]:
                deleted += 1
            rows[position] = row

        return {
            "track_index": track_index,
            "track_name": track.name,
            # False if a single one of them failed: a half-finished cleanup
            # that reports success is how the wrong clips get deleted next.
            "success": deleted == len(rows),
            "requested_count": len(rows),
            "deleted_count": deleted,
            "failed_count": len(rows) - deleted,
            # In the caller's order, not the execution order — they asked in
            # beats and can read the answer back in beats.
            "deletions": rows,
            "arrangement_clip_count": len(list(track.arrangement_clips)),
        }

    def _move_arrangement_clip(self, track_index=0, clip_index=0,
                               destination_time=0.0, expect_track_name=None):
        """Move an Arrangement clip so it starts at destination_time (beats).

        Live's LOM has no true move: Clip.position is the clip's LOOP
        position (== loop_start), so writing it slides the content window
        rather than the clip — and on builds where marker writes move the
        footprint it could even pass a footprint readback while silently
        re-slicing the take. The honest recipe is the two documented
        Live 11+ calls this script already uses: duplicate the clip to the
        destination, verify the copy landed, then delete the original.
        A destination overlapping the clip's own span is refused — Live's
        overlap handling would eat into the source before it could be
        deleted; make such a move in two hops via a clear stretch of the
        timeline.
        """
        try:
            track, clip = self._resolve_arrangement_clip(
                track_index, clip_index, expect_track_name=expect_track_name)
            if (not hasattr(track, "duplicate_clip_to_arrangement")
                    or not hasattr(track, "delete_clip")):
                raise Exception(
                    "This Live build does not expose the Live 11+ clip "
                    "duplicate/delete APIs; move the clip in the UI instead")

            clip_name = clip.name
            old_start = clip.start_time
            old_end = clip.end_time
            length = old_end - old_start
            target = float(destination_time)

            if abs(target - old_start) <= 1e-3:
                return {
                    "track_index": track_index,
                    "track_name": track.name,
                    "clip_name": clip_name,
                    "start_time": old_start,
                    "end_time": old_end,
                    "moved": False,
                }
            if target < old_end and target + length > old_start:
                raise Exception(
                    "Destination %s overlaps the clip's own span (%s to %s); "
                    "Live's overlap handling would eat into the source before "
                    "the move completes — move it in two hops via a clear "
                    "stretch of the timeline" % (target, old_start, old_end))

            track.duplicate_clip_to_arrangement(clip, target)
            moved = None
            for candidate in track.arrangement_clips:
                if (candidate is not clip
                        and abs(candidate.start_time - target) <= 1e-3):
                    moved = candidate
                    break
            if moved is None:
                raise Exception(
                    "The duplicate did not appear at %s; the original clip "
                    "was left untouched" % target)
            track.delete_clip(clip)
            return {
                "track_index": track_index,
                "track_name": track.name,
                "clip_name": clip_name,
                "start_time": moved.start_time,
                "end_time": moved.end_time,
                "moved": True,
            }
        except Exception as e:
            self.log_message("Error moving arrangement clip: " + str(e))
            raise

    def _duplicate_arrangement_clip(self, track_index=0, clip_index=0,
                                    destination_time=0.0,
                                    allow_loop_phase_reset=False,
                                    expect_track_name=None):
        """Copy an Arrangement clip to another position on the same track.

        The same Live 11+ API the session duplicate uses —
        track.duplicate_clip_to_arrangement — accepts an Arrangement clip
        as its source, which is how an already-recorded take gets reused
        at another section. An occupied destination is resolved by Live
        itself (typically by replacing the overlapped region of the
        existing clip — including the source's own span, which truncates
        the source); the source echo below is captured BEFORE the call, so
        the reply stays valid even when the copy lands on the source.

        Carries the same loop-phase refusal as the Session stamp: a looping
        clip left surviving to the right of this copy silently replays its
        loop from the top. allow_loop_phase_reset=True stamps anyway.
        """
        try:
            track, clip = self._resolve_arrangement_clip(
                track_index, clip_index, expect_track_name=expect_track_name)
            if not hasattr(track, "duplicate_clip_to_arrangement"):
                raise Exception(
                    "This Live build does not expose "
                    "Track.duplicate_clip_to_arrangement (Live 11+)")
            # Read the echo before mutating: an overlap with the source's
            # own span can truncate or destroy the source clip object.
            source_name = clip.name
            source_start = clip.start_time
            source_end = clip.end_time

            # An Arrangement source's footprint is its own span; Clip.length
            # is the fallback for a build that reports one and not the other.
            stamp_start = float(destination_time)
            span = float(source_end) - float(source_start)
            if span <= 0:
                span = float(clip.length)
            stamp_end = stamp_start + span

            victims = self._loop_phase_victims(track, stamp_start, stamp_end)
            if victims and not allow_loop_phase_reset:
                raise Exception(self._loop_phase_refusal(
                    track, victims, stamp_start, stamp_end))
            victim_names = [(self._clip_geometry(v) or {}).get("name")
                            for v in victims]

            overlapped, before = self._stamp_overlap_before(
                track, stamp_start, stamp_end)

            track.duplicate_clip_to_arrangement(clip, stamp_start)

            rows, span_after = self._stamp_overlap_after(
                track, overlapped, before, stamp_start, stamp_end)

            result = {
                "track_index": track_index,
                "track_name": track.name,
                "clip_name": source_name,
                "destination_time": float(destination_time),
                "source_start_time": source_start,
                "source_end_time": source_end,
                "stamp_start_time": stamp_start,
                "stamp_end_time": stamp_end,
                "overlapped_clips": rows,
                "clips_in_span_after": span_after,
            }
            if victim_names:
                result["loop_phase_reset"] = victim_names
            return result
        except Exception as e:
            self.log_message("Error duplicating arrangement clip: " + str(e))
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

    def _duplicate_track(self, track_index=0, expect_track_name=None):
        """Duplicate a track whole — devices, mixer, routing and clips.

        Song.duplicate_track(index) is the LOM twin of Ctrl-D on a track
        header, and it copies everything: the device chain with its dialled
        values, the mixer (volume, pan, sends), the routing, and every clip
        in both views. Double-tracking a part used to mean creating a track,
        re-routing it to the same bus, re-panning it, and reloading each
        device by hand — one call replaces all of it.

        The copy is inserted immediately BELOW the source, which is the
        second reason to prefer this over create + rebuild: the index shift
        is knowable in advance (everything at or below source+1 moves down
        by one) instead of having to re-read the whole session afterwards.

        Live names the copy itself (typically the source name with a
        numeric suffix); follow with set_track_name to name it properly.

        The track cap is NOT pre-checked, because it cannot be: the LOM
        exposes no edition or product property, so Intro's 16-track ceiling
        surfaces only as a raise from this call. That raise is caught and
        reported as itself rather than dressed up as success.
        """
        try:
            track = self._resolve_track(track_index,
                                        expect_track_name=expect_track_name)
            if not hasattr(self._song, "duplicate_track"):
                raise Exception(
                    "This Live build does not expose Song.duplicate_track; "
                    "duplicate the track in the UI instead")

            source_name = track.name
            count_before = len(self._song.tracks)

            try:
                self._song.duplicate_track(track_index)
            except Exception as e:
                # Almost always the edition track cap. Say what Live said and
                # state plainly that nothing was created — a caller that
                # assumes success here goes on to address a track that does
                # not exist.
                raise Exception(
                    "Live refused to duplicate track %d ('%s'): %s. No track "
                    "was created — Live's Intro and Lite editions cap the "
                    "track count and the cap cannot be read ahead of time."
                    % (track_index, source_name, e))

            count_after = len(self._song.tracks)
            new_index = track_index + 1
            duplicated = (count_after == count_before + 1
                          and new_index < count_after)
            new_name = None
            if duplicated:
                # Verified by reading the slot the copy is supposed to
                # occupy, not assumed from the call returning: this is the
                # index every following command will use.
                new_name = self._song.tracks[new_index].name

            return {
                "source_track_index": track_index,
                "source_track_name": source_name,
                "duplicated": duplicated,
                # The copy's own index and Live-assigned name — None when the
                # count says nothing was actually inserted.
                "index": new_index if duplicated else None,
                "name": new_name,
                "track_count_before": count_before,
                "track_count_after": count_after,
            }
        except Exception as e:
            self.log_message("Error duplicating track: " + str(e))
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

    def _set_track_arm(self, track_index=0, value=True, expect_track_name=None):
        """Arm or disarm a track for recording"""
        try:
            track = self._resolve_track(track_index,
                                        expect_track_name=expect_track_name)
            if not track.can_be_armed:
                raise ValueError("Track %d cannot be armed" % track_index)
            track.arm = bool(value)
            return {"track_index": track_index, "track_name": track.name,
                    "arm": bool(track.arm)}
        except Exception as e:
            self.log_message("Error arming track: " + str(e))
            raise

    def _set_track_monitoring(self, track_index=0, value="auto",
                              expect_track_name=None):
        """Set input monitoring. 0 = In, 1 = Auto, 2 = Off (Live's own ordering)."""
        try:
            track = self._resolve_track(track_index,
                                        expect_track_name=expect_track_name)
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

    def _load_instrument_or_effect(self, track_index=0, uri="", track_type="regular",
                                   expect_track_name=None):
        """Load an instrument or effect onto a track by its browser URI.

        The command dispatcher above calls this method, but it was never
        defined — and "load_instrument_or_effect" was missing from the list of
        main-thread commands as well, so the command fell through to the final
        "Unknown command" branch. Loading a device is exactly what
        _load_browser_item does, so delegate to it; the only difference is the
        parameter name the MCP server uses ("uri" vs "item_uri").
        """
        return self._load_browser_item(track_index, uri, track_type,
                                       expect_track_name=expect_track_name)

    def _load_browser_item(self, track_index=0, item_uri="", track_type="regular",
                           expect_track_name=None):
        """Load a browser item onto a track by its URI.

        track_type accepts "regular", "return" or "master", so effects can be
        placed on the master bus and on send returns, not just regular tracks.
        """
        try:
            # expect_track_name earns its keep here above all: this is the
            # call that put an Auto Pan on the BASS bus instead of GUITARS,
            # and a device loaded onto the wrong track is silent until
            # somebody notices the wrong thing moving.
            track = self._resolve_track(track_index, track_type,
                                        expect_track_name=expect_track_name)
            
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            
            # Find the browser item by URI
            item = self._find_browser_item_by_uri(app.browser, item_uri)
            
            if not item:
                raise ValueError("Browser item with URI '{0}' not found".format(item_uri))
            
            # Select the track
            self._song.view.selected_track = track

            devices_before = [d.name for d in track.devices]

            # Load the item
            app.browser.load_item(item)

            devices_after = [d.name for d in track.devices]
            # Multiset diff, so a second copy of an already-present device
            # still counts as new.
            remaining = {}
            for name in devices_before:
                remaining[name] = remaining.get(name, 0) + 1
            new_devices = []
            for name in devices_after:
                if remaining.get(name, 0) > 0:
                    remaining[name] -= 1
                else:
                    new_devices.append(name)

            result = {
                "loaded": True,
                "item_name": item.name,
                "track_name": track.name,
                "uri": item_uri,
                "new_devices": new_devices,
                "devices_after": devices_after,
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

    def _map_rack_magnitude(self, track_index=0, device_index=0,
                            macro_name="Magnitude", expect_track_name=None):
        """Rename Macro 1 and map nested Dry/Wet (or Mix/Amount) params to it."""
        track = self._resolve_track(track_index,
                                    expect_track_name=expect_track_name)
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

    def _warp_marker_count(self, clip):
        """How many warp markers a clip has, without materialising them.

        The count is what actually answers the question a caller has ("is
        this take hand-warped, or is it one marker at zero?"); the pairs
        themselves are what blow the payload. So the count stays free and
        the list is opt-in — see _serialize_clip_common.
        """
        try:
            raw = getattr(clip, "warp_markers", None)
            if not raw:
                return 0
            return len(raw)
        except Exception:
            return 0

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

    def _chain_count(self, rack):
        """len(RackDevice.chains) — the cheap stand-in for the whole walk."""
        try:
            raw = getattr(rack, "chains", None)
            if not raw:
                return 0
            return len(raw)
        except Exception:
            return 0

    def _serialize_device(self, device, device_index, include_params=True,
                          include_rack_chains=False, depth=0):
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
        # That walk is also ~9.5 KB for a single drum rack, and a drum rack is
        # exactly the device a session keeps re-reading for unrelated reasons,
        # so since v3 the caller opts in. chain_count keeps the rack legible
        # when they do not: 16 chains says "there are pads here" for 20 bytes.
        if getattr(device, "can_have_chains", False):
            if depth >= self._MAX_CHAIN_DEPTH:
                info["chains_truncated"] = True
            elif include_rack_chains:
                info["chains"] = self._serialize_chains(
                    device, include_params=include_params, depth=depth
                )
            else:
                info["chain_count"] = self._chain_count(device)
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
                                    # Already inside an opted-in walk: a rack
                                    # nested in a chain keeps expanding, or
                                    # the caller who asked for chains gets a
                                    # bare count exactly where the sound is.
                                    include_rack_chains=True,
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

    def _serialize_clip_common(self, clip, include_warp_markers=False):
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
        # Warp markers dominated every early snapshot — 73-81% of the bytes,
        # because one warped audio take carries hundreds of
        # {beat_time, sample_time} pairs and a long arrangement carries the
        # same take fifteen times over. They were the one field gated by
        # neither existing flag, so a caller who turned notes AND params off
        # still could not get a snapshot under the output cap. Default off
        # since v3; the count survives, so a clip that needs a closer look
        # still announces itself.
        if include_warp_markers:
            markers = self._warp_markers_from_clip(clip)
            if markers:
                info["warp_markers"] = markers
                info["warp_marker_count"] = len(markers)
        else:
            count = self._warp_marker_count(clip)
            if count:
                info["warp_marker_count"] = count
        return dict((k, v) for k, v in info.items() if v is not None)

    def _serialize_session_clip(self, clip, include_notes=True,
                                include_warp_markers=False):
        info = {
            "name": clip.name,
            "length": float(clip.length),
            "is_playing": bool(clip.is_playing),
            "is_recording": bool(getattr(clip, "is_recording", False)),
            "is_midi_clip": bool(getattr(clip, "is_midi_clip", False)),
            "is_audio_clip": bool(getattr(clip, "is_audio_clip", False)),
            "color": int(getattr(clip, "color", 0)),
        }
        info.update(self._serialize_clip_common(
            clip, include_warp_markers=include_warp_markers))
        if include_notes and info["is_midi_clip"]:
            info["notes"] = self._notes_from_clip(clip)
            info["note_count"] = len(info["notes"])
        return info

    def _serialize_arrangement_clip(self, clip, include_notes=True,
                                    include_warp_markers=False):
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
        info.update(self._serialize_clip_common(
            clip, include_warp_markers=include_warp_markers))
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

    def _serialize_return_tracks(self, include_params=True,
                                 include_rack_chains=False):
        returns = []
        try:
            for i, track in enumerate(self._song.return_tracks):
                devices = []
                for d_i, device in enumerate(track.devices):
                    devices.append(
                        self._serialize_device(
                            device, d_i, include_params=include_params,
                            include_rack_chains=include_rack_chains)
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

    def _serialize_master_track(self, include_params=True,
                                include_rack_chains=False):
        """Master chain — the bus compressor/limiter that shapes the final sound."""
        try:
            track = self._song.master_track
            devices = []
            for d_i, device in enumerate(track.devices):
                devices.append(
                    self._serialize_device(
                        device, d_i, include_params=include_params,
                        include_rack_chains=include_rack_chains)
                )
            return {
                "volume": float(track.mixer_device.volume.value),
                "panning": float(track.mixer_device.panning.value),
                "devices": devices,
            }
        except Exception as e:
            self.log_message("master_track serialize failed: " + str(e))
            return None

    def _get_clip_notes(self, track_index=0, clip_index=0, arrangement=False):
        """Read one clip's MIDI notes, from either view.

        arrangement=True reads a clip on the timeline instead of a Session
        slot — the pairing that lets a caller read the bar it is about to
        edit, edit it, and read it back without going through the Session
        clip it was stamped from. Note times are the clip's own beats in
        both views, so they can be handed straight back to
        add_notes_to_clip.
        """
        try:
            track, clip = self._resolve_clip_in_view(
                track_index, clip_index, arrangement=arrangement)
            if not getattr(clip, "is_midi_clip", False):
                raise Exception("Clip is not a MIDI clip")
            notes = self._notes_from_clip(clip)
            return {
                "track_index": track_index,
                "track_name": track.name,
                "clip_index": clip_index,
                "clip_name": clip.name,
                "arrangement": bool(arrangement),
                "length": float(clip.length),
                "note_count": len(notes),
                "notes": notes,
            }
        except Exception as e:
            self.log_message("Error getting clip notes: " + str(e))
            raise

    def _snapshot_track_selection(self, tracks):
        """Resolve a snapshot's `tracks` filter to a set of song-track indices.

        Accepts indices, names, or a mix of the two, and a bare selector as
        well as a list, because "just the DRUMS bus" is the common case and
        wrapping it is one more thing to get wrong. Names are matched the
        way _resolve_track's guard matches them — case-insensitively after
        stripping, since Live's names collect trailing spaces.

        A selector nothing matches is refused rather than skipped: a
        snapshot missing the track you asked about looks exactly like a
        track with nothing on it, and that is the mistake this whole
        command exists to stop a caller making. Returns None for "no
        filter", or a sorted list of indices into song.tracks.
        """
        if tracks is None:
            return None
        selectors = list(tracks) if isinstance(tracks, (list, tuple)) else [tracks]
        if not selectors:
            raise ValueError(
                "tracks was an empty list, which would return a snapshot with "
                "no tracks in it; omit tracks entirely to snapshot all of them.")

        song_tracks = self._song.tracks
        by_name = {}
        for i, track in enumerate(song_tracks):
            key = str(track.name).strip().lower()
            # Duplicate names are legal in Live; the lower index is the one a
            # caller counting from the top of the session means.
            if key not in by_name:
                by_name[key] = i

        chosen = []
        missing = []
        for selector in selectors:
            index = None
            # bool is an int subclass, so True would silently select track 1.
            if isinstance(selector, bool):
                index = None
            elif isinstance(selector, int):
                if 0 <= selector < len(song_tracks):
                    index = selector
            else:
                text = str(selector).strip()
                index = by_name.get(text.lower())
                if index is None:
                    # JSON callers stringify numbers more often than they mean
                    # to. A name always wins, so this only fires when nothing
                    # in the set is actually called "3".
                    try:
                        candidate = int(text)
                    except (TypeError, ValueError):
                        candidate = None
                    if candidate is not None and 0 <= candidate < len(song_tracks):
                        index = candidate
            if index is None:
                missing.append(selector)
            elif index not in chosen:
                chosen.append(index)

        if missing:
            available = ", ".join("%d '%s'" % (i, track.name)
                                  for i, track in enumerate(song_tracks))
            raise ValueError(
                "No track matches %s; nothing was returned rather than a "
                "snapshot that silently leaves it out. Track indices renumber "
                "whenever a track is created or deleted. Tracks are: %s"
                % (", ".join(repr(m) for m in missing), available))
        return sorted(chosen)

    def _get_session_snapshot(self, include_notes=True, include_params=True,
                              include_warp_markers=False,
                              include_rack_chains=False,
                              include_empty_slots=False,
                              tracks=None):
        """Project state dump (schema v3), scoped by the caller.

        A full dump of a real arrangement does not fit in an MCP response:
        21 of 24 calls in one production session blew the output cap even
        with include_notes and include_params both off, and each failure
        cost a round of shell calls to dump and parse the payload by hand.
        Everything the caller actually wanted was a handful of tracks.

        So v3 defaults to the lean view and lets the caller buy detail back:

          include_notes         every MIDI note in every clip
          include_params        every parameter of every device
          include_warp_markers  every warp marker pair (73-81% of v2 bytes)
          include_rack_chains   a rack's nested chains (~9.5 KB per drum rack)
          include_empty_slots   clip slots with nothing in them
          tracks                indices and/or names to restrict the dump to

        The counts that make omissions visible are kept: warp_marker_count,
        chain_count, clip_slot_count, and session.track_count against the
        `tracks_selected` echo. Every track entry keeps its real song index,
        so an index read out of a filtered snapshot still addresses the same
        track in every other command.

        Filtering applies to song tracks only. The returns and the master are
        the mix context a caller reads a track against and are small under
        the default flags, so they are always present.
        """
        try:
            selected = self._snapshot_track_selection(tracks)
            session = self._get_session_info()
            tracks_out = []
            for track_index, track in enumerate(self._song.tracks):
                if selected is not None and track_index not in selected:
                    continue

                clip_slots = []
                for slot_index, slot in enumerate(track.clip_slots):
                    has_clip = bool(slot.has_clip)
                    # A Live set has as many slots per track as it has scenes,
                    # and most of them are empty forever. Dropping them costs
                    # nothing readable: each surviving entry carries its own
                    # index and the track carries the total.
                    if not has_clip and not include_empty_slots:
                        continue
                    clip_info = None
                    if has_clip:
                        clip_info = self._serialize_session_clip(
                            slot.clip, include_notes=include_notes,
                            include_warp_markers=include_warp_markers
                        )
                    clip_slots.append({
                        "index": slot_index,
                        "has_clip": has_clip,
                        "clip": clip_info,
                    })

                devices = []
                for device_index, device in enumerate(track.devices):
                    devices.append(
                        self._serialize_device(
                            device, device_index, include_params=include_params,
                            include_rack_chains=include_rack_chains
                        )
                    )

                arrangement_clips = []
                try:
                    for clip in track.arrangement_clips:
                        arrangement_clips.append(
                            self._serialize_arrangement_clip(
                                clip, include_notes=include_notes,
                                include_warp_markers=include_warp_markers
                            )
                        )
                except Exception as e:
                    self.log_message(
                        "arrangement_clips unavailable on track %d: %s"
                        % (track_index, str(e))
                    )

                tracks_out.append({
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
                    # Total slots, so an omitted empty slot is never mistaken
                    # for a slot index that is out of range.
                    "clip_slot_count": len(track.clip_slots),
                    "clip_slots": clip_slots,
                    "devices": devices,
                    "arrangement_clips": arrangement_clips,
                })

            result = {
                "schema": "ableton_mcp_snapshot_v3",
                "session": session,
                "tracks": tracks_out,
                "scenes": self._serialize_scenes(),
                "return_tracks": self._serialize_return_tracks(
                    include_params=include_params,
                    include_rack_chains=include_rack_chains
                ),
                "master_track": self._serialize_master_track(
                    include_params=include_params,
                    include_rack_chains=include_rack_chains
                ),
                "cue_points": self._serialize_cue_points(),
                "include_notes": bool(include_notes),
                "include_params": bool(include_params),
                "include_warp_markers": bool(include_warp_markers),
                "include_rack_chains": bool(include_rack_chains),
                "include_empty_slots": bool(include_empty_slots),
            }
            # Present only when a filter was applied, so its absence is an
            # unambiguous "this is every track" and its presence names
            # exactly which ones the caller is looking at.
            if selected is not None:
                result["tracks_selected"] = list(selected)
            return result
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
