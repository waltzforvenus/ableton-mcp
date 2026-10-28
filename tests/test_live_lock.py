"""The shared Live lock (live_lock.py), its enforcement in the service's
``_send``, the passive macOS reads (ui_macos.py) and the get_live_status
preflight — against real temp files and recorded subprocess outputs.

Nothing here runs osascript or ioreg: every UI read goes through a fake
runner. Runs anywhere — no Ableton, no network.
"""

import json
import os
from types import SimpleNamespace

import pytest

from ableton_mcp import presenters, tools
from ableton_mcp.app import Deps, Settings
from ableton_mcp.commands import COMMANDS
from ableton_mcp.live_lock import LiveLock, LockError, unsaved_problem
from ableton_mcp.services import AbletonService
from ableton_mcp.ui_macos import MacUI, parse_main_title, summarize_windows

from fake_ableton import make_harness
from golden_runner import AllCapabilitiesHandshake


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def lock_path(tmp_path):
    return str(tmp_path / "ableton" / "live.lock")


def _lock(lock_path, require=True, clock=None):
    return LiveLock(lock_path, require=require, clock=clock or Clock())


def _read(path):
    with open(path) as f:
        return json.load(f)


# ── the file contract (shared with the command-line helper) ─────────────────

def test_acquire_writes_the_helper_compatible_record(lock_path):
    clock = Clock(1_700_000_000.0)
    lock = _lock(lock_path, clock=clock)
    result = lock.acquire("session-a", "mix pass")
    data = _read(lock_path)
    assert set(data) == {"holder", "task", "acquired", "acquired_ts", "last_edit_ts"}
    assert data["holder"] == "session-a" and data["task"] == "mix pass"
    assert data["acquired_ts"] == data["last_edit_ts"] == 1_700_000_000.0
    assert data["acquired"].startswith("2023-11-14T22:13:20")
    assert result["adopted"] is False and lock.holder == "session-a"
    # The mutex file and the one-line history sit beside the lock.
    assert os.path.exists(lock_path + ".mutex")
    assert "acquire session-a" in open(lock_path + ".log").read()


def test_acquire_adopts_a_lock_taken_elsewhere_under_the_same_name(lock_path):
    os.makedirs(os.path.dirname(lock_path))
    record = {"holder": "session-a", "task": "from the helper",
              "acquired": "2026-01-01T00:00:00+00:00", "acquired_ts": 5.0,
              "last_edit_ts": 7.0}
    with open(lock_path, "w") as f:
        json.dump(record, f)
    lock = _lock(lock_path)
    result = lock.acquire("session-a")
    assert result["adopted"] is True
    assert _read(lock_path) == record  # untouched: same holder, same edit stamp
    lock.check_write("set_tempo")  # and now this process may write


def test_acquire_refuses_another_holder_and_never_takes_over(lock_path):
    _lock(lock_path).acquire("session-a", "bounce")
    other = _lock(lock_path)
    with pytest.raises(LockError) as e:
        other.acquire("session-b")
    assert e.value.reason == "held_by_other"
    assert e.value.details["lock"]["holder"] == "session-a"
    assert _read(lock_path)["holder"] == "session-a"
    assert other.holder is None


def test_a_corrupt_lock_file_reads_as_held_not_free(lock_path):
    os.makedirs(os.path.dirname(lock_path))
    with open(lock_path, "w") as f:
        f.write("{not json")
    lock = _lock(lock_path, require=False)
    with pytest.raises(LockError) as e:
        lock.check_write("set_tempo")
    assert e.value.reason == "held_by_other"


def test_empty_holder_is_rejected(lock_path):
    with pytest.raises(ValueError):
        _lock(lock_path).acquire("  ")


# ── check_write ─────────────────────────────────────────────────────────────

def test_no_lock_refuses_by_default_and_passes_when_optional(lock_path):
    with pytest.raises(LockError) as e:
        _lock(lock_path, require=True).check_write("set_tempo")
    assert e.value.reason == "not_held"
    _lock(lock_path, require=False).check_write("set_tempo")


def test_someone_elses_lock_refuses_even_when_the_lock_is_optional(lock_path):
    _lock(lock_path).acquire("session-a")
    with pytest.raises(LockError) as e:
        _lock(lock_path, require=False).check_write("set_tempo")
    assert e.value.reason == "held_by_other"


