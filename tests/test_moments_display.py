"""The limb correction has to be visible in the times, not just in the model.

The C2 and C3 rows show the limb-corrected contacts when the correction is on,
because that is what an eclipse script schedules against.  The bead windows are
shown too: a contact burst is aimed at a window, not at a contact.

The site constants below are the production site, chosen because the correction
is worth whole seconds there and the assertions have something to bite on.
"""

import datetime
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from astropy.time import Time
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from solareclipseworkbench import gui, limb_correction
from solareclipseworkbench.gui import SolarEclipseView
from solareclipseworkbench.reference_moments import calculate_reference_moments

# The production site, where the limb correction is known to be worth seconds.
LON, LAT, ALT = -4.5289, 42.0095, 740.0
ECLIPSE = Time("2026-08-12 00:00:00")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _own_settings(tmp_path, monkeypatch):
    """Keep the window out of the user's real settings file.

    The view restores its dock layout from disk on construction, so without this
    the docks test passes or fails according to where someone last dragged a
    pane in the real application - which is not a property of the code.
    """
    monkeypatch.setattr(gui, "SETTINGS_PATH", tmp_path / "settings.ini")


@pytest.fixture
def view(app):
    widget = SolarEclipseView()
    yield widget
    widget.deleteLater()
    limb_correction.set_enabled(True)


def _shown(view, enabled: bool) -> None:
    limb_correction.set_enabled(enabled)
    moments, magnitude, kind = calculate_reference_moments(LON, LAT, ALT, ECLIPSE)
    view.show_reference_moments(moments, magnitude, kind)


def test_the_correction_moves_the_contact_times_on_screen(view):
    _shown(view, True)
    corrected_c2 = view.c2_time_utc_label.text()
    corrected_c3 = view.c3_time_utc_label.text()

    _shown(view, False)
    assert view.c2_time_utc_label.text() != corrected_c2
    assert view.c3_time_utc_label.text() != corrected_c3


def test_the_bead_windows_are_shown_with_their_duration(view):
    _shown(view, True)

    for label in (view.beads_c2_label, view.beads_c3_label):
        text = label.text()
        assert " - " in text, text
        assert text.endswith("s)"), text


def test_the_bead_rows_say_why_they_are_empty(view):
    _shown(view, False)

    # A blank here would read like a solve that failed rather than a correction
    # that is switched off.
    assert view.beads_c2_label.text() == "correction off"
    assert view.beads_c3_label.text() == "correction off"


def test_geometry_and_beads_are_open_and_stacked(view):
    view.show()
    QApplication.processEvents()

    for dock in (view.geometry_dock, view.beads_dock):
        assert dock.objectName()
        assert dock.isVisible(), dock.objectName()
    # Stacked, not tabbed: both are pictures of the same moment and both are
    # worth watching at once.
    assert not view.tabifiedDockWidgets(view.geometry_dock)


def test_the_mount_starts_closed(view):
    # A mount is the exception, not the rule.  An empty panel taking a quarter of
    # the window is worse than a toolbar button that opens it on request.
    view.show()
    QApplication.processEvents()

    assert view.mount_dock.objectName()
    assert not view.mount_dock.isVisible()
    assert view.mount_dock_action.text() == "Mount"


def test_the_relay_button_sits_next_to_the_cameras(view):
    labels = [action.text() for action in view.toolbar.actions() if action.text()]
    assert labels.index("Relay") == labels.index("Camera(s)") + 1


def _controller(view, cameras, scheduler=None):
    """A stand-in controller.  The real methods under test are called unbound
    with this as self, so anything they call on self has to be attached here."""
    from types import SimpleNamespace
    from solareclipseworkbench import gui as gui_mod
    c = SimpleNamespace(
        view=view, scheduler=scheduler, _live_view_window=None,
        _live_view_yield_timer=None, _live_view_yielded=False,
        model=SimpleNamespace(camera_overview=SimpleNamespace(
            camera_overview_dict=cameras)),
    )
    c._seconds_to_next_frame = lambda: gui_mod.SolarEclipseController._seconds_to_next_frame(c)
    c._yield_live_view_for_frames = lambda: gui_mod.SolarEclipseController._yield_live_view_for_frames(c)
    c._stop_live_view_yielding = lambda: gui_mod.SolarEclipseController._stop_live_view_yielding(c)
    return c


