import locale
import logging
import threading
import time

import gphoto2
import gphoto2 as gp
from datetime import datetime

from gphoto2 import Camera


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


class GPhotoCameraAdapter(BaseCamera):
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
        logging.debug('GPhotoCameraAdapter created for %s, vendor=%s', name, self.vendor)
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


class CanonCamera(GPhotoCameraAdapter):
    """Adapter specialized for Canon cameras. Future Canon-specific
    helpers can be added here."""

    def __init__(self, gp_camera, name: str):
        super().__init__(gp_camera, name)
        self.vendor = 'Canon'


class NikonCamera(GPhotoCameraAdapter):
    """Adapter specialized for Nikon cameras. Future Nikon-specific
    helpers can be added here."""

    def __init__(self, gp_camera, name: str):
        super().__init__(gp_camera, name)
        self.vendor = 'Nikon'



def take_picture(camera: Camera, camera_settings: CameraSettings) -> None:
    """ Take a picture with the selected camera

    Args:
        - camera_name: Camera object
        - camera_settings: Settings of the camera (exposure, f, iso)
    """
    lock = getattr(camera, '_lock', None)
    if lock:
        lock.acquire()
    try:
        context, config = __adapt_camera_settings(camera, camera_settings)

        # If __adapt_camera_settings returned None context, it's a virtual/non-gphoto
        # camera: call its capture() method directly and return.
        if context is None:
            try:
                camera.capture()
                return
            except Exception as e:
                logging.exception('Virtual camera capture failed: %s', e)
                raise

        # Take picture for real gphoto cameras
        try:
            camera.capture(gp.GP_CAPTURE_IMAGE, context)
        except Exception as e:
            logging.exception('High-level capture failed, attempting low-level gp capture: %s', e)
            # Fallback to lower-level gphoto call if possible
            try:
                target = camera._camera if hasattr(camera, '_camera') else camera
                ctx = gp.gp_context_new()
                file_path = gp.check_result(gp.gp_camera_capture(target, gp.GP_CAPTURE_IMAGE, ctx))
                logging.info('Low-level capture returned file path: %s', file_path)
            except Exception:
                logging.exception('Low-level gp capture also failed')
                raise
    finally:
        if lock:
            lock.release()


def __adapt_camera_settings(camera, camera_settings):
    # For virtual or non-gphoto cameras, skip gphoto configuration and
    # return (None, None) so callers can handle capture directly.
    if isinstance(camera, BaseCamera) and not hasattr(camera, '_camera'):
        # For Fuji cameras, apply settings via SDK before returning
        if getattr(camera, 'vendor', None) == 'Fuji':
            camera.configure(
                shutter_speed=camera_settings.shutter_speed,
                aperture=camera_settings.aperture,
                iso=camera_settings.iso,
            )
        return None, None

    # Skip reconfiguration if settings unchanged
    if (hasattr(camera, '_last_settings') and camera._last_settings is not None
            and camera._last_settings.shutter_speed == camera_settings.shutter_speed
            and camera._last_settings.aperture == camera_settings.aperture
            and camera._last_settings.iso == camera_settings.iso):
        context = gp.gp_context_new()
        target = camera._camera if hasattr(camera, '_camera') else camera
        config = gp.check_result(gp.gp_camera_get_config(target, context))
        return context, config

    context = gp.gp_context_new()
    target = camera._camera if hasattr(camera, '_camera') else camera
    config = gp.check_result(gp.gp_camera_get_config(target, context))
    
    vendor = getattr(camera, 'vendor', None)
    
    # Set camera to Manual mode first (required for full control of settings)
    if vendor == 'Nikon':
        try:
            # Set exposure program to Manual (M = 1)
            exp_program = gp.check_result(gp.gp_widget_get_child_by_name(config, 'expprogram'))
            gp.gp_widget_set_value(exp_program, "1")  # 1 = Manual mode (must be string)
            _set_gp_config(target, config, context)
            logging.debug('Set Nikon camera to Manual mode')
        except gphoto2.GPhoto2Error as e:
            logging.warning('Could not set Nikon camera to Manual mode: %s', e)
    elif vendor == 'Canon':
        try:
            # Set autoexposuremode to Manual
            ae_mode = gp.check_result(gp.gp_widget_get_child_by_name(config, 'autoexposuremodedial'))
            gp.gp_widget_set_value(ae_mode, "Manual")
            _set_gp_config(target, config, context)
            logging.debug('Set Canon camera to Manual mode')
        except gphoto2.GPhoto2Error as e:
            logging.warning('Could not set Canon camera to Manual mode: %s', e)
    
    # Set ISO
    if vendor == 'Nikon':
        try:
            gp.gp_widget_set_value(gp.check_result(gp.gp_widget_get_child_by_name(config, 'autoiso')), str("Off"))
            # set config
            _set_gp_config(target, config, context)
        except gphoto2.GPhoto2Error as e:
            logging.debug('Could not disable auto ISO: %s', e)

    gp.gp_widget_set_value(gp.check_result(gp.gp_widget_get_child_by_name(config, 'iso')), str(camera_settings.iso))
    # set config
    gp.gp_camera_set_config(target, config, context)
    time.sleep(0.1)

    # Set aperture
    try:
        if getattr(camera, 'vendor', None) == 'Canon':
            gp.gp_widget_set_value(gp.check_result(gp.gp_widget_get_child_by_name(config, 'aperture')),
                                   str(camera_settings.aperture))
        elif getattr(camera, 'vendor', None) == 'Nikon':
            gp.gp_widget_set_value(gp.check_result(gp.gp_widget_get_child_by_name(config, 'f-number')),
                                   str(camera_settings.aperture))
        # set config
        _set_gp_config(target, config, context)
        time.sleep(0.1)
    except gphoto2.GPhoto2Error:
        pass

    # Set shutter speed
    gp.gp_widget_set_value(gp.check_result(gp.gp_widget_get_child_by_name(config, 'shutterspeed')),
                           str(camera_settings.shutter_speed))
    # set config
    _set_gp_config(camera, config, context)
    time.sleep(0.1)

    camera._last_settings = camera_settings

    return context, config


