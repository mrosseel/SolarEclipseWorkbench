"""Validate the limb correction against Jubier's published figures.

Reference case, from the Solar Eclipse Maestro limb profile for the total solar
eclipse of 2015 Mar 20 at Longyearbyen (Svalbard):

    libration    l = +0.88 deg, b = -0.26 deg, c = 335.08 deg
    ratio        Moon/Sun diameter 1.0431
    uncorrected  C2 10:10:43.9  C3 10:13:11.5   duration 2m27.5s
    corrected    C2' +0.4 s     C3' -2.8 s      duration 2m24.4s

Usage:
    python tools/validate_limb_correction.py
"""

import math
import sys
from pathlib import Path

import numpy as np
from skyfield.api import load

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from solareclipseworkbench.limb_correction import (  # noqa: E402
    K2, EARTH_RADIUS_KM, LunarLimb, beads, contact_position_angle, solve_limb_contact)
from solareclipseworkbench.solar_eclipse import get_element_coeffs, get_elements  # noqa: E402

DATA = ROOT / "data"
SRC = ROOT / "src" / "solareclipseworkbench"

SITE_LAT = 78 + 13.328 / 60
SITE_LON = 15 + 39.028 / 60
SITE_ELEVATION = 6.1
ECLIPSE_DATE = "2015-03-20"

JUBIER = {
    "c2": 10 + 10 / 60 + 43.9 / 3600,
    "c3": 10 + 13 / 60 + 11.5 / 3600,
    "c2_correction": +0.4,
    "c3_correction": -2.8,
}


def hms(hours):
    hours %= 24
    h = int(hours)
    m = int((hours - h) * 60)
    s = (hours - h - m / 60) * 3600
    return f"{h:02d}:{m:02d}:{s:04.1f}"


def solve_internal_contact(elements, start, latitude, longitude, height, sign, umbral_radius=None):
    """Newton-iterate an internal contact, optionally with a corrected L2'.

    sign is -1 for third contact and +1 for second contact, matching the
    existing solver in solar_eclipse.get_local_circumstances.
    """
    contact = start
    for _ in range(20):
        o = get_elements(elements, contact, latitude, longitude, height)
        l2 = o["L2p"] if umbral_radius is None else umbral_radius(o, contact)
        s = (o["a"] * o["v"] - o["u"] * o["b"]) / (o["n"] * l2)
        step = (-(o["u"] * o["a"] + o["v"] * o["b"]) / (o["n"] * o["n"])
                + sign * l2 / o["n"] * math.sqrt(max(0.0, 1 - s * s)))
        contact += step
        if abs(step) < 1e-12:
            break
    return contact


