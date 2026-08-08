"""USB relay shutter trigger.

Fires a camera through the wired remote-release jack (2.5 mm TRS on Fujifilm X
bodies) by closing relay contacts, instead of driving the shutter over USB.

The relay's contacts are dry, so they carry no polarity and are electrically
isolated from the host — the same guarantee a mechanical cable release gives.
Two wiring layouts are supported:

    single channel   tip and ring joined to one NO contact.  Closing the contact
                     is a full press; the camera free-runs in whatever drive mode
                     it is set to for as long as the contact is held.

    dual channel     tip on one NO contact (S1, half press) and ring on another
                     (S2, full press).  Allows an explicit half-press settle
                     before each frame, and per-frame control of the interval.
                     On the X-T4's 2.5 mm jack the tip is S1 and the ring is
                     S2; check the body's own pinout before wiring anything
                     else.

Wire the *normally open* terminals.  On normally-closed contacts the shutter is
held down whenever the relay is unpowered, so unplugging the USB cable would fire
the camera and keep it firing.
"""

import atexit
import logging
import signal
import sys
import threading
import time
import weakref
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Optional

import serial
import serial.tools.list_ports

from solareclipseworkbench import hardware_problems
from solareclipseworkbench.discovery import Candidate
from solareclipseworkbench.hardware_registry import HARDWARE
from solareclipseworkbench.serial_ports import resolve_port, usb_serial_ports

try:
    import hid
except ImportError:
    hid = None

logger = logging.getLogger(__name__)

#: Longest a single mid-burst drain may run before the hold rechecks its own
#: deadline.  Short enough that a burst ends close to its scripted length, long
#: enough to free a useful number of slots: the deletes cost about 0.15 s each
#: while the camera is firing, so this is roughly a dozen frames per pass.
MID_BURST_DRAIN_S = 2.0

ENTRY_POINT_GROUP = "solareclipseworkbench.relay_backends"

# Time to let the camera wake and arm after S1 closes, before S2 is asserted.
# Below roughly 100 ms the first frame of a sequence arrives late or not at all.
# In manual mode there is no metering to settle, but the body still has to wake.
DEFAULT_SETTLE_S = 0.12

# How long S2 stays closed for a single frame in dual-channel mode.  The camera
# samples the release line periodically, so too short a pulse is simply missed.
DEFAULT_PULSE_S = 0.04

# USB-serial adapters commonly found on cheap relay boards, by (vid, pid).
KNOWN_SERIAL_ADAPTERS = {
    (0x1A86, 0x7523): "CH340",
    (0x1A86, 0x5523): "CH341",
    (0x0403, 0x6001): "FT232",
    (0x10C4, 0xEA60): "CP2102",
    (0x2A19, 0x0C01): "Numato",
}

# dcttech / "USBRelay" HID boards.
HID_RELAY_IDS = [(0x16C0, 0x05DF)]


class RelayError(Exception):
    """Raised when the relay cannot be reached or refuses a command."""


@dataclass
class Wiring:
    """Which relay channel drives which release line.

    For a single-channel board, leave ``s1_channel`` as None: tip and ring are
    joined on one contact, so there is no separate half-press to assert.
    """

    s2_channel: int = 1
    s1_channel: Optional[int] = None
    settle_s: float = DEFAULT_SETTLE_S
    pulse_s: float = DEFAULT_PULSE_S

    @property
    def is_single_channel(self) -> bool:
        return self.s1_channel is None

    @property
    def channels(self) -> list:
        """Every channel this wiring touches, low to high."""
        if self.is_single_channel:
            return [self.s2_channel]
        return sorted({self.s1_channel, self.s2_channel})


@dataclass
class Event:
    """One contact transition, kept for bench diagnostics."""

    at: float
    channel: int
    closed: bool
    elapsed_ms: float
    error: Optional[str] = None


class Backend(ABC):
    """A USB relay board.  Channels are 1-based throughout.

    A backend supplies only the two primitives: close a contact, open a contact.
    Everything above that — settle timing, pulse width, burst, bulb, and the
    guarantee that contacts are released on the way out — belongs to
    :class:`RelayTrigger` and is deliberately not reimplemented per board.  A
    held contact through totality is unrecoverable, so that logic lives in one
    place.
    """

    #: Short identifier used in configuration and on the command line.
    name = "backend"
    #: One line describing the hardware this backend speaks to.
    description = ""

    @classmethod
    def discover(cls) -> list:
        """Places one of these boards might be.

        Default is none, which is right for backends needing an explicit
        address.  Being listed is a hint to probe, never proof.
        """
        return []

    @abstractmethod
    def set_channel(self, channel: int, closed: bool) -> None:
        """Close or open one channel."""

    @abstractmethod
    def describe(self) -> str:
        """Human-readable identification, for logs and the bench console."""

    def close(self) -> None:
        """Release the underlying device."""


