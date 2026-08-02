"""Fuji X Series camera adapter for Solar Eclipse Workbench.

Bridges the fujixsdk Python bindings into the workbench's BaseCamera
abstraction (defined in ``camera.py``) so Fuji cameras appear alongside
gphoto2 cameras.  Fuji bodies are driven through Fujifilm's native Shooting
SDK rather than gphoto2/libgphoto2, which does not support tethered control of
the X series.

Detection (:func:`detect_fuji_cameras`) runs before gphoto2 claims the USB
device; :func:`find_fuji_sdk_path` locates the redistributable SDK libraries.
"""

from __future__ import annotations

import logging
import math
import os
import ctypes
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import hardware_problems
from .camera import BaseCamera, CameraError
from .hardware_registry import HARDWARE, register_hardware

# Lazy import — fujixsdk may not be installed / the SDK libs may be absent.
try:
    import fujixsdk
    from fujixsdk import (
        Camera as SDKCamera,
        CameraIssue,
        EclipseShooter,
        ensure_ld_library_path,
        validate_for_eclipse,
    )
    from fujixsdk._constants import (
        AE_MODE_NAMES,
        AE_OFF,
        FOCUS_MODE_NAMES,
        ISO_100,
        SHUTTER_SPEED_NAMES,
        SDK_FOCUS_MANUAL,
    )
    FUJIXSDK_AVAILABLE = True
    FUJIXSDK_IMPORT_ERROR = None
except ImportError as _exc:
    FUJIXSDK_AVAILABLE = False
    FUJIXSDK_IMPORT_ERROR = str(_exc)


# ======================================================================
# Relay-driven shooting
#
# With the drive dial on CH — which a total eclipse needs for its Baily's
# beads bursts — the SDK cannot fire the shutter at all (0x1008).  A relay
# on the release jack can, at the body's native 15 fps, so when one is
# connected the relay does the firing and the SDK is left to do what it is
# good at: exposure and draining.  Measured on the bench, 1 August 2026.
# ======================================================================

# 15 fps for longer than this reaches the 32-slot buffer before the drain that
# follows can run, and a full buffer stops the camera dead.
MAX_BURST_S = 1.9
CH_FPS = 15

# Contact closure per frame.  40 ms missed roughly 8% of taps; 80 ms never did.
TAP_S = 0.08

# Shortest useful gap between taps; long exposures extend it (see _tap_gap).
TAP_GAP_S = 0.35

# The session survives a drain only once the camera has genuinely stopped.
SETTLE_BEFORE_DRAIN_S = 1.0

# For about a second after a frame the body refuses exposure changes with
# 0x1006 while it writes to the card.  The busy clears by itself.
BUSY_RETRIES = 6
BUSY_BACKOFF_S = 0.3


def _retry_busy(action, what: str) -> bool:
    """Run a camera call that may be refused while the body is busy."""
    for attempt in range(BUSY_RETRIES):
        try:
            action()
            return True
        except Exception:
            if attempt + 1 == BUSY_RETRIES:
                logging.exception('%s failed after %d attempts', what, BUSY_RETRIES)
                return False
            time.sleep(BUSY_BACKOFF_S)
    return False


class _RelayShooter:
    """Relay-driven stand-in for EclipseShooter.

    Exposes the same two methods ``take_burst`` and ``take_bracket`` call, so
    neither needs to know which mechanism is firing the shutter.
    """

    def __init__(self, camera: FujiCamera):
        self.camera = camera

    def burst_no_download(self, count: int, min_interval_ms: int = 0) -> int:
        """Hold the release long enough for ``count`` frames at the CH rate."""
        seconds = min(count / CH_FPS, MAX_BURST_S)
        with self.camera.relay.pressed():
            time.sleep(seconds)
        # `pressed()` leaves S1 closed when the caller pre-armed, and draining
        # with S1 still held drops the session for good (0x2001).
        self.camera.relay.release_all()
        self.camera.drain()
        return int(seconds * CH_FPS)

    def bracket_no_download(self, speeds: list, iso=None, aperture=None) -> int:
        """One tap per speed, the speed set over USB between taps.

        ISO and aperture are deliberately ignored: ``configure`` has already
        applied them, and re-applying them here — as the SDK shooter does —
        would silently undo the ISO the caller asked for.
        """
        relay = self.camera.relay
        taken = 0
        relay.half_press()
        try:
            for speed in speeds:
                if not _retry_busy(lambda s=speed: self.camera._sdk_cam.set_shutter_speed(s),
                                   f'{self.camera.name}: set shutter speed {speed}'):
                    # Fire anyway: a frame at the previous speed beats no frame at
                    # all, and there is no second chance at a contact.  But the
                    # frame will look perfectly normal until it is reviewed, so the
                    # failure has to reach the user — quietly, through the
                    # indicator, never through a dialog while the script runs.
                    hardware_problems.report(
                        self.camera.name,
                        'Bracket frames were taken at the wrong shutter speed',
                        detail=f'{speed} could not be set; the frame was taken anyway',
                        severity='warning',
                    )
                relay.shoot(pulse=TAP_S)
                taken += 1
                # A slow frame must finish before the next speed is sent.
                time.sleep(max(TAP_GAP_S, speed / 1_000_000 + 0.3))
        finally:
            relay.release_all()
        self.camera.drain()
        return taken


