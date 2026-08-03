"""Generate the production Solar Eclipse Workbench script for 12 August 2026.

Two bodies, both on an 80/480 refractor behind photographic solar film, driven from one
script: a Fujifilm X-T4 (native Fuji SDK, plus a relay trigger on its remote jack)
and a Canon EOS 800D (gphoto2).

Contact times and sun altitudes come from ``reference_moments``; exposures come
from ``exposure_calculator`` (Xavier Jubier's tables) evaluated at the sun altitude
of each individual frame, so the sequence follows the atmospheric extinction of a
sunset eclipse rather than using one fixed exposure per phase.

The two rigs are identical, so they use identical exposures and the same commands
wherever the commands do the same thing.  They differ in exactly three places, each
forced by the hardware or the code:

  * corona ladders - ``take_hdr`` ramps the shutter between frames on gphoto2, but
    on the Fuji SDK path it falls through to the virtual-camera branch and fires
    2*stops+1 frames at ONE unchanged speed.  The X-T4 therefore uses
    ``take_bracket``, which is properly wired to the SDK via ``bracket_no_download``.
  * contact bursts - only the X-T4 has the relay on its remote jack, so it gets
    ``relay_burst`` at ~15 fps.  The 800D uses ``take_burst`` (held shutter, ~6 fps).
  * shutter ceiling - 1/8000 on the X-T4, 1/4000 on the 800D.  Only the Baily's
    beads frames are fast enough for this to bite; the 800D's are capped.

Partial-phase frames alternate between the bodies so the pair samples the disc
twice as often as either alone.

The 800D is parked, so the two halves are written to separate files: the X-T4 gets
``scripts/real/20260812_production.txt`` - the only thing in that directory,
because it is the only file that runs on the day - and the 800D's commands go to
``scripts/test/20260812_production_EOS800D.txt``.  Solar Eclipse Workbench loads
one script at a time, so leaving the 800D lines in the production script would only
have produced a wall of "camera not found" at load.

    python scripts/generate_20260812_production.py
"""

import math
import sys
import types
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "src" / "solareclipseworkbench"

# Import the calculation modules without executing the package __init__, which
# pulls in the Qt GUI.
_pkg = types.ModuleType("solareclipseworkbench")
_pkg.__path__ = [str(PACKAGE)]
sys.modules["solareclipseworkbench"] = _pkg

from astropy.time import Time
from skyfield.api import load, wgs84

from solareclipseworkbench.reference_moments import calculate_reference_moments
from solareclipseworkbench.exposure_calculator import calculate_exposure, format_shutter_speed

OUTPUT = REPO / "scripts" / "real" / "20260812_production.txt"
PARKED = REPO / "scripts" / "test" / "20260812_production_EOS800D.txt"

# --- Site -----------------------------------------------------------------
SITE = "Palencia, N Spain"
LAT, LON, OBS_ALT = 42.0095, -4.5289, 740.0
ECLIPSE_DATE = "2026-08-12"

# --- Gear, identical on both bodies ---------------------------------------
FOCAL_RATIO = 6.0          # 80/480 refractor, used for the exposure arithmetic
# A telescope has no electronic aperture, so the column says "-" and the setting
# is skipped.  An f-number here makes every frame try to set an aperture the body
# cannot change, and answer 0x1006 "camera is busy".
APERTURE_FIELD = "-"
ND = 4.0                   # Baader AstroSolar PHOTOGRAPHIC film (ND 3.8), not the
                           # ND 5.0 visual film.  Partial phases only.
K_EXT = 0.25               # mag / airmass; 0.15 is clear, 0.40 is hazy

XT4 = "Fuji Fujifilm X-T4"
EOS = "Canon EOS 800D"
XT4_MAX_SHUTTER = 1 / 8000.0
EOS_MAX_SHUTTER = 1 / 4000.0

# Measured/rated throughput used for the frame budget in the header.
XT4_SDK_FPS = 1.8          # USB PTP round-trip ceiling, measured on this body
XT4_RELAY_FPS = 15.0       # CH drive, shutter held closed by the relay
EOS_BURST_FPS = 6.0

# ISO 100 for the filtered partials: with photographic film at f/6 the sun wants
# 1/7139 just after C1, which fits the X-T4's 1/8000 but not the 800D's 1/4000.
# Anything faster than ISO 100 puts both bodies over their ceiling.
ISO_PARTIAL, ISO_BEADS, ISO_CORONA, ISO_DEEP = 100, 100, 400, 800

T = Time(ECLIPSE_DATE + " 00:00:00")
MOMENTS, MAGNITUDE, TYPE = calculate_reference_moments(LON, LAT, OBS_ALT, T)

_eph = load(str(PACKAGE / "de421.bsp"))
_ts = load.timescale()
_place = _eph["Earth"] + wgs84.latlon(LAT, LON, OBS_ALT)


def sun_altitude(when) -> float:
    return _place.at(_ts.from_datetime(when)).observe(_eph["Sun"]).apparent().altaz()[0].degrees


