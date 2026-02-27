import logging
import time
from datetime import datetime

import gphoto2
import gphoto2 as gp
from gphoto2 import Camera

from .types import BaseCamera, CameraError


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
                from .discovery import get_camera
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
                from .discovery import get_camera
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
        if getattr(e, 'code', None) != -53:
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
        if getattr(e, 'code', None) != -53:
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
                from .discovery import get_camera
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
    # For non-gphoto cameras (FujiCamera, VirtualCamera), time sync is a no-op
    # — FujiCamera clock is set via the SDK, not gphoto2.
    if isinstance(camera, BaseCamera) and not hasattr(camera, '_camera'):
        return
    try:
        config = camera.get_config()
    except gphoto2.GPhoto2Error as e:
        # -53 = USB claimed by another driver (e.g. Fuji SDK owns the device).
        # No point retrying — the SDK camera handles its own clock.
        if getattr(e, 'code', None) == -53:
            logging.debug('Camera %s USB owned by SDK, skipping time sync',
                          getattr(camera, 'name', str(camera)))
            return
        logging.warning('gphoto2 error getting config for %s: %s', getattr(camera, 'name', str(camera)), e)
        # Attempt reinitialisation and retry once
        try:
            if hasattr(camera, 'name'):
                logging.info('Reinitialising camera %s to retry set_time', camera.name)
                from .discovery import get_camera
                camera = get_camera(camera.name)
                config = camera.get_config()
        except Exception:
            logging.exception('Reinitialisation attempt failed for %s', getattr(camera, 'name', str(camera)))
            return
    except Exception:
        # VirtualCamera or other adapters may not expose get_config(); skip
        return

    if _set_datetime(config):
        # apply the changed config
        try:
            camera.set_config(config)
        except Exception:
            logging.error('Could not apply date & time to camera')
    else:
        logging.error('Could not set date & time')


def _set_datetime(config) -> bool:
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
