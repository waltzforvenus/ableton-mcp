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
    let t = null, sr = null, doc = null;
    try { t = ws[i].name(); } catch (e) {}
    try { sr = ws[i].subrole(); } catch (e) {}
    // The open .als as a file:// URL; Live 12.4.7's title no longer names it.
    try { doc = ws[i].attributes.byName("AXDocument").value(); } catch (e) {}
    out.push({title: t, subrole: sr, document: doc});
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


_JXA_READ_WINDOW = """
const se = Application("System Events");
const ps = se.processes.whose({name: {_beginsWith: %(proc)s}});
if (ps.length === 0) { JSON.stringify({ok: false, error: "live_not_running"}); } else {
  const ws = ps[0].windows.whose({name: {_beginsWith: %(title)s}});
  if (ws.length === 0) { JSON.stringify({ok: false, error: "no_window"}); } else {
    const win = ws[0];
    const out = [];
    const els = win.entireContents();
    for (let i = 0; i < els.length; i++) {
      const e = els[i];
      const rec = {role: null, name: null, value: null, desc: null,
                   x: null, y: null, w: null, h: null, enabled: null};
      try { rec.role = e.role(); } catch (x) {}
      try { rec.name = e.name(); } catch (x) {}
      try { const v = e.value(); rec.value = (v === null || typeof v !== "object") ? v : String(v); } catch (x) {}
      try { rec.desc = e.description(); } catch (x) {}
      try { const p = e.position(); rec.x = p[0]; rec.y = p[1]; } catch (x) {}
      try { const z = e.size(); rec.w = z[0]; rec.h = z[1]; } catch (x) {}
      try { rec.enabled = e.enabled(); } catch (x) {}
      out.push(rec);
    }
    JSON.stringify({ok: true, title: win.name(), elements: out});
  }
}
"""

# Act on ONE element of a window, found again by role and position (within
# 3 px) — stable for as long as the dialog stays open, and never an index
# into a list whose order a Live update can change.
_JXA_ACT = """
const se = Application("System Events");
const ps = se.processes.whose({name: {_beginsWith: %(proc)s}});
const want = %(spec)s;
function find(win) {
  const els = win.entireContents();
  for (let i = 0; i < els.length; i++) {
    const e = els[i];
    let r = null, p = null;
    try { r = e.role(); } catch (x) { continue; }
    if (r !== want.role) continue;
    if (want.name !== undefined && want.name !== null) {
      let n = null; try { n = e.name(); } catch (x) {}
      if (n === want.name) return e;
      continue;
    }
    try { p = e.position(); } catch (x) { continue; }
    if (Math.abs(p[0] - want.x) <= 3 && Math.abs(p[1] - want.y) <= 3) return e;
  }
  return null;
}
if (ps.length === 0) { JSON.stringify({ok: false, error: "live_not_running"}); } else {
  const p = ps[0];
  const ws = p.windows.whose({name: {_beginsWith: want.window}});
  if (ws.length === 0) { JSON.stringify({ok: false, error: "no_window"}); } else {
    p.frontmost = true;
    const e = find(ws[0]);
    if (e === null) { JSON.stringify({ok: false, error: "element_not_found"}); } else {
      let result = {ok: true};
      if (want.action === "press") {
        e.actions.byName("AXPress").perform();
      } else if (want.action === "toggle") {
        // Checkboxes report 0/1; Live 12's Export toggles are AXButtons
        // reporting "On"/"Off" (Boolean("Off") is true, so normalize).
        const on = v => v === true || v === 1 ||
          ["1", "on", "true"].indexOf(String(v).toLowerCase()) >= 0;
        if (on(e.value()) !== Boolean(want.value)) e.click();
        delay(0.1); result.value = e.value();
      } else if (want.action === "step") {
        // Live 12's bar/beat/sixteenth boxes are AXSliders that ignore a
        // written value; step them with AXIncrement/AXDecrement instead.
        const target = Math.round(Number(want.value));
        for (let i = 0; i < 2000; i++) {
          const cur = Math.round(Number(e.value()));
          if (cur === target) break;
          e.actions.byName(cur < target ? "AXIncrement" : "AXDecrement").perform();
        }
        result.value = e.value();
        if (Math.round(Number(result.value)) !== target) result = {ok: false, error: "step_failed", value: result.value};
      } else if (want.action === "choose") {
        const wanted = String(want.value).toLowerCase();
        // Live marks the current option "✔ 44100"; compare without it.
        const bare = n => String(n).replace(/^\\u2714\\s*/, "").trim();
        if (bare(e.value()).toLowerCase() === wanted) {
          result.value = e.value();   // already set: no menu round-trip
        } else {
          e.click(); delay(0.4);
          // A Cocoa pop-up owns its menu; Live 12's opens as a separate
          // nameless window of AXMenuItems instead.
          let items = [];
          try { items = e.menus[0].menuItems(); } catch (x) { items = []; }
          if (items.length === 0) {
            const others = p.windows();
            for (let w = 0; w < others.length; w++) {
              let t = null; try { t = others[w].name(); } catch (x) {}
              if (t !== "" && t !== null) continue;
              const kids = others[w].entireContents();
              for (let k = 0; k < kids.length; k++) {
                let r = null; try { r = kids[k].role(); } catch (x) {}
                if (r === "AXMenuItem") items.push(kids[k]);
              }
            }
          }
          let hit = null; const names = [];
          for (let i = 0; i < items.length; i++) {
            let n = null; try { n = items[i].name(); } catch (x) {}
            if (n === null || n === "") continue;
            names.push(bare(n));
            if (hit === null && bare(n).toLowerCase() === wanted) hit = items[i];
          }
          if (hit === null) { se.keyCode(53); result = {ok: false, error: "option_not_found", options: names}; }
          else {
            try { hit.actions.byName("AXPress").perform(); } catch (x) { hit.click(); }
            delay(0.3); result.value = e.value();
          }
        }
      } else if (want.action === "text") {
        e.focused = true; e.value = String(want.value); delay(0.1);
        se.keyCode(48);  // Tab commits the field; Return could press Export
        delay(0.1); result.value = e.value();
      }
      JSON.stringify(result);
    }
  }
}
"""

