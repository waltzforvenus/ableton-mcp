"""Model: what the server can learn from (and, later, do to) Live's macOS UI.

Some things Live's Python API simply does not offer — the window title
(and with it the set's name and its unsaved marker), whether a modal dialog
is blocking the main thread, how long since the user last touched the
machine. macOS answers those through System Events and IOKit, so this
module asks them, in subprocesses: ``osascript -l JavaScript`` (JXA) and
``ioreg``. No socket, no network, nothing leaves the machine — the
guardrail tests' import checks still hold.

Everything goes through one injectable ``runner`` so tests replay recorded
outputs and never touch a real UI. The reads (titles, roles, an idle
counter, Spotlight lookups) are passive. The few ACTIONS — bring Live to
the front, click one named menu item, press Escape, give focus back, take
an evidence screenshot — live in clearly separate methods, and callers only
reach them behind an explicit ``allow_ui_automation`` (services.py); they
address menu items by title, never by position or screen coordinates.

Off macOS, or without the Accessibility permission, every read returns
``None`` — "unknown", which callers must treat as unknown, never as "fine".
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
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


_JXA_CLICK_MENU = """
const se = Application("System Events");
const ps = se.processes.whose({name: {_beginsWith: %(proc)s}});
if (ps.length === 0) { JSON.stringify({ok: false, error: "live_not_running"}); } else {
  const p = ps[0];
  const want = %(want)s, exclude = %(exclude)s, menuName = %(menu)s;
  let menu = null;
  try { menu = p.menuBars[0].menuBarItems.byName(menuName).menus[0]; } catch (e) {}
  if (menu === null) { JSON.stringify({ok: false, error: "menu_not_found", menu: menuName}); } else {
    const names = [];
    let target = null;
    const items = menu.menuItems();
    for (let i = 0; i < items.length; i++) {
      let n = null;
      try { n = items[i].name(); } catch (e) {}
      if (n === null || n === "") continue;
      names.push(n);
      // Normalise the ellipsis: Live's titles end in U+2026, scripts type "...".
      const norm = n.replace(/\u2026/g, "...");
      if (target === null && norm.indexOf(want) === 0
          && !exclude.some(function (x) { return norm.indexOf(x) === 0; })) {
        target = items[i];
      }
    }
    if (target === null) {
      JSON.stringify({ok: false, error: "item_not_found", items: names});
    } else if (!target.enabled()) {
      JSON.stringify({ok: false, error: "item_disabled", item: target.name()});
    } else {
      p.frontmost = true;
      delay(0.3);
      target.click();
      JSON.stringify({ok: true, clicked: target.name()});
    }
  }
}
"""

_JXA_FRONTMOST = """
const se = Application("System Events");
const f = se.processes.whose({frontmost: true});
JSON.stringify(f.length ? f[0].name() : null);
"""

_JXA_ACTIVATE = """
const se = Application("System Events");
const ps = se.processes.whose({name: %s});
if (ps.length) { ps[0].frontmost = true; JSON.stringify(true); } else { JSON.stringify(false); }
"""

_JXA_ESCAPE = """
const se = Application("System Events");
const ps = se.processes.whose({name: {_beginsWith: %s}});
if (ps.length && ps[0].frontmost()) { se.keyCode(53); JSON.stringify(true); }
else { JSON.stringify(false); }
"""


class MacUI:
    """macOS reads about Live, and the few title-addressed actions.
    ``available`` is False off darwin."""

    def __init__(self, runner: Optional[Runner] = None,
                 platform: str = sys.platform,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._run = runner or _default_runner
        self.available = platform == "darwin"
        self.sleep = sleep
        self.clock = clock

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


    # ── Spotlight (passive) ──────────────────────────────────────────────

    def find_set_paths(self, set_name: str, project: Optional[str] = None,
                       limit: int = 20) -> Optional[List[str]]:
        """Absolute paths of ``<set_name>.als`` files Spotlight knows, the
        ones inside a folder named ``project`` first. None if it cannot ask.

        Live's API has no Song.file_path, so the window title (set and
        project name) plus the local Spotlight index is how the server finds
        the file it must check the mtime of. Backup/ copies are excluded."""
        if not self.available or not set_name:
            return None
        query = 'kMDItemFSName == "%s.als"' % set_name.replace('"', '\\"')
        code, out, _err = self._run(["mdfind", query], 15.0)
        if code != 0:
            return None
        paths = [line.strip() for line in out.splitlines() if line.strip()]
        paths = [p for p in paths
                 if os.path.basename(os.path.dirname(p)) != "Backup"]
        if project:
            paths.sort(key=lambda p: os.path.basename(os.path.dirname(p)) != project)
        return paths[:limit]

    # ── actions (callers gate these behind allow_ui_automation) ──────────

    def frontmost_app(self) -> Optional[str]:
        data = self._jxa(_JXA_FRONTMOST)
        return data if isinstance(data, str) else None

    def activate(self, process_name: str) -> bool:
        return self._jxa(_JXA_ACTIVATE % json.dumps(process_name)) is True

    def click_menu_item(self, menu: str, prefix: str,
                        exclude: Sequence[str] = ()) -> Dict[str, Any]:
        """Bring Live to the front and click the first item of ``menu`` whose
        title starts with ``prefix`` (U+2026 compared as "...") and with none
        of ``exclude``. Refuses — clicking nothing — when the item is missing
        or disabled, reporting the titles that were there."""
        if not self.available:
            return {"ok": False, "error": "not_macos"}
        data = self._jxa(_JXA_CLICK_MENU % {
            "proc": json.dumps(LIVE_PROCESS_PREFIX), "menu": json.dumps(menu),
            "want": json.dumps(prefix), "exclude": json.dumps(list(exclude)),
        }, timeout=20.0)
        if not isinstance(data, dict):
            return {"ok": False, "error": "osascript_failed"}
        return data

    def press_escape(self) -> bool:
        """Escape, only while Live is frontmost (so it can never land in
        whatever the user switched to)."""
        return self._jxa(_JXA_ESCAPE % json.dumps(LIVE_PROCESS_PREFIX)) is True

    def screenshot(self, path: str) -> Optional[str]:
        """Full-screen evidence PNG at ``path`` (silent, no shutter sound)."""
        if not self.available:
            return None
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        code, _out, _err = self._run(["screencapture", "-x", "-t", "png", path], 15.0)
        return path if code == 0 and os.path.exists(path) else None

    def wait_for(self, predicate: Callable[[], Any], timeout_s: float,
                 interval_s: float = 0.5) -> Any:
        """Poll ``predicate`` until it returns something truthy or the time
        runs out; returns the last value either way."""
        deadline = self.clock() + timeout_s
        value = predicate()
        while not value and self.clock() < deadline:
            self.sleep(interval_s)
            value = predicate()
        return value


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
