"""In-process stand-in for Ableton's ``_Framework`` package.

The Remote Script's only Live-bound import is
``from _Framework.ControlSurface import ControlSurface`` (docs/REFACTOR_PLAN.md
section 5 Level 3, Appendix B "ControlSurface stub contract"). Installing this
stub into ``sys.modules`` BEFORE the canonical script is imported lets the real
``AbletonMCP_Remote_Script/__init__.py`` execute anywhere — no Live, no
network.

The stub base class implements exactly the contract Appendix B lists:
``__init__(c_instance)``, ``disconnect()``, ``song()``, ``application()``,
``log_message(str)``, ``show_message(str)``, ``schedule_message(delay, task)``.
The harness never actually uses these class methods (it attaches instance
attributes that shadow them), but the class must exist for the script's
``class AbletonMCP(ControlSurface)`` statement and its
``ControlSurface.__init__`` / ``ControlSurface.disconnect`` calls.
"""

import sys
import types


def install_framework_stub():
    """Install ``_Framework`` and ``_Framework.ControlSurface`` into
    ``sys.modules``. Idempotent: repeated calls reuse the already-installed
    stub so every load of the Remote Script shares one ControlSurface base.

    Returns the ``_Framework`` package module.
    """
    existing = sys.modules.get("_Framework.ControlSurface")
    if existing is not None and hasattr(existing, "ControlSurface"):
        return sys.modules["_Framework"]

    package = types.ModuleType("_Framework")
    package.__path__ = []  # mark as a package so submodule import resolves

    submodule = types.ModuleType("_Framework.ControlSurface")

    class ControlSurface(object):
        """Minimal base matching what the Remote Script calls on itself."""

        def __init__(self, c_instance):
            self._c_instance = c_instance

        def disconnect(self):
            pass

        def song(self):
            return None

        def application(self):
            return None

        def log_message(self, message):
            pass

        def show_message(self, message):
            pass

        def schedule_message(self, delay, task):
            # Live schedules onto the main thread; outside Live the nearest
            # honest behavior is to run the task immediately. The harness
            # normally shadows this with its own recording version.
            task()

    submodule.ControlSurface = ControlSurface
    package.ControlSurface = submodule

    sys.modules["_Framework"] = package
    sys.modules["_Framework.ControlSurface"] = submodule
    return package