def test_touch_stamps_last_edit_only_for_the_holder(lock_path):
    clock = Clock(100.0)
    lock = _lock(lock_path, clock=clock)
    lock.acquire("session-a")
    clock.t = 250.0
    lock.touch()
    assert _read(lock_path)["last_edit_ts"] == 250.0
    stranger = _lock(lock_path, clock=Clock(999.0))
    stranger.touch()  # holds nothing: a no-op, never a stamp on someone else's lock
    assert _read(lock_path)["last_edit_ts"] == 250.0


# ── release: only after a verified save ─────────────────────────────────────

def _saved_set(tmp_path, mtime):
    path = tmp_path / "song.als"
    path.write_bytes(b"\x1f\x8b fake gzip")
    os.utime(path, (mtime, mtime))
    return str(path)


def test_release_refuses_a_set_older_than_the_last_edit(lock_path, tmp_path):
    clock = Clock(2_000.0)
    lock = _lock(lock_path, clock=clock)
    lock.acquire("session-a")
    lock.touch()
    path = _saved_set(tmp_path, 1_500.0)
    with pytest.raises(LockError) as e:
        lock.release("session-a", path)
    assert e.value.reason == "set_older_than_edit"
    assert os.path.exists(lock_path)  # the lock stays held


def test_release_after_a_newer_save_removes_the_lock(lock_path, tmp_path):
    lock = _lock(lock_path, clock=Clock(2_000.0))
    lock.acquire("session-a")
    path = _saved_set(tmp_path, 2_001.0)
    result = lock.release("session-a", path,
                          live_windows=["song [proj] - Ableton Live 12 Suite"])
    assert result["released"] is True
    assert not os.path.exists(lock_path)
    assert lock.holder is None
    assert "release session-a" in open(lock_path + ".log").read()


@pytest.mark.parametrize("windows, problem", [
    (["song* [proj] - Ableton Live 12 Suite"], "unsaved_marker"),
    (["other [proj] - Ableton Live 12 Suite"], "different_set"),
    (["song [proj] - Ableton Live 12 Suite", "Export Audio/Video"], "dialog_open"),
])
def test_release_refuses_what_the_window_titles_show(lock_path, tmp_path,
                                                     windows, problem):
    lock = _lock(lock_path, clock=Clock(2_000.0))
    lock.acquire("session-a")
    path = _saved_set(tmp_path, 2_001.0)
    with pytest.raises(LockError) as e:
        lock.release("session-a", path, live_windows=windows)
    assert e.value.reason == "live_unsaved"
    assert e.value.details["problem"] == problem
    assert os.path.exists(lock_path)


def test_release_refuses_a_non_holder_and_a_missing_set(lock_path, tmp_path):
    lock = _lock(lock_path)
    lock.acquire("session-a")
    with pytest.raises(LockError) as e:
        lock.release("session-b", _saved_set(tmp_path, 9e9))
    assert e.value.reason == "not_holder"
    with pytest.raises(LockError) as e:
        lock.release("session-a", str(tmp_path / "missing.als"))
    assert e.value.reason == "set_missing"


def test_release_with_nothing_held_is_a_no_op(lock_path, tmp_path):
    assert _lock(lock_path).release("x", "")["released"] is False


def test_unsaved_problem_mirrors_the_helper_rule():
    assert unsaved_problem(["a - Ableton Live 12 Suite"], "/x/a.als") is None
    assert unsaved_problem([], "/x/a.als") == "different_set"


# ── enforcement in AbletonService._send ─────────────────────────────────────

class RecordingClient:
    def __init__(self):
        self.sent = []

    def send_command(self, command_type, params=None):
        self.sent.append(command_type)
        return {"ok": True}


def _service(lock):
    client = RecordingClient()
    return AbletonService(client, AllCapabilitiesHandshake(), lock=lock), client


def test_a_refused_write_never_reaches_the_wire(lock_path):
    _lock(lock_path).acquire("session-a")
    service, client = _service(_lock(lock_path))
    with pytest.raises(LockError):
        service.set_tempo(100.0)
    assert client.sent == []


def test_reads_never_need_the_lock(lock_path):
    service, client = _service(_lock(lock_path, require=True))
    service.get_session_info()
    assert client.sent == ["get_session_info"]


def test_writes_pass_for_the_holder_and_stamp_the_edit(lock_path):
    clock = Clock(100.0)
    lock = _lock(lock_path, clock=clock)
    lock.acquire("session-a")
    service, client = _service(lock)
    clock.t = 300.0
    service.set_tempo(100.0)
    assert client.sent == ["set_tempo"]
    assert _read(lock_path)["last_edit_ts"] == 300.0