def take_burst(camera: Camera, camera_settings: CameraSettings, duration: float) -> None:
    """ Take a burst with the selected camera.  For Canon, the duration is the duration in seconds, for Nikon, the
        duration is the number of pictures to take.

    Args:
        - camera_name: Camera object
        - camera_settings: Settings of the camera (exposure, f, iso)
        - duration: Duration of the burst in seconds (Canon) or number of pictures (Nikon)
    """
    lock = getattr(camera, '_lock', None)
    if lock:
        lock.acquire()
    try:
        context, config = __adapt_camera_settings(camera, camera_settings)

        # Non-gphoto cameras (virtual, Fuji, etc.)
        if context is None:
            if getattr(camera, 'vendor', None) == 'Fuji':
                camera.configure(shutter_speed=camera_settings.shutter_speed,
                                 aperture=camera_settings.aperture,
                                 iso=camera_settings.iso)
                camera.shooter.burst_no_download(max(1, int(round(duration))))
                return
            try:
                n = max(1, int(round(duration)))
                for _ in range(n):
                    camera.capture()
                return
            except Exception:
                logging.exception('Virtual burst capture failed')
                raise

        # Take picture for real cameras
        if getattr(camera, 'vendor', None) == 'Canon':
            # Push the button
            remote_release = gp.check_result(gp.gp_widget_get_child_by_name(config, 'eosremoterelease'))
            gp.gp_widget_set_value(remote_release, "Press Full")
            # set config
            _set_gp_config(camera, config, context)
            time.sleep(duration)

            # Release the button
            remote_release = gp.check_result(gp.gp_widget_get_child_by_name(config, 'eosremoterelease'))
            gp.gp_widget_set_value(remote_release, "Release Full")
            # set config
            _set_gp_config(camera, config, context)
        elif getattr(camera, 'vendor', None) == 'Nikon':
            # Set capture mode to burst/continuous
            # Try different widget names (differs between DSLR and mirrorless models)
            try:
                # Try 'capturemode' first (older DSLRs)
                capture_mode = gp.check_result(gp.gp_widget_get_child_by_name(config, 'capturemode'))
                gp.gp_widget_set_value(capture_mode, "Burst")
                _set_gp_config(camera, config, context)
                logging.debug('Set capturemode to Burst')
            except gphoto2.GPhoto2Error:
                try:
                    # Try 'stillcapturemode' (newer mirrorless like Z8/Z9)
                    # Value 2 is typically Continuous Low, higher values for High Speed
                    capture_mode = gp.check_result(gp.gp_widget_get_child_by_name(config, 'stillcapturemode'))
                    gp.gp_widget_set_value(capture_mode, 2)  # Continuous shooting mode
                    _set_gp_config(camera, config, context)
                    logging.debug('Set stillcapturemode to Continuous (2)')
                except gphoto2.GPhoto2Error as e:
                    logging.warning('Could not set Nikon burst/continuous mode: %s', e)

            # Set burst number
            try:
                burst_number = gp.check_result(gp.gp_widget_get_child_by_name(config, 'burstnumber'))
                gp.gp_widget_set_value(burst_number, round(duration))
                _set_gp_config(camera, config, context)
            except gphoto2.GPhoto2Error as e:
                logging.warning('Could not set Nikon burst number: %s', e)

            try:
                camera.capture(gp.GP_CAPTURE_IMAGE, context)
            except Exception:
                logging.exception('Nikon high-level capture failed, trying low-level gp capture')
                try:
                    target = camera._camera if hasattr(camera, '_camera') else camera
                    ctx = gp.gp_context_new()
                    gp.check_result(gp.gp_camera_capture(target, gp.GP_CAPTURE_IMAGE, ctx))
                except Exception:
                    logging.exception('Nikon low-level capture failed')
                    raise
    finally:
        if lock:
            lock.release()