_backend_registry: dict = {}
_backends_discovered = False


def register_backend(backend_class):
    """Register a relay backend.  Usable as a decorator."""
    if not issubclass(backend_class, Backend):
        raise TypeError(f"{backend_class!r} is not a Backend")
    if not backend_class.name or backend_class.name == "backend":
        raise ValueError(f"{backend_class!r} must set its own 'name'")
    if (backend_class.name in _backend_registry
            and _backend_registry[backend_class.name] is not backend_class):
        logger.warning("Relay backend %r is being replaced by %r", backend_class.name, backend_class)
    _backend_registry[backend_class.name] = backend_class
    logger.debug("Registered relay backend: %s", backend_class.name)
    return backend_class


def discover_backends(force: bool = False) -> dict:
    """Load backends published by other packages, and return the registry.

    Built-in backends register themselves when this module is imported; this
    adds any supplied through the entry point group.
    """
    global _backends_discovered
    if _backends_discovered and not force:
        return dict(_backend_registry)
    try:
        points = entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:
        # Older importlib.metadata returns a dict keyed by group.
        points = entry_points().get(ENTRY_POINT_GROUP, [])
    for point in points:
        try:
            loaded = point.load()
            if isinstance(loaded, type) and issubclass(loaded, Backend):
                register_backend(loaded)
            else:
                logger.warning("Entry point %r did not provide a relay Backend", point.name)
        except Exception:
            # One broken plugin must not stop the rest from loading.
            logger.warning("Could not load relay backend plugin %r", point.name, exc_info=True)
    _backends_discovered = True
    return dict(_backend_registry)


def list_backends() -> list:
    """Every registered backend class, sorted by name."""
    return [_backend_registry[name] for name in sorted(discover_backends())]


def get_backend(name: str):
    """Look up one backend class by name."""
    backends = discover_backends()
    try:
        return backends[name]
    except KeyError:
        available = ", ".join(sorted(backends)) or "none"
        raise RelayError(f"unknown relay backend {name!r} (available: {available})") from None


def discover_relays(backend: Optional[str] = None) -> list:
    """Ask backends where their boards might be."""
    classes = [get_backend(backend)] if backend else list_backends()
    candidates = []
    for backend_class in classes:
        try:
            candidates.extend(backend_class.discover())
        except Exception:
            logger.debug("Backend %s failed to enumerate", backend_class.name, exc_info=True)
    return candidates


def _port_text(port) -> str:
    """Description and manufacturer of a serial port, lowered, for matching."""
    return " ".join(
        filter(None, [port.description, getattr(port, "manufacturer", None)])
    ).lower()


@register_backend
class LcusSerialBackend(Backend):
    """LCUS-1 / LCUS-2 style boards behind a CH340.

    Protocol is four bytes: 0xA0, channel, state, checksum, where the checksum is
    the low byte of the sum of the preceding three.
    """

    name = "lcus"
    description = "LCUS-style serial relay boards (CH340 and similar)"

    # All three serial backends open with exclusive=True: a second claim on a
    # held port must fail at open rather than put two devices on one wire.

    @classmethod
    def discover(cls) -> list:
        """Any USB serial adapter that is not identifiably something else.

        These boards answer nothing, so they cannot be probed — the only way to
        confirm one is to pulse a channel and listen for the click.

        A CP2102 is identifiably something else: LCUS boards are CH340-based,
        while DSD TECH builds on the CP2102 — an SH-UR04A offered as LCUS here
        cost a bench evening on 4 August 2026.  Numato has its own vendor id.
        Both are left to their own backends; an explicitly configured port
        still opens as LCUS regardless.
        """
        candidates = []
        for port in usb_serial_ports():
            description = port.description or "USB serial"
            if any(vendor in _port_text(port) for vendor in ("numato", "dsd")):
                continue
            adapter = KNOWN_SERIAL_ADAPTERS.get((port.vid, port.pid))
            if adapter in ("CP2102", "Numato"):
                continue
            candidates.append(Candidate(
                kind="relay", driver=cls.name, target=port.device,
                description=f"{adapter} — {description}" if adapter else description,
                config={"port": port.device},
            ))
        return candidates

    def __init__(self, port: str, baudrate: int = 9600, timeout: float = 0.2):
        self.port = port
        try:
            self._serial = serial.Serial(port, baudrate, timeout=timeout, exclusive=True)
        except serial.SerialException as exc:
            raise RelayError(f"cannot open relay on {port}: {exc}") from exc

    def set_channel(self, channel: int, closed: bool) -> None:
        packet = bytes([0xA0, channel, 0x01 if closed else 0x00])
        packet += bytes([sum(packet) & 0xFF])
        try:
            self._serial.write(packet)
            self._serial.flush()
        except serial.SerialException as exc:
            raise RelayError(f"write failed on {self.port}: {exc}") from exc

    def describe(self) -> str:
        return f"LCUS-style serial relay on {self.port}"

    def close(self) -> None:
        try:
            self._serial.close()
        except Exception:
            logger.debug("Error closing relay serial port", exc_info=True)