def test_save_set_is_checked_but_does_not_stamp_itself(lock_path):
    clock = Clock(100.0)
    lock = _lock(lock_path, clock=clock)
    lock.acquire("session-a")
    service, client = _service(lock)
    clock.t = 300.0
    service.save_set()
    assert client.sent == ["save_set"]
    assert _read(lock_path)["last_edit_ts"] == 100.0
    # ...and a stranger still cannot save someone else's set
    stranger, _ = _service(_lock(lock_path))
    with pytest.raises(LockError):
        stranger.save_set()


def test_stop_playback_is_never_refused(lock_path):
    _lock(lock_path).acquire("session-a")
    service, client = _service(_lock(lock_path))
    service.stop_playback()
    assert client.sent == ["stop_playback"]


def test_registry_lock_modes_are_valid():
    assert {s.lock for s in COMMANDS.values()} <= {"write", "check", "exempt"}
    assert COMMANDS["save_set"].lock == "check"
    assert COMMANDS["stop_playback"].lock == "exempt"
    # Commands that take over Live but leave nothing to save never stamp.
    assert sorted(n for n, s in COMMANDS.items() if s.modifying and s.lock == "check") == [
        "back_to_arrangement", "fire_clip", "jump_to_locator", "save_set",
        "select_track", "set_current_song_time", "start_playback", "stop_clip",
        "switch_to_arrangement_view"]


def test_transport_moves_do_not_make_a_clean_save_look_stale(lock_path, tmp_path):
    clock = Clock(1_000.0)
    lock = _lock(lock_path, clock=clock)
    lock.acquire("session-a")
    service, client = _service(lock)
    service.set_tempo(100.0)                       # a real edit at t=1000
    path = _saved_set(tmp_path, 1_001.0)           # saved after it
    clock.t = 2_000.0
    service.switch_to_arrangement_view()           # view/transport afterwards
    service.set_current_song_time(16.0)
    assert lock.release("session-a", path)["released"] is True


def test_a_failed_send_still_stamps_the_edit(lock_path):
    clock = Clock(100.0)
    lock = _lock(lock_path, clock=clock)
    lock.acquire("session-a")

    class Exploding(RecordingClient):
        def send_command(self, command_type, params=None):
            raise RuntimeError("Timeout waiting for operation to complete")

    service = AbletonService(Exploding(), AllCapabilitiesHandshake(), lock=lock)
    clock.t = 400.0
    with pytest.raises(RuntimeError):
        service.set_tempo(90.0)
    assert _read(lock_path)["last_edit_ts"] == 400.0


# ── the tool surface: refusals are worded, not "Error ..." ──────────────────

def _ctx(service, client):
    deps = Deps(client=client, handshake=AllCapabilitiesHandshake(), service=service)
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=deps))


def test_tools_word_a_lock_refusal_naming_the_holder(lock_path):
    _lock(lock_path).acquire("session-a", "bounce stems")
    service, client = _service(_lock(lock_path))
    text = tools.set_tempo(_ctx(service, client), tempo=100.0)
    assert text.startswith("Refused: Live is locked by 'session-a' (task: bounce stems)")
    assert "set_tempo was not sent" in text


def test_tool_round_trip_acquire_then_write_then_release(lock_path, tmp_path):
    clock = Clock(1_000.0)
    lock = _lock(lock_path, clock=clock)
    service, client = _service(lock)
    ctx = _ctx(service, client)
    assert "not hold the Live lock" in tools.set_tempo(ctx, tempo=100.0)
    assert tools.acquire_live_lock(ctx, holder="session-a", task="t").startswith(
        "Acquired the Live lock as 'session-a'")
    tools.set_tempo(ctx, tempo=100.0)
    assert client.sent == ["set_tempo"]
    early = _saved_set(tmp_path, 999.0)
    assert "older than your last edit" in tools.release_live_lock(
        ctx, holder="session-a", set_path=early)
    saved = _saved_set(tmp_path, 1_001.0)
    assert tools.release_live_lock(ctx, holder="session-a", set_path=saved).startswith(
        "Released the Live lock")


def test_every_lock_reason_has_wording():
    for reason in ("held_by_other", "not_held", "not_holder", "set_missing",
                   "live_unsaved", "disabled"):
        assert presenters.lock_refusal(LockError(reason)).strip()
    assert "older than" in presenters.lock_refusal(LockError(
        "set_older_than_edit", set_path="/x.als", set_mtime=1.0, last_edit_ts=2.0))


# ── settings ────────────────────────────────────────────────────────────────