def take_bracket(camera: Camera, camera_settings: CameraSettings, steps: str) -> None:
    """ Take a bracketing of images with the selected camera.

    Args:
        - camera_name: Camera object
        - camera_settings: Settings of the camera (exposure, f, iso)
        - steps: Steps for each bracketing step (e.g. +/- 1 2/3)
    """
    lock = getattr(camera, '_lock', None)
    if lock:
        lock.acquire()
    try:
        context, config = __adapt_camera_settings(camera, camera_settings)

        # Non-gphoto cameras (virtual, Fuji, etc.)
        if context is None:
            if getattr(camera, 'vendor', None) == 'Fuji':
                camera.configure(shutter_speed=camera_settings.shutter_speed,
                                 aperture=camera_settings.aperture,
                                 iso=camera_settings.iso)
                speeds = camera.parse_bracket_speeds(steps)
                camera.shooter.bracket_no_download(speeds)
                return
            try:
                for _ in range(3):
                    camera.capture()
                return
            except Exception:
                logging.exception('Virtual bracket capture failed')
                raise

        if getattr(camera, 'vendor', None) == 'Canon':
            # Set aeb
            aeb = gp.check_result(gp.gp_widget_get_child_by_name(config, 'aeb'))
            gp.gp_widget_set_value(aeb, steps)
            # set config
            _set_gp_config(camera, config, context)

            for _ in range(5):
                try:
                    camera.capture(gp.GP_CAPTURE_IMAGE, context)
                except Exception:
                    logging.exception('Bracket capture high-level failed, trying low-level gp capture')
                    try:
                        target = camera._camera if hasattr(camera, '_camera') else camera
                        ctx = gp.gp_context_new()
                        gp.check_result(gp.gp_camera_capture(target, gp.GP_CAPTURE_IMAGE, ctx))
                    except Exception:
                        logging.exception('Bracket low-level capture failed')
                        raise

            # Set aeb
            aeb = gp.check_result(gp.gp_widget_get_child_by_name(config, 'aeb'))
            gp.gp_widget_set_value(aeb, "off")
            # set config
            _set_gp_config(camera, config, context)
    finally:
        if lock:
            lock.release()