@register_backend
class NumatoSerialBackend(Backend):
    """Numato Lab USB relay boards, which take plain-text commands.

    Numato numbers its relays from zero, so the 1-based channel is shifted here
    to keep the rest of the module consistent.
    """

    name = "numato"
    description = "Numato Lab USB relay boards"

    @classmethod
    def discover(cls) -> list:
        return [
            Candidate(kind="relay", driver=cls.name, target=port.device,
                      description=port.description or "Numato", config={"port": port.device})
            for port in usb_serial_ports()
            if (port.vid, port.pid) == (0x2A19, 0x0C01)
            or "numato" in (port.description or "").lower()
        ]

    def __init__(self, port: str, baudrate: int = 19200, timeout: float = 0.2):
        self.port = port
        try:
            self._serial = serial.Serial(port, baudrate, timeout=timeout, exclusive=True)
        except serial.SerialException as exc:
            raise RelayError(f"cannot open relay on {port}: {exc}") from exc

    def set_channel(self, channel: int, closed: bool) -> None:
        verb = "on" if closed else "off"
        command = f"relay {verb} {channel - 1}\r".encode("ascii")
        try:
            self._serial.write(command)
            self._serial.flush()
        except serial.SerialException as exc:
            raise RelayError(f"write failed on {self.port}: {exc}") from exc

    def describe(self) -> str:
        return f"Numato serial relay on {self.port}"

    def close(self) -> None:
        try:
            self._serial.close()
        except Exception:
            logger.debug("Error closing relay serial port", exc_info=True)


@register_backend
class DsdSerialBackend(Backend):
    """DSD TECH SH-UR series boards (SH-UR01A, SH-UR04A) behind a CP2102.

    Protocol is ASCII AT commands at 9600 baud: ``AT+CH1=1`` closes channel 1,
    ``AT+CH1=0`` opens it.  The parser is line-buffered and executes nothing
    until CR/LF arrives — proven on an SH-UR04A on 4 August 2026, where
    unterminated commands were silently held forever and the relays never
    moved.  The same buffering means bytes from an interrupted sender corrupt
    the next command, so the line buffer is cleared with a bare CR/LF on open.
    """

    name = "dsd"
    description = "DSD TECH SH-UR series relay boards (AT commands over CP2102)"

    @classmethod
    def discover(cls) -> list:
        """CP2102 ports, with DSD's own USB strings as the strong signal.

        DSD TECH flashes its name into the CP2102's product string, so a port
        that says so is near-certain.  A bare CP2102 is only a maybe — plenty of
        other hardware (GPS dongles included) uses the same chip — and is listed
        after the certain ones so auto-connect tries it last.
        """
        certain, maybe = [], []
        for port in usb_serial_ports():
            is_dsd = "dsd" in _port_text(port)
            is_cp2102 = (port.vid, port.pid) == (0x10C4, 0xEA60)
            if not (is_dsd or is_cp2102):
                continue
            candidate = Candidate(
                kind="relay", driver=cls.name, target=port.device,
                description=(port.description or "DSD TECH relay") if is_dsd
                else f"CP2102 — possibly a DSD board: {port.description or port.device}",
                config={"port": port.device},
            )
            (certain if is_dsd else maybe).append(candidate)
        return certain + maybe

    def __init__(self, port: str, baudrate: int = 9600, timeout: float = 0.2):
        self.port = port
        try:
            self._serial = serial.Serial(port, baudrate, timeout=timeout, exclusive=True)
            # Terminate whatever half-command the firmware may still be
            # holding, so the first real command is parsed clean.
            self._serial.write(b"\r\n")
            self._serial.flush()
        except serial.SerialException as exc:
            raise RelayError(f"cannot open relay on {port}: {exc}") from exc
        # The SH-UR firmware acknowledges every command with "OK+...", so
        # unlike most relay boards this one can prove what it is - worth
        # doing, since /dev names reshuffle on replug.  Opening channel 1 is
        # the no-op probe: every connect starts from released contacts.
        try:
            time.sleep(0.1)
            self._serial.reset_input_buffer()
            self._serial.write(b"AT+CH1=0\r\n")
            self._serial.flush()
            reply = self._serial.read(16)
        except serial.SerialException as exc:
            self.close()
            raise RelayError(f"cannot probe relay on {port}: {exc}") from exc
        if b"OK" not in reply:
            self.close()
            raise RelayError(
                f"nothing answering AT commands on {port} (got {reply!r}) - "
                "wrong port, or not a DSD board")

    def set_channel(self, channel: int, closed: bool) -> None:
        command = f"AT+CH{channel}={1 if closed else 0}\r\n".encode("ascii")
        try:
            self._serial.write(command)
            self._serial.flush()
        except serial.SerialException as exc:
            raise RelayError(f"write failed on {self.port}: {exc}") from exc

    def describe(self) -> str:
        return f"DSD TECH serial relay on {self.port}"

    def close(self) -> None:
        try:
            self._serial.close()
        except Exception:
            logger.debug("Error closing relay serial port", exc_info=True)


