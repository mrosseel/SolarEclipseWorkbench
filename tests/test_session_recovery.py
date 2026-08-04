"""A session that died with live view running must not wedge the next one.

4 August: an X-T4 refused SetPriorityMode in both directions for twenty
seconds while answering every read normally.  It was not a wedged body and it
did not need a power cycle - it was still in live view from a process that had
crashed.  One StopLiveView freed it immediately.
"""

import ctypes

from fujixsdk import _constants as C
from fujixsdk.camera import Camera


class _FakeLib:
    """Records the order of calls and refuses priority until live view stops."""

    def __init__(self, live_view_running=True):
        self.calls = []
        self._live_view_running = live_view_running

    def XSDK_Release(self, handle, mode, shot_opt, af_status):
        self.calls.append("release_cancel")
        return C.COMPLETE

    def XSDK_SetPriorityMode(self, handle, mode):
        self.calls.append("set_priority")
        # The body's actual behaviour: busy while live view runs.
        return 0x1006 if self._live_view_running else C.COMPLETE

    def XSDK_GetBufferCapacity(self, handle, captured, total):
        return C.COMPLETE

    def XSDK_Close(self, handle):
        # __del__ closes the session; without this the teardown raises
        # unraisably and pytest reports a warning for every test here.
        self.calls.append("close")
        return C.COMPLETE

    def stop_live_view(self):
        self._live_view_running = False


def _camera(lib):
    cam = object.__new__(Camera)
    cam._lib_inst = lib
    cam._handle = ctypes.c_void_p()
    cam._closed = False
    cam.drain_buffer = lambda: lib.calls.append("drain") or 0
    cam.stop_live_view = lambda: (lib.calls.append("stop_live_view"),
                                  lib.stop_live_view())[0]
    cam.shoot = lambda *a, **k: lib.calls.append("FLUSH SHOT")
    return cam


def test_live_view_is_stopped_before_priority_is_attempted():
    lib = _FakeLib(live_view_running=True)
    cam = _camera(lib)

    cam._cleanup_stale_state()

    assert "stop_live_view" in lib.calls, "never stopped the stream"
    assert lib.calls.index("stop_live_view") < lib.calls.index("set_priority"), \
        "went for priority while the body was still in live view"


def test_no_shutter_is_fired_when_stopping_live_view_is_enough():
    # The flush shot cannot clear a live view, and costs an actuation to find
    # out.  On a body with 200k shutter actuations of life this matters, and
    # during an eclipse it is a frame.
    lib = _FakeLib(live_view_running=True)
    cam = _camera(lib)

    cam._cleanup_stale_state()

    assert "FLUSH SHOT" not in lib.calls, "fired the shutter to fix a live view"


def test_a_clean_body_is_left_alone():
    lib = _FakeLib(live_view_running=False)
    cam = _camera(lib)

    cam._cleanup_stale_state()

    assert "FLUSH SHOT" not in lib.calls
    assert lib.calls.count("set_priority") == 1, "kept poking a working body"
