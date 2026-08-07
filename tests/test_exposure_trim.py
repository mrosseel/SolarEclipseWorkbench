"""The haze correction: one number applied to every exposure the script asks for.

The exposures come from tables through an extinction coefficient that is a guess
- 0.15 clear, 0.40 hazy, 0.25 assumed - and being wrong about it shifts every
frame by the same amount.  This is where the observer's judgement goes, so the
cases that matter are the ones where it would silently apply twice, or to the
wrong things, or not at all.
"""

import pytest

from solareclipseworkbench import camera as camera_mod
from solareclipseworkbench import exposure_trim
from solareclipseworkbench.camera import CameraSettings


@pytest.fixture(autouse=True)
def _no_trim():
    exposure_trim.set_stops(0)
    yield
    exposure_trim.set_stops(0)


def test_nothing_moves_at_zero():
    assert exposure_trim.apply_seconds(0.002) == 0.002
    assert exposure_trim.apply_microseconds(2000) == 2000


def test_a_stop_doubles_the_time():
    exposure_trim.set_stops(1)
    assert exposure_trim.apply_seconds(0.002) == pytest.approx(0.004)
    exposure_trim.set_stops(-1)
    assert exposure_trim.apply_seconds(0.002) == pytest.approx(0.001)


def test_half_stops_are_the_step_an_observer_can_judge():
    exposure_trim.set_stops(0.5)
    assert exposure_trim.apply_seconds(1.0) == pytest.approx(2 ** 0.5)


def test_it_refuses_to_go_past_three_stops():
    # Past that the exposures were not approximately right to begin with, and the
    # script wants regenerating with a better coefficient rather than correcting.
    assert exposure_trim.set_stops(9) == 3.0
    assert exposure_trim.set_stops(-9) == -3.0


def test_the_correction_reads_the_way_a_photographer_writes_it():
    exposure_trim.set_stops(0)
    assert exposure_trim.describe() == "0 EV"
    exposure_trim.set_stops(-1.5)
    assert exposure_trim.describe() == "-1.5 EV"
    exposure_trim.set_stops(2)
    assert exposure_trim.describe() == "+2.0 EV"


# --------------------------------------------------- applied to real settings

def test_the_caller_s_settings_are_never_corrected_in_place():
    # The same CameraSettings is held by a scheduled job and reused every time it
    # fires.  Correcting it in place would compound the trim on every frame -
    # after ten partials the exposure would be a thousand times out.
    settings = CameraSettings("X-T4", "1/1000", "-", 100)
    exposure_trim.set_stops(1)

    first = camera_mod._trimmed(settings)
    second = camera_mod._trimmed(settings)

    assert settings.shutter_speed == "1/1000", "the original must not be touched"
    assert first.shutter_speed == second.shutter_speed == "1/500"


def test_a_shutter_speed_the_parser_cannot_read_is_left_alone():
    # Better an uncorrected frame than no frame: an unreadable speed is passed
    # through for the camera layer to reject or snap as it already would.
    settings = CameraSettings("X-T4", "gibberish", "-", 100)
    exposure_trim.set_stops(1)

    assert camera_mod._trimmed(settings).shutter_speed == "gibberish"


@pytest.mark.parametrize("written,seconds", [
    ("1/2000", 0.0005), ("0.5", 0.5), ("2", 2.0), ('1/4"', 0.25),
])
def test_the_speeds_a_script_writes_are_all_understood(written, seconds):
    assert camera_mod._speed_to_seconds(written) == pytest.approx(seconds)


def test_a_corrected_speed_is_written_the_way_a_script_writes_them():
    # Every path downstream parses this string again, so it has to come back in
    # a form they all read.
    assert camera_mod._seconds_to_speed(0.001) == "1/1000"
    assert camera_mod._seconds_to_speed(2.0) == "2"


def test_zero_trim_returns_the_very_same_object():
    # The common case must cost nothing at all.
    settings = CameraSettings("X-T4", "1/1000", "-", 100)
    assert camera_mod._trimmed(settings) is settings


