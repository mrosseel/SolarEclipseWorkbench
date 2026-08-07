"""Per-frame timing, and the scheduler faults that used to vanish.

Two silences this closes.  A command knew its camera but not the time it was
meant to run, so a frame four seconds late looked exactly like one on its mark.
And APScheduler discards a job that raises or misses its grace window without
telling anyone, which during totality is the eclipse going wrong quietly.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from solareclipseworkbench import frame_log, hardware_problems


@pytest.fixture(autouse=True)
def _fresh(tmp_path):
    frame_log.reset(tmp_path / "frames.csv")
    hardware_problems.clear()
    yield
    hardware_problems.clear()


def _intended(seconds_ago=0.0):
    return datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)


def test_a_frame_records_what_it_was_meant_to_do_and_when():
    frame_log.begin("Corona ladder 1", _intended())

    frame_log.record("ok")

    summary = frame_log.summarise()
    assert summary["rows"] == 1
    assert summary["ok"] == 1
    assert summary["lost"] == 0


def test_a_late_frame_is_visible_as_late():
    # The whole point: this is the case that used to be indistinguishable from
    # an on-time frame once the run was over.
    frame_log.begin("Corona ladder 1", _intended(seconds_ago=4.0))

    frame_log.record("ok")

    assert frame_log.summarise()["worst_delta_s"] >= 3.9


def test_a_dropped_frame_is_recorded_against_the_command_that_lost_it():
    # _serialised_on_camera drops a shot rather than take it late.  Deliberate,
    # but the log line alone was joined to nothing.
    frame_log.begin("C3 beads", _intended())

    frame_log.record("dropped", "camera busy for more than 1.5s")

    summary = frame_log.summarise()
    assert summary["lost"] == 1
    assert summary["outcomes"]["dropped"] == 1


def test_only_the_first_outcome_for_a_frame_is_kept():
    # The wrapper writes an outcome on the way out and the lock writes one when
    # it gives up; the specific one must win, not be doubled.
    frame_log.begin("C3 beads", _intended())

    frame_log.record("dropped", "camera busy")
    frame_log.record("ok")

    summary = frame_log.summarise()
    assert summary["rows"] == 1
    assert summary["outcomes"] == {"dropped": 1}


def test_recording_without_a_frame_in_progress_is_harmless():
    # A listener can fire for a job that never reached the wrapper.
    frame_log.record("error", "nothing was running")


def test_a_run_is_summarised_rather_than_read():
    for n, outcome in enumerate(("ok", "ok", "dropped", "error")):
        frame_log.begin(f"frame {n}", _intended(seconds_ago=n))
        frame_log.record(outcome)

    summary = frame_log.summarise()
    assert summary["rows"] == 4
    assert summary["ok"] == 2
    assert summary["lost"] == 2
    assert summary["worst_delta_s"] >= 2.9


def test_bookkeeping_never_costs_a_frame(monkeypatch):
    # A frame is worth more than its own record: if the file cannot be written,
    # the shot still happens.
    frame_log.begin("Corona ladder 1", _intended())
    monkeypatch.setattr(frame_log, "_append",
                        lambda row: (_ for _ in ()).throw(OSError("read-only")))

    frame_log.record("ok")          # must not raise


# ------------------------------------------------- the scheduler's own faults

def test_a_job_that_raises_is_reported_rather_than_swallowed():
    from solareclipseworkbench import utils

    frame_log.begin("Corona ladder 1", _intended())
    event = SimpleNamespace(job_id="job-1", exception=RuntimeError("USB gone"),
                            job=SimpleNamespace(name="Corona ladder 1"))

    utils._on_job_problem(event)

    problems = hardware_problems.peek()
    assert any("Corona ladder 1" in str(p) for p in problems), problems
    assert frame_log.summarise()["outcomes"] == {"error": 1}


def test_a_job_that_never_ran_is_reported_too():
    from solareclipseworkbench import utils

    frame_log.begin("C3 beads", _intended())
    event = SimpleNamespace(job_id="job-2", exception=None,
                            job=SimpleNamespace(name="C3 beads"))

    utils._on_job_problem(event)

    assert any("C3 beads" in str(p) for p in hardware_problems.peek())
    assert frame_log.summarise()["outcomes"] == {"missed": 1}


# ------------------------------------------------- live view around totality

class _FakeLiveView:
    """Enough of the Fuji live view window to exercise the pause contract."""

    def __init__(self):
        self._thread = object()
        self.events = []
        # The window tells the user why it stopped; the fake only has to
        # tolerate being told.
        self._status_bar = SimpleNamespace(showMessage=lambda *a, **k: None)

    def stop_stream(self):
        self._thread = None
        self.events.append("stop")

    def start_stream(self):
        self._thread = object()
        self.events.append("start")


def test_live_view_stops_for_a_frame_and_stays_stopped():
    """A stream start is a session operation, and this SDK does not survive many.

    4 August, live view open during a run: an exposure write stopped and
    restarted the stream, the restart was refused 0x1006 because a frame had
    the camera, and within twenty seconds every call answered 0x2001 with the
    session dead.  Restarting by itself looks considerate and costs the run.
    """
    from solareclipseworkbench.liveview import LiveViewWindow

    window = _FakeLiveView()
    LiveViewWindow.set_totality_paused(window, True)
    LiveViewWindow.set_totality_paused(window, True)     # every tick, not just the edge
    LiveViewWindow.set_totality_paused(window, False)

    assert window.events == ["stop"], "restarted the stream by itself"


def test_a_live_view_the_user_had_closed_is_not_opened_by_totality_ending():
    # Resuming something that was not running would turn live view on during the
    # partials after C3, holding the camera nobody asked it to hold.
    from solareclipseworkbench.liveview import LiveViewWindow

    window = _FakeLiveView()
    window._thread = None                                 # not streaming
    LiveViewWindow.set_totality_paused(window, True)
    LiveViewWindow.set_totality_paused(window, False)

    assert window.events == []


# ------------------------------------------------- live view exposure controls

class _FakeSDK:
    """A body that answers the exposure calls live view makes."""

    def __init__(self, speed=8000, iso=400, supported_iso=None):
        self.speed, self.iso = speed, iso
        self._supported = supported_iso
        self.written = []

    def get_shutter_speed(self):
        return self.speed, 0

    def get_iso(self):
        return self.iso

    def get_supported_iso(self):
        if self._supported is None:
            raise RuntimeError("CapSensitivity not implemented on this body")
        return self._supported

    def set_shutter_speed(self, value):
        self.speed = value
        self.written.append(("shutter", value))

    def set_iso(self, value):
        self.iso = value
        self.written.append(("iso", value))


def _live_view_stub(sdk):
    """The window's exposure methods, bound to a stand-in with just the widgets."""
    from types import SimpleNamespace
    from solareclipseworkbench.liveview import LiveViewWindow

    class _Combo:
        def __init__(self): self.items, self.index = [], -1
        def blockSignals(self, b): pass
        def clear(self): self.items = []
        def addItem(self, text, data): self.items.append((text, data))
        def findData(self, data):
            return next((i for i, (_, d) in enumerate(self.items) if d == data), -1)
        def setCurrentIndex(self, i): self.index = i
        def currentData(self):
            return self.items[self.index][1] if 0 <= self.index < len(self.items) else None

    win = SimpleNamespace(_camera=sdk, _shutter_combo=_Combo(), _iso_combo=_Combo(),
                          _status_bar=SimpleNamespace(showMessage=lambda *a: None))
    win.shown = ""
    win._exposure_label = SimpleNamespace(setText=lambda t: setattr(win, "shown", t))
    # The methods run unbound with this as self, so what they call on self has to
    # be attached here.
    import threading
    win._worker = None                      # no stream running in these tests
    win._stream = None                      # ...so nothing to stop for a write
    win._usb_lock = threading.RLock()       # the real one is the camera's

    # The real refresh reads the SDK on a worker thread and shows the result
    # via a queued signal; these tests want it shown by the time they look,
    # so the two halves run here back to back.
    def _sync_refresh():
        skip, result = LiveViewWindow._read_exposure(win)
        if not skip:
            LiveViewWindow._show_exposure(win, result)
    win._refresh_exposure = _sync_refresh
    win._fill_shutter_combo = lambda speeds: LiveViewWindow._fill_shutter_combo(
        win, speeds)
    win._fill_iso_combo = lambda isos: LiveViewWindow._fill_iso_combo(win, isos)
    # The write path speaks to the GUI thread through guarded emits; here
    # they run where they are called, which is what the real queued
    # connection does a moment later on the GUI thread.  Late-bound, so a
    # test that replaces win.stop_stream or win._refresh_exposure sees it.
    win._say = lambda text, ms=0: win._status_bar.showMessage(text, ms)
    win._ask_stop = lambda: win.stop_stream()
    win._ask_resync = lambda: win._refresh_exposure()
    win.stop_stream = lambda: None
    win._write_exposure = lambda action, what, hint: LiveViewWindow._write_exposure(
        win, action, what, hint)
    # The real window writes on a background thread so the window keeps
    # painting; these tests want the write to have happened by the time they
    # look, so here it runs where it is called.
    win._write_in_background = win._write_exposure
    LiveViewWindow._populate_exposure(win)
    return win, LiveViewWindow


