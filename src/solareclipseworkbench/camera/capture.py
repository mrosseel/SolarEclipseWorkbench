import logging
import time

import gphoto2
import gphoto2 as gp
from gphoto2 import Camera

from .types import BaseCamera, CameraSettings, _set_gp_config


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
        context, config = _adapt_camera_settings(camera, camera_settings)

        # If _adapt_camera_settings returned None context, it's a virtual/non-gphoto
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


def _adapt_camera_settings(camera, camera_settings):
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
        context, config = _adapt_camera_settings(camera, camera_settings)

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
        context, config = _adapt_camera_settings(camera, camera_settings)

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

        context, config = _adapt_camera_settings(camera, camera_settings)

        # Set mirror lock back to off
        lock = gp.check_result(gp.gp_widget_get_child_by_name(config, 'mirrorlock'))
        gp.gp_widget_set_value(lock, "0")
        # set config
        gp.gp_camera_set_config(target, config, context)