@register_backend
class HidRelayBackend(Backend):
    """dcttech-style HID relay boards, which expose no serial port."""

    name = "hid"
    description = "dcttech-style HID relay boards (USBRelay1/2/4)"

    @classmethod
    def discover(cls) -> list:
        if hid is None:
            return []
        candidates = []
        try:
            for device in hid.enumerate():
                if (device.get("vendor_id"), device.get("product_id")) in HID_RELAY_IDS:
                    candidates.append(Candidate(
                        kind="relay", driver=cls.name,
                        target=f"{device['vendor_id']:04x}:{device['product_id']:04x}",
                        description=device.get("product_string") or "HID relay",
                        config={"vendor_id": device["vendor_id"], "product_id": device["product_id"]},
                    ))
        except Exception:
            logger.debug("HID enumeration failed", exc_info=True)
        return candidates

    def __init__(self, vendor_id: int = 0x16C0, product_id: int = 0x05DF, **config):
        if hid is None:
            raise RelayError(
                "HID relay boards need the hidapi bindings: 'uv pip install hidapi', or "
                "install this project with its 'hid' extra.  Take care to install 'hidapi' "
                "and not the similarly named 'hid', which imports the same but expects the "
                "shared library to already be on the system."
            )
        try:
            self._device = hid.device()
            self._device.open(vendor_id, product_id)
        except Exception as exc:
            # The message hidapi gives ("unable to open device") never says why,
            # and the cause differs by platform.
            if sys.platform == "darwin":
                hint = ("Check the board is plugged in directly rather than through a hub — "
                        "these boards are known to be fussy about hubs.")
            else:
                hint = ("If lsusb shows the device, this is almost certainly permissions: a udev "
                        "rule granting access to the hidraw node is needed.")
            raise RelayError(
                f"cannot open HID relay {vendor_id:04x}:{product_id:04x}: {exc}.  {hint}"
            ) from exc
        self._ids = (vendor_id, product_id)

    def set_channel(self, channel: int, closed: bool) -> None:
        report = [0x00, 0xFF if closed else 0xFD, channel, 0, 0, 0, 0, 0, 0]
        try:
            self._device.send_feature_report(report)
        except Exception as exc:
            raise RelayError(f"HID report failed: {exc}") from exc

    def describe(self) -> str:
        return f"HID relay {self._ids[0]:04x}:{self._ids[1]:04x}"

    def close(self) -> None:
        try:
            self._device.close()
        except Exception:
            logger.debug("Error closing HID relay", exc_info=True)


