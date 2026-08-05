""" Solar Eclipse Workbench GUI, implemented according to the MVC pattern:

    - Model: SolarEclipseModel
    - View: SolarEclipseView
    - Controller: SolarEclipseController
"""
import argparse
import datetime
import faulthandler
import logging
import math
import os
import os.path
import queue
import shutil
import sys
import time
from dataclasses import dataclass
from importlib.metadata import version, PackageNotFoundError
from enum import Enum
from pathlib import Path
from typing import Union, Optional

import geopandas
import numpy as np
import pandas as pd
import pytz
from PyQt6.QtGui import QFont, QFontDatabase, QGuiApplication, QIcon, QAction, QIntValidator, QCloseEvent, QPixmap, QImage, QPainter, QPen, QColor
from PyQt6.QtCore import (QTimer, QRect, Qt, QAbstractTableModel, QModelIndex,
                         QSettings, QSignalBlocker, pyqtSignal)
from PyQt6.QtWidgets import QMainWindow, QApplication, QWidget, QFrame, QLabel, QHBoxLayout, QVBoxLayout, QSizePolicy, \
QGridLayout, QGroupBox, QComboBox, QPushButton, QLineEdit, QFileDialog, QScrollArea, QSlider, QTableView, \
QMessageBox, QDialog, QPlainTextEdit, QProgressBar, QToolButton, QCheckBox, QSplitter, QDockWidget, QMenu, \
QTableWidget, QTableWidgetItem, QHeaderView
from PyQt6 import QtWidgets
from apscheduler.job import Job
from apscheduler.schedulers import SchedulerNotRunningError
from apscheduler.schedulers.background import BackgroundScheduler
from astropy.time import Time
from geodatasets import get_path
from gphoto2 import GPhoto2Error, Camera
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from skyfield.api import load, wgs84

import threading

from solareclipseworkbench.camera import get_camera_dict, get_battery_level, get_free_space, get_space, \
    get_shooting_mode, get_focus_mode, set_time, CameraSettings, LiveViewThread, \
    get_sony_save_destination, get_sony_image_quality
from solareclipseworkbench.fuji_camera import maybe_reexec_for_fuji_sdk
from solareclipseworkbench import exposure_trim, hardware_problems
from solareclipseworkbench.coverage_ui import CoverageDock
from solareclipseworkbench.hardware_registry import (register_hardware,
                                                     seconds_to_next_camera_job)
from solareclipseworkbench.observer import Observer, Observable
from solareclipseworkbench.relay_trigger import (RelayError, RelayTrigger, Wiring, discover_relays,
                                                 list_backends, make_backend)
from solareclipseworkbench.qt_utils import apply_system_color_scheme
from solareclipseworkbench.limb_correction import (is_enabled as limb_correction_is_enabled,
                                                    set_enabled as set_limb_correction_enabled)
from solareclipseworkbench.mounts import (MountDriver, MountError, MountNotSupported, mount_track_sun,
                                          connect as connect_mount, discover_mounts,
                                          format_dec, format_ra, list_drivers)
from solareclipseworkbench.limb_ui import BeadsPanel, beads_icon
from solareclipseworkbench.reference_moments import calculate_reference_moments, ReferenceMomentInfo
from solareclipseworkbench.location_ui import ConfigManager, LocationWidget
from solareclipseworkbench.tile_map import TileMap, MIN_ZOOM, MAX_ZOOM
from solareclipseworkbench.constants import SUN_RADIUS, MOON_RADIUS
from solareclipseworkbench import configuration

#: Where the window layout and formats are remembered.  Module level so a test
#: can point it at a temporary file rather than the user's own settings - a test
#: that reads those fails or passes according to where someone last dragged a
#: pane, which is not a property of the code.
SETTINGS_PATH = Path.home() / ".SolarEclipseWorkbench.ini"


#: Starting width of the left dock column.  The eclipse geometry is a pair of
#: discs and reads best near square; wider than this only adds empty margin, and
#: the schedule beside it is the thing that benefits from width.  The canvas asks
#: for 600x620 if left alone, which is where the long empty slice came from.
DOCK_COLUMN_WIDTH = 340

#: Starting height of the camera strip along the bottom.  Enough for a header and
#: a few bodies; the schedule above it wants the rest.
CAMERA_DOCK_HEIGHT = 150

#: Starting height of the contact-times strip: what its ten rows measure, plus
#: the group box's own margins.  It scrolls if given less, so this is where it
#: starts and not a floor.
MOMENTS_DOCK_HEIGHT = 240

ICON_PATH = Path(__file__).parent.resolve() / "img"

TIME_FORMATS = {
    "24 hours": "%H:%M:%S.%f",
    "12 hours": "%I:%M:%S.%f"
}

LIMB_CORRECTION_TOOLTIP = (
    "Use the Moon's topography to apply the Maestro-style contact-point "
    "correction to C2/C3. A separate whole-arc photographic capture window "
    "is available when the configured illuminated-arc criterion can be solved."
)
LIMB_CORRECTION_LOCKED_TOOLTIP = (
    "The lunar limb correction cannot be changed while a script has pending "
    "jobs. Stop the scheduler first, then change the correction and reload the script."
)

DATE_FORMATS = {
    "dd Month yyyy": "%d %b %Y",
    "dd/mm/yyyy": "%d/%m/%Y",
    "mm/dd/yy": "%m/%d/%Y"
}

BEFORE_AFTER = {
    "before": 1,
    "after": -1
}

REFERENCE_MOMENTS = ["C1", "C2", "MAX", "C3", "C4", "sunset", "sunrise"]

LOGGER = logging.getLogger("Solar Eclipse Workbench UI")
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-8s %(message)s', datefmt='%a, %d %b %Y %H:%M:%S', filename="/tmp/solareclipseworkbench.log", filemode='w')

# Where the user's own scripts live: a "scripts" directory in whatever directory the
# application was started from.  The bundled examples sit inside the installed package, which
# is not a location anyone can reasonably be expected to find in a file dialog, so on first
# run they are copied here — right next to where the user works, and freely editable.
SCRIPTS_DIR = Path.cwd() / "scripts"


def _describe_camera_mode(camera_name: str, camera) -> str:
    """Return a short "shoot/focus" description for the camera overview table.

    Modes that are not Manual are marked with a warning sign, so a camera left on AV or AF is
    visible at a glance in the table instead of only in a modal that is easy to miss and in
    the log file.  Reading either mode can fail on some bodies; that is reported as '?' rather
    than being allowed to drop the whole row.
    """
    try:
        shooting_mode = get_shooting_mode(camera_name, camera)
    except Exception:
        shooting_mode = '?'
    try:
        focus_mode = get_focus_mode(camera)
    except Exception:
        focus_mode = '?'

    ok = shooting_mode.lower() == 'manual' and focus_mode.lower() == 'manual'
    return f"{shooting_mode}/{focus_mode}" + ("" if ok else "  ⚠️")


def get_scripts_dir() -> Path:
    """Return the user's scripts directory, seeding it with the bundled examples once.

    Existing files are never overwritten: the copy only fills in names that are not already
    present, so edits made by the user survive upgrades and restarts.
    """
    try:
        SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        for example in sorted((Path(__file__).parent / "example_scripts").glob("*.txt")):
            target = SCRIPTS_DIR / example.name
            if not target.exists():
                shutil.copy2(example, target)
                LOGGER.info("Installed example script %s", target)
    except OSError:
        LOGGER.exception("Could not prepare the scripts directory %s", SCRIPTS_DIR)
    return SCRIPTS_DIR


class BannerNotification(QFrame):
    def __init__(self, text="", parent=None):
        super().__init__(parent)

        # Scope style ONLY to this class to prevent internal widget inheritance bugs
        self.setStyleSheet('''
            BannerNotification {
                background-color: #FFF3CD;
                border-radius: 4px;
            }
            QLabel {
                color: #856404;
                background: transparent;
                border: none;
            }
            QToolButton {
                border: none;
                background: transparent;
                color: #856404;
                font-weight: bold;
            }
            QToolButton:hover {
                color: #000000;
            }
        ''')

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)

        self.label = QLabel(text)
        self.label.setWordWrap(True)

        self.close_btn = QToolButton()
        self.close_btn.setText("✕")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        # Explicitly hide the root frame (self) when clicked
        self.close_btn.clicked.connect(self.hide)

        layout.addWidget(self.label, 1)
        layout.addWidget(self.close_btn, 0, Qt.AlignmentFlag.AlignTop)

    def setText(self, text: str):
        self.label.setText(text)


class SolarEclipseModel:
    """ Model for the Solar Eclipse Workbench UI in the MVC pattern. """

    def __init__(self):
        """ Initialisation of the model of the Solar Eclipse Workbench UI.

        This model keeps stock of the following information:

            - The longitude, latitude, and altitude of the location at which the solar eclipse will be observed;
            - The date of the eclipse that will be observed;
            - The current time (local time + UTC);
            - The information of the reference moments (C1, C2, maximum eclipse, C3, C4, sunrise, and sunset) of the
              solar eclipse: time (local time + UTC), azimuth, and altitude;
            - A dictionary with the connected cameras.
        """

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

        # GPS–computer time offset: set when the user acquires a USB GPS fix.
        # timedelta(0) means "use computer clock", which is the default.
        self.gps_time_offset: datetime.timedelta = datetime.timedelta(0)

    def set_position(self, longitude: float, latitude: float, altitude: float):
        """ Set the geographical position of the observing location.

        Args:
            - longitude: Longitude of the location [degrees]
            - latitude: Latitude of the location [degrees]
            - altitude: Altitude of the location [meters]
        """

        self.longitude = longitude
        self.latitude = latitude
        self.altitude = altitude

        self.is_location_set = True

    def set_eclipse_date(self, eclipse_date: Time):
        """ Set the eclipse date.

        Args:
            - eclipse_date: Eclipse date
        """

        self.eclipse_date = eclipse_date

        self.is_eclipse_date_set = True

    def get_reference_moments(self):
        """ Calculate and return timing of reference moments, eclipse magnitude, and eclipse type.

        Returns:
            - Dictionary with the information about the reference moments (C1, C2, maximum eclipse, C3, C4, sunrise,
              and sunset)
            - Magnitude of the eclipse (0: no eclipse, 1: total eclipse)
            - Eclipse type (total / annular / partial / no eclipse)
        """

        self.reference_moments, magnitude, eclipse_type = calculate_reference_moments(self.longitude, self.latitude,
                                                                                      self.altitude, self.eclipse_date)

        # No eclipse

        if eclipse_type == "No eclipse":
            self.c1_info = None
            self.c2_info = None
            self.max_info = None
            self.c3_info = None
            self.c4_info = None

        # Partial / total eclipse

        elif eclipse_type == "Partial":
            self.c1_info = self.reference_moments["C1"]
            self.c2_info = None
            self.max_info = self.reference_moments["MAX"]
            self.c3_info = None
            self.c4_info = self.reference_moments["C4"]

        # Total eclipse

        else:
            self.c1_info = self.reference_moments["C1"]
            self.c2_info = self.reference_moments["C2"]
            self.max_info = self.reference_moments["MAX"]
            self.c3_info = self.reference_moments["C3"]
            self.c4_info = self.reference_moments["C4"]

            # The number a script is chosen by: how many corona brackets fit
            # between the contacts is decided when the script is written, and one
            # laid out for a longer totality than this is still exposing when the
            # sun comes back.  Load the longest script that does not exceed it.
            duration = self.reference_moments["duration"].total_seconds()
            LOGGER.info(f"Totality lasts {duration:.0f} s at {self.latitude:.4f}, "
                        f"{self.longitude:.4f}, {self.altitude:.0f} m")
            if "duration_mean" in self.reference_moments:
                mean = self.reference_moments["duration_mean"].total_seconds()
                LOGGER.info(f"Totality is {duration - mean:+.1f} s against the smooth-limb "
                            f"{mean:.0f} s; the lunar limb profile is what makes the difference")

        self.sunrise_info = self.reference_moments["sunrise"]
        self.sunset_info = self.reference_moments["sunset"]

        return self.reference_moments, magnitude, eclipse_type

    # def set_camera_overview(self, camera_overview: dict):
    #     """ Set the camera overview to the given dictionary.
    #
    #     Args:
    #         - camera_overview: Dictionary containing the camera overview
    #     """
    #
    #     self.camera_overview = camera_overview

    def sync_camera_time(self):
        """ Set the time of all connected cameras to the time of the computer."""

        if not self.camera_overview or not getattr(self.camera_overview, 'camera_overview_dict', None):
            logging.debug('sync_camera_time: no camera overview available yet; skipping')
            return

        seen_ids: set = set()
        for camera_name, camera in self.camera_overview.camera_overview_dict.items():
            if id(camera) in seen_ids:
                continue
            seen_ids.add(id(camera))
            logging.info(f"Syncing time for camera {camera_name}")
            set_time(camera)

    def check_camera_state(self):
        """ Check whether the focus mode and shooting mode of all connected cameras is set to 'Manual'.

        For the camera(s) for which the focus mode and/or shooting mode is not set to 'Manual', a warning message is
        logged.

        Returns:
            List of warning strings (empty if all cameras are in Manual mode).
        """

        warnings = []

        if not self.camera_overview or not getattr(self.camera_overview, 'camera_overview_dict', None):
            logging.debug('check_camera_state: no camera overview available yet; skipping')
            return warnings

        seen_ids: set = set()
        for camera_name, camera in self.camera_overview.camera_overview_dict.items():
            if id(camera) in seen_ids:
                continue
            seen_ids.add(id(camera))

            # Focus mode

            try:
                focus_mode = get_focus_mode(camera)
                if focus_mode.lower() != "manual":
                    msg = (f"Focus mode for {camera_name} should be 'Manual' (currently '{focus_mode}').\n"
                           f"Switch the lens autofocus switch to MF.")
                    LOGGER.warning(msg)
                    warnings.append(msg)
            except GPhoto2Error:
                msg = f"Could not read focus mode for {camera_name}."
                LOGGER.warning(msg)
                warnings.append(msg)

            # Shooting mode

            try:
                shooting_mode = get_shooting_mode(camera_name, camera)
                if shooting_mode.lower() != "manual":
                    msg = (f"Shooting mode for {camera_name} should be 'Manual' (currently '{shooting_mode}').\n"
                           f"Turn the camera's mode dial to M.")
                    LOGGER.warning(msg)
                    warnings.append(msg)
            except GPhoto2Error:
                msg = f"Could not read shooting mode for {camera_name}."
                LOGGER.warning(msg)
                warnings.append(msg)

        return warnings


