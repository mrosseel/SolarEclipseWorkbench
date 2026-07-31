"""Generate the Solar Eclipse Workbench script for the total solar eclipse of
12 August 2026, as observed from northern Spain with a Canon EOS 800D on an
80/480 refractor.

Contact times and sun altitudes come from ``reference_moments``; exposures come
from ``exposure_calculator`` (Xavier Jubier's tables) evaluated at the sun
altitude of each individual frame, so the sequence follows the atmospheric
extinction of a sunset eclipse instead of using one fixed exposure per phase.

Change the site block below and re-run to retarget the script:

    python scripts/generate_20260812_EOS800D.py
"""

import math
import sys
import types
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "src" / "solareclipseworkbench"

# Import the two calculation modules without executing the package __init__,
# which pulls in the Qt GUI.
_pkg = types.ModuleType("solareclipseworkbench")
_pkg.__path__ = [str(PACKAGE)]
sys.modules["solareclipseworkbench"] = _pkg

from astropy.time import Time
from skyfield.api import load, wgs84

from solareclipseworkbench.reference_moments import calculate_reference_moments
from solareclipseworkbench.exposure_calculator import calculate_exposure, format_shutter_speed

# --test writes a rehearsal version instead: same exposures, same totality, but
# the partial phases replayed on a compressed clock so the whole run fits in a
# coffee break.  Only emission times move; every exposure is still computed from
# the sun altitude at the frame's real moment.
TEST = "--test" in sys.argv
OUTPUT = REPO / "scripts" / ("20260812_EOS800D_test.txt" if TEST
                             else "20260812_EOS800D.txt")

# Compressed spacing, seconds between partial-phase frames in test mode.
TEST_PRE_STEP = 10.0       # C1->C2 partials, replayed before C2
TEST_POST_STEP = 18.0      # C3->sunset partials and brackets, replayed after C3

# --- Site and gear -------------------------------------------------------
SITE = "Palencia, N Spain"
LAT, LON, OBS_ALT = 42.0095, -4.5289, 740.0
ECLIPSE_DATE = "2026-08-12"

CAMERA = "Canon EOS 800D"
MAX_SHUTTER = 1 / 4000.0   # fastest speed the 800D offers
FOCAL_RATIO = 6.0          # 80/480 refractor
APERTURE_FIELD = "6.3"     # nearest standard f-stop; read-only on a telescope
ND = 4.0                   # Baader AstroSolar PHOTOGRAPHIC film (ND 3.8), not the
                           # ND 5.0 visual film.  Partial phases only.
K_EXT = 0.25               # mag / airmass; 0.15 is clear, 0.40 is hazy

T = Time(ECLIPSE_DATE + " 00:00:00")
MOMENTS, MAGNITUDE, TYPE = calculate_reference_moments(LON, LAT, OBS_ALT, T)

_eph = load(str(PACKAGE / "de421.bsp"))
_ts = load.timescale()
_place = _eph["Earth"] + wgs84.latlon(LAT, LON, OBS_ALT)


def sun_altitude(when) -> float:
    t = _ts.from_datetime(when)
    return _place.at(t).observe(_eph["Sun"]).apparent().altaz()[0].degrees


def airmass(alt_deg: float) -> float:
    """Kasten & Young (1989) relative airmass."""
    h = max(alt_deg, 0.0)
    return 1.0 / (math.sin(math.radians(h)) + 0.50572 * (h + 6.07995) ** -1.6364)


def shutter(seconds: float) -> str:
    return format_shutter_speed(max(seconds, MAX_SHUTTER))


def partial_exposure(alt_deg: float, iso: int) -> float:
    """Filtered partial-phase exposure in seconds.

    Above 5 deg the Jubier tables are used directly.  Below that they are
    unusable (they interpolate towards a sea-level 0 deg entry, which produces a
    1/2000 -> 1/8 cliff between 5 and 4 deg), so the 5 deg value is extrapolated
    with a Kasten-Young airmass and K_EXT mag/airmass of extinction.
    """
    if alt_deg >= 5.0:
        return calculate_exposure("partial", alt_deg, OBS_ALT, iso=iso,
                                  aperture=FOCAL_RATIO, nd_filter=ND)
    anchor = calculate_exposure("partial", 5.0, OBS_ALT, iso=iso,
                                aperture=FOCAL_RATIO, nd_filter=ND)
    extra_mag = K_EXT * (airmass(alt_deg) - airmass(5.0))
    return anchor * 10 ** (0.4 * extra_mag)