def airmass(alt_deg: float) -> float:
    """Kasten & Young (1989) relative airmass."""
    h = max(alt_deg, 0.0)
    return 1.0 / (math.sin(math.radians(h)) + 0.50572 * (h + 6.07995) ** -1.6364)


def shutter(seconds: float, max_shutter: float = XT4_MAX_SHUTTER) -> str:
    return format_shutter_speed(max(seconds, max_shutter))


def partial_exposure(alt_deg: float, iso: int) -> float:
    """Filtered partial-phase exposure in seconds.

    Above 5 deg the Jubier tables are used directly.  Below that they are unusable
    (they interpolate towards a sea-level 0 deg entry, producing a 1/2000 -> 1/8
    cliff between 5 and 4 deg), so the 5 deg value is extrapolated with a
    Kasten-Young airmass and K_EXT mag/airmass of extinction.
    """
    if alt_deg >= 5.0:
        return calculate_exposure("partial", alt_deg, OBS_ALT, iso=iso,
                                  aperture=FOCAL_RATIO, nd_filter=ND)
    anchor = calculate_exposure("partial", 5.0, OBS_ALT, iso=iso,
                                aperture=FOCAL_RATIO, nd_filter=ND)
    return anchor * 10 ** (0.4 * K_EXT * (airmass(alt_deg) - airmass(5.0)))


def totality_exposure(phenomenon: str, alt_deg: float, iso: int) -> float:
    return calculate_exposure(phenomenon, alt_deg, OBS_ALT, iso=iso, aperture=FOCAL_RATIO)


