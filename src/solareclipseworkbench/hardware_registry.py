"""Registry of non-camera hardware available to scheduled script commands.

Lives in its own module so both the GUI and the scheduler (utils) can import it
without creating an import cycle: utils imports the GUI, so the GUI cannot
import utils at module level.
"""

import logging

# Devices opened by the GUI or the CLI, looked up when a command is scheduled.
HARDWARE: dict = {}

# Script commands that act on a piece of hardware rather than a camera, mapped to
# the kind of device they need.  Lives here rather than in utils so that scripts
# can consult it too without importing utils, which would be a cycle.
HARDWARE_COMMANDS = {
    'relay_shoot': 'relay',
    'relay_burst': 'relay',
    'relay_bulb': 'relay',
    'relay_arm': 'relay',
    'relay_release': 'relay',
    'mount_track_sun': 'mount',
    'mount_goto_sun': 'mount',
    'mount_tracking': 'mount',
    'mount_park': 'mount',
    'mount_unpark': 'mount',
    'mount_stop': 'mount',
}


# Which script command each scheduled job runs, by job id.  Filled in as the
# script is scheduled.  Live view needs this: it may run while a script is
# loaded, but not across a frame, and "is a job due" is the wrong question -
# most of the jobs around second contact are voice prompts, which touch
# nothing.  Refusing for those is what stopped a focus check in the last minute
# before totality, which is the one minute it is most needed.
JOB_COMMANDS: dict = {}

#: Commands that leave the camera alone, so live view may run across them.
CAMERA_FREE_COMMANDS = frozenset(
    {'voice_prompt'}
    | {name for name, kind in HARDWARE_COMMANDS.items() if kind == 'mount'}
)


def note_job_command(job_id: str, command: str) -> None:
    """Record which command a scheduled job will run."""
    JOB_COMMANDS[job_id] = command


def job_touches_camera(job) -> bool:
    """Whether this job will use the camera or fire the shutter.

    Unknown jobs count as touching it: an unrecognised command is not a reason
    to let a preview run across a frame.
    """
    return JOB_COMMANDS.get(getattr(job, 'id', None), '') not in CAMERA_FREE_COMMANDS


def seconds_to_next_camera_job(scheduler, now=None) -> float | None:
    """Seconds until the next job that needs the camera, or None if there is none."""
    if scheduler is None:
        return None
    import datetime
    soonest = None
    for job in scheduler.get_jobs():
        when = getattr(job, 'next_run_time', None)
        if when is None or not job_touches_camera(job):
            continue
        reference = now or datetime.datetime.now(when.tzinfo)
        gap = (when - reference).total_seconds()
        if gap >= 0 and (soonest is None or gap < soonest):
            soonest = gap
    return soonest


def register_hardware(kind: str, device) -> None:
    """Make a relay trigger or mount available to scheduled commands.

    Pass None to unregister, so a disconnected device does not leave scheduled
    commands pointing at a dead handle.
    """
    if device is None:
        HARDWARE.pop(kind, None)
        logging.info("Unregistered %s", kind)
    else:
        HARDWARE[kind] = device
        logging.info("Registered %s: %s", kind, getattr(device, 'describe', lambda: device)())


def get_hardware(kind: str):
    """The registered device of the given kind, or None."""
    return HARDWARE.get(kind)
