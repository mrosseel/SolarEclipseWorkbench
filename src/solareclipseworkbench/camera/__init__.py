"""Camera hardware control subpackage — ZERO UI dependencies."""

from .types import (
    CameraError,
    CameraSettings,
    CameraInfo,
    BaseCamera,
    _set_gp_config,
)
from .adapters import (
    VirtualCamera,
    GPhotoCameraAdapter,
    CanonCamera,
    NikonCamera,
)
from .capture import (
    take_picture,
    take_burst,
    take_bracket,
    mirror_lock,
)
from .discovery import (
    get_cameras,
    get_camera,
    get_camera_dict,
    get_camera_overview,
)
from .info import (
    get_free_space,
    get_space,
    get_shooting_mode,
    get_focus_mode,
    get_battery_level,
    get_time,
    set_time,
)