# ======================================================================
# Shutter speed / aperture / ISO mapping (workbench string -> SDK int)
# ======================================================================

def _build_speed_reverse_map() -> dict[str, int]:
    """Build a reverse lookup from human-readable speed string to SDK constant.

    The workbench passes shutter speeds as strings like "1/2000".
    SHUTTER_SPEED_NAMES maps int->str with trailing quotes (e.g. '1/2000"').
    We strip the quote and also handle bare values.
    """
    if not FUJIXSDK_AVAILABLE:
        return {}
    rmap: dict[str, int] = {}
    for val, name in SHUTTER_SPEED_NAMES.items():
        if val == 0:
            continue
        clean = name.rstrip('"').strip()
        rmap[clean] = val
        # Also map without spaces
        rmap[clean.replace(" ", "")] = val
    return rmap


_SPEED_REVERSE: dict[str, int] = {}
_SPEED_BY_SECONDS: list[tuple[float, int]] = []


def _get_speed_reverse() -> dict[str, int]:
    global _SPEED_REVERSE
    if not _SPEED_REVERSE and FUJIXSDK_AVAILABLE:
        _SPEED_REVERSE = _build_speed_reverse_map()
    return _SPEED_REVERSE


def _speed_name_seconds(name: str) -> Optional[float]:
    """Seconds for one SHUTTER_SPEED_NAMES entry, or None if it is not a duration.

    Covers the three forms the table uses: '1/2000"', '1.6"' and '4min'.
    """
    clean = name.rstrip('"').strip()
    try:
        if clean.endswith("min"):
            return float(clean[:-3]) * 60.0
        if "/" in clean:
            num, _, den = clean.partition("/")
            return float(num) / float(den)
        return float(clean)
    except (ValueError, ZeroDivisionError):
        return None


def _get_speeds_by_seconds() -> list[tuple[float, int]]:
    global _SPEED_BY_SECONDS
    if not _SPEED_BY_SECONDS and FUJIXSDK_AVAILABLE:
        _SPEED_BY_SECONDS = sorted(
            (secs, val)
            for val, name in SHUTTER_SPEED_NAMES.items()
            if val > 0 and (secs := _speed_name_seconds(name)) is not None
        )
    return _SPEED_BY_SECONDS


# The camera's speeds are 1/3 EV apart, so anything closer than 1/6 EV to a rung
# is that rung written a different way.  Beyond that it is a value the schedule
# asked for and the body does not have, which the caller has to hear about.
_SPEED_MATCH_TOLERANCE = 2.0 ** (1.0 / 6.0)


def _parse_shutter_speed(speed_str: str) -> Optional[int]:
    """Map a workbench shutter speed string to a fujixsdk constant.

    The same exposure has several spellings — the table calls half a second
    '1/2"', a schedule of decimal-second corona frames calls it '0.5' — so an
    unmatched string falls back to matching on duration.
    """
    rmap = _get_speed_reverse()
    clean = speed_str.strip().rstrip('"')
    val = rmap.get(clean)
    if val is not None:
        return val

    wanted = _speed_name_seconds(clean)
    if wanted is None or wanted <= 0:
        return None

    grid = _get_speeds_by_seconds()
    if not grid:
        return None

    secs, val = min(grid, key=lambda pair: abs(math.log(pair[0] / wanted)))
    ratio = max(secs, wanted) / min(secs, wanted)
    if ratio > _SPEED_MATCH_TOLERANCE:
        return None
    if ratio > 1.0001:
        logging.warning(
            'Shutter speed %s is not on this camera\'s scale; using %s instead',
            speed_str, SHUTTER_SPEED_NAMES.get(val, val),
        )
    return val


