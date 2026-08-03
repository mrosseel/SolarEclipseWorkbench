import logging
import csv
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
import pytz
from solareclipseworkbench import voice_prompt, take_picture, take_burst, take_bracket, take_hdr, \
    sync_cameras, scripts, execute_command
from solareclipseworkbench import relay_shoot, relay_burst, relay_bulb
from solareclipseworkbench.relay_trigger import relay_arm, relay_release
from solareclipseworkbench import mount_track_sun, mount_goto_sun, mount_tracking, mount_park, mount_unpark, mount_stop
from solareclipseworkbench import hardware_problems
from solareclipseworkbench.camera import CameraSettings
from solareclipseworkbench.notifications import check_notification
from solareclipseworkbench.gui import SolarEclipseController
# The registry lives in its own module so the GUI can reach it without
# importing this one (which imports the GUI).  Re-exported for existing callers.
from solareclipseworkbench.hardware_registry import HARDWARE, HARDWARE_COMMANDS, register_hardware
from solareclipseworkbench.solar_eclipse import get_solar_eclipses

COMMANDS = {
    'voice_prompt': voice_prompt,
    'take_picture': take_picture,
    'take_burst': take_burst,
    'take_bracket': take_bracket,
    'take_hdr': take_hdr,
    'sync_cameras': sync_cameras,
    'command': execute_command,
    'relay_shoot': relay_shoot,
    'relay_burst': relay_burst,
    'relay_bulb': relay_bulb,
    'relay_arm': relay_arm,
    'relay_release': relay_release,
    'mount_track_sun': mount_track_sun,
    'mount_goto_sun': mount_goto_sun,
    'mount_tracking': mount_tracking,
    'mount_park': mount_park,
    'mount_unpark': mount_unpark,
    'mount_stop': mount_stop,
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
                          minutes_to_reference_moment: float,
                          gps_time_offset: timedelta = timedelta(0)) -> BackgroundScheduler:
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
        - gps_time_offset: Offset between GPS UTC time and computer system time (GPS − computer).
                           When the GPS is ahead of the computer by D seconds the computer fires D seconds
                           late; subtracting this offset from every scheduled time compensates for the drift.
                           Defaults to timedelta(0) (use computer clock as-is).

    Returns: (scheduler, unknown), the scheduler used to schedule the commands and the reference moments
             the script asked for that do not exist, as {name: number of lines lost}.
    """

    scheduler = start_scheduler()

    # Calculate simulated time
    if reference_moment:
        now = datetime.now(pytz.utc)
        simulated_start = now + timedelta(minutes=minutes_to_reference_moment)

        offset = ref_moments[reference_moment].time_utc - timedelta(minutes=minutes_to_reference_moment) - now
    else:
        simulated_start = None
        offset = timedelta(minutes=0)

    # Update the visualization offset only when running with a GUI; the headless
    # command-line path (sew.py without --gui) has no controller/view.
    if controller is not None:
        controller.view.eclipse_visualization.set_offset(offset)

    # Schedule commands
    unknown = schedule_commands(commands_filename, scheduler, ref_moments, cameras, controller, reference_moment,
                                simulated_start, gps_time_offset=gps_time_offset)

    return scheduler, unknown


def start_scheduler():
    """ Start background scheduler and return it.

    Returns: Background scheduler that has been started.
    """

    # Use the default misfire_grace_time (1 s).  Timing accuracy is enforced
    # inside _serialised_on_camera: if the USB lock is busy for more than
    # _MAX_LOCK_WAIT_S the shot is dropped rather than taken late.
    scheduler = BackgroundScheduler()
    scheduler.start()

    return scheduler


def schedule_commands(filename: str, scheduler: BackgroundScheduler, reference_moments: dict,
                      cameras: dict, controller: SolarEclipseController, reference_moment, simulated_start: datetime,
                      gps_time_offset: timedelta = timedelta(0)):
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
        - gps_time_offset: GPS–computer time offset (see observe_solar_eclipse).  Defaults to timedelta(0).

    Returns: Reference moments the script asked for that do not exist, as {name: number of lines lost}.
             Empty when every line found its moment.
    """
    script_file = scripts.convert_script(filename, reference_moments)
    script_file.seek(0)

    unknown: dict = {}

    # Loop over all lines in script file
    for cmd_str in script_file:
        missing = schedule_command(
            scheduler, reference_moments, cmd_str, cameras, controller, reference_moment, simulated_start,
            gps_time_offset=gps_time_offset)
        if missing is not None:
            unknown[missing] = unknown.get(missing, 0) + 1

    return unknown


