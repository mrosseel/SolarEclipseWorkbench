"""Application entry point for the Solar Eclipse Workbench GUI."""

import argparse
import logging
import os
import sys
import time

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from .qt_utils import apply_system_color_scheme
from .view import SolarEclipseView, ICON_PATH
from .model import SolarEclipseModel
from .controller import SolarEclipseController

LOGGER = logging.getLogger("Solar Eclipse Workbench UI")


def main():
    time_string = time.strftime("%Y%m%d-%H%M%S")
    logging.basicConfig(filename=f'{time_string}.log', level=logging.DEBUG, format='%(asctime)s %(message)s')
    # Also log to stdout so users see debug output in terminal
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.getLogger().addHandler(console_handler)
    LOGGER.info("Starting up Solar Eclipse Workbench")

    pkg_dir = os.path.dirname(os.path.dirname(__file__))
    missing = []
    if not os.path.exists(os.path.join(pkg_dir, 'eclipse', 'eclipse_besselian.csv')):
        missing.append('eclipse/eclipse_besselian.csv (expected in package directory)')
    if not os.path.exists('de440s.bsp'):
        missing.append('de440s.bsp (expected in working directory)')
    if missing:
        LOGGER.warning('Missing data files: %s', ', '.join(missing))

    parser = argparse.ArgumentParser(description="Solar Eclipse Workbench")
    parser.add_argument(
        "-s",
        "--sim",
        help="Start up in simulator mode",
        default=False,
        action='store_true'
    )
    parser.add_argument(
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
        help="altitude of the location where to watch the solar eclipse (in meters)",
        default=False,
        type=float
    )

    parser.add_argument(
        "-d",
        "--date",
        help="date of the solar eclipse (in YYYY-MM-DD format)",
        default=False,
    )

    args = parser.parse_args()

    app = QApplication(list(sys.argv))
    apply_system_color_scheme(app)
    app.setWindowIcon(QIcon(str(ICON_PATH / "logo-small.svg")))
    app.setApplicationName("Solar Eclipse Workbench")

    model = SolarEclipseModel()
    view = SolarEclipseView(is_simulator=args.sim)
    # Attach virtual camera defaults to the view so other parts can query them
    view.virtual_camera_enabled = args.virtual_camera

    controller = SolarEclipseController(model, view, is_simulator=args.sim)

    # Make the view available to the camera overview model so it can read simulator flags
    model.camera_overview.view = view

    if args.longitude and args.latitude and args.altitude:
        controller.set_location(args.longitude, args.latitude, args.altitude)

    if args.date:
        controller.set_eclipse_date(args.date, date_format=None)

    if args.longitude and args.latitude and args.altitude and args.date:
        controller.set_reference_moments()

    view.show()

    return app.exec()
