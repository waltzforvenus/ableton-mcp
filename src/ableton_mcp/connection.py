"""Transport to the Ableton Remote Script socket (docs/REFACTOR_PLAN.md §3.5).

The ONLY module in the package that imports ``socket`` — enforced by the
no-egress guardrail test's import allowlist. The server's one outbound
connection is TCP to the local Ableton socket (CLAUDE.md hard rule 1).

``AbletonConnection`` is the request/response exchange moved out of the old
``server.py`` (its embedded modifying-command list and timeout dict replaced
by lookups into the commands.py registry). ``AbletonClient`` absorbs the
module-global ``get_ableton_connection()`` machinery — lazy connect, the
MSG_PEEK liveness probe, the 3-attempt retry — as instance state, plus the
three §3.5 hardenings (reconnect under the send lock, close-on-timeout,
handshake invalidation via ``on_reconnect``).
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .commands import COMMANDS

logger = logging.getLogger("AbletonMCPServer")


@dataclass
class AbletonConnection:
    host: str
    port: int
    sock: socket.socket = None

    # The receive-side socket timeout. This was a hardcoded 15.0 inside
    # receive_full_response before the transport moved; it is a field so the
    # Level-2 transport tests can use a sub-second budget instead of stalling
    # the suite for 15 s per timeout case. Production always uses the default.
    receive_timeout: float = 15.0

    # One socket, several threads: tool calls run on the MCP worker while the
    # passive poller drains events on its own thread. Serialise so their
    # sendall/recv pairs cannot interleave.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def connect(self) -> bool:
        """Connect to the Ableton Remote Script socket server"""
        if self.sock:
            return True

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((self.host, self.port))
            self.sock.settimeout(None)
            logger.info(f"Connected to Ableton at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Ableton at {self.host}:{self.port}: {str(e)}")
            self.sock = None
            return False

    def disconnect(self):
        """Disconnect from the Ableton Remote Script"""
        with self._lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception as e:
                    logger.error(f"Error disconnecting from Ableton: {str(e)}")
                finally:
                    self.sock = None

    def _drop_socket(self):
        """Close and discard the socket after a failed exchange.

        The pre-refactor code only set ``self.sock = None`` on failure, which
        leaked the descriptor and left the OS socket half-open. Actually
        closing matters for correctness, not just hygiene: the wire protocol
        has no request ids, so a late reply arriving on a socket that saw a
        timed-out command would be read as the answer to the NEXT command.
        Every failure path in ``_send_command_locked`` funnels through here so
        no socket that saw a half-finished exchange can ever be read again.
        """
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def receive_full_response(self, sock, buffer_size=8192):
        """Receive the complete response, potentially in multiple chunks"""
        chunks = []
        sock.settimeout(self.receive_timeout)

        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        if not chunks:
                            raise Exception("Connection closed before receiving any data")
                        break

                    chunks.append(chunk)

                    # Check if we've received a complete JSON object
                    try:
                        data = b''.join(chunks)
                        json.loads(data.decode('utf-8'))
                        logger.info(f"Received complete response ({len(data)} bytes)")
                        return data
                    except json.JSONDecodeError:
                        # Incomplete JSON, continue receiving
                        continue
                except socket.timeout:
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Socket connection error during receive: {str(e)}")
                    raise
        except Exception as e:
            logger.error(f"Error during receive: {str(e)}")
            raise

        # If we get here, we either timed out or broke out of the loop
        if chunks:
            data = b''.join(chunks)
            logger.info(f"Returning data after receive completion ({len(data)} bytes)")
            try:
                json.loads(data.decode('utf-8'))
                return data
            except json.JSONDecodeError:
                raise Exception("Incomplete JSON response received")
        else:
            raise Exception("No data received")

    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a command to Ableton and return the response.

        Serialised on ``self._lock``: the request and its response are one
        indivisible exchange on a shared socket. Holding the lock across the
        whole round-trip (including reconnect) is what keeps a concurrent
        caller from reading someone else's reply.
        """
        with self._lock:
            return self._send_command_locked(command_type, params)

    def _send_command_locked(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Ableton")

        command = {
            "type": command_type,
            "params": params or {}
        }

        # Timeout policy comes from the commands.py registry — the single
        # source of truth for which commands modify Live's state (and so run
        # on its main thread, needing the longer budget) and which need an
        # explicit override. An unknown command gets the short read-only
        # default, exactly as an unlisted command did before the registry.
        spec = COMMANDS.get(command_type)

        try:
            logger.info(f"Sending command: {command_type} with params: {params}")

            # Send the command
            self.sock.sendall(json.dumps(command).encode('utf-8'))
            logger.info(f"Command sent, waiting for response...")

            # Set timeout based on command type
            if spec is not None and spec.timeout is not None:
                timeout = spec.timeout
            else:
                timeout = 15.0 if spec is not None and spec.modifying else 10.0
            self.sock.settimeout(timeout)

            # Receive the response
            response_data = self.receive_full_response(self.sock)
            logger.info(f"Received {len(response_data)} bytes of data")

            # Parse the response
            response = json.loads(response_data.decode('utf-8'))
            logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")

            if response.get("status") == "error":
                logger.error(f"Ableton error: {response.get('message')}")
                raise Exception(response.get("message", "Unknown error from Ableton"))

            return response.get("result", {})
        except socket.timeout:
            logger.error("Socket timeout while waiting for response from Ableton")
            self._drop_socket()
            raise Exception("Timeout waiting for Ableton response")
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"Socket connection error: {str(e)}")
            self._drop_socket()
            raise Exception(f"Connection to Ableton lost: {str(e)}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from Ableton: {str(e)}")
            if 'response_data' in locals() and response_data:
                logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
            self._drop_socket()
            raise Exception(f"Invalid response from Ableton: {str(e)}")
        except Exception as e:
            logger.error(f"Error communicating with Ableton: {str(e)}")
            self._drop_socket()
            raise Exception(f"Communication error with Ableton: {str(e)}")