def mirror_lock(camera: Camera, camera_settings: CameraSettings) -> None:
    """ Lock the mirror

    Args:
        - camera_name: Camera object
    """
    # For virtual cameras, nothing to do
    if isinstance(camera, BaseCamera) and not hasattr(camera, '_camera'):
        return

    context = gp.gp_context_new()
    target = camera._camera if hasattr(camera, '_camera') else camera
    config = gp.check_result(gp.gp_camera_get_config(target, context))

    if getattr(camera, 'vendor', None) == 'Canon':
        lock = gp.check_result(gp.gp_widget_get_child_by_name(config, 'mirrorlock'))
        gp.gp_widget_set_value(lock, "1")
        # set config
        gp.gp_camera_set_config(target, config, context)

        context, config = __adapt_camera_settings(camera, camera_settings)

        # # Push the button
        # remote_release = gp.check_result(gp.gp_widget_get_child_by_name(config, 'eosremoterelease'))
        # gp.gp_widget_set_value(remote_release, "Press 2")
        # # set config
        # gp.gp_camera_set_config(camera, config, context)

        # Release the button
        # remote_release = gp.check_result(gp.gp_widget_get_child_by_name(config, 'eosremoterelease'))
        # gp.gp_widget_set_value(remote_release, "Release Full")
        # # set config
        # gp.gp_camera_set_config(camera, config, context)

        # Set mirror lock back to off
        lock = gp.check_result(gp.gp_widget_get_child_by_name(config, 'mirrorlock'))
        gp.gp_widget_set_value(lock, "0")
        # set config
        gp.gp_camera_set_config(target, config, context)


def get_cameras() -> list:
    """ Returns a list with the cameras.

    Returns: List with all the attached cameras ([name, USB port]).
    """

    locale.setlocale(locale.LC_ALL, '')

    gp.check_result(gp.use_python_logging())
    # make a list of all available cameras
    detected = list(gp.Camera.autodetect())
    logging.debug('gphoto autodetect returned %d entries', len(detected))
    for d in detected:
        logging.debug('Detected camera: %s @ %s', d[0], d[1])
    return detected


def __get_address(camera_name: str) -> str:
    """ Gets the address of the camera if the name is given 
    
    Args:
        - camera_name: Name of the camera
    
    Returns: Address of the camera
    """

    camera_list = get_cameras()
    logging.debug('Searching for camera address for %s among %d cameras', camera_name, len(camera_list))
    camera_tuple = [camera[1] for camera in camera_list if camera[0] == camera_name]
    try:
        return camera_tuple[0]
    except IndexError:
        logging.debug('Camera %s not found in autodetect list', camera_name)
        raise CameraError(f"Camera {camera_name} not found")


def get_camera(camera_name: str):
    """ Returns the initialized camera object of the selected camera

    Args: 
        - camera_name: Name of the camera
    
    Returns: Initialized camera object of the selected camera.
    """

    addr = __get_address(camera_name)
    if addr == '':
        return ''
    logging.debug('get_camera(%s): address=%s', camera_name, addr)

    # prepare variable in case initialization fails early
    camera = None

    # get port info
    port_info_list = gp.PortInfoList()
    port_info_list.load()
    abilities_list = gp.CameraAbilitiesList()
    abilities_list.load()

    camera = gp.Camera()
    idx = port_info_list.lookup_path(addr)
    camera.set_port_info(port_info_list[idx])
    idx = abilities_list.lookup_model(camera_name)
    camera.set_abilities(abilities_list[idx])

    context = gp.gp_context_new()

    # Initialize the camera
    try:
        camera.init(context)

        # find the capture target config item (to save to the memory card)
        config = gp.check_result(gp.gp_camera_get_config(camera, context))
        capture_target = gp.check_result(gp.gp_widget_get_child_by_name(config, 'capturetarget'))
        # set value
        value = gp.check_result(gp.gp_widget_get_choice(capture_target, 1))
        gp.gp_widget_set_value(capture_target, value)
        # set config
        gp.gp_camera_set_config(camera, config, context)

        # Try to set the drivemode to Continuous high speed (for cameras that support it)
        # Note: Not all cameras have this widget (e.g., Nikon Z-series mirrorless cameras)
        try:
            drive_mode = gp.check_result(gp.gp_widget_get_child_by_name(config, 'drivemode'))
            gp.gp_widget_set_value(drive_mode, "Continuous high speed")
            # set config
            gp.gp_camera_set_config(camera, config, context)
            logging.debug('Set drivemode to Continuous high speed for %s', camera_name)
        except gphoto2.GPhoto2Error as e:
            # drivemode widget doesn't exist or value not supported - this is OK for many cameras
            logging.debug('Could not set drivemode for %s (this is normal for some camera models): %s', camera_name, e)
    except gphoto2.GPhoto2Error as e:
        logging.exception('gphoto2 error while initializing camera %s: %s', camera_name, e)
        # continue and attempt to wrap camera object anyway

    # Wrap the gphoto camera in a vendor-aware adapter
    if camera is None:
        logging.error('get_camera: camera object was not created for %s', camera_name)
        raise CameraError(f'Could not create camera object for {camera_name}')

    if "Canon" in camera_name:
        logging.debug('Wrapping camera %s as CanonCamera', camera_name)
        return CanonCamera(camera, camera_name)
    elif "Nikon" in camera_name:
        logging.debug('Wrapping camera %s as NikonCamera', camera_name)
        return NikonCamera(camera, camera_name)
    else:
        logging.debug('Wrapping camera %s as generic GPhotoCameraAdapter', camera_name)
        return GPhotoCameraAdapter(camera, camera_name)


