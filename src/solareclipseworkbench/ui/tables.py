"""Table models and views for the Solar Eclipse Workbench UI."""

import datetime
import logging
import threading
from enum import Enum
from typing import Union

import pandas as pd
import pytz
from PyQt6.QtCore import Qt, QAbstractTableModel, QModelIndex, QTimer, pyqtSignal
from PyQt6.QtWidgets import QTableView
from apscheduler.job import Job
from apscheduler.schedulers.background import BackgroundScheduler
from timezonefinder import TimezoneFinder

from solareclipseworkbench.camera import (
    get_camera_dict, get_battery_level, get_free_space, get_space, CameraSettings,
)
from solareclipseworkbench.observer import Observable
from .helpers import format_countdown, format_time


class CameraOverviewTableColumnNames(Enum):
    """ Enumeration of the column names for the table with the camera overview table. """

    CAMERA = "Camera name"
    BATTERY_LEVEL = "Battery level [%]"
    FREE_MEMORY_GB = "Free memory [GB]"
    FREE_MEMORY_PERCENTAGE = "Free memory [%]"


class CameraOverviewTableModel(QAbstractTableModel):

    def __init__(self):
        """ Initialisation of the model for the table with the camera overview. """

        super().__init__()

        self.camera_overview_dict: Union[dict, None] = None

        self._data = pd.DataFrame(columns=[CameraOverviewTableColumnNames.CAMERA.value,
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
            print('CameraOverview: scheduling worker to probe cameras', flush=True)
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
        try:
            is_sim = getattr(self.view, 'is_simulator', False) and getattr(self.view, 'virtual_camera_enabled', False)
            vc_fps = getattr(self.view, 'virtual_camera_fps', 1)
            # Reuse existing camera objects if available to avoid opening a new USB
            # connection while a previous connection (e.g. from take_picture) is still held.
            existing_map = getattr(self, 'camera_overview_dict', None)
            if existing_map and all(v is not None for v in existing_map.values()):
                camera_dict = existing_map
                logging.debug('CameraOverview: reusing %d existing camera object(s)', len(camera_dict))
            else:
                camera_dict = get_camera_dict(is_simulator=is_sim)

            data = []
            for camera_name, camera in camera_dict.items():
                try:
                    logging.debug('Worker: processing camera %s', camera_name)
                    battery_level = get_battery_level(camera).rstrip('%')
                    free_space_gb = get_free_space(camera)
                    total_space = get_space(camera)
                    free_space_percentage = int(free_space_gb / total_space * 100)
                    data.append([camera_name, str(battery_level), str(free_space_gb), str(free_space_percentage)])
                except Exception:
                    logging.exception('Worker: exception while processing camera %s', camera_name)
                    continue
            # schedule UI update on main thread
            try:
                print('Worker: gathered camera overview data:', data, flush=True)
            except Exception:
                pass
            # write pending data and the camera objects for the main thread poll to pick up
            try:
                self._pending_data = data
                # keep the mapping of camera name -> camera object for later actions
                self._pending_camera_map = camera_dict
            except Exception:
                logging.exception('Worker: could not set pending data')
        except Exception:
            logging.exception('Worker: failed to gather camera info')

    def _on_data_ready(self, data):
        try:
            print('CameraOverview: on_data_ready called with', data, flush=True)
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

        self.beginResetModel()
        self._data = pd.DataFrame(data, columns=self._data.columns)
        self.endResetModel()

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
                print('CameraOverview: view updated', flush=True)
        except Exception:
            logging.exception('Could not update camera overview view after data ready')

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
    STATUS = "Status"


class JobsTableModel(QAbstractTableModel, Observable):
    def __init__(self, scheduler: BackgroundScheduler, controller):
        """ Initialisation of the model for the table with the scheduled jobs.

        Args:
            - scheduler: Background scheduler
            - controller: SolarEclipseController instance
        """

        super().__init__()
        self.controller = controller
        self.time_format = self.controller.view.time_format

        tf = TimezoneFinder()
        timezone = pytz.timezone(
            tf.timezone_at(lng=self.controller.model.longitude, lat=self.controller.model.latitude))

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

                data.append([countdown, formatted_execution_time_local, formatted_execution_time_utc, job_string,
                             description, "Pending"])

        self._data = pd.DataFrame(data, columns=[JobsTableColumnNames.COUNTDOWN.value,
                                                 JobsTableColumnNames.EXEC_TIME_LOCAL.value,
                                                 JobsTableColumnNames.EXEC_TIME_UTC.value,
                                                 JobsTableColumnNames.COMMAND.value,
                                                 JobsTableColumnNames.DESCRIPTION.value,
                                                 JobsTableColumnNames.STATUS.value])

        self._job_id_to_row = {}
        for idx, job in enumerate(scheduler.get_jobs()):
            if job.next_run_time:
                self._job_id_to_row[job.id] = idx

    def mark_job_failed(self, job_id: str, error_msg: str):
        row = self._job_id_to_row.get(job_id)
        if row is not None:
            self.beginResetModel()
            self._data.loc[row, JobsTableColumnNames.STATUS.value] = f"FAILED: {error_msg}"
            self.endResetModel()

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
                        self._data.loc[row, JobsTableColumnNames.STATUS.value] = "Done"
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
