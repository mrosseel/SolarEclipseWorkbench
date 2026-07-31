"""Eclipse-specific shooting helpers for Fujifilm X cameras.

Pre-configured exposure settings for each phase of a solar eclipse,
camera validation, live view streaming, and high-speed no-download shooting.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from . import _constants as C
from ._errors import BusyError, XSDKError
from .camera import Camera

log = logging.getLogger(__name__)


# ======================================================================
# Camera validation
# ======================================================================

@dataclass
class CameraIssue:
    """A single camera configuration issue found during validation."""
    severity: str  # "error", "warning", "info"
    setting: str
    current: str
    expected: str
    message: str


def validate_for_eclipse(cam: Camera) -> list[CameraIssue]:
    """Check all camera settings and report what needs to change for eclipse shooting."""
    issues = []

    # --- ERRORS (will prevent eclipse shooting) ---

    # AE mode must be Manual
    try:
        ae = cam.get_ae_mode()
        ae_name = C.AE_MODE_NAMES.get(ae, f"0x{ae:04X}")
        if ae != C.AE_OFF:
            issues.append(CameraIssue(
                "error", "AE Mode", ae_name, "Manual",
                "Set AE mode to Manual (M on mode dial)",
            ))
    except XSDKError:
        pass

    # Focus must be Manual
    try:
        fm = cam.get_focus_mode()
        fm_name = C.FOCUS_MODE_NAMES.get(fm, f"0x{fm:04X}")
        if fm != C.SDK_FOCUS_MANUAL:
            issues.append(CameraIssue(
                "error", "Focus Mode", fm_name, "MF",
                "Switch lens/body focus selector to M",
            ))
    except XSDKError as exc:
        # Swallowing this hides the setting most likely to cost frames: a body
        # left in AF refuses S2 whenever focus does not confirm.  Say that the
        # check could not run rather than implying it passed.
        issues.append(CameraIssue(
            "warning", "Focus Mode", f"unreadable ({exc})", "MF",
            "Could not read the focus mode - check the selector is on M by hand",
        ))

    # ISO must be a fixed value.  On auto the body meters every frame, which for
    # a corona means metering off a mostly black sky, and every ISO the script
    # asks for is ignored.
    try:
        iso = cam.get_iso()
        if iso < 0:
            issues.append(CameraIssue(
                "error", "ISO", f"Auto ({iso})", "a fixed value",
                "ISO is on Auto: turn the ISO dial to C so the script can set it",
            ))
    except XSDKError:
        pass

    # Camera must be in tether mode
    try:
        mode = cam.camera_mode
        if not (mode & 0x0001):
            issues.append(CameraIssue(
                "error", "Camera Mode", f"0x{mode:04X}", "Tether",
                "Set USB mode to TETHER in camera connection settings",
            ))
    except XSDKError:
        pass

    # --- WARNINGS (suboptimal) ---

    # Drive mode: the SDK reports only S/MOVIE/INVALID on a dial-equipped body.
    # DRIVE_MODE_S means stills rather than single-frame drive - an X-T4 with the
    # dial physically on CH still reads back 0x0004 - so CH and CL cannot be told
    # apart from here and the dial has to be checked by eye.  This is worth
    # getting right because a relay burst holds the shutter contacts closed and
    # lets the body free-run: on Single that is one frame per contact, not 37.
    try:
        dm = cam.get_drive_mode()
        if dm == C.DRIVE_MODE_MOVIE:
            issues.append(CameraIssue(
                "error", "Drive Mode", "Movie", "CH",
                "Camera is in Movie mode. Set drive dial to CH.",
            ))
        elif dm == C.DRIVE_MODE_INVALID:
            issues.append(CameraIssue(
                "error", "Drive Mode", "Invalid", "CH",
                "Drive mode dial in unsupported position. Set to CH.",
            ))
        else:
            issues.append(CameraIssue(
                "info", "Drive Mode", "Stills",
                "CH (verify on camera dial)",
                "SDK cannot distinguish CH/CL/Single. Verify the drive dial is on CH.",
            ))
    except XSDKError:
        pass

    # Exposure bias should be 0
    try:
        eb = cam.get_exposure_bias()
        if eb != 0:
            ev = eb / 30.0
            issues.append(CameraIssue(
                "warning", "Exposure Bias", f"{ev:+.1f} EV", "0 EV",
                "Reset exposure compensation to 0",
            ))
    except XSDKError:
        pass

    # Image quality must be RAW alone.  Measured on an X-T4 at 1/1000: recording
    # a JPEG alongside the RAW put two entries in the volatile buffer per frame
    # and dropped the rate from 1.85 fps to 0.61 - so it both halves the number
    # of frames the buffer holds and takes three times as long to get them.  The
    # old check accepted FINE+RAW and NORMAL+RAW silently.
    #
    # The SDK exposes no RAW compression property, so lossless-compressed versus
    # uncompressed cannot be checked here; set it on the body and see fuji-xt4.md.
    try:
        iq = cam.get_image_quality()
        iq_name = C.IMAGE_QUALITY_NAMES.get(iq, f"0x{iq:04X}")
        if iq in (C.IMAGE_QUALITY_FINE_PLUS_RAW, C.IMAGE_QUALITY_NORMAL_PLUS_RAW):
            issues.append(CameraIssue(
                "error", "Image Quality", iq_name, "RAW",
                "A JPEG per frame doubles buffer use and triples the time per "
                "frame - set image quality to RAW alone",
            ))
        elif iq != C.IMAGE_QUALITY_RAW:
            issues.append(CameraIssue(
                "error", "Image Quality", iq_name, "RAW",
                "Set image quality to RAW",
            ))
    except XSDKError as exc:
        issues.append(CameraIssue(
            "warning", "Image Quality", f"unreadable ({exc})", "RAW",
            "Could not read image quality - confirm RAW, lossless compressed, "
            "no JPEG, on the camera by hand",
        ))

    # White balance: Daylight
    try:
        wb = cam.get_wb_mode()
        wb_name = C.WB_MODE_NAMES.get(wb, f"0x{wb:04X}")
        if wb != C.WB_DAYLIGHT:
            issues.append(CameraIssue(
                "warning", "White Balance", wb_name, "Daylight",
                "Set WB to Daylight for consistent color across bracket",
            ))
    except XSDKError:
        pass

    # Long exposure NR should be OFF
    try:
        nr = cam.get_long_exposure_nr()
        if nr != C.OFF:
            issues.append(CameraIssue(
                "warning", "Long Exposure NR", "ON", "OFF",
                "Turn off Long Exposure NR (wastes time between shots)",
            ))
    except XSDKError:
        pass

    # --- INFO (nice to know) ---

    # Current exposure settings
    try:
        speed, _ = cam.get_shutter_speed()
        iso = cam.get_iso()
        aperture = cam.get_aperture()
        speed_name = C.SHUTTER_SPEED_NAMES.get(speed, f"{speed}")
        f_str = f"f/{aperture / 100:.1f}" if aperture > 0 else "Auto"
        issues.append(CameraIssue(
            "info", "Exposure", f"{speed_name}  ISO {iso}  {f_str}", "",
            "Current exposure settings",
        ))
    except XSDKError:
        pass

    # Buffer capacity
    try:
        captured, total_frames = cam.get_buffer_capacity()
        available = total_frames - captured
        issues.append(CameraIssue(
            "info", "Buffer", f"{available}/{total_frames} available", "",
            "Available buffer capacity",
        ))
    except XSDKError:
        pass

    # Battery
    try:
        level, _, _ = cam.get_battery_info()
        issues.append(CameraIssue(
            "info", "Battery", f"{level}%", "",
            "Battery level",
        ))
    except XSDKError:
        pass

    # Card space
    try:
        capacity_kb = cam.get_media_capacity()
        capacity_gb = capacity_kb / (1024 * 1024)
        issues.append(CameraIssue(
            "info", "Card", f"{capacity_gb:.1f} GB free", "",
            "Available card space",
        ))
    except XSDKError:
        pass

    # Shutter count
    try:
        count = cam.get_shutter_count()
        issues.append(CameraIssue(
            "info", "Shutter Count", f"{count} actuations", "",
            "Total shutter actuations",
        ))
    except XSDKError:
        pass

    # Lens info
    try:
        lens = cam.lens_info
        issues.append(CameraIssue(
            "info", "Lens", lens.product_name or lens.model, "",
            "Attached lens",
        ))
    except XSDKError:
        pass

    return issues


def print_validation_report(cam: Camera) -> list[CameraIssue]:
    """Run validation and print a formatted status report. Returns the issues."""
    issues = validate_for_eclipse(cam)

    # Header
    try:
        info = cam.device_info
        fw = cam.firmware_version
        print(f"\n=== Eclipse Camera Validation ===")
        print(f"Camera: {info.product} (S/N: {info.serial_no})  Firmware: {fw}")
    except XSDKError:
        print("\n=== Eclipse Camera Validation ===")

    # Collect by severity
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]
    infos = [i for i in issues if i.severity == "info"]

    if errors:
        print(f"\nERRORS (must fix):")
        for i in errors:
            print(f"  [!] {i.setting}: {i.current} -> {i.expected}")
    if warnings:
        print(f"\nWARNINGS (recommended):")
        for i in warnings:
            print(f"  [~] {i.setting}: {i.current} -> Should be {i.expected}")
    if infos:
        print(f"\nOK:")
        for i in infos:
            print(f"  [+] {i.setting}: {i.current}")

    if not errors and not warnings:
        print("\nAll checks passed!")

    print()
    return issues


# ======================================================================
# Live View Stream
# ======================================================================

class LiveViewStream:
    """Stream live view JPEG frames from camera for focus preview."""

    _LIVE_FORMATS = {
        C.IMAGEFORMAT_LIVE,
        C.IMAGEFORMAT_LIVE_90,
        C.IMAGEFORMAT_LIVE_180,
        C.IMAGEFORMAT_LIVE_270,
    }

    def __init__(
        self,
        camera: Camera,
        size: int = C.LIVEVIEW_SIZE_XGA,
        quality: int = C.LIVEVIEW_QUALITY_FINE,
    ):
        self.camera = camera
        self.size = size
        self.quality = quality
        self._running = False

    def start(self):
        try:
            self.camera.set_live_view_size(self.size)
        except XSDKError as e:
            log.warning("Could not set live view size: %s", e)
        try:
            self.camera.set_live_view_quality(self.quality)
        except XSDKError as e:
            log.warning("Could not set live view quality: %s", e)
        self.camera.start_live_view()
        self._running = True

    def stop(self):
        self._running = False
        try:
            self.camera.stop_live_view()
        except XSDKError:
            pass

    def read_frame(self) -> bytes | None:
        """Read one JPEG frame. Returns None if no frame ready."""
        if not self._running:
            return None
        try:
            info = self.camera.read_image_info()
            fmt = info.format & 0xFF
            if fmt == (C.IMAGEFORMAT_LIVE & 0xFF) and info.data_size > 0:
                return self.camera.read_image(info.data_size)
        except XSDKError:
            pass
        return None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


# ======================================================================
# Eclipse Shooter
# ======================================================================

class EclipseShooter:
    """Pre-configured shooting for solar eclipse high-speed photography.

    Usage:
        with Camera(sdk_path, device_name) as cam:
            shooter = EclipseShooter(cam)
            shooter.configure_for_partial()
            cam.shoot_no_af()
            # ... at C2 ...
            shooter.configure_for_totality_corona()
            shooter.bracket_no_download([
                C.SHUTTER_1_2000, C.SHUTTER_1_500, C.SHUTTER_1_125,
                C.SHUTTER_1_30, C.SHUTTER_1_4, C.SHUTTER_1_2, C.SHUTTER_1,
            ])
    """

    def __init__(self, camera: Camera):
        self.camera = camera

    def _configure(
        self,
        iso: int,
        shutter: int,
        aperture: Optional[int] = None,
        wb: int = C.WB_DAYLIGHT,
    ):
        """Apply a complete exposure configuration."""
        self.camera.set_ae_mode(C.AE_OFF)
        self.camera.set_iso(iso)
        self.camera.set_shutter_speed(shutter)
        if aperture is not None:
            self.camera.set_aperture(aperture)
        self.camera.set_wb_mode(wb)

    def configure_for_partial(
        self,
        iso: int = C.ISO_100,
        shutter: int = C.SHUTTER_1_1000,
        aperture: Optional[int] = None,
    ):
        """Partial eclipse phases (C1-C2, C3-C4). Solar filter on."""
        self._configure(iso, shutter, aperture)

    def configure_for_diamond_ring(
        self,
        iso: int = C.ISO_100,
        shutter: int = C.SHUTTER_1_4000,
        aperture: Optional[int] = None,
    ):
        """Diamond ring effect — brief, very bright. Filter off."""
        self._configure(iso, shutter, aperture)

    def configure_for_baily_beads(
        self,
        iso: int = C.ISO_200,
        shutter: int = C.SHUTTER_1_8000,
        aperture: Optional[int] = None,
    ):
        """Baily's beads — fast shutter to freeze the beads."""
        self._configure(iso, shutter, aperture)

    def configure_for_totality_corona(
        self,
        iso: int = C.ISO_200,
        shutter: int = C.SHUTTER_1_2,
        aperture: Optional[int] = None,
    ):
        """Outer corona during totality — long exposure for faint detail."""
        self._configure(iso, shutter, aperture)

    def configure_for_totality_inner(
        self,
        iso: int = C.ISO_100,
        shutter: int = C.SHUTTER_1_250,
        aperture: Optional[int] = None,
    ):
        """Inner corona during totality — moderate exposure."""
        self._configure(iso, shutter, aperture)

    def configure_for_chromosphere(
        self,
        iso: int = C.ISO_100,
        shutter: int = C.SHUTTER_1_2000,
        aperture: Optional[int] = None,
    ):
        """Chromosphere — thin red layer, fast shutter."""
        self._configure(iso, shutter, aperture)

    def configure_for_prominences(
        self,
        iso: int = C.ISO_100,
        shutter: int = C.SHUTTER_1_500,
        aperture: Optional[int] = None,
    ):
        """Solar prominences — moderate-fast shutter."""
        self._configure(iso, shutter, aperture)

    # ------------------------------------------------------------------
    # High-speed no-download shooting
    # ------------------------------------------------------------------
    def wait_until_idle(self, timeout_s: float = 3.0, poll_s: float = 0.02) -> bool:
        """Block until the body is no longer mid-release.

        ``Camera.wait_ready`` polls GetBufferCapacity, which keeps answering
        happily while the shutter is refusing to fire, so it does not tell us
        anything useful here.  GetReleaseStatus does: the SHOOTING bit stays set
        until the body has finished with the previous frame.

        Measured on an X-T4 at 1/1000: firing without this, seven of ten frames
        came back ShootError within 0.15 s while the body was still busy.

        Returns False on timeout, and True if the status cannot be read - an
        unreadable status is no reason to refuse to take the picture.
        """
        from ._errors import BusyError, XSDKError as _XSDKError

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if not (self.camera.get_release_status() & C.RELEASE_STATUS_SHOOTING):
                    return True
            except BusyError:
                pass
            except _XSDKError:
                return True
            time.sleep(poll_s)
        log.warning("Camera still mid-release after %.1f s", timeout_s)
        return False

    #: Fraction of the volatile buffer that may fill before it is cleared.
    #: Nothing else empties it: shooting no-download leaves every frame queued
    #: for a PC transfer that never comes, and at 32 slots a run of any length
    #: simply stops.  Draining costs 0.018 s per image on an X-T4, so clearing
    #: two dozen mid-totality is under half a second.
    DRAIN_AT = 0.75

    def _keep_buffer_clear(self) -> None:
        """Clear the volatile buffer before it fills.

        The images are on the card - this only discards the queued PC transfer.
        """
        captured, total = self.camera.get_buffer_capacity()
        if total > 0 and captured >= total * self.DRAIN_AT:
            drained = self.camera.drain_buffer()
            log.info("Drained %d image(s) at %d/%d to keep shooting",
                     drained, captured, total)

    def shoot_fast(self, retries: int = 5) -> bool:
        """Fire one shot, no download. Returns False if buffer full."""
        self._keep_buffer_clear()
        captured, total = self.camera.get_buffer_capacity()
        if captured >= total:
            log.warning("Buffer full (%d/%d), cannot shoot", captured, total)
            return False
        from ._errors import ShootError
        for attempt in range(retries):
            # Waiting for the body to finish the previous frame turns most of
            # the ShootError retries below into shots that simply work.
            self.wait_until_idle()
            try:
                self.camera.shoot_no_af()
                return True
            except BusyError:
                log.debug("Camera busy on shoot attempt %d/%d", attempt + 1, retries)
                time.sleep(0.3)
            except ShootError:
                log.warning("ShootError on attempt %d/%d, retrying", attempt + 1, retries)
                time.sleep(0.2)
        log.error("Camera still failing after %d shoot attempts", retries)
        return False

    def burst_no_download(self, count: int, min_interval_ms: int = 0) -> int:
        """Fire N shots without downloading. Returns actual shots taken."""
        taken = 0
        interval_s = min_interval_ms / 1000.0
        for i in range(count):
            if not self.shoot_fast():
                log.info("Burst stopped at frame %d/%d", i, count)
                break
            taken += 1
            if interval_s > 0:
                time.sleep(interval_s)
        return taken

    def bracket_no_download(
        self,
        speeds: list[int],
        iso: int = C.ISO_100,
        aperture: Optional[int] = None,
    ) -> int:
        """Bracket at different speeds without downloading. Returns shots taken."""
        self.camera.set_ae_mode(C.AE_OFF)
        self.camera.set_iso(iso)
        if aperture is not None:
            self.camera.set_aperture(aperture)
        taken = 0
        for speed in speeds:
            self.camera.set_shutter_speed(speed)
            if not self.shoot_fast():
                break
            taken += 1
        return taken

    # ------------------------------------------------------------------
    # Legacy methods (with download)
    # ------------------------------------------------------------------
    def exposure_bracket(
        self,
        speeds: list[int],
        iso: int = C.ISO_100,
        aperture: Optional[int] = None,
    ):
        """Rapid-fire sequence at different shutter speeds for HDR corona.

        Fires one shot at each speed as fast as the camera allows.
        Uses shoot_no_af() to skip autofocus for maximum speed.

        Args:
            speeds: List of shutter speed constants (fast to slow recommended).
            iso: ISO value to use for all frames.
            aperture: Optional aperture value (leave None to keep current).
        """
        self.camera.set_ae_mode(C.AE_OFF)
        self.camera.set_iso(iso)
        if aperture is not None:
            self.camera.set_aperture(aperture)

        for speed in speeds:
            self.camera.set_shutter_speed(speed)
            self.camera.shoot_no_af()

    def burst_sequence(self, count: int, interval_ms: int = 0):
        """Fire N shots as fast as possible.

        For maximum speed, set drive mode to CH before calling this,
        or use interval_ms=0 which fires individual shots back-to-back.

        Args:
            count: Number of frames to capture.
            interval_ms: Minimum interval between shots in milliseconds.
                         0 means fire as fast as the camera allows.
        """
        interval_s = interval_ms / 1000.0

        for _ in range(count):
            self.camera.shoot_no_af()
            if interval_s > 0:
                time.sleep(interval_s)