class SolarEclipseView(QMainWindow, Observable):
    """ View for the Solar Eclipse Workbench UI in the MVC pattern. """

    def __init__(self, is_simulator: bool = False, low_cpu_mode: bool = False):
        """ Initialisation of the view of the Solar Eclipse Workbench UI.

        This view is responsible for:

            - Visualisation of:
                - The current date ant time (local time + UTC);
                - The location at which the solar eclipse will be observed (longitude, latitude, and altitude);
                - The date and type (total/partial/annular) of the observed eclipse;
                - The information of the reference moments (C1, C2, maximum eclipse, C3, C4, sunrise, and sunset) of the
                  observed solar eclipse: time (local time + UTC), azimuth, and altitude;
                - The information about the connected cameras: camera name, battery level, and free memory;
            - Bringing up a pop-up window in which the location at which the solar eclipse will be observed (longitude,
              latitude, and altitude) can be chosen and visualised;
            - Bringing up a pop-up in which the date of the solar eclipse can be selected from a drop-down menu;
            - Load the information of the reference moments of the observed solar eclipse;
            - Load the information about the connected cameras and synchronises their time to the time of the computer
              they are connected to;
            - Load the configuration file to schedule the tasks (voice prompts, taking pictures, updating the camera
              state);
            - Choose the time and date format.

        Args:
            - is_simulator: Indicates whether the UI should be started in simulator mode
        """

        super().__init__()

        self.controller = None
        self.is_simulator = is_simulator
        self.low_cpu_mode = low_cpu_mode

        self.setGeometry(300, 300, 1500, 1000)
        try:
            _version = version("solareclipseworkbench")
        except PackageNotFoundError:
            _version = "unknown"
        self.setWindowTitle(f"Solar Eclipse Workbench v{_version}")

        self.date_format = list(DATE_FORMATS.keys())[0]
        self.time_format = list(TIME_FORMATS.keys())[0]

        self.toolbar = None
        self.location_action = QAction("Location", self)
        self.date_action = QAction("Date", self)
        self.camera_action = QAction("Camera(s)", self)
        self.simulator_action = QAction("Simulator", self)
        self.file_action = QAction("File", self)
        self.shutdown_scheduler_action = QAction("Stop", self)
        self.relay_action = QAction("Relay", self)
        self.datetime_format_action = QAction("Datetime format", self)
        self.save_action = QAction("Save", self)
        self.live_view_action = QAction("Live View", self)

        self.place_time_frame = QFrame()

        self.eclipse_date_widget = QWidget()
        self.eclipse_date_label = QLabel("Eclipse date")
        self.eclipse_date = QLabel("")

        self.reference_moments_widget = QWidget()

        # A contact burst is aimed at the bead window rather than at the contact,
        # so the window is worth reading directly.
        self.beads_c2_label = QLabel()
        self.beads_c2_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.beads_c3_label = QLabel()
        self.beads_c3_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        # The window is a time and a length.  Kept apart so each sits under the
        # header it belongs to; as one string spanning two columns it ran across
        # the table and made every column look ragged.

        self.beads_c2_duration_label = QLabel()
        self.beads_c2_duration_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.beads_c3_duration_label = QLabel()
        self.beads_c3_duration_label.setAlignment(Qt.AlignmentFlag.AlignRight)

        self.limb_correction_checkbox = QCheckBox(
            "Apply the lunar limb correction to the contact times")
        # The controller replaces this with whatever was remembered.
        self.limb_correction_checkbox.setChecked(True)
        self.limb_correction_checkbox.setToolTip(LIMB_CORRECTION_TOOLTIP)

        self.c1_time_local_label = QLabel()
        self.c1_time_local_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.c2_time_local_label = QLabel()
        self.c2_time_local_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.max_time_local_label = QLabel()
        self.max_time_local_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.c3_time_local_label = QLabel()
        self.c3_time_local_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.c4_time_local_label = QLabel()
        self.c4_time_local_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.sunrise_time_local_label = QLabel()
        self.sunrise_time_local_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.sunset_time_local_label = QLabel()
        self.sunset_time_local_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.c1_time_utc_label = QLabel()
        self.c1_time_utc_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.c2_time_utc_label = QLabel()
        self.c2_time_utc_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.max_time_utc_label = QLabel()
        self.max_time_utc_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.c3_time_utc_label = QLabel()
        self.c3_time_utc_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.c4_time_utc_label = QLabel()
        self.c4_time_utc_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.sunrise_time_utc_label = QLabel()
        self.sunrise_time_utc_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.sunset_time_utc_label = QLabel()
        self.sunset_time_utc_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.c1_countdown_label = QLabel()
        self.c1_countdown_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.c2_countdown_label = QLabel()
        self.c2_countdown_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.max_countdown_label = QLabel()
        self.max_countdown_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.c3_countdown_label = QLabel()
        self.c3_countdown_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.c4_countdown_label = QLabel()
        self.c4_countdown_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.sunrise_countdown_label = QLabel()
        self.sunrise_countdown_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.sunset_countdown_label = QLabel()
        self.sunset_countdown_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.c1_azimuth_label = QLabel()
        self.c1_azimuth_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.c2_azimuth_label = QLabel()
        self.c2_azimuth_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.max_azimuth_label = QLabel()
        self.max_azimuth_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.c3_azimuth_label = QLabel()
        self.c3_azimuth_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.c4_azimuth_label = QLabel()
        self.c4_azimuth_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.c1_altitude_label = QLabel()
        self.c1_altitude_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.c2_altitude_label = QLabel()
        self.c2_altitude_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.max_altitude_label = QLabel()
        self.max_altitude_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.c3_altitude_label = QLabel()
        self.c3_altitude_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.c4_altitude_label = QLabel()
        self.c4_altitude_label.setAlignment(Qt.AlignmentFlag.AlignRight)

        self.date_label = QLabel("Date")
        self.date_label_local = QLabel()
        self.date_label_local.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.time_label_local = QLabel()
        self.time_label_local.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.time_label_local.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.date_label_utc = QLabel()
        self.date_label_utc.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.time_label_utc = QLabel()
        self.time_label_utc.setAlignment(Qt.AlignmentFlag.AlignRight)

        self.longitude_label = QLabel()
        self.longitude_label.setToolTip(
            "Positive values: East of Greenwich meridian; Negative values: West of Greenwich meridian")
        self.latitude_label = QLabel()
        self.latitude_label.setToolTip("Positive values: Northern hemisphere; Negative values: Southern hemisphere")
        self.altitude_label = QLabel()

        self.eclipse_type = QLabel()
        _type_font = self.eclipse_type.font()
        _type_font.setBold(True)
        self.eclipse_type.setFont(_type_font)

        self.camera_overview = QTableView()

        self.eclipse_visualization = EclipsePlotWidget()
        self.beads_panel = BeadsPanel()

        self.jobs_table = QJobsTableView()

        # One-line Sony banner (hidden by default; shown when a Sony camera is present)
        self.sony_banner_label = BannerNotification()
        self.sony_banner_label.setVisible(False)

        self.init_ui()

    def save_settings(self):
        """ Save the settings.

        The settings that are saved are:

            - Longitude [degrees];
            - Latitude [degrees];
            - Altitude [m];
            - Eclipse date;
            - Date format;
            - Time format.
        """

        # Location

        longitude = self.longitude_label.text()
        latitude = self.latitude_label.text()
        altitude = self.altitude_label.text()

        try:
            self.settings.setValue("longitude", float(longitude))
            self.settings.setValue("latitude", float(latitude))
            self.settings.setValue("altitude", float(altitude))
        except ValueError:
            # Unset shows an em dash now, not an empty string; a placeholder
            # is not a coordinate and must not be saved as one.
            pass

        # Eclipse date

        if self.eclipse_date.text() and self.eclipse_date.text() != "\u2014":
            self.settings.setValue("eclipse_date", self.eclipse_date.text())

        # Date & time format

        self.settings.setValue("date_format", self.date_format)
        self.settings.setValue("time_format", self.time_format)

    def init_ui(self):
        """ Add all components to the UI. """

        app_frame = QFrame()
        app_frame.setObjectName("AppFrame")

        # Geometry above, beads below, the schedule beside them spanning both:
        # they are two pictures of the same moment and both are worth watching at
        # once, so they get a row each rather than sharing one tabbed slot.  Any
        # of them can be dragged to another edge, tabbed onto another by dropping
        # it on top, floated onto a second screen for totality, or closed; Qt
        # tracks whatever you end up with through toggleViewAction and saveState.
        self.geometry_dock = QDockWidget("Sun View", self)
        self.geometry_dock.setObjectName("geometry_dock")
        self.geometry_dock.setWidget(self.eclipse_visualization)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.geometry_dock)

        self.beads_dock = QDockWidget("Baily's beads", self)
        self.beads_dock.setObjectName("beads_dock")
        self.beads_dock.setWidget(self.beads_panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.beads_dock)
        self.splitDockWidget(self.geometry_dock, self.beads_dock,
                             Qt.Orientation.Vertical)

        # A floor under each, so neither can be dragged down to a sliver that is
        # then remembered.  Small enough to still get out of the way.
        self.eclipse_visualization.setMinimumHeight(160)
        self.beads_panel.setMinimumHeight(120)

        # The geometry is two discs - it wants to be about as wide as it is tall.
        # Left to itself the dock column takes a third of the window and draws a
        # small pair of circles in the middle of a tall empty slice, so the
        # column is given a width that suits the drawing and the schedule keeps
        # the rest.  A drag still overrides this; it is only where it starts.
        self.resizeDocks([self.geometry_dock], [DOCK_COLUMN_WIDTH],
                         Qt.Orientation.Horizontal)
        self.resizeDocks([self.geometry_dock, self.beads_dock],
                         [1, 1], Qt.Orientation.Vertical)

        # The mount is watchable for the whole run, and closing it must not
        # disturb the rest of the window.  All three docks are built before the
        # toolbar, which borrows their show/hide actions.
        self.mount_dock = MountDock(self)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.mount_dock)

        # What the body has to have set, and every warning the run produces.
        # Both were previously only in the log, which nobody reads while an
        # eclipse is happening.
        # What the loaded script will actually photograph.  A script read as
        # text does not show that an hour of partials is one frame every three
        # minutes while totality is everything at once - or that a gap somebody
        # meant to fill is still there.
        self.coverage_dock = CoverageDock(self)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.coverage_dock)
        # Closed to begin with - it is worth looking at when a script is loaded
        # and empty before that.  Findable regardless: it has an entry in the
        # Panels menu whether it is open or not, which is what was missing when
        # it could not be found at all.
        self.coverage_dock.hide()

        self.problems_dock = ProblemsDock(self)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.problems_dock)
        # Closed by default: a mount is the exception, not the rule, and an empty
        # panel taking a quarter of the window is worse than a toolbar button.
        self.mount_dock.hide()

        self.add_toolbar()

        vbox_left = QVBoxLayout()

        # The date and the time are one line of text between them, and a group
        # box two hundred and fifty pixels wide to hold it pushed everything
        # below it down.  They live in a strip under the toolbar now, where a
        # clock belongs - see status_strip() - and the space goes to the panels
        # that need it.

        # The place joins the clock in the strip.  Moving the date and time out
        # and leaving this behind was half a job: a group box two hundred and
        # fifty pixels wide, holding three numbers, under a strip that already
        # said where and when.

        # The eclipse date and type are on the strip with the rest of it.  This
        # was the last box in the column, and a column of one box is a margin.

        reference_moments_group_box = QGroupBox()
        reference_moments_grid_layout = QGridLayout()
        reference_moments_grid_layout.setVerticalSpacing(2)
        reference_moments_grid_layout.setHorizontalSpacing(14)
        reference_moments_grid_layout.setContentsMargins(8, 6, 8, 6)
        reference_moments_grid_layout.addWidget(QLabel("Time (local)", alignment=Qt.AlignmentFlag.AlignRight), 0, 1)
        reference_moments_grid_layout.addWidget(self.c1_time_local_label, 1, 1)
        reference_moments_grid_layout.addWidget(self.c2_time_local_label, 2, 1)
        reference_moments_grid_layout.addWidget(self.max_time_local_label, 3, 1)
        reference_moments_grid_layout.addWidget(self.c3_time_local_label, 4, 1)
        reference_moments_grid_layout.addWidget(self.c4_time_local_label, 5, 1)
        reference_moments_grid_layout.addWidget(self.sunrise_time_local_label, 6, 1)
        reference_moments_grid_layout.addWidget(self.sunset_time_local_label, 7, 1)
        # The UTC column is not laid out: it doubled the width of the panel and
        # the same times are on the local column as a tooltip.  The widgets are
        # kept and still updated, because the settings dialog and the clock both
        # write to them.
        reference_moments_grid_layout.addWidget(QLabel("Countdown", alignment=Qt.AlignmentFlag.AlignRight), 0, 3)
        reference_moments_grid_layout.addWidget(self.c1_countdown_label, 1, 3)
        reference_moments_grid_layout.addWidget(self.c2_countdown_label, 2, 3)
        reference_moments_grid_layout.addWidget(self.max_countdown_label, 3, 3)
        reference_moments_grid_layout.addWidget(self.c3_countdown_label, 4, 3)
        reference_moments_grid_layout.addWidget(self.c4_countdown_label, 5, 3)
        reference_moments_grid_layout.addWidget(self.sunrise_countdown_label, 6, 3)
        reference_moments_grid_layout.addWidget(self.sunset_countdown_label, 7, 3)
        reference_moments_grid_layout.addWidget(QLabel("Azimuth [°]", alignment=Qt.AlignmentFlag.AlignRight), 0, 4)
        reference_moments_grid_layout.addWidget(self.c1_azimuth_label, 1, 4)
        reference_moments_grid_layout.addWidget(self.c2_azimuth_label, 2, 4)
        reference_moments_grid_layout.addWidget(self.max_azimuth_label, 3, 4)
        reference_moments_grid_layout.addWidget(self.c3_azimuth_label, 4, 4)
        reference_moments_grid_layout.addWidget(self.c4_azimuth_label, 5, 4)
        reference_moments_grid_layout.addWidget(QLabel("Altitude [°]", alignment=Qt.AlignmentFlag.AlignRight), 0, 5)
        reference_moments_grid_layout.addWidget(self.c1_altitude_label, 1, 5)
        reference_moments_grid_layout.addWidget(self.c2_altitude_label, 2, 5)
        reference_moments_grid_layout.addWidget(self.max_altitude_label, 3, 5)
        reference_moments_grid_layout.addWidget(self.c3_altitude_label, 4, 5)
        reference_moments_grid_layout.addWidget(self.c4_altitude_label, 5, 5)
        reference_moments_grid_layout.addWidget(QLabel("First contact (C1)"), 1, 0)
        reference_moments_grid_layout.addWidget(QLabel("Second contact (C2)"), 2, 0)
        reference_moments_grid_layout.addWidget(QLabel("Maximum eclipse"), 3, 0)
        reference_moments_grid_layout.addWidget(QLabel("Third contact (C3)"), 4, 0)
        reference_moments_grid_layout.addWidget(QLabel("Fourth contact (C4)"), 5, 0)
        reference_moments_grid_layout.addWidget(QLabel("Sunrise"), 6, 0)
        reference_moments_grid_layout.addWidget(QLabel("Sunset"), 7, 0)
        reference_moments_grid_layout.addWidget(QLabel("Beads window (C2)"), 8, 0)
        reference_moments_grid_layout.addWidget(self.beads_c2_label, 8, 1)
        reference_moments_grid_layout.addWidget(self.beads_c2_duration_label, 8, 3)
        reference_moments_grid_layout.addWidget(QLabel("Beads window (C3)"), 9, 0)
        reference_moments_grid_layout.addWidget(self.beads_c3_label, 9, 1)
        reference_moments_grid_layout.addWidget(self.beads_c3_duration_label, 9, 3)

        # The correction belongs with the numbers it changes: it moves C2 and C3
        # by seconds, which is more than a bead burst is long.  Default on — the
        # corrected contacts are the real ones, a smooth Moon is the approximation.
        reference_moments_grid_layout.addWidget(self.limb_correction_checkbox, 10, 0, 1, 6)
        # Somewhere for the slack to go.  Without these the grid stretches to
        # fill a full-height dock: ten rows spread seventy pixels apart and four
        # columns spread across five hundred, which is the "waaaay too much
        # space" - the panel was not big, it was inflated.
        # The whole panel in one face, not the numbers alone.  Digits want a
        # fixed width - a countdown jitters sideways on every tick otherwise, and
        # times do not line up under each other - but setting it on the values
        # and leaving the labels proportional made one small table look like two
        # pasted together.
        numeric = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        reference_moments_group_box.setFont(numeric)

        reference_moments_grid_layout.setRowStretch(11, 1)
        reference_moments_grid_layout.setColumnStretch(6, 1)

        reference_moments_group_box.setLayout(reference_moments_grid_layout)
        # A minimum rather than a fixed width, so the box cannot be squeezed until
        # the reference-moment columns collide but can still give space back when
        # the window is narrow.
        # No minimum: it scrolls, so it can be given whatever width is left
        # rather than dictating the window's.  It used to sit in the input row
        # with a 544px floor, which is most of a laptop screen for a table of
        # ten numbers.

        # noinspection SpellCheckingInspection
        moments_scroller = QScrollArea()
        moments_scroller.setWidget(reference_moments_group_box)
        moments_scroller.setWidgetResizable(True)
        moments_scroller.setFrameShape(QFrame.Shape.NoFrame)

        self.moments_dock = QDockWidget("Contact times", self)
        self.moments_dock.setObjectName("moments_dock")
        self.moments_dock.setWidget(moments_scroller)
        # Beside the cameras along the bottom.  A dock fills whatever area it is
        # alone in - down the right it left six hundred pixels of nothing beside
        # ten numbers, along the top it left two thirds of the width empty - so
        # it is given a neighbour instead, and the two short panels share one
        # strip while the schedule keeps the middle of the window.
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.moments_dock)
        self.moments_dock.show()

        input_hbox = QHBoxLayout()
        input_hbox.addLayout(vbox_left, 0)
        input_hbox.addStretch(1)

        # The camera overview is a table and does not belong wedged into a row of
        # input boxes: on a 1440-wide screen that row plus the dock column forced
        # a 1714px minimum, so the window could not fit the display at all and
        # every panel in it was crushed.  As a dock it gets real width, and it
        # opens by default because "is the body actually there" is the question
        # asked most often.
        # No floor: with one body connected there is one row to show, and the
        # strip should be draggable down to just that.  CAMERA_DOCK_HEIGHT is
        # where it starts, not the least it can be.
        self.camera_overview.setMinimumHeight(0)
        self.camera_dock = QDockWidget("Cameras", self)
        self.camera_dock.setObjectName("camera_dock")
        self.camera_dock.setWidget(self.camera_overview)
        # Along the bottom, not down the side: it is a table of a few rows, so it
        # reads better wide than tall, and the window is already at its minimum
        # width - a fourth column would leave it the sliver it had before.
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.camera_dock)
        self.camera_dock.show()
        # Built after add_toolbar, so its toggle is added here rather than with
        # the others.  Placed before the mount's, matching the order they matter.
        self.moments_dock_action = self.moments_dock.toggleViewAction()
        self.moments_dock_action.setText("Contact times")
        self.moments_dock_action.setStatusTip("Show the contact times and countdowns")

        # Both are short panels, and a dock alone in an area fills it: down the
        # right that left six hundred pixels of nothing beside ten numbers,
        # along the top two thirds of the width.  Sharing one strip, neither
        # does, and the schedule keeps the middle of the window.
        self.splitDockWidget(self.camera_dock, self.moments_dock,
                             Qt.Orientation.Horizontal)
        self.resizeDocks([self.camera_dock, self.moments_dock], [3, 4],
                         Qt.Orientation.Horizontal)
        self.resizeDocks([self.moments_dock], [MOMENTS_DOCK_HEIGHT],
                         Qt.Orientation.Vertical)

        # The error log is worth a button of its own with the count on it: the
        # whole point of the dock is that a problem which only reaches a file is
        # a problem nobody sees until afterwards, and a closed dock is a file.
        self.coverage_dock_action = self.coverage_dock.toggleViewAction()
        self.coverage_dock_action.setText("Coverage")
        self.coverage_dock_action.setStatusTip(
            "Show what the loaded script photographs, on a timeline")

        self.problems_dock_action = self.problems_dock.toggleViewAction()
        self.problems_dock_action.setText("Error Log")
        self.problems_dock_action.setStatusTip(
            "Show every warning and error this run has produced")
        self.problems_dock.count_changed.connect(self._show_problem_count)
        self._show_problem_count(0)

        self.camera_dock_action = self.camera_dock.toggleViewAction()
        self.camera_dock_action.setText("Cameras")
        self.camera_dock_action.setStatusTip("Show the connected cameras")

        # Every toggle exists by now: the three built with the toolbar and the
        # four built with the docks below it.
        self.build_panels_menu()

        # Below about this width the two discs stop being readable.
        self.eclipse_visualization.setMinimumWidth(240)

        global_layout = QVBoxLayout()
        global_layout.addWidget(self.status_strip())
        # show reminder banner at top
        global_layout.addWidget(self.sony_banner_label)
        global_layout.addLayout(input_hbox)

        global_layout.addWidget(self.jobs_table)

        app_frame.setLayout(global_layout)

        self.setCentralWidget(app_frame)

        self.restore_splitter_state()

    def restore_splitter_state(self):
        """Put the docks back where the user left them."""
        settings = QSettings(str(SETTINGS_PATH), QSettings.Format.IniFormat)
        # Docks are keyed by object name, so a layout saved before one of them
        # existed leaves that dock where the code put it.
        trim = settings.value("exposure/trim_stops")
        if trim is not None:
            try:
                exposure_trim.set_stops(float(trim))
                index = self.exposure_trim_combo.findData(exposure_trim.stops())
                if index >= 0:
                    self.exposure_trim_combo.blockSignals(True)
                    self.exposure_trim_combo.setCurrentIndex(index)
                    self.exposure_trim_combo.blockSignals(False)
            except (TypeError, ValueError):
                logging.debug("Ignoring an unreadable saved exposure trim: %r", trim)

        dock_state = settings.value("layout/docks")
        if dock_state is not None:
            self.restoreState(dock_state)
        self.fit_to_screen()

    def fit_to_screen(self):
        """Never open larger than the display, whatever the saved layout says.

        A window whose minimum exceeds the screen cannot be shrunk at all - the
        edge is off the display and there is nothing to drag - so a layout that
        demands too much width locks the user out of fixing it.  This clamps the
        window to what the screen actually offers and pulls it back on-screen if
        a saved position put it half off.
        """
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        width = min(self.width(), available.width())
        height = min(self.height(), available.height())
        if (width, height) != (self.width(), self.height()):
            logging.info('Window clamped to the display: %dx%d', width, height)
            self.resize(width, height)
        frame = self.frameGeometry()
        if not available.contains(frame):
            frame.moveCenter(available.center())
            self.move(frame.topLeft())

    def reset_layout(self):
        """Put every pane back where the code puts it.

        A saved layout overrides the defaults completely, so a pane dragged down
        to a few pixels stays that way across restarts and there is no way back
        to a working window short of editing an ini file by hand.  This is that
        way back.
        """
        settings = QSettings(str(SETTINGS_PATH), QSettings.Format.IniFormat)
        settings.remove("layout/docks")
        # Written before the panes were docks and read by nothing since.
        settings.remove("layout/output_splitter")
        settings.sync()

        for dock in (self.geometry_dock, self.beads_dock, self.camera_dock,
                     self.moments_dock, self.mount_dock):
            dock.setFloating(False)
            dock.show()
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.geometry_dock)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.beads_dock)
        self.splitDockWidget(self.geometry_dock, self.beads_dock,
                             Qt.Orientation.Vertical)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.camera_dock)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.moments_dock)
        self.splitDockWidget(self.camera_dock, self.moments_dock,
                             Qt.Orientation.Horizontal)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.mount_dock)
        self.resizeDocks([self.geometry_dock], [DOCK_COLUMN_WIDTH],
                         Qt.Orientation.Horizontal)
        self.resizeDocks([self.geometry_dock, self.beads_dock],
                         [1, 1], Qt.Orientation.Vertical)
        self.resizeDocks([self.camera_dock], [CAMERA_DOCK_HEIGHT],
                         Qt.Orientation.Vertical)
        self.resizeDocks([self.moments_dock], [MOMENTS_DOCK_HEIGHT],
                         Qt.Orientation.Vertical)
        self.mount_dock.hide()
        logging.info("Window layout reset to the default arrangement")

    def save_splitter_state(self):
        """Remember where the docks were left."""
        settings = QSettings(str(SETTINGS_PATH), QSettings.Format.IniFormat)
        settings.setValue("layout/docks", self.saveState())

    def _show_problem_count(self, count: int) -> None:
        """Put the number of logged problems in the toolbar button.

        A count in the button is the difference between a dock somebody thought
        to open and one they had no reason to.  Red, because the numbers that
        matter here are the ones nobody went looking for.
        """
        if count:
            self.problems_dock_action.setText("Error Log  \u25cf %d" % count)
            font = self.font()
            font.setBold(True)
            for widget in self.toolbar.findChildren(QToolButton):
                if widget.defaultAction() is self.problems_dock_action:
                    widget.setStyleSheet(
                        "color: #c0392b; font-weight: bold;")
        else:
            self.problems_dock_action.setText("Error Log")
            for widget in self.toolbar.findChildren(QToolButton):
                if widget.defaultAction() is self.problems_dock_action:
                    widget.setStyleSheet("")

    def build_panels_menu(self) -> None:
        """One button listing every dock, instead of a button each.

        Six toggles in a row is most of the toolbar spent on panels that are
        mostly closed, and it pushed the buttons that do something to the far
        end.  A menu also shows their state together, which is the question
        actually being asked: what is open.
        """
        menu = QMenu("Panels", self)
        for action in (self.geometry_dock_action, self.beads_action,
                       self.moments_dock_action, self.camera_dock_action,
                       self.coverage_dock_action, self.problems_dock_action,
                       self.mount_dock_action):
            if action is not None:
                menu.addAction(action)

        self.panels_button = QToolButton(self)
        self.panels_button.setText("Panels")
        self.panels_button.setMenu(menu)
        self.panels_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        self.panels_button.setToolTip("Show or hide the panels")
        self.toolbar.addWidget(self.panels_button)

    def status_strip(self) -> QWidget:
        """The clock, the place and the eclipse, in one line under the toolbar.

        All of it monospaced: a proportional 1 is narrower than a 0, so a
        ticking clock makes everything to its right twitch once a second.
        The captions are italic to read as captions; the two values a run is
        planned around - the eclipse type and its duration - are bold.
        Unset values show an em dash rather than nothing, so "no location yet"
        looks deliberate instead of broken.
        """
        strip = QHBoxLayout()
        strip.setContentsMargins(8, 2, 8, 3)

        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        caption_font = QFont(mono)
        caption_font.setItalic(True)
        bold = QFont(mono)
        bold.setBold(True)

        for label in (self.date_label_local, self.time_label_local,
                      self.longitude_label, self.latitude_label,
                      self.altitude_label, self.eclipse_date):
            label.setFont(mono)
        for label in (self.eclipse_type,):
            label.setFont(bold)
        for label in (self.longitude_label, self.latitude_label,
                      self.altitude_label, self.eclipse_type):
            if not label.text():
                label.setText("\u2014")

        def caption(text: str) -> QLabel:
            label = QLabel(text)
            label.setFont(caption_font)
            return label

        strip.addWidget(self.date_label_local)
        strip.addSpacing(10)
        strip.addWidget(self.time_label_local)
        strip.addSpacing(22)
        for name, value in (("Lon", self.longitude_label),
                            ("Lat", self.latitude_label),
                            ("Alt", self.altitude_label)):
            strip.addWidget(caption(name))
            strip.addWidget(value)
            strip.addSpacing(14)
        strip.addSpacing(8)
        strip.addWidget(caption("Eclipse"))
        strip.addWidget(self.eclipse_date)
        strip.addSpacing(14)
        strip.addWidget(self.eclipse_type)
        strip.addStretch(1)

        # In a widget whose minimum width is nothing, so however long its text
        # gets it can only ever be clipped, never widen the window.  A QLabel's
        # text is a minimum width, and one long value here once pushed the
        # toolbar's right end out of view.
        holder = QWidget()
        holder.setLayout(strip)
        holder.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

        # Camera, relay and script are NOT on the strip.  They were, briefly:
        # a camera name plus "HID relay 16c0:05df - dual channel" plus a job
        # count made the line wider than the window, and a QLabel's text is a
        # minimum width, so the strip pushed the toolbar's right end - the
        # Panels button - out of view.  The amber dots on the toolbar icons
        # carry the same answers without costing an inch of width.
        self._readiness_labels = (
            self.longitude_label, self.latitude_label, self.altitude_label,
            self.eclipse_date, self.eclipse_type)
        self.refresh_readiness_colours()
        return holder

    #: Quietly amber, not red: half of these stay unset in a legitimate run -
    #: no mount, no relay on the parked body - and a row of red reads as a
    #: fault, which "not filled in yet" is not.
    UNSET_COLOUR = "color: #b98514;"

    def _remember_base_icon(self, action) -> None:
        if not hasattr(self, '_base_icons'):
            self._base_icons = {}
            self._badge_state = {}
        self._base_icons[action] = action.icon()

    def set_action_unset(self, action, unset: bool) -> None:
        """A small amber dot on a toolbar icon whose thing is not set yet.

        On the icon itself, not only in the strip: the buttons are what the eye
        goes to during setup, and the dot disappearing as each one is dealt
        with is the checklist working itself off.
        """
        if getattr(self, '_badge_state', {}).get(action) is unset:
            return
        self._badge_state[action] = unset
        base = self._base_icons.get(action)
        if base is None:
            return
        if not unset:
            action.setIcon(base)
            return
        size = 32
        pixmap = base.pixmap(size, size)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#e6a817"))
        dot = size * 0.34
        painter.drawEllipse(int(size - dot), int(size - dot), int(dot), int(dot))
        painter.end()
        action.setIcon(QIcon(pixmap))

    def refresh_readiness_colours(self) -> None:
        """Amber for the em dashes, the normal palette for real values."""
        for label in self._readiness_labels:
            unset = label.text() in ("", "\u2014")
            if unset and not label.text():
                label.setText("\u2014")
            label.setStyleSheet(self.UNSET_COLOUR if unset else "")



    def add_toolbar(self):
        """ Create the toolbar of the UI.

        The toolbar has buttons for:

            - Bringing up a pop-up window in which the location at which the solar eclipse will be observed (longitude,
              latitude, and altitude) can be chosen and visualised;
            - Bringing up a pop-up in which the date of the solar eclipse can be selected from a drop-down menu;
            - Loading the information of the reference moments of the observed solar eclipse;
            - Loading the information about the connected cameras and synchronises their time to the time of the
              computer they are connected to;
            - Loading the configuration file to schedule the tasks (voice prompts, taking pictures, updating the camera
              state);
            - Bringing up a pop-up window in which you can choose the time and date format.
        """

        self.toolbar = self.addToolBar('MainToolbar')
        # saveState() skips widgets without an objectName - the warning at every
        # exit - so the toolbar's position was never remembered.
        self.toolbar.setObjectName("MainToolbar")

        # Location

        self.location_action.setStatusTip("Location")
        self.location_action.setIcon(QIcon(str(ICON_PATH / "location.png")))
        self._remember_base_icon(self.location_action)
        self.location_action.triggered.connect(self.on_toolbar_button_click)
        self.toolbar.addAction(self.location_action)

        # Date

        self.date_action.setStatusTip("Date")
        # The drawn diamond ring, not a calendar: the button chooses an
        # eclipse, and the calendar glyph made it read as a date-format thing.
        self.date_action.setIcon(QIcon(beads_icon(32)))
        self._remember_base_icon(self.date_action)
        self.date_action.triggered.connect(self.on_toolbar_button_click)
        self.toolbar.addAction(self.date_action)

        # Reference moments


        # Camera(s)

        self.camera_action.setStatusTip("Camera(s)")
        self.camera_action.setIcon(QIcon(str(ICON_PATH / "camera.png")))
        self._remember_base_icon(self.camera_action)
        self.camera_action.triggered.connect(self.on_toolbar_button_click)
        self.toolbar.addAction(self.camera_action)

        # Next to the cameras: it is the shutter release for one of them.

        self.relay_action.setStatusTip("Relay shutter trigger")
        self.relay_action.setIcon(QIcon(str(ICON_PATH / "relay.png")))
        self._remember_base_icon(self.relay_action)
        self.relay_action.triggered.connect(self.on_toolbar_button_click)
        self.toolbar.addAction(self.relay_action)

        if self.is_simulator:
            self.simulator_action.setStatusTip("Configure simulator")
            self.simulator_action.setIcon(QIcon(str(ICON_PATH / "simulator.png")))
            self.simulator_action.triggered.connect(self.on_toolbar_button_click)
            self.toolbar.addAction(self.simulator_action)

        # Configuration file

        self.file_action.setStatusTip("File")
        self.file_action.setIcon(QIcon(str(ICON_PATH / "folder.png")))
        self._remember_base_icon(self.file_action)
        self.file_action.triggered.connect(self.on_toolbar_button_click)
        self.toolbar.addAction(self.file_action)

        # Shutdown scheduler

        self.shutdown_scheduler_action.setStatusTip("Shut down scheduler")
        self.shutdown_scheduler_action.setIcon(QIcon(str(ICON_PATH / "stop.png")))
        self.shutdown_scheduler_action.triggered.connect(self.on_toolbar_button_click)
        self.toolbar.addAction(self.shutdown_scheduler_action)

        # Date & time format

        self.datetime_format_action.setStatusTip("Datetime format")
        self.datetime_format_action.setIcon(QIcon(str(ICON_PATH / "settings.png")))
        self.datetime_format_action.triggered.connect(self.on_toolbar_button_click)
        self.toolbar.addAction(self.datetime_format_action)

        # Save settings

        self.save_action.setStatusTip("Save configuration")
        self.save_action.setIcon(QIcon(str(ICON_PATH / "save.png")))
        self.save_action.triggered.connect(self.on_toolbar_button_click)
        self.toolbar.addAction(self.save_action)

        # Live View

        self.live_view_action.setStatusTip("Open live view window (1 fps preview from camera)")
        self.live_view_action.setIcon(QIcon(str(ICON_PATH / "live.png")))
        self.live_view_action.triggered.connect(self.on_toolbar_button_click)
        self.toolbar.addAction(self.live_view_action)

        # Refresh Plot
        if self.low_cpu_mode:
            self.refresh_plot_action = QAction("Refresh Plot", self)
            self.refresh_plot_action.setStatusTip("Manually update the eclipse geometry plot")
            self.refresh_plot_action.setIcon(QIcon(str(ICON_PATH / "refresh.png")))
            self.refresh_plot_action.triggered.connect(self.on_toolbar_button_click)
            self.toolbar.addAction(self.refresh_plot_action)

        # These set options rather than performing actions, so they sit at the far
        # end, away from the buttons that do something when pressed.
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.toolbar.addWidget(spacer)

        # Qt's own toggles, so closing a dock by its X keeps the button in step.
        self.geometry_dock_action = self.geometry_dock.toggleViewAction()
        self.geometry_dock_action.setText("Sun View")
        self.geometry_dock_action.setStatusTip("Show the Sun view")

        self.beads_action = self.beads_dock.toggleViewAction()
        # Text, like every other toggle beside it.  With an icon set, this was
        # the only one Qt drew as a picture, which read as a different kind of
        # control rather than the same one.
        self.beads_action.setText("Beads")
        self.beads_action.setStatusTip("Show the Baily's beads graphic")

        self.mount_dock_action = self.mount_dock.toggleViewAction()
        self.mount_dock_action.setText("Mount")
        self.mount_dock_action.setStatusTip("Show the mount controls")

        # The haze correction.  Sits on the toolbar rather than in a dialog
        # because it is judged by eye against live view and adjusted while the
        # eclipse runs, not set once and forgotten.
        self.toolbar.addWidget(QLabel("  Exposure "))
        self.exposure_trim_combo = QComboBox()
        steps = int(exposure_trim.LIMIT_STOPS / exposure_trim.STEP_STOPS)
        for n in range(steps, -steps - 1, -1):
            value = n * exposure_trim.STEP_STOPS
            self.exposure_trim_combo.addItem(
                "0 EV" if not value else f"{value:+.1f} EV", value)
        self.exposure_trim_combo.setCurrentIndex(steps)      # 0 EV
        self.exposure_trim_combo.setStatusTip(
            "Correct every scheduled exposure - for haze, judged against live view")
        self.exposure_trim_combo.currentIndexChanged.connect(self.on_exposure_trim_changed)
        self.toolbar.addWidget(self.exposure_trim_combo)

        # A saved layout overrides the defaults completely, so a pane dragged to
        # a few pixels stays that way across restarts.  Without this the only way
        # back is editing an ini by hand, which is not a thing to discover on
        # eclipse morning.
        self.reset_layout_action = QAction("Reset layout", self)
        self.reset_layout_action.setStatusTip(
            "Put the panes back where they started")
        self.reset_layout_action.triggered.connect(self.reset_layout)
        self.toolbar.addAction(self.reset_layout_action)

    def on_exposure_trim_changed(self, index: int):
        """Apply the observer's haze correction to every exposure from now on."""
        value = self.exposure_trim_combo.currentData()
        if value is None:
            return
        exposure_trim.set_stops(value)
        settings = QSettings(str(SETTINGS_PATH), QSettings.Format.IniFormat)
        settings.setValue("exposure/trim_stops", value)
        if hasattr(self, "statusBar"):
            self.statusBar().showMessage(
                f"Exposure trim {exposure_trim.describe()} - applies to every "
                f"frame from now on, not to those already taken", 8000)

    def on_toolbar_button_click(self):
        """ Action triggered when a toolbar button is clicked."""

        sender = self.sender()
        self.notify_observers(sender)

    def update_time(self, current_time_local: datetime.datetime, current_time_utc: datetime.datetime,
                    countdown_c1: datetime.timedelta, countdown_c2: datetime.timedelta,
                    countdown_max: datetime.timedelta, countdown_c3: datetime.timedelta,
                    countdown_c4: datetime.timedelta, countdown_sunrise: datetime.timedelta,
                    countdown_sunset: datetime.timedelta):
        """ Update the displayed current time and countdown clocks.

        Args:
            - current_time_local: Current time in local timezone
            - current_time_utc: Current time in UTC timezone
            - countdown_c1: Countdown clock to C1
            - countdown_c2: Countdown clock to C2
            - countdown_max: Countdown clock to maximum eclipse
            - countdown_c3: Countdown clock to C3
            - countdown_c4: Countdown clock to C4
            - countdown_sunrise: Countdown clock to sunrise
            - countdown_sunset: Countdown clock to sunset
        """

        self.eclipse_date_label.setText("Eclipse date")

        self.date_label.setText("Date")
        self.date_label_local.setText(datetime.datetime.strftime(current_time_local, DATE_FORMATS[self.date_format]))
        self.date_label_utc.setText(datetime.datetime.strftime(current_time_utc, DATE_FORMATS[self.date_format]))

        self.time_label_local.setText(format_time(current_time_local, self.time_format))
        self.time_label_utc.setText(format_time(current_time_utc, self.time_format))
        # Still updated, still read by the settings dialog - just shown on hover.
        self.date_label_local.setToolTip(
            f"{datetime.datetime.strftime(current_time_utc, DATE_FORMATS[self.date_format])} UTC")
        self.time_label_local.setToolTip(
            f"{format_time(current_time_utc, self.time_format)} UTC")

        if not countdown_c1 or countdown_c1.total_seconds() <= 0:
            label_text = "-"
        else:
            label_text = str(format_countdown(countdown_c1))
        self.c1_countdown_label.setText(label_text)

        if not countdown_c2 or countdown_c2.total_seconds() <= 0:
            label_text = "-"
        else:
            label_text = str(format_countdown(countdown_c2))
        self.c2_countdown_label.setText(label_text)

        if not countdown_max or countdown_max.total_seconds() <= 0:
            label_text = "-"
        else:
            label_text = str(format_countdown(countdown_max))
        self.max_countdown_label.setText(label_text)

        if not countdown_c3 or countdown_c3.total_seconds() <= 0:
            label_text = "-"
        else:
            label_text = str(format_countdown(countdown_c3))
        self.c3_countdown_label.setText(label_text)

        if not countdown_c4 or countdown_c4.total_seconds() <= 0:
            label_text = "-"
        else:
            label_text = str(format_countdown(countdown_c4))
        self.c4_countdown_label.setText(label_text)

        if not countdown_sunrise or countdown_sunrise.total_seconds() <= 0:
            label_text = "-"
        else:
            label_text = str(format_countdown(countdown_sunrise))
        self.sunrise_countdown_label.setText(label_text)

        if not countdown_sunset or countdown_sunset.total_seconds() <= 0:
            label_text = "-"
        else:
            label_text = str(format_countdown(countdown_sunset))
        self.sunset_countdown_label.setText(label_text)


    def show_reference_moments(self, reference_moments: dict, magnitude: float, eclipse_type: str):
        """ Display the given reference moments, magnitude, and eclipse type.

        Args:
            - reference_moments: Dictionary with the reference moments (C1, C2, maximum eclipse, C3, C4, sunrise, and
                                 sunset)
            - magnitude: Eclipse magnitude (0: no eclipse, 1: total eclipse)
            - eclipse_type: Eclipse type (total / annular / partial / no eclipse)
        """

        if eclipse_type == "Partial" or eclipse_type == "Annular":
            self.eclipse_type.setText(eclipse_type + f" ({round(magnitude, 2)})")
        elif eclipse_type == "No eclipse":
            self.eclipse_type.setText(eclipse_type)
        else:
            # Seconds as well as m:ss, because the scripts are chosen by the
            # number of seconds of totality they fill.
            total = round(reference_moments["duration"].total_seconds())
            minutes, seconds = divmod(reference_moments["duration"].seconds, 60)
            self.eclipse_type.setText(f"{eclipse_type} ({minutes}:{seconds:02} = {total} s)")
            # The duration lives with the eclipse type, in bold, and nowhere
            # else: it was in the moments dock too, which is a table of moments
            # rather than of how long they are apart.

        # First contact

        if "C1" in reference_moments:
            c1_info: ReferenceMomentInfo = reference_moments["C1"]
            self.c1_time_utc_label.setText(format_time(c1_info.time_utc, self.time_format))
            self.c1_time_local_label.setText(format_time(c1_info.time_local, self.time_format))
            self.c1_time_local_label.setToolTip(
                f"{format_time(c1_info.time_utc, self.time_format)} UTC")
            self.c1_azimuth_label.setText(str(int(c1_info.azimuth)))
            self.c1_altitude_label.setText(str(int(c1_info.altitude)))
        else:
            self.c1_time_utc_label.setText("")
            self.c1_time_local_label.setText("")
            self.c1_azimuth_label.setText("")
            self.c1_altitude_label.setText("")

        # Second contact

        if "C2" in reference_moments:
            # The limb-corrected moment when there is one, the mean-limb moment
            # otherwise.  Scripts schedule against the corrected contacts, so the
            # display has to follow them or the two disagree.
            c2_info: ReferenceMomentInfo = reference_moments.get(
                "C2_LIMB", reference_moments["C2"])
            self.c2_time_utc_label.setText(format_time(c2_info.time_utc, self.time_format))
            self.c2_time_local_label.setText(format_time(c2_info.time_local, self.time_format))
            self.c2_time_local_label.setToolTip(
                f"{format_time(c2_info.time_utc, self.time_format)} UTC")
            self.c2_azimuth_label.setText(str(int(c2_info.azimuth)))
            self.c2_altitude_label.setText(str(int(c2_info.altitude)))
        else:
            self.c2_time_utc_label.setText("")
            self.c2_time_local_label.setText("")
            self.c2_azimuth_label.setText("")
            self.c2_altitude_label.setText("")

        # Bead windows

        for contact, label, duration_label in (
                ("C2", self.beads_c2_label, self.beads_c2_duration_label),
                ("C3", self.beads_c3_label, self.beads_c3_duration_label)):
            start = reference_moments.get(f"BEADS_{contact}_START")
            end = reference_moments.get(f"BEADS_{contact}_END")
            if start is None or end is None:
                status = reference_moments.get(f"BEADS_{contact}_STATUS")
                if status == "unresolved":
                    label.setText("capture window unresolved")
                else:
                    # Name the reason: a blank cell reads like a solve that failed.
                    label.setText("correction off" if not limb_correction_is_enabled()
                                  else "no limb profile")
                duration_label.setText("")
                continue
            seconds = (end.time_utc - start.time_utc).total_seconds()
            label.setText("%s - %s" % (format_time(start.time_local, self.time_format),
                                       format_time(end.time_local, self.time_format)))
            duration_label.setText("%.2f s" % seconds)
            label.setToolTip("%s - %s UTC" % (format_time(start.time_utc, self.time_format),
                                              format_time(end.time_utc, self.time_format)))

        # Maximum eclipse

        if "MAX" in reference_moments:
            max_info: ReferenceMomentInfo = reference_moments["MAX"]
            self.max_time_utc_label.setText(format_time(max_info.time_utc, self.time_format))
            self.max_time_local_label.setText(format_time(max_info.time_local, self.time_format))
            self.max_time_local_label.setToolTip(
                f"{format_time(max_info.time_utc, self.time_format)} UTC")
            self.max_azimuth_label.setText(str(int(max_info.azimuth)))
            self.max_altitude_label.setText(str(int(max_info.altitude)))
        else:
            self.max_time_utc_label.setText("")
            self.max_time_local_label.setText("")
            self.max_azimuth_label.setText("")
            self.max_altitude_label.setText("")

        # Third contact

        if "C3" in reference_moments:
            # The limb-corrected moment when there is one, the mean-limb moment
            # otherwise.  Scripts schedule against the corrected contacts, so the
            # display has to follow them or the two disagree.
            c3_info: ReferenceMomentInfo = reference_moments.get(
                "C3_LIMB", reference_moments["C3"])
            self.c3_time_utc_label.setText(format_time(c3_info.time_utc, self.time_format))
            self.c3_time_local_label.setText(format_time(c3_info.time_local, self.time_format))
            self.c3_time_local_label.setToolTip(
                f"{format_time(c3_info.time_utc, self.time_format)} UTC")
            self.c3_azimuth_label.setText(str(int(c3_info.azimuth)))
            self.c3_altitude_label.setText(str(int(c3_info.altitude)))
        else:
            self.c3_time_utc_label.setText("")
            self.c3_time_local_label.setText("")
            self.c3_azimuth_label.setText("")
            self.c3_altitude_label.setText("")

        # Fourth contact

        if "C4" in reference_moments:
            c4_info: ReferenceMomentInfo = reference_moments["C4"]
            self.c4_time_utc_label.setText(format_time(c4_info.time_utc, self.time_format))
            self.c4_time_local_label.setText(format_time(c4_info.time_local, self.time_format))
            self.c4_time_local_label.setToolTip(
                f"{format_time(c4_info.time_utc, self.time_format)} UTC")
            self.c4_azimuth_label.setText(str(int(c4_info.azimuth)))
            self.c4_altitude_label.setText(str(int(c4_info.altitude)))
        else:
            self.c4_time_utc_label.setText("")
            self.c4_time_local_label.setText("")
            self.c4_azimuth_label.setText("")
            self.c4_altitude_label.setText("")

        # Sunrise

        sunrise_info: ReferenceMomentInfo = reference_moments["sunrise"]
        self.sunrise_time_utc_label.setText(format_time(sunrise_info.time_utc, self.time_format))
        self.sunrise_time_local_label.setText(format_time(sunrise_info.time_local, self.time_format))

        # Sunset

        sunset_info: ReferenceMomentInfo = reference_moments["sunset"]
        self.sunset_time_utc_label.setText(format_time(sunset_info.time_utc, self.time_format))
        self.sunset_time_local_label.setText(format_time(sunset_info.time_local, self.time_format))

    def closeEvent(self, close_event: QCloseEvent):
        """ Ask for confirmation before closing the application. """
        reply = QMessageBox.question(
            self,
            "Confirm Exit",
            "Are you sure you want to exit Solar Eclipse Workbench?\n\n"
            "Any running scheduler will be stopped.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            self.save_splitter_state()
            # Notify controller so it can clean up cameras, scheduler, etc.
            self.notify_observers(close_event)
            close_event.accept()
        else:
            close_event.ignore()


#: Live view will not open when a frame is closer than this.  It has to start a
#: stream, be looked at, and stop again; opening it into an imminent frame just
#: means the clock tick pauses it before anything can be seen.
LIVE_VIEW_MIN_GAP_S = 30.0

#: How long before a frame the stream is stopped.  Longer than the ~1 s a stop
#: takes, and longer than the exposure write that may follow it, so the camera
#: is unambiguously free when the frame is due.
LIVE_VIEW_CLEAR_BEFORE_S = 8.0

#: Clearance for an exposure write from the live view controls: the write
#: stops the stream, lands, restarts - a few seconds, not the stream's eight.
EXPOSURE_WRITE_CLEAR_S = 4.0


class SolarEclipseController(Observer):
    """ Controller for the Solar Eclipse Workbench UI in the MVC pattern. """

    def __init__(self, model: SolarEclipseModel,
                 view: SolarEclipseView,
                 is_simulator: bool,
                 low_cpu_mode: bool):
        """ Initialisation of the controller of the Solar Eclipse Workbench UI.

        Args:
            - model: Model for the Solar Eclipse Workbench UI
            - view: View for the Solar Eclipse Workbench UI
            - is_simulator: Indicates whether the UI should be started in simulator mode
        """

        self.model = model
        self.jobs_model: Union[JobsTableModel, None] = None
        self.model.camera_overview = CameraOverviewTableModel()

        self.view: SolarEclipseView = view
        self.view.camera_overview.setModel(self.model.camera_overview)
        self.view.camera_overview.resizeColumnsToContents()
        self.view.camera_overview.setColumnWidth(0, 100)
        self.view.add_observer(self)

        self.is_simulator: bool = is_simulator

        self.scheduler: Union[BackgroundScheduler, None] = None
        self._problem_marker_shown = False
        # The bead panel redrawn once a second is a slideshow at the exact
        # moment it matters: the beads live for three or four seconds, so at
        # 1 Hz the ring is two frames of animation.  A 150 ms timer
        # extrapolates the (possibly simulated) clock between ticks and only
        # bothers the panel within two minutes of a contact.
        self._beads_clock = None
        self._beads_fast_timer = QTimer()
        self._beads_fast_timer.setInterval(150)
        self._beads_fast_timer.timeout.connect(self._animate_beads)
        self._beads_fast_timer.start()

        self.sim_reference_moment: Union[str, None] = None
        self.sim_offset_minutes: Union[int, None] = None

        self.location_popup: Union[LocationPopup, None] = None
        self.eclipse_popup: Union[EclipsePopup, None] = None
        self.simulator_popup: Union[SimulatorPopup, None] = None
        self.settings_popup: Union[SettingsPopup, None] = None
        self.relay_popup: Union[RelayPopup, None] = None
        self.relay_trigger: Union[RelayTrigger, None] = None

        self.time_display_timer = QTimer()
        self.time_display_timer.timeout.connect(self.update_time)
        self.time_display_timer.setInterval(1000)
        self.time_display_timer.start()

        # Hardware problems are raised on worker threads, which cannot open a
        # dialog, so they are queued and collected here on the UI thread.
        self._problem_timer = QTimer()
        self._problem_timer.timeout.connect(self._show_camera_problems)
        self._problem_timer.setInterval(2000)
        self._problem_timer.start()

        # Update the eclipse visualization less frequently to save CPU/battery.
        # The main time display remains at 1 Hz; the plot updates every 5 seconds.
        self.visualization_timer = QTimer()
        self.visualization_timer.timeout.connect(self.update_visualization)
        self.visualization_timer.setInterval(5000)

        if not low_cpu_mode:
            self.visualization_timer.start()

        self._live_view_window: Union[LiveViewWindow, None] = None

        self.load_settings()

        # After load_settings: it creates view.settings, and it schedules the
        # reference moments onto the event loop, which has not run yet — so the
        # correction is in place before they are first computed.  Restored before
        # the signal is connected, so start-up triggers no recalculation.
        remembered = self.view.settings.value("limb_correction", True, type=bool)
        self.view.limb_correction_checkbox.setChecked(remembered)
        set_limb_correction_enabled(remembered)
        self.view.limb_correction_checkbox.toggled.connect(self.on_limb_correction_toggled)

    def on_limb_correction_toggled(self, enabled: bool):
        """Apply or drop the lunar limb correction and redo the contact times.

        The times on screen answer the question this checkbox asks, so they are
        recomputed rather than left stale.
        """
        if self._run_in_progress():
            # Defensive even though the checkbox is disabled when jobs are
            # loaded: programmatic changes must not make displayed contacts
            # disagree with already-created absolute DateTriggers.
            blocker = QSignalBlocker(self.view.limb_correction_checkbox)
            self.view.limb_correction_checkbox.setChecked(limb_correction_is_enabled())
            del blocker
            logging.warning("Stop the scheduler before changing the lunar limb correction")
            return

        set_limb_correction_enabled(enabled)
        self.view.settings.setValue("limb_correction", enabled)
        logging.info('Lunar limb correction %s', 'on' if enabled else 'off')

        if self.model.is_location_set and self.model.is_eclipse_date_set:
            self.set_reference_moments()
        self._refresh_beads_panel()

    def _refresh_beads_panel(self):
        """Point the beads panel at the current location and eclipse, if both are set.

        Solving the limb costs a couple of seconds, so the panel only redoes it
        when the place or the date actually changes.
        """
        if not (self.model.is_location_set and self.model.is_eclipse_date_set):
            return
        try:
            date = str(self.model.eclipse_date).split(" ")[0]
            self.view.beads_panel.set_context(date, self.model.longitude,
                                              self.model.latitude, self.model.altitude)
        except Exception as exc:
            logging.warning("Could not update the Baily's beads panel: %s", exc)

    def _set_limb_correction_locked(self, locked: bool):
        """Keep contact settings immutable while absolute jobs are pending."""
        self.view.limb_correction_checkbox.setEnabled(not locked)
        self.view.limb_correction_checkbox.setToolTip(
            LIMB_CORRECTION_LOCKED_TOOLTIP if locked else LIMB_CORRECTION_TOOLTIP)

    def _run_in_progress(self) -> bool:
        """True while a schedule is loaded and running."""
        try:
            return bool(self.scheduler and self.scheduler.get_jobs())
        except Exception:
            return False

    def _show_camera_problems(self):
        """Put queued camera problems where they can be read, not in the way.

        This used to raise a dialog whenever a schedule was not running.  Every
        problem is already in the Problems dock, with its time and level, so
        the dialog said nothing new and had to be dismissed before anything
        else could be done - during setup, which is when problems arrive in
        numbers.

        What is left is the count in the window title, so a problem that
        arrived while the window was behind something else is still noticed.
        """
        try:
            if hardware_problems.count() == 0:
                if self._problem_marker_shown:
                    self.view.setWindowTitle("Solar Eclipse Workbench")
                    self._problem_marker_shown = False
                return

            pending = hardware_problems.peek()
            worst = "error" if any(p.severity == "error" for p in pending) else "warning"
            marker = "\u26d4" if worst == "error" else "\u26a0"
            self.view.setWindowTitle(
                f"{marker} {len(pending)} camera problem(s) - Solar Eclipse Workbench"
            )
            self._problem_marker_shown = True
        except Exception:
            LOGGER.exception("Could not show camera problems")

    def update_time(self):
        """ Update the displayed current time and countdown clocks."""

        current_time_local = datetime.datetime.now()
        current_time_utc = current_time_local.astimezone(tz=datetime.timezone.utc)

        self.model.local_time = current_time_local
        self.model.utc_time = current_time_utc

        # When simulating, the scheduler has moved every command so that the chosen reference
        # moment happens now-ish.  The countdowns must follow that same shift, otherwise they
        # keep showing the real time to the eclipse (days away) while the commands fire.  This
        # is the offset the eclipse visualization already applies in plot(); zero when not
        # simulating, so the normal case is unaffected.
        offset = getattr(self.view.eclipse_visualization, 'offset', datetime.timedelta(0))
        reference_now = current_time_utc + offset

        # The beads follow the same shifted clock: in simulation the eclipse is
        # happening now-ish, and a live view on the real time would sit at
        # "waiting for totality" throughout.
        self.view.beads_panel.set_current_time(reference_now)
        # The fast animation timer extrapolates from here between ticks.
        self._beads_clock = (reference_now, time.monotonic())

        countdown_c1 = self.model.c1_info.time_utc - reference_now if self.model.c1_info else None
        countdown_c2 = self.model.c2_info.time_utc - reference_now if self.model.c2_info else None
        countdown_max = self.model.max_info.time_utc - reference_now if self.model.max_info else None
        countdown_c3 = self.model.c3_info.time_utc - reference_now if self.model.c3_info else None
        countdown_c4 = self.model.c4_info.time_utc - reference_now if self.model.c4_info else None
        countdown_sunrise = self.model.sunrise_info.time_utc - reference_now if self.model.sunrise_info else None
        countdown_sunset = self.model.sunset_info.time_utc - reference_now if self.model.sunset_info else None

        self.view.update_time(current_time_local, current_time_utc, countdown_c1, countdown_c2, countdown_max,
                              countdown_c3, countdown_c4, countdown_sunrise, countdown_sunset)

        # Get live view off the bus before any frame, and keep it off through
        # totality.  This is what makes it safe to leave a preview open while a
        # script is loaded: the stream stops itself in time rather than relying
        # on the observer to remember, and it comes back once the frame is done.
        #
        # Totality is a single stretch rather than a gap between frames because
        # the frames there are dense and unrepeatable, and because the observer
        # is looking at the sky, not the screen.
        if self._live_view_window is not None:
            c2 = self.model.c2_info
            c3 = self.model.c3_info
            _MARGIN = datetime.timedelta(seconds=15)
            in_totality = (
                c2 is not None
                and c3 is not None
                and (c2.time_utc - _MARGIN) <= reference_now <= (c3.time_utc + _MARGIN)
            )
            gap = self._seconds_to_next_frame()
            frame_imminent = gap is not None and gap < LIVE_VIEW_CLEAR_BEFORE_S
            accepts = getattr(self._live_view_window, 'user_accepts_blocking', False)
            if accepts:
                pass          # their eclipse, their call - see _open_fuji_live_view
            else:
                self._live_view_window.set_totality_paused(in_totality or frame_imminent)
            # The exposure controls are never locked by the schedule.  They were
            # - first for the whole run, then near frames, then near frames
            # but less so - and every version of it ended with the person at
            # the telescope shouting "leave the controls to me".  They are
            # right: a write near a frame risks that frame, and whose frame is
            # it?  Theirs.  The only disable left is the seconds a write is
            # physically in flight, which protects the session, not the plan.

        # self.view.eclipse_visualization.plot(current_time_utc)    FIXME

        self._refresh_readiness()
        self.update_jobs_countdown()

    def update_jobs_countdown(self):
        """ Update the countdown of the scheduled jobs. """

        if self.jobs_model:
            self.jobs_model.update_countdown()

    def update_visualization(self):
        """Update the eclipse visualization at a reduced frequency.

        Uses the controller's current UTC time if available; falls back to
        computing the current UTC time if needed.
        """
        try:
            t = getattr(self.model, 'utc_time', None)
            if t is None:
                current_time_local = datetime.datetime.now()
                t = current_time_local.astimezone(tz=datetime.timezone.utc)

            self.view.eclipse_visualization.plot(t)
        except Exception:
            logging.exception("Error updating eclipse visualization")

    def do(self, actions):
        pass

    def update(self, changed_object):
        """ Take action when a notification is received from an observable.

        The following notifications can be received:

            - Change in location at which the solar eclipse will be observed;
            - Change in date at which the solar eclipse will be observed;
            - Change in simulation starting time (only when the UI was started in simulation mode);
            - Change in date and/or time format;
            - Closure of the UI window;
            - One of the buttons in the toolbar of the view is clicked.

        Args:
            - changed_object: Object from which the update was requested
        """

        if isinstance(changed_object, LocationPopup):
            longitude = float(changed_object.longitude.text())
            latitude = float(changed_object.latitude.text())
            altitude = float(changed_object.altitude.text())

            self.model.set_position(longitude, latitude, altitude)

            # Carry over any GPS–computer time offset measured by the USB GPS
            self.model.gps_time_offset = changed_object.location_widget.gps_time_offset

            self.view.longitude_label.setText(str(longitude))
            self.view.latitude_label.setText(str(latitude))
            self.view.altitude_label.setText(str(altitude))

            self.view.eclipse_visualization.set_location(longitude, latitude, altitude)
            self._refresh_beads_panel()

            self._moments_if_ready()
            return

        elif isinstance(changed_object, EclipsePopup):
            # Extract the date portion from the combobox entry (format: "<date> - <type> - ...").
            eclipse_text = changed_object.eclipse_combobox.currentText()
            eclipse_date_str = eclipse_text.split(" - ", 1)[0]
            self.model.set_eclipse_date(
                Time(datetime.datetime.strptime(eclipse_date_str, DATE_FORMATS[self.view.date_format])))

            self.view.eclipse_date.setText(eclipse_date_str)
            self._refresh_beads_panel()
            self._moments_if_ready()
            return

        elif isinstance(changed_object, SimulatorPopup):
            self.sim_reference_moment = changed_object.reference_moment_combobox.currentText()
            self.sim_offset_minutes = (int(changed_object.offset_minutes.text())
                                       * BEFORE_AFTER[changed_object.before_after_combobox.currentText()])
            self._apply_simulation_offset()
            return

        elif isinstance(changed_object, SettingsPopup):
            date_format = changed_object.date_combobox.currentText()
            self.view.date_format = date_format
            if self.model.eclipse_date:
                self.view.eclipse_date.setText(self.model.eclipse_date.strftime(DATE_FORMATS[date_format]))

            time_format = changed_object.time_combobox.currentText()
            self.view.time_format = time_format

            if self.model.c1_info:
                self.view.c1_time_utc_label.setText(format_time(self.model.c1_info.time_utc, time_format))
                self.view.c1_time_local_label.setText(format_time(self.model.c1_info.time_local, time_format))

            if self.model.c2_info:
                self.view.c2_time_utc_label.setText(format_time(self.model.c2_info.time_utc, time_format))
                self.view.c2_time_local_label.setText(format_time(self.model.c2_info.time_local, time_format))

            if self.model.max_info:
                self.view.max_time_utc_label.setText(format_time(self.model.max_info.time_utc, time_format))
                self.view.max_time_local_label.setText(format_time(self.model.max_info.time_local, time_format))

            if self.model.c3_info:
                self.view.c3_time_utc_label.setText(format_time(self.model.c3_info.time_utc, time_format))
                self.view.c3_time_local_label.setText(format_time(self.model.c3_info.time_local, time_format))

            if self.model.c4_info:
                self.view.c4_time_utc_label.setText(format_time(self.model.c4_info.time_utc, time_format))
                self.view.c4_time_local_label.setText(format_time(self.model.c4_info.time_local, time_format))

            if self.model.sunrise_info:
                self.view.sunrise_time_utc_label.setText(format_time(self.model.sunrise_info.time_utc, time_format))
                self.view.sunrise_time_local_label.setText(format_time(self.model.sunrise_info.time_local, time_format))

            if self.model.sunset_info:
                self.view.sunset_time_utc_label.setText(format_time(self.model.sunset_info.time_utc, time_format))
                self.view.sunset_time_local_label.setText(format_time(self.model.sunset_info.time_local, time_format))

            return

        elif isinstance(changed_object, RelayPopup):
            # Connection state already lives on the controller; nothing more to do.
            return

        elif isinstance(changed_object, QCloseEvent):

            if self._live_view_window is not None:
                self._live_view_window.close()
                self._live_view_window = None

            if self.relay_trigger is not None:
                self.relay_trigger.close()
                self.relay_trigger = None
                register_hardware('relay', None)

            if self.model.camera_overview.camera_overview_dict:
                cameras = self.model.camera_overview.camera_overview_dict.values()

                camera: Camera
                for camera in cameras:
                    # Close every camera even if one refuses.  Without this, the
                    # first camera to raise aborts the loop and leaves the rest
                    # open, still holding their USB devices, so the next run
                    # cannot claim them and fails with a device-busy error.
                    try:
                        camera.exit()
                    except Exception:
                        LOGGER.exception("Could not close camera %s",
                                         getattr(camera, "name", camera))

            return

        text = changed_object.text()

        if text == "Location":
            self.location_popup = LocationPopup(self)
            self.location_popup.show()

        elif text == "Date":
            self.eclipse_popup = EclipsePopup(self)
            self.eclipse_popup.show()

        elif text == "Camera(s)":
            logging.debug('User requested Camera(s) update')
            try:
                logging.debug('Calling model.camera_overview.update_camera_overview()')
                # Register a callback so sync+check run after cameras are fully loaded
                self.model.camera_overview.on_ready_callback = self._on_cameras_ready
                self.model.camera_overview.update_camera_overview()
                logging.debug('Returned from update_camera_overview()')
            except Exception:
                logging.exception('Exception while updating camera overview')

        elif text == "Simulator":
            self.simulator_popup = SimulatorPopup(self)
            self.simulator_popup.show()

        elif text == "Relay":
            self.relay_popup = RelayPopup(self)
            self.relay_popup.show()

        elif text == "File":
            # Start in the directory the user picked last time, falling back to their own
            # scripts directory (seeded with the bundled examples on first run).
            settings = QSettings(str(SETTINGS_PATH), QSettings.Format.IniFormat)
            start_dir = settings.value("last_script_dir", "")
            if not start_dir or not os.path.isdir(start_dir):
                start_dir = str(get_scripts_dir())

            filename, _ = QFileDialog.getOpenFileName(None, "Load script", start_dir,
                                                      "Script Files (*.txt);;All Files (*);;Python Files (*.py)")

            if not filename:
                return  # user cancelled the dialog

            settings.setValue("last_script_dir", os.path.dirname(filename))

            if not self.model.reference_moments:
                QMessageBox.warning(
                    self.view,
                    "Reference Moments Not Set",
                    "No eclipse reference moments have been calculated yet.\n\n"
                    "Before loading a script, please:\n"
                    "  1. Set the observation location (Location button)\n"
                    "  2. Set the eclipse date (Date button)\n"
                    "  3. Click \"Reference moments\" to compute contact times\n\n"
                    "Then try loading the script again."
                )
                return

            if not os.path.exists(filename):
                QMessageBox.warning(
                    self.view,
                    "File Not Found",
                    f"The selected file does not exist:\n{filename}"
                )
                return

            # Loading a script always starts a *new* scheduler, so any scheduler from a
            # previously loaded script must be stopped first — otherwise both stay alive and
            # every command fires twice.  This is what previously made a restart necessary to
            # pick up an edited script.
            if self.scheduler:
                LOGGER.info("Replacing the previously loaded script")
                self._shutdown_scheduler()

            # Loading a script always starts a *new* scheduler, so any scheduler from a
            # previously loaded script must be stopped first — otherwise both stay alive and
            # every command fires twice.
            if self.scheduler:
                LOGGER.info("Replacing the previously loaded script")
                self._shutdown_scheduler()

            try:
                from solareclipseworkbench.utils import observe_solar_eclipse
                self.scheduler: BackgroundScheduler
                self.scheduler, unknown_moments \
                    = observe_solar_eclipse(self.model.reference_moments, filename,
                                            self.model.camera_overview.camera_overview_dict, self,
                                            self.sim_reference_moment, self.sim_offset_minutes,
                                            gps_time_offset=self.model.gps_time_offset)

                # Loading is the last moment this can be fixed: afterwards the
                # names are resolved and the missing lines are simply gone.
                if unknown_moments:
                    lost = "\n".join(f"    {name} — {count} line(s) not scheduled"
                                     for name, count in unknown_moments.items())
                    QMessageBox.warning(
                        self.view,
                        "Unknown reference moments",
                        f"The script schedules against reference moments that do not exist:\n\n"
                        f"{lost}\n\n"
                        f"Those lines were skipped; the rest of the script is loaded.\n\n"
                        f"Available: {', '.join(sorted(self.model.reference_moments))}\n\n"
                        f"Limb-corrected moments (C2_LIMB, C3_LIMB, BEADS_*) exist only when the "
                        f"limb correction is on and the lunar limb profile is installed."
                    )

                # A loaded script means the eclipse is the thing to watch, so
                # the bead panel follows the clock from here rather than staying
                # on whatever second was last scrubbed to.  A starting position,
                # not a lock: Free is still there.
                try:
                    self.view.beads_panel.follow_live()
                except Exception:
                    logging.debug("Could not put the bead panel on the clock",
                                  exc_info=True)

                try:
                    self.view.coverage_dock.set_schedule(
                        self.scheduler, self.model.reference_moments)
                    self.view.coverage_dock.show()
                except Exception:
                    logging.debug("Could not draw the coverage", exc_info=True)

                self.jobs_model = JobsTableModel(self.scheduler, self)
                self.view.jobs_table.setModel(self.jobs_model)
                self.jobs_model.add_observer(self.view.jobs_table)
                self.view.jobs_table.resizeColumnsToContents()

                # Detection opens SDK sessions and resets the camera daemons -
                # both proven session-killers - so it is off while a schedule
                # owns the body.  The button says so, because a greyed button
                # with no reason just reads as broken.
                self.view.camera_action.setDisabled(True)
                self.view.camera_action.setToolTip(
                    "Detection is off while a script is loaded - press STOP first")

                n_jobs = len(self.scheduler.get_jobs())
                self._set_limb_correction_locked(n_jobs > 0)
                if n_jobs == 0:
                    cam_keys = list(
                        (self.model.camera_overview.camera_overview_dict or {}).keys()
                    )
                    QMessageBox.warning(
                        self.view,
                        "No Jobs Scheduled",
                        f"The script was loaded but no commands were scheduled.\n\n"
                        f"This usually means the camera name in the script does not match "
                        f"the name of a detected camera.\n\n"
                        f"Detected cameras: {cam_keys or '(none — click Camera(s) first)'}\n\n"
                        f"Check that each camera name in the script exactly matches one of "
                        f"the names shown in the Camera(s) overview table above."
                    )

            except IndexError:
                LOGGER.warning(f"File {filename} does not contain scheduled jobs")

        elif text == "Stop":
            # Ask for confirmation before stopping the scheduler
            if not hasattr(self, 'scheduler') or not self.scheduler or not self.scheduler.get_jobs():
                # No active jobs → no need to confirm
                self._shutdown_scheduler()
                return

            reply = QMessageBox.question(
                self.view,
                "Confirm Stop",
                "Are you sure you want to stop the scheduler?\n\n"
                "All pending jobs will be cancelled.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )

            if reply == QMessageBox.StandardButton.Yes:
                self._shutdown_scheduler()

        elif text == "Datetime format":
            self.settings_popup = SettingsPopup(self)
            self.settings_popup.show()

        elif text == "Save":
            self.view.save_settings()

        elif text == "Live View":
            self._open_live_view()

        elif text == "Refresh Plot":
            self.update_visualization()

    def sync_camera_time(self):
        """ Set the time of all connected cameras to the time of the computer."""

        self.model.sync_camera_time()

    def _on_cameras_ready(self):
        """Called by CameraOverviewTableModel after camera dict is populated.

        Runs sync_camera_time and check_camera_state on the GUI thread so they
        always have access to the fully-populated camera_overview_dict.
        """
        try:
            logging.debug('_on_cameras_ready: syncing camera time')
            self.sync_camera_time()
        except Exception:
            logging.exception('Exception while syncing camera time')
        try:
            logging.debug('_on_cameras_ready: checking camera state')
            rows = self._camera_settings_rows()
            if rows:
                CameraSettingsDialog(rows, self.view).exec()
        except Exception:
            logging.exception('Exception while checking camera state')

    def _camera_settings_rows(self):
        """Each body's outstanding settings as (severity, camera, setting, needs, is).

        Bodies that can describe their own state - the Fuji, through the SDK -
        give the setting, the required value and the current one separately.
        Anything else has only a sentence to offer, which goes in the setting
        column rather than being parsed into columns it never had.
        """
        rows = []
        overview = getattr(self.model, 'camera_overview', None)
        cameras = getattr(overview, 'camera_overview_dict', None) or {}
        seen: set = set()
        for name, camera in cameras.items():
            if id(camera) in seen:
                continue
            seen.add(id(camera))
            validate = getattr(camera, 'validate', None)
            if validate is None:
                continue
            try:
                issues = validate() or []
            except Exception:
                logging.debug('Could not validate %s', name, exc_info=True)
                continue
            for issue in issues:
                if issue.severity in ('error', 'warning'):
                    rows.append((issue.severity, getattr(camera, 'name', name),
                                 issue.setting, issue.expected, issue.current))

        for warning in self.check_camera_state_warnings():
            rows.append(('warning', '', warning, '', ''))

        rows.sort(key=lambda r: 0 if r[0] == 'error' else 1)
        return rows

    def check_camera_state_warnings(self):
        """The plain-sentence warnings from bodies that cannot say more."""
        try:
            return self.model.check_camera_state() or []
        except Exception:
            logging.debug('check_camera_state failed', exc_info=True)
            return []

    def _open_fuji_live_view(self, camera):
        """Open the SDK live view for a Fuji body, if there is room before the
        next frame.

        Live view opens a video stream on the same USB session the eclipse runs
        on and takes PC priority to do it, so it must not be running when a
        frame is due.  What it must NOT do is refuse for the whole run: the
        script is loaded from twenty minutes before first contact, and the hour
        of partials after that is exactly when focus wants checking - on a
        telescope it drifts as the tube cools.  So the test is how long until
        the next frame, not whether a schedule exists, and the stream closes
        itself before that frame rather than waiting to be told.
        """
        # Allowed while a script is loaded, because focus has to be checked in
        # the last minutes before totality - a tube still cooling drifts, and
        # that is exactly when the old rule refused.
        #
        # It refused for a reason: live view took the camera off the USB bus in
        # both rehearsals on 4 August.  Since then all three mechanisms have
        # been found and fixed - gphoto2 claiming the USB device out from under
        # the SDK, the variadic ABI bug that was generating the segfaults, and
        # live view left running by a crashed process wedging priority.  The
        # comment that justified this said "why is not yet understood"; it is
        # understood now, so the blanket refusal has outlived its evidence.
        #
        # What replaces it is the question that actually matters: how long until
        # a frame.  Not "is a job due" - most jobs around second contact are
        # voice prompts, which touch nothing - and not "does a schedule exist".
        # The clock tick closes the stream before the frame and through totality
        # (see _tick), so this only has to refuse when a frame is imminent.
        gap = self._seconds_to_next_frame()
        accepts_blocking = False
        if gap is not None and gap < LIVE_VIEW_MIN_GAP_S:
            # A choice, not a refusal.  The guard used to treat the schedule as
            # sacred, but the schedule exists to photograph a focused sun: if
            # focus has drifted at C2 minus two minutes, the frames it protects
            # are worthless, and the person at the telescope is the only one
            # who can weigh that.  The consequences are stated, not hidden.
            reply = QMessageBox.question(
                self.view,
                "A frame is due in %.0f s" % gap,
                "The schedule takes its next frame in %.0f seconds.\n\n"
                "Open live view anyway?  While it stays open, scheduled\n"
                "frames - including the contact bursts - may be LOST.\n"
                "It will not close itself; close it as soon as focus is done."
                % gap,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                logging.info('Live view declined: a frame is due in %.0fs', gap)
                return
            accepts_blocking = True
            logging.warning('Live view is overriding the schedule by choice: '
                            'frames due while it is open may be lost')

        from solareclipseworkbench.liveview import LiveViewWindow
        if self._live_view_window is not None:
            try:
                self._live_view_window.close()
            except Exception:
                logging.debug("Could not close the previous live view", exc_info=True)

        # The adapter, not the bare handle: the window needs its lock.
        window = LiveViewWindow(camera, self.view)
        # Consent travels with this window and dies with it: the clock tick
        # leaves an overriding live view alone rather than closing it under
        # the person who just said they need it.
        window.user_accepts_blocking = accepts_blocking
        self.view.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, window)
        window.setFloating(True)
        window.show()
        self._live_view_window = window

        logging.info('Live view opened for %s', getattr(camera, 'name', 'the Fuji body'))

    def _seconds_to_next_frame(self):
        """Seconds until the next job that needs the camera, or None.

        Jobs that leave the camera alone do not count.  Counting them is what
        made this useless in the last minute before totality, where the
        schedule is dense with voice prompts and holds no frame at all.
        """
        return seconds_to_next_camera_job(getattr(self, 'scheduler', None))

    def _open_live_view(self):
        """Open (or bring to front) the live view window.

        When exactly one real camera is connected it is used directly.
        When multiple real cameras are connected a small selection dialog
        is shown so the user can pick which one to preview.
        Shows a warning when no real camera is connected.
        """
        # If window already exists, bring it to the front
        if self._live_view_window is not None and self._live_view_window.isVisible():
            self._live_view_window.activateWindow()
            self._live_view_window.raise_()
            return

        # Collect all real gphoto cameras (deduplicated by object identity).
        # Use cam.name as the display label: when an alias is configured it is
        # set to the primary alias (i.e. the name used in scripts); when no alias
        # is configured it falls back to the gphoto2 model name.
        cam_dict = getattr(self.model.camera_overview, 'camera_overview_dict', None) or {}
        from solareclipseworkbench.camera import GPhotoCameraAdapter, VirtualCamera
        seen_ids: set = set()
        real_cameras: list = []  # list of (display_name, camera) tuples
        for cam in cam_dict.values():
            if isinstance(cam, (GPhotoCameraAdapter, VirtualCamera)) and id(cam) not in seen_ids:
                seen_ids.add(id(cam))
                real_cameras.append((cam.name, cam))

        if not real_cameras:
            # A body driven through its own SDK rather than gphoto2 is connected
            # and shooting perfectly well; it simply has no preview path here.
            # Saying "no camera connected" while the camera is visibly firing
            # sends the user hunting for a fault that does not exist.
            sdk_cameras = [cam for cam in cam_dict.values()
                           if not isinstance(cam, (GPhotoCameraAdapter, VirtualCamera))
                           and getattr(cam, '_sdk_cam', None) is not None]
            if sdk_cameras:
                self._open_fuji_live_view(sdk_cameras[0])
            else:
                QMessageBox.warning(
                    self.view,
                    "No Camera Connected",
                    "Live view requires a connected camera.\n\n"
                    "Click the Camera(s) button first to detect connected cameras.\n"
                    "In simulator mode the VirtualCamera is also supported."
                )
            return

        if len(real_cameras) == 1:
            camera = real_cameras[0][1]
        else:
            # Ask the user which camera to preview
            names = [name for name, _ in real_cameras]
            chosen, ok = QtWidgets.QInputDialog.getItem(
                self.view,
                "Select Camera for Live View",
                "Camera:",
                names,
                0,
                False,
            )
            if not ok:
                return
            camera = dict(real_cameras)[chosen]

        self._live_view_window = LiveViewWindow(camera, parent=None)
        self._live_view_window.show()

    def load_settings(self):
        """ Load the UI settings.

        These settings are:

            - Date format (always present);
            - Time format (always present);
            - Location (longitude, latitude, altitude);
            - Eclipse date.

        If the location and eclipse date are present in the settings file, the reference moments will be updated
        automatically.
        """

        self.view.settings = QSettings(str(SETTINGS_PATH), QSettings.Format.IniFormat)

        # Date & time format
        # TODO Requires Python 3.7

        default_date_format, *_ = DATE_FORMATS
        date_format = self.view.settings.value("date_format", default_date_format, type=str)
        default_time_format, *_ = TIME_FORMATS
        time_format = self.view.settings.value("time_format", default_time_format, type=str)
        self.set_datetime_format(date_format, time_format)

        # Location

        is_location_loaded = self.set_location(self.view.settings.value("longitude", None, type=float),
                                               self.view.settings.value("latitude", None, type=float),
                                               self.view.settings.value("altitude", None, type=float))

        # Eclipse date

        is_eclipse_date_loaded = self.set_eclipse_date(self.view.settings.value("eclipse_date", None, type=str),
                                                       date_format)

        # Reference moments

        if is_location_loaded and is_eclipse_date_loaded:
            # Defer reference-moments calculation so the main window can appear
            # before any potential ephemeris downloads. Schedule it on the
            # Qt event loop to run after the UI has been shown.
            try:
                QTimer.singleShot(0, self.set_reference_moments)
            except Exception:
                # Fall back to synchronous call if scheduling fails for any reason
                try:
                    self.set_reference_moments()
                except Exception:
                    pass

    def set_datetime_format(self, date_format: str, time_format: str):
        """ Set the date and time format in the view. """

        self.view.date_format = date_format
        self.view.time_format = time_format

    def set_location(self, longitude: float, latitude: float, altitude: float) -> bool:
        """ Set the observing location in the model and the view.

        Args:
            - longitude: Longitude of the location [degrees]
            - latitude: Latitude of the location [degrees]
            - altitude: Altitude of the location [meters]

        Returns: True if the location was set, false otherwise.
        """


        if longitude and latitude and altitude:
            self.model.set_position(longitude, latitude, altitude)

            self.view.longitude_label.setText(str(longitude))
            self.view.latitude_label.setText(str(latitude))
            self.view.altitude_label.setText(str(altitude))

            self.view.eclipse_visualization.set_location(longitude, latitude, altitude)

            return True

        return False

    def set_eclipse_date(self, eclipse_date: str, date_format: str = None) -> bool:
        """ Set the eclipse date in the model and the view.

        Args:
            - eclipse_date: Eclipse date
            - date_format: Date format for the given eclipse date (if None, the date format is %Y-%m-%d)

        Returns: True if the eclipse date was set, false otherwise.
        """

        if eclipse_date:
            if date_format:
                dt = datetime.datetime.strptime(eclipse_date, DATE_FORMATS[date_format])
                date = datetime.datetime.strftime(dt, "%Y-%m-%d")
                self.view.eclipse_date.setText(eclipse_date)
            else:
                date = datetime.datetime.strptime(eclipse_date, "%Y-%m-%d")
                self.view.eclipse_date.setText(date.strftime(DATE_FORMATS[self.view.date_format]))

            self.model.set_eclipse_date(Time(date))
            self._refresh_beads_panel()
            return True

        return False

    def _refresh_readiness(self):
        """Fill the strip's readiness answers from state already in memory.

        Runs every clock tick, so nothing here may touch the camera or the
        bus: a body is "connected" if an adapter is registered, not because it
        was just asked.
        """
        try:
            overview = getattr(self.model, 'camera_overview', None)
            cameras = getattr(overview, 'camera_overview_dict', None) or {}
            names = {getattr(cam, 'name', str(name)) for name, cam in cameras.items()}
            trigger = getattr(self, 'relay_trigger', None)
            scheduler = getattr(self, 'scheduler', None)
            jobs = len(scheduler.get_jobs()) if scheduler is not None else 0

            self.view.refresh_readiness_colours()
            for action, unset in (
                    (self.view.location_action, not self.model.is_location_set),
                    (self.view.date_action, not self.model.is_eclipse_date_set),
                    (self.view.camera_action, not names),
                    (self.view.relay_action, trigger is None),
                    (self.view.file_action, not jobs)):
                self.view.set_action_unset(action, unset)
        except Exception:
            logging.debug("Could not refresh the readiness strip", exc_info=True)

    def _animate_beads(self):
        """Give the bead panel smooth time near the contacts.

        Extrapolates the last tick's reference clock, so simulation offsets
        are carried for free, and stays quiet when nothing is within two
        minutes of a contact - at 1 Hz elsewhere nobody can tell.
        """
        try:
            if self._beads_clock is None:
                return
            reference, base = self._beads_clock
            now = reference + datetime.timedelta(seconds=time.monotonic() - base)
            for info in (self.model.c2_info, self.model.c3_info):
                if info is not None and abs((info.time_utc - now).total_seconds()) < 120:
                    self.view.beads_panel.set_current_time(now)
                    return
        except Exception:
            logging.debug("Bead animation tick failed", exc_info=True)

    def _apply_simulation_offset(self):
        """Move the countdowns onto the simulated clock straight away.

        The offset was only ever set when a schedule was started, so choosing a
        different reference moment left every countdown on the previous one -
        or, before any run, on the real time to an eclipse days away.  The
        contact times themselves do not change: they depend on a place and a
        date, and the simulator changes neither.  What moves is now.
        """
        from solareclipseworkbench.utils import simulation_offset

        moments = self.model.reference_moments
        if not moments:
            return
        offset = simulation_offset(moments, self.sim_reference_moment,
                                   self.sim_offset_minutes)
        self.view.eclipse_visualization.set_offset(offset)
        logging.info('Simulating %s%+d minutes: the clock reads %s ahead',
                     self.sim_reference_moment or 'nothing',
                     self.sim_offset_minutes or 0, offset)
        self.update_time()

    def _moments_if_ready(self):
        """Work out the contact times as soon as both halves of the question exist.

        The times depend on a place and a date and on nothing else, so once both
        are known there is nothing left to ask for.  Only start-up did this, and
        only when both had been saved from a previous run: choosing either from
        the toolbar left the contact times blank, or worse, showing the previous
        site's, until the reference-moments button was pressed as well.
        """
        if self.model.is_location_set and self.model.is_eclipse_date_set:
            self.set_reference_moments()

    def set_reference_moments(self):
        """ Set the reference moments of the eclipse in the model and the view."""
        # Run the possibly-slow calculation in a background thread while showing a
        # compact modal dialog so the user sees that helper files are being loaded.
        dialog = QDialog(self.view)
        dialog.setWindowTitle("Loading helper files...")
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg_layout = QVBoxLayout(dialog)

        progress_bar = QProgressBar()
        progress_bar.setRange(0, 0)  # indeterminate
        dlg_layout.addWidget(progress_bar)

        label = QLabel("Loading helper files...")
        dlg_layout.addWidget(label)

        dialog.setLayout(dlg_layout)

        result_container = {}

        class _DevNull:
            def write(self, s):
                return

            def flush(self):
                return

        def worker():
            old_out = sys.stdout
            old_err = sys.stderr
            sys.stdout = _DevNull()
            sys.stderr = _DevNull()
            import logging
            root_logger = logging.getLogger()
            old_handlers = list(root_logger.handlers)
            old_level = root_logger.level
            try:
                # Prevent external libraries from printing to terminal while
                # downloads happen.
                root_logger.handlers = []
                reference_moments, magnitude, eclipse_type = self.model.get_reference_moments()
                result_container["result"] = (reference_moments, magnitude, eclipse_type)
            except Exception:
                import traceback

                result_container["error"] = traceback.format_exc()
            finally:
                sys.stdout = old_out
                sys.stderr = old_err
                root_logger.handlers = old_handlers
                root_logger.setLevel(old_level)

        th = threading.Thread(target=worker, daemon=True)

        # Show dialog immediately so it is visible before downloads start.
        try:
            dialog.show()
            QApplication.processEvents()
        except Exception:
            pass

        LOGGER.debug("Starting reference-moments worker thread (controller)")

        th.start()

        timer = QTimer(dialog)

        def pump_and_close():
            if not th.is_alive():
                timer.stop()
                dialog.accept()

        timer.timeout.connect(pump_and_close)
        timer.start(100)

        dialog.exec()

        if "error" in result_container:
            LOGGER.exception("Error while calculating reference moments")
            QMessageBox.critical(
                self.view,
                "Reference moments failed",
                f"Error calculating reference moments:\n{result_container['error']}"
            )
        else:
            reference_moments, magnitude, eclipse_type = result_container["result"]
            self.view.show_reference_moments(reference_moments, magnitude, eclipse_type)

    def _shutdown_scheduler(self):
        """Safely shut down the scheduler and update UI.

        STOP is the make-everything-safe button, so it closes live view too:
        a stream left holding the camera and PC priority after the schedule is
        gone is exactly the state that has cost sessions all week - and
        whoever pressed STOP wants the camera back, not a preview.  This also
        clears a blocking override, which must not outlive the schedule it was
        overriding.
        """
        try:
            if self.scheduler:
                # Not wait=True: that blocks until the job in flight finishes,
                # and during totality that is a whole bracket.
                self.scheduler.shutdown(wait=False)
                if self.jobs_model:
                    self.jobs_model.clear_jobs_overview()
                self.view.camera_action.setEnabled(True)
                self.view.camera_action.setToolTip("Detect the connected cameras")
                LOGGER.info("Scheduler stopped by user")
        except SchedulerNotRunningError:
            pass  # already stopped
        except Exception:
            logging.exception("Error while shutting down scheduler")
        finally:
            self._set_limb_correction_locked(False)

        window = getattr(self, '_live_view_window', None)
        if window is not None:
            try:
                LOGGER.info("Closing live view with the schedule")
                window.close()
            except Exception:
                logging.exception("Could not close live view on stop")
            self._live_view_window = None


class LocationPopup(QWidget, Observable):
    def __init__(self, observer: SolarEclipseController):
        """ Initialisation of a pop-up window for setting the observing location.

        A pop-up window is shown, in which the user can choose the following information about the observing location:

            - Longitude [degrees];
            - Latitude [degrees];
            - Altitude [meters].

        The window also provides a saved-locations drop-down and an address-search bar (when geopy is installed).  When
        pressing the "Plot" button, the location will be displayed on a world map (as a red dot).  When pressing the
        "OK" button, the controller will be notified.

        If the location had already been set before, the coordinate fields will be pre-filled.

        Args:
            - observer: SolarEclipseController that needs to be notified about the selection of a new location.
        """

        QWidget.__init__(self)
        self.setWindowTitle("Location")
        self.setGeometry(QRect(100, 100, 1000, 800))
        self.add_observer(observer)

        model = observer.model

        layout = QVBoxLayout()

        # Shared location widget: saved-locations drop-down + address search + coordinate fields.
        config_manager = ConfigManager()
        self.location_widget = LocationWidget(config_manager)
        # Only fall back to model coordinates when no saved location was restored
        # by the widget (i.e. the combo is still on "Custom"). If a saved location
        # was restored, its own coordinates should be shown, not the ones from the
        # .SolarEclipseWorkbench.ini file.
        if model.longitude is not None and self.location_widget.location_combo.currentText() == "Custom":
            self.location_widget.set_coordinates(
                model.longitude, model.latitude, model.altitude
            )
        layout.addWidget(self.location_widget)

        # Detailed map of the site, with the world map next to it for context.
        self.tile_map = TileMap()
        self.location_plot = LocationPlot()
        self.location_plot.setMaximumWidth(360)

        maps_layout = QHBoxLayout()
        maps_layout.addWidget(self.tile_map, stretch=3)
        maps_layout.addWidget(self.location_plot, stretch=1)
        layout.addLayout(maps_layout)

        zoom_layout = QHBoxLayout()
        zoom_layout.addWidget(QLabel("Zoom"))
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(MIN_ZOOM, MAX_ZOOM)
        self.zoom_slider.setValue(self.tile_map.zoom())
        self.zoom_slider.valueChanged.connect(self.tile_map.set_zoom)
        zoom_layout.addWidget(self.zoom_slider)
        self.zoom_label = QLabel(f"{self.tile_map.zoom()}")
        self.zoom_label.setFixedWidth(30)
        self.zoom_slider.valueChanged.connect(lambda value: self.zoom_label.setText(f"{value}"))
        self.tile_map.zoom_changed.connect(self.zoom_slider.setValue)
        zoom_layout.addWidget(self.zoom_label)
        layout.addLayout(zoom_layout)

        ok_button = QPushButton("OK")
        ok_button.clicked.connect(self.accept_location)
        ok_button.setFixedWidth(100)
        layout.addWidget(ok_button)

        self.setLayout(layout)

        # Auto-plot: debounce coordinate changes so the map refreshes 300 ms
        # after the user stops typing or after a saved/geocoded location is applied.
        self._plot_timer = QTimer(self)
        self._plot_timer.setSingleShot(True)
        self._plot_timer.setInterval(300)
        self._plot_timer.timeout.connect(self.plot_location)

        self.location_widget.longitude_edit.textChanged.connect(self._schedule_auto_plot)
        self.location_widget.latitude_edit.textChanged.connect(self._schedule_auto_plot)

        # Plot whatever coordinates are currently in the fields — covers both the
        # case where the model already had a location and the case where LocationWidget
        # restored the last-used saved location during its own initialisation.
        self.plot_location()

    # ------------------------------------------------------------------
    # Backward-compatible properties so the controller can still do
    #   changed_object.longitude.text() / .latitude.text() / .altitude.text()
    # ------------------------------------------------------------------

    @property
    def longitude(self):
        """Return the longitude QLineEdit from the embedded LocationWidget."""
        return self.location_widget.longitude_edit

    @property
    def latitude(self):
        """Return the latitude QLineEdit from the embedded LocationWidget."""
        return self.location_widget.latitude_edit

    @property
    def altitude(self):
        """Return the altitude QLineEdit from the embedded LocationWidget."""
        return self.location_widget.altitude_edit

    def _schedule_auto_plot(self):
        """Restart the debounce timer whenever a coordinate field changes."""
        self._plot_timer.start()

    def plot_location(self):
        """Plot the selected location on the world map.

        Silently ignored when longitude or latitude are empty or not yet valid numbers.
        """
        try:
            lon = float(self.longitude.text())
            lat = float(self.latitude.text())
        except ValueError:
            return
        self.location_plot.plot_location(longitude=lon, latitude=lat)
        name = self.location_widget.location_combo.currentText()
        self.tile_map.set_location(lon, lat, label="" if name == "Custom" else name)

    def accept_location(self):
        """ Notify the observer about the selection of a new location and close the pop-up window.

        Check:
            - longitude specified
            - latitude specified
            - altitude specified
        """

        if self.longitude.text() and self.latitude.text() and self.altitude.text():
            self.notify_observers(self)
            self.close()


class EclipsePopup(QWidget, Observable):

    def __init__(self, observer: SolarEclipseController):
        """ Initialisation of a pop-up window for setting the eclipse date.

        A pop-up window is shown, in which the user can choose the date of the eclipse.

        When pressing the "OK" button, the given controller will be notified about this.

        If the eclipse date had already been set before, this will be shown in the combobox.

        Args:
            - observer: SolarEclipseController that needs to be notified about the selection of a new location.
        """

        QWidget.__init__(self)
        self.setWindowTitle("Eclipse date")
        self.setGeometry(QRect(100, 100, 400, 75))
        self.add_observer(observer)

        self.eclipse_combobox = QComboBox()

        date_format = DATE_FORMATS[observer.view.date_format]

        formatted_eclipse_dates = []

        from solareclipseworkbench.utils import calculate_next_solar_eclipses
        for eclipse_date in calculate_next_solar_eclipses(20):
            formatted_eclipse_date = datetime.datetime.strptime(eclipse_date['date'], "%d/%m/%Y").strftime(date_format) + " - " + eclipse_date['type']
            if eclipse_date['type'] == "T" or eclipse_date['type'] == "A" or eclipse_date['type'] == "H":
                # For total, annular, and hybrid eclipses, also show the duration
                duration = eclipse_date["duration"]
                # Convert duration to minutes:seconds
                minutes, seconds = divmod(duration, 60)
                formatted_eclipse_date += f" - {int(minutes)}m {int(seconds):02}s"
                formatted_eclipse_dates.append(formatted_eclipse_date)
            else:
                formatted_eclipse_dates.append(formatted_eclipse_date + " - " + str(int(eclipse_date["magnitude"] * 100)) + "%")

        self.eclipse_combobox.addItems(formatted_eclipse_dates)

        ok_button = QPushButton("OK")
        ok_button.clicked.connect(self.load_eclipse_date)

        layout = QHBoxLayout()

        layout.addWidget(self.eclipse_combobox)
        layout.addWidget(ok_button)
        self.setLayout(layout)

    def load_eclipse_date(self):
        """ Notify the observer about the selection of a new eclipse date and close the pop-up window."""

        self.notify_observers(self)
        self.close()


class SimulatorPopup(QWidget, Observable):
    def __init__(self, observer: SolarEclipseController):
        """ Initialisation of pop-up window to specify the start time of the simulation.

        Args:
            - observer: SolarEclipseController that needs to be notified about the specification of the start time of
                        the simulation
        """

        QWidget.__init__(self)
        self.setWindowTitle("Starting time")
        self.setGeometry(QRect(100, 100, 300, 75))
        self.add_observer(observer)

        # noinspection SpellCheckingInspection
        hbox1 = QHBoxLayout()
        # noinspection SpellCheckingInspection
        hbox2 = QHBoxLayout()

        self.offset_minutes = QLineEdit()
        offset_minutes_validator = QIntValidator()
        self.offset_minutes.setValidator(offset_minutes_validator)

        self.before_after_combobox = QComboBox()
        self.before_after_combobox.addItems(BEFORE_AFTER.keys())

        if observer.sim_offset_minutes:
            self.offset_minutes.setText(str(abs(observer.sim_offset_minutes)))

            if observer.sim_offset_minutes < 0:
                self.before_after_combobox.setCurrentText("after")
            else:
                self.before_after_combobox.setCurrentText("before")

        self.reference_moment_combobox = QComboBox()

        # Populate the reference-moment combobox based on the model's computed reference moments.
        # If a moment is not present in the model (e.g. no C2/C3 for partial eclipses, or no C1/C4/MAX
        # for no-eclipse), it will not be offered as a simulation start point.
        model_ref = getattr(observer.model, 'reference_moments', None)
        if model_ref:
            options = []
            for key in ["C1", "C2", "MAX", "C3", "C4", "sunset", "sunrise"]:
                if key in model_ref:
                    options.append(key)
            # If nothing was detected (defensive), fall back to the full list
            if not options:
                options = list(REFERENCE_MOMENTS)
            self.reference_moment_combobox.addItems(options)
        else:
            # No reference moments computed yet; show full list so the user can pick (or compute moments first)
            self.reference_moment_combobox.addItems(REFERENCE_MOMENTS)

        # Restore previously chosen simulator reference moment if still available, otherwise choose a sensible default
        if observer.sim_reference_moment:
            available = [self.reference_moment_combobox.itemText(i) for i in range(self.reference_moment_combobox.count())]
            if observer.sim_reference_moment in available:
                self.reference_moment_combobox.setCurrentText(observer.sim_reference_moment)
            else:
                if "MAX" in available:
                    self.reference_moment_combobox.setCurrentText("MAX")
                elif available:
                    self.reference_moment_combobox.setCurrentIndex(0)

        layout = QVBoxLayout()

        hbox1.addWidget(self.offset_minutes)
        hbox1.addWidget(QLabel("minute(s)"))
        hbox1.addWidget(self.before_after_combobox)
        hbox1.addWidget(self.reference_moment_combobox)

        ok_button = QPushButton("OK")
        ok_button.clicked.connect(self.accept_starting_time)

        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.cancel_starting_time)

        hbox2.addWidget(ok_button)
        hbox2.addWidget(cancel_button)

        layout.addLayout(hbox1)
        layout.addLayout(hbox2)

        self.setLayout(layout)

    def accept_starting_time(self):
        """ Notify the observer about specification of the starting time of the simulation and close the pop-up window.

        Check:
            - offset specified
        """

        if self.offset_minutes.text():
            self.notify_observers(self)
            self.close()

    def cancel_starting_time(self):
        """ Close the pop-up window. """

        self.close()


class CameraSettingsDialog(QDialog):
    """What has to be set on each body, and what it is set to now.

    A list, not prose.  The validator already returns the setting, what it must
    be and what it is; those were being flattened into sentences that had to be
    read through to find the one word that mattered, at the moment there is
    least time to read anything.
    """

    MARKS = {"error": ("FIX", "#c0392b"), "warning": ("?", "#d68910")}

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Camera settings")

        layout = QVBoxLayout(self)
        table = QTableWidget(len(rows), 5)
        table.setHorizontalHeaderLabels(["", "Camera", "Setting", "Needs", "Is"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)

        for row, (severity, camera, setting, needs, is_now) in enumerate(rows):
            mark, colour = self.MARKS.get(severity, ("?", "#d68910"))
            for column, text in enumerate((mark, camera, setting, needs, is_now)):
                item = QTableWidgetItem(str(text))
                if column in (0, 3, 4):
                    item.setFont(mono)
                item.setForeground(QColor(colour))
                table.setItem(row, column, item)

        header = table.horizontalHeader()
        for column in range(4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        table.resizeRowsToContents()
        layout.addWidget(table)

        buttons = QHBoxLayout()
        buttons.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.resize(720, min(150 + 24 * len(rows), 520))


class ProblemsDock(QDockWidget):
    """Every warning and error the run produces, on screen instead of in a file.

    A problem that only reaches the log is a problem nobody sees until
    afterwards, and afterwards is too late for an eclipse.

    Warnings and errors only: the information lines are the schedule doing its
    job and would bury the rest.  Nothing here is specific to any device - it
    is whatever was logged, from wherever.
    """

    LEVELS = {logging.WARNING: "WARN", logging.ERROR: "ERROR",
              logging.CRITICAL: "FATAL"}
    COLOURS = {"ERROR": "#c0392b", "FATAL": "#c0392b", "WARN": "#d68910"}
    MAX_ROWS = 500

    #: Records arrive on whatever thread logged them - scheduler pool threads
    #: included - and a Qt widget touched off the GUI thread is undefined
    #: behaviour.
    logged = pyqtSignal(str, str, str)

    def __init__(self, parent=None):
        super().__init__("Error Log", parent)
        self.setObjectName("problems_dock")

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 4, 4, 4)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Time", "", "Problem"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)

        buttons = QHBoxLayout()
        self.count_label = QLabel("no problems")
        buttons.addWidget(self.count_label)
        buttons.addStretch()
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear)
        buttons.addWidget(clear)
        layout.addLayout(buttons)

        self.setWidget(body)
        self.logged.connect(self._append)
        logging.getLogger().addHandler(_DockLogHandler(self))

    def _append(self, when: str, level: str, message: str):
        row = self.table.rowCount()
        self.table.insertRow(row)
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        colour = self.COLOURS.get(level)
        for column, text in enumerate((when, level, message)):
            item = QTableWidgetItem(text)
            if column != 2:
                item.setFont(mono)
            if colour:
                item.setForeground(QColor(colour))
            self.table.setItem(row, column, item)
        while self.table.rowCount() > self.MAX_ROWS:
            self.table.removeRow(0)
        self.table.scrollToBottom()
        self._recount()

    def clear(self):
        self.table.setRowCount(0)
        self._recount()

    #: Emitted whenever the number of lines changes, so a toolbar button can
    #: carry the count without polling for it.
    count_changed = pyqtSignal(int)

    def _recount(self):
        rows = self.table.rowCount()
        errors = sum(1 for row in range(rows)
                     if (self.table.item(row, 1) or QTableWidgetItem("")).text()
                     in ("ERROR", "FATAL"))
        self.count_label.setText(
            "no problems" if not rows
            else "%d logged, %d error%s" % (rows, errors, "" if errors == 1 else "s"))
        self.count_changed.emit(rows)


class _DockLogHandler(logging.Handler):
    """Feeds the dock without ever being able to break logging."""

    def __init__(self, dock: ProblemsDock):
        super().__init__(level=logging.WARNING)
        self._dock = dock

    def emit(self, record):
        try:
            self._dock.logged.emit(
                datetime.datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                ProblemsDock.LEVELS.get(record.levelno, record.levelname),
                record.getMessage().split("\n")[0])
        except Exception:
            pass          # a broken log view must not break the run


class MountDock(QDockWidget):
    """Connect to the mount and watch it, without leaving the main window.

    During a run the mount is either tracking or it quietly is not, and the
    difference has to be visible from across a field — hence a dock that can sit
    open beside the schedule, float onto a second screen, or be closed.

    The connected driver is registered with the hardware registry, so the
    mount_* commands in an eclipse script find it when they fire.
    """

    #: Carries a finished background connect back to the UI thread.  Serial
    #: probing walks every port and can take seconds, which would otherwise
    #: freeze the window.
    connected = pyqtSignal(object, str)

    POLL_MS = 2000

    def __init__(self, parent=None):
        super().__init__("Mount", parent)
        self.setObjectName("mount_dock")
        self.mount: Optional[MountDriver] = None
        self._busy = False

        body = QWidget()
        layout = QVBoxLayout(body)

        # ---------------------------------------------------------- connection
        connect_box = QGroupBox("Connection")
        connect_grid = QGridLayout(connect_box)

        self.driver_combo = QComboBox()
        self.driver_combo.addItem("Find automatically", None)
        for driver_class in list_drivers():
            self.driver_combo.addItem(
                getattr(driver_class, "display_name", driver_class.name), driver_class.name)
        connect_grid.addWidget(QLabel("Driver"), 0, 0)
        connect_grid.addWidget(self.driver_combo, 0, 1)

        self.candidate_combo = QComboBox()
        self.candidate_combo.setToolTip(
            "Where a driver thinks a mount might be. Being listed is a hint, not "
            "a promise — a USB serial adapter is a reason to probe, not evidence.")
        connect_grid.addWidget(QLabel("Address"), 1, 0)
        connect_grid.addWidget(self.candidate_combo, 1, 1)

        self.scan_button = QPushButton("Scan")
        self.scan_button.clicked.connect(self.scan)
        connect_grid.addWidget(self.scan_button, 1, 2)

        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.toggle_connection)
        connect_grid.addWidget(self.connect_button, 0, 2)

        layout.addWidget(connect_box)

        # -------------------------------------------------------------- status
        self.status_label = QLabel("not connected")
        font = self.status_label.font()
        font.setBold(True)
        self.status_label.setFont(font)
        layout.addWidget(self.status_label)

        self.where_label = QLabel("")
        self.where_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.where_label)

        # -------------------------------------------------------------- actions
        self.action_box = QGroupBox("Sun")
        action_grid = QGridLayout(self.action_box)
        self.goto_sun_button = QPushButton("Goto Sun")
        self.goto_sun_button.clicked.connect(self.goto_sun)
        action_grid.addWidget(self.goto_sun_button, 0, 0)

        self.track_button = QPushButton("Track")
        self.track_button.setCheckable(True)
        self.track_button.clicked.connect(self.toggle_tracking)
        action_grid.addWidget(self.track_button, 0, 1)

        # The one control that matters when something is going wrong: fixed size,
        # fixed place, live whenever there is a mount to stop.
        self.stop_button = QPushButton("STOP")
        self.stop_button.setMinimumHeight(44)
        self.stop_button.clicked.connect(self.stop)
        action_grid.addWidget(self.stop_button, 1, 0, 1, 2)

        self.park_button = QPushButton("Park")
        self.park_button.clicked.connect(self.park)
        action_grid.addWidget(self.park_button, 2, 0)

        self.unpark_button = QPushButton("Unpark")
        self.unpark_button.clicked.connect(self.unpark)
        action_grid.addWidget(self.unpark_button, 2, 1)

        layout.addWidget(self.action_box)

        # ---------------------------------------------------------- manual move
        self.move_box = QGroupBox("Nudge")
        move_grid = QGridLayout(self.move_box)
        self.move_buttons = {}
        for direction, row, column in (("north", 0, 1), ("west", 1, 0),
                                       ("east", 1, 2), ("south", 2, 1)):
            button = QPushButton(direction[0].upper())
            # Held, not clicked: the mount moves while the button is down, which is
            # what framing by eye needs.
            button.pressed.connect(lambda d=direction: self.move(d))
            button.released.connect(lambda d=direction: self.stop_move(d))
            move_grid.addWidget(button, row, column)
            self.move_buttons[direction] = button

        self.rate_combo = QComboBox()
        self.rate_combo.currentTextChanged.connect(self.set_rate)
        move_grid.addWidget(self.rate_combo, 1, 1)

        layout.addWidget(self.move_box)
        layout.addStretch(1)

        # Three stacked group boxes want 362x474 between them, and a dock that
        # cannot be smaller than its contents forces the window wider than the
        # screen the moment it opens.  Scrolling lets it be any size; the
        # controls are reached by scrolling rather than by resizing the display.
        scroller = QScrollArea()
        scroller.setWidget(body)
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidget(scroller)
        self.connected.connect(self._on_connected)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.setInterval(self.POLL_MS)

        self._apply_capabilities()
        self._set_controls_enabled(False)

    # -------------------------------------------------------------- connection

    def scan(self) -> None:
        """Ask the drivers where mounts might be, without connecting to any."""
        self.candidate_combo.clear()
        wanted = self.driver_combo.currentData()
        try:
            candidates = discover_mounts(wanted)
        except MountError as exc:
            self.status_label.setText(f"scan failed: {exc}")
            return
        for candidate in candidates:
            self.candidate_combo.addItem(str(candidate), candidate)
        if not candidates:
            self.candidate_combo.addItem("nothing found", None)
        self.status_label.setText(f"{len(candidates)} candidate(s)")

    def toggle_connection(self) -> None:
        if self.mount is not None:
            self.disconnect_mount()
        else:
            self.connect_mount()

    def connect_mount(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.connect_button.setEnabled(False)
        self.status_label.setText("connecting...")

        driver = self.driver_combo.currentData()
        candidate = self.candidate_combo.currentData()
        config = dict(candidate.config) if candidate is not None else {}
        if candidate is not None:
            driver = candidate.driver

        def worker():
            try:
                mount = connect_mount(driver, **config)
            except Exception as exc:
                self.connected.emit(None, str(exc))
                return
            self.connected.emit(mount, "")

        threading.Thread(target=worker, daemon=True).start()

    def _on_connected(self, mount, error: str) -> None:
        self._busy = False
        self.connect_button.setEnabled(True)
        if mount is None:
            self.status_label.setText("not connected")
            logging.warning('Mount connection failed: %s', error)
            QMessageBox.warning(self, "Mount", f"Could not connect:\n\n{error}")
            return

        self.mount = mount
        # Scheduled mount_* commands look the device up here.
        register_hardware('mount', mount)
        self.connect_button.setText("Disconnect")
        self._apply_capabilities()
        self._set_controls_enabled(True)
        logging.info('Mount connected: %s', mount.describe())
        # Set here rather than left to the poll: a hidden dock does not poll.
        self.status_label.setText(mount.describe())
        self.refresh()
        if self.isVisible():
            self._timer.start()

    def disconnect_mount(self) -> None:
        self._timer.stop()
        mount, self.mount = self.mount, None
        register_hardware('mount', None)
        if mount is not None:
            try:
                mount.close()
            except Exception:
                logging.exception('Closing the mount failed')
        self.connect_button.setText("Connect")
        self.status_label.setText("not connected")
        self.where_label.setText("")
        self._set_controls_enabled(False)

    # ------------------------------------------------------------------ status

    def refresh(self) -> None:
        """Poll the mount, unless nobody is looking at the answer.

        Every poll is a round trip down the serial line the eclipse script also
        uses, so a hidden dock stops asking.
        """
        if self.mount is None or not self.isVisible():
            return
        try:
            status = self.mount.status()
        except MountError as exc:
            self.status_label.setText(f"unreadable: {exc}")
            return

        self.status_label.setText(status.summary())
        self.track_button.setChecked(status.tracking)

        where = []
        try:
            ra_hours, dec_degrees = self.mount.get_radec()
            where.append(f"RA {format_ra(ra_hours)}  Dec {format_dec(dec_degrees)}")
        except MountError:
            pass
        if self.mount.capabilities.altaz_readout:
            try:
                altitude, azimuth = self.mount.get_altaz()
                where.append(f"Alt {altitude:.2f}°  Az {azimuth:.2f}°")
            except MountError:
                pass
        self.where_label.setText("\n".join(where))

    # ----------------------------------------------------------------- actions

    #: What a serial write says when the device itself has left the bus.
    _VANISHED = ("device not configured", "errno 6", "device disconnected",
                 "no such file or directory")

    def _guard(self, what: str, action) -> None:
        """Run a mount command, turning a refusal into a message not a crash."""
        if self.mount is None:
            return
        try:
            action()
        except MountNotSupported as exc:
            self.status_label.setText(str(exc))
        except MountError as exc:
            text = str(exc).lower()
            if any(marker in text for marker in self._VANISHED):
                # The controller has dropped off the USB bus - on 5 August it
                # did so two seconds into a slew, which is the signature of the
                # motors' current spike browning out the logic.  Retrying
                # writes into a dead port just repeats the error every poll;
                # disconnect cleanly, say why once, and leave the Connect
                # button as the way back.
                logging.warning("Mount %s failed because the controller left "
                                "the USB bus - check the mount's power supply, "
                                "then reconnect", what)
                self.status_label.setText(
                    "The mount vanished from USB - check its power, then Connect")
                self.disconnect_mount()
                return
            logging.warning('Mount %s failed: %s', what, exc)
            QMessageBox.warning(self, "Mount", f"{what} failed:\n\n{exc}")
        self.refresh()

    def goto_sun(self) -> None:
        self._guard("Goto Sun", lambda: self.mount.goto_sun(wait=False))

    def toggle_tracking(self) -> None:
        wanted = self.track_button.isChecked()
        self._guard("Tracking",
                    (lambda: mount_track_sun(self.mount)) if wanted
                    else self.mount.tracking_off)

    def stop(self) -> None:
        self._guard("Stop", self.mount.abort)

    def park(self) -> None:
        self._guard("Park", self.mount.park)

    def unpark(self) -> None:
        self._guard("Unpark", self.mount.unpark)

    def move(self, direction: str) -> None:
        self._guard("Move", lambda: self.mount.move(direction))

    def stop_move(self, direction: str) -> None:
        self._guard("Stop move", lambda: self.mount.stop_move(direction))

    def set_rate(self, rate: str) -> None:
        if rate:
            self._guard("Rate", lambda: self.mount.set_rate(rate))

    # ------------------------------------------------------------ capabilities

    def _apply_capabilities(self) -> None:
        """Hide what this mount cannot do rather than offering and refusing it."""
        capabilities = self.mount.capabilities if self.mount else None
        self.action_box.setVisible(True)
        self.goto_sun_button.setVisible(capabilities is None or capabilities.goto)
        self.track_button.setVisible(capabilities is None or capabilities.tracking_toggle)
        self.park_button.setVisible(capabilities is None or capabilities.park)
        self.unpark_button.setVisible(capabilities is None or capabilities.park)
        self.move_box.setVisible(capabilities is None or capabilities.manual_move)

        self.rate_combo.blockSignals(True)
        self.rate_combo.clear()
        if capabilities is not None:
            self.rate_combo.addItems(list(capabilities.rate_presets))
        self.rate_combo.blockSignals(False)

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in (self.goto_sun_button, self.track_button, self.stop_button,
                       self.park_button, self.unpark_button, self.rate_combo,
                       *self.move_buttons.values()):
            widget.setEnabled(enabled)

    # -------------------------------------------------------------- visibility

    def showEvent(self, event):
        super().showEvent(event)
        if self.mount is not None:
            self.refresh()
            self._timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()


class RelayPopup(QWidget, Observable):

    def __init__(self, observer: 'SolarEclipseController'):
        """ Panel to connect, configure, and test the USB relay shutter trigger.

        The connected trigger is registered with the hardware registry, so
        relay_shoot / relay_burst / relay_bulb commands in an eclipse script
        find it when they fire.

        Args:
            - observer: SolarEclipseController that needs to be notified when the
                        relay is connected or disconnected
        """

        QWidget.__init__(self)
        self.setWindowTitle("Relay shutter trigger")
        self.setGeometry(QRect(100, 100, 420, 220))
        self.add_observer(observer)
        self.controller = observer

        settings = QSettings(str(SETTINGS_PATH), QSettings.Format.IniFormat)

        layout = QVBoxLayout()

        # Connection

        connection_group_box = QGroupBox("Connection")
        connection_layout = QGridLayout()

        connection_layout.addWidget(QLabel("Backend"), 0, 0)
        self.backend_combobox = QComboBox()
        self.backend_combobox.addItem("auto")
        for backend in list_backends():
            self.backend_combobox.addItem(backend.name)
            self.backend_combobox.setItemData(self.backend_combobox.count() - 1,
                                              backend.description, Qt.ItemDataRole.ToolTipRole)
        self.backend_combobox.setCurrentText(settings.value("relay/backend", "auto", type=str))
        connection_layout.addWidget(self.backend_combobox, 0, 1)

        connection_layout.addWidget(QLabel("Port"), 1, 0)
        self.port_combobox = QComboBox()
        self.port_combobox.setEditable(True)
        connection_layout.addWidget(self.port_combobox, 1, 1)
        scan_button = QPushButton("Scan")
        scan_button.clicked.connect(self.scan_ports)
        connection_layout.addWidget(scan_button, 1, 2)

        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.connect_or_disconnect)
        connection_layout.addWidget(self.connect_button, 0, 2)

        connection_group_box.setLayout(connection_layout)
        layout.addWidget(connection_group_box)

        # Wiring

        wiring_group_box = QGroupBox("Wiring")
        wiring_layout = QGridLayout()

        wiring_layout.addWidget(QLabel("Shutter (S2) channel"), 0, 0)
        self.s2_channel = QLineEdit(settings.value("relay/s2_channel", "2", type=str))
        self.s2_channel.setValidator(QIntValidator(1, 64))
        wiring_layout.addWidget(self.s2_channel, 0, 1)

        self.s1_checkbox = QCheckBox("Half-press (S1) on its own channel")
        self.s1_checkbox.setChecked(settings.value("relay/s1_enabled", True, type=bool))
        wiring_layout.addWidget(self.s1_checkbox, 1, 0)
        self.s1_channel = QLineEdit(settings.value("relay/s1_channel", "1", type=str))
        self.s1_channel.setValidator(QIntValidator(1, 64))
        wiring_layout.addWidget(self.s1_channel, 1, 1)

        wiring_group_box.setLayout(wiring_layout)
        layout.addWidget(wiring_group_box)

        # Test & status

        test_layout = QHBoxLayout()
        self.test_button = QPushButton("Test fire")
        self.test_button.clicked.connect(self.test_fire)
        test_layout.addWidget(self.test_button)
        self.release_button = QPushButton("Release contacts")
        self.release_button.clicked.connect(self.release_contacts)
        test_layout.addWidget(self.release_button)
        layout.addLayout(test_layout)

        self.status_label = QLabel()
        layout.addWidget(self.status_label)

        self.setLayout(layout)

        self.scan_ports()
        saved_port = settings.value("relay/port", "", type=str)
        if saved_port:
            self.port_combobox.setCurrentText(saved_port)
        self.show_state()

    def scan_ports(self):
        """ Repopulate the port list from relay discovery, keeping any typed text. """

        typed = self.port_combobox.currentText()
        self.port_combobox.clear()
        for candidate in discover_relays():
            self.port_combobox.addItem(candidate.target)
            self.port_combobox.setItemData(self.port_combobox.count() - 1,
                                           candidate.description, Qt.ItemDataRole.ToolTipRole)
        if typed:
            self.port_combobox.setCurrentText(typed)

    def show_state(self):
        """ Reflect the connection state in the buttons and status label. """

        trigger = self.controller.relay_trigger
        connected = trigger is not None
        self.connect_button.setText("Disconnect" if connected else "Connect")
        self.test_button.setEnabled(connected)
        self.release_button.setEnabled(connected)
        self.status_label.setText(trigger.describe() if connected else "Not connected")

    def connect_or_disconnect(self):
        """ Open the configured relay and register it, or close the open one. """

        if self.controller.relay_trigger is not None:
            self.controller.relay_trigger.close()
            self.controller.relay_trigger = None
            register_hardware('relay', None)
            self.show_state()
            self.notify_observers(self)
            return

        kind = self.backend_combobox.currentText()
        port = self.port_combobox.currentText().strip() or None
        s2 = int(self.s2_channel.text() or "2")
        s1 = int(self.s1_channel.text() or "1") if self.s1_checkbox.isChecked() else None

        try:
            backend = make_backend(kind, port)
            trigger = RelayTrigger(backend, Wiring(s2_channel=s2, s1_channel=s1))
        except RelayError as exc:
            QMessageBox.warning(self, "Relay", str(exc))
            return

        self.controller.relay_trigger = trigger
        register_hardware('relay', trigger)

        settings = QSettings(str(SETTINGS_PATH), QSettings.Format.IniFormat)
        settings.setValue("relay/backend", kind)
        settings.setValue("relay/port", port or "")
        settings.setValue("relay/s2_channel", str(s2))
        settings.setValue("relay/s1_enabled", self.s1_checkbox.isChecked())
        settings.setValue("relay/s1_channel", self.s1_channel.text() or "1")

        self.show_state()
        self.notify_observers(self)

    def test_fire(self):
        """ One frame through the relay — bench check, lens cap on. """

        try:
            self.controller.relay_trigger.shoot()
        except RelayError as exc:
            QMessageBox.warning(self, "Relay", str(exc))

    def release_contacts(self):
        """ Force every contact open — the panic button. """

        self.controller.relay_trigger.release_all()


