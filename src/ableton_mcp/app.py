"""Composition root (docs/REFACTOR_PLAN.md §3.3).

The ONLY module that reads the environment, configures logging, or wires
objects together. Everything behind the ``Deps`` it yields is
constructor-injected; tests build a ``Deps`` full of fakes and hand tools a
two-line stub ctx instead of monkeypatching module globals (which no longer
exist).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Mapping, Optional, Protocol

from mcp.server.fastmcp import FastMCP

from . import tools
from .connection import AbletonClient
from .handshake import ScriptHandshake
from .services import AbletonService

logger = logging.getLogger("AbletonMCPServer")


@dataclass(frozen=True)
class Settings:
    """Process configuration, read from the environment exactly once."""

    host: str = "localhost"
    port: int = 9877

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        return cls(
            host=env.get("ABLETON_HOST", "localhost"),
            port=int(env.get("ABLETON_PORT", "9877")),
        )


class AbletonClientProtocol(Protocol):
    """The one Protocol in the package — the seam that carries the test
    suite (docs/REFACTOR_PLAN.md §3.2): anything with ``send_command`` can
    stand in for the real transport."""

    def send_command(self, command_type: str,
                     params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]: ...


@dataclass
class Deps:
    """What the lifespan yields into ``ctx.request_context.lifespan_context``;
    what tests fake."""

    client: AbletonClientProtocol
    handshake: ScriptHandshake
    service: AbletonService


def build_deps(settings: Settings) -> Deps:
    """The one wiring function: every production dependency is constructed
    here and nowhere else. The client invalidates the handshake cache
    whenever it establishes a replacement socket (§3.5 hardening 3)."""
    handshake = ScriptHandshake()
    client = AbletonClient(settings.host, settings.port,
                           on_reconnect=handshake.invalidate)
    service = AbletonService(client, handshake)
    return Deps(client=client, handshake=handshake, service=service)


def build_app(settings: Optional[Settings] = None,
              deps: Optional[Deps] = None) -> FastMCP:
    """Assemble the FastMCP application.

    ``deps`` injects a pre-built dependency set (tests hand in fakes);
    otherwise the lifespan builds the real one from ``settings`` (or the
    environment) when the server actually starts — importing and building
    the app still has zero side effects.
    """
    injected = deps

    @asynccontextmanager
    async def server_lifespan(_app: FastMCP) -> AsyncIterator[Deps]:
        """Manage server startup and shutdown lifecycle"""
        d = injected if injected is not None else build_deps(
            settings if settings is not None else Settings.from_env())
        try:
            logger.info("AbletonMCP server starting up")

            try:
                # Injected test doubles may implement only the Protocol
                # (send_command); the pre-flight connect is best-effort.
                if hasattr(d.client, "ensure_connected"):
                    d.client.ensure_connected()
                logger.info("Successfully connected to Ableton on startup")
                d.handshake.perform(d.client.send_command)
            except Exception as e:
                logger.warning(f"Could not connect to Ableton on startup: {str(e)}")
                logger.warning("Make sure the Ableton Remote Script is running")

            yield d
        finally:
            # `connected` mirrors the old `_ableton_connection is not None`
            # check; bare-Protocol fakes report False via the getattr default
            # and are simply left alone.
            if getattr(d.client, "connected", False):
                logger.info("Disconnecting from Ableton on shutdown")
                d.client.close()
            logger.info("AbletonMCP server shut down")

    app = FastMCP("AbletonMCP", lifespan=server_lifespan)
    for fn in tools.TOOLS:
        app.add_tool(fn)
    return app


def main() -> None:
    """Run the MCP server"""
    # Logging is configured here, in the process entry point — importing the
    # package must have no side effects.
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    build_app().run()
