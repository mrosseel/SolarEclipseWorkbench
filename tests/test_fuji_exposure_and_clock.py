"""Exposure and clock handling on the X-T4, where a failure is silent.

Each case guards something that produces normal-looking frames at the wrong
settings, or a sync that reports success without moving the camera clock — the
kind of fault that only shows up once the card is read.
"""

import time
from unittest.mock import MagicMock

import pytest

from fujixsdk._constants import ERRCODE_BUSY, ERRCODE_PARAM, SHUTTER_SPEED_NAMES
from fujixsdk._errors import BusyError, ParamError

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
    cam._applied_speed = None
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

def test_a_body_that_lists_no_speeds_still_gets_a_full_ladder():
    cam, sdk = _camera()
    # The X-T4's SDK module does not implement CapShutterSpeed and answers with
    # an empty list.  The ladder is then computed in exposure time instead.
    sdk.get_shutter_speed.return_value = (fuji_camera._parse_shutter_speed("1/200"), None)
    sdk.get_supported_shutter_speeds.return_value = []

    speeds = cam.parse_bracket_speeds("+/- 2")

    assert len(speeds) == 13
    assert len(set(speeds)) == 13


def test_the_computed_ladder_is_a_third_of_a_stop_per_rung():
    cam, sdk = _camera()
    sdk.get_shutter_speed.return_value = (fuji_camera._parse_shutter_speed("1/200"), None)
    sdk.get_supported_shutter_speeds.return_value = []

    speeds = cam.parse_bracket_speeds("+/- 2")

    # Ends two stops either side of the base, so the slowest is 16x the fastest.
    seconds = [fuji_camera._speed_name_seconds(SHUTTER_SPEED_NAMES[s]) for s in speeds]
    assert seconds == sorted(seconds)
    assert 15.0 < seconds[-1] / seconds[0] < 17.0
    assert fuji_camera._parse_shutter_speed("1/200") in speeds


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
    assert "the scale reaches" in caplog.text


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


# ------------------------------------------------- waiting out a busy body

# The real backoff is a third of a second; the tests only care about the shape
# of the waiting, not its duration.
@pytest.fixture
def _quick_backoff(monkeypatch):
    monkeypatch.setattr(fuji_camera, 'BUSY_BACKOFF_S', 0.001)
    monkeypatch.setattr(fuji_camera, 'EXPOSURE_BUDGET_S', 0.05)


def _busy():
    return BusyError(ERRCODE_BUSY, 'Camera is busy')


def test_a_body_still_writing_is_waited_out_not_reported_as_a_failure(_quick_backoff):
    # The X-T4 refuses exposure changes with 0x1006 for about a second after a
    # frame.  Reporting that as a rejection sent the whole bracket out at the
    # previous exposure on 2 August.
    cam, sdk = _camera()
    sdk.set_iso.side_effect = [_busy(), _busy(), None]

    cam.configure(iso=400)

    assert sdk.set_iso.call_count == 3
    assert cam._applied_iso == fuji_camera._parse_iso(400)


def test_a_value_the_body_rejects_outright_is_not_retried(_quick_backoff):
    # Backing off six times cannot make an unsupported value supported, and the
    # seconds spent are seconds of totality.
    cam, sdk = _camera()
    sdk.set_iso.side_effect = ParamError(ERRCODE_PARAM, 'Invalid parameter')

    with pytest.raises(camera_mod.CameraError):
        cam.configure(iso=400)

    assert sdk.set_iso.call_count == 1


def test_waiting_stops_at_the_budget_so_the_next_frame_is_not_missed(_quick_backoff):
    cam, sdk = _camera()
    sdk.set_iso.side_effect = _busy
    sdk.set_shutter_speed.side_effect = _busy

    started = time.monotonic()
    with pytest.raises(camera_mod.CameraError):
        cam.configure(iso=400, shutter_speed='1/200')
    elapsed = time.monotonic() - started

    # One budget for the whole call, not one per setting: a body that stays busy
    # must cost the schedule that much once.
    assert elapsed < fuji_camera.EXPOSURE_BUDGET_S * 2, elapsed


def test_a_setting_that_never_goes_on_is_still_reported(_quick_backoff):
    # A frame at the previous exposure looks perfectly normal until the card is
    # read, so giving up on time must never mean giving up quietly.
    cam, sdk = _camera()
    sdk.set_shutter_speed.side_effect = _busy

    with pytest.raises(camera_mod.CameraError, match='shutter speed'):
        cam.configure(shutter_speed='1/200')


def test_a_busy_body_does_not_leave_a_stale_iso_remembered(_quick_backoff):
    # The remembered ISO is what lets `configure` skip the write next time; if a
    # timed-out write left it set, the skip would outlive its evidence.
    cam, sdk = _camera()
    sdk.set_iso.side_effect = _busy

    with pytest.raises(camera_mod.CameraError):
        cam.configure(iso=400)

    assert cam._applied_iso is None


def test_the_same_shutter_speed_is_not_written_twice(_quick_backoff):
    # The body refuses the write with 0x1006 while it flushes the previous frame,
    # so a run of singles at one exposure spent ~1.7s a frame waiting to write a
    # value it already had.  Measured 2.0s a frame before, 0.3s after.
    cam, sdk = _camera()

    cam.configure(shutter_speed='1/200')
    cam.configure(shutter_speed='1/200')

    assert sdk.set_shutter_speed.call_count == 1


def test_a_changed_shutter_speed_is_still_written(_quick_backoff):
    cam, sdk = _camera()

    cam.configure(shutter_speed='1/200')
    cam.configure(shutter_speed='1/500')

    assert sdk.set_shutter_speed.call_count == 2


def test_a_busy_body_does_not_leave_a_stale_speed_remembered(_quick_backoff):
    # A skip that outlives its evidence photographs the next frame at whatever
    # the body kept, and it looks perfectly normal until the card is read.
    cam, sdk = _camera()
    sdk.set_shutter_speed.side_effect = _busy

    with pytest.raises(camera_mod.CameraError):
        cam.configure(shutter_speed='1/200')

    assert cam._applied_speed is None


def test_a_bracket_leaves_configure_knowing_what_it_wrote(monkeypatch):
    # The bracket writes speeds straight to the SDK.  If `configure` still
    # believed the pre-bracket speed was applied, the next single at the
    # bracket's last rung would be skipped and shot at the wrong exposure.
    monkeypatch.setattr(fuji_camera, 'TAP_GAP_S', 0.0)
    monkeypatch.setattr(fuji_camera, 'SETTLE_BEFORE_DRAIN_S', 0.0)
    from solareclipseworkbench.hardware_registry import register_hardware

    cam, sdk = _camera()
    sdk.get_buffer_capacity.return_value = (0, 32)
    register_hardware('relay', MagicMock())
    try:
        fuji_camera._RelayShooter(cam).bracket_no_download([100, 200, 300])
    finally:
        register_hardware('relay', None)

    assert cam._applied_speed == 300