class SettingsPopup(QWidget, Observable):

    def __init__(self, observer: SolarEclipseController):
        """ A pop-up window is shown, in which the user can choose the settings.

        When pressing the "OK" button, the given controller will be notified about this.

        If the setting had already been set before, this will be shown in the comboboxes.

        Args:
            - observer: SolarEclipseController that needs to be notified about the settings.
        """

        QWidget.__init__(self)
        self.setWindowTitle("Datetime format")
        self.setGeometry(QRect(100, 100, 300, 75))
        self.add_observer(observer)

        layout = QGridLayout()
        layout.addWidget(QLabel("Date format"), 0, 0)
        self.date_combobox = QComboBox()
        self.date_combobox.addItems(DATE_FORMATS.keys())
        layout.addWidget(self.date_combobox, 0, 1)
        layout.addWidget(QLabel("Time format"), 1, 0)
        self.time_combobox = QComboBox()
        self.time_combobox.addItems(TIME_FORMATS.keys())
        layout.addWidget(self.time_combobox, 1, 1)

        self.date_combobox.setCurrentText(observer.view.date_format)
        self.time_combobox.setCurrentText(observer.view.time_format)

        ok_button = QPushButton("OK")
        ok_button.clicked.connect(self.accept_settings)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.cancel_settings)
        layout.addWidget(ok_button, 2, 0)
        layout.addWidget(cancel_button, 2, 1)

        self.setLayout(layout)

    def accept_settings(self):
        """ Notify the observer about the settings changes and close the pop-up window."""

        self.notify_observers(self)
        self.close()

    def cancel_settings(self):
        """ Close the pop-up window without accepting any settings changes."""

        self.close()


