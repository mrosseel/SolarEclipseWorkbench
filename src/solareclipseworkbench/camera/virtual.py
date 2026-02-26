import time
from typing import Any

import gphoto2 as gp

from .base import BaseCamera, CameraError


class VirtualCamera(BaseCamera):
    """Simple virtual camera for simulator mode. Returns a plain
    numpy image (H, W, 3) of the configured resolution. This is a
    minimal stub that will be extended with modes (static, scripted,
    generated) in subsequent steps.
    """

    def __init__(self):
        super().__init__(name="VirtualCamera")

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def configure(self, **kwargs: Any) -> None:
        # Accept resolution, mode, source, fps, timestamp_mode
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)

    def capture(self):
        """Return a single RGB frame as a numpy.ndarray (H, W, 3).

        Raises CameraError if not connected.
        """
        if not self._connected:
            raise CameraError("VirtualCamera is not connected")

        return

    # GPhoto-style stubs so the VirtualCamera can be used in places where
    # code expects gphoto Camera-like methods.
    class _WidgetStub:
        def __init__(self, name: str, value: Any):
            self._name = name
            self._value = value

        def get_value(self):
            return self._value

        def set_value(self, v: Any):
            self._value = v

        def get_type(self):
            # Mimic GP_WIDGET_DATE for datetime if value is numeric
            try:
                float(self._value)
                return gp.GP_WIDGET_DATE
            except Exception:
                return gp.GP_WIDGET_TEXT


    class _ConfigStub:
        def __init__(self, parent):
            self.parent = parent

        def get_child_by_name(self, name: str):
            # Return a widget-like stub with plausible defaults
            name = name.lower()
            if name in ('focusmode',):
                return VirtualCamera._WidgetStub(name, 'Manual')
            if name in ('batterylevel',):
                return VirtualCamera._WidgetStub(name, '67%')
            if name in ('autoexposuremodedial',):
                return VirtualCamera._WidgetStub(name, 'Manual')
            if name in ('expprogram',):
                return VirtualCamera._WidgetStub(name, 'M')
            if name in ('datetime', 'datetimeutc', 'd034'):
                # return an ISO formatted string for convenience
                return VirtualCamera._WidgetStub(name, time.strftime('%Y-%m-%d %H:%M:%S'))
            # Generic fallback
            return VirtualCamera._WidgetStub(name, '')

    def get_config(self):
        """Return a minimal config-like object for compatibility with callers
        that expect `get_config().get_child_by_name(name).get_value()`.
        """
        return VirtualCamera._ConfigStub(self)

    def set_config(self, config) -> None:
        # no-op for virtual camera
        return None

    def get_storageinfo(self):
        """Return a list-like object with storage info entries that have
        `freekbytes` and `capacitykbytes` attributes. Values are large
        defaults since virtual camera has plentiful space.
        """
        class _StorageEntry:
            def __init__(self):
                self.freekbytes = 34.9 * 1024 * 1024
                self.capacitykbytes = 64.9 * 1024 * 1024

        return [_StorageEntry()]

    def exit(self):
        # mirror gphoto Camera.exit()
        self.disconnect()