def _parse_aperture(aperture_str: str) -> Optional[int]:
    """Map workbench aperture string (e.g. "5.6") to SDK int (f-number * 100)."""
    try:
        f_num = float(str(aperture_str))
        return int(round(f_num * 100))
    except (ValueError, TypeError):
        return None


def _parse_iso(iso_val) -> Optional[int]:
    """Map workbench ISO value (int or string) to SDK int."""
    try:
        return int(iso_val)
    except (ValueError, TypeError):
        return None


# ======================================================================
# GPhoto-compatible stubs
# ======================================================================

class _FujiWidgetStub:
    """Mimics gphoto2 widget's get_value()/set_value()/get_type() interface."""

    def __init__(self, name: str, value: Any):
        self._name = name
        self._value = value

    def get_value(self):
        return self._value

    def set_value(self, v: Any):
        self._value = v

    def get_type(self):
        try:
            import gphoto2 as gp
            return gp.GP_WIDGET_TEXT
        except ImportError:
            return 0


class _FujiConfigStub:
    """Mimics gphoto2 config's get_child_by_name() pattern.

    Maps widget names to real SDK values so existing helper functions
    (get_battery_level, get_focus_mode, etc.) work without modification.
    """

    def __init__(self, fuji_camera: FujiCamera):
        self._cam = fuji_camera

    def get_child_by_name(self, name: str) -> _FujiWidgetStub:
        name_lower = name.lower()
        sdk_cam = self._cam._sdk_cam

        if name_lower == 'batterylevel':
            try:
                level, _, _ = sdk_cam.get_battery_info()
                return _FujiWidgetStub(name, f"{level}%")
            except Exception:
                return _FujiWidgetStub(name, "Unknown")

        if name_lower == 'focusmode':
            try:
                fm = sdk_cam.get_focus_mode()
                fm_name = FOCUS_MODE_NAMES.get(fm, f"0x{fm:04X}")
                # Map to workbench-expected values
                if fm == SDK_FOCUS_MANUAL:
                    return _FujiWidgetStub(name, "Manual")
                return _FujiWidgetStub(name, fm_name)
            except Exception:
                return _FujiWidgetStub(name, "Manual")

        if name_lower in ('autoexposuremodedial', 'expprogram'):
            try:
                ae = sdk_cam.get_ae_mode()
                ae_name = AE_MODE_NAMES.get(ae, f"0x{ae:04X}")
                return _FujiWidgetStub(name, ae_name)
            except Exception:
                return _FujiWidgetStub(name, "Manual")

        if name_lower == 'shutterspeed':
            try:
                speed, _ = sdk_cam.get_shutter_speed()
                speed_name = SHUTTER_SPEED_NAMES.get(speed, str(speed))
                return _FujiWidgetStub(name, speed_name)
            except Exception:
                return _FujiWidgetStub(name, "")

        if name_lower == 'iso':
            try:
                iso = sdk_cam.get_iso()
                return _FujiWidgetStub(name, str(iso))
            except Exception:
                return _FujiWidgetStub(name, "")

        if name_lower in ('aperture', 'f-number'):
            try:
                ap = sdk_cam.get_aperture()
                return _FujiWidgetStub(name, f"{ap / 100:.1f}")
            except Exception:
                return _FujiWidgetStub(name, "")

        if name_lower in ('datetime', 'datetimeutc', 'd034'):
            return _FujiWidgetStub(name, time.strftime('%Y-%m-%d %H:%M:%S'))

        return _FujiWidgetStub(name, '')


class _FujiStorageEntry:
    """Mimics gphoto2 storage info entry with freekbytes/capacitykbytes."""

    def __init__(self, free_kb: float, capacity_kb: float):
        self.freekbytes = free_kb
        self.capacitykbytes = capacity_kb


# ======================================================================
# FujiCamera adapter
# ======================================================================