class LocationPlot(FigureCanvas):
    """ Display the world with the selected location marked with a red dot."""

    def __init__(self, parent=None, dpi=100):
        """ Plot a world map."""

        self.figure = Figure(dpi=dpi)
        self.ax = self.figure.add_subplot(111, aspect='equal')

        FigureCanvas.__init__(self, self.figure)
        self.setParent(parent)

        FigureCanvas.updateGeometry(self)

        self.location_is_drawn = False
        self.location = None
        self.gdf = None

        # noinspection SpellCheckingInspection
        world = geopandas.read_file(get_path("naturalearth.land"))
        # Crop -> min longitude, min latitude, max longitude, max latitude
        world.clip([-180, -90, 180, 90]).plot(color="white", edgecolor="black", ax=self.ax)

        self.ax.set_aspect("equal")

        self.draw()

    def plot_location(self, longitude: float, latitude: float):
        """ Indicate the given location on the world map with a red dot.

        Args:
            - longitude: Longitude of the location [degrees]
            - latitude: Latitude of the location [degrees]
        """

        if self.location_is_drawn:
            self.gdf.plot(ax=self.ax, color="white")

        df = pd.DataFrame(
            {
                "Latitude": [latitude],
                "Longitude": [longitude],
            }
        )
        self.gdf = geopandas.GeoDataFrame(df, geometry=geopandas.points_from_xy(df.Longitude, df.Latitude),
                                          crs="EPSG:4326")
        self.gdf.plot(ax=self.ax, color="red")

        self.ax.set_aspect("equal")

        self.draw()
        self.location_is_drawn = True