@register_backend
class SimulatedBackend(Backend):
    """Stand-in for a real board, for rehearsing scripts with no hardware.

    Mirrors the virtual camera: the whole schedule can be exercised indoors, and
    the contact state is readable so tests can assert on it.
    """

    name = "simulated"
    description = "Software-only relay, for rehearsing with no hardware connected"

    @classmethod
    def discover(cls) -> list:
        # Never offered by discovery: selecting a simulated trigger has to be a
        # deliberate choice, or a real board could be silently replaced by one
        # and a script would appear to run while firing nothing.
        return []

    def __init__(self, latency_s: float = 0.006, **config):
        # Tolerates the same keyword arguments as the real backends, so a
        # configuration can be pointed at the simulator without editing it.
        self.latency_s = latency_s
        self.state: dict = {}

    def set_channel(self, channel: int, closed: bool) -> None:
        time.sleep(self.latency_s)
        self.state[channel] = closed
        logger.debug("Simulated relay: channel %d %s", channel, "closed" if closed else "open")

    def describe(self) -> str:
        return "simulated relay (no hardware)"


def make_backend(kind: str = "auto", port: Optional[str] = None, **config) -> Backend:
    """Build a backend by name, or find one.

    With an explicit ``kind`` the named backend is used.  With ``auto`` every
    backend is asked where its boards might be and the first that opens wins.
    """
    kind = (kind or "auto").lower()

    # A port remembered from a previous run may name the other driver's node
    # for the same adapter, or a name the adapters have since swapped between.
    # Resolve it to whatever is actually on the bus before anything is opened.
    if port:
        port = resolve_port(port)

    if kind != "auto":
        backend_class = get_backend(kind)
        if port:
            config.setdefault("port", port)
        return backend_class(**config)

    if port:
        # A bare port with no backend named is the common case, and LCUS is the
        # protocol the cheap boards speak.
        return get_backend("lcus")(port=port, **config)

    errors = []
    for candidate in discover_relays():
        merged = dict(candidate.config)
        merged.update(config)
        try:
            return get_backend(candidate.driver)(**merged)
        except RelayError as exc:
            errors.append(f"{candidate}: {exc}")

    detail = ("  " + "\n  ".join(errors)) if errors else "  (nothing found to try)"
    raise RelayError(
        "no relay could be opened.  Tried:\n" + detail +
        "\nPass an explicit port, or use the 'simulated' backend to rehearse without hardware."
    )


# Every live trigger, so the interpreter can release contacts on the way out.
_live_triggers: weakref.WeakSet = weakref.WeakSet()


def _release_all_contacts() -> None:
    """Open every contact on every live trigger.

    A held contact is the one state that must never survive this process.  In
    bulb it is an exposure that never ends; in continuous drive it is a camera
    that fills its card.
    """
    for trigger in list(_live_triggers):
        try:
            trigger.release_all()
        except Exception:
            logger.debug("Failed to release contacts during shutdown", exc_info=True)


def _install_shutdown_guards() -> None:
    """Make sure contacts are opened when the process ends.

    The release happens at exit, not in the signal handler.  A Python signal
    handler runs on the main thread between bytecodes, so it can interrupt any
    critical section - and this one iterated a WeakSet and did USB I/O.  On
    4 August a Ctrl-C landed inside a lock in hardware_problems, the handler
    raised while another exception was already pending:

        SystemError: WeakSet.__iter__ returned a result with an exception set

    and the lock was never released.  The interface then blocked forever in
    count(), the window stopped responding, and SIGTERM could not get through
    either - the process had to be killed outright.

    Raising is all a handler needs to do.  The interpreter unwinds, atexit
    runs, and the contacts are opened there - on a normal stack, where taking
    locks and talking to USB is safe.
    """
    atexit.register(_release_all_contacts)
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous = signal.getsignal(signum)

            def handler(sig, frame, _previous=previous):
                if callable(_previous):
                    _previous(sig, frame)
                elif sig == signal.SIGINT:
                    raise KeyboardInterrupt
                else:
                    raise SystemExit(1)

            signal.signal(signum, handler)
        except (ValueError, OSError):
            # Not the main thread, or a platform without this signal.
            logger.debug("Could not install shutdown guard for signal %s", signum)


_install_shutdown_guards()


