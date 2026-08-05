"""The mount dock has to be safe before anything is plugged in.

Scanning with nothing attached, connecting to something that does not answer,
and pressing a button the mount cannot honour: none may raise, and none may
leave the dock claiming a state it is not in.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from solareclipseworkbench.gui import MountDock
from solareclipseworkbench.hardware_registry import HARDWARE


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dock(app):
    widget = MountDock()
    yield widget
    widget.disconnect_mount()
    widget.deleteLater()


def _connect(dock, driver: str) -> None:
    """Drive the dock's own connect path and wait for its signal to land."""
    index = dock.driver_combo.findData(driver)
    assert index >= 0, f"{driver} driver is not offered"
    dock.driver_combo.setCurrentIndex(index)
    dock.candidate_combo.clear()
    dock.connect_mount()
    for _ in range(200):
        QApplication.processEvents()
        if not dock._busy:
            return
    pytest.fail("connect never completed")


def test_dock_starts_disconnected_with_its_controls_off(dock):
    assert dock.mount is None
    assert dock.status_label.text() == "not connected"
    assert not dock.goto_sun_button.isEnabled()
    assert not dock.stop_button.isEnabled()


def test_every_installed_driver_is_offered(dock):
    offered = {dock.driver_combo.itemData(i) for i in range(dock.driver_combo.count())}
    assert None in offered, "the automatic option is missing"
    assert "simulator" in offered


def test_scanning_with_nothing_attached_does_not_raise(dock):
    dock.scan()
    assert "candidate" in dock.status_label.text()


def test_connecting_registers_the_mount_for_scripts(dock):
    _connect(dock, "simulator")

    assert dock.mount is not None
    # A mount connected by hand has to be the one the scheduled mount_* commands
    # find, or the script silently drives nothing.
    assert HARDWARE.get('mount') is dock.mount
    assert dock.connect_button.text() == "Disconnect"
    assert dock.goto_sun_button.isEnabled()


def test_disconnecting_unregisters_it_again(dock):
    _connect(dock, "simulator")
    dock.disconnect_mount()

    assert dock.mount is None
    assert HARDWARE.get('mount') is None
    assert dock.status_label.text() == "not connected"
    assert not dock.goto_sun_button.isEnabled()


def test_goto_sun_moves_the_mount(dock):
    _connect(dock, "simulator")
    dock.show()
    QApplication.processEvents()

    before = dock.mount.get_radec()
    dock.goto_sun()
    assert dock.mount.get_radec() != before


def test_a_hidden_dock_does_not_poll(dock):
    # Every poll is a round trip down the same serial line the eclipse script
    # uses, so a dock nobody is looking at must not keep asking — including one
    # that was connected while it was closed.
    _connect(dock, "simulator")
    assert not dock._timer.isActive()

    dock.show()
    QApplication.processEvents()
    assert dock._timer.isActive()

    dock.hide()
    QApplication.processEvents()
    assert not dock._timer.isActive()


def test_a_failed_connection_leaves_the_dock_usable(dock, monkeypatch):
    import solareclipseworkbench.gui as gui
    from solareclipseworkbench.mounts import MountError

    def refuse(*args, **kwargs):
        raise MountError("nothing on that port")

    monkeypatch.setattr(gui, "connect_mount", refuse)
    monkeypatch.setattr(gui.QMessageBox, "warning", lambda *a, **k: None)

    _connect(dock, "simulator")

    assert dock.mount is None
    assert dock.status_label.text() == "not connected"
    assert dock.connect_button.isEnabled()
    assert dock.connect_button.text() == "Connect"
"""The mount dock survives its controller leaving the bus."""

from types import SimpleNamespace


def test_a_vanished_controller_disconnects_cleanly_and_says_why(caplog):
    """5 August: the SAL-33 dropped off USB two seconds into a slew - the
    motors' current spike browning out the logic - and the dock kept writing
    into the dead port, one error dialog per attempt."""
    import logging

    from solareclipseworkbench.gui import MountDock
    from solareclipseworkbench.mounts import MountError

    from solareclipseworkbench.gui import MountDock as _Dock

    dock = SimpleNamespace(
        mount=SimpleNamespace(),
        status_label=SimpleNamespace(setText=lambda t: None),
        disconnected=[],
        refresh=lambda: None,
        _VANISHED=_Dock._VANISHED,
    )
    dock.disconnect_mount = lambda: dock.disconnected.append(True)

    def dead_write():
        raise MountError("write failed on /dev/cu.usbserial-0001: "
                         "write failed: [Errno 6] Device not configured")

    with caplog.at_level(logging.WARNING):
        MountDock._guard(dock, "Stop move", dead_write)

    assert dock.disconnected == [True], "kept talking to a dead port"
    assert any("power" in r.getMessage() for r in caplog.records), \
        "nothing pointed at the likely cause"


def test_goto_sun_is_equinox_of_date():
    """The P1 from the review: J2000 coordinates sent to a mount that works in
    current ones - 22.5 arcminutes on eclipse day, 1.4 solar radii.  The
    simulator shares sun_radec, which is why no simulator test could catch it;
    this pins the call itself."""
    import inspect

    from solareclipseworkbench.mounts import base

    source = inspect.getsource(base.sun_radec)
    assert "epoch='date'" in source or 'epoch="date"' in source, \
        "goto_sun is aiming 22 arcminutes behind the sun again"