@dataclass(frozen=True)
class Observer:
    latitude: float
    longitude: float
    altitude: float


class EclipsePlotWidget(QtWidgets.QWidget):
    """
    PyQt6 QWidget that plots Sun & Moon discs to scale for a given observer and datetime.

    - Axes are in units of *solar radii* with the Sun drawn as a unit circle centered at (0, 0).
    - By default: North is up, East is right (astronomical convention).
      Set east_left=True to mirror the X axis (East to the left) like many star charts.
    - Call .plot(datetime_obj) to render a frame.
    """

    # Cache ephemerides + timescales across instances to avoid repeated downloads

    CACHED_EPHEMERIDES = None
    CACHED_TIMESCALE = None

    def __init__(
        self,

        east_left: bool = False,
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        super().__init__(parent)

        self.offset = datetime.timedelta(minutes=0)

        # Defer loading of Skyfield ephemerides until the plot is actually used.
        # Loading can take time and may download large files; avoid doing that
        # during UI construction so the main window can appear immediately.
        self.sun_ephemeris = None
        self.moon_ephemeris = None

        # --- Matplotlib figure canvas inside this QWidget ---
        self.fig = Figure(figsize=(6.0, 6.2), dpi=100)
        self.canvas = FigureCanvas(self.fig)
        self.ax = self.fig.add_subplot(111)  # single axes

        # UI layout
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.canvas)

        # Prepare static plot styling (labels, grid, aspect)
        self._init_axes()

        # self.longitude = None
        # self.latitude = None
        # self.altitude = None

        self.observer = None
        self.is_location_set = False

    def set_location(self, longitude, latitude, altitude):

        # self.longitude = longitude
        # self.latitude = latitude
        # self.altitude = altitude
        self.observer = Observer(latitude=latitude, longitude=longitude, altitude=altitude)

        self.is_location_set = True

    def _ensure_ephemerides_loaded(self):
        """Ensure shared Skyfield ephemerides and timescale are loaded.

        This is intentionally called lazily from `plot()` so the GUI can
        appear before any potential downloads start.
        """
        if EclipsePlotWidget.CACHED_TIMESCALE is not None and EclipsePlotWidget.CACHED_EPHEMERIDES is not None:
            # Already loaded
            self.sun_ephemeris = EclipsePlotWidget.CACHED_EPHEMERIDES["sun"]
            self.moon_ephemeris = EclipsePlotWidget.CACHED_EPHEMERIDES["moon"]
            return

        # Show a compact modal dialog while we load ephemerides in background.
        parent = self.window() if self.window() is not None else self
        dialog = QDialog(parent)
        dialog.setWindowTitle("Loading helper files...")
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg_layout = QVBoxLayout(dialog)
        progress_bar = QProgressBar()
        progress_bar.setRange(0, 0)
        dlg_layout.addWidget(progress_bar)
        label = QLabel("Loading helper files...")
        dlg_layout.addWidget(label)
        dialog.setLayout(dlg_layout)

        result = {}

        class _DevNull:
            def write(self, s):
                return

            def flush(self):
                return

        def worker():
            old_out = sys.stdout
            old_err = sys.stderr
            sys.stdout = _DevNull()
            sys.stderr = _DevNull()
            import logging
            root_logger = logging.getLogger()
            old_handlers = list(root_logger.handlers)
            old_level = root_logger.level
            try:
                # Temporarily remove handlers to avoid terminal noise
                root_logger.handlers = []
                if EclipsePlotWidget.CACHED_TIMESCALE is None:
                    EclipsePlotWidget.CACHED_TIMESCALE = load.timescale()
                if EclipsePlotWidget.CACHED_EPHEMERIDES is None:
                    EclipsePlotWidget.CACHED_EPHEMERIDES = load("de440s.bsp")
                result['ok'] = True
            except Exception:
                import traceback

                result['error'] = traceback.format_exc()
            finally:
                sys.stdout = old_out
                sys.stderr = old_err
                root_logger.handlers = old_handlers
                root_logger.setLevel(old_level)

        th = threading.Thread(target=worker, daemon=True)

        try:
            dialog.show()
            QApplication.processEvents()
        except Exception:
            pass

        th.start()

        timer = QTimer(dialog)

        def poll():
            if not th.is_alive():
                timer.stop()
                dialog.accept()
                if 'error' in result:
                    raise Exception(result['error'])
                # Populate instance references
                self.sun_ephemeris = EclipsePlotWidget.CACHED_EPHEMERIDES["sun"]
                self.moon_ephemeris = EclipsePlotWidget.CACHED_EPHEMERIDES["moon"]

        timer.timeout.connect(poll)
        timer.start(100)

        dialog.exec()

    # ------------- Public API -------------

    def set_offset(self, offset):
        self.offset = offset

    def plot(self, when: datetime) -> None:
        """
        Render eclipse geometry for the current observer at a given datetime.

        Parameters
        ----------
        when : datetime
            Prefer a timezone-aware datetime (UTC). If naive, it's interpreted as UTC.
        """

        when += self.offset

        # Ensure ephemerides are loaded lazily to avoid blocking UI startup.
        self._ensure_ephemerides_loaded()

        if not self.is_location_set:
            LOGGER.info("Location not set. Please use set_location() first.")
            return

        # Interpret naive datetimes as UTC for robustness
        if when.tzinfo is None:
            when = when.replace(tzinfo=pytz.UTC)

        t = self.CACHED_TIMESCALE.from_datetime(when)



        # Build topocentric observer by attaching a Topos to the Earth body
        # (compose `earth_ephemeris + wgs84.latlon(...)`). The older direct
        # Topos.at(t) path is unreliable across Skyfield versions, so use the
        # earth+Topos site object which consistently supports `.at(t)`.
        earth_ephemeris = self.CACHED_EPHEMERIDES["earth"]
        site = earth_ephemeris + wgs84.latlon(
            self.observer.latitude,
            self.observer.longitude,
            elevation_m=self.observer.altitude,
        )

        sun_app = site.at(t).observe(self.sun_ephemeris).apparent()
        moon_app = site.at(t).observe(self.moon_ephemeris).apparent()

        # Compute topocentric unit direction vectors using local horizontal
        # (alt/az) coordinates and form ENU unit vectors. Then project the
        # difference between Moon and Sun directions onto the tangent plane
        # (East, North) at the observer. This yields small-angle offsets in
        # radians which we scale to solar radii for plotting.

        # Use 3D topocentric unit vectors and project the Moon into the
        # tangent plane perpendicular to the Sun direction. This gives a
        # robust small-angle offset that matches angular separation.

        # Compute small-angle horizontal offsets using alt/az differences so
        # the plotted geometry is in local horizontal coordinates (matching
        # Stellarium). For small separations:
        #   x_east ≈ Δaz * cos(mean_alt)   (radians)
        #   y_north ≈ Δalt                  (radians)
        sun_alt, sun_az, _ = sun_app.altaz()
        moon_alt, moon_az, _ = moon_app.altaz()

        a_s = sun_alt.radians
        A_s = sun_az.radians
        a_m = moon_alt.radians
        A_m = moon_az.radians

        # Wrap Δaz into [-pi, +pi]
        delta_az = (A_m - A_s + math.pi) % (2.0 * math.pi) - math.pi
        mean_alt = 0.5 * (a_s + a_m)

        x_east = float(delta_az * math.cos(mean_alt))
        y_north = float(a_m - a_s)

        # Position Angle (PA): from North through East
        pa_deg = (math.degrees(math.atan2(x_east, y_north)) + 360.0) % 360.0

        # Apparent angular radii (radians)
        sun_distance = sun_app.distance().m     # Distance Earth - Sun [m]
        moon_dist = moon_app.distance().m     # Distance Earth - Moon [m]
        sun_ang_radius = math.asin(SUN_RADIUS / sun_distance)   # Angular radius of the Sun [radians]
        moon_ang_radius = math.asin(MOON_RADIUS / moon_dist)    # Angular radius of the Moon [radians]

        # Normalise to solar radius for plotting
        scale = 1.0 / sun_ang_radius
        xm = x_east * scale
        ym = y_north * scale
        r_moon_scaled = moon_ang_radius * scale

        

        # ---- Draw ----
        self.ax.clear()
        self._init_axes()  # re-apply static styling

        # Sun disk (unit radius)
        sun_disk = self._mpl_circle((0.0, 0.0), 1.0, facecolor="#FDB813", edgecolor="k", alpha=0.85, lw=1.0)
        self.ax.add_patch(sun_disk)

        # Moon disk
        moon_disk = self._mpl_circle((xm, ym), r_moon_scaled, facecolor="k", edgecolor="k", alpha=0.92, lw=1.0)
        self.ax.add_patch(moon_disk)

        # # Cardinal labels on Sun’s rim
        # self._cardinal_marks()

        # # Annotation box
        # txt = (
        #     f"Separation: {math.degrees(sep_rad):.3f}°\n"
            # f"Position angle (Moon from Sun): {pa_deg:.1f}°  (0°=N, 90°=E)\n"
        #     f"R_sun: {math.degrees(R_sun)*60:.2f}′   R_moon: {math.degrees(R_moon)*60:.2f}′"
        # )
        # self.ax.text(
        #     0.02,
        #     0.98,
        #     txt,
        #     transform=self.ax.transAxes,
        #     ha="left",
        #     va="top",
        #     fontsize=9,
        #     bbox=dict(facecolor="white", alpha=0.85, edgecolor="none"),
        # )

        # Title

        title_loc = f"({self.observer.latitude:.4f}°, {self.observer.longitude:.4f}°, {self.observer.altitude:.0f} m)"
        # ISO string in UTC for clarity
        self.ax.set_title(
            f"Solar eclipse geometry",
            fontsize=11,
        )

        # Limits

        # margin = 1.25 * max(1.0, abs(xm) + r_moon_scaled, abs(ym) + r_moon_scaled)
        margin = 1.5
        self.ax.set_xlim(-margin, margin)
        self.ax.set_ylim(-margin, margin)
        
        # Redraw canvas

        self.canvas.draw_idle()

    # ------------- Internals -------------

    def _init_axes(self) -> None:
        """Initialize axes labels, grids, aspect, and crosshair."""
        self.ax.set_aspect("equal", adjustable="box")
        # self.ax.set_xlabel("East (in solar radii)")
        # self.ax.set_ylabel("North (in solar radii)")
        # self.ax.grid(False, alpha=0.25, lw=0.6)

        # Centre crosshair
        # self.ax.axhline(0, color="lightgray", lw=0.6)
        # self.ax.axvline(0, color="lightgray", lw=0.6)

    def _mpl_circle(self, center, radius, **kwargs):
        import matplotlib.patches as mpatches

        return mpatches.Circle(center, radius, **kwargs)

    # def _cardinal_marks(self) -> None:
    #     offs = 1.08
    #     for label, (x, y) in [
    #         ("N", (0, +offs)),
    #         ("E", (+offs if not self.east_left else -offs, 0)),
    #         ("S", (0, -1.12)),
    #         ("W", (-1.12 if not self.east_left else +1.12, 0)),
    #     ]:
    #         self.ax.text(x, y, label, ha="center", va="center", fontsize=10, color="dimgray")


