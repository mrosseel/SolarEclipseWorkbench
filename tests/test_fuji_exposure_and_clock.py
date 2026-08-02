"""Exposure and clock handling on the X-T4, where a failure is silent.

Each case guards something that produces normal-looking frames at the wrong
settings, or a sync that reports success without moving the camera clock — the
kind of fault that only shows up once the card is read.
"""

from unittest.mock import MagicMock

import pytest

from solareclipseworkbench import camera as camera_mod
from solareclipseworkbench import fuji_camera, hardware_problems
from solareclipseworkbench.fuji_camera import FujiCamera


@pytest.fixture(autouse=True)
def _clean():
    hardware_problems.clear()
    yield
    hardware_problems.clear()


def _camera(**sdk):
    sdk_cam = MagicMock(**sdk)
    cam = FujiCamera.__new__(FujiCamera)      # bypass SDK-dependent __init__
    cam._sdk_cam = sdk_cam
    cam.name = "X-T4"
    cam._applied_iso = None
    import threading
    cam._lock = threading.RLock()
    return cam, sdk_cam


# --------------------------------------------------------- shutter speed names

HALF_A_SECOND = 500_000       # SHUTTER_SPEED_NAMES calls this '1/2"'


@pytest.mark.parametrize("written", ["0.5", "1/2", '0.5"', " 0.5 "])
def test_half_a_second_is_understood_however_it_is_written(written):
    assert fuji_camera._parse_shutter_speed(written) == HALF_A_SECOND


def test_decimal_seconds_agree_with_their_fractional_spelling():
    for decimal, fraction in (("0.25", "1/4"), ("0.125", "1/8"), ("2", "2")):
        assert fuji_camera._parse_shutter_speed(decimal) == \
               fuji_camera._parse_shutter_speed(fraction)


def test_a_speed_off_the_scale_is_still_refused():
    # Nothing within 1/6 EV, so this must not be silently rounded to a rung the
    # schedule did not ask for.
    assert fuji_camera._parse_shutter_speed("gibberish") is None
    assert fuji_camera._parse_shutter_speed("0") is None


def test_an_off_grid_value_snaps_but_says_so(caplog):
    with caplog.at_level("WARNING"):
        assert fuji_camera._parse_shutter_speed("0.7") is not None
    assert "not on this camera's scale" in caplog.text


# ---------------------------------------------------------------- bracket size

def test_bracket_collapsing_to_one_frame_is_reported():
    cam, sdk = _camera()
    # A speed the body does not list among its supported ones collapses the
    # ladder to a single frame.
    sdk.get_shutter_speed.return_value = (12_345, None)
    sdk.get_supported_shutter_speeds.return_value = [1000, 2000, 4000]

    speeds = cam.parse_bracket_speeds("+/- 2")

    assert speeds == [12_345]


def test_bracket_ladder_is_symmetric_and_named():
    cam, sdk = _camera()
    supported = [100, 200, 400, 800, 1600, 3200, 6400]
    sdk.get_shutter_speed.return_value = (800, None)
    sdk.get_supported_shutter_speeds.return_value = supported

    speeds = cam.parse_bracket_speeds("+/- 1")     # 3 positions either side

    assert speeds == supported
    assert cam.describe_speeds([100, 200]).count(",") == 1


def test_short_bracket_at_the_end_of_the_scale_warns(caplog):
    cam, sdk = _camera()
    sdk.get_shutter_speed.return_value = (100, None)
    sdk.get_supported_shutter_speeds.return_value = [100, 200, 400]

    with caplog.at_level("WARNING"):
        speeds = cam.parse_bracket_speeds("+/- 1")

    assert len(speeds) < 7
    assert "only" in caplog.text


# ------------------------------------------------------------------ clock sync

def test_fuji_sync_reports_that_the_clock_cannot_be_set():
    cam, _ = _camera()

    cam.sync_clock()

    problems = hardware_problems.peek()
    assert any("by hand" in str(p) for p in problems), problems


def test_set_time_delegates_to_a_camera_that_owns_its_clock():
    cam = MagicMock()
    cam.sync_clock = MagicMock()

    camera_mod.set_time(cam)

    cam.sync_clock.assert_called_once()
    # The gphoto2 widget path must not also run: it reports success without
    # moving the clock.
    cam.get_config.assert_not_called()
