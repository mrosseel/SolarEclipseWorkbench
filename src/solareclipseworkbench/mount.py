"""OnStepX mount control over the LX200 protocol.

Written against an MLAstro SAL-33 running OnStepX, connected by USB cable.  The
same interface works over TCP for the WiFi builds, but the cable is the primary
path: it does not drop, and it does not need the mount's access point to be up.

Protocol shape, from the OnStepX command reference:

    - A command frame is ``:CC...#``.
    - Replies come in three shapes, and which one you get depends on the command:
      a payload terminated with ``#``, a single ``0``/``1`` boolean with no
      terminator, or nothing at all.
    - There is no way to tell these apart from the reply stream, so every command
      here declares the shape it expects.  Guessing desynchronises the link.

Coordinates use the highest-precision variants (``:GRH#``, ``:GDH#``) so the
reply format does not depend on the controller's precision mode.
"""

import logging
import re
import socket
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

import serial
import serial.tools.list_ports
from skyfield.api import load

logger = logging.getLogger(__name__)

DEFAULT_BAUDRATE = 9600
# Baud rates worth probing when the configured one gets no answer.  OnStepX
# builds vary, and the SAL-33's USB interface is whatever its firmware was built
# with rather than a documented constant.
CANDIDATE_BAUDRATES = [9600, 19200, 38400, 57600, 115200]

DEFAULT_TCP_PORT = 9999

# A goto can run for a long time; a status query should never block that long.
DEFAULT_TIMEOUT_S = 2.0
SLEW_POLL_INTERVAL_S = 0.5

GOTO_ERRORS = {
    "0": "goto accepted",
    "1": "below horizon limit",
    "2": "above overhead limit",
    "3": "controller in standby",
    "4": "mount parked",
    "5": "goto already in progress",
    "6": "outside limits",
    "7": "hardware fault",
    "8": "already in motion",
    "9": "unspecified error",
}


class MountError(Exception):
    """Raised when the mount cannot be reached, or rejects a command."""


class ReplyKind(Enum):
    """What a command sends back."""

    NONE = "none"
    BOOL = "bool"            # a single '0' or '1', no terminator
    CHAR = "char"            # a single character, no terminator (goto results)
    TERMINATED = "terminated"  # payload ending in '#'


# --------------------------------------------------------------------- transport