def test_settings_default_requires_the_lock():
    s = Settings.from_env({})
    assert s.require_lock is True
    assert s.lock_file == "~/.ableton-mcp/live.lock"


@pytest.mark.parametrize("value", ["0", "false", "no", "off", " OFF "])
def test_settings_opt_out(value):
    assert Settings.from_env({"ABLETON_MCP_REQUIRE_LOCK": value}).require_lock is False


def test_settings_lock_file_override():
    s = Settings.from_env({"ABLETON_MCP_LOCK_FILE": "/tmp/x.lock"})
    assert s.lock_file == "/tmp/x.lock"


# ── ui_macos: passive reads through a fake runner ───────────────────────────

class FakeRunner:
    def __init__(self, outputs):
        self.outputs = outputs  # program name -> (code, stdout, stderr)
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        return self.outputs.get(args[0], (1, "", "not faked"))


WINDOWS_JSON = json.dumps({"frontmost": False, "windows": [
    {"title": "song* [proj] - Ableton Live 12 Suite", "subrole": "AXStandardWindow"},
    {"title": "Reverb/2-Audio", "subrole": "AXStandardWindow"},
    {"title": "", "subrole": "AXDialog"},
]})


def test_macui_reads_windows_and_idle_through_the_runner():
    runner = FakeRunner({
        "osascript": (0, WINDOWS_JSON + "\n", ""),
        "ioreg": (0, '  |   "HIDIdleTime" = 42500000000\n', ""),
    })
    ui = MacUI(runner=runner, platform="darwin")
    summary = summarize_windows(ui.live_windows())
    assert summary["set_name"] == "song" and summary["unsaved"] is True
    assert summary["project"] == "proj"
    assert summary["modal_dialog"] == "AXDialog"
    assert ui.user_idle_s() == 42.5
    assert runner.calls[0][:3] == ["osascript", "-l", "JavaScript"]


def test_macui_is_unknown_off_darwin_and_on_failure():
    runner = FakeRunner({})
    assert MacUI(runner=runner, platform="linux").live_windows() is None
    assert runner.calls == []  # never even tries
    failing = MacUI(runner=FakeRunner({"osascript": (1, "", "not allowed")}),
                    platform="darwin")
    assert failing.live_windows() is None
    assert summarize_windows(None)["readable"] is False


@pytest.mark.parametrize("title, expected", [
    ("song [proj] - Ableton Live 12 Suite", ("song", "proj", False)),
    ("Untitled - Ableton Live 12 Suite", ("Untitled", None, False)),
    ("A b c* [P Q] - Ableton Live 12 Suite", ("A b c", "P Q", True)),
])
def test_parse_main_title(title, expected):
    parsed = parse_main_title(title)
    assert (parsed["set_name"], parsed["project"], parsed["unsaved"]) == expected


# ── get_live_status: the Remote Script half and the assembled view ──────────

def test_remote_script_live_status_reports_solo_mute_arm():
    harness = make_harness()
    song = harness.song
    song.tracks[0].solo = True
    song.tracks[1].mute = True
    if getattr(song.tracks[1], "can_be_armed", True):
        song.tracks[1].arm = True
    resp = harness.process({"type": "get_live_status", "params": {}})
    assert resp["status"] == "success", resp
    status = resp["result"]
    assert [t["index"] for t in status["soloed"]] == [0]
    assert [t["index"] for t in status["muted"]] == [1]
    assert status["track_count"] == len(song.tracks)
    assert status["is_playing"] is False
    for key in ("record_mode", "session_record", "last_event_time",
                "live_version", "selected_track", "exclusive_solo"):
        assert key in status


def test_service_status_survives_an_unreachable_live(lock_path):
    class Down(RecordingClient):
        def send_command(self, command_type, params=None):
            raise ConnectionRefusedError("Could not connect to Ableton")

    lock = _lock(lock_path)
    lock.acquire("session-a", "mix")
    ui = MacUI(runner=FakeRunner({"osascript": (0, WINDOWS_JSON, ""),
                                  "ioreg": (1, "", "")}), platform="darwin")
    service = AbletonService(Down(), AllCapabilitiesHandshake(), lock=lock, ui=ui)
    status = service.get_live_status()
    assert status["connected"] is False and "Could not connect" in status["live_error"]
    assert status["lock"]["lock"]["holder"] == "session-a"
    assert status["lock"]["held_by_this_server"] is True
    assert status["ui"]["unsaved"] is True and status["user_idle_s"] is None