def get_free_space(camera: Camera) -> float:
    """ Return the free space on the card of the selected camera 
    
    Args: 
        - camera: Camera object

    Returns: Free space on the card of the camera [GB]
    """
    try:
        result = round(camera.get_storageinfo()[0].freekbytes / 1024 / 1024, 1)
        try:
            camera._cached_free_space = result  # cache for -53 fallback
        except Exception:
            pass
        return result
    except gphoto2.GPhoto2Error as e:
        # Error -53 means the OS has reclaimed the USB device (common on macOS when
        # ptpcamerad / Image Capture grabs the camera after gphoto2 releases it).
        # There is no point retrying or reinitialising – both will fail the same way.
        # Return the previously cached value if available, otherwise -1 as a sentinel.
        if getattr(e, 'code', None) == -53:
            cached = getattr(camera, '_cached_free_space', -1.0)
            logging.debug(
                'Camera %s USB reclaimed by OS (-53), returning cached free space %.1f GB',
                getattr(camera, 'name', str(camera)), cached)
            return cached
        # For other errors, try to reinitialise the camera once and retry
        try:
            if hasattr(camera, 'name'):
                logging.info('Reinitialising camera %s to retry storage query', camera.name)
                new_cam = get_camera(camera.name)
                try:
                    return round(new_cam.get_storageinfo()[0].freekbytes / 1024 / 1024, 1)
                except gphoto2.GPhoto2Error:
                    # Try lower level gp call with explicit context
                    try:
                        ctx = gp.gp_context_new()
                        stor = gp.check_result(gp.gp_camera_get_storageinfo(new_cam._camera if hasattr(new_cam, '_camera') else new_cam, ctx))
                        return round(stor[0].freekbytes / 1024 / 1024, 1)
                    except Exception:
                        raise
        except Exception:
            logging.exception('Reinitialisation attempt failed for %s', getattr(camera, 'name', str(camera)))
        # If recovery failed, propagate the original error so caller can handle it
        try:
            ctx = gp.gp_context_new()
            stor = gp.check_result(gp.gp_camera_get_storageinfo(camera._camera if hasattr(camera, '_camera') else camera, ctx))
            return round(stor[0].freekbytes / 1024 / 1024, 1)
        except Exception:
            raise CameraError(f"Could not read storage info for {getattr(camera, 'name', camera)}: {e}") from e
    except Exception:
        # For virtual or non-gphoto cameras return a large default value
        if isinstance(camera, BaseCamera) and not hasattr(camera, '_camera'):
            return 999.9
        raise


