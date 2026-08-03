"""The limb correction has to be visible in the times, not just in the model.

The C2 and C3 rows show the limb-corrected contacts when the correction is on,
because that is what an eclipse script schedules against.  The bead windows are
shown too: a contact burst is aimed at a window, not at a contact.

The site constants below are the production site, chosen because the correction
is worth whole seconds there and the assertions have something to bite on.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from astropy.time import Time
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


def test_the_three_docks_exist_and_are_visible(view):
    view.show()
    QApplication.processEvents()

    for dock in (view.geometry_dock, view.beads_dock, view.mount_dock):
        assert dock.objectName()
        assert dock.isVisible(), dock.objectName()


def test_the_relay_button_sits_next_to_the_cameras(view):
    labels = [action.text() for action in view.toolbar.actions() if action.text()]
    assert labels.index("Relay") == labels.index("Camera(s)") + 1