def test_live_view_shows_the_shutter_speed_and_iso():
    # Reported 3 August: "I don't see what the shutter speed or the ISO is, I
    # only see that it's under-exposed and I can't change it."
    win, _ = _live_view_stub(_FakeSDK(speed=8000, iso=400))

    assert "ISO 400" in win.shown
    assert "Exposure:" in win.shown


def test_the_shutter_list_is_built_without_asking_the_body():
    # This body does not implement CapShutterSpeed - it answers with an empty
    # list, and a dropdown built from that would be empty too.
    win, _ = _live_view_stub(_FakeSDK())

    values = [d for _, d in win._shutter_combo.items]
    assert len(values) > 20
    # The endpoints by their table keys: Fuji's labels sit on powers of two,
    # so 1/8000 is 122 us and 30 seconds is 32_000_000 us.  This test used to
    # pin the rounded bounds (125 and 30_000_000), which is exactly the bug
    # that kept 1/8000 off the list.
    assert max(values) == 32_000_000        # 30", nothing past it
    assert min(values) == 122               # 1/8000, nothing faster


def test_the_iso_list_falls_back_when_the_body_offers_none():
    win, _ = _live_view_stub(_FakeSDK())     # get_supported_iso raises

    values = [d for _, d in win._iso_combo.items]
    assert 400 in values and 3200 in values


