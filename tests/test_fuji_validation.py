"""validate_for_eclipse must name the settings that actually cost frames.

Each of these pins a camera state that was measured on the bench and reported
nothing: the X-T4 sat in Manual with auto ISO and the drive dial on Single, the
validator raised one issue about AE mode, and the run failed anyway.
"""

from unittest.mock import MagicMock

from fujixsdk.camera import BatteryInfo, MediaCapacity, ShutterCount

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
        # The real shapes: the body reports a coarse battery state rather than
        # a percentage, and the card calls take a slot.
        get_battery_info=lambda: BatteryInfo(
            body=C.POWERCAPACITY_80, grip=C.POWERCAPACITY_EMPTY,
            grip2=C.POWERCAPACITY_EMPTY, body_ratio=80, grip_ratio=0,
            grip2_ratio=0),
        get_media_status=lambda slot=1: C.MEDIASTATUS_OK,
        get_media_capacity=lambda slot=1: MediaCapacity(
            blank_frames=940, remaining_sectors=32 * 1024 * 1024,
            sector_size=512, card_size=64 * 1000 ** 3),
        get_shutter_count=lambda: ShutterCount(current=18955, total=18955,
                                               exchanges=0),
    )
    defaults.update(overrides)
    cam = MagicMock()
    for name, fn in defaults.items():
        setattr(cam, name, MagicMock(side_effect=lambda *a, fn=fn, **k: fn(*a, **k)))
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


def test_an_api_the_body_does_not_have_is_not_a_warning():
    """Asked for on 4 August: the X-T4 cannot report its image quality or its
    battery, and saying so as a warning every session trains the reader to skim
    past warnings that do matter.

    0x1013 is the body saying it has no such API - confirmed against
    GetDeviceInfoEx, whose list of implemented codes omits CheckBatteryInfo
    however supported the manual claims the model is.
    """
    from fujixsdk._errors import XSDKError

    def unimplemented():
        raise XSDKError(0x1013, "API not found in model module")

    issues = validate_for_eclipse(_camera(get_image_quality=unimplemented))
    quality = _find(issues, "Image Quality")

    assert quality, "the setting disappeared instead of being stated"
    assert quality[0].severity == "info", "still warning about a missing API"
    assert quality[0].current == "not available on this body"


def test_a_failure_that_is_not_a_missing_api_still_warns():
    # The distinction is the point: busy, or a dead handle, is a real problem
    # and must not be filed away as a fact about the model.
    from fujixsdk._errors import XSDKError

    def busy():
        raise XSDKError(0x1006, "Camera is busy")

    issues = validate_for_eclipse(_camera(get_image_quality=busy))
    quality = _find(issues, "Image Quality")

    assert quality[0].severity == "warning"
    assert "0x1006" in quality[0].current


def test_the_focus_mode_keeps_the_same_distinction():
    from fujixsdk._errors import XSDKError

    def unimplemented():
        raise XSDKError(0x1013, "API not found in model module")

    issues = validate_for_eclipse(_camera(get_focus_mode=unimplemented))
    focus = _find(issues, "Focus Mode")

    assert focus[0].severity == "info"
    assert focus[0].expected == "MF"


def test_a_trim_cannot_ask_for_a_speed_the_body_has_not_got():
    """4 August, mid-run: a -0.5 EV trim turned 1/6400 into 1/9051.

    That snapped to 1/10000 - nearest on the SDK's table, which runs to
    1/180000 because it covers every model and the electronic shutter - and the
    body answered 0x2003, invalid parameter *combination*: its mechanical
    shutter does not go there.  The frame was then taken at whatever the body
    was last set to, which is the kind of failure that looks fine until the
    photographs are reviewed.
    """
    from fujixsdk._constants import SHUTTER_SPEED_NAMES

    from solareclipseworkbench import exposure_limits
    from solareclipseworkbench.fuji_camera import _parse_shutter_speed

    # The fast end is configurable now; this is the mechanical-shutter case,
    # where it really is 1/8000.
    before = exposure_limits.limits()
    exposure_limits.set_limits(fastest_s=1.0 / 8000)
    try:
        for asked in ("1/9051", "1/11314", "1/32000"):
            value = _parse_shutter_speed(asked)
            assert value is not None, "%s was rejected outright" % asked
            assert SHUTTER_SPEED_NAMES.get(value) == '1/8000"', \
                "%s gave %s, which the body cannot take" % (
                    asked, SHUTTER_SPEED_NAMES.get(value))

        # Everything inside the range is untouched.
        assert SHUTTER_SPEED_NAMES.get(_parse_shutter_speed("1/6400")) == '1/6400"'
        assert SHUTTER_SPEED_NAMES.get(_parse_shutter_speed("0.5")) == '1/2"'

        # A half-stop speed from another model's scale is refused by this
        # body, so it must land on the nearest third-stop one instead.
        assert SHUTTER_SPEED_NAMES.get(_parse_shutter_speed("1/750")) == '1/800"'

        # With the electronic shutter allowed the same ask is honoured rather
        # than clamped, which is what the override is for.
        exposure_limits.set_limits(fastest_s=1.0 / 32000,
                                   accepted_speeds=exposure_limits.XT4_SPEEDS_S)
        assert SHUTTER_SPEED_NAMES.get(_parse_shutter_speed("1/16000")) == '1/16000"'
    finally:
        exposure_limits.set_limits(**vars(before))


