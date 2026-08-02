"""Building the controller must not raise.

A view, or a single widget, constructs perfectly well while the controller that
drives it is broken — so this does what main() does, in the same order, and
asserts only that it survives.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from solareclipseworkbench.gui import (SolarEclipseController, SolarEclipseModel,
                                       SolarEclipseView)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def controller(app, tmp_path, monkeypatch):
    # Point QSettings at a scratch file: the real one is the user's own, and a
    # test must not read their location or write their preferences.
    monkeypatch.setenv("HOME", str(tmp_path))
    model = SolarEclipseModel()
    view = SolarEclipseView(is_simulator=True, low_cpu_mode=True)
    return SolarEclipseController(model, view, is_simulator=True, low_cpu_mode=True)


def test_the_controller_builds(controller):
    assert controller.view is not None
    assert controller.model is not None


def test_the_limb_correction_box_is_on_by_default(controller):
    # Nothing remembered in the scratch settings file, so this is the
    # out-of-the-box answer: corrected contacts are the real ones.
    assert controller.view.limb_correction_checkbox.isChecked()


def test_toggling_the_box_without_a_location_does_not_raise(controller):
    # The moments cannot be recomputed before a place and a date are set, and
    # asking for them anyway is the obvious way for this to blow up.
    controller.view.limb_correction_checkbox.setChecked(False)
    controller.view.limb_correction_checkbox.setChecked(True)


def test_the_mount_dock_exists_and_starts_hidden(controller):
    assert controller.view.mount_dock is not None
    assert not controller.view.mount_dock.isVisible()
