"""Model for the Solar Eclipse Workbench UI (MVC pattern)."""

import datetime
import logging
from typing import Union

from astropy.time import Time
from gphoto2 import GPhoto2Error

from solareclipseworkbench.camera import get_focus_mode, get_shooting_mode, set_time
from solareclipseworkbench.eclipse.reference_moments import calculate_reference_moments, ReferenceMomentInfo
from .tables import CameraOverviewTableModel

LOGGER = logging.getLogger("Solar Eclipse Workbench UI")


class SolarEclipseModel:
    """ Model for the Solar Eclipse Workbench UI in the MVC pattern. """

    def __init__(self):
        """ Initialisation of the model of the Solar Eclipse Workbench UI. """

        # Location
        self.is_location_set = False
        self.longitude: Union[float, None] = None
        self.latitude: Union[float, None] = None
        self.altitude: Union[float, None] = None

        # Eclipse date
        self.is_eclipse_date_set = False
        self.eclipse_date: Union[Time, None] = None

        # Time
        self.local_time: Union[datetime.datetime, None] = None
        self.utc_time: Union[datetime.datetime, None] = None

        # Reference moments
        self.reference_moments: Union[dict, None] = None

        self.c1_info: Union[ReferenceMomentInfo, None] = None
        self.c2_info: Union[ReferenceMomentInfo, None] = None
        self.max_info: Union[ReferenceMomentInfo, None] = None
        self.c3_info: Union[ReferenceMomentInfo, None] = None
        self.c4_info: Union[ReferenceMomentInfo, None] = None
        self.sunrise_info: Union[ReferenceMomentInfo, None] = None
        self.sunset_info: Union[ReferenceMomentInfo, None] = None

        # Camera(s)
        self.camera_overview: CameraOverviewTableModel = CameraOverviewTableModel()

    def set_position(self, longitude: float, latitude: float, altitude: float):
        """ Set the geographical position of the observing location. """

        self.longitude = longitude
        self.latitude = latitude
        self.altitude = altitude

        self.is_location_set = True

    def set_eclipse_date(self, eclipse_date: Time):
        """ Set the eclipse date. """

        self.eclipse_date = eclipse_date

        self.is_eclipse_date_set = True

    def get_reference_moments(self):
        """ Calculate and return timing of reference moments, eclipse magnitude, and eclipse type. """

        self.reference_moments, magnitude, eclipse_type = calculate_reference_moments(self.longitude, self.latitude,
                                                                                      self.altitude, self.eclipse_date)

        if eclipse_type == "No eclipse":
            self.c1_info = None
            self.c2_info = None
            self.max_info = None
            self.c3_info = None
            self.c4_info = None

        elif eclipse_type == "Partial":
            self.c1_info = self.reference_moments["C1"]
            self.c2_info = None
            self.max_info = self.reference_moments["MAX"]
            self.c3_info = None
            self.c4_info = self.reference_moments["C4"]

        else:
            self.c1_info = self.reference_moments["C1"]
            self.c2_info = self.reference_moments["C2"]
            self.max_info = self.reference_moments["MAX"]
            self.c3_info = self.reference_moments["C3"]
            self.c4_info = self.reference_moments["C4"]

        self.sunrise_info = self.reference_moments["sunrise"]
        self.sunset_info = self.reference_moments["sunset"]

        return self.reference_moments, magnitude, eclipse_type

    def sync_camera_time(self):
        """ Set the time of all connected cameras to the time of the computer."""

        if not self.camera_overview or not getattr(self.camera_overview, 'camera_overview_dict', None):
            logging.debug('sync_camera_time: no camera overview available yet; skipping')
            return

        for camera_name, camera in self.camera_overview.camera_overview_dict.items():
            logging.info(f"Syncing time for camera {camera_name}")
            set_time(camera)

    def check_camera_state(self):
        """ Check whether the focus mode and shooting mode of all connected cameras is set to 'Manual'. """

        if not self.camera_overview or not getattr(self.camera_overview, 'camera_overview_dict', None):
            logging.debug('check_camera_state: no camera overview available yet; skipping')
            return

        for camera_name, camera in self.camera_overview.camera_overview_dict.items():

            try:
                focus_mode = get_focus_mode(camera)
                if focus_mode.lower() != "manual":
                    LOGGER.warning(f"The focus mode for camera {camera_name} should be set to 'Manual' "
                                   f"(is '{focus_mode}')")
            except GPhoto2Error:
                LOGGER.warning(f"The focus mode for camera {camera_name} could not be determined")

            try:
                shooting_mode = get_shooting_mode(camera_name, camera)
                if shooting_mode.lower() != "manual":
                    LOGGER.warning(f"The shooting mode for camera {camera_name} should be set to 'Manual' "
                                    f"(is '{shooting_mode}')")
            except GPhoto2Error:
                LOGGER.warning(f"The shooting mode for camera {camera_name} could not be determined")