def test_a_trimmed_ladder_rung_lands_on_a_real_shutter_speed():
    """What cost the corona ladders on 4 August.

    The trim multiplies microseconds directly, so -0.5 EV turned 1/8000 - 125 us
    - into 88 us, which is not a shutter speed.  The SDK took it, the body
    answered 0x2003, and the frame was taken at whatever was set before.  Every
    ladder starts at 1/8000, so every ladder died on its first rung while the
    slower partials carried on working; the log said the frames were fine and
    the card had none of them.
    """
    from fujixsdk._constants import SHUTTER_SPEED_NAMES

    from solareclipseworkbench import exposure_trim
    from solareclipseworkbench.fuji_camera import snap_to_scale

    ladder = (125, 500, 2000, 8000, 33333, 125000, 500000)   # the corona ladder
    try:
        for stops in (-1.0, -0.5, 0.0, +0.5, +1.0):
            exposure_trim.set_stops(stops)
            for rung in ladder:
                trimmed = exposure_trim.apply_microseconds(rung)
                snapped = snap_to_scale(trimmed)
                assert snapped in SHUTTER_SPEED_NAMES, (
                    "%+.1f EV on %d us gave %s, which the body has no name for"
                    % (stops, rung, snapped))
    finally:
        exposure_trim.set_stops(0.0)


def test_the_fastest_rung_clamps_rather_than_failing():
    from fujixsdk._constants import SHUTTER_SPEED_NAMES

    from solareclipseworkbench import exposure_limits
    from solareclipseworkbench.fuji_camera import snap_to_scale

    before = exposure_limits.limits()
    exposure_limits.set_limits(fastest_s=1.0 / 8000)
    try:
        # 88 us is 1/8000 trimmed by half a stop; on MS the body stops there.
        assert SHUTTER_SPEED_NAMES[snap_to_scale(88)] == '1/8000"'
        assert SHUTTER_SPEED_NAMES[snap_to_scale(125)] == '1/8000"'
        # A positive trim has room and must not be clamped.
        assert SHUTTER_SPEED_NAMES[snap_to_scale(177)] != '1/8000"'

        # The slow end clamps the same way: +3 EV on a 4 s corona frame asks
        # for 32 s, and the body is given the cap rather than a refusal.
        exposure_limits.set_limits(slowest_s=6.0)
        assert SHUTTER_SPEED_NAMES[snap_to_scale(32_000_000)] == '6"'
    finally:
        exposure_limits.set_limits(**vars(before))


def test_a_bracket_past_the_fast_end_does_not_take_the_same_frame_twice():
    """From the log, 4 August 22:03:

        take_bracket +/- 2 -> 13 frame(s):
          1/8000", 1/8000", 1/8000", 1/8000", 1/8000", 1/8000", 1/8000...
        take_bracket took all 13 frame(s)

    A bracket around a body already at 1/8000 clamps every faster rung to the
    same speed, so half of it was identical frames - and it reported complete
    success.  During totality each duplicate is a slot in the transfer queue, a
    card write, and about a second of a hundred that cannot be had again.
    """
    import logging

    from fujixsdk._constants import SHUTTER_SPEED_NAMES

    from solareclipseworkbench import exposure_limits
    from solareclipseworkbench.fuji_camera import _distinct, snap_to_scale

    ladder = [30, 38, 48, 61, 76, 96, 122, 154, 194, 244, 308, 388, 488]

    # The mechanical-shutter case: on MS every rung past 1/8000 lands on
    # 1/8000, and the duplicates must be dropped rather than shot.
    before = exposure_limits.limits()
    exposure_limits.set_limits(fastest_s=1.0 / 8000)
    try:
        kept = _distinct(snap_to_scale(v) for v in ladder)
    finally:
        exposure_limits.set_limits(**vars(before))

    assert len(kept) == len(set(kept)), "the same exposure twice in one bracket"
    assert len(kept) < len(ladder), "nothing was dropped"
    assert SHUTTER_SPEED_NAMES[kept[0]] == '1/8000"'
    # And what is left is a real range, not a single speed repeated.
    assert SHUTTER_SPEED_NAMES[kept[-1]] == '1/2000"'


def test_a_bracket_that_fits_is_left_alone():
    from solareclipseworkbench.fuji_camera import _distinct, snap_to_scale

    ladder = [500, 1000, 2000, 4000, 8000]          # 1/2000 to 1/125
    kept = _distinct(snap_to_scale(v) for v in ladder)

    assert len(kept) == len(ladder)