class FujiCamera(BaseCamera):
    """Adapter wrapping a fujixsdk.Camera into the workbench's BaseCamera interface.

    Exposure settings are applied through :meth:`configure`; single shots through
    :meth:`capture`; bursts and brackets through the :attr:`shooter`
    (:class:`fujixsdk.EclipseShooter`).  Gphoto-style ``get_config`` /
    ``get_storageinfo`` stubs let the workbench's vendor-agnostic helper
    functions (battery level, focus mode, free space, ...) work unmodified.
    """

    vendor = 'Fuji'
    connection_type = 'Fuji SDK'

    def __init__(self, sdk_cam: SDKCamera, name: str, sdk_path: str, device_name: str = "ENUM:0"):
        super().__init__(name=name)
        self._sdk_cam = sdk_cam
        self._sdk_path = sdk_path
        self._device_name = device_name
        self._shooter: Optional[EclipseShooter] = None
        self._lock = threading.RLock()
        self._connected = True
        # The ISO this session last wrote successfully; see configure().
        self._applied_iso: Optional[int] = None

    def connect(self) -> None:
        self._connected = True

    @property
    def max_relay_hold_s(self) -> float:
        """Longest the release may be held before the transfer queue overruns.

        Published so a bare ``relay_burst`` in a script — which knows nothing
        about the body on the other end of the cable — can be held to the same
        limit the Fuji burst path already respects.
        """
        return MAX_BURST_S

    def disconnect(self) -> None:
        if HARDWARE.get('sdk_camera') is self:
            register_hardware('sdk_camera', None)
        try:
            self._sdk_cam.close()
        except Exception:
            pass
        self._connected = False

    def configure(self, **kwargs: Any) -> None:
        """Apply camera settings via SDK.

        Accepts keyword arguments:
            shutter_speed: str (e.g. "1/2000")
            aperture: str (e.g. "5.6")
            iso: int or str (e.g. 100)

        Raises:
            CameraError: if any requested setting could not be applied, naming
                every one that failed.

        A setting that silently fails to apply is worse than one that fails
        loudly: the next frame is then taken at the previous exposure and looks
        perfectly normal until the images are reviewed.  Every failure is
        collected here — one bad value must not stop the others being tried —
        and reported together.
        """
        failures: list = []

        def _apply(name: str, parse, setter, raw) -> None:
            value = parse(raw)
            if value is None:
                failures.append(f"{name}={raw!r} is not a value this camera understands")
                return
            try:
                setter(value)
            except Exception as exc:
                failures.append(f"{name}={raw!r} rejected by the camera ({exc})")

        def _apply_iso(raw) -> None:
            """Write the ISO only when it is not the one already on the body.

            ``set_iso`` is refused with 0x1006 unless the transfer queue is
            empty, while ``set_shutter_speed`` tolerates pending frames.  A
            script holds one ISO for a whole phase, so writing it on every frame
            is dozens of USB round-trips that can only fail, never help.  The
            remembered value is cleared whenever a write fails or the session is
            rebuilt, so a skip never outlives its evidence.
            """
            value = _parse_iso(raw)
            if value is None:
                failures.append(f"ISO={raw!r} is not a value this camera understands")
                return
            if value == self._applied_iso:
                logging.debug('%s: ISO already %s, not writing it again', self.name, raw)
                return
            try:
                self._sdk_cam.set_iso(value)
                self._applied_iso = value
            except Exception as exc:
                self._applied_iso = None
                failures.append(f"ISO={raw!r} rejected by the camera ({exc})")

        with self._lock:
            if kwargs.get('iso') is not None:
                _apply_iso(kwargs['iso'])

            # A telescope has no electronic aperture, so a script says "-" and
            # the setting is skipped rather than failing every single frame.
            if kwargs.get('aperture') not in (None, '', '-'):
                _apply('aperture', _parse_aperture, self._sdk_cam.set_aperture, kwargs['aperture'])

            if kwargs.get('shutter_speed') is not None:
                _apply('shutter speed', lambda v: _parse_shutter_speed(str(v)),
                       self._sdk_cam.set_shutter_speed, kwargs['shutter_speed'])

        if failures:
            raise CameraError(
                f"{self.name}: could not apply " + "; ".join(failures)
            )

    @property
    def relay(self):
        """The relay trigger driving this body, or None if none is connected."""
        return HARDWARE.get('relay')

    def drain(self) -> int:
        """Discard the queued PC transfers once shooting has stopped.

        Every frame taken with an SDK session open holds one of 32 buffer
        slots until it is drained, and a full buffer stops the body dead —
        recoverable only by pulling the battery.  The images themselves are
        already on the card; only the transfer nobody asked for is discarded.
        Callers must have released the relay first: draining while the camera
        is still shooting drops the USB session for good.
        """
        time.sleep(SETTLE_BEFORE_DRAIN_S)
        try:
            return self._sdk_cam.drain_buffer()
        except Exception:
            logging.exception('%s: drain failed; shooting is unaffected', self.name)
            return 0

    def capture(self):
        """Fire the shutter, through the relay when one is connected.

        Retries once after reconnect on failure.
        """
        with self._lock:
            if self.relay is not None:
                self.relay.shoot(pulse=TAP_S)
                self.drain()
                return
            try:
                self._sdk_cam.shoot_no_af()
            except Exception as first_err:
                logging.warning('Fuji capture failed (%s), attempting reconnect...', first_err)
                if not self._reconnect():
                    raise
                try:
                    self._sdk_cam.shoot_no_af()
                except Exception:
                    logging.exception('Fuji capture failed again after reconnect')
                    raise

    def _reconnect(self) -> bool:
        """Attempt to close and reopen the SDK camera connection."""
        try:
            self._sdk_cam.close()
        except Exception:
            pass
        try:
            self._sdk_cam = SDKCamera(self._sdk_path, self._device_name)
            self._shooter = None
            # A new session knows nothing about what the old one wrote.
            self._applied_iso = None
            logging.info('Fuji camera reconnected successfully')
            return True
        except Exception as e:
            logging.error('Fuji reconnect failed: %s', e)
            return False

    def sync_clock(self) -> None:
        """Report that this body's clock cannot be written from here.

        The Shooting SDK's model headers list a SetDateTime API code, but the
        public headers declare no entry point and XAPI exports none, so there is
        nothing to call.  ``set_config`` below is a no-op, so the gphoto2 path is
        not an alternative either.

        Frames therefore carry whatever the body's own clock says: set it by hand
        and note the residual offset.
        """
        hardware_problems.report(
            self.name,
            'Camera clock cannot be set from the computer — set it on the body by hand',
            detail='the Fuji Shooting SDK exposes no date/time call, so frame '
                   'timestamps follow the camera clock, not this computer',
            severity='warning',
        )

    # gphoto-compatible stubs
    def get_config(self) -> _FujiConfigStub:
        return _FujiConfigStub(self)

    def set_config(self, config) -> None:
        pass

    def get_storageinfo(self) -> list:
        try:
            free_kb = self._sdk_cam.get_media_capacity()
            # SDK only returns free capacity; estimate total as 2x free
            # (we don't have a total capacity API)
            return [_FujiStorageEntry(float(free_kb), float(free_kb) * 2)]
        except Exception:
            return [_FujiStorageEntry(999.9 * 1024 * 1024, 999.9 * 1024 * 1024)]

    def exit(self):
        self.disconnect()

    # Fuji-specific
    @property
    def shooter(self):
        """Whatever can fire this body: the relay if one is connected, else the SDK."""
        if self.relay is not None:
            return _RelayShooter(self)
        if self._shooter is None:
            self._shooter = EclipseShooter(self._sdk_cam)
        return self._shooter

    def validate(self) -> list[CameraIssue]:
        return validate_for_eclipse(self._sdk_cam)

    def parse_bracket_speeds(self, steps_str: str) -> list[int]:
        """Parse a bracket steps string into SDK shutter speed constants.

        The workbench passes bracket steps like "+/- 1 2/3" for Canon AEB.
        For Fuji, we interpret this as EV steps around the current speed
        and return a list of SDK shutter speed constants.

        A semicolon-separated list of speeds ("1/2000;1/125;1/8;2") is taken
        literally instead.  A corona ladder spans some twelve stops in 2 EV
        steps, which the symmetric 1/3-EV form cannot express without firing
        dozens of redundant frames.
        """
        if ";" in steps_str:
            speeds, unknown = [], []
            for text in (part.strip() for part in steps_str.split(";")):
                if not text:
                    continue
                value = _parse_shutter_speed(text)
                (speeds if value is not None else unknown).append(value or text)
            if unknown:
                raise CameraError(
                    f"{self.name}: bracket lists shutter speeds this camera does "
                    f"not have: {', '.join(unknown)}"
                )
            return speeds

        # Both of these have to succeed for a bracket to mean anything.  The
        # previous version returned an empty list when the read failed, which
        # made the bracket take no frames at all without saying so.
        try:
            current_speed, _ = self._sdk_cam.get_shutter_speed()
            supported = self._sdk_cam.get_supported_shutter_speeds()
        except Exception as exc:
            raise CameraError(
                f"{self.name}: cannot build a bracket — the camera would not report its "
                f"shutter speed ({exc})"
            ) from exc

        if current_speed not in supported:
            # One frame at the speed already on the body: safe, but not the
            # bracket that was asked for, so it has to be said out loud.
            logging.warning(
                '%s: the camera reports it is at %s, which is not in the %d speeds it '
                'says it supports — bracketing %s collapses to a single frame',
                self.name, SHUTTER_SPEED_NAMES.get(current_speed, current_speed),
                len(supported), steps_str,
            )
            return [current_speed]

        idx = supported.index(current_speed)

        # Parse the step size from the steps string.  Positions are 1/3 EV
        # apart, so "+/- 1" spans 3 positions either side of the current speed
        # and yields 7 frames.
        try:
            clean = steps_str.replace("+/-", "").strip()
            if " " in clean:
                parts = clean.split()
                whole = int(parts[0])
                frac_parts = parts[1].split("/")
                frac = int(frac_parts[0]) / int(frac_parts[1])
                ev_steps = whole + frac
            else:
                ev_steps = float(clean)
            # Convert EV to 1/3 stop positions
            positions = int(round(ev_steps * 3))
        except (ValueError, IndexError):
            positions = 3  # default: +/- 1 EV

        speeds = []
        for offset in range(-positions, positions + 1):
            i = idx + offset
            if 0 <= i < len(supported):
                speeds.append(supported[i])

        wanted = 2 * positions + 1
        if len(speeds) < wanted:
            # Running off either end leaves the bracket lopsided around the base
            # exposure rather than symmetric, which the schedule did not assume.
            logging.warning(
                '%s: bracketing %s around %s wanted %d frames but the scale only '
                'reaches %d of them',
                self.name, steps_str, SHUTTER_SPEED_NAMES.get(current_speed, current_speed),
                wanted, len(speeds),
            )
        return speeds

    def describe_speeds(self, speeds: list[int]) -> str:
        """The human-readable ladder behind a list of SDK shutter constants."""
        return ', '.join(str(SHUTTER_SPEED_NAMES.get(s, s)) for s in speeds)