# Minimum per-pixel gradient (in 8-bit levels) for an edge to count as "in focus" for
# peaking.  Set by how a defocused edge behaves: the same brightness step smeared over more
# pixels yields a proportionally smaller gradient, so this is effectively "the edge must
# rise by at least this much per pixel".
_PEAKING_MIN_GRADIENT = 30.0


def qimage_to_gray_array(image: QImage) -> np.ndarray:
    """Return *image* as a 2-D uint8 luminance array.

    Each scan line in a QImage is padded to a 4-byte boundary, so the raw buffer is wider
    than the image; the padding columns are trimmed before returning.
    """
    grayscale = image.convertToFormat(QImage.Format.Format_Grayscale8)
    width, height = grayscale.width(), grayscale.height()
    bits = grayscale.constBits()
    bits.setsize(grayscale.sizeInBytes())
    return np.frombuffer(bits, np.uint8).reshape(height, grayscale.bytesPerLine())[:, :width]


def focus_score(gray: np.ndarray) -> float:
    """Return a sharpness score for *gray*: the variance of its Laplacian.

    Higher is sharper.  The absolute value is meaningless on its own — it depends on the
    subject, the exposure and the zoom level — so the UI shows it against the best value
    seen since the last reset, which is what makes it usable for finding best focus.
    """
    if gray.size == 0 or gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    values = gray.astype(np.float32)
    laplacian = (
        4.0 * values[1:-1, 1:-1]
        - values[:-2, 1:-1] - values[2:, 1:-1]
        - values[1:-1, :-2] - values[1:-1, 2:]
    )
    return float(laplacian.var())