def test_live_view_opens_for_a_fuji_over_the_sdk(view, monkeypatch):
    # The Fuji shoots through its own SDK, so it is not a GPhotoCameraAdapter and
    # was filtered out of the live-view list entirely - the window then said "no
    # camera connected" while the camera was visibly firing.  Reported on the
    # bench, 3 August; the SDK preview had been written in February and stranded.
    from types import SimpleNamespace
    from solareclipseworkbench import gui as gui_mod

    opened = {}
    fuji = SimpleNamespace(name="Fuji Fujifilm X-T4", _sdk_cam=object())
    controller = _controller(view, {"Fuji Fujifilm X-T4": fuji})
    # The stand-in is a namespace, so the hand-off has to be stubbed on it
    # rather than on the class.
    controller._open_fuji_live_view = lambda cam: opened.update(camera=cam)

    gui_mod.SolarEclipseController._open_live_view(controller)

    assert opened["camera"] is fuji


def _scheduler_with_next_frame_in(seconds):
    from types import SimpleNamespace
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    return SimpleNamespace(get_jobs=lambda: [SimpleNamespace(next_run_time=when)])


def test_live_view_is_refused_while_a_script_is_loaded(view, monkeypatch):
    # Live view took the camera off the USB bus mid-run twice on 4 August, the
    # second time after it was made to serialise on the camera lock - so the API
    # calls were not the whole of it.  Until that is understood on a bench, the
    # two do not happen together.
    from types import SimpleNamespace
    from solareclipseworkbench import gui as gui_mod

    shown = {}
    monkeypatch.setattr(gui_mod.QMessageBox, "warning",
                        lambda *a, **k: shown.update(title=a[1], body=a[2]))
    fuji = SimpleNamespace(name="Fuji Fujifilm X-T4", _sdk_cam=object())
    controller = _controller(view, {"X-T4": fuji}, scheduler=_scheduler_with_next_frame_in(3))

    gui_mod.SolarEclipseController._open_fuji_live_view(controller, fuji)

    assert "script is loaded" in shown["title"].lower()


