"""save_set's File-menu path, checkpoint_set and release_live_lock(save_first)
— against a scripted fake of the macOS UI and real temp files.

The fake replaces MacUI's methods, so nothing here runs osascript or
touches a real Live; the UI flows themselves (menu titles, timing, the
Save panel on an Untitled set) are UNVERIFIED until the live pass.
"""

import hashlib
import os

import pytest

from ableton_mcp import presenters
from ableton_mcp.live_lock import LiveLock, LockError
from ableton_mcp.services import AbletonService
from ableton_mcp.ui_macos import MacUI

from golden_runner import AllCapabilitiesHandshake


class Client:
    """Live's API save probe: never finds a save (as on 12.4.x)."""

    def __init__(self, saved=False):
        self.sent = []
        self.saved = saved

    def send_command(self, command_type, params=None):
        self.sent.append(command_type)
        if command_type == "save_set":
            if self.saved:
                return {"saved": True, "method": "song.save_set()", "attempts": []}
            return {"saved": False, "method": None, "attempts": []}
        return {}


def _title(name, unsaved):
    return f"{name}{'*' if unsaved else ''} [{name} Project] - Ableton Live 12 Suite"


class FakeUI(MacUI):
    """Scripted UI: clicking Save rewrites the .als and clears the '*'."""

    def __init__(self, set_path=None, set_name="song", unsaved=True,
                 click_result=None, on_click="save", spotlight=None):
        super().__init__(runner=lambda a, t: (1, "", ""), platform="darwin",
                         sleep=lambda s: None, clock=self._tick)
        self._t = 0.0
        self.set_path = set_path
        self.set_name = set_name
        self.unsaved = unsaved
        self.dialog = None
        self.click_result = click_result
        self.on_click = on_click
        self.spotlight = spotlight
        self.calls = []

    def _tick(self):
        self._t += 1.0
        return self._t

    def live_windows(self):
        windows = [{"title": _title(self.set_name, self.unsaved),
                    "subrole": "AXStandardWindow"}]
        if self.dialog:
            windows.append({"title": self.dialog, "subrole": "AXDialog"})
        return {"frontmost": True, "windows": windows}

    def find_set_paths(self, set_name, project=None, limit=20):
        self.calls.append(("find", set_name, project))
        if self.spotlight is not None:
            return self.spotlight
        return [self.set_path] if self.set_path else []

    def frontmost_app(self):
        return "Terminal"

    def activate(self, process_name):
        self.calls.append(("activate", process_name))
        return True

    def click_menu_item(self, menu, prefix, exclude=()):
        self.calls.append(("click", menu, prefix, tuple(exclude)))
        if self.click_result is not None:
            return self.click_result
        if self.on_click == "save":
            st = os.stat(self.set_path)
            os.utime(self.set_path, (st.st_atime, st.st_mtime + 10))
            self.unsaved = False
        elif self.on_click == "panel":
            self.dialog = "Save Live Set As"
        return {"ok": True, "clicked": "Save Live Set"}

    def press_escape(self):
        self.calls.append(("escape",))
        self.dialog = None
        return True


@pytest.fixture
def als(tmp_path):
    folder = tmp_path / "song Project"
    folder.mkdir()
    path = folder / "song.als"
    path.write_bytes(b"\x1f\x8b" + b"set-bytes" * 100)
    os.utime(path, (1_000.0, 1_000.0))
    return str(path)


def _service(ui, saved=False, allow_ui=False, lock=None):
    client = Client(saved=saved)
    return AbletonService(client, AllCapabilitiesHandshake(), lock=lock, ui=ui,
                          allow_ui=allow_ui), client


def test_without_consent_only_the_api_is_tried(als):
    ui = FakeUI(set_path=als)
    service, client = _service(ui)
    result = service.save_set()
    assert result["saved"] is False and result["ui_path"] == "not_allowed"
    assert client.sent == ["save_set"]
    assert not any(c[0] == "click" for c in ui.calls)
    assert "allow_ui_automation=true" in presenters.save_set(result)


def test_menu_save_is_verified_by_title_and_mtime(als):
    ui = FakeUI(set_path=als)
    service, _ = _service(ui)
    result = service.save_set(allow_ui_automation=True)
    assert result["saved"] is True
    assert result["method"] == "ui: File > Save Live Set"
    v = result["verified"]
    assert v["set_path"] == als and v["set_path_source"] == "spotlight"
    assert v["mtime_advanced"] is True and v["unsaved_after"] is False
    # Clicked by title, never "Save Live Set As…"; focus handed back.
    assert ("click", "File", "Save Live Set", ("Save Live Set As",)) in ui.calls
    assert ("activate", "Terminal") in ui.calls
    text = presenters.save_set(result)
    assert text.startswith("Saved the Live Set via ui: File > Save Live Set")
    assert "modified time advanced" in text


def test_server_wide_consent_is_enough(als):
    service, _ = _service(FakeUI(set_path=als), allow_ui=True)
    assert service.save_set()["saved"] is True


def test_api_save_skips_the_menu(als):
    ui = FakeUI(set_path=als)
    service, _ = _service(ui, saved=True)
    result = service.save_set(allow_ui_automation=True)
    assert result["saved"] is True and result["method"] == "song.save_set()"
    assert not any(c[0] == "click" for c in ui.calls)


