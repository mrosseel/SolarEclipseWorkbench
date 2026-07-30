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
import threading
import time
import weakref
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

import serial
import serial.tools.list_ports

try:
    import hid
except ImportError:
    hid = None

logger = logging.getLogger(__name__)

# Time to let the camera wake and meter after S1 closes, before S2 is asserted.
# Below roughly 100 ms the first frame of a sequence arrives late or not at all.
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
    """A USB relay board.  Channels are 1-based throughout."""

    name = "backend"

    @abstractmethod
    def set_channel(self, channel: int, closed: bool) -> None:
        """Close or open one channel."""

    @abstractmethod
    def describe(self) -> str:
        """Human-readable identification, for logs and the bench console."""

    def close(self) -> None:
        """Release the underlying device."""


class LcusSerialBackend(Backend):
    """LCUS-1 / LCUS-2 style boards behind a CH340.

    Protocol is four bytes: 0xA0, channel, state, checksum, where the checksum is
    the low byte of the sum of the preceding three.
    """

    name = "lcus"

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


class NumatoSerialBackend(Backend):
    """Numato Lab USB relay boards, which take plain-text commands.

    Numato numbers its relays from zero, so the 1-based channel is shifted here
    to keep the rest of the module consistent.
    """

    name = "numato"

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


class HidRelayBackend(Backend):
    """dcttech-style HID relay boards, which expose no serial port."""

    name = "hid"

    def __init__(self, vendor_id: int = 0x16C0, product_id: int = 0x05DF):
        if hid is None:
            raise RelayError("the 'hid' package is required for HID relay boards")
        try:
            self._device = hid.device()
            self._device.open(vendor_id, product_id)
        except Exception as exc:
            raise RelayError(f"cannot open HID relay {vendor_id:04x}:{product_id:04x}: {exc}") from exc
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


class SimulatedBackend(Backend):
    """Stand-in for a real board, for rehearsing scripts with no hardware.

    Mirrors the virtual camera: the whole schedule can be exercised indoors, and
    the contact state is readable so tests can assert on it.
    """

    name = "simulated"

    def __init__(self, latency_s: float = 0.006):
        self.latency_s = latency_s
        self.state: dict = {}

    def set_channel(self, channel: int, closed: bool) -> None:
        time.sleep(self.latency_s)
        self.state[channel] = closed
        logger.debug("Simulated relay: channel %d %s", channel, "closed" if closed else "open")

    def describe(self) -> str:
        return "simulated relay (no hardware)"


def find_relay_ports() -> list:
    """Serial ports that look like a relay board.

    Returns a list of (device, description) tuples, best guesses first.  A USB
    serial adapter is not proof of a relay — it is a starting point for the bench
    console to probe.
    """
    candidates = []
    for port in serial.tools.list_ports.comports():
        ident = (port.vid, port.pid)
        if ident in KNOWN_SERIAL_ADAPTERS:
            candidates.append((port.device, f"{KNOWN_SERIAL_ADAPTERS[ident]} — {port.description}"))
        elif port.vid is not None:
            candidates.append((port.device, port.description or "unknown USB serial"))
    return candidates


def make_backend(kind: str = "auto", port: Optional[str] = None) -> Backend:
    """Build a backend by name, or guess one.

    ``kind`` is one of auto, lcus, numato, hid, simulated.  Guessing prefers a
    Numato board when the adapter identifies as one, an LCUS board on any other
    USB serial adapter, and an HID board when no serial port is present.
    """
    kind = kind.lower()

    if kind == "simulated":
        return SimulatedBackend()
    if kind == "lcus":
        return LcusSerialBackend(port or _require_single_port())
    if kind == "numato":
        return NumatoSerialBackend(port or _require_single_port())
    if kind == "hid":
        return HidRelayBackend()
    if kind != "auto":
        raise RelayError(f"unknown relay backend: {kind}")

    if port:
        return LcusSerialBackend(port)

    ports = find_relay_ports()
    if ports:
        device, description = ports[0]
        if "numato" in description.lower():
            return NumatoSerialBackend(device)
        return LcusSerialBackend(device)

    if hid is not None:
        for vid, pid in HID_RELAY_IDS:
            try:
                return HidRelayBackend(vid, pid)
            except RelayError:
                continue

    raise RelayError(
        "no relay found — pass an explicit port, or use the simulated backend to rehearse without hardware"
    )


def _require_single_port() -> str:
    ports = find_relay_ports()
    if not ports:
        raise RelayError("no USB serial ports found")
    return ports[0][0]


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
        for channel in self.wiring.channels:
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
        """
        settle_s = self.wiring.settle_s if settle is None else settle
        try:
            self.half_press()
            if not self.wiring.is_single_channel and settle_s > 0:
                time.sleep(settle_s)
            self._set(self.wiring.s2_channel, True)
            yield
        finally:
            self.release_all()

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
    """
    duration = float(duration)
    interval = None if interval in (None, "") else float(interval)
    logger.info("relay_burst: %.3f s (interval %s)", duration, interval)
    pulses = trigger.burst(duration, interval)
    logger.info("relay_burst issued %d pulse(s)", pulses)


def relay_bulb(trigger: RelayTrigger, seconds: float) -> None:
    """Hold a bulb exposure for ``seconds``."""
    seconds = float(seconds)
    logger.info("relay_bulb: %.3f s", seconds)
    trigger.bulb(seconds)
