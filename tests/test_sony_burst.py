from unittest.mock import patch

from solareclipseworkbench.camera import CameraSettings, take_burst


class _DummyLock:
    def acquire(self, timeout=None):
        return True

    def release(self):
        return None


class _SonyCamera:
    def __init__(self):
        self.vendor = "Sony"
        self._camera = object()
        self._usb_lock = _DummyLock()

    def capture(self, *args, **kwargs):
        raise AssertionError("fallback capture should not be used in this regression test")


def test_take_burst_uses_sony_event_drain_instead_of_capture_complete_wait():
    camera = _SonyCamera()
    settings = CameraSettings("Sony Test", "1/2000", "5.6", 100)

    with patch("solareclipseworkbench.camera.__adapt_camera_settings", return_value=(object(), object())), \
         patch("solareclipseworkbench.camera.gp.check_result", side_effect=lambda value: value), \
         patch("solareclipseworkbench.camera.gp.gp_widget_get_child_by_name", return_value=object()), \
         patch("solareclipseworkbench.camera.gp.gp_widget_set_value"), \
         patch("solareclipseworkbench.camera.gp.gp_camera_trigger_capture", return_value=0) as trigger_capture, \
         patch("solareclipseworkbench.camera.gp.gp_camera_get_config", return_value=object()), \
         patch("solareclipseworkbench.camera._find_capturemode_choice", side_effect=["Continuous Shooting", "Single Shooting"]), \
         patch("solareclipseworkbench.camera._set_gp_config"), \
         patch("solareclipseworkbench.camera._sony_drain_events") as sony_drain, \
         patch("solareclipseworkbench.camera._drain_camera_events") as drain_events, \
         patch("solareclipseworkbench.camera._wait_for_capture_complete") as wait_for_capture_complete:
        take_burst(camera, settings, 3)

    assert trigger_capture.call_count == 3
    assert sony_drain.call_count == 3
    assert drain_events.call_count == 3
    wait_for_capture_complete.assert_not_called()