def get_space(camera: Camera) -> float:
    """ Return the size of the memory card of the selected camera 
    
    Args: 
        - camera: Camera object

    Returns: Size of memory card of the camera [GB]
    """

    try:
        result = round(camera.get_storageinfo()[0].capacitykbytes / 1024 / 1024, 1)
        try:
            camera._cached_total_space = result  # cache for -53 fallback
        except Exception:
            pass
        return result
    except gphoto2.GPhoto2Error as e:
        # Error -53: OS reclaimed the USB device (macOS ptpcamerad / Image Capture).
        # Return previously cached value if available.
        if getattr(e, 'code', None) == -53:
            cached = getattr(camera, '_cached_total_space', -1.0)
            logging.debug(
                'Camera %s USB reclaimed by OS (-53), returning cached total space %.1f GB',
                getattr(camera, 'name', str(camera)), cached)
            return cached
        logging.warning('gphoto2 error reading capacity for %s: %s', getattr(camera, 'name', str(camera)), e)
        # Try to reinitialise the camera once and retry
        try:
            if hasattr(camera, 'name'):
                logging.info('Reinitialising camera %s to retry capacity query', camera.name)
                new_cam = get_camera(camera.name)
                try:
                    return round(new_cam.get_storageinfo()[0].capacitykbytes / 1024 / 1024, 1)
                except gphoto2.GPhoto2Error:
                    try:
                        ctx = gp.gp_context_new()
                        stor = gp.check_result(gp.gp_camera_get_storageinfo(new_cam._camera if hasattr(new_cam, '_camera') else new_cam, ctx))
                        return round(stor[0].capacitykbytes / 1024 / 1024, 1)
                    except Exception:
                        raise
        except Exception:
            logging.exception('Reinitialisation attempt failed for %s', getattr(camera, 'name', str(camera)))
        # If recovery failed, propagate the original error so caller can handle it
        try:
            ctx = gp.gp_context_new()
            stor = gp.check_result(gp.gp_camera_get_storageinfo(camera._camera if hasattr(camera, '_camera') else camera, ctx))
            return round(stor[0].capacitykbytes / 1024 / 1024, 1)
        except Exception:
            raise CameraError(f"Could not read storage capacity for {getattr(camera, 'name', camera)}: {e}") from e
    except Exception:
        # For virtual or non-gphoto cameras return a large default value
        if isinstance(camera, BaseCamera) and not hasattr(camera, '_camera'):
            return 999.9
        raise


def get_shooting_mode(camera_name: str, camera: Camera) -> str:
    """ Return the shooting mode of the selected camera. Should be "Manual".
    
    Args: 
        - camera: Camera object

    Returns: Shooting mode of the camera
    """
    # For virtual/non-gphoto cameras assume Manual shooting mode
    if isinstance(camera, BaseCamera) and not hasattr(camera, '_camera'):
        return "Manual"

    vendor = getattr(camera, 'vendor', None)
    try:
        if vendor == 'Fuji':
            return camera.get_config().get_child_by_name('autoexposuremodedial').get_value()
        elif vendor == 'Canon':
            return camera.get_config().get_child_by_name('autoexposuremodedial').get_value()
        elif vendor == 'Nikon':
            mode = camera.get_config().get_child_by_name('expprogram').get_value()
            if mode == "M":
                return "Manual"
            else:
                return mode
        else:
            return ""
    except gphoto2.GPhoto2Error as e:
        logging.warning('gphoto2 error reading shooting mode for %s: %s', getattr(camera, 'name', str(camera)), e)
        return "Manual"
    except Exception:
        # For virtual/non-gphoto cameras assume Manual shooting mode
        if isinstance(camera, BaseCamera) and not hasattr(camera, '_camera'):
            return "Manual"
        raise


def get_focus_mode(camera: Camera) -> str:
    """ Return the focus mode of the selected camera. Should be "Manual"
    
    Args: 
        - camera: Camera object

    Returns: Focus mode of the camera
    """

    try:
        return camera.get_config().get_child_by_name('focusmode').get_value()
    except gphoto2.GPhoto2Error as e:
        logging.warning('gphoto2 error reading focus mode for %s: %s', getattr(camera, 'name', str(camera)), e)
        return "Manual"
    except Exception:
        # For virtual/non-gphoto cameras assume Manual focus
        if isinstance(camera, BaseCamera) and not hasattr(camera, '_camera'):
            return "Manual"
        raise


def get_battery_level(camera: Camera) -> str:
    """ Return the battery level of the selected camera 
    
    Args: 
        - camera: Name of the camera

    Returns: Current battery level of the camera [%]
    """

    try:
        return camera.get_config().get_child_by_name('batterylevel').get_value()
    except gphoto2.GPhoto2Error as e:
        if getattr(e, 'code', None) == -53:
            # Camera is still busy (USB claimed) after a recent capture - this is normal
            logging.debug('Camera %s busy (USB claimed), skipping battery read', getattr(camera, 'name', str(camera)))
        else:
            logging.warning('gphoto2 error reading battery level for %s: %s', getattr(camera, 'name', str(camera)), e)
        # Return unknown battery level to avoid crashing the UI
        return "Unknown"
    except Exception:
        # VirtualCamera and other non-gphoto cameras don't expose battery info
        if isinstance(camera, BaseCamera) and not hasattr(camera, '_camera'):
            return "100%"
        raise