def main():
    latitude, longitude, height = SITE_LAT, -SITE_LON, SITE_ELEVATION
    elements = get_element_coeffs(ECLIPSE_DATE)

    # Maximum eclipse, then the uncorrected internal contacts, using the same
    # geometry the application already uses.
    t = 0.0
    for _ in range(30):
        o = get_elements(elements, t, latitude, longitude, height)
        step = -(o["u"] * o["a"] + o["v"] * o["b"]) / (o["n"] * o["n"])
        t += step

    o = get_elements(elements, t, latitude, longitude, height)
    s = (o["a"] * o["v"] - o["u"] * o["b"]) / (o["n"] * o["L2p"])
    tau = o["L2p"] / o["n"] * math.sqrt(max(0.0, 1 - s * s))

    c3 = solve_internal_contact(elements, t - tau, latitude, longitude, height, -1)
    c2 = solve_internal_contact(elements, t + tau, latitude, longitude, height, +1)

    delta_t_hours = elements["Δt"] / 3600.0
    ut_c2 = elements["T0"] + c2 - delta_t_hours
    ut_c3 = elements["T0"] + c3 - delta_t_hours
    ut_max = elements["T0"] + t - delta_t_hours

    print(f"uncorrected  C2 {hms(ut_c2)}  C3 {hms(ut_c3)}  "
          f"duration {(ut_c3 - ut_c2) * 3600:6.1f}s")
    print(f"     Jubier  C2 {hms(JUBIER['c2'])}  C3 {hms(JUBIER['c3'])}  "
          f"duration {(JUBIER['c3'] - JUBIER['c2']) * 3600:6.1f}s")
    print(f"     offset  C2 {(ut_c2 - JUBIER['c2']) * 3600:+.2f}s  "
          f"C3 {(ut_c3 - JUBIER['c3']) * 3600:+.2f}s")

    limb = LunarLimb(DATA / "lunar_limb_band_v1.bin",
                     DATA / "moon_080317.tf",
                     DATA / "moon_pa_de421_1900-2050.bpc",
                     SRC / "de440s.bsp")

    # The profile is evaluated once, at maximum eclipse, the way Jubier does.
    ts = load.timescale()
    year, month, day = (int(part) for part in ECLIPSE_DATE.split("-"))
    moment = ts.ut1(year, month, day, 0, 0, ut_max * 3600.0)

    angles = np.arange(0.0, 360.0, 0.01)
    heights_km = limb.height_above_k2(moment, SITE_LAT, SITE_LON, SITE_ELEVATION, angles)
    print(f"\nlimb heights vs k2 ({K2 * EARTH_RADIUS_KM:.3f} km): "
          f"{heights_km.min():+.3f} .. {heights_km.max():+.3f} km, "
          f"mean {heights_km.mean():+.3f} km")

    o2 = get_elements(elements, c2, latitude, longitude, height)
    o3 = get_elements(elements, c3, latitude, longitude, height)
    print(f"contact position angles: C2 {contact_position_angle(o2):.2f} deg, "
          f"C3 {contact_position_angle(o3):.2f} deg")

    # Single position angle, the way the first cut did it, for comparison.
    def corrected_umbral_radius(o, _when):
        angle = contact_position_angle(o)
        height_km = float(np.interp(angle, angles, heights_km, period=360.0))
        return o["L2p"] - height_km / EARTH_RADIUS_KM

    point_c2 = solve_internal_contact(elements, c2, latitude, longitude, height,
                                      +1, corrected_umbral_radius)
    point_c3 = solve_internal_contact(elements, c3, latitude, longitude, height,
                                      -1, corrected_umbral_radius)
    print(f"\nsingle position angle: C2 {(point_c2 - c2) * 3600:+.2f}s  "
          f"C3 {(point_c3 - c3) * 3600:+.2f}s")

    # The whole arc: the contact is set by the lowest limb point anywhere.
    def evaluate(when):
        return get_elements(elements, when, latitude, longitude, height)

    c2_corrected = solve_limb_contact(elements, evaluate, c2, True, angles, heights_km)
    c3_corrected = solve_limb_contact(elements, evaluate, c3, False, angles, heights_km)

    c2_shift = (c2_corrected - c2) * 3600.0
    c3_shift = (c3_corrected - c3) * 3600.0

    for label, when in (("C2", c2_corrected), ("C3", c3_corrected)):
        for offset in (-1.0, 1.0):
            lit = beads(evaluate(when + offset / 3600.0), angles, heights_km)
            spans = ", ".join(f"{a:.1f}-{b:.1f}" for a, b in lit[:4])
            print(f"beads {offset:+.0f}s around {label}: {len(lit)} at {spans or 'none'} deg")

    print(f"\ncorrection   C2 {c2_shift:+.2f}s  C3 {c3_shift:+.2f}s  "
          f"duration {(c3_shift - c2_shift):+.2f}s")
    print(f"     Jubier  C2 {JUBIER['c2_correction']:+.2f}s  "
          f"C3 {JUBIER['c3_correction']:+.2f}s  duration "
          f"{JUBIER['c3_correction'] - JUBIER['c2_correction']:+.2f}s")

    errors = (abs(c2_shift - JUBIER["c2_correction"]), abs(c3_shift - JUBIER["c3_correction"]))
    print(f"     residual C2 {errors[0]:.2f}s  C3 {errors[1]:.2f}s")
    ok = max(errors) < 0.5
    print("PASS" if ok else "FAIL: correction differs from Jubier by more than 0.5 s")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
