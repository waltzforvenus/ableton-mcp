"""Model: what the server can learn from (and, later, do to) Live's macOS UI.

Some things Live's Python API simply does not offer — the window title
(and with it the set's name and its unsaved marker), whether a modal dialog
is blocking the main thread, how long since the user last touched the
machine. macOS answers those through System Events and IOKit, so this
module asks them, in subprocesses: ``osascript -l JavaScript`` (JXA) and
``ioreg``. No socket, no network, nothing leaves the machine — the
guardrail tests' import checks still hold.

Everything goes through one injectable ``runner`` so tests replay recorded
outputs and never touch a real UI. Reads here are passive (titles, roles,
an idle counter); nothing in this module clicks, types or focuses.

Off macOS, or without the Accessibility permission, every read returns
``None`` — "unknown", which callers must treat as unknown, never as "fine".
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# (returncode, stdout, stderr)
RunResult = Tuple[int, str, str]
Runner = Callable[[Sequence[str], float], RunResult]

# Live's process name on macOS ("Live"); matched as a prefix so a renamed
# bundle ("Live 12 Suite") still resolves, as the shared lock helper does.
LIVE_PROCESS_PREFIX = "Live"

# AX subroles that mean a window blocks Live's main thread until dismissed.
MODAL_SUBROLES = ("AXDialog", "AXSystemDialog")

# Window titles Live uses for its own modal windows, as a second signal
# where the subrole is a plain AXStandardWindow (UNVERIFIED on 12.4.7).
MODAL_TITLE_PREFIXES = ("Export Audio/Video", "Save", "Live Set Recovery",
                        "Preferences", "Settings")

_JXA_WINDOWS = """
const se = Application("System Events");
const ps = se.processes.whose({name: {_beginsWith: %s}});
if (ps.length === 0) { JSON.stringify(null); } else {
  const p = ps[0];
  const out = [];
  const ws = p.windows();
  for (let i = 0; i < ws.length; i++) {
    let t = null, sr = null;
    try { t = ws[i].name(); } catch (e) {}
    try { sr = ws[i].subrole(); } catch (e) {}
    out.push({title: t, subrole: sr});
  }
  JSON.stringify({frontmost: p.frontmost(), windows: out});
}
"""


def _default_runner(args: Sequence[str], timeout: float) -> RunResult:
    try:
        proc = subprocess.run(list(args), capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)
    return proc.returncode, proc.stdout, proc.stderr


def parse_main_title(title: str) -> Dict[str, Any]:
    """Split Live's main-window title into the set name and unsaved flag.

    Live titles its window ``<Set>[*] [<Project>] - Ableton Live <edition>``
    (``Untitled`` before a first save); the ``*`` marks unsaved changes.
    The exact shape is UNVERIFIED on 12.4.7, so unknown shapes return the
    raw title with ``set_name`` None rather than a guess."""
    m = re.match(r"^(?P<set>.*?)(?P<star>\*)?(?:\s+\[(?P<project>[^\]]*)\])?"
                 r"\s+-\s+Ableton Live\b", title or "")
    if not m:
        return {"title": title, "set_name": None, "project": None,
                "unsaved": ("*" in (title or "")) or None}
    return {"title": title, "set_name": m.group("set").strip() or None,
            "project": m.group("project"), "unsaved": bool(m.group("star"))}


class MacUI:
    """Passive macOS reads about Live. ``available`` is False off darwin."""

    def __init__(self, runner: Optional[Runner] = None,
                 platform: str = sys.platform) -> None:
        self._run = runner or _default_runner
        self.available = platform == "darwin"

    def _jxa(self, source: str, timeout: float = 15.0) -> Optional[Any]:
        if not self.available:
            return None
        code, out, _err = self._run(["osascript", "-l", "JavaScript", "-e", source],
                                    timeout)
        if code != 0:
            return None
        try:
            return json.loads(out.strip() or "null")
        except ValueError:
            return None

    def live_windows(self) -> Optional[Dict[str, Any]]:
        """``{"frontmost": bool, "windows": [{"title", "subrole"}]}``, or
        None when Live is not running or the windows cannot be read (no
        Accessibility permission for the server's host process)."""
        data = self._jxa(_JXA_WINDOWS % json.dumps(LIVE_PROCESS_PREFIX))
        if not isinstance(data, dict) or not isinstance(data.get("windows"), list):
            return None
        return data

    def window_titles(self) -> Optional[List[str]]:
        data = self.live_windows()
        if data is None:
            return None
        return [w.get("title") or "" for w in data["windows"]]

    def user_idle_s(self) -> Optional[float]:
        """Seconds since the last keyboard/mouse event (IOHIDSystem's
        HIDIdleTime, in nanoseconds), or None if it cannot be read."""
        if not self.available:
            return None
        code, out, _err = self._run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], 10.0)
        if code != 0:
            return None
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
        return round(int(m.group(1)) / 1e9, 1) if m else None


def summarize_windows(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The status view of ``MacUI.live_windows()``: the main window parsed,
    any modal dialog named, and every title (plugin windows included)."""
    if data is None:
        return {"readable": False, "set_name": None, "unsaved": None,
                "modal_dialog": None, "frontmost": None, "titles": None}
    windows = data.get("windows") or []
    titles = [w.get("title") or "" for w in windows]
    main = next((parse_main_title(t) for t in titles if "Ableton Live" in t), None)
    modal = None
    for w in windows:
        title = w.get("title") or ""
        if (w.get("subrole") in MODAL_SUBROLES
                or any(title.startswith(p) for p in MODAL_TITLE_PREFIXES)):
            modal = title or w.get("subrole")
            break
    return {
        "readable": True,
        "set_name": main["set_name"] if main else None,
        "project": main["project"] if main else None,
        "unsaved": main["unsaved"] if main else None,
        "modal_dialog": modal,
        "frontmost": data.get("frontmost"),
        "titles": titles,
    }