# ======================================================================
# LD_LIBRARY_PATH startup handling
# ======================================================================

def _reexec_process() -> None:
    """Restart the current process so the dynamic linker picks up an updated
    LD_LIBRARY_PATH (it is only read once, at process startup)."""
    logging.info('LD_LIBRARY_PATH updated for the Fuji SDK, restarting process')
    # Reconstruct the command, preserving a `python -m <module>` invocation.
    main_spec = getattr(sys.modules.get('__main__'), '__spec__', None)
    if main_spec and main_spec.name:
        args = [sys.executable, '-m', main_spec.name] + sys.argv[1:]
    else:
        args = [sys.executable] + sys.argv
    os.execvp(sys.executable, args)


def ensure_fuji_library_path() -> bool:
    """Add the Fuji SDK libraries (and their NixOS dependencies) to
    LD_LIBRARY_PATH if they are missing.

    Returns True if the environment was changed and the process must be
    re-exec'd for the dynamic linker to see it, False if nothing was needed.
    """
    if not FUJIXSDK_AVAILABLE:
        return False
    sdk_path = find_fuji_sdk_path()
    if not sdk_path:
        return False
    try:
        # ensure_ld_library_path returns True when the path was already
        # complete, False when it modified the environment.
        return not ensure_ld_library_path(sdk_path)
    except Exception:
        logging.debug('Fuji ensure_ld_library_path failed', exc_info=True)
        return False