def focus_peaking_overlay(gray: np.ndarray, fraction: float = 0.25) -> Union[QImage, None]:
    """Build a transparent overlay highlighting the sharpest edges in *gray*.

    Edges are found with a central-difference gradient, and everything within *fraction* of
    the strongest edge in the frame is marked.  The threshold is relative to the frame's own
    peak rather than a percentile of all pixels: the subject here is typically a solar disc
    on empty sky, whose limb occupies well under 2% of the frame, so any percentile-based
    threshold lands in the blank sky and highlights nothing.

    What to look for while focusing: at best focus the limb is outlined by a thin, tight
    line.  As focus is lost the same brightness step is smeared over more pixels, so the
    outline first broadens into a band and then disappears entirely once the edge is softer
    than the floor below.  The marked area is therefore not a monotonic measure of focus —
    it is a visual aid, and :func:`focus_score` is the precise instrument.

    Returns None when there is nothing worth drawing.
    """
    if gray.size == 0 or gray.shape[0] < 3 or gray.shape[1] < 3:
        return None

    values = gray.astype(np.float32)
    gradient_x = np.zeros_like(values)
    gradient_y = np.zeros_like(values)
    gradient_x[:, 1:-1] = values[:, 2:] - values[:, :-2]
    gradient_y[1:-1, :] = values[2:, :] - values[:-2, :]
    magnitude = np.hypot(gradient_x, gradient_y)

    # Marking has to be earned by genuine local contrast rather than by merely being the
    # strongest thing present: a purely relative threshold lights up a badly defocused frame
    # most of all, and marks sensor noise in a blank one.  The floor prevents both, since a
    # sufficiently defocused edge has a low gradient per pixel however strong the step is.
    peak = float(magnitude.max())
    threshold = max(_PEAKING_MIN_GRADIENT, fraction * peak)
    if peak < threshold:
        return None

    height, width = gray.shape
    # Qt's ARGB32 is byte-order BGRA on little-endian machines, which is what all supported
    # platforms use; the alpha channel is what keeps the un-marked pixels transparent.
    overlay = np.zeros((height, width, 4), np.uint8)
    overlay[magnitude >= threshold] = (0, 0, 255, 255)
    image = QImage(overlay.data, width, height, width * 4, QImage.Format.Format_ARGB32)
    # Copy so the QImage owns its pixels rather than referencing the local array.
    return image.copy()