def totality_exposure(phenomenon: str, alt_deg: float, iso: int) -> float:
    return calculate_exposure(phenomenon, alt_deg, OBS_ALT, iso=iso, aperture=FOCAL_RATIO)


def fmt_delta(seconds: float) -> str:
    seconds = abs(seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - h * 3600 - m * 60
    return "%d:%02d:%04.1f" % (h, m, s)


LINES = []


def emit(text=""):
    LINES.append(text)


def moment_time(ref: str, sign: str, offset: float):
    base = MOMENTS[ref].time_utc
    return base + timedelta(seconds=offset if sign == "+" else -offset)


def picture(ref, sign, offset, exposure, iso, comment):
    emit("take_picture, %s, %s, %s, %s, %s, %s, %d, \"%s\"" %
         (ref, sign, fmt_delta(offset), CAMERA, exposure, APERTURE_FIELD, iso, comment))


def burst(ref, sign, offset, exposure, iso, duration, comment):
    emit("take_burst, %s, %s, %s, %s, %s, %s, %d, %s, \"%s\"" %
         (ref, sign, fmt_delta(offset), CAMERA, exposure, APERTURE_FIELD, iso, duration, comment))


def hdr(ref, sign, offset, exposure, iso, stops, comment):
    emit("take_hdr, %s, %s, %s, %s, %s, %s, %d, %d, \"%s\"" %
         (ref, sign, fmt_delta(offset), CAMERA, exposure, APERTURE_FIELD, iso, stops, comment))


def announce(ref, sign, offset, name, comment):
    emit("voice_prompt, %s, %s, %s, %s, \"%s\"" % (ref, sign, fmt_delta(offset), name, comment))


def sync(ref, sign, offset, comment):
    emit("sync_cameras, %s, %s, %s, \"%s\"" % (ref, sign, fmt_delta(offset), comment))


def filtered_shot(ref, sign, offset, iso, comment, emit_at=None):
    """One filtered partial frame.

    The exposure always comes from the sun altitude at (ref, sign, offset) - the
    frame's real moment.  ``emit_at`` only moves when the line is scheduled, which
    is how the rehearsal script replays the same exposure ladder on a short clock
    without touching any of the eclipse calculations.
    """
    alt = sun_altitude(moment_time(ref, sign, offset))
    e_ref, e_sign, e_offset = emit_at or (ref, sign, offset)
    picture(e_ref, e_sign, e_offset, shutter(partial_exposure(alt, iso)), iso,
            "%s (sun %.1f deg, X=%.1f)" % (comment, alt, airmass(alt)))


# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------
c1, c2, mx, c3, c4 = (MOMENTS[k].time_utc for k in ("C1", "C2", "MAX", "C3", "C4"))
sunset = MOMENTS["sunset"].time_utc
alt_c1 = sun_altitude(c1)
alt_c2 = sun_altitude(c2)
alt_max = sun_altitude(mx)
alt_c3 = sun_altitude(c3)

emit("# Solar Eclipse Workbench script - total solar eclipse of 12 August 2026")
emit("# Generated by scripts/generate_20260812_EOS800D.py - edit that, not this file.")
emit("#")
emit("# Camera : %s (max shutter 1/4000, ISO 100-25600)" % CAMERA)
emit("# Optics : 80/480 mm refractor, fixed f/6 - the aperture column is informational,")
emit("#          the body cannot drive a telescope's aperture and the value is ignored.")
emit("# Filter : ND %.1f photographic solar film for every partial-phase frame." % ND)
emit("#")
emit("# Site   : %s  %.4f N, %.4f E, %d m" % (SITE, LAT, LON, OBS_ALT))
emit("# Type   : %s, magnitude %.4f, totality %.0f s" %
     (TYPE, MAGNITUDE, MOMENTS["duration"].total_seconds()))
emit("#")
emit("#   C1     %s UTC   sun %5.1f deg" % (c1.strftime("%H:%M:%S"), alt_c1))
emit("#   C2     %s UTC   sun %5.1f deg" % (c2.strftime("%H:%M:%S"), alt_c2))
emit("#   MAX    %s UTC   sun %5.1f deg" % (mx.strftime("%H:%M:%S"), alt_max))
emit("#   C3     %s UTC   sun %5.1f deg" % (c3.strftime("%H:%M:%S"), alt_c3))
emit("#   C4     %s UTC   sun %5.1f deg" % (c4.strftime("%H:%M:%S"), sun_altitude(c4)))
emit("#   sunset %s UTC" % sunset.strftime("%H:%M:%S"))
emit("#")
emit("# This is a sunset eclipse.  Totality happens at only %.1f degrees altitude and the sun" % alt_max)
emit("# sets %d s after C4, so the last partial phases are shot straight into the horizon haze." %
     (sunset - c4).total_seconds())
emit("# A clear, low western horizon matters more than anything else at this site.")
emit("#")
emit("# Atmospheric dimming")
emit("# -------------------")
emit("# Exposures are not constant through the eclipse: the sun drops from %.1f deg at C1" % alt_c1)
emit("# to the horizon, and airmass rises from %.1f to over 30.  Every filtered frame below" % airmass(alt_c1))
emit("# was computed for the sun altitude at that exact instant:")
emit("#   - above 5 deg : Jubier exposure tables, interpolated in sun altitude and site height")
emit("#   - below 5 deg : the tables break down, so the 5 deg value is extrapolated with a")
emit("#                   Kasten-Young airmass and %.2f mag/airmass extinction." % K_EXT)
emit("# That extinction coefficient is a guess (0.15 clear .. 0.40 hazy), and the error grows")
emit("# to several stops at the horizon - hence the wide HDR brackets after C3+18m.")
emit("#")
emit("# Warning: at f/6 with photographic film the sun wants 1/7139 just after C1 at ISO 100,")
emit("# which the 800D cannot reach - it tops out at 1/4000, so the early frames run up to")
emit("# 0.85 stop over, easing to nothing once the sun is below 10 degrees.  ISO 100 is both")
emit("# the body's base and its widest dynamic range, and a faster ISO would only make the")
emit("# ceiling worse, so the fix if you want one is a 1-stop ND on the scope.  Shoot RAW.")
emit("#")
emit("# Comments carry the sun altitude and airmass X used for each exposure.")
if TEST:
    emit("#")
    emit("# REHEARSAL VERSION.  Totality runs at full speed, exactly as on the day, because")
    emit("# that is the part whose throughput has to be proven.  The partial phases keep")
    emit("# their real exposures - each frame is still computed from the sun altitude at its")
    emit("# real moment - but are replayed on a compressed clock: %.0f s apart before C2 and" % TEST_PRE_STEP)
    emit("# %.0f s apart after C3, all anchored to C2 and C3 so one simulated contact drives" % TEST_POST_STEP)
    emit("# the whole run.  No eclipse calculation differs from the production script.")
    emit("# Simulate C2 a few minutes out and the run takes about %d minutes." % 14)
emit()

# --------------------------------------------------------------------------
# Set-up, before first contact
# --------------------------------------------------------------------------
# Every filtered partial frame, as (ref, sign, offset, label) on the real clock.
# In test mode the same list is emitted against a compressed clock instead.
c1_to_c2 = (c2 - c1).total_seconds()
pre_c2 = [("C1", "-", 15 * 60, "Focus check, uneclipsed disc"),
          ("C1", "-", 10 * 60, "Focus check, uneclipsed disc"),
          ("C1", "-", 5 * 60, "Framing check, uneclipsed disc"),
          ("C1", "-", 15, "Just before first contact"),
          ("C1", "-", 5, "Just before first contact")]
pre_c2 += [("C1", "+", off, "First contact") for off in (5, 15, 30, 60, 120)]
t = 240.0
while t < c1_to_c2 - 12 * 60:
    pre_c2.append(("C1", "+", t, "Partial C1-C2"))
    t += 180.0
pre_c2 += [("C2", "-", b, "Partial approaching C2")
           for b in (720, 630, 540, 450, 360, 270, 180, 120, 75, 45)]

if TEST:
    emit("# --- Partial phases, replayed %.0f s apart (real exposures, short clock) ---" % TEST_PRE_STEP)
    sync("C2", "-", TEST_PRE_STEP * len(pre_c2) + 55, "Sync camera clock and settings")
    hdr("C2", "-", TEST_PRE_STEP * len(pre_c2) + 40,
        shutter(partial_exposure(sun_altitude(moment_time("C1", "-", 180)), 100) / 4),
        100, 4, "Exposure-check bracket, four stops down from the fastest speed the body has")
    for i, (ref, sign, offset, label) in enumerate(pre_c2):
        at = TEST_PRE_STEP * (len(pre_c2) - i) + 30
        filtered_shot(ref, sign, offset, 100, label, emit_at=("C2", "-", at))
    emit()
else:
    emit("# --- Set-up and focus, before C1 (solar filter ON) ---")
    sync("C1", "-", 20 * 60, "Sync camera clock and settings")
    for ref, sign, offset, label in pre_c2[:3]:
        filtered_shot(ref, sign, offset, 100, label)
    hdr("C1", "-", 3 * 60,
        shutter(partial_exposure(sun_altitude(moment_time("C1", "-", 180)), 100) / 4),
        100, 4, "Exposure-check bracket, four stops down from the fastest speed the body has")
    sync("C1", "-", 90, "Re-sync before the eclipse starts")
    emit()

    emit("# --- First contact ---")
    announce("C1", "-", 60, "C1_IN_60_SECONDS", "One minute to first contact")
    announce("C1", "-", 30, "C1_IN_30_SECONDS", "Thirty seconds to first contact")
    filtered_shot(*pre_c2[3][:3], 100, pre_c2[3][3])
    announce("C1", "-", 10, "C1_IN_10_SECONDS", "Ten seconds to first contact")
    filtered_shot(*pre_c2[4][:3], 100, pre_c2[4][3])
    announce("C1", "-", 5, "C1_IN_5_SECONDS", "Five seconds to first contact")
    announce("C1", "-", 0, "C1", "First contact - the eclipse has started")
    for ref, sign, offset, label in pre_c2[5:10]:
        filtered_shot(ref, sign, offset, 100, label)
    emit()

    emit("# --- Partial phases C1 -> C2 (filter ON, exposure tracks the sinking sun) ---")
    for ref, sign, offset, label in pre_c2[10:]:
        filtered_shot(ref, sign, offset, 100, label)
    emit()

emit("# --- Countdown to totality ---")
for offset, name in [(50 * 60, "C2_IN_50_MINUTES"), (40 * 60, "C2_IN_40_MINUTES"),
                     (30 * 60, "C2_IN_30_MINUTES"), (25 * 60, "C2_IN_25_MINUTES"),
                     (20 * 60, "C2_IN_20_MINUTES"), (15 * 60, "C2_IN_15_MINUTES"),
                     (10 * 60, "C2_IN_10_MINUTES"), (6 * 60, "C2_IN_6_MINUTES"),
                     (5 * 60, "C2_IN_5_MINUTES"), (4 * 60, "C2_IN_4_MINUTES"),
                     (2 * 60, "C2_IN_2_MINUTES")]:
    # The long countdown would stretch a rehearsal to an hour on its own, and the
    # compressed partials already start well inside it.
    if TEST and offset > 2 * 60:
        continue
    announce("C2", "-", offset, name, "%d minutes to totality" % (offset // 60))
announce("C2", "-", 90, "C2_IN_90_SECONDS", "Ninety seconds to totality")
announce("C2", "-", 60, "C2_IN_60_SECONDS", "One minute to totality - get ready")
announce("C2", "-", 40, "C2_IN_40_SECONDS", "Forty seconds to totality")
announce("C2", "-", 30, "C2_IN_30_SECONDS", "FILTERS OFF")
sync("C2", "-", 25, "Last sync before totality")
announce("C2", "-", 20, "C2_IN_20_SECONDS", "Twenty seconds - filters off, eclipse glasses off at C2")
announce("C2", "-", 10, "C2_IN_10_SECONDS", "Ten seconds to totality")
announce("C2", "-", 5, "C2_IN_5_SECONDS", "Five seconds to totality")
emit()

# --------------------------------------------------------------------------
# Totality
# --------------------------------------------------------------------------
totality = (c3 - c2).total_seconds()
beads_iso, corona_iso = 100, 400
beads = shutter(totality_exposure("bailys_beads", alt_c2, beads_iso))
chromo = shutter(totality_exposure("chromosphere", alt_c2, beads_iso))
prom = shutter(totality_exposure("prominences", alt_c2, beads_iso))
inner = shutter(totality_exposure("corona_inner_0.5R", alt_max, corona_iso))
deep = shutter(totality_exposure("corona_outer_8R", alt_max, 800))
deeper = shutter(totality_exposure("corona_outer_8R", alt_max, 800) * 2)
cor_low = totality_exposure("corona_lower", alt_max, corona_iso)
cor_out = totality_exposure("corona_outer_8R", alt_max, corona_iso)
ladder_start = shutter(cor_low / 2)
# 8 stops, not the ~11 the tables span: the 800D's 21-frame RAW buffer is the
# binding constraint during totality, and the outer corona is covered by the
# dedicated ISO 800 frames instead of by extending every ladder.
ladder_stops = min(8, int(round(math.log2(cor_out / (cor_low / 2)))))

C2_BURST_S, C3_BURST_S = 3, 5
BURST_FPS = 6.0            # 800D rated continuous rate
TOTALITY_SINGLES = 11
totality_frames = (int(BURST_FPS * (C2_BURST_S + C3_BURST_S))
                   + 3 * (2 * ladder_stops + 1) + TOTALITY_SINGLES)

emit("# --- TOTALITY (%.0f s, sun %.1f deg - FILTER OFF) ---" % (totality, alt_max))
emit("# Ladder: %s, %d stops down and back = %d frames at ISO %d." %
     (ladder_start, ladder_stops, 2 * ladder_stops + 1, corona_iso))
emit("# Three identical ladders so the corona can be stacked, with the window around")
emit("# maximum filled by deep ISO 800 outer-corona frames.  The 1 s and 1.6 s frames")
emit("# need the mount tracking at 480 mm.")
emit("#")
emit("# Throughput: the 800D holds ~21 RAW frames of buffer and sustains roughly 1.5")
emit("# frames/s to a fast UHS-I card.  Every camera command here takes a per-camera USB")
emit("# lock and is DROPPED if it cannot get it within 1.5 s (camera.py _MAX_LOCK_WAIT_S),")
emit("# so an overrunning ladder deletes whatever is scheduled next rather than running")
emit("# late.  The bursts and ladders below are sized to stay under that: %d frames in" % totality_frames)
emit("# totality, %.1f frames/s and ~%.0f MB/s to the card." %
     (totality_frames / totality, totality_frames * 27.0 / totality))
announce("C2", "-", 0, "C2", "Second contact - filters off, totality has begun")
burst("C2", "-", 3, beads, beads_iso, C2_BURST_S, "Diamond ring and Baily's beads at C2")
picture("C2", "+", 4.0, chromo, beads_iso, "Chromosphere")
picture("C2", "+", 5.5, prom, beads_iso, "Prominences")
hdr("C2", "+", 7.0, ladder_start, corona_iso, ladder_stops, "Corona ladder A")
hdr("C2", "+", 26.0, ladder_start, corona_iso, ladder_stops, "Corona ladder B")
announce("C2", "+", 30, "C2_PLUS_30_SECONDS", "Thirty seconds into totality")
picture("C2", "+", 45.0, deep, 800, "Deep outer corona")
picture("C2", "+", 47.5, deeper, 800, "Deepest outer corona / earthshine")
announce("MAX", "-", 10, "MAX_IN_10_SECONDS", "Ten seconds to maximum eclipse")
picture("C2", "+", 50.5, inner, corona_iso, "Inner corona at maximum eclipse")
announce("MAX", "-", 0, "MAX", "Maximum eclipse")
picture("C2", "+", 53.0, deeper, 800, "Deepest outer corona / earthshine")
picture("C2", "+", 55.5, deep, 800, "Deep outer corona")
announce("C3", "-", 45, "C3_IN_45_SECONDS", "Forty-five seconds of totality left")
hdr("C2", "+", 58.5, ladder_start, corona_iso, ladder_stops, "Corona ladder C")
picture("C3", "-", 23.0, deep, 800, "Deep outer corona")
picture("C3", "-", 20.5, inner, corona_iso, "Inner corona")
announce("C3", "-", 20, "C3_IN_20_SECONDS", "Twenty seconds to third contact")
picture("C3", "-", 16.0, prom, beads_iso, "Prominences before C3")
picture("C3", "-", 14.0, chromo, beads_iso, "Chromosphere before C3")
announce("C3", "-", 8, "C3_IN_8_SECONDS", "Eight seconds - look away from the eyepiece")
burst("C3", "-", 3, beads, beads_iso, C3_BURST_S, "Baily's beads and diamond ring at C3")
announce("C3", "+", 2, "C3_PLUS_2_SECONDS", "Third contact - totality is over")
announce("C3", "+", 10, "FILTERS_ON", "FILTERS ON")
emit()

# --------------------------------------------------------------------------
# Partial phases C3 -> sunset
# --------------------------------------------------------------------------
# Post-C3 frames on the real clock: singles while the sun is above 5 degrees, then
# wide brackets once the exposure tables stop being trustworthy.
post_c3 = []
for after in (45, 75, 120, 180, 270, 360, 450, 540, 660, 780, 900, 1020):
    if sun_altitude(moment_time("C3", "+", after)) < 5.0:
        break
    post_c3.append(("single", after, None))
after = 1080.0
while True:
    alt = sun_altitude(moment_time("C3", "+", after))
    if alt < 0.2:
        break
    post_c3.append(("bracket", after, alt))
    after += 120.0


def low_sun_bracket(after, alt, emit_after=None):
    iso = 100 if alt > 2.0 else 400
    centre = partial_exposure(alt, iso)
    stops = 4 if alt > 2.0 else 6
    hdr("C3", "+", after if emit_after is None else emit_after,
        shutter(centre / (2 ** (stops / 2))), iso, stops,
        "Low-sun bracket, sun %.1f deg, X=%.0f, centre %s" % (alt, airmass(alt), shutter(centre)))


if TEST:
    emit("# --- Partial phases after C3, replayed %.0f s apart ---" % TEST_POST_STEP)
    for i, (kind, after, alt) in enumerate(post_c3):
        at = 15.0 + TEST_POST_STEP * i
        if kind == "single":
            filtered_shot("C3", "+", after, 100, "Partial after C3", emit_at=("C3", "+", at))
        else:
            low_sun_bracket(after, alt, emit_after=at)
    emit()
    emit("# --- End of rehearsal ---")
    announce("C3", "+", 15.0 + TEST_POST_STEP * len(post_c3) + 20, "C4",
             "End of the rehearsal - on the day this is fourth contact")
else:
    emit("# --- Partial phases C3 -> sunset (filter ON, sun dropping to the horizon) ---")
    for kind, after, alt in post_c3:
        if kind == "single":
            filtered_shot("C3", "+", after, 100, "Partial after C3")
    emit()

    emit("# --- Below 5 degrees: the tables are extrapolated, so bracket widely ---")
    for kind, after, alt in post_c3:
        if kind == "bracket":
            low_sun_bracket(after, alt)
    emit()

    emit("# --- Last light ---")
    announce("C4", "-", 60, "C4_IN_60_SECONDS", "One minute to fourth contact (sun is already setting)")
    announce("C4", "-", 20, "C4_IN_20_SECONDS", "Twenty seconds to fourth contact")
    announce("C4", "-", 0, "C4", "Fourth contact - eclipse over")

OUTPUT.write_text("\n".join(LINES) + "\n")
print("written %s (%d lines)" % (OUTPUT, len(LINES)))
