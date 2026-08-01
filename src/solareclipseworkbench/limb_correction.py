"""Limb-corrected second and third contact times.

Standard eclipse geometry gives the Moon a single mean radius, so C2 and C3 are
computed as if the limb were smooth.  It is not: a valley lets sunlight through
for another second or two, a mountain cuts totality short.  Corrections of a few
seconds are normal and 30 s is possible near the edge of the path.

This module reads the true limb radius out of the marginal-zone blob (see
lunar_limb.py) at the position angle where each contact actually happens, and
feeds a corrected umbral radius back into the Besselian contact solver.

Frames matter here.  The Besselian elements are referred to the true equator and
equinox of date, so position angles are computed in that frame rather than in
ICRF -- the difference is a few tenths of a degree, which is kilometres along
the limb.
"""

import math

import numpy as np
from skyfield import framelib
from skyfield.api import load, wgs84
from skyfield.planetarylib import PlanetaryConstants

from solareclipseworkbench.constants import EARTH_RADIUS
from solareclipseworkbench.lunar_limb import LimbBand

EARTH_RADIUS_KM = EARTH_RADIUS / 1000.0

# The reduced mean limb radius the l2 coefficients are built on.  Jubier charts
# the same value as k2, and it is already the constant used when we generate
# Besselian elements ourselves.
K2 = 0.272281

# The mean radius the LRO and Kaguya profiles are quoted against (IAU, k =
# 0.2725076).  Used only for reporting heights the way Jubier does.
IAU_MEAN_RADIUS_KM = 1738.091

# Radius used to place the tangent point.  An error of a few km here moves the
# sampled point by under a metre, so the mean is plenty.
NOMINAL_RADIUS_KM = 1737.4


class LunarLimb:
    """The Moon's true limb, as seen from one place at one time."""

    def __init__(self, band_path, frame_kernel_path, orientation_kernel_path, ephemeris_path):
        self.band = LimbBand(band_path)
        self.ephemeris = load(str(ephemeris_path))

        constants = PlanetaryConstants()
        with open(frame_kernel_path, "rb") as text_kernel:
            constants.read_text(text_kernel)
        # The binary kernel is read lazily on every rotation_at() call, so the
        # handle has to outlive this constructor.
        self._orientation_kernel = open(orientation_kernel_path, "rb")
        constants.read_binary(self._orientation_kernel)
        self.frame = constants.build_frame_named("MOON_ME_DE421")

    def profile(self, t, latitude, longitude, elevation_m, position_angles):
        """Limb radius in metres at each sky position angle, in degrees.

        Position angles are measured from north through east, in the true
        equator and equinox of date.
        """
        site = self.ephemeris["earth"] + wgs84.latlon(latitude, longitude,
                                                      elevation_m=elevation_m)
        apparent = site.at(t).observe(self.ephemeris["moon"]).apparent()

        of_date = apparent.frame_xyz(framelib.true_equator_and_equinox_of_date).au
        distance_km = apparent.distance().km
        direction = of_date / np.linalg.norm(of_date)

        east = np.cross([0.0, 0.0, 1.0], direction)
        east /= np.linalg.norm(east)
        north = np.cross(direction, east)

        # The limb is where the line of sight grazes the sphere, so the outward
        # normal there is tilted off the plane of the sky by asin(R/D) -- about
        # 0.26 deg, which is 8 km of lunar surface.  Not optional.
        sin_parallax = NOMINAL_RADIUS_KM / distance_km
        cos_parallax = np.sqrt(1.0 - sin_parallax * sin_parallax)

        angles = np.radians(np.asarray(position_angles, dtype=np.float64))
        offsets = (np.cos(angles)[:, None] * north + np.sin(angles)[:, None] * east)
        normals = cos_parallax * offsets - sin_parallax * direction

        to_icrf = framelib.true_equator_and_equinox_of_date.rotation_at(t).T
        to_moon = self.frame.rotation_at(t)
        selenographic = normals @ to_icrf.T @ to_moon.T

        psi = np.degrees(np.arcsin(np.clip(selenographic[:, 0], -1.0, 1.0)))
        phi = np.degrees(np.arctan2(selenographic[:, 2], selenographic[:, 1])) % 360.0

        if np.abs(psi).max() > self.band.psi_limit_deg:
            raise ValueError("limb falls outside the marginal-zone band; blob is too narrow")

        return self.band.radius_m(phi, psi)

    def height_above_k2(self, t, latitude, longitude, elevation_m, position_angles):
        """Limb height in kilometres above the reduced mean radius k2.

        This is the quantity the umbral radius has to be corrected by: positive
        where a mountain makes the umbra larger, negative in a valley.
        """
        radius_km = self.profile(t, latitude, longitude, elevation_m, position_angles) / 1000.0
        return radius_km - K2 * EARTH_RADIUS_KM


