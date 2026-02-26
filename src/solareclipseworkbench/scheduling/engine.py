from __future__ import annotations

import logging
import csv
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from solareclipseworkbench.notifications import voice_prompt
from solareclipseworkbench.camera import take_picture, take_burst, take_bracket, CameraSettings
from solareclipseworkbench.commands import execute_command
from solareclipseworkbench.scheduling.sync import sync_cameras
from solareclipseworkbench.scheduling import scripts
from solareclipseworkbench.eclipse.solar_eclipse import get_solar_eclipses

if TYPE_CHECKING:
    from solareclipseworkbench.gui import SolarEclipseController

COMMANDS = {
    'voice_prompt': voice_prompt,
    'take_picture': take_picture,
    'take_burst': take_burst,
    'take_bracket': take_bracket,
    'sync_cameras': sync_cameras,
    'command': execute_command
}

def calculate_next_solar_eclipses(count: int) -> list:
    """ Calculate the next solar eclipses, starting from today.

    Args:
        - count: Number of solar eclipses to calculate

    Returns:
        - List of solar eclipses, starting from today, as an array in the DD/MM/YYYY format
    """
    # Get current date
    from datetime import datetime, timedelta
    current_date = datetime.now()
    current_date = current_date - timedelta(days=3)  # Start from 3 days ago to ensure we catch today's eclipse
    current_date = current_date.strftime("%Y-%m-%d")  # Format as YYYY-MM-DD

    return get_solar_eclipses(count, current_date)


def observe_solar_eclipse(ref_moments: dict, commands_filename: str, cameras: dict,
                          controller: SolarEclipseController, reference_moment: str,
                          minutes_to_reference_moment: float) -> tuple:
    """ Observe (and photograph) the solar eclipse, as per given files.

    Args:
        - ref_moments: ReferenceMomentInfo that specifies the timing of the reference moments (C1,..., C4, and
                                maximum eclipse)
        - commands_filename: Name of the configuration file that specifies which commands have to be executed at which
                             moment during the solar eclipse
        - cameras: Dictionary of camera names and camera objects
        - controller: Controller of the Solar Eclipse Workbench UI
        - reference_moment: Reference moment to use for the simulation.  Possible values are C1, C2, C3, C4, sunrise,
                            sunset, and MAX.  None if no simulation should be used
        - minutes_to_reference_moment: Minutes to reference moment when simulating, None if no simulation should be used

    Returns: Tuple of (scheduler, unmatched_camera_names) where unmatched_camera_names is a set
             of camera names found in the script but not in the cameras dict.
    """

    scheduler = start_scheduler()

    # Calculate simulated time
    if reference_moment:
        simulated_start = datetime.now(pytz.utc) + timedelta(minutes=minutes_to_reference_moment)
    else:
        simulated_start = None

    # Schedule commands
    unmatched = schedule_commands(commands_filename, scheduler, ref_moments, cameras, controller, reference_moment, simulated_start)

    return scheduler, unmatched


def start_scheduler():
    """ Start background scheduler and return it.

    Returns: Background scheduler that has been started.
    """

    scheduler = BackgroundScheduler()
    scheduler.start()

    return scheduler


def schedule_commands(filename: str, scheduler: BackgroundScheduler, reference_moments: dict,
                      cameras: dict, controller: SolarEclipseController, reference_moment, simulated_start: datetime) -> set:
    """ Schedule commands as specified in the given file.

    Args:
        - filename: Name of the file in which the commands have been listed, scheduled relatively to the given
                    reference moments
        - scheduler: Background scheduler to use to schedule the commands
        - reference_moments: Dictionary with the reference moments (1st - 4th contact and maximum eclipse), with
                             respect to which the commands are scheduled
        - cameras: Dictionary of camera names and camera objects
        - controller: Controller of the Solar Eclipse Workbench UI
        - reference_moment: Reference moment to use for the simulation.  Possible values are C1, C2, C3, C4, sunrise,
                            sunset, LAST and MAX. None if no simulation should be used.
        - simulated_start: datetime with the time to simulate relative to the reference moment.
                            None if no simulation is to be used.

    Returns: Set of camera names found in the script but not in the cameras dict.
    """
    unmatched = set()
    script_file = scripts.convert_script(filename, reference_moments)
    script_file.seek(0)

    # Loop over all lines in script file
    for cmd_str in script_file:
        missed = schedule_command(
            scheduler, reference_moments, cmd_str, cameras, controller, reference_moment, simulated_start)
        if missed:
            unmatched.add(missed)

    return unmatched