def get_time(camera: Camera) -> str:
    """ Returns the current time of the selected camera

    Args: 
        - camera: Camera object

    Returns: Current time of the camera
    """

    # For virtual/non-gphoto cameras return host time quickly
    if isinstance(camera, BaseCamera) and not hasattr(camera, '_camera'):
        return datetime.now().isoformat(' ')

    # get configuration tree
    try:
        config = camera.get_config()
    except gphoto2.GPhoto2Error as e:
        logging.warning('gphoto2 error reading camera time for %s: %s', getattr(camera, 'name', str(camera)), e)
        # Attempt reinitialisation and retry once
        try:
            if hasattr(camera, 'name'):
                logging.info('Reinitialising camera %s to retry time query', camera.name)
                new_cam = get_camera(camera.name)
                config = new_cam.get_config()
        except Exception:
            logging.exception('Reinitialisation attempt failed for %s', getattr(camera, 'name', str(camera)))
            # If retry failed, propagate the original error so the caller can handle it
            raise
    except Exception:
        # If any other error, fall back to host time
        return datetime.now().isoformat(' ')
    # find the date/time setting config item and get it
    # name varies with camera driver
    #   Canon EOS - 'datetime'
    #   PTP - 'd034'
    for name, fmt in (('datetime', '%Y-%m-%d %H:%M:%S'),
                      ('d034', None)):
        now = datetime.now()
        ok, datetime_config = gp.gp_widget_get_child_by_name(config, name)
        if ok >= gp.GP_OK:
            widget_type = datetime_config.get_type()
            raw_value = datetime_config.get_value()
            if widget_type == gp.GP_WIDGET_DATE:
                camera_time = datetime.fromtimestamp(raw_value)
            else:
                if fmt:
                    camera_time = datetime.strptime(raw_value, fmt)
                else:
                    camera_time = datetime.utcfromtimestamp(float(raw_value))
            logging.info('Camera clock:  ', camera_time.isoformat(' '))
            logging.info('Computer clock:', now.isoformat(' '))
            err = now - camera_time
            if err.days < 0:
                err = -err
                lead_lag = 'ahead'
                logging.info('Camera clock is ahead by', )
            else:
                lead_lag = 'behind'
            logging.warning('Camera clock is %s by %d days and %d seconds' % (
                lead_lag, err.days, err.seconds))
            break
    else:
        logging.warning('Unknown date/time config item')
        return "Unknown date/time config item"

    return camera_time.isoformat(' ')


def set_time(camera: Camera) -> None:
    """ Set the computer time on the selected camera """
    # For physical gphoto cameras we set the camera clock; for virtual or
    # non-gphoto cameras this is a no-op.
    try:
        config = camera.get_config()
    except gphoto2.GPhoto2Error as e:
        logging.warning('gphoto2 error getting config for %s: %s', getattr(camera, 'name', str(camera)), e)
        # Attempt reinitialisation and retry once
        try:
            if hasattr(camera, 'name'):
                logging.info('Reinitialising camera %s to retry set_time', camera.name)
                camera = get_camera(camera.name)
                config = camera.get_config()
        except Exception:
            logging.exception('Reinitialisation attempt failed for %s', getattr(camera, 'name', str(camera)))
            # propagate error to caller
            raise
    except Exception:
        # VirtualCamera or other adapters may not expose get_config(); skip
        return

    if __set_datetime(config):
        # apply the changed config
        try:
            camera.set_config(config)
        except Exception:
            logging.error('Could not apply date & time to camera')
    else:
        logging.error('Could not set date & time')


def __set_datetime(config) -> bool:
    """ Private method to set the date and time of the camera. """
    # Try gphoto widget API first
    try:
        ok, date_config = gp.gp_widget_get_child_by_name(config, 'datetimeutc')
        if ok == -2:
            ok, date_config = gp.gp_widget_get_child_by_name(config, 'datetime')

        if ok >= gp.GP_OK:
            widget_type = date_config.get_type()
            if widget_type == gp.GP_WIDGET_DATE:
                now = int(time.time())
                date_config.set_value(now)
            else:
                now = time.strftime('%Y-%m-%d %H:%M:%S')
                date_config.set_value(now)
            return True
    except Exception:
        # fall back to stub config API
        pass

    # Fallback for VirtualCamera._ConfigStub or other non-gphoto configs
    try:
        if hasattr(config, 'get_child_by_name'):
            for name in ('datetimeutc', 'datetime', 'd034'):
                try:
                    widget = config.get_child_by_name(name)
                except Exception:
                    widget = None
                if not widget:
                    continue
                try:
                    widget_type = widget.get_type()
                except Exception:
                    widget_type = None

                if widget_type == gp.GP_WIDGET_DATE:
                    now = int(time.time())
                    if hasattr(widget, 'set_value'):
                        widget.set_value(now)
                    else:
                        setattr(widget, '_value', now)
                else:
                    now = time.strftime('%Y-%m-%d %H:%M:%S')
                    if hasattr(widget, 'set_value'):
                        widget.set_value(now)
                    else:
                        setattr(widget, '_value', now)
                return True
    except Exception:
        pass

    return False


