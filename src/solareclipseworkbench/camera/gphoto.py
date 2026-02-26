import logging
import threading
from typing import Any

import gphoto2 as gp

from .base import BaseCamera, CameraError, CameraSettings


class GPhotoCamera(BaseCamera):
    """Adapter that wraps a gphoto2 Camera object and exposes a
    `vendor` attribute so higher-level code can branch on vendor-less
    checks via the adapter.
    """

    def __init__(self, gp_camera, name: str):
        super().__init__(name=name)
        self._camera = gp_camera
        self.vendor = 'Canon' if 'Canon' in name else ('Nikon' if 'Nikon' in name else None)
        self._lock = threading.Lock()
        self._last_settings: CameraSettings | None = None
        logging.debug('GPhotoCamera created for %s, vendor=%s', name, self.vendor)
        # camera returned by get_camera() is already initialised
        self._connected = True

    def __getattr__(self, item):
        # Delegate attribute access to the underlying gphoto Camera
        return getattr(self._camera, item)

    def connect(self) -> None:
        # Already initialised in get_camera
        self._connected = True

    def disconnect(self) -> None:
        try:
            self._camera.exit()
        except Exception:
            pass
        self._connected = False

    def configure(self, **kwargs: Any) -> None:
        # noop -- configuration typically done via gp widgets in other functions
        return None

    def capture(self, *args, **kwargs):
        return self._camera.capture(*args, **kwargs)


# Backward-compatible alias
GPhotoCameraAdapter = GPhotoCamera