def test_changing_the_shutter_reaches_the_camera():
    sdk = _FakeSDK(speed=8000, iso=400)
    win, LiveViewWindow = _live_view_stub(sdk)
    target = win._shutter_combo.findData(500_000)     # 1/2 s
    win._shutter_combo.setCurrentIndex(target)

    LiveViewWindow._on_shutter_changed(win, target)

    assert ("shutter", 500_000) in sdk.written
    assert "ISO 400" in win.shown


def test_changing_the_iso_reaches_the_camera():
    sdk = _FakeSDK(speed=8000, iso=400, supported_iso=[200, 400, 1600])
    win, LiveViewWindow = _live_view_stub(sdk)
    target = win._iso_combo.findData(1600)
    win._iso_combo.setCurrentIndex(target)

    LiveViewWindow._on_iso_changed(win, target)

    assert ("iso", 1600) in sdk.written
    assert "ISO 1600" in win.shown


# ------------------------------------------------------- live view histogram

def _grey_image(value, w=64, h=64):
    from PyQt6.QtGui import QImage
    img = QImage(w, h, QImage.Format.Format_Grayscale8)
    img.fill(value)
    return img


def test_the_histogram_reports_clipping():
    # The number that matters for a filtered partial: the disc is the only bright
    # thing in frame, so anything at the top of the scale is the disc against the
    # wall.
    from PyQt6.QtWidgets import QApplication
    from solareclipseworkbench.liveview import _Histogram
    QApplication.instance() or QApplication([])

    hist = _Histogram()
    hist.set_image(_grey_image(255))

    assert hist._clipped > 99.0


def test_the_histogram_reports_a_dark_frame_as_black_not_clipped():
    from PyQt6.QtWidgets import QApplication
    from solareclipseworkbench.liveview import _Histogram
    QApplication.instance() or QApplication([])

    hist = _Histogram()
    hist.set_image(_grey_image(2))

    assert hist._black > 99.0
    assert hist._clipped == 0.0


def test_a_well_exposed_frame_is_neither():
    from PyQt6.QtWidgets import QApplication
    from solareclipseworkbench.liveview import _Histogram
    QApplication.instance() or QApplication([])

    hist = _Histogram()
    hist.set_image(_grey_image(128))

    assert hist._clipped == 0.0 and hist._black == 0.0


def test_clipping_counts_the_shoulder_not_only_pure_white():
    # The JPEG's tone curve rolls the top off, so waiting for a true 255
    # understates how close the disc is to the wall.
    from PyQt6.QtWidgets import QApplication
    from solareclipseworkbench.liveview import _Histogram
    QApplication.instance() or QApplication([])

    hist = _Histogram()
    hist.set_image(_grey_image(252))

    assert hist._clipped > 99.0


class _FakeStream:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_the_stream_is_stopped_while_an_exposure_is_written():
    # Measured 4 August: with the frame loop paused and the lock held, every
    # retry over a two second budget came back 0x1006.  The body is busy for as
    # long as it is in live view at all, so the stream has to actually stop.
    sdk = _FakeSDK(speed=8000, iso=400)
    win, LiveViewWindow = _live_view_stub(sdk)
    stream = _FakeStream()
    win._stream = stream
    started = []
    win._start_live_view_stream = lambda: (started.append(True),
                                           setattr(win, "_stream", _FakeStream()),
                                           True)[-1]

    target = win._shutter_combo.findData(500_000)
    win._shutter_combo.setCurrentIndex(target)
    LiveViewWindow._on_shutter_changed(win, target)

    assert stream.stopped, "live view was left running for the write"
    assert started, "live view was not started again afterwards"
    assert ("shutter", 500_000) in sdk.written