def test_live_view_opens_when_no_script_is_loaded(view, monkeypatch):
    # Focusing happens before the script goes on; with no schedule there is
    # nothing for live view to collide with.
    from types import SimpleNamespace
    from solareclipseworkbench import gui as gui_mod

    monkeypatch.setattr(gui_mod.QMessageBox, "warning",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not refuse three minutes out")))
    opened = {}
    from PyQt6.QtWidgets import QDockWidget

    class _Win(QDockWidget):
        """A real dock, so addDockWidget accepts it; the SDK stream is not started."""
        _thread = None

        def __init__(self, sdk, parent):
            super().__init__("Live View", parent)
            opened["sdk"] = sdk

    import solareclipseworkbench.liveview as lv
    monkeypatch.setattr(lv, "LiveViewWindow", _Win)

    fuji = SimpleNamespace(name="Fuji Fujifilm X-T4", _sdk_cam=object())
    controller = _controller(view, {"X-T4": fuji}, scheduler=None)
    controller.view = view

    gui_mod.SolarEclipseController._open_fuji_live_view(controller, fuji)

    # The adapter, not the bare SDK handle: the window serialises on the
    # camera's lock, and without it a preview read and a scheduled shot end up
    # in the SDK together and drop the session.
    assert opened["sdk"] is fuji


def test_live_view_still_says_so_when_nothing_is_connected(view, monkeypatch):
    from solareclipseworkbench import gui as gui_mod

    shown = {}
    monkeypatch.setattr(gui_mod.QMessageBox, "warning",
                        lambda *a, **k: shown.update(title=a[1], body=a[2]))

    gui_mod.SolarEclipseController._open_live_view(_controller(view, {}))

    assert "no camera" in shown["title"].lower()


def test_the_window_fits_a_1440_wide_screen(view):
    # It demanded 1714 while the laptop is 1440 logical, so the window could not
    # fit the display and every panel in it was crushed - which is what "the
    # screen is a mess" was.  Reported 3 August.
    assert view.minimumSizeHint().width() <= 1440


def test_the_cameras_panel_is_open_and_readable(view):
    # It was wedged into the input row, squeezed to nothing by a moments box with
    # a 600px floor.  Along the bottom it has the full width instead.
    view.show()
    QApplication.processEvents()

    assert view.camera_dock.isVisible()
    assert view.dockWidgetArea(view.camera_dock) == Qt.DockWidgetArea.BottomDockWidgetArea


def test_the_contact_times_scroll_rather_than_clip_or_dictate_the_width(view):
    # A 544px floor stopped the labels clipping to "First conta..." but made the
    # panel most of a laptop screen for a table of ten numbers.  Scrolling gets
    # both: it can be given any width and the content is still reachable.
    from PyQt6.QtWidgets import QScrollArea
    assert isinstance(view.moments_dock.widget(), QScrollArea)
    assert view.moments_dock.widget().widgetResizable()
    assert view.moments_dock.minimumSizeHint().width() < 250


def test_the_contact_times_are_a_dock_that_can_be_closed(view):
    view.show()
    QApplication.processEvents()
    assert view.moments_dock.isVisible()
    assert view.moments_dock_action.text() == "Contact times"


def test_utc_survives_the_column_being_dropped(view):
    # Published predictions are in UTC, so it is what a cross-check is against.
    # The column is gone; the number is on the local time as a tooltip.
    _shown(view, True)
    assert "UTC" in view.c2_time_local_label.toolTip()


def test_the_window_never_opens_larger_than_the_display(view):
    # A window whose minimum exceeds the screen cannot be shrunk: the edge is off
    # the display and there is nothing to drag, so a bad saved layout locks the
    # user out of fixing it.  Reported 3 August - "bigger than my screen and I
    # can't resize it".
    from PyQt6.QtGui import QGuiApplication
    view.show()
    QApplication.processEvents()
    available = QGuiApplication.primaryScreen().availableGeometry()

    # It cannot go below its own minimum, so the invariant is that it is clamped
    # as far as it can be.  That the minimum itself fits a real display is
    # test_the_window_fits_a_1440_wide_screen; the test screen here is 800x600.
    assert view.width() <= max(view.minimumWidth(), available.width())
    assert view.height() <= max(view.minimumHeight(), available.height())


def test_the_camera_strip_can_be_pulled_down_to_one_row(view):
    # With one body connected there is one row to show.  A 110px floor meant the
    # strip kept taking height from the schedule that it had nothing to put in.
    assert view.camera_overview.minimumHeight() == 0


def test_both_live_view_windows_answer_the_same_contract():
    # The controller drives whichever window the connected camera needs, and
    # restoring one that predated set_totality_paused crashed the clock on the
    # first tick.  Whatever the controller calls, both must answer.
    from solareclipseworkbench.gui import LiveViewWindow as GPhotoLiveView
    from solareclipseworkbench.liveview import LiveViewWindow as FujiLiveView

    for name in ("set_totality_paused", "is_streaming", "start_stream", "stop_stream"):
        for window in (GPhotoLiveView, FujiLiveView):
            assert callable(getattr(window, name, None)), \
                f"{window.__module__}.{window.__name__} is missing {name}()"


def test_opening_the_mount_does_not_push_the_window_off_the_screen(view):
    # Docked in the right column its controls wanted 362x496, which took the
    # window's minimum from 1215 to 1578 - wider than the laptop - so opening it
    # sent the window off the display and the panel out of reach.  Reported
    # 4 August: "when fullscreening the mount it out of view".
    view.show()
    QApplication.processEvents()
    before = view.minimumWidth()

    view.mount_dock.show()
    QApplication.processEvents()

    # Opening it costs some width, but a bounded amount: its contents want
    # 362x474 and before this it charged all of that, taking the window's
    # minimum from 1215 to 1578 on a 1440 laptop.  The panel scrolls now, so it
    # is charged what a scroll area needs rather than what its contents want.
    assert view.minimumWidth() - before < 150, "the mount must not widen the window by its full contents"
    assert view.mount_dock.minimumSizeHint().width() < 200, \
        "a dock that cannot be smaller than its contents pushes the window off screen"


def test_the_beads_toggle_reads_like_its_neighbours(view):
    # It was the only toolbar toggle with an icon, so Qt drew it as a picture
    # among words - it read as a different kind of control rather than the same
    # one.
    assert view.beads_action.text() == "Beads"
    assert view.beads_action.icon().isNull()


def test_the_number_columns_use_a_fixed_width_face(view):
    # A proportional font gives every digit a different width, so a countdown
    # jitters sideways as it ticks and the times do not line up under each
    # other.  Reported 4 August: "columns with numbers are kinda sloppy".
    from PyQt6.QtGui import QFontDatabase
    fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()

    for label in (view.c2_time_local_label, view.c2_countdown_label,
                  view.c2_azimuth_label, view.c2_altitude_label,
                  view.beads_c2_label, view.time_label_local):
        assert label.font().family() == fixed, label.objectName()


def test_the_bead_rows_say_they_are_a_window(view):
    # They hold a start and an end.  In a column of single times that read as
    # two contacts rather than the span between them.
    from PyQt6.QtWidgets import QLabel
    texts = [w.text() for w in view.findChildren(QLabel)]
    assert "Beads window (C2)" in texts
    assert "Beads window (C3)" in texts


def test_a_bead_window_reads_as_a_span_with_its_length(view):
    _shown(view, True)
    text = view.beads_c2_label.text()
    assert "-" in text and text.strip().endswith("s)"), text