class LiveViewWindow(QWidget):
    """Floating window that shows a live-view preview from a gphoto2 camera.

    A background ``LiveViewThread`` grabs one preview frame per second.  When
    the camera USB lock is held by a scheduled shot the frame is silently
    skipped so timing accuracy is never compromised.

    The window can be:
      - Disabled/re-enabled at any time via the toggle button.
      - Auto-paused between C2 and C3 (totality) by the controller calling
        ``set_totality_paused(True/False)``.
    """

    def __init__(self, camera, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setMinimumSize(480, 400)

        self._camera = camera
        self._thread = None
        self._frame_queue: queue.Queue = queue.Queue(maxsize=1)
        self._user_enabled: bool = True
        self._totality_paused: bool = False

        self.setWindowTitle(f"Live View — {camera.name}")

        # Image display
        self._image_label = QLabel("Waiting for first preview frame…")
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setMinimumSize(320, 240)

        # Timestamp of last received frame
        self._timestamp_label = QLabel()
        self._timestamp_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._timestamp_label.setStyleSheet("color: gray; font-size: 11px;")

        # Status line
        self._status_label = QLabel()
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Buttons and zoom control
        self._toggle_btn = QPushButton("Disable Live View")
        self._toggle_btn.clicked.connect(self._on_toggle)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)

        # Zoom control: Fit / 5x / 10x
        self._zoom_combo = QComboBox()
        self._zoom_combo.addItems(["Fit", "5x", "10x"])
        self._zoom_combo.setCurrentIndex(0)
        self._zoom_factor = 1.0
        self._zoom_combo.currentIndexChanged.connect(self._on_zoom_changed)

        # Pan controls (hidden until a zoom > 1 is selected)
        self._last_pixmap: Union[QPixmap, None] = None
        self._last_ts: Union[datetime.datetime, None] = None
        self._h_slider = QSlider(Qt.Orientation.Horizontal)
        self._h_slider.setRange(0, 0)
        self._h_slider.setVisible(False)
        self._h_slider.valueChanged.connect(self._on_pan_changed)

        self._v_slider = QSlider(Qt.Orientation.Vertical)
        self._v_slider.setRange(0, 0)
        self._v_slider.setVisible(False)
        self._v_slider.setFixedWidth(18)
        self._v_slider.valueChanged.connect(self._on_pan_changed)

        # Focusing aids
        self._peaking_check = QCheckBox("Peaking")
        self._peaking_check.setToolTip(
            "Highlight the sharpest edges in red — the solar limb lights up when in focus."
        )
        self._peaking_check.toggled.connect(self._on_overlay_changed)

        self._crosshair_check = QCheckBox("Crosshair")
        self._crosshair_check.setChecked(True)
        self._crosshair_check.setToolTip("Show the centring crosshair.")
        self._crosshair_check.toggled.connect(self._on_overlay_changed)

        # Sharpness readout.  The best value seen so far is what makes this usable: an
        # absolute focus number means nothing, but "am I above or below my best?" does.
        self._focus_score: float = 0.0
        self._best_focus_score: float = 0.0
        self._focus_label = QLabel("Focus: –")
        self._focus_label.setToolTip(
            "Sharpness of the visible area, against the best seen since the last reset.\n"
            "Turn the focus ring to maximise it; zoom in first for a finer reading."
        )
        self._focus_reset_btn = QPushButton("Reset")
        self._focus_reset_btn.setToolTip("Forget the best sharpness seen so far.")
        self._focus_reset_btn.clicked.connect(self._on_focus_reset)

        btn_layout = QHBoxLayout()
        btn_layout.addWidget(self._toggle_btn)
        btn_layout.addWidget(close_btn)
        btn_layout.addStretch(1)
        btn_layout.addWidget(self._focus_label)
        btn_layout.addWidget(self._focus_reset_btn)
        btn_layout.addWidget(self._peaking_check)
        btn_layout.addWidget(self._crosshair_check)
        btn_layout.addWidget(QLabel("Zoom:"))
        btn_layout.addWidget(self._zoom_combo)

        # Image area with optional vertical pan slider
        layout = QVBoxLayout()
        image_area = QHBoxLayout()
        image_area.addWidget(self._image_label, stretch=1)
        image_area.addWidget(self._v_slider)
        layout.addLayout(image_area)
        layout.addWidget(self._h_slider)
        layout.addWidget(self._timestamp_label)
        layout.addWidget(self._status_label)
        layout.addLayout(btn_layout)
        self.setLayout(layout)

        # Poll the frame queue every 500 ms on the GUI thread
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(500)
        self._poll_timer.timeout.connect(self._poll_frame)
        self._poll_timer.start()

        self._start_thread()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _start_thread(self):
        if self._thread is not None:
            self._thread.stop()
        self._thread = LiveViewThread(
            camera=self._camera,
            frame_callback=self._on_frame,
            interval_s=1.0,
        )
        self._thread.start()
        self._update_status_label()

    def _on_frame(self, jpeg_bytes: bytes):
        """Called from the LiveViewThread; enqueue (frame, timestamp) for GUI thread."""
        try:
            self._frame_queue.put_nowait((jpeg_bytes, datetime.datetime.now()))
        except queue.Full:
            pass  # drop the frame; the previous one hasn't been displayed yet

    def _poll_frame(self):
        """Called on the Qt main thread by the poll timer; updates the image."""
        try:
            jpeg_bytes, ts = self._frame_queue.get_nowait()
        except queue.Empty:
            return
        if isinstance(jpeg_bytes, memoryview):
            jpeg_bytes = jpeg_bytes.tobytes()
        elif isinstance(jpeg_bytes, bytearray):
            jpeg_bytes = bytes(jpeg_bytes)
        elif not isinstance(jpeg_bytes, bytes):
            LOGGER.warning("Live view frame had unsupported type: %s", type(jpeg_bytes).__name__)
            return

        image = QImage.fromData(jpeg_bytes)
        if image.isNull():
            return
        pixmap = QPixmap.fromImage(image)
        # Store and render using the centralized renderer so pan/zoom UI
        # remains in sync with the latest frame.
        self._render_pixmap(pixmap, ts)

    def _apply_state(self):
        """Push the user+totality state into the thread and refresh the button."""
        if self._thread is None:
            return
        if self._user_enabled and not self._totality_paused:
            self._thread.resume()
            self._toggle_btn.setText("Disable Live View")
        else:
            self._thread.pause()
            # Actually exit live view on the camera (Nikon D610 etc.)
            self._thread.exit_live_view()
            self._toggle_btn.setText("Enable Live View")
        self._update_status_label()

    def _update_status_label(self):
        if not self._user_enabled:
            self._status_label.setText("○  Disabled")
            self._status_label.setStyleSheet("color: gray;")
        elif self._totality_paused:
            self._status_label.setText(
                "\u23f8  Paused during totality — script has full USB control"
            )
            self._status_label.setStyleSheet("color: #856404; background: #FFF3CD; padding: 3px; border-radius: 3px;")
        else:
            self._status_label.setText("\u25cf  Active")
            self._status_label.setStyleSheet("color: green;")

    # ------------------------------------------------------------------
    # Public API (called by the controller)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # The live view contract.  Both windows - this one and the Fuji SDK one in
    # liveview.py - answer these, so the controller never has to know which
    # camera it is driving or reach into either one's private state.  Restoring
    # a window that predated set_totality_paused crashed the clock on 3 August;
    # writing the contract down is what stops the next one doing the same.
    # ------------------------------------------------------------------

    def set_totality_paused(self, paused: bool):
        """Auto-pause or auto-resume live view around totality."""
        if self._totality_paused == paused:
            return
        self._totality_paused = paused
        self._apply_state()

    def is_streaming(self) -> bool:
        """True when frames are actually being fetched."""
        return self._thread is not None and self._user_enabled and not self._totality_paused

    def start_stream(self):
        """Begin fetching frames, as though the user had enabled it."""
        self._user_enabled = True
        self._apply_state()

    def stop_stream(self):
        """Stop fetching frames and let go of the camera."""
        self._user_enabled = False
        self._apply_state()

    # ------------------------------------------------------------------
    # Slots / event handlers
    # ------------------------------------------------------------------

    def _on_toggle(self):
        self._user_enabled = not self._user_enabled
        self._apply_state()

    def _on_zoom_changed(self, index: int):
        """Slot for zoom combo box changes."""
        text = self._zoom_combo.currentText() if hasattr(self, "_zoom_combo") else "Fit"
        if text == "Fit":
            self._zoom_factor = 1.0
        else:
            try:
                # e.g. '5x' -> 5.0
                self._zoom_factor = float(text.rstrip('x'))
            except Exception:
                self._zoom_factor = 1.0
        # Re-render the last frame with the new zoom (if any)
        if getattr(self, "_last_pixmap", None) is not None:
            self._render_pixmap(self._last_pixmap, getattr(self, "_last_ts", datetime.datetime.now()))

    def _on_pan_changed(self, _value: int):
        """Called when either pan slider moves; re-render using last pixmap."""
        if getattr(self, "_last_pixmap", None) is not None:
            self._render_pixmap(self._last_pixmap, getattr(self, "_last_ts", datetime.datetime.now()))

    def _on_overlay_changed(self, _checked: bool):
        """Re-render with the current overlay settings, without waiting for a new frame."""
        if getattr(self, "_last_pixmap", None) is not None:
            self._render_pixmap(self._last_pixmap, getattr(self, "_last_ts", datetime.datetime.now()))

    def _on_focus_reset(self):
        """Forget the best sharpness seen, e.g. after moving to a different target."""
        self._best_focus_score = 0.0
        self._update_focus_label()

    def _update_focus_label(self):
        """Show the current sharpness relative to the best seen since the last reset."""
        if self._best_focus_score <= 0.0:
            self._focus_label.setText("Focus: –")
            self._focus_label.setStyleSheet("color: gray;")
            return

        percent = 100.0 * self._focus_score / self._best_focus_score
        self._focus_label.setText(f"Focus: {self._focus_score:,.0f}  ({percent:.0f}% of best)")
        if percent >= 99.0:
            # At or above the best seen — this is the reading to stop turning the ring on.
            self._focus_label.setStyleSheet("color: green; font-weight: bold;")
        elif percent >= 85.0:
            self._focus_label.setStyleSheet("color: #856404;")
        else:
            self._focus_label.setStyleSheet("color: gray;")

    def _render_pixmap(self, pixmap: QPixmap, ts: datetime.datetime):
        """Render the given QPixmap into the image label respecting zoom and pan.

        Stores the last pixmap/timestamp so slider updates can trigger re-renders.
        """
        self._last_pixmap = pixmap
        self._last_ts = ts

        zoom = getattr(self, "_zoom_factor", 1.0)
        ow, oh = pixmap.width(), pixmap.height()

        if zoom and zoom > 1.0 and ow > 0 and oh > 0:
            crop_w = max(1, int(ow / zoom))
            crop_h = max(1, int(oh / zoom))
            max_x = max(0, ow - crop_w)
            max_y = max(0, oh - crop_h)

            # Update slider ranges and make visible
            self._h_slider.blockSignals(True)
            self._v_slider.blockSignals(True)
            self._h_slider.setRange(0, max_x)
            self._h_slider.setPageStep(max(1, crop_w))
            self._v_slider.setRange(0, max_y)
            self._v_slider.setPageStep(max(1, crop_h))

            # If sliders were previously hidden, default to centred view
            if not self._h_slider.isVisible():
                self._h_slider.setValue(max_x // 2)
            if not self._v_slider.isVisible():
                self._v_slider.setValue(max_y // 2)

            self._h_slider.setVisible(True)
            self._v_slider.setVisible(True)
            self._h_slider.blockSignals(False)
            self._v_slider.blockSignals(False)

            x0 = min(self._h_slider.value(), max_x) if max_x > 0 else 0
            y0 = min(self._v_slider.value(), max_y) if max_y > 0 else 0

            try:
                crop = pixmap.copy(x0, y0, crop_w, crop_h)
            except Exception:
                crop = pixmap

            disp = crop.scaled(
                self._image_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        else:
            # Hide pan controls when not zoomed
            self._h_slider.setVisible(False)
            self._v_slider.setVisible(False)
            disp = pixmap.scaled(
                self._image_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )

        # Analyse what is actually on screen, so both the sharpness reading and the peaking
        # overlay follow the zoom: magnifying a detail gives a far more sensitive reading
        # than judging the whole frame at once.
        try:
            gray = qimage_to_gray_array(disp.toImage())
        except Exception:
            LOGGER.debug("Could not analyse live-view frame", exc_info=True)
            gray = None

        if gray is not None:
            self._focus_score = focus_score(gray)
            self._best_focus_score = max(self._best_focus_score, self._focus_score)
            self._update_focus_label()

        w, h = disp.width(), disp.height()
        painter = QPainter(disp)

        if gray is not None and self._peaking_check.isChecked():
            overlay = focus_peaking_overlay(gray)
            if overlay is not None:
                painter.drawImage(0, 0, overlay)

        if self._crosshair_check.isChecked():
            # Blue crosshair at the centre of the displayed pixmap
            pen = QPen(QColor(0, 120, 255))
            pen.setWidth(1)
            painter.setPen(pen)
            painter.drawLine(0, h // 2, w, h // 2)  # horizontal
            painter.drawLine(w // 2, 0, w // 2, h)  # vertical

        painter.end()

        self._image_label.setPixmap(disp)
        self._timestamp_label.setText("Last frame: " + ts.strftime("%Y-%m-%d  %H:%M:%S"))

    def closeEvent(self, event):
        self._poll_timer.stop()
        if self._thread is not None:
            self._thread.stop()
            self._thread = None
        event.accept()


def format_countdown(countdown: datetime.timedelta):
    """ Format the given countdown.

    Args:
        - countdown: Countdown as datetime

    Returns: Formatted countdown, with the days (if any), hours (if any), minutes, and seconds.
    """

    formatted_countdown = ""
    days = countdown.days

    if days > 0:
        formatted_countdown += f" {days}d "

    hours = countdown.seconds // 3600
    if days > 0 or hours > 0:
        formatted_countdown += f"{hours:02d}:"

    minutes, seconds = (countdown.seconds // 60) % 60, countdown.seconds % 60
    formatted_countdown += f"{minutes:02d}:{seconds:02d}"

    return formatted_countdown


def format_time(time: datetime.datetime, time_format: str) -> str:
    """Format the given time according to the given time format."""

    # Format with standard strftime (includes 6 digits of microseconds)
    formatted = time.strftime(TIME_FORMATS[time_format])

    # Slice off the last 5 digits of %f, leaving 1 decimal place (.f)
    formatted = formatted[:-5]

    suffix = ""
    if time_format == "12 hours":
        suffix = " am" if time.hour < 12 else " pm"

    return f"{formatted}{suffix}"


class CameraOverviewTableColumnNames(Enum):
    """ Enumeration of the column names for the table with the camera overview table. """

    CAMERA = "Camera name"
    MODE = "Mode (shoot/focus)"
    BATTERY_LEVEL = "Battery level [%]"
    FREE_MEMORY_GB = "Free memory [GB]"
    FREE_MEMORY_PERCENTAGE = "Free memory [%]"


class CameraOverviewTableModel(QAbstractTableModel):

    def __init__(self):
        """ Initialisation of the model for the table with the camera overview. """

        super().__init__()

        self.camera_overview_dict: Union[dict, None] = None
        # Optional callable invoked on the GUI thread after camera data is applied.
        # Set by the controller before calling update_camera_overview() so that
        # sync_camera_time / check_camera_state run only once the dict is ready.
        self.on_ready_callback = None

        self._data = pd.DataFrame(columns=[CameraOverviewTableColumnNames.CAMERA.value,
                                           CameraOverviewTableColumnNames.MODE.value,
                                           CameraOverviewTableColumnNames.BATTERY_LEVEL.value,
                                           CameraOverviewTableColumnNames.FREE_MEMORY_GB.value,
                                           CameraOverviewTableColumnNames.FREE_MEMORY_PERCENTAGE.value])
        # signal for async worker
        try:
            self.data_ready = pyqtSignal(object)
            self.data_ready.connect(self._on_data_ready)
        except Exception:
            # fallback for environments without Qt signal support during tests
            self.data_ready = None

    def rowCount(self, index):
        return self._data.shape[0]

    def columnCount(self, index):
        return self._data.shape[1]

    def headerData(self, section, orientation, role):
        # section is the index of the column/row.
        if role == Qt.ItemDataRole.DisplayRole:
            if orientation == Qt.Orientation.Horizontal:
                return str(self._data.columns[section])

            if orientation == Qt.Orientation.Vertical:
                return str(self._data.index[section])

    def data(self, index: QModelIndex, role):
        """ Formatting of the data to display. """

        if role == Qt.ItemDataRole.DisplayRole:

            value = self._data.loc[index.row()].iat[index.column()]
            return value

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if index.column() == 0:
                return Qt.AlignmentFlag.AlignLeft
            else:
                return Qt.AlignmentFlag.AlignRight

    def update_camera_overview(self):
        """ Update the camera overview. """
        logging.debug('CameraOverviewTableModel.update_camera_overview(): start (scheduling worker)')
        try:
            LOGGER.debug("CameraOverview: scheduling worker to probe cameras")
        except Exception:
            pass

        # clear current data quickly on UI thread
        self._data = pd.DataFrame(columns=self._data.columns)
        self.beginResetModel()
        self.endResetModel()

        # start background worker to probe cameras
        # prepare a slot for pending data written by the worker
        self._pending_data = None

        worker = threading.Thread(target=self._gather_camera_info, daemon=True)
        worker.start()

        # start a short polling timer on the main thread to apply data when available
        QTimer.singleShot(200, self._try_apply_pending)

    def _gather_camera_info(self):
        max_retries = 2
        attempt = 0

        while attempt < max_retries:
            try:
                is_sim = getattr(self.view, 'is_simulator', False) and getattr(self.view, 'virtual_camera_enabled', False)
                vc_fps = getattr(self.view, 'virtual_camera_fps', 1)

                # Reuse existing camera objects if available to avoid opening a new USB
                # connection while a previous connection (e.g. from take_picture) is still held.
                existing_map = getattr(self, 'camera_overview_dict', None)
                force_refresh = getattr(self, '_force_camera_refresh', False)

                if force_refresh or not existing_map or not all(v is not None for v in existing_map.values()):
                    if force_refresh:
                        logging.info('CameraOverview: forcing fresh detection (attempt %d)', attempt + 1)
                        seen = set()
                        for camera_name, camera in (existing_map or {}).items():
                            if camera is None or id(camera) in seen:
                                continue
                            seen.add(id(camera))
                            try:
                                camera.disconnect()
                            except Exception:
                                logging.debug('Worker: disconnect of %s raised (non-fatal)', camera_name)
                        self._force_camera_refresh = False

                    alias_map = ConfigManager().get_camera_aliases() or None
                    camera_dict = get_camera_dict(is_simulator=is_sim, alias_map=alias_map)
                    logging.debug('CameraOverview: fresh camera detection performed')
                else:
                    camera_dict = existing_map
                    logging.debug('CameraOverview: reusing %d existing camera object(s)', len(camera_dict))

                data = []
                seen_camera_ids: set = set()
                needs_refresh = False

                for camera_name, camera in camera_dict.items():
                    # Skip bare-key aliases that point to the same physical camera object
                    # already added under its full gphoto2 name (e.g. "Sony Alpha-A7r II"
                    # is a duplicate of "Sony Alpha-A7r II (Control)").
                    cam_id = id(camera)
                    if cam_id in seen_camera_ids:
                        logging.debug('Worker: skipping duplicate alias "%s" (same camera object)', camera_name)
                        continue
                    seen_camera_ids.add(cam_id)
                    try:
                        logging.debug('Worker: processing camera %s', camera_name)
                        battery_level = get_battery_level(camera).rstrip('%')
                        free_space_gb = get_free_space(camera)
                        total_space = get_space(camera)
                        if free_space_gb < 0 or total_space <= 0:
                            free_space_gb_str = 'N/A'
                            free_space_pct_str = 'N/A'
                        else:
                            free_space_gb_str = str(free_space_gb)
                            free_space_pct_str = str(int(free_space_gb / total_space * 100))
                        data.append([camera_name, _describe_camera_mode(camera_name, camera),
                                     str(battery_level), free_space_gb_str, free_space_pct_str])
                    except Exception as exc:
                        error_str = str(exc).lower()
                        if any(x in error_str for x in ['-52', '-2', 'could not find the requested device', 'bad parameters']):
                            logging.warning('Stale camera connection detected on %s (%s)', camera_name, exc)
                            hardware_problems.report(
                                str(camera_name),
                                'Lost the USB connection to this camera',
                                detail=str(exc),
                            )
                            needs_refresh = True
                            self._force_camera_refresh = True
                            break
                        logging.exception('Worker: exception while processing camera %s', camera_name)
                        # The row survives with N/A values, which on its own looks
                        # like the camera is simply idle.  Say why.
                        hardware_problems.report(
                            str(camera_name),
                            'Could not read battery and free space from this camera',
                            detail=str(exc),
                            severity='warning',
                        )
                        # Preserve the camera row with N/A values when probing fails so
                        # the camera does not disappear from the UI.
                        try:
                            data.append([camera_name, 'N/A', 'N/A', 'N/A', 'N/A'])
                        except Exception:
                            pass
                        continue

                # If we hit critical errors, retry once with fresh objects
                if needs_refresh and attempt == 0:
                    logging.info("Critical USB errors detected - resetting camera_overview_dict and retrying...")
                    self.camera_overview_dict = None
                    attempt += 1
                    continue

                # schedule UI update on main thread
                LOGGER.debug('Worker: gathered camera overview data: %s', data)
                # write pending data and the camera objects for the main thread poll to pick up
                try:
                    self._pending_data = data
                    # keep the mapping of camera name -> camera object for later actions
                    self._pending_camera_map = camera_dict
                except Exception:
                    logging.exception('Worker: could not set pending data')
                break
            except Exception as exc:
                logging.exception('Worker: failed to gather camera info')
                attempt += 1
                if attempt >= max_retries:
                    hardware_problems.report(
                        'Cameras',
                        'Could not read the connected cameras',
                        detail=str(exc),
                    )
                    break

    def _on_data_ready(self, data):
        try:
            LOGGER.debug("CameraOverview: on_data_ready called with " + data)
        except Exception:
            pass
        # Update internal dict for other parts of the app (store camera objects if available)
        try:
            # prefer the actual camera objects if the worker provided them
            pending_map = getattr(self, '_pending_camera_map', None)
            if pending_map:
                self.camera_overview_dict = pending_map
            else:
                # fallback: create a name->None map
                dmap = {}
                for row in data:
                    name = row[0]
                    dmap[name] = None
                self.camera_overview_dict = dmap
            # clear pending map
            try:
                self._pending_camera_map = None
            except Exception:
                pass
        except Exception:
            self.camera_overview_dict = None

        # Merge incoming data with previous data to avoid dropping cameras when
        # a probe fails or the worker returns partial/empty results.
        try:
            # Map new data by camera name for quick lookup
            new_map = {row[0]: list(row) for row in data}

            # Build a map of previous rows (camera_name -> row values)
            prev_map = {}
            try:
                for i in range(self._data.shape[0]):
                    prev_row = list(self._data.iloc[i].values)
                    prev_map[str(prev_row[0])] = prev_row
            except Exception:
                prev_map = {}

            # Determine the canonical ordering: prefer pending camera_map keys if available
            pending_map = getattr(self, '_pending_camera_map', None)
            if pending_map:
                order = list(pending_map.keys())
            elif data:
                order = [row[0] for row in data]
            else:
                order = list(prev_map.keys())

            # If there's no new order and we have previous data, keep previous table
            if not order and self._data is not None and not self._data.empty:
                return

            merged_rows = []
            for name in order:
                if name in new_map:
                    merged_rows.append(new_map[name])
                elif name in prev_map:
                    merged_rows.append(prev_map[name])
                else:
                    merged_rows.append([name, 'N/A', 'N/A', 'N/A', 'N/A'])

            self.beginResetModel()
            self._data = pd.DataFrame(merged_rows, columns=self._data.columns)
            self.endResetModel()
        except Exception:
            logging.exception('Could not merge camera overview data')
            # Fallback to previous behaviour
            try:
                self.beginResetModel()
                self._data = pd.DataFrame(data, columns=self._data.columns)
                self.endResetModel()
            except Exception:
                logging.exception('Fallback: could not set camera overview data')

        # Ensure the view updates and columns are sized to the new data
        try:
            if hasattr(self, 'view') and getattr(self.view, 'camera_overview', None):
                self.view.camera_overview.setModel(self)
                self.view.camera_overview.resizeColumnsToContents()
                try:
                    self.view.camera_overview.selectRow(0)
                except Exception:
                    pass
                self.view.camera_overview.repaint()
                LOGGER.debug("CameraOverview: view updated")
        except Exception:
            logging.exception('Could not update camera overview view after data ready')

        # If we have actual camera objects, start the Sony background downloader
        # automatically only when the camera reports PC-Only save destination.
        # Also, show an informational banner about the relevant settings.
        try:
            pm = getattr(self, 'camera_overview_dict', None)

            if pm:
                seen = set()
                banner_lines = []
                sony_banner_label_visibility = False

                for cam in pm.values():
                    camera_vendor = getattr(cam, 'vendor', None)

                    if camera_vendor == "Sony":
                        try:
                            if cam is None:
                                continue
                            if id(cam) in seen:
                                continue
                            seen.add(id(cam))

                            dest = get_sony_save_destination(cam)
                            image_quality = get_sony_image_quality(cam)
                            camera_model = getattr(cam, 'name', 'Unknown Sony')

                            # Decide action + message for this camera
                            if dest == "sdram":
                                text = (f"{camera_model}: currently saving photos to PC. "
                                        f"We recommend testing if 'PC+Camera' mode is faster.")
                                if image_quality != "RAW":
                                    text += f" Also, 'File Format' is set to '{image_quality}'. Please set it to 'RAW'!"
                                try:
                                    cam.start_background_downloader()
                                except Exception:
                                    logging.warning('%s: failed to start Background Downloader', camera_model)

                                banner_lines.append(text)
                                sony_banner_label_visibility = True

                            elif dest == "card+sdram":
                                text = (f"{camera_model}: currently in 'PC+Camera' mode. "
                                        f"We recommend testing if 'PC' mode is faster.")
                                if not image_quality.startswith("RAW+JPEG"):
                                    text += (" Also, 'File Format' is set to '{}'."
                                            " Please set it to 'RAW+JPEG' and choose 'JPEG Only' "
                                            "for 'RAW+J PC Save Img'.").format(image_quality)
                                try:
                                    cam.stop_background_downloader()
                                except Exception:
                                    pass

                                banner_lines.append(text)
                                sony_banner_label_visibility = True

                            elif dest == "card":
                                if image_quality != "RAW":
                                    text = f"{camera_model}: 'File Format' is set to '{image_quality}'. Please set it to 'RAW'!"
                                    banner_lines.append(text)
                                    sony_banner_label_visibility = True
                                else:
                                    # No banner needed for good RAW + Card-only config
                                    try:
                                        cam.stop_background_downloader()
                                    except Exception:
                                        pass
                            else:
                                # Unknown / unavailable destination
                                if image_quality != "RAW":
                                    text = f"{camera_model}: 'Quality' is set to '{image_quality}'. Please set it to 'RAW'!"
                                    banner_lines.append(text)
                                    sony_banner_label_visibility = True
                                else:
                                    try:
                                        cam.start_background_downloader()
                                    except Exception:
                                        logging.warning(
                                            '%s: failed to start Background Downlaoder', camera_model)

                        except Exception:
                            logging.debug('Error while checking Sony save destination for a camera', exc_info=True)

                # === Build final banner text ===
                if banner_lines:
                    full_text = "\n".join(banner_lines)
                    full_text += "\nIf this info is wrong or you changed settings - press 'Camera(s)' again!"

                    if hasattr(self, 'view') and getattr(self.view, 'sony_banner_label', None) is not None:
                        self.view.sony_banner_label.setText(full_text)
                        self.view.sony_banner_label.setVisible(sony_banner_label_visibility)
                        logging.info("Sony banner updated to:\n%s", full_text)
                else:
                    # Hide banner if no messages
                    if hasattr(self, 'view') and getattr(self.view, 'sony_banner_label', None) is not None:
                        self.view.sony_banner_label.setVisible(False)
            else:
                # Hide banner if no Sony cameras connected
                if hasattr(self, 'view') and getattr(self.view, 'sony_banner_label', None) is not None:
                    self.view.sony_banner_label.setVisible(False)
        except Exception:
            logging.exception("Error updating Sony banner / background downloaders")

        # Notify controller that cameras are ready (fires sync_camera_time + check_camera_state)
        cb = getattr(self, 'on_ready_callback', None)
        if cb is not None:
            try:
                cb()
            except Exception:
                logging.exception('on_ready_callback raised an exception')
            finally:
                self.on_ready_callback = None

    def _try_apply_pending(self):
        """Poll for pending data written by the background worker and apply it on the GUI thread."""
        try:
            data = getattr(self, '_pending_data', None)
            if data:
                # clear pending before applying to avoid races
                self._pending_data = None
                self._on_data_ready(data)
            else:
                # not ready yet — try again shortly
                QTimer.singleShot(200, self._try_apply_pending)
        except Exception:
            logging.exception('Error while polling for pending camera overview data')


class JobsTableColumnNames(Enum):
    """ Enumeration of the column names for the table with the scheduled jobs. """

    EXEC_TIME_UTC = "Execution time (UTC)"
    EXEC_TIME_LOCAL = "Execution time (local)"
    COUNTDOWN = "Countdown"
    COMMAND = "Command"
    DESCRIPTION = "Description"


class JobsTableModel(QAbstractTableModel, Observable):
    def __init__(self, scheduler: BackgroundScheduler, controller: SolarEclipseController):
        """ Initialisation of the model for the table with the scheduled jobs.

        Args:
            - scheduler: Background scheduler
            - model: Model for the Solar Eclipse Workbench UI
        """

        super().__init__()
        self.controller = controller
        self.time_format = self.controller.view.time_format

        from solareclipseworkbench.reference_moments import _find_timezone
        timezone = pytz.timezone(_find_timezone(self.controller.model.longitude, self.controller.model.latitude))

        now_utc = datetime.datetime.now().astimezone(tz=datetime.timezone.utc)
        data = []

        self.execution_times_utc_as_datetime = []
        self.execution_times_local_as_datetime = []

        job: Job
        for job in scheduler.get_jobs():

            execution_time_utc: datetime.datetime = job.next_run_time
            if execution_time_utc:
                execution_time_local = execution_time_utc.astimezone(timezone)

                countdown = "-"
                if now_utc <= execution_time_utc:
                    countdown = format_countdown(execution_time_utc - now_utc)
                description: str = job.name

                job_string = ""

                if job.func.__name__ == "take_picture":
                    camera_settings: CameraSettings = job.args[1]
                    camera_name = camera_settings.camera_name
                    shutter_speed = camera_settings.shutter_speed
                    aperture = camera_settings.aperture
                    iso = camera_settings.iso

                    job_string = f"take_picture(\"{camera_name}\", {shutter_speed}, {aperture}, {iso})"

                elif job.func.__name__ == "take_burst":
                    camera_settings: CameraSettings = job.args[1]
                    camera_name = camera_settings.camera_name
                    shutter_speed = camera_settings.shutter_speed
                    aperture = camera_settings.aperture
                    iso = camera_settings.iso
                    duration = job.args[2]

                    job_string = f"take_burst(\"{camera_name}\", {shutter_speed}, {aperture}, {iso}, {duration})"

                elif job.func.__name__ == "take_bracket":
                    camera_settings: CameraSettings = job.args[1]
                    camera_name = camera_settings.camera_name
                    shutter_speed = camera_settings.shutter_speed
                    aperture = camera_settings.aperture
                    iso = camera_settings.iso
                    step = job.args[2]

                    job_string = f"take_bracket(\"{camera_name}\", {shutter_speed}, {aperture}, {iso}, {step})"

                elif job.func.__name__ == "take_hdr":
                    camera_settings: CameraSettings = job.args[1]
                    camera_name = camera_settings.camera_name
                    shutter_speed = camera_settings.shutter_speed
                    aperture = camera_settings.aperture
                    iso = camera_settings.iso
                    stops = job.args[2]

                    job_string = f"take_hdr(\"{camera_name}\", {shutter_speed}, {aperture}, {iso}, {stops} stops)"

                elif job.func.__name__ == "sync_cameras":
                    job_string = f"sync_cameras()"

                elif job.func.__name__ == "voice_prompt":
                    job_string = f"{job.func.__name__}({', '.join(job.args).strip()})"

                elif job.func.__name__ == "execute_command":
                    job_string = f"command({', '.join(job.args).strip()})"

                self.execution_times_utc_as_datetime.append(execution_time_utc)
                formatted_execution_time_utc = format_time(execution_time_utc, self.time_format)

                self.execution_times_local_as_datetime.append(execution_time_local)
                formatted_execution_time_local = format_time(execution_time_local, self.time_format)

                data.append([countdown, formatted_execution_time_local, formatted_execution_time_utc,
                             job_string, description])

        self._data = pd.DataFrame(data, columns=[JobsTableColumnNames.COUNTDOWN.value,
                                                 JobsTableColumnNames.EXEC_TIME_LOCAL.value,
                                                 JobsTableColumnNames.EXEC_TIME_UTC.value,
                                                 JobsTableColumnNames.COMMAND.value,
                                                 JobsTableColumnNames.DESCRIPTION.value])

    def update_countdown(self):
        """ Update the countdown until execution time."""

        if self._data.shape[0] > 0:

            self.beginResetModel()
            now_utc = datetime.datetime.now().astimezone(tz=datetime.timezone.utc)
            time_format = self.controller.view.time_format
            for row in range(len(self.execution_times_local_as_datetime)):

                new_countdown = self.execution_times_utc_as_datetime[row] - now_utc
                if new_countdown.total_seconds() >= 0:
                    if int(new_countdown.total_seconds()) == 0:
                        self.notify_observers(row)
                    new_countdown = format_countdown(new_countdown)
                else:
                    new_countdown = "-"
                self._data.loc[row, JobsTableColumnNames.COUNTDOWN.value] = new_countdown

                if self.time_format != time_format:
                    self._data.loc[row, JobsTableColumnNames.EXEC_TIME_UTC.value] \
                        = format_time(self.execution_times_utc_as_datetime[row], time_format)
                    self._data.loc[row, JobsTableColumnNames.EXEC_TIME_LOCAL.value] \
                        = format_time(self.execution_times_local_as_datetime[row], time_format)

            self.time_format = time_format

            self.endResetModel()

    def clear_jobs_overview(self):
        """ Clear the scheduled jobs overview. """

        self.beginResetModel()
        self._data = pd.DataFrame(columns=self._data.columns)
        self.endResetModel()

    def rowCount(self, index):
        return self._data.shape[0]

    def columnCount(self, index):
        return self._data.shape[1]

    def headerData(self, section, orientation, role):
        # section is the index of the column/row.
        if role == Qt.ItemDataRole.DisplayRole:
            if orientation == Qt.Orientation.Horizontal:
                return str(self._data.columns[section])

            if orientation == Qt.Orientation.Vertical:
                return str(self._data.index[section])

    def data(self, index: QModelIndex, role):
        """ Formatting of the data to display. """

        if role == Qt.ItemDataRole.DisplayRole:

            value = self._data.loc[index.row()].iat[index.column()]

            # Perform per-type checks and render accordingly.
            if isinstance(value, datetime.datetime):
                return format_time(value, self.controller.view.time_format)
            return value

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if index.column() == 0:
                return Qt.AlignmentFlag.AlignRight
            elif index.column() <= 2:
                return Qt.AlignmentFlag.AlignHCenter
            else:
                return Qt.AlignmentFlag.AlignLeft


class QJobsTableView(QTableView):

    def __init__(self):
        super().__init__()

    def update(self, row: int):
        """ Scroll to the jobs that are up next.

        Args:
            - row: Row index of the first job that will be executed next.
        """

        index: QModelIndex = self.model().index(min(row + 5, self.model().rowCount(None) - 1), 0)
        self.setCurrentIndex(index)

    def do(self, actions):
        pass


#: Where a native crash writes its stack.  Kept out of the rotating log because
#: the crash happens after logging has stopped being able to help.
CRASH_LOG = Path("/tmp/solareclipseworkbench-crash.log")


def _catch_native_crashes():
    """Make a segmentation fault name the thread and line that caused it.

    The SDK is a C library called from several threads, and when it faults the
    process dies with no Python traceback at all - the log simply stops.  That
    happened at 15:59:14 on 4 August, two seconds after live view opened during
    a run, and left nothing to work from: no traceback, and macOS wrote no
    crash report either.

    faulthandler writes the stack of every thread on SIGSEGV, SIGBUS, SIGFPE
    and SIGABRT.  It costs nothing until something goes wrong, and it is the
    difference between "it crashed" and knowing which call did it.
    """
    try:
        handle = CRASH_LOG.open("a")
        faulthandler.enable(file=handle, all_threads=True)
        LOGGER.debug("Native crash handler writing to %s", CRASH_LOG)
    except Exception:
        LOGGER.debug("Could not install the native crash handler", exc_info=True)


def _keep_running_on_unhandled_errors():
    """Log an unhandled exception instead of killing the application.

    PyQt calls qFatal() when a Python exception escapes a slot, so the process
    aborts outright - no traceback in the log, because the message goes to
    stderr, and no crash report.  That is how a single AttributeError in the
    live view button took the whole GUI down at 15:59 on 4 August, two minutes
    before second contact in the simulation.

    Installing an excepthook stops the abort.  A bug in a button must not be
    able to end a run: the schedule is the point of this program, it is still
    running in its own threads, and totality does not wait while somebody
    restarts the app and reloads a script.
    """
    interrupted = []

    def _hook(kind, value, traceback_object):
        # Asking to quit is not an error to be survived.  The relay's SIGINT
        # guard releases its contacts and re-raises, that lands in a Qt slot,
        # and a hook that logs everything and carries on makes Ctrl-C do
        # nothing at all - which is what it did on 4 August.
        if issubclass(kind, (KeyboardInterrupt, SystemExit)):
            application = QApplication.instance()
            if interrupted or application is None:
                # Asked twice, or there is no event loop left to ask.  Go now
                # rather than leaving somebody holding a window that will not
                # close; the shutdown guards have already run by this point.
                LOGGER.warning("Interrupted again - exiting immediately")
                os._exit(130)
            interrupted.append(True)
            LOGGER.info("Interrupted - closing down")
            # Stop the background work first.  A scheduler still running keeps
            # the interpreter alive long after the window has gone, and its
            # default shutdown waits for the job in flight - which can be a
            # seven rung bracket.  Somebody pressing Ctrl-C has stopped caring
            # about the frame that is in the air.
            try:
                from solareclipseworkbench.utils import stop_all_schedulers
                stop_all_schedulers()
            except Exception:
                LOGGER.debug("Could not stop the schedulers", exc_info=True)
            try:
                from solareclipseworkbench.relay_trigger import _release_all_contacts
                _release_all_contacts()
            except Exception:
                LOGGER.debug("Could not open the relay contacts", exc_info=True)
            # No closeAllWindows() either.  Closing the main window runs its
            # closeEvent, which asks "are you sure you want to exit?" in a modal
            # box - so the attempt to leave puts up one more dialog to be stuck
            # behind.  Somebody who pressed Ctrl-C has already answered that
            # question.
            #
            # Not application.quit().  That ends the main event loop, and a
            # modal dialog runs its own nested one - QDialog::exec() was
            # exactly where the main thread sat, on 4 August, while "Interrupted
            # - closing down" was already in the log and the process would not
            # die.  Any dialog open at the wrong moment made Ctrl-C useless.
            #
            # Everything that must happen has happened by now: the contacts are
            # open, the schedulers are stopped, the log is flushed.  What is
            # left is a window, and the answer to "quit" should not depend on
            # which dialog is on top of it.
            logging.shutdown()
            os._exit(130)

        LOGGER.error("Unhandled %s in the interface - the schedule keeps "
                     "running", kind.__name__,
                     exc_info=(kind, value, traceback_object))
        try:
            faulthandler.dump_traceback(file=CRASH_LOG.open("a"), all_threads=False)
        except Exception:
            pass

    sys.excepthook = _hook


def main():
    _catch_native_crashes()
    _keep_running_on_unhandled_errors()

    # Ensure the Fuji SDK's libraries are on LD_LIBRARY_PATH before anything
    # else runs.  If a re-exec is needed it happens here, at launch, instead of
    # mid-session during camera detection (which would discard unsaved settings).
    maybe_reexec_for_fuji_sdk()

    time_string = time.strftime("%Y%m%d-%H%M%S")
    logging.basicConfig(filename=f'{time_string}.log', level=logging.DEBUG, format='%(asctime)s %(message)s')
    # Also log to stdout so users see debug output in terminal
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.getLogger().addHandler(console_handler)
    LOGGER.info("Starting up Solar Eclipse Workbench")

    parser = argparse.ArgumentParser(description="Solar Eclipse Workbench")
    parser.add_argument(
        "-s",
        "--sim",
        help="Start up in simulator mode",
        default=False,
        action='store_true'
    )
    parser.add_argument(
        "-vc",
        "--virtual-camera",
        help="Enable virtual camera (when starting GUI in simulator mode)",
        action='store_true',
        default=False,
    )
    parser.add_argument(
        "-lon",
        "--longitude",
        help="longitude of the location where to watch the solar eclipse (W is negative)",
        default=False,
        type=float
    )

    parser.add_argument(
        "-lat",
        "--latitude",
        help="latitude of the location where to watch the solar eclipse (N is positive)",
        default=False,
        type=float
    )

    parser.add_argument(
        "-alt",
        "--altitude",
        help="altitude of the location where to watch the solar eclipse (in metres)",
        default=False,
        type=float
    )

    parser.add_argument(
        "-d",
        "--date",
        help="date of the solar eclipse (in YYYY-MM-DD format)",
        default=False,
    )

    parser.add_argument(
        "-lc",
        "--low-cpu",
        help="Disable the eclipse visualization plot auto-update",
        action='store_true',
        default=False,
    )

    parser.add_argument(
        "-scm",
        "--sony-cont-mode",
        help="Specify continuous mode name for Sony cameras"
    )

    args = parser.parse_args()
    configuration.SONY_CONTINUOUS_MODE = args.sony_cont_mode
    # args[1:1] = ["-stylesheet", str(styles_location)]
    app = QApplication(list(sys.argv))
    apply_system_color_scheme(app)
    app.setWindowIcon(QIcon(str(ICON_PATH / "logo-small.svg")))
    app.setApplicationName("Solar Eclipse Workbench")

    model = SolarEclipseModel()
    view = SolarEclipseView(is_simulator=args.sim,low_cpu_mode=args.low_cpu)
    # Attach virtual camera defaults to the view so other parts can query them
    view.virtual_camera_enabled = args.virtual_camera

    controller = SolarEclipseController(
        model,
        view,
        is_simulator=args.sim,
        low_cpu_mode=args.low_cpu
    )

    # Make the view available to the camera overview model so it can read simulator flags
    model.camera_overview.view = view

    if args.longitude and args.latitude and args.altitude:
        controller.set_location(args.longitude, args.latitude, args.altitude)

    if args.date:
        controller.set_eclipse_date(args.date, date_format=None)

    # Show the main window first, then schedule reference-moments calculation so
    # the loading dialog is shown on top of the visible GUI if helper files
    # need to be downloaded.
    view.show()

    if args.longitude and args.latitude and args.altitude and args.date:
        # Schedule after the event loop starts so the main window is painted.
        QTimer.singleShot(0, controller.set_reference_moments)

    code = app.exec()

    # The event loop has ended; everything below decides whether the process
    # actually goes away.
    #
    # It did not, on 4 August: one Ctrl-C left the window gone and the process
    # alive.  concurrent.futures registers an atexit hook that joins its worker
    # threads, the scheduler runs jobs on exactly such a pool, and a job in
    # flight can be a seven rung bracket - so the interpreter waited for a
    # frame nobody was waiting for any more.
    #
    # So the work that matters is done here, explicitly and in order, and then
    # the process ends without waiting for anything else.  The contacts are
    # opened first: that is the one thing that must never be skipped, and
    # os._exit skips atexit, which is where it used to be handled.
    try:
        from solareclipseworkbench.relay_trigger import _release_all_contacts
        _release_all_contacts()
    except Exception:
        LOGGER.debug("Could not open the relay contacts on the way out",
                     exc_info=True)
    try:
        from solareclipseworkbench.utils import stop_all_schedulers
        stop_all_schedulers()
    except Exception:
        LOGGER.debug("Could not stop the schedulers on the way out", exc_info=True)

    logging.shutdown()
    os._exit(code)


def sync_cameras(controller: SolarEclipseController):
    """ Synchronise the cameras for the given controller.

    This consists of the following steps:

        - Update the camera overview in the model and the view of the given controller;
        - Set the time of all connected cameras to the time of the computer;
        - Check whether the focus mode and shooting mode of all connected cameras is set to 'Manual'.

    Args:
        - controller: Controller of the Solar Eclipse Workbench UI, or None when
                      running headless (sew.py without --gui).
    """

    if controller is None:
        # What this refreshes is a Qt table, so headless there is nothing to do.
        # Raising instead would turn every sync_cameras line in a script into a
        # traceback: observe_solar_eclipse passes None for the controller on the
        # command-line path, and the scripts all carry several syncs.
        logging.info('sync_cameras: running headless, no camera overview to refresh')
        return

    controller.model.camera_overview.update_camera_overview()


if __name__ == "__main__":

    sys.exit(main())