def test_a_stream_that_will_not_restart_leaves_the_window_stopped():
    # Better the stopped state the buttons describe than a worker polling a
    # stream that is not there.
    sdk = _FakeSDK(speed=8000, iso=400)
    win, LiveViewWindow = _live_view_stub(sdk)
    win._stream = _FakeStream()
    win._start_live_view_stream = lambda: False
    win.stopped = False
    win.stop_stream = lambda: setattr(win, "stopped", True)

    target = win._shutter_combo.findData(500_000)
    win._shutter_combo.setCurrentIndex(target)
    LiveViewWindow._on_shutter_changed(win, target)

    assert ("shutter", 500_000) in sdk.written, "the setting still had to land"
    assert win.stopped, "the window was left thinking it was streaming"


def test_the_write_waits_for_the_frame_read_already_in_flight():
    # 4 August: two ISO changes failed with "still in use after 3s".  Not the
    # body refusing - the worker was inside a read_frame that had not returned,
    # holding the camera lock, so racing the lock spent the whole timeout on a
    # wait that could not succeed.
    sdk = _FakeSDK(speed=8000, iso=400)
    win, LiveViewWindow = _live_view_stub(sdk)
    win._stream = _FakeStream()
    win._start_live_view_stream = lambda: setattr(win, "_stream", _FakeStream()) or True

    waited = []

    class _Worker:
        def pause(self): pass
        def resume(self): pass
        def stop(self): pass
        def set_stream(self, stream): pass

        def wait_idle(self, timeout):
            waited.append(timeout)
            return True

    win._worker = _Worker()
    target = win._iso_combo.findData(1600)
    win._iso_combo.setCurrentIndex(target)
    LiveViewWindow._on_iso_changed(win, target)

    assert waited, "the write went for the lock without waiting for the read"
    assert ("iso", 1600) in sdk.written


def test_a_worker_is_not_resumed_onto_a_stream_that_is_gone():
    # Resuming it points the loop at the stream stopped for the write, and
    # reading from that is what parks a thread inside the SDK holding the lock.
    sdk = _FakeSDK(speed=8000, iso=400)
    win, LiveViewWindow = _live_view_stub(sdk)
    win._stream = _FakeStream()
    win._start_live_view_stream = lambda: False
    win.stop_stream = lambda: None
    calls = []

    class _Worker:
        def pause(self): calls.append("pause")
        def resume(self): calls.append("resume")
        def stop(self): calls.append("stop")
        def wait_idle(self, timeout): return True
        def set_stream(self, stream): calls.append("set_stream")

    win._worker = _Worker()
    target = win._iso_combo.findData(1600)
    win._iso_combo.setCurrentIndex(target)
    LiveViewWindow._on_iso_changed(win, target)

    assert "resume" not in calls, "resumed onto a stopped stream"
    assert "stop" in calls


def test_the_frame_loop_waits_for_the_body_rather_than_spinning():
    """Measured: the X-T4 emits a frame every ~200 ms and will not be hurried.

    Polling with no gap, or at 5, 20, 40 or 80 ms, all returned the same 40
    frames in eight seconds, at every frame size.  The old 5/10 ms loop
    therefore asked five times per frame and discarded four answers, each a USB
    round trip taken with the camera lock held - the traffic the shooting path
    competed with, and the pressure that let a waiting thread starve.
    """
    from solareclipseworkbench import liveview

    # Anything much below the frame interval is back to spinning.
    assert liveview._POLL_AFTER_FRAME_MS >= 100
    # ...and anything at or above it starts dropping frames: at 150/60 the
    # same twelve seconds yielded 52 frames instead of 60.
    assert liveview._POLL_AFTER_FRAME_MS < 150
    assert liveview._POLL_WHEN_EMPTY_MS < liveview._POLL_AFTER_FRAME_MS
    assert liveview._POLL_WHEN_QUIET_MS > liveview._POLL_WHEN_EMPTY_MS


def test_ensure_ready_is_asked_of_the_adapter_not_the_bare_handle():
    """The AttributeError that aborted the GUI at 15:59 on 4 August.

    The window unwraps the adapter and keeps the SDK camera to stream from, so
    an adapter method called on it does not exist.  PyQt turns an exception
    escaping a slot into qFatal(), so this did not raise - it killed the
    process, two minutes before second contact in the simulation.
    """
    from types import SimpleNamespace

    from solareclipseworkbench.liveview import LiveViewWindow

    asked = []
    sdk_camera = object()               # no ensure_ready, like the real handle
    adapter = SimpleNamespace(
        _sdk_cam=sdk_camera,
        ensure_ready=lambda priority, allow_shot=True, why="": asked.append(
            (priority, allow_shot, why)) or True)

    win = SimpleNamespace(_adapter=adapter, _camera=sdk_camera)
    LiveViewWindow._ensure_ready(win, 1)

    assert asked, "went to the unwrapped handle, which has no such method"
    priority, allow_shot, why = asked[0]
    assert allow_shot is False, "a preview must never fire the shutter"
    assert why == "live view"


