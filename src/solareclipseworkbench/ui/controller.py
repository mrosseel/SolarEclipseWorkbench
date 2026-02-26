"""Controller for the Solar Eclipse Workbench UI (MVC pattern)."""

import datetime
import logging
import os.path
from pathlib import Path
from typing import Union

from PyQt6.QtCore import QTimer, QSettings
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QFileDialog, QDialog, QVBoxLayout, QLabel, QComboBox, QFormLayout, QDialogButtonBox
from apscheduler.schedulers import SchedulerNotRunningError
from apscheduler.schedulers.background import BackgroundScheduler
from astropy.time import Time
from gphoto2 import Camera

from solareclipseworkbench.camera import get_battery_level
from solareclipseworkbench.observer import Observer
from .helpers import DATE_FORMATS, TIME_FORMATS, BEFORE_AFTER, format_time
from .model import SolarEclipseModel
from .view import SolarEclipseView
from .tables import CameraOverviewTableModel, JobsTableModel
from .popups import LocationPopup, EclipsePopup, SimulatorPopup, SettingsPopup

LOGGER = logging.getLogger("Solar Eclipse Workbench UI")


class SolarEclipseController(Observer):
    """ Controller for the Solar Eclipse Workbench UI in the MVC pattern. """

    def __init__(self, model: SolarEclipseModel, view: SolarEclipseView, is_simulator: bool):
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
        self.sim_reference_moment: Union[str, None] = None
        self.sim_offset_minutes: Union[int, None] = None

        self.location_popup: Union[LocationPopup, None] = None
        self.eclipse_popup: Union[EclipsePopup, None] = None
        self.simulator_popup: Union[SimulatorPopup, None] = None
        self.settings_popup: Union[SettingsPopup, None] = None

        self.time_display_timer = QTimer()
        self.time_display_timer.timeout.connect(self.update_time)
        self.time_display_timer.setInterval(1000)
        self.time_display_timer.start()

        self.load_settings()

    def update_time(self):
        """ Update the displayed current time and countdown clocks."""

        current_time_local = datetime.datetime.now()
        current_time_utc = current_time_local.astimezone(tz=datetime.timezone.utc)

        self.model.local_time = current_time_local
        self.model.utc_time = current_time_utc

        countdown_c1 = self.model.c1_info.time_utc - current_time_utc if self.model.c1_info else None
        countdown_c2 = self.model.c2_info.time_utc - current_time_utc if self.model.c2_info else None
        countdown_max = self.model.max_info.time_utc - current_time_utc if self.model.max_info else None
        countdown_c3 = self.model.c3_info.time_utc - current_time_utc if self.model.c3_info else None
        countdown_c4 = self.model.c4_info.time_utc - current_time_utc if self.model.c4_info else None
        countdown_sunrise = self.model.sunrise_info.time_utc - current_time_utc if self.model.sunrise_info else None
        countdown_sunset = self.model.sunset_info.time_utc - current_time_utc if self.model.sunset_info else None

        self.view.update_time(current_time_local, current_time_utc, countdown_c1, countdown_c2, countdown_max,
                              countdown_c3, countdown_c4, countdown_sunrise, countdown_sunset)

        self.update_jobs_countdown()

    def update_jobs_countdown(self):
        """ Update the countdown of the scheduled jobs. """

        if self.jobs_model:
            self.jobs_model.update_countdown()

        if self.scheduler:
            for job in self.scheduler.get_jobs():
                if job.next_run_time is None:
                    job.remove()

    def do(self, actions):
        pass

    def update(self, changed_object):
        """ Take action when a notification is received from an observable. """

        if isinstance(changed_object, LocationPopup):
            longitude = float(changed_object.longitude.text())
            latitude = float(changed_object.latitude.text())
            altitude = float(changed_object.altitude.text())

            self.model.set_position(longitude, latitude, altitude)

            self.view.longitude_label.setText(str(longitude))
            self.view.latitude_label.setText(str(latitude))
            self.view.altitude_label.setText(str(altitude))
            return

        elif isinstance(changed_object, EclipsePopup):
            eclipse_date = changed_object.eclipse_combobox.currentText()
            eclipse_date = eclipse_date[:11]
            self.model.set_eclipse_date(
                Time(datetime.datetime.strptime(eclipse_date, DATE_FORMATS[self.view.date_format])))

            self.view.eclipse_date.setText(changed_object.eclipse_combobox.currentText()[:11])
            return

        elif isinstance(changed_object, SimulatorPopup):
            self.sim_reference_moment = changed_object.reference_moment_combobox.currentText()
            self.sim_offset_minutes = (int(changed_object.offset_minutes.text())
                                       * BEFORE_AFTER[changed_object.before_after_combobox.currentText()])
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

        elif isinstance(changed_object, QCloseEvent):

            if self.model.camera_overview.camera_overview_dict:
                cameras = self.model.camera_overview.camera_overview_dict.values()

                camera: Camera
                for camera in cameras:
                    camera.exit()

            return

        text = changed_object.text()

        if text == "Location":
            self.location_popup = LocationPopup(self)
            self.location_popup.show()

        elif text == "Date":
            self.eclipse_popup = EclipsePopup(self)
            self.eclipse_popup.show()

        elif text == "Reference moments":
            if self.model.is_location_set and self.model.is_eclipse_date_set:
                reference_moments, magnitude, eclipse_type = self.model.get_reference_moments()
                self.view.show_reference_moments(reference_moments, magnitude, eclipse_type)

        elif text == "Camera(s)":
            logging.debug('User requested Camera(s) update')
            try:
                logging.debug('Calling model.camera_overview.update_camera_overview()')
                self.model.camera_overview.update_camera_overview()
                logging.debug('Returned from update_camera_overview()')
            except Exception:
                logging.exception('Exception while updating camera overview')

            try:
                logging.debug('Calling sync_camera_time()')
                self.sync_camera_time()
                logging.debug('Returned from sync_camera_time()')
            except Exception:
                logging.exception('Exception while syncing camera time')

            try:
                logging.debug('Calling check_camera_state()')
                self.check_camera_state()
                logging.debug('Returned from check_camera_state()')
            except Exception:
                logging.exception('Exception while checking camera state')

        elif text == "Simulator":
            self.simulator_popup = SimulatorPopup(self)
            self.simulator_popup.show()

        elif text == "File":
            filename, _ = QFileDialog.getOpenFileName(None, "QFileDialog.getOpenFileName()", "",
                                                      "All Files (*);;Python Files (*.py);;Text Files (*.txt)")

            if self.model.reference_moments and os.path.exists(filename):
                try:
                    from solareclipseworkbench.scheduling.engine import observe_solar_eclipse
                    cameras = self.model.camera_overview.camera_overview_dict or {}

                    for cam_name, cam_obj in cameras.items():
                        try:
                            get_battery_level(cam_obj)
                        except Exception:
                            LOGGER.warning('Camera "%s" failed health check — may be disconnected', cam_name)

                    self.scheduler, unmatched \
                        = observe_solar_eclipse(self.model.reference_moments, filename,
                                                cameras, self,
                                                self.sim_reference_moment, self.sim_offset_minutes)

                    if unmatched:
                        mapping = self._show_camera_mapping_dialog(unmatched, cameras.keys())
                        if mapping is None:
                            self.scheduler.shutdown(wait=False)
                            self.scheduler = None
                            return
                        mapped_cameras = {script_name: cameras[real_name]
                                          for script_name, real_name in mapping.items()}
                        all_cameras = {**cameras, **mapped_cameras}
                        self.scheduler.shutdown(wait=False)
                        self.scheduler, unmatched = observe_solar_eclipse(
                            self.model.reference_moments, filename,
                            all_cameras, self,
                            self.sim_reference_moment, self.sim_offset_minutes)

                    self.jobs_model = JobsTableModel(self.scheduler, self)
                    self.view.jobs_table.setModel(self.jobs_model)
                    self.jobs_model.add_observer(self.view.jobs_table)
                    self.view.jobs_table.resizeColumnsToContents()

                    from apscheduler.events import EVENT_JOB_ERROR
                    self.scheduler.add_listener(self._on_job_error, EVENT_JOB_ERROR)

                    self.view.camera_action.setDisabled(True)

                except IndexError:
                    LOGGER.warning(f"File {filename} does not contain scheduled jobs")

        elif text == "Stop":
            try:
                if self.scheduler:
                    self.scheduler.shutdown()
                    self.jobs_model.clear_jobs_overview()

                    self.view.camera_action.setEnabled(True)
            except SchedulerNotRunningError:
                pass

        elif text == "Datetime format":
            self.settings_popup = SettingsPopup(self)
            self.settings_popup.show()

        elif text == "Save":
            self.view.save_settings()

    def sync_camera_time(self):
        """ Set the time of all connected cameras to the time of the computer."""

        self.model.sync_camera_time()

    def check_camera_state(self):
        """ Check whether the focus mode and shooting mode of all connected cameras is set to 'Manual'. """

        self.model.check_camera_state()

    def _on_job_error(self, event):
        LOGGER.error('Scheduled job %s failed: %s', event.job_id, event.exception)
        if hasattr(self, 'jobs_model') and self.jobs_model:
            self.jobs_model.mark_job_failed(event.job_id, str(event.exception))

    def _show_camera_mapping_dialog(self, unmatched: set, detected_names) -> dict | None:
        """Show a dialog to map unmatched script camera names to detected cameras."""
        detected = list(detected_names)
        dialog = QDialog(self.view)
        dialog.setWindowTitle("Camera Name Mismatch")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("The script contains camera names not found among detected cameras.\n"
                                "Map each script name to a detected camera:"))
        form = QFormLayout()
        combos = {}
        for name in sorted(unmatched):
            combo = QComboBox()
            combo.addItems(detected)
            combos[name] = combo
            form.addRow(f"Script: {name}", combo)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None

        return {name: combo.currentText() for name, combo in combos.items()}

    def load_settings(self):
        """ Load the UI settings. """

        self.view.settings = QSettings(str(Path.home() / ".SolarEclipseWorkbench.ini"), QSettings.Format.IniFormat)

        default_date_format, *_ = DATE_FORMATS
        date_format = self.view.settings.value("date_format", default_date_format, type=str)
        default_time_format, *_ = TIME_FORMATS
        time_format = self.view.settings.value("time_format", default_time_format, type=str)
        self.set_datetime_format(date_format, time_format)

        is_location_loaded = self.set_location(self.view.settings.value("longitude", None, type=float),
                                               self.view.settings.value("latitude", None, type=float),
                                               self.view.settings.value("altitude", None, type=float))

        is_eclipse_date_loaded = self.set_eclipse_date(self.view.settings.value("eclipse_date", None, type=str),
                                                       date_format)

        if is_location_loaded and is_eclipse_date_loaded:
            try:
                self.set_reference_moments()
            except AttributeError:
                pass

    def set_datetime_format(self, date_format: str, time_format: str):
        """ Set the date and time format in the view. """

        self.view.date_format = date_format
        self.view.time_format = time_format

    def set_location(self, longitude: float, latitude: float, altitude: float) -> bool:
        """ Set the observing location in the model and the view. """

        if longitude and latitude and altitude:
            self.model.set_position(longitude, latitude, altitude)

            self.view.longitude_label.setText(str(longitude))
            self.view.latitude_label.setText(str(latitude))
            self.view.altitude_label.setText(str(altitude))

            return True

        return False

    def set_eclipse_date(self, eclipse_date: str, date_format: str = None) -> bool:
        """ Set the eclipse date in the model and the view. """

        if eclipse_date:
            if date_format:
                dt = datetime.datetime.strptime(eclipse_date, DATE_FORMATS[date_format])
                date = datetime.datetime.strftime(dt, "%Y-%m-%d")
                self.view.eclipse_date.setText(eclipse_date)
            else:
                date = datetime.datetime.strptime(eclipse_date, "%Y-%m-%d")
                self.view.eclipse_date.setText(date.strftime(DATE_FORMATS[self.view.date_format]))

            self.model.set_eclipse_date(Time(date))
            return True

        return False

    def set_reference_moments(self):
        """ Set the reference moments of the eclipse in the model and the view."""

        reference_moments, magnitude, eclipse_type = self.model.get_reference_moments()
        self.view.show_reference_moments(reference_moments, magnitude, eclipse_type)
