"""The load-alert watchdog and the cue settle-retry: against fakes.

The watchdog turns Live's "The preset cannot be loaded." modal - which
blocks the Remote Script and wedges every later command - into a clean
`live_alert` refusal that names the alert's own text, and only ever
dismisses an alert that appeared while OUR command was in flight. The cue
retry clears the script's side-effect-free "Transport did not settle ...
retry in a moment" refusal on the next tick. No osascript runs here.
"""

import json
import threading

import pytest

from ableton_mcp import render
from ableton_mcp.live_lock import LiveLock
from ableton_mcp.services import AbletonService
from ableton_mcp.ui_macos import MacUI

from golden_runner import AllCapabilitiesHandshake


ALERT = {"title": None, "subrole": "AXDialog",
         "text": "The preset cannot be loaded.", "buttons": ["OK"]}


class FakeUI:
    def __init__(self, alerts_after_baseline=True, reply_unblocks=True):
        self.available = True
        self.calls = 0
        self.alerts_after_baseline = alerts_after_baseline
        self.dismissed = []
        self.unblock = threading.Event() if reply_unblocks else None

    def modal_alerts(self):
        self.calls += 1
        if self.calls == 1 or not self.alerts_after_baseline:
            return []
        return [ALERT]

    def dismiss_modal_alert(self, button="OK"):
        self.dismissed.append(button)
        if self.unblock:
            self.unblock.set()
        return {"ok": True, "text": ALERT["text"]}


class FakeClient:
    """Blocks in send_command until the watchdog dismisses the alert."""

    def __init__(self, reply=None, error=None):
        self.reply = reply if reply is not None else {"loaded": True}
        self.error = error
        self.sent = []

    def send_command(self, command_type, params=None):
        self.sent.append(command_type)
        if hasattr(self, "gate"):
            self.gate.wait(timeout=5.0)
        if self.error is not None:
            raise self.error
        return self.reply


class CueClient:
    def __init__(self):
        self.sent = []
        self.replies = [Exception("Transport did not settle on beat 0.0 "
                                  "(still reads 4.0); no cue was toggled - "
                                  "retry in a moment"),
                        {"success": True}]

    def send_command(self, command_type, params=None):
        self.sent.append(command_type)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _service(client, ui=None, tmp_path="/tmp"):
    lock = LiveLock(str(tmp_path / "live.lock"), require=False)
    lock.acquire("test-session")
    return AbletonService(client, AllCapabilitiesHandshake(),
                          lock=lock, ui=ui)


def test_modal_alerts_parse_from_a_recorded_runner():
    def runner(args, timeout):
        assert args[0] == "osascript" and args[1] == "-l"
        return 0, json.dumps([ALERT]), ""
    ui = MacUI(runner=runner, platform="darwin")
    assert ui.modal_alerts() == [ALERT]
    assert MacUI(platform="linux").modal_alerts() is None


def test_dismiss_presses_the_label_it_was_given():
    seen = []

    def runner(args, timeout):
        seen.append(args[-1])
        return 0, json.dumps({"ok": True, "text": ALERT["text"]}), ""
    ui = MacUI(runner=runner, platform="darwin")
    out = ui.dismiss_modal_alert("OK")
    assert out["ok"] is True and out["text"] == ALERT["text"]
    assert '"ok"' in seen[0] and '"live"' in seen[0].lower()


def test_watchdog_returns_the_reply_with_the_alert_folded_in(tmp_path):
    ui = FakeUI()
    client = FakeClient(reply={"loaded": True})
    client.gate = ui.unblock
    service = _service(client, ui=ui, tmp_path=tmp_path)
    out = service.load_device_from_path(4, "/presets/broken.adv")
    assert out["loaded"] is True
    assert out["alert"] == ALERT["text"]
    assert ui.dismissed == ["OK"]


def test_watchdog_raises_live_alert_naming_the_text(tmp_path):
    ui = FakeUI()
    client = FakeClient(error=TimeoutError("No data received"))
    client.gate = ui.unblock
    service = _service(client, ui=ui, tmp_path=tmp_path)
    with pytest.raises(render.RenderRefused) as caught:
        service.load_device_from_path(4, "/presets/broken.adv")
    assert caught.value.reason == "live_alert"
    assert ALERT["text"] in caught.value.details.get("alert_text", "")


def test_without_a_ui_the_load_is_a_plain_send(tmp_path):
    client = FakeClient(reply={"loaded": True})
    service = _service(client, ui=None, tmp_path=tmp_path)
    assert service.load_device_from_path(4, "/presets/ok.adv") == {"loaded": True}


def test_a_preset_modal_open_before_the_load_refuses_without_sending(tmp_path):
    ui = FakeUI(alerts_after_baseline=False)  # the baseline itself sees one
    ui.modal_alerts = lambda: [ALERT]
    client = FakeClient()
    service = _service(client, ui=ui, tmp_path=tmp_path)
    with pytest.raises(render.RenderRefused) as caught:
        service.load_device_from_path(4, "/presets/x.adv")
    assert caught.value.reason == "modal_open"
    assert client.sent == []


def test_cue_settle_refusal_is_retried_once_on_a_fresh_tick(tmp_path):
    client = CueClient()
    service = _service(client, ui=None, tmp_path=tmp_path)
    out = service.create_locator("smoke-test", 0.0)
    assert out == {"success": True}
    assert client.sent == ["create_locator", "create_locator"]


def test_other_cue_errors_are_not_retried(tmp_path):
    class Once:
        def __init__(self):
            self.sent = []

        def send_command(self, command_type, params=None):
            self.sent.append(command_type)
            raise RuntimeError("No locator matches name='x'")

    client = Once()
    service = _service(client, ui=None, tmp_path=tmp_path)
    with pytest.raises(RuntimeError):
        service.jump_to_locator(name="x")
    assert client.sent == ["jump_to_locator"]
