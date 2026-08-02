"""USB relay shutter trigger.

Fires a camera through the wired remote-release jack (2.5 mm TRS on Fujifilm X
bodies) by closing relay contacts, instead of driving the shutter over USB.

The relay's contacts are dry, so they carry no polarity and are electrically
isolated from the host — the same guarantee a mechanical cable release gives.
Two wiring layouts are supported:

    single channel   tip and ring joined to one NO contact.  Closing the contact
                     is a full press; the camera free-runs in whatever drive mode
                     it is set to for as long as the contact is held.

    dual channel     ring on one NO contact (S1, half press) and tip on another
                     (S2, full press).  Allows an explicit half-press settle
                     before each frame, and per-frame control of the interval.

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
from solareclipseworkbench.serial_ports import usb_serial_ports

try:
    import hid
except ImportError:
    hid = None

logger = logging.getLogger(__name__)

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

    @classmethod
    def discover(cls) -> list:
        """Any USB serial adapter that is not identifiably something else.

        These boards answer nothing, so they cannot be probed — the only way to
        confirm one is to pulse a channel and listen for the click.
        """
        candidates = []
        for port in usb_serial_ports():
            description = port.description or "USB serial"
            if any(vendor in _port_text(port) for vendor in ("numato", "dsd")):
                continue
            adapter = KNOWN_SERIAL_ADAPTERS.get((port.vid, port.pid))
            candidates.append(Candidate(
                kind="relay", driver=cls.name, target=port.device,
                description=f"{adapter} — {description}" if adapter else description,
                config={"port": port.device},
            ))
        return candidates

    def __init__(self, port: str, baudrate: int = 9600, timeout: float = 0.2):
        self.port = port
        try:
            self._serial = serial.Serial(port, baudrate, timeout=timeout)
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
            self._serial = serial.Serial(port, baudrate, timeout=timeout)
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
    ``AT+CH1=0`` opens it.  The firmware misparses a trailing CR/LF, so no
    terminator is sent.
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
            self._serial = serial.Serial(port, baudrate, timeout=timeout)
        except serial.SerialException as exc:
            raise RelayError(f"cannot open relay on {port}: {exc}") from exc

    def set_channel(self, channel: int, closed: bool) -> None:
        command = f"AT+CH{channel}={1 if closed else 0}".encode("ascii")
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
    atexit.register(_release_all_contacts)
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous = signal.getsignal(signum)

            def handler(sig, frame, _previous=previous):
                _release_all_contacts()
                if callable(_previous):
                    _previous(sig, frame)
                else:
                    raise KeyboardInterrupt if sig == signal.SIGINT else SystemExit(1)

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

def relay_shoot(trigger: RelayTrigger) -> None:
    """Take a single frame through the relay."""
    logger.info("relay_shoot")
    trigger.shoot()


def relay_burst(trigger: RelayTrigger, duration: float, interval: Optional[float] = None) -> None:
    """Hold the shutter for ``duration`` seconds, or pulse it at ``interval``.

    Arguments arrive from the script as strings, so they are coerced before use.

    A held burst runs at the body's own continuous rate with the host out of the
    loop, so it can outrun the transfer queue of an open SDK session — 15 fps
    fills 32 slots in a little over two seconds, and a full queue stops the
    camera dead in the middle of totality.  The hold is therefore capped at
    whatever that session says is safe, and the queue is drained afterwards.
    Both are no-ops when no SDK session is open: without one there is no queue.
    """
    duration = float(duration)
    interval = None if interval in (None, "") else float(interval)

    owner = HARDWARE.get('sdk_camera')
    limit = getattr(owner, 'max_relay_hold_s', None)
    if limit is not None and interval is None and duration > limit:
        logger.warning("relay_burst: %.3f s would overrun the %s transfer queue, "
                       "holding %.3f s instead", duration, getattr(owner, 'name', 'camera'), limit)
        hardware_problems.report(
            getattr(owner, 'name', 'camera'),
            'A relay burst in the script is longer than the camera can buffer',
            detail=f'{duration:.2f} s shortened to {limit:.2f} s',
            severity='warning',
        )
        duration = limit

    logger.info("relay_burst: %.3f s (interval %s)", duration, interval)
    pulses = trigger.burst(duration, interval)
    logger.info("relay_burst issued %d pulse(s)", pulses)

    if owner is not None:
        # Every contact open first.  A burst that was pre-armed leaves S1 closed
        # on the way out — that is the point of pre-arming — but draining with S1
        # still held drops the USB session for good with 0x2001, proven twice on
        # the bench.  Re-arming before the next burst is free; losing exposure
        # control in the middle of totality is not.
        trigger.release_all()
        owner.drain()


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
    """
    logger.info("relay_arm: S1 held")
    trigger.half_press()


def relay_release(trigger: RelayTrigger) -> None:
    """Open every contact — the counterpart of relay_arm."""
    logger.info("relay_release")
    trigger.release_all()
