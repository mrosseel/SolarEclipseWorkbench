"""Eclipse science/math subpackage — ZERO UI or camera dependencies."""
from .solar_eclipse import (
    get_element_coeffs,
    get_elements,
    get_local_circumstances,
    get_solar_eclipses,
    compute_central_lat_lon_for_time,
    compute_extremes,
    compute_estimate,
    refine_estimate,
    get_extreme_points,
    compute_rise_set_point,
    compute_rise_set_points,
    get_rise_set_curves,
    get_limits_by_longitude_as_list,
    get_limits_for_longitude,
    compute_outline_point,
    proper_angle,
    get_outline_curve_q_range,
    solve_quadrant,
)
from .besselian_element_generator import BesselianElementGenerator
from .nutation import Nutation
from .vec import Vec, PolynomialRegression
from .reference_moments import (
    ReferenceMomentInfo,
    calculate_reference_moments,
    calculate_alt_az,
    ut_to_hms,
)
from .exposure_calculator import (
    calculate_exposure,
    format_shutter_speed,
    parse_shutter_speed,
    round_to_camera_shutter_speed,
    get_exposure_bracket,
    calculate_eclipse_exposures,
    calculate_sun_altitude_at_time,
    EXPOSURE_TABLES,
)