class Transport(ABC):
    """A byte pipe to the controller."""

    @abstractmethod
    def write(self, data: bytes) -> None: ...

    @abstractmethod
    def read(self, size: int, timeout: float) -> bytes: ...

    @abstractmethod
    def reset_input(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def describe(self) -> str: ...


class SerialTransport(Transport):
    """USB cable to the controller."""

    def __init__(self, port: str, baudrate: int = DEFAULT_BAUDRATE, timeout: float = DEFAULT_TIMEOUT_S):
        self.port = port
        self.baudrate = baudrate
        try:
            self._serial = serial.Serial(port, baudrate, timeout=timeout)
        except serial.SerialException as exc:
            raise MountError(f"cannot open mount on {port}: {exc}") from exc
        # Controllers commonly emit a banner or reset when the port opens.
        time.sleep(0.2)
        self.reset_input()

    def write(self, data: bytes) -> None:
        try:
            self._serial.write(data)
            self._serial.flush()
        except serial.SerialException as exc:
            raise MountError(f"write failed on {self.port}: {exc}") from exc

    def read(self, size: int, timeout: float) -> bytes:
        self._serial.timeout = timeout
        try:
            return self._serial.read(size)
        except serial.SerialException as exc:
            raise MountError(f"read failed on {self.port}: {exc}") from exc

    def reset_input(self) -> None:
        try:
            self._serial.reset_input_buffer()
        except Exception:
            logger.debug("Could not reset serial input buffer", exc_info=True)

    def close(self) -> None:
        try:
            self._serial.close()
        except Exception:
            logger.debug("Error closing mount serial port", exc_info=True)

    def describe(self) -> str:
        return f"serial {self.port} @ {self.baudrate}"


class TcpTransport(Transport):
    """WiFi or Ethernet build of OnStepX."""

    def __init__(self, host: str, port: int = DEFAULT_TCP_PORT, timeout: float = DEFAULT_TIMEOUT_S):
        self.host = host
        self.port = port
        try:
            self._socket = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise MountError(f"cannot connect to mount at {host}:{port}: {exc}") from exc

    def write(self, data: bytes) -> None:
        try:
            self._socket.sendall(data)
        except OSError as exc:
            raise MountError(f"write failed to {self.host}: {exc}") from exc

    def read(self, size: int, timeout: float) -> bytes:
        self._socket.settimeout(timeout)
        try:
            return self._socket.recv(size)
        except socket.timeout:
            return b""
        except OSError as exc:
            raise MountError(f"read failed from {self.host}: {exc}") from exc

    def reset_input(self) -> None:
        self._socket.settimeout(0.05)
        try:
            while self._socket.recv(4096):
                pass
        except OSError:
            pass

    def close(self) -> None:
        try:
            self._socket.close()
        except Exception:
            logger.debug("Error closing mount socket", exc_info=True)

    def describe(self) -> str:
        return f"tcp {self.host}:{self.port}"


class SimulatedTransport(Transport):
    """Answers enough of the protocol to rehearse a script without the mount."""

    def __init__(self):
        self._pending = b""
        self.tracking = False
        self.parked = False
        self.slewing = False
        self.ra_hours = 0.0
        self.dec_degrees = 0.0
        self.target: Tuple[Optional[float], Optional[float]] = (None, None)
        self.rate = "solar"

    def write(self, data: bytes) -> None:
        command = data.decode("ascii", errors="replace")
        self._pending += self._respond(command).encode("ascii")

    def _respond(self, command: str) -> str:
        if command == ":GVP#":
            return "OnStepX (simulated)#"
        if command == ":GVN#":
            return "10.24#"
        if command == ":GRH#":
            return _format_ra_highp(self.ra_hours)
        if command == ":GDH#":
            return _format_dec_highp(self.dec_degrees)
        if command == ":GU#":
            flags = ""
            flags += "n" if not self.tracking else ""
            flags += "N" if not self.slewing else ""
            flags += "P" if self.parked else "p"
            flags += "O" if self.rate == "solar" else ""
            return flags + "#"
        if command == ":Te#":
            self.tracking = True
            return "1"
        if command == ":Td#":
            self.tracking = False
            return "1"
        if command in (":TS#", ":TQ#", ":TL#", ":TK#"):
            self.rate = {":TS#": "solar", ":TQ#": "sidereal", ":TL#": "lunar", ":TK#": "king"}[command]
            return ""
        if command.startswith(":Sr"):
            self.target = (_parse_ra(command[3:-1]), self.target[1])
            return "1"
        if command.startswith(":Sd"):
            self.target = (self.target[0], _parse_dec(command[3:-1]))
            return "1"
        if command == ":MS#":
            if self.parked:
                return "4"
            if self.target[0] is None or self.target[1] is None:
                return "9"
            self.ra_hours, self.dec_degrees = self.target
            return "0"
        if command == ":Q#":
            self.slewing = False
            return ""
        if command == ":hP#":
            self.parked = True
            self.tracking = False
            return "1"
        if command == ":hR#":
            self.parked = False
            return "1"
        if command.startswith(":M") or command.startswith(":R") or command.startswith(":Q"):
            return ""
        return "0"

    def read(self, size: int, timeout: float) -> bytes:
        chunk, self._pending = self._pending[:size], self._pending[size:]
        return chunk

    def reset_input(self) -> None:
        self._pending = b""

    def close(self) -> None:
        self._pending = b""

    def describe(self) -> str:
        return "simulated OnStepX (no hardware)"


# ------------------------------------------------------------------ coordinates


def _format_ra_highp(hours: float) -> str:
    h = int(hours)
    remainder = (hours - h) * 60.0
    m = int(remainder)
    s = (remainder - m) * 60.0
    return f"{h:02d}:{m:02d}:{s:07.4f}#"


def _format_dec_highp(degrees: float) -> str:
    sign = "+" if degrees >= 0 else "-"
    value = abs(degrees)
    d = int(value)
    remainder = (value - d) * 60.0
    m = int(remainder)
    s = (remainder - m) * 60.0
    return f"{sign}{d:02d}*{m:02d}:{s:06.3f}#"


def format_ra(hours: float) -> str:
    """Right ascension as the ``HH:MM:SS`` OnStepX accepts for a target."""
    hours = hours % 24.0
    h = int(hours)
    remainder = (hours - h) * 60.0
    m = int(remainder)
    s = int(round((remainder - m) * 60.0))
    if s == 60:
        s, m = 0, m + 1
    if m == 60:
        m, h = 0, (h + 1) % 24
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_dec(degrees: float) -> str:
    """Declination as the ``sDD*MM:SS`` OnStepX accepts for a target."""
    sign = "+" if degrees >= 0 else "-"
    value = abs(degrees)
    d = int(value)
    remainder = (value - d) * 60.0
    m = int(remainder)
    s = int(round((remainder - m) * 60.0))
    if s == 60:
        s, m = 0, m + 1
    if m == 60:
        m, d = 0, d + 1
    return f"{sign}{d:02d}*{m:02d}:{s:02d}"


def _parse_ra(text: str) -> float:
    """Parse ``HH:MM:SS(.ssss)`` or ``HH:MM.T`` into hours."""
    text = text.strip().rstrip("#")
    parts = re.split(r"[:]", text)
    if len(parts) == 2:
        return float(parts[0]) + float(parts[1]) / 60.0
    if len(parts) == 3:
        return float(parts[0]) + float(parts[1]) / 60.0 + float(parts[2]) / 3600.0
    raise MountError(f"cannot parse right ascension: {text!r}")


def _parse_dec(text: str) -> float:
    """Parse ``sDD*MM(:SS(.sss))`` into degrees."""
    text = text.strip().rstrip("#")
    sign = -1.0 if text.startswith("-") else 1.0
    text = text.lstrip("+-")
    parts = re.split(r"[*:'°]", text)
    parts = [p for p in parts if p]
    if len(parts) == 2:
        return sign * (float(parts[0]) + float(parts[1]) / 60.0)
    if len(parts) == 3:
        return sign * (float(parts[0]) + float(parts[1]) / 60.0 + float(parts[2]) / 3600.0)
    raise MountError(f"cannot parse declination: {text!r}")


def sun_radec(when=None, ephemeris: str = "de421.bsp") -> Tuple[float, float]:
    """Apparent geocentric right ascension and declination of the Sun.

    Returns (ra_hours, dec_degrees).  Good enough to put the Sun inside the
    field of a telephoto lens; the mount's own alignment dominates the error.
    """
    eph = load(ephemeris)
    ts = load.timescale()
    t = ts.now() if when is None else ts.from_datetime(when)
    astrometric = eph["Earth"].at(t).observe(eph["Sun"]).apparent()
    ra, dec, _ = astrometric.radec()
    return ra.hours, dec.degrees


# ---------------------------------------------------------------------- status


@dataclass
class MountStatus:
    """Decoded ``:GU#`` reply.

    Only the flags that matter for eclipse work are broken out; the raw string is
    kept so the bench console can show everything the controller reported.
    """

    raw: str = ""
    tracking: bool = False
    slewing: bool = False
    parked: bool = False
    parking: bool = False
    park_failed: bool = False
    at_home: bool = False
    homing: bool = False
    pulse_guiding: bool = False
    tracking_rate: str = "sidereal"
    pier_side: str = "none"
    mount_type: str = "unknown"
    error_code: Optional[str] = None

    @classmethod
    def parse(cls, raw: str) -> "MountStatus":
        raw = raw.strip().rstrip("#")
        status = cls(raw=raw)

        # The reply is a set of condition characters; absence is as meaningful as
        # presence, which is why tracking is "not the 'n' flag" rather than a
        # positive flag of its own.
        status.tracking = "n" not in raw
        status.slewing = "N" not in raw
        status.parked = "P" in raw
        status.parking = "I" in raw
        status.park_failed = "F" in raw
        status.at_home = "H" in raw
        status.homing = "h" in raw
        status.pulse_guiding = "G" in raw

        if "(" in raw:
            status.tracking_rate = "lunar"
        elif "O" in raw:
            status.tracking_rate = "solar"
        elif "k" in raw:
            status.tracking_rate = "king"
        else:
            status.tracking_rate = "sidereal"

        if "T" in raw:
            status.pier_side = "east"
        elif "W" in raw:
            status.pier_side = "west"
        else:
            status.pier_side = "none"

        for char, name in (("E", "GEM"), ("K", "FORK"), ("A", "ALTAZM"), ("L", "ALTALT")):
            if char in raw:
                status.mount_type = name
                break

        trailing = re.search(r"(\d+)$", raw)
        if trailing:
            status.error_code = trailing.group(1)

        return status

    def summary(self) -> str:
        bits = []
        bits.append("parked" if self.parked else ("slewing" if self.slewing else
                    ("tracking " + self.tracking_rate if self.tracking else "idle")))
        if self.at_home:
            bits.append("at home")
        if self.park_failed:
            bits.append("PARK FAILED")
        if self.pier_side != "none":
            bits.append(f"pier {self.pier_side}")
        return ", ".join(bits)


# ----------------------------------------------------------------------- mount


class OnStepXMount:
    """LX200 client for an OnStepX controller."""

    def __init__(self, transport: Transport, name: str = "mount"):
        self.transport = transport
        self.name = name
        self._lock = threading.RLock()
        self.traffic: list = []
        self.record_traffic = True

    # ---------------------------------------------------------------- plumbing

    def _read_terminated(self, timeout: float) -> str:
        deadline = time.perf_counter() + timeout
        buffer = ""
        while time.perf_counter() < deadline:
            chunk = self.transport.read(1, max(0.01, deadline - time.perf_counter()))
            if not chunk:
                continue
            char = chunk.decode("ascii", errors="replace")
            if char == "#":
                return buffer
            buffer += char
        raise MountError(f"timed out waiting for a terminated reply (got {buffer!r})")

    def send(self, command: str, reply: ReplyKind = ReplyKind.TERMINATED,
             timeout: float = DEFAULT_TIMEOUT_S) -> str:
        """Send one command frame and read the reply shape it declares.

        ``command`` may be given with or without the leading ``:`` and trailing
        ``#``; both are added if missing.
        """
        if not command.startswith((":", ";")):
            command = ":" + command
        if not command.endswith("#"):
            command = command + "#"

        with self._lock:
            started = time.perf_counter()
            self.transport.write(command.encode("ascii"))

            if reply is ReplyKind.NONE:
                result = ""
            elif reply in (ReplyKind.BOOL, ReplyKind.CHAR):
                raw = self.transport.read(1, timeout)
                if not raw:
                    raise MountError(f"no reply to {command}")
                result = raw.decode("ascii", errors="replace")
            else:
                result = self._read_terminated(timeout)

            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if self.record_traffic:
                self.traffic.append((time.time(), command, result, elapsed_ms))
            logger.debug("Mount %s -> %r (%.1f ms)", command, result, elapsed_ms)
            return result

    def send_bool(self, command: str, timeout: float = DEFAULT_TIMEOUT_S) -> bool:
        return self.send(command, ReplyKind.BOOL, timeout) == "1"

    def raw(self, command: str, reply: str = "terminated", timeout: float = DEFAULT_TIMEOUT_S) -> str:
        """Escape hatch for the bench console: send anything, read any shape."""
        return self.send(command, ReplyKind(reply), timeout)

    def close(self) -> None:
        self.transport.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def describe(self) -> str:
        return f"{self.product_name()} on {self.transport.describe()}"

    # ------------------------------------------------------------------- identity

    def product_name(self) -> str:
        return self.send(":GVP#")

    def firmware_version(self) -> str:
        return self.send(":GVN#")

    # --------------------------------------------------------------------- state

    def status(self) -> MountStatus:
        return MountStatus.parse(self.send(":GU#"))

    def get_radec(self) -> Tuple[float, float]:
        """Current pointing as (ra_hours, dec_degrees)."""
        ra = _parse_ra(self.send(":GRH#"))
        dec = _parse_dec(self.send(":GDH#"))
        return ra, dec

    def get_altaz(self) -> Tuple[float, float]:
        """Current pointing as (altitude_degrees, azimuth_degrees)."""
        alt = _parse_dec(self.send(":GAH#"))
        az = _parse_dec(self.send(":GZH#"))
        return alt, az

    # ------------------------------------------------------------------ tracking

    def tracking_on(self) -> bool:
        return self.send_bool(":Te#")

    def tracking_off(self) -> bool:
        return self.send_bool(":Td#")

    def set_tracking_rate(self, rate: str) -> None:
        """Select sidereal, solar, lunar or king tracking.

        Solar is the one that matters here: over the couple of hours from first
        to last contact, sidereal rate lets the Sun drift out of a long lens.
        """
        commands = {"sidereal": ":TQ#", "solar": ":TS#", "lunar": ":TL#", "king": ":TK#"}
        try:
            command = commands[rate.lower()]
        except KeyError:
            raise MountError(f"unknown tracking rate: {rate}") from None
        self.send(command, ReplyKind.NONE)

    # ---------------------------------------------------------------------- goto

    def set_target(self, ra_hours: float, dec_degrees: float) -> None:
        if not self.send_bool(f":Sr{format_ra(ra_hours)}#"):
            raise MountError(f"mount rejected target RA {format_ra(ra_hours)}")
        if not self.send_bool(f":Sd{format_dec(dec_degrees)}#"):
            raise MountError(f"mount rejected target Dec {format_dec(dec_degrees)}")

    def goto(self, ra_hours: float, dec_degrees: float, wait: bool = False,
             timeout: float = 180.0) -> None:
        """Slew to a target.  Raises with the controller's reason if refused."""
        self.set_target(ra_hours, dec_degrees)
        code = self.send(":MS#", ReplyKind.CHAR)
        if code != "0":
            raise MountError(f"goto refused: {GOTO_ERRORS.get(code, f'unknown code {code}')}")
        logger.info("Mount slewing to RA %s Dec %s", format_ra(ra_hours), format_dec(dec_degrees))
        if wait:
            self.wait_for_slew(timeout)

    def goto_sun(self, when=None, wait: bool = False, timeout: float = 180.0) -> Tuple[float, float]:
        """Slew to the Sun's current position and return the coordinates used."""
        ra_hours, dec_degrees = sun_radec(when)
        self.goto(ra_hours, dec_degrees, wait=wait, timeout=timeout)
        return ra_hours, dec_degrees

    def sync(self, ra_hours: float, dec_degrees: float) -> str:
        """Tell the mount it is already pointing at these coordinates."""
        self.set_target(ra_hours, dec_degrees)
        return self.send(":CM#")

    def wait_for_slew(self, timeout: float = 180.0) -> bool:
        """Block until the goto finishes.  False if it was still running at timeout."""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if not self.status().slewing:
                return True
            time.sleep(SLEW_POLL_INTERVAL_S)
        return False

    def abort(self) -> None:
        """Stop every motion immediately."""
        self.send(":Q#", ReplyKind.NONE)
        logger.info("Mount motion aborted")

    # ------------------------------------------------------------ manual motion

    def move(self, direction: str) -> None:
        """Start moving north, south, east or west at the current rate."""
        letters = {"north": "n", "south": "s", "east": "e", "west": "w"}
        letter = letters.get(direction.lower(), direction.lower())
        if letter not in ("n", "s", "e", "w"):
            raise MountError(f"unknown direction: {direction}")
        self.send(f":M{letter}#", ReplyKind.NONE)

    def stop_move(self, direction: Optional[str] = None) -> None:
        if direction is None:
            self.send(":Q#", ReplyKind.NONE)
            return
        letters = {"north": "n", "south": "s", "east": "e", "west": "w"}
        letter = letters.get(direction.lower(), direction.lower())
        self.send(f":Q{letter}#", ReplyKind.NONE)

    def set_rate(self, rate: str) -> None:
        """Guide, centering, find, fast or slew preset."""
        presets = {"guide": ":RG#", "center": ":RC#", "find": ":RM#", "fast": ":RF#", "slew": ":RS#"}
        try:
            command = presets[rate.lower()]
        except KeyError:
            raise MountError(f"unknown rate preset: {rate}") from None
        self.send(command, ReplyKind.NONE)

    def pulse_guide(self, direction: str, milliseconds: int) -> bool:
        letters = {"north": "n", "south": "s", "east": "e", "west": "w"}
        letter = letters.get(direction.lower(), direction.lower())
        return self.send_bool(f":MG{letter}{int(milliseconds)}#")

    # ------------------------------------------------------------------- parking

    def park(self) -> bool:
        return self.send_bool(":hP#")

    def unpark(self) -> bool:
        return self.send_bool(":hR#")

    def set_park_here(self) -> bool:
        return self.send_bool(":hQ#")


# ------------------------------------------------------------------ discovery


def find_mount_ports() -> list:
    """USB serial ports that could be the controller, as (device, description)."""
    return [(p.device, p.description or "unknown USB serial")
            for p in serial.tools.list_ports.comports() if p.vid is not None]


def probe_serial(port: str, baudrates=None, timeout: float = 1.0) -> Optional[int]:
    """Find the baud rate a controller answers on, or None.

    OnStepX builds do not all use the same rate, and the SAL-33's is not
    documented anywhere I could find, so probing beats guessing.
    """
    for baudrate in (baudrates or CANDIDATE_BAUDRATES):
        transport = None
        try:
            transport = SerialTransport(port, baudrate, timeout=timeout)
            mount = OnStepXMount(transport)
            name = mount.send(":GVP#", ReplyKind.TERMINATED, timeout=timeout)
            if name:
                logger.info("Mount answered at %d baud: %s", baudrate, name)
                return baudrate
        except MountError:
            continue
        finally:
            if transport is not None:
                transport.close()
    return None


def connect(port: Optional[str] = None, baudrate: Optional[int] = None,
            host: Optional[str] = None, tcp_port: int = DEFAULT_TCP_PORT,
            simulated: bool = False, name: str = "mount") -> OnStepXMount:
    """Open a mount connection.

    Cable first: with no arguments this probes USB serial ports and returns the
    first controller that answers.  ``host`` switches to the TCP transport for
    the WiFi builds.
    """
    if simulated:
        return OnStepXMount(SimulatedTransport(), name=name)

    if host:
        return OnStepXMount(TcpTransport(host, tcp_port), name=name)

    if port:
        rate = baudrate or probe_serial(port) or DEFAULT_BAUDRATE
        return OnStepXMount(SerialTransport(port, rate), name=name)

    for device, description in find_mount_ports():
        logger.debug("Probing %s (%s)", device, description)
        rate = probe_serial(device)
        if rate:
            return OnStepXMount(SerialTransport(device, rate), name=name)

    raise MountError(
        "no OnStepX controller found on any USB serial port — pass an explicit port, "
        "or use the simulated transport to rehearse without hardware"
    )


# ------------------------------------------------------------ scheduler commands


def mount_track_sun(mount: OnStepXMount) -> None:
    """Switch to solar rate and start tracking."""
    logger.info("mount_track_sun")
    mount.set_tracking_rate("solar")
    mount.tracking_on()


def mount_goto_sun(mount: OnStepXMount, wait: str = "false") -> None:
    """Slew to the Sun, optionally blocking until the slew completes."""
    should_wait = str(wait).strip().lower() in ("1", "true", "yes", "wait")
    ra_hours, dec_degrees = mount.goto_sun(wait=should_wait)
    logger.info("mount_goto_sun: RA %s Dec %s", format_ra(ra_hours), format_dec(dec_degrees))


def mount_tracking(mount: OnStepXMount, state: str = "on") -> None:
    """Turn tracking on or off."""
    if str(state).strip().lower() in ("on", "1", "true", "yes"):
        mount.tracking_on()
    else:
        mount.tracking_off()


def mount_park(mount: OnStepXMount) -> None:
    logger.info("mount_park")
    mount.park()


def mount_unpark(mount: OnStepXMount) -> None:
    logger.info("mount_unpark")
    mount.unpark()


def mount_stop(mount: OnStepXMount) -> None:
    """Abort all motion.  Safe to schedule defensively."""
    logger.info("mount_stop")
    mount.abort()
