from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from solareclipseworkbench.ui.controller import SolarEclipseController


def sync_cameras(controller: SolarEclipseController):
    """ Synchronise the cameras for the given controller.

    This consists of the following steps:

        - Update the camera overview in the model and the view of the given controller;
        - Set the time of all connected cameras to the time of the computer;
        - Check whether the focus mode and shooting mode of all connected cameras is set to 'Manual'.

    Args:
        - controller: Controller of the Solar Eclipse Workbench UI
    """

    controller.model.camera_overview.update_camera_overview()