def maybe_reexec_for_fuji_sdk() -> None:
    """Call once at application startup, before building the GUI or reading any
    session state.

    Ensures the Fuji SDK libraries are on LD_LIBRARY_PATH and re-execs the
    process immediately if they had to be added — so the one-time restart
    happens at launch rather than mid-session during camera detection, where it
    would discard the user's unsaved settings.
    """
    if ensure_fuji_library_path():
        _reexec_process()


# ======================================================================
# Detection
# ======================================================================

def _reset_mac_camera_stack() -> None:
    """Kill macOS's camera daemons so they respawn with fresh state.

    Not a pre-detect reflex — the daemons are the SDK's transport and must
    normally be left alone.  But a stale ICA session (from a crashed or
    just-closed connection) makes detect return zero cameras or a phantom
    handle whose every call fails 0x2001, and every recovery that has worked
    on the bench involved forcing fresh daemons.  They respawn on demand.
    """
    if platform.system() != "Darwin":
        return
    try:
        subprocess.run(["killall", "-9", "ptpcamerad", "mscamerad-xpc"],
                       capture_output=True, timeout=5)
        logging.info("Reset macOS camera daemons; they respawn on demand")
    except Exception:
        logging.debug("Camera daemon reset failed", exc_info=True)


def _preload_mac_transport(sdk_path: str) -> None:
    """Load the SDK's PTP transport dylibs before XAPI goes looking for them.

    FTLPTP.dylib carries the install name /usr/local/lib/FTLPTP.dylib, where it
    is typically not installed.  Loading it by full path first means XAPI's own
    dlopen resolves to the already-loaded image instead of the missing path.

    Note that ptpcamerad must be left alive on macOS: FTLPTP links
    ImageCaptureCore, whose broker that daemon is — the SDK talks to the camera
    *through* it.  Killing it (the reflex carried over from Linux, where gvfs
    really does steal the device) is self-sabotage here.
    """
    if platform.system() != "Darwin":
        return
    for hit in sorted(Path(sdk_path).rglob("FTLPTP.dylib")):
        for name in ("FTLPTP.dylib", "FTLPTPIP.dylib"):
            candidate = hit.parent / name
            if candidate.exists():
                try:
                    ctypes.CDLL(str(candidate), mode=ctypes.RTLD_GLOBAL)
                    logging.debug("Preloaded %s", candidate)
                except OSError:
                    logging.debug("Could not preload %s", candidate, exc_info=True)
        return


