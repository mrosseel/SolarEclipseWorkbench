"""Fuji X Series camera adapter for Solar Eclipse Workbench.

Bridges the fujixsdk Python bindings into the workbench's BaseCamera
abstraction so Fuji cameras appear alongside gphoto2 cameras.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

from .camera import BaseCamera, CameraError

# Lazy import — fujixsdk may not be installed
try:
    import fujixsdk
    from fujixsdk import (
        Camera as SDKCamera,
        CameraIssue,
        EclipseShooter,
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
except ImportError:
    FUJIXSDK_AVAILABLE = False


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


def _get_speed_reverse() -> dict[str, int]:
    global _SPEED_REVERSE
    if not _SPEED_REVERSE and FUJIXSDK_AVAILABLE:
        _SPEED_REVERSE = _build_speed_reverse_map()
    return _SPEED_REVERSE


def _parse_shutter_speed(speed_str: str) -> Optional[int]:
    """Map workbench shutter speed string to fujixsdk constant."""
    rmap = _get_speed_reverse()
    clean = speed_str.strip().rstrip('"')
    val = rmap.get(clean)
    if val is not None:
        return val
    # Try with/without leading "1/" variations
    if clean.startswith("1/"):
        val = rmap.get(clean)
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
            import time
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
    """Adapter wrapping a fujixsdk.Camera into the workbench's BaseCamera interface."""

    vendor = 'Fuji'

    def __init__(self, sdk_cam: SDKCamera, name: str, sdk_path: str, device_name: str = "ENUM:0"):
        super().__init__(name=name)
        self._sdk_cam = sdk_cam
        self._sdk_path = sdk_path
        self._device_name = device_name
        self._shooter: Optional[EclipseShooter] = None
        self._lock = threading.RLock()
        self._connected = True

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
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
        """
        with self._lock:
            if 'iso' in kwargs and kwargs['iso'] is not None:
                iso_val = _parse_iso(kwargs['iso'])
                if iso_val is not None:
                    try:
                        self._sdk_cam.set_iso(iso_val)
                    except Exception as e:
                        logging.warning('Fuji: failed to set ISO %s: %s', kwargs['iso'], e)

            if 'aperture' in kwargs and kwargs['aperture'] is not None:
                ap_val = _parse_aperture(kwargs['aperture'])
                if ap_val is not None:
                    try:
                        self._sdk_cam.set_aperture(ap_val)
                    except Exception as e:
                        logging.warning('Fuji: failed to set aperture %s: %s', kwargs['aperture'], e)

            if 'shutter_speed' in kwargs and kwargs['shutter_speed'] is not None:
                speed_val = _parse_shutter_speed(str(kwargs['shutter_speed']))
                if speed_val is not None:
                    try:
                        self._sdk_cam.set_shutter_speed(speed_val)
                    except Exception as e:
                        logging.warning('Fuji: failed to set shutter speed %s: %s', kwargs['shutter_speed'], e)

    def capture(self):
        """Fire the shutter without AF. Retries once after reconnect on failure."""
        with self._lock:
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
            logging.info('Fuji camera reconnected successfully')
            return True
        except Exception as e:
            logging.error('Fuji reconnect failed: %s', e)
            return False

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
    def shooter(self) -> EclipseShooter:
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
        """
        try:
            current_speed, _ = self._sdk_cam.get_shutter_speed()
        except Exception:
            return [current_speed] if 'current_speed' in dir() else []

        supported = self._sdk_cam.get_supported_shutter_speeds()
        if current_speed not in supported:
            return [current_speed]

        idx = supported.index(current_speed)

        # Parse the step size from the steps string
        # "+/- 1" = 3 stops, "+/- 1 2/3" = 5 stops, "+/- 2" = 6 stops
        # Each 1/3 EV step ≈ 1 position in the supported speeds list
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
        return speeds


# ======================================================================
# Detection
# ======================================================================

def detect_fuji_cameras(sdk_path: str) -> dict[str, FujiCamera]:
    """Detect Fuji cameras via SDK. Returns {name: FujiCamera} dict."""
    if not FUJIXSDK_AVAILABLE:
        return {}

    try:
        cameras = SDKCamera.detect(sdk_path)
    except Exception as e:
        logging.debug('Fuji SDK detection failed: %s', e)
        return {}

    result = {}
    for info in cameras:
        name = f"Fujifilm {info.product}" if info.product != "(unknown)" else f"Fujifilm Camera ({info.device_name})"
        try:
            sdk_cam = SDKCamera(sdk_path, info.device_name)
            fuji_cam = FujiCamera(sdk_cam, name, sdk_path, info.device_name)
            result[name] = fuji_cam
            logging.info('Detected Fuji camera: %s (device=%s)', name, info.device_name)
        except Exception as e:
            logging.warning('Failed to open Fuji camera %s: %s', info.device_name, e)

    return result


# ======================================================================
# SDK path resolution
# ======================================================================

def find_fuji_sdk_path() -> Optional[str]:
    """Find the Fuji SDK library path.

    Checks in order:
    1. FUJI_SDK_PATH environment variable
    2. ConfigManager fuji_sdk_path setting
    3. Auto-detect: look for SDK dirs containing XAPI.so
    """
    # 1. Environment variable
    env_path = os.environ.get('FUJI_SDK_PATH')
    if env_path and Path(env_path).is_dir():
        return env_path

    # 2. ConfigManager setting
    try:
        from .location_ui import ConfigManager
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
            pkg_dir.parent.parent / "fujixsdk",
            pkg_dir.parent.parent.parent / "fujixsdk",
        ])
    except Exception:
        pass

    for base in search_dirs:
        if not base.is_dir():
            continue
        # Look for SDK* dirs containing the shared lib
        for sdk_dir in sorted(base.glob("SDK*")):
            if sdk_dir.is_dir() and list(sdk_dir.glob("**/XAPI.so")):
                return str(sdk_dir)
        # Or the base dir itself
        if list(base.glob("**/XAPI.so")):
            return str(base)

    return None