# ----------------------------------------------------- applied to the ladders

def _fuji():
    """A Fuji camera with the SDK bypassed, as the other Fuji tests build one."""
    import threading
    from unittest.mock import MagicMock
    from solareclipseworkbench.fuji_camera import FujiCamera
    sdk = MagicMock()
    cam = FujiCamera.__new__(FujiCamera)
    cam._sdk_cam, cam.name = sdk, "X-T4"
    cam._applied_iso = cam._applied_speed = None
    cam._frame_busy_until = 0.0
    cam._lock = threading.RLock()
    return cam, sdk


def test_the_trim_moves_a_whole_semicolon_ladder():
    # The rungs are exposures too.  Correcting only the frames outside the ladder
    # would leave the corona ladders - which is most of totality - uncorrected.
    from solareclipseworkbench import fuji_camera
    cam, _ = _fuji()

    exposure_trim.set_stops(0)
    plain = cam.parse_bracket_speeds("1/2000;1/500;1/125")
    exposure_trim.set_stops(1)
    lifted = cam.parse_bracket_speeds("1/2000;1/500;1/125")

    # Doubled, but landing on speeds the body actually has: the rungs are put
    # back on the camera's scale after the trim, so 1/125 lifted a stop is the
    # body's 1/60 rather than an exact 15625 us that it would refuse.  Within
    # 2% is closer than the gaps in the scale.
    from fujixsdk._constants import SHUTTER_SPEED_NAMES
    for before, after in zip(plain, lifted):
        assert after in SHUTTER_SPEED_NAMES, "%d us is not a speed" % after
        assert abs(after / (before * 2.0) - 1.0) < 0.02, (
            "%d us doubled should be near %d, got %d" % (before, before * 2, after))


def test_the_trim_moves_a_computed_ladder_too():
    cam, sdk = _fuji()
    sdk.get_shutter_speed.return_value = (8000, 0)     # 1/125
    sdk.get_supported_shutter_speeds.return_value = []

    exposure_trim.set_stops(0)
    plain = cam.parse_bracket_speeds("+/- 1")
    exposure_trim.set_stops(1)
    lifted = cam.parse_bracket_speeds("+/- 1")

    assert len(plain) == len(lifted)
    assert all(b > a for a, b in zip(plain, lifted)), "every rung moves"


def test_a_trim_never_asks_for_a_speed_the_body_does_not_own():
    """7 August, the final rehearsal: half a stop under 1/8000 is 1/11314.

    That went to the body verbatim, came back "not a value this camera
    understands", and the frame kept whatever exposure it already had - a
    silent wrong exposure, loud only in the log.  Every trimmed speed must
    land on the body's own scale.
    """
    from solareclipseworkbench import exposure_limits, exposure_trim
    from solareclipseworkbench.camera import _speed_to_seconds, _usable_speed

    accepted = set(exposure_limits.limits().accepted_speeds)
    try:
        for stops in (-1.0, -0.5, -0.3, 0.3, 0.5, 1.0):
            exposure_trim.set_stops(stops)
            for base in ('1/8000', '1/4000', '1/1000', '1/125', '1/8', '1'):
                seconds = _speed_to_seconds(base)
                got = _usable_speed(exposure_trim.apply_seconds(seconds), 'X-T4')
                back = _speed_to_seconds(got)
                assert any(abs(back - a) < a * 0.02 for a in accepted), \
                    f'{base} {stops:+} EV produced {got}, not on the body scale'
    finally:
        exposure_trim.set_stops(0.0)


def test_a_trim_past_the_mechanical_shutter_is_clamped_not_invented():
    # The body stops at 1/8000 on MS; asking for faster fails outright, so
    # the trim gives up the last half stop rather than the whole frame.
    from solareclipseworkbench import exposure_trim
    from solareclipseworkbench.camera import _usable_speed

    try:
        exposure_trim.set_stops(-0.5)
        assert _usable_speed(exposure_trim.apply_seconds(1.0 / 8000), 'X-T4') == '1/8000'
    finally:
        exposure_trim.set_stops(0.0)