def test_a_bare_sdk_camera_still_gets_unblocked():
    # The constructor allows a window built on a raw SDK camera, so that path
    # must not depend on the adapter contract.
    from types import SimpleNamespace

    from solareclipseworkbench import liveview as liveview_mod

    called = []
    bare = object()
    win = SimpleNamespace(_adapter=bare, _camera=bare)
    original = liveview_mod.sdk_recovery.unblock
    liveview_mod.sdk_recovery.unblock = lambda cam, priority, allow_shot=True, why="": (
        called.append((cam, allow_shot)) or True)
    try:
        liveview_mod.LiveViewWindow._ensure_ready(win, 1)
    finally:
        liveview_mod.sdk_recovery.unblock = original

    assert called == [(bare, False)]


def test_the_schedule_has_no_claim_on_the_exposure_controls():
    """They were locked for the whole run, then near frames, then near frames
    but less so - and every version ended with the person at the telescope
    shouting "leave the controls to me".  They are right: a write near a frame
    risks that frame, and it is their frame.  The mechanism is gone; the only
    disable left is while a write is physically in flight.
    """
    from solareclipseworkbench.liveview import LiveViewWindow

    assert not hasattr(LiveViewWindow, "set_schedule_owns_exposure"), \
        "the schedule grew a claim on the controls again"


def test_a_refused_change_puts_the_dropdown_back():
    """Reported 4 August: the dropdown showed the new value, the status line
    said it could not be set, and the picture did not change.

    All three were true at once.  The lock-timeout path returned before the
    resync, so the control kept the value that was clicked while the body
    stayed where it was - a control lying about the camera it controls.
    """
    import threading

    from solareclipseworkbench.liveview import LiveViewWindow

    sdk = _FakeSDK(speed=8000, iso=400)          # body is on 1/8000
    win, _ = _live_view_stub(sdk)
    win._stream = None
    win._worker = None

    # A plain Lock, not an RLock: an RLock is reentrant, so the thread holding
    # it takes it again and the write would succeed.
    held = threading.Lock()
    held.acquire()
    win._usb_lock = held

    target = win._shutter_combo.findData(500_000)
    win._shutter_combo.setCurrentIndex(target)    # the user clicks 1/2 s

    assert LiveViewWindow._write_exposure(
        win, lambda: sdk.set_shutter_speed(500_000), "shutter speed", "") is False
    assert ("shutter", 500_000) not in sdk.written

    shown = win._shutter_combo.currentData()
    assert shown == 8000, ("the dropdown kept %r while the body is on 8000"
                           % (shown,))


def test_a_speed_the_list_does_not_offer_is_still_shown():
    # The body can sit on a value that is not in the dropdown, and then the
    # resync used to do nothing at all - leaving the control showing the last
    # thing clicked, which is worse than showing an unfamiliar number.
    from solareclipseworkbench.liveview import LiveViewWindow

    sdk = _FakeSDK(speed=8000, iso=400)
    win, _ = _live_view_stub(sdk)

    sdk.speed = 12345                       # a value no list would offer
    assert win._shutter_combo.findData(12345) < 0
    win._refresh_exposure()

    assert win._shutter_combo.currentData() == 12345, \
        "the control kept showing something the camera is not set to"


def test_the_window_keeps_painting_while_a_write_waits():
    """4 August: setting the ISO hung the application.

    The write waits for the frame in flight, then for the camera lock - up to
    eighteen seconds between them.  Waiting is correct; waiting on the thread
    that paints is what looked like a hang.
    """
    import threading
    import time

    from solareclipseworkbench.liveview import LiveViewWindow

    started = threading.Event()
    release = threading.Event()

    def slow_write(action, what, hint):
        started.set()
        release.wait(5)
        return True

    win = SimpleNamespace(
        _write_exposure=slow_write,
        _shutter_combo=SimpleNamespace(setEnabled=lambda e: None),
        _iso_combo=SimpleNamespace(setEnabled=lambda e: None),
        _status_bar=SimpleNamespace(showMessage=lambda *a, **k: None),
        write_finished=SimpleNamespace(emit=lambda: None),
        _write_thread=None)

    began = time.perf_counter()
    LiveViewWindow._write_in_background(win, lambda: None, "ISO", "")
    returned_after = time.perf_counter() - began

    assert started.wait(2), "the write never started"
    assert returned_after < 0.5, (
        "the caller was blocked for %.1fs - that is the frozen window"
        % returned_after)
    release.set()
    win._write_thread.join(5)