class AbletonClient:
    """The connection lifecycle as instance state (docs/REFACTOR_PLAN.md §3.5).

    Absorbs everything the module-global ``get_ableton_connection()`` used to
    do — lazy connect, the MSG_PEEK liveness probe, the 3-attempt retry — and
    satisfies the ``AbletonClientProtocol`` seam: callers only ever use
    ``send_command``.
    """

    def __init__(self, host: str, port: int, *,
                 on_reconnect: Optional[Callable[[], None]] = None,
                 receive_timeout: float = 15.0) -> None:
        self._host = host
        self._port = port
        # Called whenever a NEW socket is established after a previous one
        # existed. The composition root wires this to
        # ScriptHandshake.invalidate: a new socket can mean Live was
        # restarted with a different Remote Script, so capability/version
        # answers learned over the old socket must be re-earned, not
        # remembered for the life of the process.
        self._on_reconnect = on_reconnect
        self._receive_timeout = receive_timeout
        # One RLock serialises the WHOLE exchange: liveness check, reconnect,
        # send, receive. FastMCP runs sync tools in worker threads, and the
        # old module-global version ran its check-then-replace connection
        # swap outside any lock — two threads could race into duplicate
        # sockets, or read each other's replies.
        self._lock = threading.RLock()
        self._connection: Optional[AbletonConnection] = None
        self._ever_connected = False

    @property
    def connected(self) -> bool:
        """True while a connection object exists (its socket may still be
        re-established lazily). Mirrors the old module-global
        ``_ableton_connection is not None`` check the lifespan used."""
        with self._lock:
            return self._connection is not None

    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a command to Ableton and return the response's result dict."""
        with self._lock:
            connection = self._ensure_connected_locked()
            return connection.send_command(command_type, params)

    def ensure_connected(self) -> None:
        """Establish (or verify) the connection without sending anything.

        Raises the same "Could not connect to Ableton" error a failing
        ``send_command`` would.
        """
        with self._lock:
            self._ensure_connected_locked()

    def close(self) -> None:
        """Disconnect and forget the current connection, if any."""
        with self._lock:
            if self._connection is not None:
                self._connection.disconnect()
                self._connection = None

    def _ensure_connected_locked(self) -> AbletonConnection:
        connection = self._connection
        if connection is not None and connection.sock is not None:
            try:
                # Check if the socket is still alive by peeking for data.
                # MSG_PEEK + MSG_DONTWAIT will raise BlockingIOError if alive
                # but no data, or return b'' if the remote end has closed the
                # connection.
                connection.sock.setblocking(False)
                try:
                    data = connection.sock.recv(1, socket.MSG_PEEK)
                    if data == b'':
                        raise ConnectionError("Remote end closed")
                except BlockingIOError:
                    pass  # Socket is alive, just no data waiting — this is normal
                finally:
                    connection.sock.setblocking(True)
                return connection
            except Exception as e:
                logger.warning(f"Existing connection is no longer valid: {str(e)}")
                try:
                    connection.disconnect()
                except Exception:
                    pass
                self._connection = None
        elif connection is not None:
            # The socket was closed and discarded after a failed exchange
            # (see AbletonConnection._drop_socket). Drop the husk so the
            # retry loop below builds a fresh connection — and so
            # on_reconnect fires for the replacement socket.
            self._connection = None

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Connecting to Ableton at {self._host}:{self._port} (attempt {attempt}/{max_attempts})...")
                connection = AbletonConnection(host=self._host, port=self._port,
                                               receive_timeout=self._receive_timeout)
                if connection.connect():
                    logger.info("Created new persistent connection to Ableton")
                    self._connection = connection
                    if self._ever_connected and self._on_reconnect is not None:
                        self._on_reconnect()
                    self._ever_connected = True
                    return connection
            except Exception as e:
                logger.error(f"Connection attempt {attempt} failed: {str(e)}")

            if attempt < max_attempts:
                time.sleep(1.0)

        # If we get here, all connection attempts failed
        logger.error("Failed to connect to Ableton after multiple attempts")
        raise Exception("Could not connect to Ableton. Make sure the Remote Script is running.")