# The native save panel Live opens after Export: name field, Go-to-folder,
# Save, and the Replace alert. Driven by role/title, never by coordinates.
_JXA_SAVE_PANEL = """
const se = Application("System Events");
const ps = se.processes.whose({name: {_beginsWith: %(proc)s}});
const want = %(spec)s;
// The element holding the panel's name field and buttons: the window or
// sheet itself, or (macOS 15+ standalone Save window) its splitter group.
function withFields(w) {
  try { if (w.textFields.length) return w; } catch (x) {}
  try { const g = w.splitterGroups[0]; if (g.textFields.length) return g; } catch (x) {}
  return null;
}
function findPanel(p) {
  const wins = p.windows();
  for (let i = 0; i < wins.length; i++) {
    try { if (wins[i].sheets.length) { const f = withFields(wins[i].sheets[0]); if (f) return f; } } catch (x) {}
  }
  for (let i = 0; i < wins.length; i++) {
    let sr = null; try { sr = wins[i].subrole(); } catch (x) {}
    let n = ""; try { n = wins[i].name() || ""; } catch (x) {}
    if (sr === "AXDialog" || n.indexOf("Save") === 0 || n.indexOf("Export") === 0) {
      const f = withFields(wins[i]); if (f) return f;
    }
  }
  return null;
}
// The name field: macOS 26.6 lists the search field FIRST in the Save
// window's splitter group (then the name field, then the tag editor), so
// textFields[0] is the search box - a name typed there searches the disk.
function nameField(c) {
  let tfs = []; try { tfs = c.textFields(); } catch (x) { return null; }
  for (const t of tfs) {
    let sr = null, d = ""; try { sr = t.subrole(); } catch (x) {}
    try { d = String(t.description() || ""); } catch (x) {}
    if (sr === "AXSearchField" || d.indexOf("search") >= 0 || d.indexOf("tag") >= 0) continue;
    return t;
  }
  return null;
}
function goToSheetOpen(p) {
  const wins = p.windows();
  for (let i = 0; i < wins.length; i++) {
    try { if (wins[i].sheets.length) return true; } catch (x) {}
  }
  return false;
}
function sheetOrWindow(p) {
  // The panel opens a moment after the button press that asked for it.
  for (let t = 0; t < 20; t++) { const f = findPanel(p); if (f) return f; delay(0.25); }
  return null;
}
if (ps.length === 0) { JSON.stringify({ok: false, error: "live_not_running"}); } else {
  const p = ps[0];
  p.frontmost = true;
  const panel = sheetOrWindow(p);
  if (panel === null) { JSON.stringify({ok: false, error: "no_save_panel"}); } else {
    if (want.action === "fill") {
      // Folder first: Go-to-folder resets the name field on macOS 26, so a
      // name typed before it is lost and Live saves "Untitled".
      // Focus the name field first: with the search field focused the
      // shortcut never opens Go-to-folder and the path is typed as a search.
      const first = nameField(panel);
      if (first === null) { JSON.stringify({ok: false, error: "no_name_field"}); } else {
      first.focused = true; delay(0.2);
      se.keystroke("g", {using: ["command down", "shift down"]}); delay(0.8);
      if (!goToSheetOpen(p)) { JSON.stringify({ok: false, error: "go_to_folder_did_not_open"}); } else {
      se.keystroke(want.directory); delay(0.3);
      se.keyCode(36); delay(0.8);           // Return in the Go-to-folder sheet only
      const name = nameField(sheetOrWindow(p));
      name.focused = true; delay(0.1);
      se.keystroke("a", {using: ["command down"]}); delay(0.1);
      se.keystroke(want.filename); delay(0.3);
      let shownName = null; try { shownName = nameField(sheetOrWindow(p)).value(); } catch (x) {}
      JSON.stringify({ok: true, filename: shownName}); } }
    } else if (want.action === "save") {
      const b = panel.buttons.whose({name: "Save"});
      if (!b.length) { JSON.stringify({ok: false, error: "no_save_button"}); }
      else { b[0].click(); delay(0.8);
        // A Replace alert is a sheet on the panel's window.
        let replace = null;
        const wins = p.windows();
        for (let i = 0; i < wins.length && replace === null; i++) {
          try { const sh = wins[i].sheets(); for (let j = 0; j < sh.length; j++) {
            const r = sh[j].buttons.whose({name: "Replace"}); if (r.length) replace = sh[j]; } } catch (x) {}
        }
        if (replace === null) { JSON.stringify({ok: true, replaced: false}); }
        else if (want.overwrite) { replace.buttons.whose({name: "Replace"})[0].click();
          JSON.stringify({ok: true, replaced: true}); }
        else { replace.buttons.whose({name: "Cancel"})[0].click(); delay(0.3);
          JSON.stringify({ok: false, error: "exists", replaced: false}); }
      }
    } else if (want.action === "cancel") {
      const b = panel.buttons.whose({name: "Cancel"});
      if (b.length) b[0].click();
      JSON.stringify({ok: true});
    }
  }
}
"""

