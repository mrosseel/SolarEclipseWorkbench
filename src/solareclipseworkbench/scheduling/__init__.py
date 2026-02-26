"""Scheduling subpackage — bridges camera + eclipse + notifications."""

from .sync import sync_cameras
from .engine import (
    COMMANDS,
    calculate_next_solar_eclipses,
    observe_solar_eclipse,
    start_scheduler,
    schedule_commands,
    schedule_command,
)
from .scripts import (
    convert_command,
    convert_script,
    display1_10th_second,
)
