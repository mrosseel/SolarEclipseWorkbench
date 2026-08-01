"""The command-line path has no GUI controller, and must not crash because of it.

sew.py passes None for the controller, and every generated script carries several
sync_cameras lines.  Before this, each one raised
``AttributeError: 'NoneType' object has no attribute 'model'`` — measured on a
real headless run against the X-T4.
"""

from unittest.mock import MagicMock

from src.solareclipseworkbench.gui import sync_cameras


def test_sync_cameras_headless_does_not_raise():
    sync_cameras(None)


def test_sync_cameras_with_controller_still_refreshes():
    controller = MagicMock()
    sync_cameras(controller)
    controller.model.camera_overview.update_camera_overview.assert_called_once()
