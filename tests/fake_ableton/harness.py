"""Harness that runs the REAL canonical Remote Script against the fake LOM.

docs/REFACTOR_PLAN.md section 5 Level 3: install the ``_Framework`` stub,
import ``remote_script/__init__.py`` itself (never a copy), build
``AbletonMCP`` without running ``__init__`` — so no socket is bound and no
thread is started — then wire a :class:`~fake_ableton.lom.FakeSong`, captured
``log_message``/``show_message``, an inline ``schedule_message``, and a
``FakeApplication`` onto the instance. Tests drive the real
``_process_command`` dispatch through :meth:`RemoteScriptHarness.process`.
"""

import importlib.util
import itertools
import sys
from pathlib import Path

from .framework_stub import install_framework_stub
from .lom import FakeApplication, FakeLiveConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
REMOTE_SCRIPT_PATH = REPO_ROOT / "remote_script" / "__init__.py"

_LOAD_COUNTER = itertools.count(1)


def load_remote_script():
    """Import the canonical Remote Script under a unique module name.

    The stub ``_Framework`` package is installed first, so the script's one
    Live-bound import resolves. Each call executes a fresh module, so no
    state leaks between harnesses.
    """
    install_framework_stub()
    module_name = "_ableton_mcp_remote_script_under_test_%d" % next(_LOAD_COUNTER)
    spec = importlib.util.spec_from_file_location(module_name,
                                                 str(REMOTE_SCRIPT_PATH))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


class RemoteScriptHarness(object):
    """The real ``AbletonMCP`` control surface wired to fake Live objects.

    Attributes tests use:

    - ``song`` — the FakeSong the script operates on
    - ``app`` — the FakeApplication returned by ``self.application()``
    - ``module`` — the freshly imported Remote Script module
      (``SCRIPT_VERSION``, ``SCRIPT_CAPABILITIES``, ...)
    - ``logs`` / ``messages`` — captured ``log_message`` / ``show_message``
    - ``scheduled`` — every ``(delay, task)`` handed to ``schedule_message``
    """

    def __init__(self, song=None, application=None, config=None,
                 schedule_raises=False):
        self.config = config if config is not None else FakeLiveConfig()
        if song is None:
            # Late import: the package __init__ imports this module, so the
            # builder cannot be imported at module top level.
            from fake_ableton import make_standard_song
            song = make_standard_song(self.config)
        self.song = song
        self.app = (application if application is not None
                    else FakeApplication(song=self.song, config=self.config))
        self.module = load_remote_script()
        self.logs = []
        self.messages = []
        self.scheduled = []

        # Build the control surface WITHOUT running __init__: the real
        # constructor binds the TCP socket and starts the server thread,
        # neither of which may happen in tests.
        mcp = object.__new__(self.module.AbletonMCP)
        mcp._song = self.song
        mcp.log_message = self.logs.append
        mcp.show_message = self.messages.append
        mcp.application = lambda: self.app

        if schedule_raises:
            # Live's schedule_message asserts when called from the main
            # thread; raising here exercises the script's direct-execution
            # fallback (except AssertionError: main_thread_task()).
            def schedule_message(delay, task):
                self.scheduled.append((delay, task))
                raise AssertionError("scheduling not allowed on the main thread")
        else:
            # Default: run main-thread tasks immediately and synchronously.
            def schedule_message(delay, task):
                self.scheduled.append((delay, task))
                task()

        mcp.schedule_message = schedule_message
        self.mcp = mcp

    def process(self, command):
        """Run one command dict through the real ``_process_command`` and
        return the response envelope ({"status": ..., ...})."""
        return self.mcp._process_command(command)