def schedule_command(scheduler: BackgroundScheduler, reference_moments: dict, cmd_str: str, cameras: dict,
                     controller: SolarEclipseController, reference_moment_for_simulation: str,
                     simulated_start: datetime):
    """ Schedule the given command with the given scheduler and reference moments.

    Args:
        - scheduler: Background scheduler to use to schedule the command
        - reference_moments: Dictionary with the reference moments of the solar eclipse, as ReferenceMomentInfo objects.
        - cmd_str: Command string
        - cameras: Dictionary of camera names and camera objects
        - controller: Controller of the Solar Eclipse Workbench UI
        - reference_moment_for_simulation: Reference moment to use for the simulation.  Possible values are C1, C2, C3,
                            C4, sunrise, sunset, LAST and MAX. None if no simulation should be used.
        - simulated_start: datetime with the time to simulate relative to the reference moment.
                            None if no simulation is to be used.
    """
    # Use CSV reader to properly handle quoted fields with commas
    try:
        cmd_str_split = next(csv.reader([cmd_str], skipinitialspace=True))
    except StopIteration:
        logging.error(f"Could not parse command: {cmd_str}")
        return

    func_name = cmd_str_split[0].strip()
    ref_moment = cmd_str_split[1].strip()

    if ref_moment.upper() == "SUNRISE":
        ref_moment = "sunrise"

    if ref_moment.upper() == "SUNSET":
        ref_moment = "sunset"

    sign = cmd_str_split[2].strip()    # + or -
    hours, minutes, seconds = cmd_str_split[3].strip().split(":")   # hh:mm:ss.ss
    description = cmd_str_split[-1].strip()

    logging.info(f"Scheduling {func_name} at {ref_moment}{sign}{cmd_str_split[3].strip()}")

    args = cmd_str_split[4:-1]

    if func_name != "voice_prompt" and func_name != "command":
        if cameras is not None:
            if func_name == "sync_cameras":
                args = [controller]
            else:
                cam_name = args[0].strip()
                camera = cameras.get(cam_name)
                if camera is None:
                    logging.warning('Skipping %s: camera "%s" not found (available: %s)',
                                    func_name, cam_name, list(cameras.keys()))
                    return cam_name
                if func_name == "take_picture":
                    settings = CameraSettings(cam_name, args[1].strip(), args[2].strip(), int(args[3].strip()))
                    args = [camera, settings]
                elif func_name == "take_burst":
                    settings = CameraSettings(cam_name, args[1].strip(), args[2].strip(), int(args[3].strip()))
                    args = [camera, settings, float(args[4].strip())]
                elif func_name == "take_bracket":
                    settings = CameraSettings(cam_name, args[1].strip(), args[2].strip(), int(args[3].strip()))
                    args = [camera, settings, str(args[4].strip())]
        else:
            return

    func = COMMANDS[func_name]


    try:
        if ref_moment == "LAST":
            try:
                # Get last job from scheduler
                last_job = scheduler.get_jobs()[-1]

                # Get the last job's time
                reference_moment = last_job.next_run_time.astimezone(pytz.utc)
            except AttributeError:
                logging.error("No jobs found in the scheduler. Cannot determine LAST reference moment.")
                return
        else:
            reference_moment = reference_moments[ref_moment].time_utc


        delta = timedelta(hours=float(hours), minutes=float(minutes), seconds=float(seconds))

        if sign == "+":
            execution_time = reference_moment + delta
        else:
            execution_time = reference_moment - delta

        if reference_moment_for_simulation:
            diff = reference_moments[reference_moment_for_simulation.upper()].time_utc - simulated_start
            execution_time = execution_time - diff

        trigger = CronTrigger(year=execution_time.year, month=execution_time.month, day=execution_time.day,
                              hour=execution_time.hour, minute=execution_time.minute,
                              second=execution_time.second, timezone=pytz.utc)

        scheduler.add_job(func, trigger=trigger, args=args, name=description)
    except KeyError as e:
        logging.warning('Skipping command "%s": missing key %s (ref_moment=%s)',
                        description, e, ref_moment)
        return
