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


def test_single_drive_mode_is_reported():
    # A relay burst holds the contacts closed and lets the body free-run; on
    # Single that is one frame per contact instead of a burst.
    issues = validate_for_eclipse(_camera(get_drive_mode=lambda: C.DRIVE_MODE_S))
    found = _find(issues, "Drive Mode")
    assert found and found[0].severity == "warning"
    assert found[0].current == "Single"


def test_ch_drive_mode_is_not_actionable():
    found = _find(validate_for_eclipse(_camera()), "Drive Mode")
    assert all(i.severity == "info" for i in found)


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
