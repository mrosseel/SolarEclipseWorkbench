"""Fuji failures must be loud.

Each of these pins a place where the old code returned normally after something
had gone wrong, leaving the eclipse to be photographed at the wrong settings or
not at all.
"""

from unittest.mock import MagicMock

import pytest

from solareclipseworkbench import fuji_camera, hardware_problems
from solareclipseworkbench.camera import CameraError
from solareclipseworkbench.fuji_camera import FujiCamera
from solareclipseworkbench.hardware_registry import register_hardware


@pytest.fixture(autouse=True)
def _clean():
    hardware_problems.clear()
    yield
    hardware_problems.clear()


def _camera(**sdk):
    sdk_cam = MagicMock(**sdk)
    camera = FujiCamera.__new__(FujiCamera)      # bypass SDK-dependent __init__
    camera._sdk_cam = sdk_cam
    camera.name = "X-T4"
    camera._applied_iso = None
    import threading
    camera._lock = threading.RLock()
    return camera, sdk_cam


# ------------------------------------------------------------------ configure


def test_configure_raises_when_a_setting_is_rejected():
    camera, sdk = _camera()
    sdk.set_shutter_speed.side_effect = RuntimeError("busy")

    with pytest.raises(CameraError, match="shutter speed"):
        camera.configure(shutter_speed="1/2000")


def test_configure_reports_every_failure_not_just_the_first():
    camera, sdk = _camera()
    sdk.set_iso.side_effect = RuntimeError("nope")
    sdk.set_shutter_speed.side_effect = RuntimeError("busy")

    with pytest.raises(CameraError) as exc:
        camera.configure(iso=200, shutter_speed="1/2000")

    assert "ISO" in str(exc.value)
    assert "shutter speed" in str(exc.value)


def test_configure_tries_the_remaining_settings_after_one_fails():
    camera, sdk = _camera()
    sdk.set_iso.side_effect = RuntimeError("nope")

    with pytest.raises(CameraError):
        camera.configure(iso=200, shutter_speed="1/2000")

    # The shutter speed must still have been attempted.
    assert sdk.set_shutter_speed.called


def test_configure_rejects_a_value_the_camera_cannot_express():
    camera, _ = _camera()

    with pytest.raises(CameraError, match="not a value"):
        camera.configure(iso="not-a-number")


def test_configure_succeeds_quietly_when_everything_applies():
    camera, sdk = _camera()

    camera.configure(iso=200, aperture="5.6")

    assert sdk.set_iso.called and sdk.set_aperture.called


# ------------------------------------------------------------------- bracket


def test_bracket_raises_instead_of_returning_no_frames():
    # The old code returned [] here, so the bracket silently took zero frames.
    camera, sdk = _camera()
    sdk.get_shutter_speed.side_effect = RuntimeError("no reply")

    with pytest.raises(CameraError, match="bracket"):
        camera.parse_bracket_speeds("+/- 1")


def test_bracket_spans_the_requested_range():
    camera, sdk = _camera()
    supported = list(range(100, 130))
    sdk.get_shutter_speed.return_value = (115, 0)
    sdk.get_supported_shutter_speeds.return_value = supported

    speeds = camera.parse_bracket_speeds("+/- 1")

    # +/- 1 EV at 1/3 EV per position is three either side, so seven frames.
    assert len(speeds) == 7
    assert 115 in speeds


def test_a_bracket_frame_whose_speed_was_refused_is_still_taken(monkeypatch):
    # There is no second chance at a contact: a frame at the previous speed
    # beats no frame at all.  But it will look normal until it is reviewed, so
    # the failure has to be reported rather than swallowed.
    monkeypatch.setattr(fuji_camera, "BUSY_BACKOFF_S", 0.0)
    monkeypatch.setattr(fuji_camera, "TAP_GAP_S", 0.0)
    monkeypatch.setattr(fuji_camera, "SETTLE_BEFORE_DRAIN_S", 0.0)

    camera, sdk = _camera()
    sdk.set_shutter_speed.side_effect = RuntimeError("busy")
    relay = MagicMock()
    register_hardware("relay", relay)
    try:
        taken = fuji_camera._RelayShooter(camera).bracket_no_download([100, 200, 300])
    finally:
        register_hardware("relay", None)

    assert taken == 3
    assert relay.shoot.call_count == 3
    assert hardware_problems.count() == 1
    assert "wrong shutter speed" in hardware_problems.peek()[0].message


def test_a_bracket_does_not_reapply_the_iso_the_caller_just_set(monkeypatch):
    # The SDK shooter defaults to ISO 100 and writes it; the relay path must not,
    # or every bracket asked for at ISO 400 would be photographed at 100.
    monkeypatch.setattr(fuji_camera, "TAP_GAP_S", 0.0)
    monkeypatch.setattr(fuji_camera, "SETTLE_BEFORE_DRAIN_S", 0.0)

    camera, sdk = _camera()
    register_hardware("relay", MagicMock())
    try:
        fuji_camera._RelayShooter(camera).bracket_no_download([100], iso=400)
    finally:
        register_hardware("relay", None)

    assert not sdk.set_iso.called


# ----------------------------------------------------------------------- ISO


def test_the_same_iso_is_written_once_not_on_every_frame():
    # set_iso is refused unless the transfer queue is empty, so a write that
    # cannot change anything is a round-trip that can only fail.
    camera, sdk = _camera()

    for _ in range(4):
        camera.configure(iso=400)

    assert sdk.set_iso.call_count == 1


def test_a_changed_iso_is_written_again():
    camera, sdk = _camera()

    camera.configure(iso=100)
    camera.configure(iso=100)
    camera.configure(iso=800)

    assert sdk.set_iso.call_count == 2


def test_a_refused_iso_is_retried_on_the_next_frame():
    # A rejected write leaves the body at an unknown ISO, so the next frame must
    # try again rather than trust a value that never landed.
    camera, sdk = _camera()
    sdk.set_iso.side_effect = RuntimeError("XSDK error 0x00001006: Camera is busy")

    for _ in range(3):
        with pytest.raises(CameraError, match="ISO"):
            camera.configure(iso=400)

    assert sdk.set_iso.call_count == 3