def schedule_command(scheduler: BackgroundScheduler, reference_moments: dict, cmd_str: str, cameras: dict,
                     controller: SolarEclipseController, reference_moment_for_simulation: str,
                     simulated_start: datetime,
                     gps_time_offset: timedelta = timedelta(0)):
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
        - gps_time_offset: GPS–computer time offset (GPS UTC − computer UTC).  When positive the computer
                            is slow; execution times are shifted earlier by this amount so that actions
                            fire at the correct GPS-referenced wall-clock time.  Defaults to timedelta(0).
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

    if func_name in HARDWARE_COMMANDS:
        # Relay and mount commands take their device as the first argument, the
        # way camera commands take a camera.
        kind = HARDWARE_COMMANDS[func_name]
        device = HARDWARE.get(kind)
        if device is None:
            logging.warning(
                'schedule_command: no %s is connected, so "%s" will be skipped.  '
                'Connect the %s before the eclipse starts, or remove the command from the script.',
                kind, func_name, kind,
            )
            return
        args = [device] + [arg.strip() for arg in args if arg.strip()]
    elif func_name == "voice_prompt":
        # Resolve the prompt now rather than when the job fires: a typo would
        # otherwise raise mid-eclipse and the prompt would simply not be heard.
        problem = check_notification(args[0] if args else "")
        if problem is not None:
            hardware_problems.report(
                "Script", f"{problem}.  This prompt will not be played.",
                detail=cmd_str.strip(),
            )
            return
    elif func_name != "command":
        if cameras is not None:
            try:
                if func_name == "take_picture":
                    settings = CameraSettings(args[0].strip(), args[1].strip(), args[2].strip(), int(args[3].strip()))
                    new_args = [cameras[args[0].strip()], settings]
                    args = new_args
                elif func_name == "take_burst":
                    settings = CameraSettings(args[0].strip(), args[1].strip(), args[2].strip(), int(args[3].strip()))
                    new_args = [cameras[args[0].strip()], settings, float(args[4].strip())]
                    args = new_args
                elif func_name == "take_bracket":
                    settings = CameraSettings(args[0].strip(), args[1].strip(), args[2].strip(), int(args[3].strip()))
                    new_args = [cameras[args[0].strip()], settings, str(args[4].strip())]
                    args = new_args
                elif func_name == "take_hdr":
                    settings = CameraSettings(args[0].strip(), args[1].strip(), args[2].strip(), int(args[3].strip()))
                    new_args = [cameras[args[0].strip()], settings, int(args[4].strip())]
                    args = new_args
                elif func_name == "sync_cameras":
                    args = [controller]
            except KeyError:
                camera_name_in_script = args[0].strip() if args else '(unknown)'
                available = list(cameras.keys()) if cameras else []
                logging.warning(
                    'schedule_command: camera "%s" not found in camera dict.  '
                    'Available cameras: %s.  '
                    'Check that the camera name in the script exactly matches the '
                    'name shown in the Camera(s) overview (or the alias you configured '
                    'in the wizard).  This command will be skipped.',
                    camera_name_in_script, available,
                )
                return
        else:
            # No camera dict at all: the script was loaded before any camera was
            # detected.  Every camera command in the file is dropped here, so say
            # so per command rather than leaving a script that looks loaded but
            # only ever plays its voice prompts.
            logging.warning(
                'schedule_command: no cameras have been detected, so "%s" will be '
                'skipped.  Detect the camera(s) first, then load the script: the '
                'commands are bound to a camera when they are scheduled, not when '
                'they run.',
                func_name,
            )
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

        # Compensate for the GPS–computer time offset.
        # If GPS time is ahead of the computer (offset > 0), the computer is slow
        # and would fire the shutter late.  Scheduling earlier on the computer clock
        # by subtracting the offset ensures the action happens at the correct moment.
        execution_time = execution_time - gps_time_offset

        trigger = DateTrigger(run_date=execution_time, timezone=pytz.utc)

        scheduler.add_job(func, trigger=trigger, args=args, name=description)
    except KeyError as missing:
        # A line naming a moment the calculation did not produce.  Usually a
        # limb-corrected moment — BEADS_C2 and friends only exist when the
        # correction is on and the limb profile is installed — or a typo.  The
        # line cannot be scheduled, but it must not disappear without a word:
        # silently dropping the contact bursts is exactly the failure nobody
        # notices until the eclipse is over.  The name goes back to the caller,
        # which is holding the user at the moment the script is loaded.
        name = missing.args[0] if missing.args else str(missing)
        logging.warning(
            'schedule_command: no reference moment %s, so "%s" (%s) is not scheduled — '
            'the rest of the script is unaffected', name, func_name, description)
        return name
    except Exception:
        # One bad line must never take the application down: PyQt6 turns an
        # unhandled exception in the load handler into a hard abort, which on
        # eclipse morning would kill every OTHER scheduled moment too.  Log it,
        # skip the line, keep the rest of the eclipse.
        logging.exception(
            'schedule_command: could not schedule "%s" (%s) — line skipped, '
            'the rest of the script is unaffected', func_name, description)
        return
    except Exception:
        # One bad line must never take the application down: PyQt6 turns an
        # unhandled exception in the load handler into a hard abort, which on
        # eclipse morning would kill every OTHER scheduled moment too.  Log it,
        # skip the line, keep the rest of the eclipse.
        logging.exception(
            'schedule_command: could not schedule "%s" (%s) — line skipped, '
            'the rest of the script is unaffected', func_name, description)
        return

# Main
def main():
    """ Main function to test the utility functions. """
    # Example usage of calculate_next_solar_eclipses
    eclipses = calculate_next_solar_eclipses(5)
    print("Next Solar Eclipses:", eclipses)

    # Example usage of observe_solar_eclipse
    # This would require actual reference moments and cameras to work properly
    # observe_solar_eclipse({}, "commands.txt", {}, SolarEclipseController(), "C1", 10)

if __name__ == "__main__":
    main()