def test_two_writes_do_not_run_at_once():
    # Two threads writing exposures to an SDK that is not thread-safe is how a
    # session dies.
    import threading

    from solareclipseworkbench.liveview import LiveViewWindow

    release = threading.Event()
    calls = []

    def slow_write(action, what, hint):
        calls.append(what)
        release.wait(5)
        return True

    messages = []
    win = SimpleNamespace(
        _write_exposure=slow_write,
        _shutter_combo=SimpleNamespace(setEnabled=lambda e: None),
        _iso_combo=SimpleNamespace(setEnabled=lambda e: None),
        _status_bar=SimpleNamespace(showMessage=lambda m, *a: messages.append(m)),
        write_finished=SimpleNamespace(emit=lambda: None),
        _write_thread=None)

    LiveViewWindow._write_in_background(win, lambda: None, "ISO", "")
    LiveViewWindow._write_in_background(win, lambda: None, "shutter speed", "")

    assert calls == ["ISO"], "started a second write over the first"
    assert any("Still setting" in m for m in messages)
    release.set()
    win._write_thread.join(5)


def test_the_window_builds_before_its_first_camera_answer():
    """6 August: building the widgets kicks off the first exposure read, and
    its guard flags were initialised after the build - the window died on an
    AttributeError before it ever opened.  The stub tests bind methods one by
    one, so only constructing the real window catches an ordering bug."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PyQt6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from solareclipseworkbench.liveview import LiveViewWindow

    class _SilentCamera:
        """Answers nothing: every probe fails, none of it fatally."""

    win = LiveViewWindow(_SilentCamera())
    assert win._exposure_read_busy is not None
    win.deleteLater()


def test_stopping_live_view_puts_the_schedules_exposure_back():
    """6 August rehearsal: the C2 beads exposure load fell inside live view's
    0x1006 window, and the relay burst after it would have fired at stale
    settings.  The accepted contract is frames lost while the window is open
    and nothing after - so the teardown re-applies the schedule's last ask,
    after the priority handshake, never before."""
    from solareclipseworkbench.liveview import LiveViewWindow

    calls = []
    win = SimpleNamespace(
        _camera=SimpleNamespace(
            vanished=False,
            set_priority=lambda p: calls.append(("priority", p))),
        _adapter=SimpleNamespace(
            apply_pending=lambda: calls.append(("catchup",)) or True),
        _thread=None,
        _stalled_threads=[],
    )

    LiveViewWindow._finish_stop(win, None, None, None)

    assert ("catchup",) in calls, "the stale exposure survived the stream"
    assert calls.index(("catchup",)) == len(calls) - 1, \
        "exposure went back before the body had its priority"


# --------------------------------------------- the blocked-settings cache

def _configure_stub(sdk, pending=None):
    import threading
    return SimpleNamespace(name="X-T4", _lock=threading.RLock(), _sdk=sdk,
                           _applied_iso=None, _applied_speed=None,
                           _frame_busy_until=0.0, _pending=dict(pending or {}),
                           recover_session=lambda: False,
                           _remember=lambda **kw: None)


def test_a_blocked_setting_is_cached_for_the_next_configure():
    """The 6 August contract: frames lost while live view is open are the
    person's to lose, but nothing after it may fire on stale settings.  So a
    write refused with busy stays cached, latest value per setting."""
    import pytest as _pytest
    from fujixsdk.camera import BusyError
    from solareclipseworkbench.camera import CameraError
    from solareclipseworkbench.fuji_camera import FujiCamera

    class _BusySDK:
        def set_shutter_speed(self, value):
            raise BusyError("0x1006: live view has the body")

    stub = _configure_stub(_BusySDK())
    with _pytest.raises(CameraError):
        FujiCamera.configure(stub, budget_s=0.05, shutter_speed="1/500")

    assert stub._pending == {"shutter_speed": "1/500"}


def test_cached_settings_ride_along_with_the_next_configure():
    from solareclipseworkbench.fuji_camera import FujiCamera

    class _OkSDK:
        def __init__(self):
            self.written = []

        def set_shutter_speed(self, value):
            self.written.append(("speed", value))

        def set_iso(self, value):
            self.written.append(("iso", value))

    sdk = _OkSDK()
    stub = _configure_stub(sdk, pending={"shutter_speed": "1/500"})
    FujiCamera.configure(stub, iso=100)   # asks only for ISO

    assert stub._pending == {}, "the cached shutter speed was not caught up"
    assert ("iso", 100) in sdk.written
    assert any(name == "speed" for name, _ in sdk.written)


def test_the_catchup_is_skipped_when_the_budget_cannot_fit_a_write():
    # Just before a relay burst there may be less than one write's worth of
    # time; arriving late to the burst is not a rescue, so nothing is tried.
    from solareclipseworkbench.fuji_camera import FujiCamera

    called = []
    stub = SimpleNamespace(name="X-T4", _pending={"iso": 100},
                           configure=lambda **kw: called.append(kw))

    assert FujiCamera.apply_pending(stub, budget_s=0.1) is False
    assert called == []

    FujiCamera.apply_pending(stub, budget_s=0.8)
    assert called, "a workable budget still did not try"


# ----------------------------------------------- the gap-bounded drop guard

def test_a_frame_waits_longer_when_the_schedule_has_room():
    """6 August, twice: the corona singles between the ladders were dropped
    at 1.5s against a body still flushing the contact burst - with nothing
    due behind them, so the wait that would have saved them was free."""
    from solareclipseworkbench import camera as camera_mod

    def _budget(gap):
        scheduler = SimpleNamespace()
        camera_mod.HARDWARE['scheduler'] = scheduler
        try:
            original = camera_mod.seconds_to_next_camera_job
            camera_mod.seconds_to_next_camera_job = lambda s, now=None: gap
            try:
                return camera_mod._lock_wait_budget()
            finally:
                camera_mod.seconds_to_next_camera_job = original
        finally:
            camera_mod.HARDWARE.pop('scheduler', None)

    # Room behind it: wait longer, but never past the ceiling.
    assert _budget(20.0) == camera_mod._MAX_LOCK_WAIT_WITH_ROOM_S
    assert _budget(None) == camera_mod._MAX_LOCK_WAIT_WITH_ROOM_S

    # A frame due right behind: a rescue here would make that one late, so
    # this must stay the old strict wait, never less.
    assert _budget(1.0) == camera_mod._MAX_LOCK_WAIT_S
    assert _budget(0.0) == camera_mod._MAX_LOCK_WAIT_S

    # In between, the wait is the gap less the margin kept for this frame.
    assert _budget(4.0) == pytest.approx(4.0 - camera_mod._NEXT_JOB_MARGIN_S)


def test_the_drop_guard_falls_back_when_there_is_no_schedule():
    # No script loaded, or a scheduler that will not answer: the guard must
    # still work, at the strict wait it has always used.
    from solareclipseworkbench import camera as camera_mod

    camera_mod.HARDWARE.pop('scheduler', None)
    assert camera_mod._lock_wait_budget() == camera_mod._MAX_LOCK_WAIT_S

    camera_mod.HARDWARE['scheduler'] = SimpleNamespace()
    original = camera_mod.seconds_to_next_camera_job

    def _boom(scheduler, now=None):
        raise RuntimeError("scheduler is shutting down")

    camera_mod.seconds_to_next_camera_job = _boom
    try:
        assert camera_mod._lock_wait_budget() == camera_mod._MAX_LOCK_WAIT_S
    finally:
        camera_mod.seconds_to_next_camera_job = original
        camera_mod.HARDWARE.pop('scheduler', None)


def test_the_teardown_leaves_the_camera_alone_when_a_frame_is_due():
    """6 August: live view stopped because a frame needed the camera - and
    the catch-up added on the way out took the camera lock the frame was
    waiting for, so the frame it made way for was never taken.  Nothing is
    lost by skipping: the cache rides along with that frame's own configure."""
    from solareclipseworkbench import liveview as liveview_mod
    from solareclipseworkbench.liveview import LiveViewWindow

    calls = []

    def _win():
        return SimpleNamespace(
            _camera=SimpleNamespace(vanished=False,
                                    set_priority=lambda p: calls.append("priority")),
            _adapter=SimpleNamespace(
                apply_pending=lambda: calls.append("catchup")),
            _thread=None, _stalled_threads=[])

    original = liveview_mod._seconds_to_next_camera_job
    try:
        # A frame is imminent: hand the camera straight back, catch up later.
        liveview_mod._seconds_to_next_camera_job = lambda: 1.0
        LiveViewWindow._finish_stop(_win(), None, None, None)
        assert calls == ["priority"], "the catch-up raced the frame"

        # Nothing due: the catch-up runs, as it must.
        calls.clear()
        liveview_mod._seconds_to_next_camera_job = lambda: None
        LiveViewWindow._finish_stop(_win(), None, None, None)
        assert calls == ["priority", "catchup"]
    finally:
        liveview_mod._seconds_to_next_camera_job = original


