"""validate_for_eclipse must name the settings that actually cost frames.

Each of these pins a camera state that was measured on the bench and reported
nothing: the X-T4 sat in Manual with auto ISO and the drive dial on Single, the
validator raised one issue about AE mode, and the run failed anyway.
"""

from unittest.mock import MagicMock

from fujixsdk import _constants as C
from fujixsdk._errors import XSDKError
from fujixsdk.eclipse import validate_for_eclipse


def _camera(**overrides):
    """A camera that passes every check, before overrides are applied."""
    defaults = dict(
        get_ae_mode=lambda: C.AE_OFF,
        get_focus_mode=lambda: C.SDK_FOCUS_MANUAL,
        get_iso=lambda: 200,
        get_drive_mode=lambda: C.DRIVE_MODE_CH,
        get_exposure_bias=lambda: 0,
        get_image_quality=lambda: C.IMAGE_QUALITY_RAW,
        get_wb_mode=lambda: C.WB_DAYLIGHT,
        get_long_exposure_nr=lambda: C.OFF,
        # The info section reads these too; they raise no issues but must not
        # blow up the checks above.
        get_shutter_speed=lambda: (C.SHUTTER_1_1000, 0),
        get_aperture=lambda: 800,
        get_buffer_capacity=lambda: (0, 32),
        get_battery_info=lambda: (80, 0, 0),
        get_media_capacity=lambda: 32 * 1024 * 1024,
    )
    defaults.update(overrides)
    cam = MagicMock()
    for name, fn in defaults.items():
        setattr(cam, name, MagicMock(side_effect=lambda fn=fn: fn()))
    cam.camera_mode = 0x0001
    return cam


def _find(issues, setting):
    return [i for i in issues if i.setting == setting]


def test_clean_camera_raises_nothing_actionable():
    issues = validate_for_eclipse(_camera())
    actionable = [i for i in issues if i.severity in ("error", "warning")]
    assert actionable == [], [i.message for i in actionable]


# ------------------------------------------------------------------------ ISO


def test_auto_iso_is_an_error():
    # Measured on the bench: get_iso() returned -3, ISO_AUTO_3.  Every ISO in the
    # script was being ignored and the body metered off a black sky instead.
    issues = validate_for_eclipse(_camera(get_iso=lambda: C.ISO_AUTO_3))
    found = _find(issues, "ISO")
    assert found, "auto ISO was not reported at all"
    assert found[0].severity == "error"
    assert "Auto" in found[0].current


def test_fixed_iso_is_accepted():
    assert _find(validate_for_eclipse(_camera(get_iso=lambda: 100)), "ISO") == []


# ----------------------------------------------------------------- drive mode


def test_stills_drive_mode_is_not_treated_as_single_frame():
    # Measured: an X-T4 with the drive dial physically on CH still reads back
    # DRIVE_MODE_S.  That value means stills rather than single-frame drive, so
    # warning about it produces a false alarm on a correctly set camera.
    issues = validate_for_eclipse(_camera(get_drive_mode=lambda: C.DRIVE_MODE_S))
    found = _find(issues, "Drive Mode")
    assert found and found[0].severity == "info"
    assert "verify" in found[0].expected.lower()


def test_movie_mode_stays_an_error():
    issues = validate_for_eclipse(_camera(get_drive_mode=lambda: C.DRIVE_MODE_MOVIE))
    assert _find(issues, "Drive Mode")[0].severity == "error"


# ----------------------------------------------------------------- focus mode


def test_unreadable_focus_mode_is_reported_not_swallowed():
    # The bench camera returned 0x1002 for focus mode with a lens attached.  The
    # old code caught XSDKError and passed, so the setting most likely to refuse
    # S2 was never mentioned.
    def boom():
        raise XSDKError(0x1002, "Invalid parameter", 0)

    issues = validate_for_eclipse(_camera(get_focus_mode=boom))
    found = _find(issues, "Focus Mode")
    assert found, "an unreadable focus mode was reported as no problem"
    assert found[0].severity == "warning"
    assert "unreadable" in found[0].current


def test_autofocus_is_still_an_error():
    issues = validate_for_eclipse(_camera(get_focus_mode=lambda: C.SDK_FOCUS_AFS))
    assert _find(issues, "Focus Mode")[0].severity == "error"


# --------------------------------------------------------------- image quality


def test_raw_plus_jpeg_is_an_error():
    # Measured: a JPEG alongside the RAW put two entries in the buffer per frame
    # and took the rate from 1.85 fps to 0.61.  The old check accepted it.
    issues = validate_for_eclipse(
        _camera(get_image_quality=lambda: C.IMAGE_QUALITY_FINE_PLUS_RAW))
    found = _find(issues, "Image Quality")
    assert found and found[0].severity == "error"
    assert "RAW" in found[0].expected


def test_jpeg_only_is_an_error():
    issues = validate_for_eclipse(_camera(get_image_quality=lambda: C.IMAGE_QUALITY_FINE))
    assert _find(issues, "Image Quality")[0].severity == "error"


def test_raw_alone_is_accepted():
    assert _find(validate_for_eclipse(_camera()), "Image Quality") == []


def test_unreadable_image_quality_is_reported():
    def boom():
        raise XSDKError(0x1002, "Invalid parameter", 0)

    found = _find(validate_for_eclipse(_camera(get_image_quality=boom)), "Image Quality")
    assert found and found[0].severity == "warning"
    assert "unreadable" in found[0].current


# ---------------------------------------------------------------- buffer drain


def test_buffer_is_drained_before_it_fills():
    # Nothing else empties the volatile buffer, so without this a run stops at
    # the 32nd frame however many the script asked for.
    from unittest.mock import MagicMock

    from fujixsdk.eclipse import EclipseShooter

    cam = MagicMock()
    cam.get_buffer_capacity.return_value = (24, 32)      # 75% of 32
    shooter = EclipseShooter(cam)
    shooter._keep_buffer_clear()
    cam.drain_buffer.assert_called_once()


def test_buffer_is_left_alone_when_it_has_room():
    from unittest.mock import MagicMock

    from fujixsdk.eclipse import EclipseShooter

    cam = MagicMock()
    cam.get_buffer_capacity.return_value = (5, 32)
    shooter = EclipseShooter(cam)
    shooter._keep_buffer_clear()
    cam.drain_buffer.assert_not_called()