class RelayTrigger:
    """A camera shutter driven through USB relay contacts."""

    vendor = "Relay"

    def __init__(self, backend: Backend, wiring: Optional[Wiring] = None, name: str = "relay"):
        self.backend = backend
        self.wiring = wiring or Wiring()
        self.name = name
        self.events: list = []
        self.record_events = True
        self._lock = threading.RLock()
        self._closed_channels: set = set()
        _live_triggers.add(self)
        # A board remembers its contacts.  The shutdown guards cover atexit,
        # SIGINT and SIGTERM, but a segmentation fault runs none of them - one
        # on 3 August left a contact latched, and the board held it across the
        # crash, the replug and into the next run.  A held contact is a shutter
        # held down: the body then answers 0x1006 to everything, including
        # SetPriorityMode, and survives every reconnect because the session was
        # never what was wrong.  Whatever state the board was found in, this run
        # starts from open.  Not recorded as an event: nothing here was
        # commanded, and the bench console's log is a record of what the run
        # did, not of the state it inherited.
        self.record_events = False
        try:
            self._release(self.wiring.channels)
        finally:
            self.record_events = True

    # ---------------------------------------------------------------- plumbing

    def _set(self, channel: int, closed: bool) -> None:
        started = time.perf_counter()
        error = None
        try:
            self.backend.set_channel(channel, closed)
            if closed:
                self._closed_channels.add(channel)
            else:
                self._closed_channels.discard(channel)
        except RelayError as exc:
            error = str(exc)
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if self.record_events:
                self.events.append(Event(time.time(), channel, closed, elapsed_ms, error))
            logger.debug(
                "Relay channel %d %s in %.2f ms%s",
                channel, "closed" if closed else "open", elapsed_ms,
                f" (error: {error})" if error else "",
            )

    def describe(self) -> str:
        layout = "single channel" if self.wiring.is_single_channel else "dual channel"
        return f"{self.backend.describe()} — {layout}"

    @property
    def closed_channels(self) -> set:
        """Channels currently believed to be closed."""
        return set(self._closed_channels)

    def release_all(self) -> None:
        """Open every channel this trigger touches, ignoring errors.

        Safe to call repeatedly and from a signal handler, so it never raises —
        one channel refusing must not stop the rest from being released.
        Transitions are still recorded, so the bench console's event log shows
        releases as well as closures.
        """
        self._release(self.wiring.channels)

    def _release(self, channels) -> None:
        """Open the given channels without ever raising."""
        for channel in channels:
            started = time.perf_counter()
            error = None
            try:
                self.backend.set_channel(channel, False)
                self._closed_channels.discard(channel)
            except Exception as exc:
                error = str(exc)
                logger.debug("Failed to open channel %d", channel, exc_info=True)
            finally:
                if self.record_events:
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    self.events.append(Event(time.time(), channel, False, elapsed_ms, error))

    def close(self) -> None:
        self.release_all()
        self.backend.close()
        _live_triggers.discard(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    # ------------------------------------------------------------------- press

    def half_press(self) -> None:
        """Assert S1.  A no-op on single-channel wiring, where S1 rides with S2."""
        if self.wiring.is_single_channel:
            return
        self._set(self.wiring.s1_channel, True)

    def release_half_press(self) -> None:
        if self.wiring.is_single_channel:
            return
        self._set(self.wiring.s1_channel, False)

    @contextmanager
    def pressed(self, settle: Optional[float] = None):
        """Hold a full press for the duration of the block.

        The release runs in a finally, so an exception inside the block still
        lets the shutter go.

        If S1 is already closed when this is entered — the caller has pre-armed
        with ``half_press()`` — the settle is skipped and S1 is left closed on
        the way out.  On an X-T4 the settle is 120 ms of a 170 ms trigger
        latency while the body's own release lag is only about 46 ms, so holding
        S1 across a sequence of frames is most of the delay gone.  Pre-arming
        keeps the camera awake as well, which is the reason the settle exists.
        """
        settle_s = self.wiring.settle_s if settle is None else settle
        pre_armed = (not self.wiring.is_single_channel
                     and self.wiring.s1_channel in self._closed_channels)
        try:
            self.half_press()
            if not self.wiring.is_single_channel and settle_s > 0 and not pre_armed:
                time.sleep(settle_s)
            self._set(self.wiring.s2_channel, True)
            yield
        finally:
            self._release([self.wiring.s2_channel] if pre_armed else self.wiring.channels)

    def shoot(self, pulse: Optional[float] = None) -> None:
        """Take one frame."""
        pulse_s = self.wiring.pulse_s if pulse is None else pulse
        with self.pressed():
            time.sleep(pulse_s)

    def burst(self, duration_s: float, interval_s: Optional[float] = None) -> int:
        """Shoot continuously for ``duration_s`` seconds.

        On single-channel wiring the contact is simply held and the camera paces
        itself — its firmware keeps frames far more even than a host-driven pulse
        train would.  The return value is the number of S2 pulses issued, which
        is 1 in that case and says nothing about how many frames resulted.

        On dual-channel wiring, S1 is held for the whole run and S2 is pulsed at
        ``interval_s``, so the frame count is known.
        """
        if self.wiring.is_single_channel or interval_s is None:
            with self.pressed():
                time.sleep(duration_s)
            return 1

        pulses = 0
        deadline = time.perf_counter() + duration_s
        try:
            self.half_press()
            time.sleep(self.wiring.settle_s)
            while time.perf_counter() < deadline:
                frame_started = time.perf_counter()
                self._set(self.wiring.s2_channel, True)
                time.sleep(self.wiring.pulse_s)
                self._set(self.wiring.s2_channel, False)
                pulses += 1
                # Busy-wait the remainder: sleep() drifts at these intervals.
                while time.perf_counter() - frame_started < interval_s:
                    if time.perf_counter() >= deadline:
                        break
        finally:
            self.release_all()
        return pulses

    def bulb(self, seconds: float) -> None:
        """Hold the shutter open for ``seconds``.

        Requires the shutter speed dial at B.  Practical from roughly a quarter
        of a second upward; anything faster belongs to the camera's own shutter.
        """
        with self.pressed():
            time.sleep(seconds)

    # -------------------------------------------------------------- diagnostics

    def timing_sample(self, samples: int = 20, channel: Optional[int] = None) -> dict:
        """Close and open one channel repeatedly, and report the spread.

        This measures the host-to-contact path only — USB, driver, and coil.  It
        does not include the camera's own shutter lag, which needs a photograph
        of a running millisecond timer to see.
        """
        target = channel or self.wiring.s2_channel
        timings = []
        for _ in range(samples):
            started = time.perf_counter()
            self.backend.set_channel(target, True)
            timings.append((time.perf_counter() - started) * 1000.0)
            time.sleep(0.02)
            self.backend.set_channel(target, False)
            time.sleep(0.02)
        self._closed_channels.discard(target)

        ordered = sorted(timings)
        mean = sum(timings) / len(timings)
        return {
            "samples": len(timings),
            "min_ms": ordered[0],
            "max_ms": ordered[-1],
            "mean_ms": mean,
            "median_ms": ordered[len(ordered) // 2],
            "spread_ms": ordered[-1] - ordered[0],
        }

    def recent_events(self, limit: int = 20) -> list:
        return self.events[-limit:]


def open_trigger(kind: str = "auto", port: Optional[str] = None,
                 s2_channel: int = 1, s1_channel: Optional[int] = None,
                 name: str = "relay") -> RelayTrigger:
    """Convenience constructor used by the GUI and the bench console."""
    backend = make_backend(kind, port)
    wiring = Wiring(s2_channel=s2_channel, s1_channel=s1_channel)
    trigger = RelayTrigger(backend, wiring, name=name)
    logger.info("Relay trigger ready: %s", trigger.describe())
    return trigger


# ------------------------------------------------------------ scheduler commands
#
# These mirror the camera command functions so eclipse scripts can drive the
# relay the same way they drive a camera.

def _catch_up_exposure(budget_s: float) -> None:
    """Put any settings a busy body blocked back on, before contacts close.

    The relay fires whatever is already dialled in, so a blocked exposure
    write would leave the burst running stale.  The budget is hard: past it
    the catch-up is skipped rather than delay the contacts it serves.
    """
    owner = HARDWARE.get('sdk_camera')
    apply_pending = getattr(owner, 'apply_pending', None)
    if apply_pending is None:
        return
    try:
        apply_pending(budget_s=budget_s)
    except Exception:
        logger.warning("Exposure catch-up before the relay failed", exc_info=True)


def relay_shoot(trigger: RelayTrigger) -> None:
    """Take a single frame through the relay."""
    logger.info("relay_shoot")
    _catch_up_exposure(budget_s=0.8)
    trigger.shoot()


def relay_burst(trigger: RelayTrigger, duration: float, interval: Optional[float] = None) -> None:
    """Hold the shutter for ``duration`` seconds, draining the queue as it fills.

    Arguments arrive from the script as strings, so they are coerced before use.

    A held burst runs at the body's own continuous rate, and with a tether
    session every frame parks a copy in the 32-slot transfer queue until the
    PC deletes it - the card writes do NOT free the slots.  At 32 the body
    hard-stops the burst.  Proven on the bench, 5 August, watching the queue
    live: 32/32 at five seconds and not one more frame in the next seven.
    That ceiling silently truncated every scripted burst there has ever been -
    "C2 stopped too soon" was this, both nights.

    So the queue is drained DURING the hold.  Also proven live: deleting while
    S1 and S2 are held does not drop the session (the old warning to the
    contrary was stale), the slots free, and the body just keeps firing -
    81 frames in a 12 s hold, ~6.9 fps sustained, in backup and sequential
    card modes alike.  The old cap-and-shorten fallback remains only for a
    burst with no SDK session, where there is no queue to fill.
    """
    duration = float(duration)
    interval = None if interval in (None, "") else float(interval)

    owner = HARDWARE.get('sdk_camera')

    if owner is None:
        # No tether session, so no queue to fill and nothing to drain.
        logger.info("relay_burst: %.3f s (interval %s), no session", duration, interval)
        pulses = trigger.burst(duration, interval)
        logger.info("relay_burst issued %d pulse(s)", pulses)
        return

    drained = 0
    deadline = time.monotonic() + duration
    lock = getattr(owner, '_usb_lock', None)
    acquired = bool(lock and lock.acquire(timeout=2.0))

    # The drain runs INLINE, on this thread, between the contact closing and
    # opening.  That looks like it stretches the hold - a drain that runs long
    # delays the release, and on 8 August an 8 s burst held 17.4 s - and the
    # obvious repair is to move the drain to its own thread and time the hold
    # exactly.  Measured the same evening, that repair costs two thirds of the
    # burst: inline, 95 frames with the queue peaking at 30 and coming back
    # down; threaded, exactly 32 frames, the queue full, the body hard-stopped
    # and the contact clicking against a shutter that would not fire.
    #
    # So the drain keeps up only while it has this thread to itself.  A burst
    # that runs long and shoots is worth more than one that stops on time and
    # does not, and the overrun is handled where it belongs - by pricing the
    # hold in the schedule.  Do not thread this again without re-measuring the
    # frame count; the wall clock alone says the opposite of the truth.
    if interval is not None:
        logger.warning("relay_burst: paced at %.3f s - the queue is drained "
                       "between pulses, which the body may outrun", interval)
    logger.info("relay_burst: %.3f s %s, queue drained live", duration,
                "held" if interval is None else f"paced at {interval:.3f} s")
    try:
        if interval is None:
            with trigger.pressed():
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    try:
                        # One round, no settle, and never more time than the
                        # burst has left.  The hold cannot end while a drain is
                        # running, so an unbounded drain IS the overrun: 8 s
                        # asked for, 18.7 s held, measured 8 August.  The settle
                        # is for frames still arriving after the shutter stops;
                        # mid-burst they are already there.
                        drained += owner.drain(rounds=1, settle_s=0.0,
                                               budget_s=min(MID_BURST_DRAIN_S, left))
                    except TypeError:
                        # A camera whose drain predates these arguments.
                        drained += owner.drain()
                    except Exception:
                        logger.warning("relay_burst: mid-hold drain failed; the "
                                       "burst runs on and may cap at the queue",
                                       exc_info=True)
                        time.sleep(max(0.0, deadline - time.monotonic()))
                        break
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        else:
            # Paced.  The pulse train blocks, so the only room for a drain is
            # between pulses; `trigger.burst` owns the timing and this owns
            # nothing until it returns.
            pulses = trigger.burst(duration, interval)
            logger.info("relay_burst issued %d pulse(s)", pulses)
    finally:
        trigger.release_all()
        try:
            # Now the contact is open the tail can be chased properly.
            drained += owner.drain()
        except Exception:
            logger.warning("relay_burst: final drain failed", exc_info=True)
        if acquired:
            lock.release()
    logger.info("relay_burst drained %d frame(s) across the burst", drained)


def relay_bulb(trigger: RelayTrigger, seconds: float) -> None:
    """Hold a bulb exposure for ``seconds``."""
    seconds = float(seconds)
    logger.info("relay_bulb: %.3f s", seconds)
    trigger.bulb(seconds)


def relay_arm(trigger: RelayTrigger) -> None:
    """Close S1 and leave it closed.

    Pre-arming drops per-frame trigger latency from ~170 ms to the body's own
    ~45 ms and keeps it awake; ``pressed()`` recognises the held S1 and leaves
    it closed on the way out, so the arm survives shots and bursts.

    The exposure catch-up runs first, before S1 closes: a write into held
    contacts kills the drive, and the arm has scheduled slack to fix things in.
    """
    _catch_up_exposure(budget_s=0.8)
    logger.info("relay_arm: S1 held")
    trigger.half_press()


def relay_release(trigger: RelayTrigger) -> None:
    """Open every contact — the counterpart of relay_arm."""
    logger.info("relay_release")
    trigger.release_all()
