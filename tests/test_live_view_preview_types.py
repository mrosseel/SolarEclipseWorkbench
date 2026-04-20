import threading
from unittest.mock import patch

from solareclipseworkbench.camera import LiveViewThread


class _DummyLock:
    def __init__(self):
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self, timeout=None):
        self.acquire_calls += 1
        return True

    def release(self):
        self.release_calls += 1


class _DelegatingPreviewCamera:
    """Camera-like object that exposes capture_preview but is not VirtualCamera.

    This mirrors wrapped gphoto camera adapters that can surface a delegated
    capture_preview() method. LiveViewThread must not treat this as virtual mode.
    """

    def __init__(self):
        self._camera = object()
        self._usb_lock = _DummyLock()
        self.capture_preview_calls = 0

    def capture_preview(self):
        self.capture_preview_calls += 1
        return object()


def test_live_view_thread_uses_gphoto_path_for_non_virtual_capture_preview():
    camera = _DelegatingPreviewCamera()
    got_frame = threading.Event()
    received = []

    def _on_frame(jpeg_bytes):
        received.append(jpeg_bytes)
        got_frame.set()

    expected_bytes = b"\xff\xd8jpeg-preview\xff\xd9"

    with patch("solareclipseworkbench.camera.gp.gp_context_new", return_value=object()), \
         patch("solareclipseworkbench.camera.gp.CameraFile", return_value=object()) as camera_file_ctor, \
         patch("solareclipseworkbench.camera.gp.gp_camera_capture_preview", return_value=0) as capture_preview_call, \
         patch("solareclipseworkbench.camera.gp.gp_file_get_data_and_size", return_value=expected_bytes), \
         patch("solareclipseworkbench.camera.gp.check_result", side_effect=lambda x: x):

        thread = LiveViewThread(camera=camera, frame_callback=_on_frame, interval_s=0.001, lock_timeout=0.01)
        thread.start()
        try:
            assert got_frame.wait(0.5), "Timed out waiting for a preview frame"
        finally:
            thread.stop()
            thread.join(timeout=1.0)

    assert received, "No preview frame was delivered"
    assert isinstance(received[0], bytes)
    assert received[0] == expected_bytes

    # Regression check: non-virtual camera must not go down the delegated capture_preview path.
    assert camera.capture_preview_calls == 0

    # gphoto path should have been used.
    assert camera_file_ctor.called
    assert capture_preview_call.called