# Roles that are a dialog field's editable control.
CONTROL_ROLES = ("AXPopUpButton", "AXCheckBox", "AXTextField", "AXComboBox",
                 "AXSlider", "AXIncrementor", "AXRadioButton", "AXButton")
# Roles whose row holds one control per part (bar / beat / sixteenth).
MULTIPART_ROLES = ("AXTextField", "AXSlider")


def map_dialog_fields(elements: List[Dict[str, Any]],
                      labels: Sequence[str]) -> Dict[str, Any]:
    """Pair each wanted label with the control(s) on its row.

    The 10-23 export runs broke on positional lookups ("slider 6 ...
    Invalid index"), so fields are found the way a person finds them: the
    static text, then the control to its right on the same row (centre
    within half a row height), nearest first. A checkbox titled with the
    label itself wins outright; a row with several text fields (bar / beat /
    sixteenth boxes, text fields or Live 12's sliders) returns all of them,
    left to right. A label Live suffixes ("Encode MP3 (CBR 320)") matches
    its prefix when no exact text exists. Missing labels are
    listed, so a renamed field refuses instead of setting the wrong one.
    """
    def centre(e):
        return (e.get("y") or 0) + (e.get("h") or 0) / 2.0

    texts = [e for e in elements if e.get("role") == "AXStaticText"]
    controls = [e for e in elements if e.get("role") in CONTROL_ROLES
                and e.get("x") is not None]
    found: Dict[str, Any] = {}
    missing: List[str] = []
    for label in labels:
        low = label.lower()
        titled = [c for c in controls
                  if str(c.get("name") or c.get("desc") or "").strip().lower() == low]
        if titled:
            found[label] = {"controls": [titled[0]], "label": None}
            continue
        def text_of(t):
            return str(t.get("value") or t.get("name") or "").strip().rstrip(":").lower()
        hits = [t for t in texts if text_of(t) == low]
        if not hits:
            # Live 12.4.7 suffixes some labels: "Encode MP3 (CBR 320)".
            hits = [t for t in texts if text_of(t).startswith(low + " (")]
        if not hits:
            missing.append(label)
            continue
        t = hits[0]
        tol = max(8.0, (t.get("h") or 16) / 2.0 + 2)
        row = [c for c in controls
               if abs(centre(c) - centre(t)) <= tol and (c.get("x") or 0) >= (t.get("x") or 0)]
        if not row:
            missing.append(label)
            continue
        row.sort(key=lambda c: c.get("x") or 0)
        if row[0].get("role") in MULTIPART_ROLES:
            role = row[0].get("role")
            row = [c for c in row if c.get("role") == role]
        else:
            row = row[:1]
        found[label] = {"controls": row, "label": t}
    return {"fields": found, "missing": missing}


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

    def read_window(self, title_prefix: str) -> Dict[str, Any]:
        """Every AX element of Live's window titled ``title_prefix…``:
        role, name, value, description, position and size."""
        if not self.available:
            return {"ok": False, "error": "not_macos"}
        data = self._jxa(_JXA_READ_WINDOW % {
            "proc": json.dumps(LIVE_PROCESS_PREFIX), "title": json.dumps(title_prefix)},
            timeout=30.0)
        return data if isinstance(data, dict) else {"ok": False, "error": "osascript_failed"}

    def act(self, window: str, element: Dict[str, Any], action: str,
            value: Any = None) -> Dict[str, Any]:
        """``press`` / ``toggle`` / ``choose`` (a pop-up's item by name) /
        ``text`` on the element of ``window`` with that role at that
        position (or that name, for buttons)."""
        if not self.available:
            return {"ok": False, "error": "not_macos"}
        spec = {"window": window, "role": element.get("role"),
                "x": element.get("x"), "y": element.get("y"),
                "name": element.get("match_name"), "action": action, "value": value}
        data = self._jxa(_JXA_ACT % {"proc": json.dumps(LIVE_PROCESS_PREFIX),
                                     "spec": json.dumps(spec)}, timeout=20.0)
        return data if isinstance(data, dict) else {"ok": False, "error": "osascript_failed"}

    def save_panel(self, action: str, directory: Optional[str] = None,
                   filename: Optional[str] = None,
                   overwrite: bool = False) -> Dict[str, Any]:
        """``fill`` (name + Go-to-folder), ``save`` (then answer a Replace
        alert per ``overwrite``) or ``cancel`` on the native save panel."""
        if not self.available:
            return {"ok": False, "error": "not_macos"}
        spec = {"action": action, "directory": directory, "filename": filename,
                "overwrite": bool(overwrite)}
        data = self._jxa(_JXA_SAVE_PANEL % {"proc": json.dumps(LIVE_PROCESS_PREFIX),
                                            "spec": json.dumps(spec)}, timeout=30.0)
        return data if isinstance(data, dict) else {"ok": False, "error": "osascript_failed"}

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


    def modal_alerts(self) -> Optional[List[Dict[str, Any]]]:
        """Live's modal alert windows: title, subrole, joined text and the
        buttons they offer. None when Live (or the permission) is missing -
        unknown, not 'no alerts'."""
        if not self.available:
            return None
        data = self._jxa(_JXA_MODAL_ALERTS % json.dumps(LIVE_PROCESS_PREFIX))
        return data if isinstance(data, list) else None

    def dismiss_modal_alert(self, button: str = "OK") -> Dict[str, Any]:
        """Press `button` (matched against name or description,
        case-insensitively) on the first modal alert that has it, and
        return {ok, text} - `text` being the alert's own words, which the
        caller folds into the error that usually follows. An alert this
        server's own command opened is the only thing this ever dismisses;
        no other window is touched."""
        if not self.available:
            return {"ok": False, "error": "not_macos"}
        data = self._jxa(_JXA_DISMISS_ALERT % (
            json.dumps(LIVE_PROCESS_PREFIX), json.dumps(button.lower())))
        return data if isinstance(data, dict) else {"ok": False,
                                                    "error": "osascript_failed"}