def fmt_delta(seconds: float) -> str:
    seconds = abs(seconds)
    return "%d:%02d:%04.1f" % (int(seconds // 3600), int((seconds % 3600) // 60),
                               seconds - int(seconds // 3600) * 3600 - int((seconds % 3600) // 60) * 60)


# Every line is tagged with the body it drives, so the schedule can be written out
# per camera.  The 800D is parked at the moment, and Solar Eclipse Workbench loads
# one script at a time, so its half goes to a file of its own instead of sitting in
# the production script referring to a camera that will not be detected.
LINES = []
FRAMES = {XT4: 0, EOS: 0}


def emit(text="", owner=None):
    """Add a line.  ``owner`` is the camera it drives, or None for shared lines."""
    LINES.append((owner, text))


def moment_time(ref, sign, offset):
    return MOMENTS[ref].time_utc + (timedelta(seconds=offset) if sign == "+"
                                    else -timedelta(seconds=offset))


def note(ref, sign, offset, what, extra=""):
    """Uniform comment: what it is, when it fires, and where the sun was.

    The absolute time makes post-hoc matching against EXIF straightforward.  It is
    only correct for the site in the header - the commands themselves are scheduled
    relative to the reference moments, which are recomputed from the observer's
    actual position at run time.
    """
    when = moment_time(ref, sign, offset)
    return "%s @ %s, sun %.1f deg%s" % (what, when.strftime("%H:%M:%S"),
                                        sun_altitude(when), extra)


def picture(cam, ref, sign, offset, exposure, iso, what, extra=""):
    FRAMES[cam] += 1
    emit("take_picture, %s, %s, %s, %s, %s, %s, %d, \"%s\"" %
         (ref, sign, fmt_delta(offset), cam, exposure, APERTURE_FIELD, iso,
          note(ref, sign, offset, what, extra)), cam)


def burst(cam, ref, sign, offset, exposure, iso, arg, frames, what):
    FRAMES[cam] += frames
    emit("take_burst, %s, %s, %s, %s, %s, %s, %d, %s, \"%s\"" %
         (ref, sign, fmt_delta(offset), cam, exposure, APERTURE_FIELD, iso, arg,
          note(ref, sign, offset, what)), cam)


def hdr(cam, ref, sign, offset, exposure, iso, stops, what, extra=""):
    FRAMES[cam] += 2 * stops + 1
    emit("take_hdr, %s, %s, %s, %s, %s, %s, %d, %d, \"%s\"" %
         (ref, sign, fmt_delta(offset), cam, exposure, APERTURE_FIELD, iso, stops,
          note(ref, sign, offset, what, extra)), cam)


def bracket(cam, ref, sign, offset, exposure, iso, steps, frames, what, extra=""):
    FRAMES[cam] += frames
    emit("take_bracket, %s, %s, %s, %s, %s, %s, %d, %s, \"%s\"" %
         (ref, sign, fmt_delta(offset), cam, exposure, APERTURE_FIELD, iso, steps,
          note(ref, sign, offset, what, extra)), cam)


def relay_burst(ref, sign, offset, seconds, frames, what):
    FRAMES[XT4] += frames
    emit("relay_burst, %s, %s, %s, %s, \"%s\"" %
         (ref, sign, fmt_delta(offset), seconds, note(ref, sign, offset, what)), XT4)


def relay_shoot(ref, sign, offset, what):
    FRAMES[XT4] += 1
    emit("relay_shoot, %s, %s, %s, \"%s\"" %
         (ref, sign, fmt_delta(offset), note(ref, sign, offset, what)), XT4)


def relay_arm(ref, sign, offset, what):
    """Close S1 and leave it closed, so the burst that follows takes the fast path."""
    emit("relay_arm, %s, %s, %s, \"%s\"" %
         (ref, sign, fmt_delta(offset), note(ref, sign, offset, what)), XT4)


def relay_release(ref, sign, offset, what):
    emit("relay_release, %s, %s, %s, \"%s\"" %
         (ref, sign, fmt_delta(offset), note(ref, sign, offset, what)), XT4)


def announce(ref, sign, offset, name, comment):
    emit("voice_prompt, %s, %s, %s, %s, \"%s\"" % (ref, sign, fmt_delta(offset), name, comment))


def sync(ref, sign, offset, comment):
    emit("sync_cameras, %s, %s, %s, \"%s\"" % (ref, sign, fmt_delta(offset), comment))


def filtered_shot(cam, ref, sign, offset, what):
    alt = sun_altitude(moment_time(ref, sign, offset))
    cap = XT4_MAX_SHUTTER if cam == XT4 else EOS_MAX_SHUTTER
    picture(cam, ref, sign, offset, shutter(partial_exposure(alt, ISO_PARTIAL), cap),
            ISO_PARTIAL, what, extra=", X=%.1f" % airmass(alt))


# --------------------------------------------------------------------------
# Exposures
# --------------------------------------------------------------------------
c1, c2, mx, c3, c4 = (MOMENTS[k].time_utc for k in ("C1", "C2", "MAX", "C3", "C4"))
sunset = MOMENTS["sunset"].time_utc
alt_c1, alt_c2, alt_max, alt_c3 = (sun_altitude(t) for t in (c1, c2, mx, c3))
totality = (c3 - c2).total_seconds()

beads_x = shutter(totality_exposure("bailys_beads", alt_c2, ISO_BEADS), XT4_MAX_SHUTTER)
beads_e = shutter(totality_exposure("bailys_beads", alt_c2, ISO_BEADS), EOS_MAX_SHUTTER)
chromo = shutter(totality_exposure("chromosphere", alt_c2, ISO_BEADS), EOS_MAX_SHUTTER)
prom = shutter(totality_exposure("prominences", alt_c2, ISO_BEADS))
inner = shutter(totality_exposure("corona_inner_0.5R", alt_max, ISO_CORONA))
deep = shutter(totality_exposure("corona_outer_8R", alt_max, ISO_DEEP))
deeper = shutter(totality_exposure("corona_outer_8R", alt_max, ISO_DEEP) * 2)

cor_low = totality_exposure("corona_lower", alt_max, ISO_CORONA)
cor_out = totality_exposure("corona_outer_8R", alt_max, ISO_CORONA)
hdr_start = shutter(cor_low / 2, EOS_MAX_SHUTTER)
hdr_stops = min(8, int(round(math.log2(cor_out / (cor_low / 2)))))

# The corona spans some twelve stops from the inner edge to the outer streamers.
# "+/- 2" is thirteen frames a third of a stop apart covering four of them, so it
# took three of them - three separate commands, 18s of shutter each - to cover
# what one ladder can, and on 3 August the middle one of each group was dropped
# because the one before it still had the camera.  take_bracket also accepts the
# speeds written out, which is how the whole range fits in one command of seven
# frames and about ten seconds.
def corona_ladder(start_s: float, stops: int, step_ev: int = 2) -> tuple:
    """A semicolon ladder from `start_s`, in `step_ev` jumps, and its frame count."""
    rungs = [shutter(start_s * (2 ** ev)) for ev in range(0, stops + 1, step_ev)]
    # Written out rather than centred, so nothing depends on what the body holds.
    return ";".join(rungs), len(rungs)


CORONA_LADDER, CORONA_FRAMES = corona_ladder(cor_low / 2, hdr_stops)

# The hold is bounded by the transfer queue, not by the card: every frame taken
# with the SDK session open holds one of 32 slots until the drain that follows
# can run, and a full queue stops the body dead.  At 15 fps that is a little over
# two seconds, so the burst is held to MAX_BURST_S (fuji_camera) with a margin.
RELAY_C2_S, RELAY_C3_S = 1.9, 1.9

# Trigger latency, measured on the bench 1 August 2026, and it depends entirely on
# the path: S1 pre-armed and held gives 43-48 ms, S2 alone with S1 never asserted
# ~130 ms, and a bare shoot() 170 ms - of which 120 ms is our own settle before S2
# closes.  So the bursts below pre-arm with relay_arm and take the fast path, which
# both shrinks the lead and stops it depending on how the release cable is wired.
RELAY_LATENCY_S = 0.045
RELAY_C2_N = int(RELAY_C2_S * XT4_RELAY_FPS)
RELAY_C3_N = int(RELAY_C3_S * XT4_RELAY_FPS)

# The burst is shorter than the window it has to cover, so where it sits inside
# that window decides what is on the card.  Measured for this site: the beads run
# 3.25 s at C2 and 4.05 s at C3, against a hold of 1.9 s.
#
# The diamond ring is the last bead before totality at C2 and the first one after
# it at C3, so at both contacts it sits against the contact rather than in the
# middle of the window.  The burst is therefore pinned to the contact-side edge:
# it ends at BEADS_C2_END and starts at BEADS_C3_START.
#
# The margin pushes it a little further towards totality than the solved edge.
# The error is asymmetric: overshooting costs a handful of black frames at beads
# exposure, undershooting loses the diamond ring, and there is no second one.
RELAY_EDGE_MARGIN_S = 0.3
EOS_C2_BURST_S, EOS_C3_BURST_S = 3, 5

# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------
emit("# Solar Eclipse Workbench PRODUCTION script - total solar eclipse of 12 August 2026")
emit("# Generated by scripts/generate_20260812_production.py - edit that, not this file.")
emit("#")
emit("# Body   : %s   (Fuji SDK + relay trigger on the remote jack)" % XT4)
emit("#          The %s is parked; its half of this schedule is in" % EOS)
emit("#          scripts/test/20260812_production_EOS800D.txt, still interleaved with the")
emit("#          frames below in case the pair runs again on a second machine.")
emit("# Optics : 80/480 mm refractor, fixed f/6.  The body cannot drive a telescope's")
emit("#          aperture, so the aperture column says '-' and the setting is skipped.")
emit("#          The f/6 still sets every exposure below; it just is not sent to the body.")
emit("# Filter : Baader AstroSolar PHOTOGRAPHIC film (ND %.1f) on the scope, for every" % ND)
emit("#          partial-phase frame.  NOT the ND 5.0 visual film - never look through this.")
emit("#")
emit("# At f/6 with photographic film the sun wants 1/7139 just after C1 at ISO 100, which")
emit("# the X-T4 reaches at its 1/8000 ceiling.")
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
emit("# Sunset eclipse: totality is only %.1f deg up and the sun sets %d s after C4, so the" %
     (alt_max, (sunset - c4).total_seconds()))
emit("# last partials are shot into horizon haze.  A clear, low western horizon matters more")
emit("# here than anything else.")
emit("#")
emit("# Which command does what on this body")
emit("# ------------------------------------")
emit("#   corona ladders  take_hdr ramps the shutter between frames on gphoto2, but on the")
emit("#                   Fuji SDK path it fires 2*stops+1 frames at ONE speed.  The X-T4")
emit("#                   uses take_bracket (wired to the SDK bracket_no_download) instead.")
emit("#   contact bursts  the relay on the remote jack free-runs CH at ~%.0f fps, which the" % XT4_RELAY_FPS)
emit("#                   SDK cannot do at all: it returns 0x1008 with the dial on CH.")
emit("#")
emit("# Atmospheric dimming")
emit("# -------------------")
emit("# The sun falls from %.1f deg at C1 to the horizon and airmass climbs from %.1f to over" %
     (alt_c1, airmass(alt_c1)))
emit("# 30, so every filtered frame was computed for the sun altitude at that exact instant:")
emit("#   - above 5 deg : Jubier tables, interpolated in sun altitude and site height")
emit("#   - below 5 deg : the tables break down, so the 5 deg value is extrapolated with a")
emit("#                   Kasten-Young airmass and %.2f mag/airmass extinction." % K_EXT)
emit("# That coefficient is a guess (0.15 clear .. 0.40 hazy) and the error grows to several")
emit("# stops at the horizon, hence the wide brackets after C3+18m.  Comments carry the sun")
emit("# altitude and airmass X used for each frame.")
emit("#")
emit("# Partial-phase frames alternate between the bodies, so the pair samples the disc")
emit("# twice as often as either alone.")
emit("#")
emit("#")
emit("# PRE-FLIGHT - every item by hand, before C1, then do not touch the body again")
emit("# =========================================================================")
emit("# The SDK cannot read most of these back, so nothing below is verified at run")
emit("# time.  An unticked box is a silent failure: the frames look normal.")
emit("#")
emit("#   BODY, set and then left alone")
emit("#   [ ] drive dial on CH          a burst free-runs at ~15 fps; on Single each")
emit("#                                 contact is one frame and the beads are lost")
emit("#   [ ] shutter speed dial on T   or the script cannot set the speed at all")
emit("#   [ ] ISO dial on C             or every ISO the script asks for is ignored")
emit("#   [ ] mode dial on M            metering off a black sky ruins every corona frame")
emit("#   [ ] focus selector on M       AF refuses S2 whenever focus does not confirm;")
emit("#                                 the SDK reads this back as 0x1002, unverifiable")
emit("#   [ ] image quality RAW only    a JPEG alongside doubles buffer use and triples")
emit("#                                 the time per frame; reads back 0x1013, unverifiable")
emit("#   [ ] exposure compensation 0")
emit("#   [ ] long-exposure NR off      it doubles the time of every frame past 1s")
emit("#   [ ] IS / OIS off              on a tripod it hunts")
emit("#   [ ] power save off            a sleeping body misses its cue and cannot be woken")
emit("#   [ ] PC CONNECTION MODE = USB TETHER SHOOTING FIXED, not card reader")
emit("#   [ ] card formatted, battery full, spare battery within reach")
emit("#")
emit("#   RIG")
emit("#   [ ] solar filter ON and secure - it comes off only between C2 and C3")
emit("#   [ ] focus set on the limb at high magnification, then taped or locked")
emit("#   [ ] framing allows for drift across the whole of totality")
emit("#   [ ] clear, low western horizon - the sun sets %.0f s after C4"
     % (sunset - c4).total_seconds())
emit("#")
emit("#   SOFTWARE, in this order")
emit("#   [ ] detect the camera")
emit("#   [ ] connect the relay (S2 = channel 2, S1 = channel 1)")
emit("#   [ ] load this script LAST - commands bind to their devices at load time,")
emit("#       so a relay connected afterwards leaves every relay_* line skipped and")
emit("#       the X-T4 loses both contact bursts")
emit("#   [ ] run the relay smoke test below and confirm the body actually fires")
emit("#   [ ] check the clock: sync_cameras runs at C1-20m and again at C1-2m")
emit("#")
emit("#   STILL UNTESTED - bench these before the day")
emit("#   [ ] firing the remote jack while the SDK holds the session")
emit("#   [ ] the whole script through the scheduler rather than command by command")
emit()

# --------------------------------------------------------------------------
# Set-up
# --------------------------------------------------------------------------
emit("# --- Set-up and focus, before C1 (solar filter ON) ---")
sync("C1", "-", 20 * 60, "Sync both cameras")
for mins, cam, label in ((18, XT4, "Focus check"), (17, EOS, "Focus check"),
                         (12, XT4, "Focus check"), (11, EOS, "Focus check"),
                         (6, XT4, "Framing check"), (5, EOS, "Framing check")):
    filtered_shot(cam, "C1", "-", mins * 60, "%s, uneclipsed disc" % label)
alt3 = sun_altitude(moment_time("C1", "-", 180))
bracket(XT4, "C1", "-", 200, shutter(partial_exposure(alt3, ISO_PARTIAL)), ISO_PARTIAL,
        "+/- 2", 13, "Exposure-check bracket")
hdr(EOS, "C1", "-", 180, shutter(partial_exposure(alt3, ISO_PARTIAL) / 4, EOS_MAX_SHUTTER),
    ISO_PARTIAL, 4, "Exposure-check bracket")
relay_shoot("C1", "-", 150, "Relay smoke test - confirm the trigger fires the X-T4")
sync("C1", "-", 120, "Re-sync before the eclipse starts")
emit()

emit("# --- First contact ---")
announce("C1", "-", 60, "C1_IN_60_SECONDS", "One minute to first contact")
announce("C1", "-", 30, "C1_IN_30_SECONDS", "Thirty seconds to first contact")
filtered_shot(XT4, "C1", "-", 15, "Just before first contact")
announce("C1", "-", 10, "C1_IN_10_SECONDS", "Ten seconds to first contact")
filtered_shot(EOS, "C1", "-", 5, "Just before first contact")
announce("C1", "-", 5, "C1_IN_5_SECONDS", "Five seconds to first contact")
announce("C1", "-", 0, "C1", "First contact - the eclipse has started")
for off, cam in ((5, XT4), (10, EOS), (30, XT4), (45, EOS), (90, XT4), (120, EOS)):
    filtered_shot(cam, "C1", "+", off, "First contact")
emit()

# --------------------------------------------------------------------------
# Partial phases C1 -> C2, alternating bodies every 90 s
# --------------------------------------------------------------------------
emit("# --- Partial phases C1 -> C2 (filter ON, exposure tracks the sinking sun) ---")
t, i = 180.0, 0
while t < (c2 - c1).total_seconds() - 12 * 60:
    filtered_shot(XT4 if i % 2 == 0 else EOS, "C1", "+", t, "Partial C1-C2")
    t += 90.0
    i += 1
for j, before in enumerate((720, 630, 540, 450, 360, 300, 240, 180, 120, 75, 50)):
    filtered_shot(XT4 if j % 2 == 0 else EOS, "C2", "-", before, "Partial approaching C2")
emit()

emit("# --- Countdown to totality ---")
for offset, name in [(50 * 60, "C2_IN_50_MINUTES"), (40 * 60, "C2_IN_40_MINUTES"),
                     (30 * 60, "C2_IN_30_MINUTES"), (25 * 60, "C2_IN_25_MINUTES"),
                     (20 * 60, "C2_IN_20_MINUTES"), (15 * 60, "C2_IN_15_MINUTES"),
                     (10 * 60, "C2_IN_10_MINUTES"), (6 * 60, "C2_IN_6_MINUTES"),
                     (5 * 60, "C2_IN_5_MINUTES"), (4 * 60, "C2_IN_4_MINUTES"),
                     (2 * 60, "C2_IN_2_MINUTES")]:
    announce("C2", "-", offset, name, "%d minutes to totality" % (offset // 60))
announce("C2", "-", 90, "C2_IN_90_SECONDS", "Ninety seconds to totality")
announce("C2", "-", 60, "C2_IN_60_SECONDS", "One minute to totality - get ready")
announce("C2", "-", 40, "C2_IN_40_SECONDS", "Forty seconds to totality")
announce("C2", "-", 30, "C2_IN_30_SECONDS", "FILTERS OFF BOTH SCOPES")
sync("C2", "-", 26, "Last sync before totality")
announce("C2", "-", 20, "C2_IN_20_SECONDS", "Twenty seconds - filters off, glasses off at C2")
announce("C2", "-", 10, "C2_IN_10_SECONDS", "Ten seconds to totality")
for n, name in ((5, "C2_IN_5_SECONDS"), (4, "C2_IN_4_SECONDS"), (3, "C2_IN_3_SECONDS"),
                (2, "C2_IN_2_SECONDS"), (1, "C2_IN_1_SECOND")):
    announce("C2", "-", n, name, "%d to totality" % n)
emit()

# --------------------------------------------------------------------------
# Totality
# --------------------------------------------------------------------------
emit("# --- TOTALITY (%.0f s, sun %.1f deg - FILTERS OFF) ---" % (totality, alt_max))
emit("# X-T4: relay bursts at both contacts, SDK brackets in between (%.1f fps over USB)." % XT4_SDK_FPS)
emit("# The frames were laid out for two bodies, staggered so one is always exposing while")
emit("# the other reads out; with the 800D parked the gaps it filled are simply empty.")
emit("# The X-T4 take_picture before each burst exists to load the beads exposure before")
emit("# the relay takes over - the relay fires whatever is already dialled in.")
emit("#")
emit("# Everything inside a minute of a contact - the bursts and the spoken countdown -")
emit("# is scheduled against the limb-corrected moments, because that is when totality")
emit("# actually begins and ends.  The cues further out stay on the mean contacts, where")
emit("# a few seconds is nothing and not depending on the limb profile is worth more.")
emit("#")
emit("# The bead bursts are scheduled against the edges of the limb-corrected bead")
emit("# window, not against the contacts.  A smooth-Moon C3 is %.1f s later than the real"
     % abs((MOMENTS["C3_MEAN"].time_utc - MOMENTS["C3"].time_utc).total_seconds()))
emit("# one here, which is most of a burst.  These lines need the lunar limb profile")
emit("# installed and the correction switched on; without it they are skipped, and")
emit("# Solar Eclipse Workbench says so when the script is loaded.")
emit("#")
emit("# The beads run %.2f s at C2 and %.2f s at C3, against a hold of %.1f s, so the burst"
     % ((MOMENTS["BEADS_C2_END"].time_utc - MOMENTS["BEADS_C2_START"].time_utc).total_seconds(),
        (MOMENTS["BEADS_C3_END"].time_utc - MOMENTS["BEADS_C3_START"].time_utc).total_seconds(),
        RELAY_C2_S))
emit("# cannot cover the whole window.  It is pinned to the contact-side edge, where the")
emit("# diamond ring is: the C2 burst ends at BEADS_C2_END, the C3 burst starts at")
emit("# BEADS_C3_START, both with %.1f s of margin towards totality." % RELAY_EDGE_MARGIN_S)
emit("#")
emit("# The hold is %.1f s because %d frames at %.0f fps is all the 32-slot transfer queue"
     % (RELAY_C2_S, RELAY_C2_N, XT4_RELAY_FPS))
emit("# takes; a full queue stops the body dead.  Covering the whole window needs a pulsed")
emit("# burst at ~%.1f fps instead of a held one, which is not yet measured on this body."
     % (RELAY_C2_N / (MOMENTS["BEADS_C2_END"].time_utc
                      - MOMENTS["BEADS_C2_START"].time_utc).total_seconds()))

picture(XT4, "BEADS_C2", "-", 6.0, beads_x, ISO_BEADS, "Load the beads exposure before the relay burst")
relay_arm("BEADS_C2", "-", 4.0, "Pre-arm S1 for the C2 burst")
burst(EOS, "BEADS_C2", "-", EOS_C2_BURST_S / 2, beads_e, ISO_BEADS, EOS_C2_BURST_S,
      int(EOS_C2_BURST_S * EOS_BURST_FPS), "Diamond ring and Baily's beads at C2")
relay_burst("BEADS_C2_END", "-", RELAY_C2_S + RELAY_LATENCY_S - RELAY_EDGE_MARGIN_S,
            RELAY_C2_S, RELAY_C2_N,
            "Diamond ring and Baily's beads at C2, relay at %.0f fps" % XT4_RELAY_FPS)
relay_release("BEADS_C2_END", "+", 1.5, "Open every contact after the C2 burst")
announce("C2", "-", 0, "C2", "Second contact - filters off, totality has begun")
picture(EOS, "C2", "+", 4.0, chromo, ISO_BEADS, "Chromosphere")
picture(EOS, "C2", "+", 5.5, prom, ISO_BEADS, "Prominences")
bracket(XT4, "C2", "+", 6.0, CORONA_LADDER.split(";")[0], ISO_CORONA,
        CORONA_LADDER, CORONA_FRAMES, "Corona ladder A1, inner edge to outer streamers")
hdr(EOS, "C2", "+", 7.0, hdr_start, ISO_CORONA, hdr_stops, "Corona ladder A")
bracket(XT4, "C2", "+", 18.0, CORONA_LADDER.split(";")[0], ISO_CORONA,
        CORONA_LADDER, CORONA_FRAMES, "Corona ladder A2, inner edge to outer streamers")
bracket(XT4, "C2", "+", 30.0, CORONA_LADDER.split(";")[0], ISO_CORONA,
        CORONA_LADDER, CORONA_FRAMES, "Corona ladder A3, inner edge to outer streamers")
hdr(EOS, "C2", "+", 26.0, hdr_start, ISO_CORONA, hdr_stops, "Corona ladder B")
announce("C2", "+", 30, "C2_PLUS_30_SECONDS", "Thirty seconds into totality")
picture(EOS, "C2", "+", 45.0, deep, ISO_DEEP, "Deep outer corona")
picture(XT4, "C2", "+", 46.0, deep, ISO_DEEP, "Deep outer corona")
picture(EOS, "C2", "+", 47.5, deeper, ISO_DEEP, "Deepest outer corona / earthshine")
announce("MAX", "-", 10, "MAX_IN_10_SECONDS", "Ten seconds to maximum eclipse")
for n, name in ((5, "MAX_IN_5_SECONDS"), (4, "MAX_IN_4_SECONDS"), (3, "MAX_IN_3_SECONDS"),
                (2, "MAX_IN_2_SECONDS"), (1, "MAX_IN_1_SECOND")):
    announce("MAX", "-", n, name, "%d to maximum eclipse" % n)
picture(XT4, "C2", "+", 49.5, deeper, ISO_DEEP, "Deepest outer corona / earthshine")
picture(EOS, "C2", "+", 50.5, inner, ISO_CORONA, "Inner corona at maximum eclipse")
announce("MAX", "-", 0, "MAX", "Maximum eclipse")
picture(XT4, "C2", "+", 52.0, inner, ISO_CORONA, "Inner corona at maximum eclipse")
picture(EOS, "C2", "+", 53.0, deeper, ISO_DEEP, "Deepest outer corona / earthshine")
picture(EOS, "C2", "+", 55.5, deep, ISO_DEEP, "Deep outer corona")
announce("C3", "-", 45, "C3_IN_45_SECONDS", "Forty-five seconds of totality left")
bracket(XT4, "C2", "+", 56.0, CORONA_LADDER.split(";")[0], ISO_CORONA,
        CORONA_LADDER, CORONA_FRAMES, "Corona ladder B1, inner edge to outer streamers")
hdr(EOS, "C2", "+", 58.5, hdr_start, ISO_CORONA, hdr_stops, "Corona ladder C")
bracket(XT4, "C2", "+", 68.0, CORONA_LADDER.split(";")[0], ISO_CORONA,
        CORONA_LADDER, CORONA_FRAMES, "Corona ladder B2, inner edge to outer streamers")
bracket(XT4, "C2", "+", 80.0, CORONA_LADDER.split(";")[0], ISO_CORONA,
        CORONA_LADDER, CORONA_FRAMES, "Corona ladder B3, inner edge to outer streamers")
picture(EOS, "C3", "-", 23.0, deep, ISO_DEEP, "Deep outer corona")
picture(EOS, "C3", "-", 20.5, inner, ISO_CORONA, "Inner corona")
announce("C3", "-", 20, "C3_IN_20_SECONDS", "Twenty seconds to third contact")
picture(EOS, "C3", "-", 16.0, prom, ISO_BEADS, "Prominences before C3")
picture(EOS, "C3", "-", 14.0, chromo, ISO_BEADS, "Chromosphere before C3")
picture(XT4, "BEADS_C3", "-", 6.0, beads_x, ISO_BEADS, "Load the beads exposure before the relay burst")
announce("C3", "-", 8, "C3_IN_8_SECONDS", "Eight seconds - look away from the eyepiece")
relay_arm("BEADS_C3", "-", 4.0, "Pre-arm S1 for the C3 burst")
burst(EOS, "BEADS_C3", "-", EOS_C3_BURST_S / 2, beads_e, ISO_BEADS, EOS_C3_BURST_S,
      int(EOS_C3_BURST_S * EOS_BURST_FPS), "Baily's beads and diamond ring at C3")
relay_burst("BEADS_C3_START", "-", RELAY_LATENCY_S + RELAY_EDGE_MARGIN_S,
            RELAY_C3_S, RELAY_C3_N,
            "Baily's beads and diamond ring at C3, relay at %.0f fps" % XT4_RELAY_FPS)
relay_release("BEADS_C3_START", "+", RELAY_C3_S + 1.5,
              "Open every contact after the C3 burst")
for n, name in ((5, "C3_IN_5_SECONDS"), (4, "C3_IN_4_SECONDS"), (3, "C3_IN_3_SECONDS"),
                (2, "C3_IN_2_SECONDS"), (1, "C3_IN_1_SECOND")):
    announce("C3", "-", n, name, "%d to third contact" % n)
announce("C3", "+", 2, "C3_PLUS_2_SECONDS", "Third contact - totality is over")
announce("C3", "+", 5, "FILTERS_ON", "FILTERS ON BOTH SCOPES")
# Recovery ladder: under adrenaline the minute after C3 is when filters get
# forgotten and settings get left where totality put them.
announce("C3", "+", 10, "C3_PLUS_10_SECONDS", "Ten seconds past third contact")
announce("C3", "+", 15, "C3_PLUS_15_SECONDS", "Fifteen seconds past third contact")
announce("C3", "+", 25, "C3_PLUS_25_SECONDS", "Twenty-five seconds past third contact")
announce("C3", "+", 45, "C3_PLUS_45_SECONDS", "Forty-five seconds past third contact")
announce("C3", "+", 60, "C3_PLUS_1_MINUTE", "One minute past third contact")
announce("C3", "+", 120, "C3_PLUS_2_MINUTES", "Two minutes past third contact")
emit()

# --------------------------------------------------------------------------
# Partial phases C3 -> sunset
# --------------------------------------------------------------------------
emit("# --- Partial phases C3 -> sunset (filter ON, sun dropping to the horizon) ---")
k = 0
for after in (45, 75, 120, 180, 240, 330, 420, 510, 600, 720, 840, 960, 1020):
    if sun_altitude(moment_time("C3", "+", after)) < 5.0:
        break
    filtered_shot(XT4 if k % 2 == 0 else EOS, "C3", "+", after, "Partial after C3")
    k += 1
emit()

emit("# --- Below 5 degrees: the tables are extrapolated, so bracket widely ---")
after, k = 1080.0, 0
while True:
    alt = sun_altitude(moment_time("C3", "+", after))
    if alt < 0.2:
        break
    iso = ISO_PARTIAL if alt > 2.0 else ISO_CORONA
    centre = partial_exposure(alt, iso)
    label = "Low-sun bracket"
    extra = ", X=%.0f, centre %s" % (airmass(alt), shutter(centre))
    if k % 2 == 0:
        bracket(XT4, "C3", "+", after, shutter(centre), iso, "+/- 2", 13,
                label, extra)
    else:
        stops = 4 if alt > 2.0 else 6
        hdr(EOS, "C3", "+", after, shutter(centre / (2 ** (stops / 2)), EOS_MAX_SHUTTER),
            iso, stops, label, extra)
    after += 90.0
    k += 1
emit()

emit("# --- Last light ---")
announce("C4", "-", 60, "C4_IN_60_SECONDS", "One minute to fourth contact (the sun is setting)")
announce("C4", "-", 20, "C4_IN_20_SECONDS", "Twenty seconds to fourth contact")
announce("C4", "-", 0, "C4", "Fourth contact - eclipse over")


# --------------------------------------------------------------------------
# Output, one file per body
# --------------------------------------------------------------------------
# The X-T4 file keeps every shared line - the comments, the voice prompts and the
# camera sync - because it is the one that runs.  The 800D file carries only its own
# commands: Solar Eclipse Workbench loads a single script at a time, so if the pair
# is ever run again it is on a second machine, and doubling the voice prompts across
# two laptops standing next to each other would be worse than having none.

production = [text for owner, text in LINES if owner in (None, XT4)]
OUTPUT.write_text("\n".join(production) + "\n")
print("written %s (%d lines, %s %d frames)" % (OUTPUT, len(production), XT4, FRAMES[XT4]))

parked = [text for owner, text in LINES if owner == EOS]
PARKED_HEADER = [
    "# Solar Eclipse Workbench script - total solar eclipse of 12 August 2026",
    "# Generated by scripts/generate_20260812_production.py - edit that, not this file.",
    "#",
    "# The %s half of the two-body production schedule, split out because the" % EOS,
    "# body is parked.  It is the 800D's commands only: the voice prompts, the comments",
    "# and the sun-altitude reasoning live in the X-T4 script it was cut from, and the",
    "# frames here are timed to interleave with that script rather than to stand alone.",
    "# For the 800D on its own, use scripts/test/20260812_EOS800D.txt instead.",
    "#",
    "# Site   : %s  %.4f N, %.4f E, %d m" % (SITE, LAT, LON, OBS_ALT),
    "# Frames : %d" % FRAMES[EOS],
    "#",
]
PARKED.write_text("\n".join(PARKED_HEADER + parked) + "\n")
print("written %s (%d lines, %s %d frames)" % (PARKED, len(parked), EOS, FRAMES[EOS]))