def _report_validation_issues(camera: FujiCamera) -> None:
    """Run the eclipse pre-flight check and surface anything it objects to.

    Done at detection rather than at first shot, which is the whole point: a
    camera left in AF, or on JPEG, or with exposure compensation dialled in, is
    trivial to fix while setting up and impossible to fix afterwards.
    """
    try:
        issues = camera.validate()
    except Exception:
        logging.debug('Fuji validation failed for %s', camera.name, exc_info=True)
        return

    for issue in issues or []:
        # "info" issues are statements of fact (the aperture in use, and so on),
        # not things to fix, so they stay in the log.
        if issue.severity == 'info':
            logging.info('%s: %s is %s', camera.name, issue.setting, issue.current)
            continue
        hardware_problems.report(
            camera.name,
            issue.message,
            detail=f"{issue.setting} is {issue.current}, expected {issue.expected}",
            severity=issue.severity,
        )

    if issues:
        logging.info('Fuji validation raised %d issue(s) for %s', len(issues), camera.name)


def detect_fuji_cameras(sdk_path: str) -> dict[str, FujiCamera]:
    """Detect Fuji cameras via SDK. Returns {name: FujiCamera} dict.

    Retries detection up to 3 times with a delay after killing ptpcamerad,
    because the USB device needs time to become available.
    """
    if not FUJIXSDK_AVAILABLE:
        # Returning silently here once cost a whole bench sitting: the script
        # reported "no camera" when the truth was "the wrapper never imported".
        logging.error("fujixsdk is not importable (%s) — the SDK was never tried. "
                      "Run from the repo root or put it on sys.path.",
                      FUJIXSDK_IMPORT_ERROR)
        return {}

    _preload_mac_transport(sdk_path)

    # Retry — after killing ptpcamerad the USB device needs a moment
    cameras = []
    for attempt in range(3):
        if attempt > 0:
            # A failed attempt usually means a stale ICA session is holding the
            # body; forcing fresh daemons is the only recovery that has worked.
            _reset_mac_camera_stack()
            time.sleep(3.0)
        try:
            cameras = SDKCamera.detect(sdk_path)
            logging.info('Fuji SDK detect attempt %d returned %d camera(s)',
                         attempt + 1, len(cameras))
            if cameras:
                break
        except fujixsdk.LDPathError:
            # Safety net: the SDK signalled LD_LIBRARY_PATH needs updating
            # mid-run.  maybe_reexec_for_fuji_sdk() at startup normally prevents
            # ever reaching this point.
            _reexec_process()
        except Exception as e:
            logging.debug('Fuji SDK detect attempt %d failed: %s', attempt + 1, e)
        time.sleep(2)

    if not cameras:
        logging.warning('Fuji SDK found no cameras after retries')
        return {}

    result = {}
    for info in cameras:
        # Use "Fuji Fujifilm <model>" to match gphoto2's naming convention
        name = f"Fuji Fujifilm {info.product}" if info.product != "(unknown)" else f"Fuji Camera ({info.device_name})"
        try:
            sdk_cam = SDKCamera(sdk_path, info.device_name)
            fuji_cam = FujiCamera(sdk_cam, name, sdk_path, info.device_name)
            result[name] = fuji_cam
            # The relay commands in a script get a trigger, not a camera, so the
            # open SDK session has to be findable from there: it is the session
            # that queues a transfer per frame, and so the session that decides
            # how long the release may be held and has to be drained afterwards.
            register_hardware('sdk_camera', fuji_cam)
            logging.info('Detected Fuji camera: %s (device=%s)', name, info.device_name)
            _report_validation_issues(fuji_cam)
        except Exception as e:
            logging.warning('Failed to open Fuji camera %s: %s', info.device_name, e)
            hardware_problems.report(
                'Fuji SDK',
                f'Found {name} but could not open it',
                detail=str(e),
            )

    return result