def test_the_crosshair_marks_the_centre_without_covering_it():
    # The sun is centred by the gap, so the very centre must stay clear.
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QPixmap
    from PyQt6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from solareclipseworkbench.liveview import _draw_crosshair

    pixmap = QPixmap(400, 300)
    pixmap.fill()
    before = pixmap.toImage().pixel(200, 150)
    _draw_crosshair(pixmap)
    after = pixmap.toImage()

    assert after.pixel(200, 150) == before, "the crosshair covered the centre"
    assert after.pixel(20, 150) != before, "no horizontal line was drawn"
    assert after.pixel(200, 20) != before, "no vertical line was drawn"


def test_the_loupe_magnifies_without_smoothing_and_scores_its_own_patch():
    """A preview scaled into a dock is a downsample, and a downsampled soft
    edge looks much like a downsampled sharp one.  The loupe shows the patch
    at native pixels, nearest-neighbour, and scores that patch rather than
    the whole frame - focusing on a limb barely moves a whole-frame number."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QImage
    from PyQt6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from solareclipseworkbench.liveview import _Loupe

    loupe = _Loupe()
    assert loupe._score is None

    patch = QImage(_Loupe.PATCH_PX, _Loupe.PATCH_PX, QImage.Format.Format_RGB888)
    patch.fill(0)
    loupe.set_patch(patch, 12.5)
    assert loupe._score == 12.5
    assert loupe._peak == 12.5
    # Magnified, not shrunk: the patch is smaller than the widget.
    assert loupe._pixmap.width() > patch.width()

    # The peak is what the percentage is measured against, so it must hold.
    loupe.set_patch(patch, 6.0)
    assert loupe._peak == 12.5

    loupe.reset()
    assert loupe._score is None and loupe._peak == 0.0


def test_peaking_colour_and_sensitivity_reach_the_overlay():
    # Red is the one colour a filtered sun already is, and a low-contrast
    # disc needs a lower threshold than a daylight scene.
    import numpy as np
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from solareclipseworkbench.liveview import (PEAKING_COLOURS,
                                                PEAKING_SENSITIVITY,
                                                _peaking_mask_from_lap)

    lap = np.full((8, 8), 20, dtype=np.int16)   # edges of middling strength

    # 'high' sensitivity (threshold 12) paints them; 'low' (60) does not.
    painted = _peaking_mask_from_lap(lap, 10, 10, PEAKING_COLOURS["cyan"],
                                     PEAKING_SENSITIVITY["high"])
    bare = _peaking_mask_from_lap(lap, 10, 10, PEAKING_COLOURS["cyan"],
                                  PEAKING_SENSITIVITY["low"])
    cyan = PEAKING_COLOURS["cyan"]
    assert painted.pixelColor(5, 5).blue() == cyan.blue()
    assert painted.pixelColor(5, 5).red() == 0, "cyan came out red"
    assert bare.pixelColor(5, 5).alpha() == 0, "a low threshold still painted"


# ------------------------------------------------- the jobs table's What column

def test_a_label_is_never_worth_the_table():
    """The What column is built once per row while a run is being watched.

    Whatever a job carries - missing args, a settings object that raises, a
    command nobody wrote a case for - the label degrades to something
    printable instead of taking the jobs table down.
    """
    from solareclipseworkbench import job_text

    class _Exploding:
        @property
        def shutter_speed(self):
            raise RuntimeError("no")

    nasty = [
        SimpleNamespace(func=SimpleNamespace(__name__="take_picture"), args=None),
        SimpleNamespace(func=SimpleNamespace(__name__="take_bracket"), args=()),
        SimpleNamespace(func=None, args=(1, 2)),
        SimpleNamespace(args=("only args",)),
        SimpleNamespace(func=SimpleNamespace(__name__="take_picture"),
                        args=(object(), _Exploding())),
        object(),
    ]
    for job in nasty:
        got = job_text.describe_job(job)
        assert isinstance(got, str), f"{job!r} produced {got!r}"

    # and the same for raw fields off a script line
    for command, fields in (("take_bracket", [None]), ("voice_prompt", []),
                            ("relay_burst", ["not a number"]),
                            ("", []), ("who_knows", [1, 2, 3])):
        assert isinstance(job_text.describe(command, fields), str)


def test_the_baked_in_clock_goes_but_the_words_stay():
    from solareclipseworkbench import job_text

    assert job_text.strip_baked_time(
        "Focus check, uneclipsed disc @ 17:15:56, sun 22.3 deg") \
        == "Focus check, uneclipsed disc, sun 22.3 deg"
    assert job_text.strip_baked_time(
        "Pre-arm S1 for the C2 burst @ BEADS_C2_END-6.4 s, sun 8.7 deg") \
        == "Pre-arm S1 for the C2 burst, sun 8.7 deg"
    assert job_text.strip_baked_time("No clock here") == "No clock here"
    assert job_text.strip_baked_time("") == ""
    assert job_text.strip_baked_time(None) is None
