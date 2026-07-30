"""Serial port enumeration, with the platform differences handled once.

macOS exposes each USB serial adapter twice: as ``/dev/tty.usbserial-X`` and as
``/dev/cu.usbserial-X``.  They are not interchangeable.  Opening the ``tty``
node blocks until it sees carrier detect, which a relay board or a mount
controller never asserts, so the open hangs or times out.  The ``cu`` node
("call-up") does not wait, and is the one to use for anything that is not a
modem.

Enumerating both would also make every device appear twice in the bench
console's scan, and half of those entries would hang if selected.
"""

import logging
import sys

import serial.tools.list_ports

logger = logging.getLogger(__name__)


def usb_serial_ports() -> list:
    """USB serial ports worth trying, as pyserial ListPortInfo objects.

    Ports with no USB vendor ID are dropped: on macOS those are the built-in
    Bluetooth serial devices, which are never what we are looking for.
    """
    ports = [p for p in serial.tools.list_ports.comports() if p.vid is not None]

    if sys.platform == "darwin":
        # Prefer the call-up node, and drop the matching tty node so the same
        # adapter is not offered twice.
        callout = [p for p in ports if "/cu." in p.device]
        if callout:
            paired = {p.device.replace("/cu.", "/tty.") for p in callout}
            ports = callout + [p for p in ports if p.device not in paired
                               and "/cu." not in p.device]
            logger.debug("macOS: using call-up nodes %s", [p.device for p in callout])

    return ports