_JXA_MODAL_ALERTS = """
const se = Application("System Events");
const ps = se.processes.whose({name: {_beginsWith: %s}});
if (ps.length === 0) { JSON.stringify([]); } else {
  const p = ps[0];
  const out = [];
  const ws = p.windows();
  for (let i = 0; i < ws.length; i++) {
    let sr = null;
    try { sr = ws[i].subrole(); } catch (e) {}
    if (sr !== "AXDialog" && sr !== "AXSystemDialog") continue;
    let t = null;
    try { t = ws[i].name(); } catch (e) {}
    const texts = [];
    const buttons = [];
    (function walk(el, depth) {
      if (depth > 8) return;
      let kids = [];
      try { kids = el.uiElements(); } catch (e) { return; }
      for (const k of kids) {
        let r = null, n = null, d = null, v = null;
        try { r = k.role(); } catch (e) {}
        try { n = k.name(); } catch (e) {}
        try { d = k.description(); } catch (e) {}
        try { v = k.value(); } catch (e) {}
        if (r === "AXStaticText" && v) texts.push(String(v));
        if (r === "AXButton") buttons.push(n || d || null);
        walk(k, depth + 1);
      }
    })(ws[i], 0);
    out.push({title: t, subrole: sr, text: texts.join(" ").trim(),
              buttons: buttons});
  }
  JSON.stringify(out);
}
"""

