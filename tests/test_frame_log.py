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

    def stop_stream(self):
        self._thread = None
        self.events.append("stop")

    def start_stream(self):
        self._thread = object()
        self.events.append("start")


def test_live_view_pauses_for_totality_and_comes_back():
    # The controller calls this every clock tick.  The restored window did not
    # have it, and the clock died with AttributeError the moment a live view was
    # open - reported 3 August, 22:11.
    from solareclipseworkbench.liveview import LiveViewWindow

    window = _FakeLiveView()
    LiveViewWindow.set_totality_paused(window, True)
    LiveViewWindow.set_totality_paused(window, True)     # every tick, not just the edge
    LiveViewWindow.set_totality_paused(window, False)

    assert window.events == ["stop", "start"]


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
    win._refresh_exposure = lambda: LiveViewWindow._refresh_exposure(win)
    win._write_exposure = lambda action, what, hint: LiveViewWindow._write_exposure(
        win, action, what, hint)
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
    assert max(values) <= 30_000_000        # nothing past 30s
    assert min(values) >= 125               # nothing faster than 1/8000


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