def get_camera_dict(is_simulator: bool = False) -> dict:
    """ Get a dictionary of camera names and their GPhoto2 camera object
    Returns: Dictionary of camera names and their GPhoto2 camera object
    """
    if is_simulator:
        # Return a single VirtualCamera for simulator mode
        vc = VirtualCamera()
        vc.connect()
        return {vc.name: vc}

    camera_names = get_cameras()
    # Print detected cameras to terminal for user visibility
    try:
        print("Found cameras:", camera_names, flush=True)
    except Exception:
        logging.debug('Could not print found cameras to terminal')

    cameras = dict()
    for camera_name in camera_names:
        cameras[camera_name[0]] = get_camera(camera_name[0])

    # Fuji SDK cameras
    try:
        from .fuji_adapter import detect_fuji_cameras, find_fuji_sdk_path
        sdk_path = find_fuji_sdk_path()
        if sdk_path:
            fuji_cams = detect_fuji_cameras(sdk_path)
            cameras.update(fuji_cams)
    except Exception:
        logging.debug('Fuji SDK detection skipped', exc_info=True)

    return cameras


def get_camera_overview() -> dict:
    """ Returns a dictionary with information of the connected cameras.

    The keys in the dictionary are the camera names and the values (the camera information) contains information about
    the battery level and space on the memory card of the camera.

    Returns: Dictionary with information of the connected cameras.
    """

    camera_overview = {}

    camera_names = get_cameras()
    for camera_name in camera_names:
        camera = get_camera(camera_name[0])

        try:
            battery_level = get_battery_level(camera)
            free_space = get_free_space(camera)
            total_space = get_space(camera)

            camera_overview[camera_name[0]] = CameraInfo(camera_name, battery_level, free_space, total_space)
            # camera.exit()
        except gp.GPhoto2Error:
            logging.error("Could not connect to the camera.  Did you start Solar Eclipse Workbench in sudo mode?")

    return camera_overview


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


def main():
    # Get cameras
    cameras = get_cameras()

    # Get all information for the gui
    get_camera_overview()

    # Get battery and free space
    for camera in cameras:
        try:
            camera_object = get_camera(camera[0])

            # Get general info
            print(f"{camera[0]}: {get_battery_level(camera_object)} - {get_free_space(camera_object)} GB "
                  f"of {get_space(camera_object)} GB free.")

            # Check if the lens and the camera are set to manual
            if get_shooting_mode(camera[0], camera_object) != "Manual":
                print("Set the camera in Manual mode!")
                exit()

            if get_focus_mode(camera_object) != "Manual":
                print("Set the lens in Manual mode!")
                exit()

            # Set the correct time
            print(get_time(camera_object))
            set_time(camera_object)

            # Take picture
            camera_settings = CameraSettings(camera[0], "1/1000", "8", 100)

            take_picture(camera_object, camera_settings)

            time.sleep(1)
            camera_settings = CameraSettings(camera[0], "1/200", "6.3", 400)
            # take_bracket(camera_object, camera_settings, "+/- 1 2/3")
            take_picture(camera_object, camera_settings)

            # Mirror lock
            # mirror_lock(camera_object, camera_settings)

            # take_picture(camera_object, camera_settings)

            time.sleep(1)
            camera_settings = CameraSettings(camera[0], "1/4000", "5.6", 200)
            take_burst(camera_object, camera_settings, 1)
            time.sleep(3)
            camera_object.exit()

        except gphoto2.GPhoto2Error:
            print("Could not connect to the camera.  Did you start Solar Eclipse Workbench in sudo mode?")


if __name__ == "__main__":
    main()
