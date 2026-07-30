"""Registry of non-camera hardware available to scheduled script commands.

Lives in its own module so both the GUI and the scheduler (utils) can import it
without creating an import cycle: utils imports the GUI, so the GUI cannot
import utils at module level.
"""

import logging

# Devices opened by the GUI or the CLI, looked up when a command is scheduled.
HARDWARE: dict = {}


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
