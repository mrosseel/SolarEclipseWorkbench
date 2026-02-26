import logging
import threading

import gphoto2
import gphoto2 as gp


class CameraError(Exception):
    pass


def _set_gp_config(camera, config, context):
    """Set camera config using underlying gphoto object when wrapped by adapter."""
    target = camera._camera if hasattr(camera, '_camera') else camera
    return gp.gp_camera_set_config(target, config, context)


class CameraSettings:

    def __init__(self, camera_name: str, shutter_speed: str, aperture: str, iso: int):
        """ Initialise new camera settings.

        Args:
            - camera_name: Name of the camera
            - shutter_speed: Exposure time [s], e.g. "1/2000".
            - aperture: Aperture (f-number), e.g. 5.6.
            - iso: ISO-value.
        """
        self.camera_name = camera_name
        self.shutter_speed = shutter_speed
        self.aperture = aperture
        self.iso = iso


from abc import ABC, abstractmethod
from typing import Any, Tuple, Optional


class BaseCamera(ABC):
    """Abstract base camera class. Implementations should provide the
    same public surface so higher-level code can use any camera
    interchangeably.
    """

    def __init__(self, name: str = ""):
        self.name = name
        self._connected = False

    @abstractmethod
    def connect(self) -> None:
        pass

    @abstractmethod
    def disconnect(self) -> None:
        pass

    @abstractmethod
    def configure(self, **kwargs: Any) -> None:
        pass

    @abstractmethod
    def capture(self):
        pass

    def is_connected(self) -> bool:
        return self._connected


class CameraInfo:

    def __init__(self, camera_name: str, battery_level: str, free_space: float, total_space: float) -> None:
        """ Create a new CameraInfo object.

        Args:
            - camera_name: Name of the camera
            - battery_level: Battery level [%]
            - free_space: Free space on the camera memory card [GB]
            - total_space: Total space on the camera memory card [GB]
        """

        self.camera_name = camera_name
        self.battery_level = battery_level
        self.free_space = free_space
        self.total_space = total_space

    def get_camera_name(self) -> str:
        """ Returns the name of the camera.

        Returns: Name of the camera.
        """
        return self.camera_name[0]

    def get_battery_level(self) -> str:
        """ Returns the battery level of the camera.

        Returns: Battery level of the camera [%].
        """
        return self.battery_level

    def get_absolute_free_space(self) -> float:
        """ Returns the absolute free space on the memory card of the camera.

        Returns: Free space on the memory card of the camera [GB].
        """

        return self.free_space

    def get_relative_free_space(self) -> float:
        """ Returns the relative free space on the memory card of the camera.

        Returns: Free space on the memory card of the camera [%].
        """

        return self.get_absolute_free_space() / self.get_total_space() * 100

    def get_total_space(self) -> float:
        """ Returns the total space on the memory card of the camera.

        Returns: Total space on the memory card of the camera [GB].
        """
        return self.total_space