def contact_position_angle(elements):
    """Sky position angle of an internal contact, from north through east.

    At an internal contact the limbs touch on the line joining the two centres,
    at the point where the Sun's limb is tangent from inside.  The solver's
    (u, v) runs from the observer to the shadow axis, so the observer sits at
    -(u, v) from the axis, the Moon appears displaced by +(u, v) against the
    Sun, and the tangent point lies in the direction -(u, v).  The Besselian x
    axis points celestial east and y north, which is also how the profile is
    sampled.

    The result puts C2 near the Sun's east limb and C3 near its west limb, which
    is the way round the relative motion requires: the Moon slides east, so the
    last sliver before totality is the eastern one.
    """
    return np.degrees(np.arctan2(-elements["u"], -elements["v"])) % 360.0


def solar_limb_reach(elements, position_angles):
    """How far the Sun's limb reaches from the Moon's centre, per position angle.

    Everything is in fundamental-plane units, where the Moon's mean radius is
    k2 and lengths are Earth radii.  For a total eclipse L2' is negative and
    |L2'| is the umbral radius, which is the Moon's radius less the Sun's, so
    the Sun's radius is k2 + L2'.

    Along the ray leaving the Moon's centre at position angle P, the far
    intersection with the Sun's disk is at

        rho * cos(P - Q) + sqrt(R_sun^2 - rho^2 * sin^2(P - Q))

    with rho the centre separation and Q the position angle of the Sun's centre.
    """
    rho = math.hypot(elements["u"], elements["v"])
    q = contact_position_angle(elements)
    r_sun = K2 + elements["L2p"]

    offset = np.radians(np.asarray(position_angles, dtype=np.float64) - q)
    under_root = r_sun * r_sun - (rho * np.sin(offset)) ** 2
    return rho * np.cos(offset) + np.sqrt(np.clip(under_root, 0.0, None))


def sunlight_margin(elements, position_angles, heights_km):
    """Positive where the Sun still shows past the true limb, per position angle.

    Totality is exactly the state where this is negative everywhere; the arcs
    where it is positive are the Baily's beads.
    """
    true_radius = K2 + np.asarray(heights_km, dtype=np.float64) / EARTH_RADIUS_KM
    return solar_limb_reach(elements, position_angles) - true_radius


def solve_limb_contact(elements, evaluate, start_hours, entering,
                       position_angles, heights_km,
                       search_hours=40.0 / 3600.0, step_hours=0.25 / 3600.0):
    """Find C2 or C3 as the moment the last, or first, bead is on the limb.

    `evaluate` is solar_eclipse.get_elements already bound to a place, and
    `start_hours` the uncorrected contact it should refine.  With `entering`
    true this returns C2, the instant sunlight last vanishes from every position
    angle; with it false, C3, when it first returns.

    Unlike a correction applied at a single position angle, this is set by the
    lowest limb point anywhere along the relevant arc.  That arc is wide: for
    disks of ratio 1.04 the radial gap grows as only about 21 arcsec per radian
    squared, so a 1 km valley 9 degrees away still governs the contact.
    """
    def worst(when):
        return sunlight_margin(evaluate(when), position_angles, heights_km).max()

    # Totality is margin < 0.  Step outwards from the uncorrected contact until
    # the sign flips, then bisect.  Stepping away from totality means going
    # backwards for C2 and forwards for C3.
    direction = -1.0 if entering else 1.0

    inside = start_hours
    for _ in range(int(search_hours / step_hours) + 1):
        if worst(inside) < 0.0:
            break
        inside += direction * -step_hours
    else:
        raise ValueError("no totality found near the uncorrected contact")

    outside = inside
    for _ in range(int(search_hours / step_hours) + 1):
        outside += direction * step_hours
        if worst(outside) >= 0.0:
            break
    else:
        raise ValueError("totality does not end within the search window")

    for _ in range(60):
        middle = 0.5 * (inside + outside)
        if worst(middle) < 0.0:
            inside = middle
        else:
            outside = middle
    return 0.5 * (inside + outside)


def beads(elements, position_angles, heights_km):
    """Position angle ranges where sunlight still shows, as (start, end) pairs.

    The ranges wrap around 360 degrees, so a bead straddling north is reported
    once, with a start angle larger than its end.
    """
    lit = sunlight_margin(elements, position_angles, heights_km) > 0.0
    angles = np.asarray(position_angles, dtype=np.float64)

    if lit.all():
        return [(float(angles[0]), float(angles[-1]))]
    if not lit.any():
        return []

    # Rotate so the array starts on an unlit sample; runs are then contiguous.
    first_dark = int(np.flatnonzero(~lit)[0])
    order = np.roll(np.arange(angles.size), -first_dark)
    rolled = lit[order]

    starts = np.flatnonzero(rolled & ~np.roll(rolled, 1))
    ends = np.flatnonzero(rolled & ~np.roll(rolled, -1))
    return [(float(angles[order[start]]), float(angles[order[end]]))
            for start, end in zip(starts, ends)]
