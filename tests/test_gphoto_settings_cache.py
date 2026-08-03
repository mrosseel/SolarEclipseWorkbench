"""The gphoto2 settings cache, where a stale entry is silent.

Skipping a reconfigure saves two USB round-trips a frame.  Skipping one the body
did not actually receive photographs the frame at whatever the camera was
holding, which looks entirely normal until the card is read - so every case here
is about when the skip must NOT happen.
"""

import pytest

from solareclipseworkbench import camera as camera_mod
from solareclipseworkbench.camera import CameraSettings


@pytest.fixture(autouse=True)
def _clean():
    camera_mod.forget_camera_settings()
    yield
    camera_mod.forget_camera_settings()


def _settings(shutter="1/1000", aperture="5.6", iso=400, name="X-T4"):
    return CameraSettings(name, shutter, aperture, iso)


def test_the_same_exposure_twice_is_recognised():
    key = camera_mod._settings_key(_settings())

    assert camera_mod._settings_key(_settings()) == key


def test_a_changed_iso_is_a_different_exposure():
    assert camera_mod._settings_key(_settings(iso=400)) != \
           camera_mod._settings_key(_settings(iso=800))


def test_a_changed_shutter_is_a_different_exposure():
    assert camera_mod._settings_key(_settings(shutter="1/1000")) != \
           camera_mod._settings_key(_settings(shutter="1/500"))


def test_a_changed_aperture_is_a_different_exposure():
    # A telescope says "-" and a lens says "5.6"; swapping bodies mid-script
    # must not inherit the other one's aperture.
    assert camera_mod._settings_key(_settings(aperture="5.6")) != \
           camera_mod._settings_key(_settings(aperture="-"))


def test_forgetting_one_camera_leaves_the_others_alone():
    camera_mod._last_settings["A"] = ("1/1000", "5.6", "400")
    camera_mod._last_settings["B"] = ("1/500", "8", "200")

    camera_mod.forget_camera_settings("A")

    assert "A" not in camera_mod._last_settings
    assert camera_mod._last_settings["B"] == ("1/500", "8", "200")


def test_forgetting_everything_clears_the_lot():
    camera_mod._last_settings["A"] = ("1/1000", "5.6", "400")
    camera_mod._last_settings["B"] = ("1/500", "8", "200")

    camera_mod.forget_camera_settings()

    assert camera_mod._last_settings == {}


def test_forgetting_a_camera_that_was_never_cached_is_harmless():
    # Called from a reconnect path that runs whether or not anything was applied.
    camera_mod.forget_camera_settings("never seen")
