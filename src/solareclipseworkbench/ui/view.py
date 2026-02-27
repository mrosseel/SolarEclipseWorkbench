"""View for the Solar Eclipse Workbench UI (MVC pattern)."""

import datetime
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon, QAction, QCloseEvent
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QFrame, QLabel, QHBoxLayout, QVBoxLayout,
    QGridLayout, QGroupBox, QScrollArea, QTableView,
)

from solareclipseworkbench.observer import Observable
from solareclipseworkbench.eclipse.reference_moments import ReferenceMomentInfo
from .helpers import DATE_FORMATS, TIME_FORMATS, format_countdown, format_time
from .tables import QJobsTableView

ICON_PATH = Path(__file__).parent.parent.resolve() / "img"


class SolarEclipseView(QMainWindow, Observable):
    """ View for the Solar Eclipse Workbench UI in the MVC pattern. """

    def __init__(self, is_simulator: bool = False):
        """ Initialisation of the view of the Solar Eclipse Workbench UI.

        Args:
            - is_simulator: Indicates whether the UI should be started in simulator mode
        """

        super().__init__()

        self.controller = None
        self.is_simulator = is_simulator

        self.setGeometry(300, 300, 1500, 1000)
        self.setWindowTitle("Solar Eclipse Workbench")

        self.date_format = list(DATE_FORMATS.keys())[0]
        self.time_format = list(TIME_FORMATS.keys())[0]

        self.toolbar = None
        self.location_action = QAction("Location", self)
        self.date_action = QAction("Date", self)
        self.reference_moments_action = QAction("Reference moments", self)
        self.camera_action = QAction("Camera(s)", self)
        self.live_view_action = QAction("Live View", self)
        self.simulator_action = QAction("Simulator", self)
        self.file_action = QAction("File", self)
        self.shutdown_scheduler_action = QAction("Stop", self)
        self.datetime_format_action = QAction("Datetime format", self)
        self.save_action = QAction("Save", self)

        self.place_time_frame = QFrame()

        self.eclipse_date_widget = QWidget()
        self.eclipse_date_label = QLabel(f"Eclipse date [{self.date_format}]")
        self.eclipse_date = QLabel("")

        self.reference_moments_widget = QWidget()

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

        self.date_label = QLabel(f"Date [{self.date_format}]")
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

        self.camera_overview = QTableView()

        self.jobs_table = QJobsTableView()

        self.init_ui()

    def save_settings(self):
        """ Save the settings. """

        longitude = self.longitude_label.text()
        latitude = self.latitude_label.text()
        altitude = self.altitude_label.text()

        if longitude and latitude and altitude:
            self.settings.setValue("longitude", float(longitude))
            self.settings.setValue("latitude", float(latitude))
            self.settings.setValue("altitude", float(altitude))

        self.settings.setValue("eclipse_date", self.eclipse_date.text())

        self.settings.setValue("date_format", self.date_format)
        self.settings.setValue("time_format", self.time_format)

    def init_ui(self):
        """ Add all components to the UI. """

        app_frame = QFrame()
        app_frame.setObjectName("AppFrame")

        self.add_toolbar()

        vbox_left = QVBoxLayout()

        place_time_group_box = QGroupBox()
        place_time_grid_layout = QGridLayout()

        place_time_grid_layout.addWidget(QLabel("Local", alignment=Qt.AlignmentFlag.AlignRight), 0, 1)
        place_time_grid_layout.addWidget(QLabel("UTC", alignment=Qt.AlignmentFlag.AlignRight), 0, 2)

        place_time_grid_layout.addWidget(self.date_label, 1, 0)
        place_time_grid_layout.addWidget(self.date_label_local, 1, 1)
        place_time_grid_layout.addWidget(self.date_label_utc, 1, 2)

        place_time_grid_layout.addWidget(QLabel("Time"), 2, 0)
        place_time_grid_layout.addWidget(self.time_label_local, 2, 1)
        place_time_grid_layout.addWidget(self.time_label_utc, 2, 2)

        place_time_group_box.setLayout(place_time_grid_layout)
        place_time_group_box.setFixedWidth(400)
        vbox_left.addWidget(place_time_group_box)

        location_group_box = QGroupBox()
        location_grid_layout = QGridLayout()
        location_grid_layout.addWidget(QLabel("Longitude [\u00b0]"), 0, 0)
        location_grid_layout.addWidget(self.longitude_label, 0, 1)
        location_grid_layout.addWidget(QLabel("Latitude [\u00b0]"), 1, 0)
        location_grid_layout.addWidget(self.latitude_label, 1, 1)
        location_grid_layout.addWidget(QLabel("Altitude [m]"), 2, 0)
        location_grid_layout.addWidget(self.altitude_label, 2, 1)
        location_group_box.setLayout(location_grid_layout)
        location_group_box.setFixedWidth(400)
        vbox_left.addWidget(location_group_box)

        eclipse_date_group_box = QGroupBox()
        eclipse_date_grid_layout = QGridLayout()
        eclipse_date_grid_layout.addWidget(self.eclipse_date_label, 0, 0)
        eclipse_date_grid_layout.addWidget(self.eclipse_date, 0, 1)
        eclipse_date_grid_layout.addWidget(QLabel("Eclipse type"), 1, 0)
        eclipse_date_grid_layout.addWidget(self.eclipse_type, 1, 1)

        eclipse_date_group_box.setLayout(eclipse_date_grid_layout)
        eclipse_date_group_box.setFixedWidth(400)
        vbox_left.addWidget(eclipse_date_group_box)

        reference_moments_group_box = QGroupBox()
        reference_moments_grid_layout = QGridLayout()
        reference_moments_grid_layout.addWidget(QLabel("Time (local)", alignment=Qt.AlignmentFlag.AlignRight), 0, 1)
        reference_moments_grid_layout.addWidget(self.c1_time_local_label, 1, 1)
        reference_moments_grid_layout.addWidget(self.c2_time_local_label, 2, 1)
        reference_moments_grid_layout.addWidget(self.max_time_local_label, 3, 1)
        reference_moments_grid_layout.addWidget(self.c3_time_local_label, 4, 1)
        reference_moments_grid_layout.addWidget(self.c4_time_local_label, 5, 1)
        reference_moments_grid_layout.addWidget(self.sunrise_time_local_label, 6, 1)
        reference_moments_grid_layout.addWidget(self.sunset_time_local_label, 7, 1)
        reference_moments_grid_layout.addWidget(QLabel("Time (UTC)", alignment=Qt.AlignmentFlag.AlignRight), 0, 2)
        reference_moments_grid_layout.addWidget(self.c1_time_utc_label, 1, 2)
        reference_moments_grid_layout.addWidget(self.c2_time_utc_label, 2, 2)
        reference_moments_grid_layout.addWidget(self.max_time_utc_label, 3, 2)
        reference_moments_grid_layout.addWidget(self.c3_time_utc_label, 4, 2)
        reference_moments_grid_layout.addWidget(self.c4_time_utc_label, 5, 2)
        reference_moments_grid_layout.addWidget(self.sunrise_time_utc_label, 6, 2)
        reference_moments_grid_layout.addWidget(self.sunset_time_utc_label, 7, 2)
        reference_moments_grid_layout.addWidget(QLabel("Countdown", alignment=Qt.AlignmentFlag.AlignRight), 0, 3)
        reference_moments_grid_layout.addWidget(self.c1_countdown_label, 1, 3)
        reference_moments_grid_layout.addWidget(self.c2_countdown_label, 2, 3)
        reference_moments_grid_layout.addWidget(self.max_countdown_label, 3, 3)
        reference_moments_grid_layout.addWidget(self.c3_countdown_label, 4, 3)
        reference_moments_grid_layout.addWidget(self.c4_countdown_label, 5, 3)
        reference_moments_grid_layout.addWidget(self.sunrise_countdown_label, 6, 3)
        reference_moments_grid_layout.addWidget(self.sunset_countdown_label, 7, 3)
        reference_moments_grid_layout.addWidget(QLabel("Azimuth [\u00b0]", alignment=Qt.AlignmentFlag.AlignRight), 0, 4)
        reference_moments_grid_layout.addWidget(self.c1_azimuth_label, 1, 4)
        reference_moments_grid_layout.addWidget(self.c2_azimuth_label, 2, 4)
        reference_moments_grid_layout.addWidget(self.max_azimuth_label, 3, 4)
        reference_moments_grid_layout.addWidget(self.c3_azimuth_label, 4, 4)
        reference_moments_grid_layout.addWidget(self.c4_azimuth_label, 5, 4)
        reference_moments_grid_layout.addWidget(QLabel("Altitude [\u00b0]", alignment=Qt.AlignmentFlag.AlignRight), 0, 5)
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
        reference_moments_group_box.setLayout(reference_moments_grid_layout)
        reference_moments_group_box.setFixedWidth(600)

        hbox = QHBoxLayout()
        hbox.addLayout(vbox_left)
        hbox.addWidget(reference_moments_group_box)

        self.camera_overview.setFixedHeight(300)
        hbox.addWidget(self.camera_overview)

        scroll = QScrollArea()
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidgetResizable(True)

        scroll.setWidget(self.jobs_table)

        global_layout = QVBoxLayout()
        global_layout.addLayout(hbox)

        global_layout.addWidget(scroll)

        app_frame.setLayout(global_layout)

        self.setCentralWidget(app_frame)

    def add_toolbar(self):
        """ Create the toolbar of the UI. """

        self.toolbar = self.addToolBar('MainToolbar')

        self.location_action.setStatusTip("Location")
        self.location_action.setIcon(QIcon(str(ICON_PATH / "location.png")))
        self.location_action.triggered.connect(self.on_toolbar_button_click)
        self.toolbar.addAction(self.location_action)

        self.date_action.setStatusTip("Date")
        self.date_action.setIcon(QIcon(str(ICON_PATH / "calendar.png")))
        self.date_action.triggered.connect(self.on_toolbar_button_click)
        self.toolbar.addAction(self.date_action)

        self.reference_moments_action.setStatusTip("Reference moments")
        self.reference_moments_action.setIcon(QIcon(str(ICON_PATH / "clock.png")))
        self.reference_moments_action.triggered.connect(self.on_toolbar_button_click)
        self.toolbar.addAction(self.reference_moments_action)

        self.camera_action.setStatusTip("Camera(s)")
        self.camera_action.setIcon(QIcon(str(ICON_PATH / "camera.png")))
        self.camera_action.triggered.connect(self.on_toolbar_button_click)
        self.toolbar.addAction(self.camera_action)

        self.live_view_action.setStatusTip("Live View")
        self.live_view_action.triggered.connect(self.on_toolbar_button_click)
        self.toolbar.addAction(self.live_view_action)

        if self.is_simulator:
            self.simulator_action.setStatusTip("Configure simulator")
            self.simulator_action.setIcon(QIcon(str(ICON_PATH / "simulator.png")))
            self.simulator_action.triggered.connect(self.on_toolbar_button_click)
            self.toolbar.addAction(self.simulator_action)

        self.file_action.setStatusTip("File")
        self.file_action.setIcon(QIcon(str(ICON_PATH / "folder.png")))
        self.file_action.triggered.connect(self.on_toolbar_button_click)
        self.toolbar.addAction(self.file_action)

        self.shutdown_scheduler_action.setStatusTip("Shut down scheduler")
        self.shutdown_scheduler_action.setIcon(QIcon(str(ICON_PATH / "stop.png")))
        self.shutdown_scheduler_action.triggered.connect(self.on_toolbar_button_click)
        self.toolbar.addAction(self.shutdown_scheduler_action)

        self.datetime_format_action.setStatusTip("Datetime format")
        self.datetime_format_action.setIcon(QIcon(str(ICON_PATH / "settings.png")))
        self.datetime_format_action.triggered.connect(self.on_toolbar_button_click)
        self.toolbar.addAction(self.datetime_format_action)

        self.save_action.setStatusTip("Save configuration")
        self.save_action.setIcon(QIcon(str(ICON_PATH / "save.png")))
        self.save_action.triggered.connect(self.on_toolbar_button_click)
        self.toolbar.addAction(self.save_action)

    def on_toolbar_button_click(self):
        """ Action triggered when a toolbar button is clicked."""

        sender = self.sender()
        self.notify_observers(sender)

    def update_time(self, current_time_local, current_time_utc,
                    countdown_c1, countdown_c2, countdown_max, countdown_c3,
                    countdown_c4, countdown_sunrise, countdown_sunset):
        """ Update the displayed current time and countdown clocks. """

        self.eclipse_date_label.setText(f"Eclipse date [{self.date_format}]")

        self.date_label.setText(f"Date [{self.date_format}]")
        self.date_label_local.setText(datetime.datetime.strftime(current_time_local, DATE_FORMATS[self.date_format]))
        self.date_label_utc.setText(datetime.datetime.strftime(current_time_utc, DATE_FORMATS[self.date_format]))

        self.time_label_local.setText(format_time(current_time_local, self.time_format))
        self.time_label_utc.setText(format_time(current_time_utc, self.time_format))

        for label, cd in [(self.c1_countdown_label, countdown_c1),
                          (self.c2_countdown_label, countdown_c2),
                          (self.max_countdown_label, countdown_max),
                          (self.c3_countdown_label, countdown_c3),
                          (self.c4_countdown_label, countdown_c4),
                          (self.sunrise_countdown_label, countdown_sunrise),
                          (self.sunset_countdown_label, countdown_sunset)]:
            if not cd or cd.total_seconds() <= 0:
                label.setText("-")
            else:
                label.setText(str(format_countdown(cd)))

    def show_reference_moments(self, reference_moments: dict, magnitude: float, eclipse_type: str):
        """ Display the given reference moments, magnitude, and eclipse type. """

        if eclipse_type == "Partial" or eclipse_type == "Annular":
            self.eclipse_type.setText(eclipse_type + f" eclipse ({round(magnitude, 2)})")
        elif eclipse_type == "No eclipse":
            self.eclipse_type.setText(eclipse_type)
        else:
            minutes, seconds = divmod(reference_moments["duration"].seconds, 60)
            self.eclipse_type.setText(f"{eclipse_type} eclipse ({minutes}:{seconds:02})")

        for key, utc_label, local_label, az_label, alt_label in [
            ("C1", self.c1_time_utc_label, self.c1_time_local_label, self.c1_azimuth_label, self.c1_altitude_label),
            ("C2", self.c2_time_utc_label, self.c2_time_local_label, self.c2_azimuth_label, self.c2_altitude_label),
            ("MAX", self.max_time_utc_label, self.max_time_local_label, self.max_azimuth_label, self.max_altitude_label),
            ("C3", self.c3_time_utc_label, self.c3_time_local_label, self.c3_azimuth_label, self.c3_altitude_label),
            ("C4", self.c4_time_utc_label, self.c4_time_local_label, self.c4_azimuth_label, self.c4_altitude_label),
        ]:
            if key in reference_moments:
                info: ReferenceMomentInfo = reference_moments[key]
                utc_label.setText(format_time(info.time_utc, self.time_format))
                local_label.setText(format_time(info.time_local, self.time_format))
                az_label.setText(str(int(info.azimuth)))
                alt_label.setText(str(int(info.altitude)))
            else:
                utc_label.setText("")
                local_label.setText("")
                az_label.setText("")
                alt_label.setText("")

        sunrise_info: ReferenceMomentInfo = reference_moments["sunrise"]
        self.sunrise_time_utc_label.setText(format_time(sunrise_info.time_utc, self.time_format))
        self.sunrise_time_local_label.setText(format_time(sunrise_info.time_local, self.time_format))

        sunset_info: ReferenceMomentInfo = reference_moments["sunset"]
        self.sunset_time_utc_label.setText(format_time(sunset_info.time_utc, self.time_format))
        self.sunset_time_local_label.setText(format_time(sunset_info.time_local, self.time_format))

    def closeEvent(self, close_event: QCloseEvent):
        """ Disconnect cameras when the UI is closed. """

        self.notify_observers(close_event)
