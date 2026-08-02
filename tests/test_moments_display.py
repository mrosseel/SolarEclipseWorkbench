"""The limb correction has to be visible in the times, not just in the model.

The checkbox flipped `limb_correction.set_enabled`, the moments were recomputed,
and the C2/C3 rows did not move — because the correction *adds* C2_LIMB and
C3_LIMB rather than changing C2 and C3, and the rows read the latter. From the
outside the control looked broken.

At this site C3 is 3.5 s earlier than a smooth disc says, which is most of a
bead burst, and the production script schedules against the corrected moment.
A display showing the disc value while the script uses the real one is worse
than no display at all.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from astropy.time import Time
from PyQt6.QtWidgets import QApplication

from solareclipseworkbench import limb_correction
from solareclipseworkbench.gui import SolarEclipseView
from solareclipseworkbench.reference_moments import calculate_reference_moments

# The production site, where the limb correction is known to be worth seconds.
LON, LAT, ALT = -4.5289, 42.0095, 740.0
ECLIPSE = Time("2026-08-12 00:00:00")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


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