_JXA_DISMISS_ALERT = """
const se = Application("System Events");
const ps = se.processes.whose({name: {_beginsWith: %s}});
if (ps.length === 0) { JSON.stringify({ok: false, error: "no_live"}); } else {
  const p = ps[0];
  const want = %s;
  const ws = p.windows();
  let clicked = null;
  let alertText = "";
  for (let i = 0; i < ws.length && !clicked; i++) {
    let sr = null;
    try { sr = ws[i].subrole(); } catch (e) {}
    if (sr !== "AXDialog" && sr !== "AXSystemDialog") continue;
    (function walk(el, depth) {
      if (depth > 8 || clicked) return;
      let kids = [];
      try { kids = el.uiElements(); } catch (e) { return; }
      for (const k of kids) {
        if (clicked) return;
        let r = null, n = null, d = null, v = null;
        try { r = k.role(); } catch (e) {}
        try { n = k.name(); } catch (e) {}
        try { d = k.description(); } catch (e) {}
        try { v = k.value(); } catch (e) {}
        if (r === "AXStaticText" && v) alertText += String(v) + " ";
        if (r === "AXButton") {
          const label = String(n || d || "");
          if (label.toLowerCase() === want) { clicked = k; return; }
        }
        walk(k, depth + 1);
      }
    })(ws[i], 0);
  }
  if (!clicked) { JSON.stringify({ok: false, error: "button_not_found",
                                  text: alertText.trim()}); }
  else { clicked.click(); JSON.stringify({ok: true, text: alertText.trim()}); }
}
"""

def _file_url_path(url: Any) -> Optional[str]:
    """``file:///a%20b/c.als`` -> ``/a b/c.als``; None for anything else."""
    if not isinstance(url, str) or not url.startswith("file://"):
        return None
    from urllib.parse import unquote, urlparse
    path = unquote(urlparse(url).path)
    return path or None


def summarize_windows(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The status view of ``MacUI.live_windows()``: the main window parsed,
    any modal dialog named, and every title (plugin windows included)."""
    if data is None:
        return {"readable": False, "set_name": None, "unsaved": None,
                "set_path": None, "modal_dialog": None, "frontmost": None,
                "titles": None}
    windows = data.get("windows") or []
    titles = [w.get("title") or "" for w in windows]
    main = next((parse_main_title(t) for t in titles if "Ableton Live" in t), None)
    standard = next((w for w in windows if w.get("subrole") == "AXStandardWindow"
                     and w.get("title")), None)
    if main is None and standard is not None:
        # Live 12.4.7 titles its main window with the bare set name: no
        # "- Ableton Live", no project, and no "*" for unsaved changes, so
        # `unsaved` is unknown (None), not False.
        main = {"title": standard["title"], "set_name": standard["title"],
                "project": None, "unsaved": None}
    set_path = _file_url_path((standard or {}).get("document"))
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
        "set_path": set_path,
        "modal_dialog": modal,
        "frontmost": data.get("frontmost"),
        "titles": titles,
    }