def test_a_click_that_changes_nothing_is_not_reported_as_saved(als):
    ui = FakeUI(set_path=als, on_click="nothing")
    service, _ = _service(ui)
    result = service.save_set(allow_ui_automation=True, set_path=als)
    assert result["saved"] is False and result["error"] == "not_confirmed"
    assert "not confirmed" in presenters.save_set(result)


def test_an_untitled_set_save_panel_is_cancelled_and_refused(als):
    ui = FakeUI(set_path=als, set_name="Untitled", on_click="panel")
    service, _ = _service(ui)
    result = service.save_set(allow_ui_automation=True)
    assert result["saved"] is False and result["error"] == "save_panel_opened"
    assert ("escape",) in ui.calls
    assert "never been saved" in presenters.save_set(result)


def test_a_dialog_already_open_refuses_before_clicking(als):
    ui = FakeUI(set_path=als)
    ui.dialog = "Live Set Recovery"
    service, _ = _service(ui)
    result = service.save_set(allow_ui_automation=True)
    assert result["error"] == "modal_open"
    assert not any(c[0] == "click" for c in ui.calls)


def test_a_missing_menu_item_names_what_was_there(als):
    ui = FakeUI(set_path=als, click_result={
        "ok": False, "error": "item_not_found", "items": ["New Live Set", "Open…"]})
    service, _ = _service(ui)
    result = service.save_set(allow_ui_automation=True)
    assert result["error"] == "menu_item_not_found"
    assert "New Live Set" in presenters.save_set(result)


def test_a_clean_set_with_save_greyed_out_counts_as_saved(als):
    ui = FakeUI(set_path=als, unsaved=False,
                click_result={"ok": False, "error": "item_disabled", "item": "Save Live Set"})
    service, _ = _service(ui)
    result = service.save_set(allow_ui_automation=True)
    assert result["saved"] is True
    assert result["method"].startswith("none needed")


def test_ambiguous_spotlight_hits_are_reported_not_guessed(als, tmp_path):
    other = tmp_path / "elsewhere" / "song.als"
    other.parent.mkdir()
    other.write_bytes(b"x")
    ui = FakeUI(set_path=als, spotlight=[str(other), als, str(tmp_path / "x" / "song.als")])
    service, _ = _service(ui)
    result = service.save_set(allow_ui_automation=True)
    # The copy inside the "<project>" folder the title names wins outright.
    assert result["verified"]["set_path"] == als


# ── checkpoint_set ──────────────────────────────────────────────────────────

def test_checkpoint_copies_and_hash_verifies(als):
    service, _ = _service(FakeUI(set_path=als))
    result = service.checkpoint_set("before re-amp!", allow_ui_automation=True)
    assert result["checkpointed"] is True
    dest = result["checkpoint_path"]
    assert os.path.dirname(dest) == os.path.join(os.path.dirname(als), "Checkpoints")
    assert os.path.basename(dest).endswith("-before-re-amp.als")
    assert result["sha256"] == hashlib.sha256(open(als, "rb").read()).hexdigest()
    assert "Checkpoint saved" in presenters.checkpoint_set(result)
    # A second checkpoint in the same second gets its own name.
    again = service.checkpoint_set("before re-amp!", allow_ui_automation=True)
    assert again["checkpoint_path"] != dest


def test_checkpoint_refuses_when_the_save_fails(als):
    service, _ = _service(FakeUI(set_path=als))
    result = service.checkpoint_set("x")  # no consent: the save cannot happen
    assert result["checkpointed"] is False and result["error"] == "save_failed"
    assert presenters.checkpoint_set(result).startswith("No checkpoint: Could NOT save")


def test_checkpoint_into_an_explicit_folder(als, tmp_path):
    service, _ = _service(FakeUI(set_path=als))
    result = service.checkpoint_set("mix", allow_ui_automation=True,
                                    checkpoint_dir=str(tmp_path / "keep"))
    assert os.path.dirname(result["checkpoint_path"]) == str(tmp_path / "keep")


# ── the lock: saving is checked, and release can save first ─────────────────

def test_save_needs_the_lock_and_release_can_save_first(als, tmp_path):
    lock = LiveLock(str(tmp_path / "lock" / "live.lock"), require=True,
                    clock=lambda: 1_005.0)
    ui = FakeUI(set_path=als)
    service, client = _service(ui, lock=lock)
    with pytest.raises(LockError):
        service.save_set(allow_ui_automation=True)
    assert client.sent == []  # refused before the wire
    lock.acquire("session-a")
    lock.touch()  # last edit at 1005; the .als is at 1000 → stale until saved
    result = service.release_live_lock("session-a", save_first=True,
                                       allow_ui_automation=True)
    assert result["released"] is True and result["save"]["saved"] is True
    assert result["saved_set"] == als
    assert presenters.release_live_lock(result).endswith("(saved first).")


def test_release_save_first_failure_keeps_the_lock(als, tmp_path):
    lock = LiveLock(str(tmp_path / "lock" / "live.lock"), require=True)
    lock.acquire("session-a")
    service, _ = _service(FakeUI(set_path=als, on_click="nothing"), lock=lock)
    with pytest.raises(LockError) as e:
        service.release_live_lock("session-a", save_first=True,
                                  allow_ui_automation=True)
    assert e.value.reason == "save_failed"
    assert os.path.exists(lock.path)
    assert "lock stays held" in presenters.lock_refusal(e.value)
