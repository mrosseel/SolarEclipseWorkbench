import locale
import logging

import gphoto2
import gphoto2 as gp

from .types import BaseCamera, CameraError, CameraInfo
from .adapters import VirtualCamera, GPhotoCameraAdapter, CanonCamera, NikonCamera
from .info import get_battery_level, get_free_space, get_space


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


def _get_address(camera_name: str) -> str:
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

    addr = _get_address(camera_name)
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


def get_camera_dict(is_simulator: bool = False) -> dict:
    """ Get a dictionary of camera names and their GPhoto2 camera object
    Returns: Dictionary of camera names and their GPhoto2 camera object
    """
    if is_simulator:
        # Return a single VirtualCamera for simulator mode
        vc = VirtualCamera()
        vc.connect()
        return {vc.name: vc}

    cameras = dict()

    # Fuji SDK cameras first — must claim USB before gphoto2 does.
    # If the SDK path is configured we always skip gphoto for Fuji cameras,
    # even if SDK detection returned 0 cameras (gphoto can't claim USB
    # either and would just produce a broken adapter).
    skip_fuji_gphoto = False
    try:
        from .fuji.sdk import detect_fuji_cameras, find_fuji_sdk_path
        sdk_path = find_fuji_sdk_path()
        if sdk_path:
            skip_fuji_gphoto = True  # SDK path configured → never use gphoto for Fuji
            logging.debug('Fuji SDK path: %s', sdk_path)
            fuji_cams = detect_fuji_cameras(sdk_path)
            if fuji_cams:
                cameras.update(fuji_cams)
                logging.info('Fuji SDK detected %d camera(s)', len(fuji_cams))
        else:
            logging.debug('Fuji SDK path not found, skipping SDK detection')
    except Exception:
        logging.debug('Fuji SDK detection failed', exc_info=True)

    camera_names = get_cameras()
    try:
        print("Found cameras:", camera_names, flush=True)
    except Exception:
        logging.debug('Could not print found cameras to terminal')

    for camera_name in camera_names:
        name = camera_name[0]
        if skip_fuji_gphoto and 'fuji' in name.lower():
            logging.debug('Skipping gphoto for %s (using Fuji SDK)', name)
            continue
        cameras[name] = get_camera(name)

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
        except gp.GPhoto2Error:
            logging.error("Could not connect to the camera.  Did you start Solar Eclipse Workbench in sudo mode?")

    return camera_overview
