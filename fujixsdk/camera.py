"""High-level Camera class — main user interface for Fujifilm X SDK."""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import _constants as C
from ._errors import BusyError, check_result, raise_for_error_code

log = logging.getLogger(__name__)
from ._library import XAPILibrary, ensure_ld_library_path
from ._structures import (
    DeviceInformation,
    ImageInformation,
    LensInformation,
)


@dataclass
class CameraInfo:
    """Summary of a detected camera."""
    product: str
    serial_no: str
    ip_address: str
    framework: str
    device_name: str


class Camera:
    """Pythonic interface to a Fujifilm X Series camera via the Shooting SDK.

    Usage:
        cameras = Camera.detect("/path/to/sdk/libs")
        with Camera("/path/to/sdk/libs", cameras[0].device_name) as cam:
            cam.set_priority(PRIORITY_PC)
            cam.set_ae_mode(AE_OFF)
            cam.set_shutter_speed(SHUTTER_1_1000)
            cam.set_iso(ISO_100)
            cam.shoot()
    """

    _lib: Optional[XAPILibrary] = None
    _lib_sdk_path: Optional[str] = None
    _init_count: int = 0
    _init_lock: threading.Lock = threading.Lock()

    @classmethod
    def _ensure_lib(cls, sdk_path: str | Path) -> XAPILibrary:
        """Lazily load and initialize the SDK library (shared across instances)."""
        with cls._init_lock:
            sdk_str = str(sdk_path)
            if cls._lib is None or cls._lib_sdk_path != sdk_str:
                cls._check_ld_path(sdk_path)
                cls._lib = XAPILibrary(sdk_path)
                cls._lib_sdk_path = sdk_str
                cls._init_count = 0

            if cls._init_count == 0:
                rc = cls._lib.XSDK_Init(cls._lib_handle())
                check_result(rc)

            cls._init_count += 1
            return cls._lib

    @classmethod
    def _lib_handle(cls) -> ctypes.c_void_p:
        """Return the dlopen handle for XSDK_Init."""
        return ctypes.c_void_p(cls._lib._lib._handle)

    @staticmethod
    def _check_ld_path(sdk_path: str | Path):
        """Ensure LD_LIBRARY_PATH is set; raise if re-exec is needed."""
        import platform
        if platform.system() != "Linux":
            return
        if not ensure_ld_library_path(sdk_path):
            raise RuntimeError(
                "LD_LIBRARY_PATH was not set. It has been updated in the "
                "environment, but the process must be restarted for it to "
                "take effect. Re-run your script, or set LD_LIBRARY_PATH "
                f"before starting Python:\n"
                f"  export LD_LIBRARY_PATH=\"{os.environ['LD_LIBRARY_PATH']}\""
            )

    @classmethod
    def _release_lib(cls):
        """Decrement reference count and exit SDK when no cameras remain."""
        with cls._init_lock:
            cls._init_count -= 1
            if cls._init_count <= 0 and cls._lib is not None:
                cls._lib.XSDK_Exit()
                cls._init_count = 0

    @staticmethod
    def detect(sdk_path: str | Path, interface: int = C.IF_USB) -> list[CameraInfo]:
        """Detect connected cameras.

        For USB cameras, returns one CameraInfo per detected camera with
        device_name set to "ENUM:0", "ENUM:1", etc.  Pass this device_name
        to Camera() to open the connection.

        Args:
            sdk_path: Path to directory containing SDK shared libraries.
            interface: Connection interface (IF_USB, IF_WIFI_LOCAL, IF_WIFI_IP).

        Returns:
            List of CameraInfo for each detected camera.
        """
        Camera._check_ld_path(sdk_path)
        lib = XAPILibrary(sdk_path)
        rc = lib.XSDK_Init(ctypes.c_void_p(lib._lib._handle))
        check_result(rc)

        try:
            count = ctypes.c_long(0)

            rc = lib.XSDK_Detect(
                ctypes.c_long(interface), None, None, ctypes.byref(count)
            )
            check_result(rc)

            if count.value == 0:
                return []

            # For USB, the SDK uses "ENUM:N" as device identifiers.
            # We open each camera briefly to read its device info.
            results = []
            for i in range(count.value):
                device_name = f"ENUM:{i}"
                cam_handle = ctypes.c_void_p()
                cam_mode = ctypes.c_long()

                rc = lib.XSDK_OpenEx(
                    device_name.encode("utf-8"),
                    ctypes.byref(cam_handle),
                    ctypes.byref(cam_mode),
                    None,
                )
                if rc != 0:
                    results.append(CameraInfo(
                        product="(unknown)",
                        serial_no="",
                        ip_address="",
                        framework="USB",
                        device_name=device_name,
                    ))
                    continue

                try:
                    info = DeviceInformation()
                    lib.XSDK_GetDeviceInfo(cam_handle, ctypes.byref(info))
                    results.append(CameraInfo(
                        product=info.product,
                        serial_no=info.serial_no,
                        ip_address="",
                        framework="USB",
                        device_name=device_name,
                    ))
                finally:
                    lib.XSDK_Close(cam_handle)

            return results
        finally:
            lib.XSDK_Exit()

    def __init__(
        self,
        sdk_path: str | Path,
        device_name: str,
        interface: int = C.IF_USB,
    ):
        self._sdk_path = sdk_path
        self._interface = interface
        self._handle = ctypes.c_void_p()
        self._camera_mode = ctypes.c_long()
        self._closed = False

        self._lib_inst = self._ensure_lib(sdk_path)

        # The SDK requires Detect before OpenEx to enumerate USB devices.
        count = ctypes.c_long(0)
        self._lib_inst.XSDK_Detect(
            ctypes.c_long(interface), None, None, ctypes.byref(count)
        )

        rc = self._lib_inst.XSDK_OpenEx(
            device_name.encode("utf-8"),
            ctypes.byref(self._handle),
            ctypes.byref(self._camera_mode),
            None,
        )
        check_result(rc)

        # Clean up any stale state from a previous crashed session.
        # SetPriorityMode fails with BusyError while images are pending,
        # and sometimes the camera's internal pipeline is stuck from a
        # previous session. Strategy: cancel, drain, try priority; if still
        # blocked, fire a shot to flush the pipeline, drain again, retry.
        self._cleanup_stale_state()

    def _cleanup_stale_state(self):
        """Reset camera to a clean state on session open.

        The camera may retain release/priority state from a crashed session.
        SetPriorityMode fails while images are in the buffer or while the
        internal processing pipeline has unfinished work. To clear this:
        1. RELEASE_CANCEL to clear any half-press state
        2. Drain pending images from the volatile buffer
        3. Try SetPriorityMode(CAMERA)
        4. If still blocked, fire a dummy shot to flush the pipeline,
           drain the resulting images, and retry
        """
        shot_opt = ctypes.c_long(1)
        af_status = ctypes.c_long()

        # Step 1: Cancel any pending release
        self._lib_inst.XSDK_Release(
            self._handle, ctypes.c_long(C.RELEASE_CANCEL),
            ctypes.byref(shot_opt), ctypes.byref(af_status))

        # Step 2: Drain + try priority (may succeed on first try)
        self.drain_buffer()
        rc = self._lib_inst.XSDK_SetPriorityMode(
            self._handle, ctypes.c_long(C.PRIORITY_CAMERA))
        if rc == C.COMPLETE:
            log.debug("Session opened, stale state cleared")
            return

        # Step 3: Pipeline is stuck — fire a shot to flush it
        log.info("Priority blocked; firing flush shot to clear pipeline")
        self._lib_inst.XSDK_Release(
            self._handle, ctypes.c_long(C.RELEASE_S1ON),
            ctypes.byref(shot_opt), ctypes.byref(af_status))
        time.sleep(0.15)
        self._lib_inst.XSDK_Release(
            self._handle, ctypes.c_long(C.RELEASE_S2_S1OFF),
            ctypes.byref(shot_opt), ctypes.byref(af_status))
        time.sleep(0.5)

        # Step 4: Drain the flush shot images + retry priority
        for attempt in range(20):
            self.drain_buffer()
            rc = self._lib_inst.XSDK_SetPriorityMode(
                self._handle, ctypes.c_long(C.PRIORITY_CAMERA))
            if rc == C.COMPLETE:
                log.debug("Session opened, stale state cleared (after flush shot)")
                return
            time.sleep(0.5)

        log.warning("Could not reset priority after flush; camera may need power cycle")

    def wait_ready(self, timeout_s: float = 10.0, poll_interval_s: float = 0.3) -> bool:
        """Wait until the camera is no longer busy.

        Polls GetBufferCapacity as a lightweight health check.
        Returns True if camera became ready, False on timeout.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                self.get_buffer_capacity()
                return True
            except BusyError:
                log.debug("Camera busy, waiting %.1fs...", poll_interval_s)
                time.sleep(poll_interval_s)
        log.warning("Camera still busy after %.1fs timeout", timeout_s)
        return False

    def close(self):
        """Close the camera connection and release SDK resources.

        Drains pending images and returns priority to camera before
        closing, so the camera is not left in a stuck state.
        """
        if not self._closed:
            self._closed = True
            try:
                shot_opt = ctypes.c_long(1)
                af_status = ctypes.c_long()
                self._lib_inst.XSDK_Release(
                    self._handle, ctypes.c_long(C.RELEASE_CANCEL),
                    ctypes.byref(shot_opt), ctypes.byref(af_status))
                # Drain buffer — SetPriorityMode fails while images pending
                self.drain_buffer()
                self._lib_inst.XSDK_SetPriorityMode(
                    self._handle, ctypes.c_long(C.PRIORITY_CAMERA))
            except Exception:
                pass
            self._lib_inst.XSDK_Close(self._handle)
            self._release_lib()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        if not self._closed:
            self.close()

    @property
    def handle(self) -> ctypes.c_void_p:
        return self._handle

    @property
    def camera_mode(self) -> int:
        return self._camera_mode.value

    # ------------------------------------------------------------------
    # Error info
    # ------------------------------------------------------------------
    def get_error(self) -> tuple[int, int]:
        """Get last error details. Returns (api_code, error_code)."""
        api_code = ctypes.c_long()
        err_code = ctypes.c_long()
        self._lib_inst.XSDK_GetErrorNumber(
            self._handle, ctypes.byref(api_code), ctypes.byref(err_code)
        )
        return api_code.value, err_code.value

    def _check(self, rc: int):
        """Check return code; on failure, retrieve and raise specific error."""
        if rc == C.COMPLETE:
            return
        api_code, err_code = self.get_error()
        raise_for_error_code(err_code, api_code)

    # ------------------------------------------------------------------
    # Device information
    # ------------------------------------------------------------------
    @property
    def device_info(self) -> DeviceInformation:
        info = DeviceInformation()
        self._check(self._lib_inst.XSDK_GetDeviceInfo(self._handle, ctypes.byref(info)))
        return info

    @property
    def lens_info(self) -> LensInformation:
        info = LensInformation()
        self._check(self._lib_inst.XSDK_GetLensInfo(self._handle, ctypes.byref(info)))
        return info

    @property
    def firmware_version(self) -> str:
        buf = ctypes.create_string_buffer(256)
        self._check(self._lib_inst.XSDK_GetFirmwareVersion(self._handle, buf))
        return buf.value.decode("utf-8", errors="replace")

    def get_sdk_version(self) -> str:
        buf = ctypes.create_string_buffer(256)
        self._lib_inst.XSDK_GetVersionString(buf)
        return buf.value.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # Priority mode
    # ------------------------------------------------------------------
    def set_priority(self, mode: int):
        self._check(self._lib_inst.XSDK_SetPriorityMode(self._handle, ctypes.c_long(mode)))

    def get_priority(self) -> int:
        val = ctypes.c_long()
        self._check(self._lib_inst.XSDK_GetPriorityMode(self._handle, ctypes.byref(val)))
        return val.value

    # ------------------------------------------------------------------
    # AE mode
    # ------------------------------------------------------------------
    def set_ae_mode(self, mode: int):
        self._check(self._lib_inst.XSDK_SetAEMode(self._handle, ctypes.c_long(mode)))

    def get_ae_mode(self) -> int:
        val = ctypes.c_long()
        self._check(self._lib_inst.XSDK_GetAEMode(self._handle, ctypes.byref(val)))
        return val.value

    # ------------------------------------------------------------------
    # Shutter speed
    # ------------------------------------------------------------------
    def set_shutter_speed(self, speed: int, bulb: int = 0):
        self._check(self._lib_inst.XSDK_SetShutterSpeed(
            self._handle, ctypes.c_long(speed), ctypes.c_long(bulb)
        ))

    def get_shutter_speed(self) -> tuple[int, int]:
        """Returns (speed, bulb)."""
        speed = ctypes.c_long()
        bulb = ctypes.c_long()
        self._check(self._lib_inst.XSDK_GetShutterSpeed(
            self._handle, ctypes.byref(speed), ctypes.byref(bulb)
        ))
        return speed.value, bulb.value

    def get_supported_shutter_speeds(self) -> list[int]:
        num = ctypes.c_long()
        # First call to get count
        speeds = (ctypes.c_long * 1024)()
        bulb_capable = ctypes.c_long()
        self._check(self._lib_inst.XSDK_CapShutterSpeed(
            self._handle, ctypes.byref(num), speeds, ctypes.byref(bulb_capable)
        ))
        return [speeds[i] for i in range(num.value)]

    # ------------------------------------------------------------------
    # ISO sensitivity
    # ------------------------------------------------------------------
    def set_iso(self, value: int):
        self._check(self._lib_inst.XSDK_SetSensitivity(self._handle, ctypes.c_long(value)))

    def get_iso(self) -> int:
        val = ctypes.c_long()
        self._check(self._lib_inst.XSDK_GetSensitivity(self._handle, ctypes.byref(val)))
        return val.value

    def get_supported_iso(self) -> list[int]:
        num = ctypes.c_long()
        values = (ctypes.c_long * 256)()
        self._check(self._lib_inst.XSDK_CapSensitivity(
            self._handle, ctypes.byref(num), values
        ))
        return [values[i] for i in range(num.value)]

    # ------------------------------------------------------------------
    # Aperture
    # ------------------------------------------------------------------
    def set_aperture(self, f_number: int):
        self._check(self._lib_inst.XSDK_SetAperture(self._handle, ctypes.c_long(f_number)))

    def get_aperture(self) -> int:
        val = ctypes.c_long()
        self._check(self._lib_inst.XSDK_GetAperture(self._handle, ctypes.byref(val)))
        return val.value

    # ------------------------------------------------------------------
    # Exposure bias
    # ------------------------------------------------------------------
    def set_exposure_bias(self, bias: int):
        self._check(self._lib_inst.XSDK_SetExposureBias(self._handle, ctypes.c_long(bias)))

    def get_exposure_bias(self) -> int:
        val = ctypes.c_long()
        self._check(self._lib_inst.XSDK_GetExposureBias(self._handle, ctypes.byref(val)))
        return val.value

    # ------------------------------------------------------------------
    # White balance
    # ------------------------------------------------------------------
    def set_wb_mode(self, mode: int):
        self._check(self._lib_inst.XSDK_SetWBMode(self._handle, ctypes.c_long(mode)))

    def get_wb_mode(self) -> int:
        val = ctypes.c_long()
        self._check(self._lib_inst.XSDK_GetWBMode(self._handle, ctypes.byref(val)))
        return val.value

    def set_wb_color_temp(self, kelvin: int):
        self._check(self._lib_inst.XSDK_SetWBColorTemp(self._handle, ctypes.c_long(kelvin)))

    def get_wb_color_temp(self) -> int:
        val = ctypes.c_long()
        self._check(self._lib_inst.XSDK_GetWBColorTemp(self._handle, ctypes.byref(val)))
        return val.value

    # ------------------------------------------------------------------
    # Drive mode
    # ------------------------------------------------------------------
    def set_drive_mode(self, mode: int):
        self._check(self._lib_inst.XSDK_SetDriveMode(self._handle, ctypes.c_long(mode)))

    def get_drive_mode(self) -> int:
        val = ctypes.c_long()
        self._check(self._lib_inst.XSDK_GetDriveMode(self._handle, ctypes.byref(val)))
        return val.value

    # ------------------------------------------------------------------
    # Dynamic range
    # ------------------------------------------------------------------
    def set_dynamic_range(self, dr: int):
        self._check(self._lib_inst.XSDK_SetDynamicRange(self._handle, ctypes.c_long(dr)))

    def get_dynamic_range(self) -> int:
        val = ctypes.c_long()
        self._check(self._lib_inst.XSDK_GetDynamicRange(self._handle, ctypes.byref(val)))
        return val.value

    # ------------------------------------------------------------------
    # Metering mode
    # ------------------------------------------------------------------
    def set_metering_mode(self, mode: int):
        self._check(self._lib_inst.XSDK_SetMeteringMode(self._handle, ctypes.c_long(mode)))

    def get_metering_mode(self) -> int:
        val = ctypes.c_long()
        self._check(self._lib_inst.XSDK_GetMeteringMode(self._handle, ctypes.byref(val)))
        return val.value

    # ------------------------------------------------------------------
    # Release / Capture
    # ------------------------------------------------------------------
    def _release(self, mode: int, shot_count: int = 1) -> tuple[int, int]:
        """Issue a Release command. Returns (shot_opt, af_status).

        Args:
            mode: Release mode constant (RELEASE_S1ON, RELEASE_S2_S1OFF, etc.)
            shot_count: Number of frames to request in burst modes (plShotOpt IN).
                        Must be >= 1; the SDK treats 0 as invalid in burst drive modes.
        """
        shot_opt = ctypes.c_long(shot_count)
        af_status = ctypes.c_long()
        self._check(self._lib_inst.XSDK_Release(
            self._handle,
            ctypes.c_long(mode),
            ctypes.byref(shot_opt),
            ctypes.byref(af_status),
        ))
        return shot_opt.value, af_status.value

    def shoot(self) -> tuple[int, int]:
        """Take a photo with AF + AE. Returns (shot_opt, af_status).

        Uses the S1ON→S2_S1OFF sequence which is compatible with all
        Fujifilm cameras including the X-T4 in tether mode.
        """
        self._release(C.RELEASE_S1ON)
        return self._release(C.RELEASE_S2_S1OFF)

    def shoot_no_af(self) -> tuple[int, int]:
        """Take a photo without AF. Returns (shot_opt, af_status).

        Uses S1ON → S2_S1OFF sequence. S1ON may return an error
        (e.g. AF fail) but still transitions the camera to S1 state,
        which is required before S2 can fire the shutter.
        """
        # S1ON puts the camera in half-press state; log but don't raise
        # (AF fail is expected when shooting without AF intent)
        shot_opt = ctypes.c_long(1)
        af_status = ctypes.c_long()
        rc = self._lib_inst.XSDK_Release(
            self._handle,
            ctypes.c_long(C.RELEASE_S1ON),
            ctypes.byref(shot_opt),
            ctypes.byref(af_status),
        )
        if rc != C.COMPLETE:
            api_code, err_code = self.get_error()
            log.debug("S1ON rc=%d err=0x%08X api=0x%08X", rc, err_code, api_code)
        # Camera needs time to transition to S1 state before S2 can fire.
        # Too short (<0.1s) causes frequent ShootErrors; 0.15s is the sweet spot.
        time.sleep(0.15)
        return self._release(C.RELEASE_S2_S1OFF)

    def half_press(self):
        """S1 on (half-press shutter: AF + AE lock)."""
        shot_opt = ctypes.c_long()
        af_status = ctypes.c_long()
        self._check(self._lib_inst.XSDK_Release(
            self._handle,
            ctypes.c_long(C.RELEASE_S1ON),
            ctypes.byref(shot_opt),
            ctypes.byref(af_status),
        ))

    def full_press(self) -> tuple[int, int]:
        """S2 (full press: take shot after S1). Returns (shot_opt, af_status)."""
        shot_opt = ctypes.c_long()
        af_status = ctypes.c_long()
        self._check(self._lib_inst.XSDK_Release(
            self._handle,
            ctypes.c_long(C.RELEASE_S2),
            ctypes.byref(shot_opt),
            ctypes.byref(af_status),
        ))
        return shot_opt.value, af_status.value

    def release_all(self):
        """Cancel all active release operations (S1 off, AF off, etc.)."""
        shot_opt = ctypes.c_long()
        af_status = ctypes.c_long()
        self._check(self._lib_inst.XSDK_Release(
            self._handle,
            ctypes.c_long(C.RELEASE_CANCEL),
            ctypes.byref(shot_opt),
            ctypes.byref(af_status),
        ))

    def bulb_start(self):
        """Begin bulb exposure."""
        shot_opt = ctypes.c_long()
        af_status = ctypes.c_long()
        self._check(self._lib_inst.XSDK_Release(
            self._handle,
            ctypes.c_long(C.RELEASE_BULB_ON),
            ctypes.byref(shot_opt),
            ctypes.byref(af_status),
        ))

    def bulb_stop(self):
        """End bulb exposure."""
        shot_opt = ctypes.c_long()
        af_status = ctypes.c_long()
        self._check(self._lib_inst.XSDK_Release(
            self._handle,
            ctypes.c_long(C.RELEASE_N_BULBOFF),
            ctypes.byref(shot_opt),
            ctypes.byref(af_status),
        ))

    def get_release_status(self) -> int:
        val = ctypes.c_long()
        self._check(self._lib_inst.XSDK_GetReleaseStatus(self._handle, ctypes.byref(val)))
        return val.value

    # ------------------------------------------------------------------
    # Image download
    # ------------------------------------------------------------------
    def get_buffer_capacity(self) -> tuple[int, int]:
        """Returns (captured_count, total_capacity).

        captured_count: number of images currently in the volatile buffer.
        total_capacity: maximum number of images the buffer can hold.
        Buffer is full when captured_count >= total_capacity.
        """
        shoot = ctypes.c_long()
        total = ctypes.c_long()
        self._check(self._lib_inst.XSDK_GetBufferCapacity(
            self._handle, ctypes.byref(shoot), ctypes.byref(total)
        ))
        return shoot.value, total.value

    def read_image_info(self) -> ImageInformation:
        info = ImageInformation()
        self._check(self._lib_inst.XSDK_ReadImageInfo(self._handle, ctypes.byref(info)))
        return info

    def read_image(self, size: int) -> bytes:
        buf = (ctypes.c_ubyte * size)()
        self._check(self._lib_inst.XSDK_ReadImage(
            self._handle, buf, ctypes.c_ulong(size)
        ))
        return bytes(buf)

    def read_preview(self, size: int) -> bytes:
        buf = (ctypes.c_ubyte * size)()
        self._check(self._lib_inst.XSDK_ReadPreview(
            self._handle, buf, ctypes.c_ulong(size)
        ))
        return bytes(buf)

    def delete_image(self):
        self._check(self._lib_inst.XSDK_DeleteImage(self._handle))

    def drain_buffer(self) -> int:
        """Delete all pending images from the volatile buffer.

        Returns the number of images drained. Logs each attempt.
        """
        captured, total = self.get_buffer_capacity()
        if captured <= 0:
            return 0

        drained = 0
        for i in range(captured):
            try:
                info = self.read_image_info()
                fmt = info.format & 0xFF
                log.debug("Buffer entry %d: format=0x%04X size=%d", i, info.format, info.data_size)
                if fmt == C.IMAGEFORMAT_NONE:
                    log.debug("No more images in queue (IMAGEFORMAT_NONE)")
                    break
                self.delete_image()
                drained += 1
            except BusyError:
                log.warning("Camera busy during drain at entry %d, waiting...", i)
                time.sleep(0.5)
                try:
                    self.delete_image()
                    drained += 1
                except Exception:
                    break
            except XSDKError as e:
                log.warning("Drain stopped at entry %d: %s", i, e)
                break

        if drained:
            log.info("Drained %d/%d pending images from buffer", drained, captured)
        else:
            log.debug("No images to drain (buffer: %d/%d)", captured, total)
        return drained

    def download_image(self, output_path: str | Path) -> str:
        """Convenience: read image info, download full image, save to disk.

        Returns the output file path as a string.
        """
        info = self.read_image_info()
        data = self.read_image(info.data_size)
        output_path = Path(output_path)
        output_path.write_bytes(data)
        return str(output_path)

    # ------------------------------------------------------------------
    # Extended properties (model-dependent via CapProp/SetProp/GetProp)
    # ------------------------------------------------------------------
    def set_prop(self, api_code: int, api_param: int, *args):
        """Call XSDK_SetProp with variadic arguments.

        Pass ctypes-typed values for additional arguments, e.g.:
            cam.set_prop(API_CODE_SetFilmSimulationMode, 0, ctypes.c_long(mode))
        """
        rc = self._lib_inst.XSDK_SetProp(
            self._handle, ctypes.c_long(api_code), ctypes.c_long(api_param), *args
        )
        self._check(rc)

    def get_prop(self, api_code: int, api_param: int, *args):
        """Call XSDK_GetProp with variadic arguments.

        Pass ctypes pointer arguments for output, e.g.:
            val = ctypes.c_long()
            cam.get_prop(API_CODE_GetFilmSimulationMode, 0, ctypes.byref(val))
        """
        rc = self._lib_inst.XSDK_GetProp(
            self._handle, ctypes.c_long(api_code), ctypes.c_long(api_param), *args
        )
        self._check(rc)

    def cap_prop(self, api_code: int, api_param: int, *args):
        """Call XSDK_CapProp with variadic arguments.

        Pass ctypes pointer arguments for output, e.g.:
            num = ctypes.c_long()
            values = (ctypes.c_long * 64)()
            cam.cap_prop(API_CODE_CapFilmSimulationMode, 0, ctypes.byref(num), values)
        """
        rc = self._lib_inst.XSDK_CapProp(
            self._handle, ctypes.c_long(api_code), ctypes.c_long(api_param), *args
        )
        self._check(rc)

    # ------------------------------------------------------------------
    # Focus mode (extended API)
    # ------------------------------------------------------------------
    def get_focus_mode(self) -> int:
        val = ctypes.c_long()
        self.get_prop(C.API_CODE_GetFocusMode, 0, ctypes.byref(val))
        return val.value

    def set_focus_mode(self, mode: int):
        self.set_prop(C.API_CODE_SetFocusMode, 0, ctypes.c_long(mode))

    # ------------------------------------------------------------------
    # Image quality (extended API)
    # ------------------------------------------------------------------
    def get_image_quality(self) -> int:
        val = ctypes.c_long()
        self.get_prop(C.API_CODE_GetImageQuality, 0, ctypes.byref(val))
        return val.value

    def set_image_quality(self, quality: int):
        self.set_prop(C.API_CODE_SetImageQuality, 0, ctypes.c_long(quality))

    # ------------------------------------------------------------------
    # Long exposure NR (extended API)
    # ------------------------------------------------------------------
    def get_long_exposure_nr(self) -> int:
        val = ctypes.c_long()
        self.get_prop(C.API_CODE_GetLongExposureNR, 0, ctypes.byref(val))
        return val.value

    def set_long_exposure_nr(self, mode: int):
        self.set_prop(C.API_CODE_SetLongExposureNR, 0, ctypes.c_long(mode))

    # ------------------------------------------------------------------
    # IS mode (image stabilization, extended API)
    # ------------------------------------------------------------------
    def get_is_mode(self) -> int:
        val = ctypes.c_long()
        self.get_prop(C.API_CODE_GetISMode, 0, ctypes.byref(val))
        return val.value

    def set_is_mode(self, mode: int):
        self.set_prop(C.API_CODE_SetISMode, 0, ctypes.c_long(mode))

    # ------------------------------------------------------------------
    # Live View (extended API)
    # ------------------------------------------------------------------
    def start_live_view(self):
        self.set_prop(C.API_CODE_StartLiveView, 0)

    def stop_live_view(self):
        self.set_prop(C.API_CODE_StopLiveView, 0)

    def set_live_view_size(self, size: int):
        self.set_prop(C.API_CODE_SetLiveViewImageSize, 0, ctypes.c_long(size))

    def get_live_view_size(self) -> int:
        val = ctypes.c_long()
        self.get_prop(C.API_CODE_GetLiveViewImageSize, 0, ctypes.byref(val))
        return val.value

    def get_live_view_status(self) -> int:
        val = ctypes.c_long()
        self.get_prop(C.API_CODE_GetLiveViewStatus, 0, ctypes.byref(val))
        return val.value

    # ------------------------------------------------------------------
    # Battery info (extended API)
    # ------------------------------------------------------------------
    def get_battery_info(self) -> tuple[int, int, int]:
        """Returns (level, a, b) from CheckBatteryInfo.

        level is the battery percentage (0-100 scale, camera-dependent).
        """
        level = ctypes.c_long()
        a = ctypes.c_long()
        b = ctypes.c_long()
        self.get_prop(
            C.API_CODE_CheckBatteryInfo, 0,
            ctypes.byref(level), ctypes.byref(a), ctypes.byref(b),
        )
        return level.value, a.value, b.value

    # ------------------------------------------------------------------
    # Media status / capacity (extended API)
    # ------------------------------------------------------------------
    def get_media_status(self) -> int:
        val = ctypes.c_long()
        self.get_prop(C.API_CODE_GetMediaStatus, 0, ctypes.byref(val))
        return val.value

    def get_media_capacity(self) -> int:
        """Returns free capacity in KB."""
        val = ctypes.c_long()
        self.get_prop(C.API_CODE_GetMediaCapacity, 0, ctypes.byref(val))
        return val.value

    # ------------------------------------------------------------------
    # Shutter count (extended API)
    # ------------------------------------------------------------------
    def get_shutter_count(self) -> int:
        val = ctypes.c_long()
        self.get_prop(C.API_CODE_GetShutterCount, 0, ctypes.byref(val))
        return val.value

    # ------------------------------------------------------------------
    # Command dial status (extended API)
    # ------------------------------------------------------------------
    def get_command_dial_status(self) -> int:
        val = ctypes.c_long()
        self.get_prop(C.API_CODE_GetCommandDialStatus, 0, ctypes.byref(val))
        return val.value