# ======================================================================
# SDK path resolution
# ======================================================================

def _sdk_marker() -> str:
    """The name of the SDK's main library on this platform.

    Linux ships XAPI.so, Windows XAPI.dll, and macOS a XAPI.bundle directory —
    so searching for the Linux name alone finds nothing on a Mac.
    """
    system = platform.system()
    if system == "Darwin":
        return "XAPI.bundle"
    if system == "Windows":
        return "XAPI.dll"
    return "XAPI.so"


def find_fuji_sdk_path() -> Optional[str]:
    """Find the Fuji SDK library path.

    Checks in order:
    1. FUJI_SDK_PATH environment variable
    2. ConfigManager fuji_sdk_path setting (if available)
    3. Auto-detect: look for SDK dirs containing the platform's XAPI library
    """
    # 1. Environment variable
    env_path = os.environ.get('FUJI_SDK_PATH')
    if env_path and Path(env_path).is_dir():
        return env_path

    # 2. ConfigManager setting (optional — older configs may not store one)
    try:
        from solareclipseworkbench.location_ui import ConfigManager
        cfg = ConfigManager()
        cfg_path = cfg.get_fuji_sdk_path()
        if cfg_path and Path(cfg_path).is_dir():
            return cfg_path
    except Exception:
        pass

    # 3. Auto-detect in common locations
    search_dirs = [
        Path.home() / "fujixsdk",
        Path.home() / "FujiSDK",
        Path("/opt/fujixsdk"),
        Path("/usr/local/lib/fujixsdk"),
    ]
    # Also look relative to the workbench install
    try:
        import solareclipseworkbench
        pkg_dir = Path(solareclipseworkbench.__file__).parent
        search_dirs.extend([
            pkg_dir.parent.parent / "SDK",
            pkg_dir.parent.parent / "fujixsdk",
            pkg_dir.parent.parent.parent / "SDK",
            pkg_dir.parent.parent.parent / "fujixsdk",
        ])
    except Exception:
        pass

    marker = _sdk_marker()
    for base in search_dirs:
        if not base.is_dir():
            continue
        # Look for SDK* dirs containing the shared lib
        for sdk_dir in sorted(base.glob("SDK*")):
            if sdk_dir.is_dir() and list(sdk_dir.glob(f"**/{marker}")):
                return str(sdk_dir)
        # Or the base dir itself
        if list(base.glob(f"**/{marker}")):
            return str(base)

    return None
