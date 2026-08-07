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
``scripts/real/`` - the only directory that runs on the day - and the 800D's
commands go to ``scripts/test/20260812_production_EOS800D.txt``.  Solar Eclipse
Workbench loads one script at a time, so leaving the 800D lines in the production
script would only have produced a wall of "camera not found" at load.

One script per totality duration
--------------------------------
How long totality lasts depends on where you stand, and weather can move that on
the morning.  Everything outside totality already follows the observer - the
commands are offsets from C1/C2/C3, which Solar Eclipse Workbench resolves from
the position set at run time, and the exposures track the sun's altitude at each
frame.  What does not follow is how many corona ladders fit between the contacts:
those are laid out here, and a layout built for a longer totality than you get is
still exposing when C3 arrives, with the filter off.

So this writes one script per duration, ``20260812_production_<D>s.txt``, each
filling D seconds.  On the day, read the totality Solar Eclipse Workbench reports
for the site and load **the largest D that does not exceed it** - the file says so
in its own header.  A file that fills less than the totality you have costs a few
unshot seconds at the end; one that fills more costs frames taken after C3.

    python scripts/generate_20260812_production.py
    python scripts/generate_20260812_production.py --lat 42.31 --lon -3.98 \
        --alt 900 --site "Burgos, N Spain"
"""

import argparse
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

OUTPUT_DIR = REPO / "scripts" / "real"
PARKED = REPO / "scripts" / "test" / "20260812_production_EOS800D.txt"

# --- Site -----------------------------------------------------------------
# The planned site.  Everything below derives from these three numbers, so a
# move is a re-run with --lat/--lon/--alt rather than an edit.
ECLIPSE_DATE = "2026-08-12"

_parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
_parser.add_argument("--site", default="Palencia, N Spain",
                     help="name for the file headers")
_parser.add_argument("--lat", type=float, default=42.0095)
_parser.add_argument("--lon", type=float, default=-4.5289)
_parser.add_argument("--alt", type=float, default=740.0, help="site height in metres")
_parser.add_argument("--durations", type=float, nargs="+",
                     default=[float(d) for d in range(40, 170, 10)],
                     help="totality durations to write a script for, in seconds")
_args = _parser.parse_args()

SITE = _args.site
LAT, LON, OBS_ALT = _args.lat, _args.lon, _args.alt
DURATIONS = sorted(_args.durations)

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
# Measured on the body, 4 August, with the relay holding a full press (S1 then
# S2) and frames counted from the shutter counter:
#
#     CL, menu set to 8 fps   32 frames in 4.15 s   7.7 fps
#     CH, menu as found       61 frames in 2.15 s  28.4 fps
#
# CL is what this script is built for.  At 28 fps the beads window cannot be
# covered at all - 4 s of it wants 113 frames - and the body hits its own
# buffer a second in, so the burst decelerates exactly where the diamond ring
# is.  At 7.7 fps one held contact covers the whole window in 32 frames.
#
# This is a menu setting (DRIVE SETTING -> CL LOW SPEED BURST) on a dial
# position that cannot be read back: GetDriveMode answers 0x0004 wherever the
# dial is, so neither the rate nor the mode can be checked in software.  Both
# are eye checks in the pre-flight.
XT4_RELAY_FPS = 7.7        # CL drive at 8 fps, shutter held closed by the relay
EOS_BURST_FPS = 6.0

# ISO 100 for the filtered partials: with photographic film at f/6 the sun wants
# 1/7139 just after C1, which fits the X-T4's 1/8000 but not the 800D's 1/4000.
# Anything faster than ISO 100 puts both bodies over their ceiling.
# 160, not 100.  This body's base is ISO 160; 100 is the extended "L" pull,
# which does not lower the noise floor and costs about a stop of highlight
# headroom.  The beads and the diamond ring are the highest-contrast frames of
# the whole day - photosphere against corona in one frame - so they are the
# last place to give a stop of highlights away.  Verified on the body: 100 is
# accepted, so this was being set, and at 160 the beads want 1/6438, still
# inside the shutter's range.
ISO_PARTIAL, ISO_BEADS, ISO_CORONA, ISO_DEEP = 160, 160, 400, 800

T = Time(ECLIPSE_DATE + " 00:00:00")
MOMENTS, MAGNITUDE, TYPE = calculate_reference_moments(LON, LAT, OBS_ALT, T)

_eph = load(str(PACKAGE / "de421.bsp"))
_ts = load.timescale()
_place = _eph["Earth"] + wgs84.latlon(LAT, LON, OBS_ALT)


def sun_altitude(when) -> float:
    # Refracted, like the main calculator: the exposure tables key on where the
    # sun APPEARS.  Geometric altitude underestimates it near the horizon by
    # over half a degree - more than a stop of airmass for the last brackets.
    return _place.at(_ts.from_datetime(when)).observe(_eph["Sun"]).apparent().altaz(
        temperature_C=15.0, pressure_mbar=1013.0)[0].degrees


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

# Inside totality the absolute clock time of a frame is a property of the site's
# own duration, not of the file, so totality frames are labelled by their offset
# instead.  A file built for 90 s carries frames the site's 97 s would put at a
# different wall-clock time, and printing that time would be a lie.
NOTE_RELATIVE = False


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

    Inside totality there is no honest absolute time to print: a file built for a
    duration other than this site's would put the frame at a clock time that never
    happens here, so those frames are labelled by their offset instead.
    """
    if NOTE_RELATIVE:
        return "%s @ %s%s%.1f s, sun %.1f deg%s" % (what, ref, sign, offset, alt_max, extra)
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
    # Two decimals: the hold is now derived from a measured rate, and
    # "4.6499999999999995" in a line read by torchlight is noise where a number
    # should be.  Hundredths are finer than the relay's own latency.
    emit("relay_burst, %s, %s, %s, %.2f, \"%s\"" %
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
# take_hdr on the 800D is what this bound is for; the X-T4's ladder is written
# out speed by speed and is bounded by the corona, not by the Canon.
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


# ISO 1600 rather than 400 buys two stops of shutter for two stops of gain, and
# the faintest corona is where the clock hurts and the noise does not: a rung at
# 1/2" reaches what 2" reached at ISO 400, in a quarter of the time.  That is
# what lets one ladder span the whole corona and still run six times.
ISO_LADDER = 1600
_gain = ISO_LADDER / ISO_CORONA

# The ladder has to reach as faint as the separate deep frames it replaced, or
# merging them in would quietly cost the outer streamers.  `deeper` is the
# faintest thing the schedule ever asked for, so the ladder runs from the inner
# edge to that, both expressed at the ladder's own ISO.
_ladder_start = cor_low / 2 / _gain
_ladder_end = totality_exposure("corona_outer_8R", alt_max, ISO_CORONA) * 2 / _gain
XT4_LADDER_STOPS = int(math.ceil(math.log2(_ladder_end / _ladder_start)))
CORONA_LADDER, CORONA_FRAMES = corona_ladder(_ladder_start, XT4_LADDER_STOPS)

sys.stderr.write(
    "ladder  %s  @ISO%d  (%d rungs, %d stops)\n"
    % (CORONA_LADDER, ISO_LADDER, CORONA_FRAMES, XT4_LADDER_STOPS))
# The rungs step 2 EV at a time, so the last one lands at or below the target -
# report the rung, not the span, or this reads as more reach than it has.
_last = _ladder_start * 2 ** (2 * (CORONA_FRAMES - 1))
sys.stderr.write(
    "  faintest rung %.3fs @ISO%d = %.2fs at ISO%d; the singles it replaces reached %.2fs\n"
    % (_last, ISO_LADDER, _last * _gain, ISO_CORONA,
       totality_exposure("corona_outer_8R", alt_max, ISO_CORONA) * 2))


# --------------------------------------------------------------------------
# How many ladders fit in a totality
# --------------------------------------------------------------------------
# A command that cannot take the camera within 1.5s is dropped rather than
# delayed, so ladders have to be spaced by what they actually cost.  These are
# the constants scripts/validate_totality.py charges them, which were fitted to
# the brackets measured on the X-T4 on 3 August; keeping the two in step means
# the validator agrees with what was laid out here.
LADDER_TAP_GAP_S = 0.35
# The speed change between two taps: the USB write plus the body settling.
# 0.5, was 0.35: measured 7 August on the fixed ladder, ten consecutive
# seven-rung ladders ran 6.32-6.54 s where the model said 5.43, so the old
# figure under-priced every ladder by about a second - eight of those is most
# of a ninth ladder, and under-pricing is the direction that runs the last
# one past C3.
LADDER_PER_RUNG_USB_S = 0.5
# 2.0, recalibrated 5 August: the eight hardware ladders of the 4 August
# rehearsal ran 6.38-6.90 s INCLUDING their drain, which prices the drain
# near 1.2 s under the lazy-drain rework.  The old 3.0 predates that rework
# and reserved 1.8 s per ladder of nothing - the reserve that was blocking a
# second single per gap.  Worst measured plus half a second of margin.
LADDER_DRAIN_S = 2.0
# Below this the next ladder is starting while the one before it still has the
# camera.  Seven ladders ran 6.4-6.9 s each on the 4 August rehearsal against
# a 13.2 s pitch - which is 47% duty and 6.6 s of idle shutter between
# ladders, the "density much too low" complaint in one number.  10 s keeps
# 3 s of clearance over the worst measured ladder and lifts the count.
# 11.0: the floor the gap single's spacing rule allows - the model's ladder
# cost (8.4 s, deliberately pessimistic) plus the single and margins is 11.2,
# and the spread stretches actual pitches above the floor anyway.  Measured
# honestly against the 4 August ladder times, mid-totality duty lands around
# two thirds; the earlier "near 70%" was arithmetic against the floor rather
# than the stretched pitch, and overstated it.
LADDER_PITCH_MIN_S = 10.3

#: How close the last gap single may sit to the next ladder: its own modelled
#: cost (0.8 s) plus clearance.  It was 2.5 s of standoff, which priced the
#: third single out of every gap for no measured reason.
GAP_SINGLE_COST_S = 1.1
# The first ladder waits for the C2 burst to be released and its frames drained.
# 8.5, was 6.0: the C2 burst with live draining keeps the camera until about
# C2+7.6 (contact to +3.5, queue tail ~4 s), and ladder 1 at C2+6 was dropped
# against it on the 5 August run - seven frames of corona.
TOTALITY_HEAD_S = 8.5
# and the last has to be out of the way before the C3 bead sequence loads.
TOTALITY_TAIL_MARGIN_S = 2.0


def ladder_seconds(ladder: str) -> float:
    """Seconds a semicolon ladder holds the camera.

    Each rung waits out its own frame and then puts the next speed over USB, and
    the bracket drains at the end.  The seven-rung production ladder models at
    8.5 s against 6.3-6.5 s of rungs plus the drain measured on 7 August -
    pessimistic by a little, which is the direction to be wrong in.
    """
    total = LADDER_DRAIN_S
    for rung in ladder.split(";"):
        exposure = _rung_seconds(rung)
        total += max(LADDER_TAP_GAP_S, exposure + 0.3) + LADDER_PER_RUNG_USB_S
    return total


def _rung_seconds(rung: str) -> float:
    rung = rung.strip().rstrip('"')
    if rung.startswith("1/"):
        return 1.0 / float(rung[2:])
    return float(rung)


LADDER_RUN_S = ladder_seconds(CORONA_LADDER)

# What the gaps between ladders are for.
#
# They used to hold one exposure, repeated: the ladder's own slowest rung,
# seven times over.  That deepened the faintest frames and added not one stop
# of range, which is the wrong thing to spend the only irreplaceable two
# minutes of the day on.
#
# The ladder steps two stops at a time, so between every pair of rungs there
# is a stop nothing covers.  These are exactly those stops, and one more
# beyond the slowest rung for the outermost streamers and the earthshine.
# Rotated through the gaps, the union of ladders and singles is a one-stop
# ladder from the inner edge to past the outer corona - which is what a
# composite is assembled from.
#
# At the ladder's own ISO, deliberately: one gain across every totality frame
# means one noise character to match when they are stacked.
def _interleave(ladder: str) -> list:
    """The stops the ladder steps over, plus one past its faint end."""
    rungs = [_rung_seconds(r) for r in ladder.split(";")]
    between = [r * 2 for r in rungs[:-1]]          # one stop above each rung
    return [shutter(x) for x in between + [rungs[-1] * 2]]


GAP_SINGLE_LADDER = _interleave(CORONA_LADDER)


def ladder_offsets(duration_s: float) -> list:
    """C2-relative start times for the corona ladders, for a totality of `duration_s`.

    As many as fit, spread evenly across what is left once the head and tail are
    reserved, and never closer together than LADDER_PITCH_MIN_S.  Spreading
    rather than packing puts the same number of ladders across the whole of
    totality instead of crowding them against C2 and leaving a hole before C3.
    """
    usable = duration_s - TOTALITY_HEAD_S - totality_tail_s() - LADDER_RUN_S
    if usable < 0.0:
        return []
    count = int(usable // LADDER_PITCH_MIN_S) + 1
    if count == 1:
        return [TOTALITY_HEAD_S]
    pitch = usable / (count - 1)
    return [TOTALITY_HEAD_S + i * pitch for i in range(count)]


def totality_tail_s() -> float:
    """Seconds before C3 that the last ladder has to be finished by.

    The C3 block opens by loading the beads exposure, and that is a camera
    command like any other: a ladder still running when it fires takes it out.
    Where the bead window sits relative to C3 comes from the limb profile at
    this site, so this is measured off the moments rather than assumed.
    """
    first_c3_command = moment_time("BEADS_C3", "-", 6.0)
    return (c3 - first_c3_command).total_seconds() + TOTALITY_TAIL_MARGIN_S

# How wide the beads actually are here, from the limb profile rather than
# assumed: the bursts are sized off these.
BEADS_C2_S = (MOMENTS["BEADS_C2_END"].time_utc
              - MOMENTS["BEADS_C2_START"].time_utc).total_seconds()
BEADS_C3_S = (MOMENTS["BEADS_C3_END"].time_utc
              - MOMENTS["BEADS_C3_START"].time_utc).total_seconds()


# Cover the whole window, with a margin at each end.
#
# The hold used to be 1.9 s against windows of 0.00 s and 0.00 s, so even with
# perfect contact times less than half the beads were photographed.  That is
# why the burst had to be pinned to the contact-side edge, and why undershooting
# cost the diamond ring: a burst that cannot span the window has to gamble on
# where inside it to sit.  At the measured CL rate it spans the window, so it
# does not have to gamble, and the timing error the pinning was guarding
# against stops mattering.
#
# One hold is also one buffer: 32 frames covered 4.15 s on the bench, and the
# card keeps every frame regardless, since the body records RAW+JPEG to it
# while tethered.  The transfer queue holds a copy of every frame until the
# PC deletes it - and at 32 undrained frames the body HARD-STOPS a held
# burst, which silently truncated every burst before 5 August.  relay_burst
# now drains the queue during the hold, so a burst runs as long as its hold.
# The priority order, fixed by the person whose eclipse it is: the diamond
# ring FOR SURE, then the beads, then everything else.
#
# The ring is the last surviving bead blazing with the corona visible - so it
# lives at the BOUNDARY between the bead window and totality: BEADS_C2_END
# going in, BEADS_C3_START coming out.  The window itself is the beads.  The
# margins therefore guard the ring's edge with two seconds against limb-solve
# error on the totality side, keep the whole window, and give the crescent
# side what remains of the hold.  (The "60-frame buffer" that used to bound
# these holds was really the 32-slot tether queue hard-stopping the body;
# with the queue drained live the practical bound is the schedule, not the
# camera.  The margins stay as the owner set them.)
#
# OWNER'S SPECIFICATION - final, not to be re-reasoned:
#
#   "I WANT SMALL DIAMOND and that is on the side of totality."
#
# The canonical diamond ring - "seen when only one or two beads are left" -
# at the beads/totality boundary: BEADS_C2_END going in, BEADS_C3_START
# coming out.  The deep margin sits on the totality side of both windows to
# hold that ring against limb-solve error.  The crescent side (fat sliver,
# forming/fading beads) gets the remainder of the hold.
#
# The margins flipped four times in two days, each flip argued from a
# different reading of the same phenomena.  The owner has now specified the
# target; margin changes from here require the owner, not an argument.
#
#     C2:  head 1.9 (crescent, beads forming)  window 3.25  tail 2.6 (SMALL DIAMOND)
#     C3:  head 2.6 (SMALL DIAMOND)  window 4.05  tail 1.1 (beads fading, crescent)
RELAY_C2_HEAD_S = 1.9
RELAY_C2_TAIL_S = 2.6
RELAY_C3_HEAD_S = 2.6
RELAY_C3_TAIL_S = 1.1
RELAY_C2_S = BEADS_C2_S + RELAY_C2_HEAD_S + RELAY_C2_TAIL_S
RELAY_C3_S = BEADS_C3_S + RELAY_C3_HEAD_S + RELAY_C3_TAIL_S

# Trigger latency, measured on the bench 1 August 2026, and it depends entirely on
# the path: S1 pre-armed and held gives 43-48 ms, S2 alone with S1 never asserted
# ~130 ms, and a bare shoot() 170 ms - of which 120 ms is our own settle before S2
# closes.  So the bursts below pre-arm with relay_arm and take the fast path, which
# both shrinks the lead and stops it depending on how the release cable is wired.
RELAY_LATENCY_S = 0.045
# Rounded up, not down.  The runtime converts the frame count back into a hold
# time, so truncating here shortens the burst - by 0.08 s at C2, which comes off
# the end of the window where the diamond ring is.  A frame too many costs one
# black frame at beads exposure.
RELAY_C2_N = math.ceil(RELAY_C2_S * XT4_RELAY_FPS)
RELAY_C3_N = math.ceil(RELAY_C3_S * XT4_RELAY_FPS)

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
emit("#   [ ] PREVIEW EXP./WB IN MANUAL MODE = ON, or live view shows a")
emit("#       normalised image and its histogram says nothing about exposure")
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
# The X-T4 shoots every step of the approach; the alternation with the EOS
# gave half these frames to a parked body, which on the 4 August rehearsal
# read as "the camera is very idle leading up to C2".  The ramp also tightens
# towards the contact - a thinning crescent changes faster than it did ten
# minutes earlier - and the singles keep the transfer queue draining so the
# burst meets an empty buffer.  It ends at 45 s out: the filters come off at
# 40, and a filtered exposure of an unfiltered sun is a white frame.
for j, before in enumerate((720, 630, 540, 450, 360, 300, 240, 180, 120, 75, 50)):
    if j % 2 == 1:
        filtered_shot(EOS, "C2", "-", before, "Partial approaching C2")
for before in (720, 540, 360, 240, 180, 150, 120, 100, 85, 70, 57, 45):
    filtered_shot(XT4, "C2", "-", before, "Partial approaching C2")
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
# Everything above is the same whatever totality turns out to be: the commands
# are offsets from C1 and C2, and the exposures follow the sun's altitude at the
# site.  Only the corona ladders have to know how long they have, so the
# totality block is built per duration and the rest is written once.
PREAMBLE = LINES
PREAMBLE_FRAMES = dict(FRAMES)
LINES, FRAMES = [], {XT4: 0, EOS: 0}


def capture(fn, *args):
    """Run `fn` and return the lines it emitted and what they cost in frames."""
    global LINES, FRAMES
    outer_lines, outer_frames = LINES, FRAMES
    LINES, FRAMES = [], {XT4: 0, EOS: 0}
    try:
        fn(*args)
        return LINES, FRAMES
    finally:
        LINES, FRAMES = outer_lines, outer_frames


def inside(offset: float, target_s: float) -> bool:
    """Is a cue this far past C2, or this far before C3, still inside totality?"""
    return 0.0 < offset < target_s


def totality_block(target_s: float) -> None:
    global NOTE_RELATIVE
    NOTE_RELATIVE = True
    try:
        _totality_block(target_s)
    finally:
        NOTE_RELATIVE = False


def _totality_block(target_s: float) -> None:
    offsets = ladder_offsets(target_s)
    emit("# --- TOTALITY (%.0f s, sun %.1f deg - FILTERS OFF) ---" % (target_s, alt_max))
    emit("# X-T4: relay bursts at both contacts, SDK brackets in between (%.1f fps over USB)." % XT4_SDK_FPS)
    emit("# The X-T4 take_picture before each burst exists to load the beads exposure before")
    emit("# the relay takes over - the relay fires whatever is already dialled in.")
    emit("#")
    emit("# This file fills %.0f s of totality: %d corona ladder%s of %d frames, the first"
         % (target_s, len(offsets), "" if len(offsets) == 1 else "s", CORONA_FRAMES))
    emit("# %.1f s after C2 and the last finishing %.1f s before C3.  Load it only when Solar"
         % (TOTALITY_HEAD_S,
            target_s - (offsets[-1] + LADDER_RUN_S) if offsets else target_s))
    emit("# Eclipse Workbench reports totality of at least %.0f s for where you are standing;"
         % target_s)
    emit("# at less than that the last ladder is still exposing when the sun comes back.")
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
    emit("# The beads run %.2f s at C2 and %.2f s at C3, and the bursts hold %.1f s and %.1f s."
         % (BEADS_C2_S, BEADS_C3_S, RELAY_C2_S, RELAY_C3_S))
    emit("# Owner's specification: the SMALL diamond - 'one or two beads left' - on the")
    emit("# totality side of each bead window.  The deep margin sits there at both")
    emit("# contacts; the crescent side gets the remainder of the hold.  Bursts drain")
    emit("# their tether queue live, so a hold is no longer capped at 32 frames.")
    emit("# Priority: small diamond ring, then beads, then everything else.")
    emit("# C2 head %.1f / tail %.1f;  C3 head %.1f / tail %.1f."
         % (RELAY_C2_HEAD_S, RELAY_C2_TAIL_S, RELAY_C3_HEAD_S, RELAY_C3_TAIL_S))
    emit("#")
    emit("# %d and %d frames, at the %.1f fps measured on this body with the drive dial on CL"
         % (RELAY_C2_N, RELAY_C3_N, XT4_RELAY_FPS))
    emit("# and CL LOW SPEED BURST set to 8 fps.  Both are menu settings on a dial position")
    emit("# that cannot be read back - GetDriveMode answers the same wherever the dial is - so")
    emit("# the pre-flight checks them by eye.  On CH as found (28 fps) the window would want")
    emit("# %d frames and the body would hit its own buffer a second in, slowing the burst"
         % round(BEADS_C3_S * 28.4))
    emit("# exactly where the diamond ring is.")

    # Load, arm and burst share one anchor and one base offset, so no future
    # widening of the head can reorder them.  On 5 August the load was anchored
    # to BEADS_C2 while the burst start had moved out past it with the 4.1 s
    # head: the shutter-speed write landed half a second INTO the held burst,
    # on a body in continuous drive, and the burst died - "C2 seemed to have
    # no burst" was exactly right.
    # The framed big-diamond ring is ~5 EV slower than the beads exposure the
    # burst runs at (the calculator's own DIAMOND_RING table: ~1/80 here), and
    # an exposure write into a held burst kills the drive - proven 5 August.
    # So the ring gets its own bracket where it actually is: the crescent side,
    # clear of the burst at both contacts.
    # Scaled with the ISO, not just relabelled: these were 1/160;1/80;1/40 at
    # ISO 100, and ISO 160 is two thirds of a stop more sensitive, so the same
    # exposure needs the shutter two thirds of a stop faster.  Changing the
    # gain and leaving the speeds would have brightened the ring by that much.
    ring_ladder = "1/250;1/125;1/60"
    _c2_burst_off = RELAY_C2_S + RELAY_LATENCY_S - RELAY_C2_TAIL_S
    bracket(XT4, "BEADS_C2_END", "-", _c2_burst_off + 6.5, "1/250", ISO_BEADS,
            ring_ladder, 3, "Framed diamond ring, big diamond forming")
    picture(XT4, "BEADS_C2_END", "-", _c2_burst_off + 2.5, beads_x, ISO_BEADS,
            "Load the beads exposure before the relay burst")
    relay_arm("BEADS_C2_END", "-", _c2_burst_off + 1.2, "Pre-arm S1 for the C2 burst")
    burst(EOS, "BEADS_C2", "-", EOS_C2_BURST_S / 2, beads_e, ISO_BEADS, EOS_C2_BURST_S,
          int(EOS_C2_BURST_S * EOS_BURST_FPS), "Diamond ring and Baily's beads at C2")
    relay_burst("BEADS_C2_END", "-", _c2_burst_off,
                RELAY_C2_S, RELAY_C2_N,
                "Diamond ring and Baily's beads at C2, relay at %.0f fps" % XT4_RELAY_FPS)
    # Tracks the tail: the safety release must fire AFTER the hold lets go, or
    # it is not a safety net but a guillotine - at tail 2.0 a release at +1.5
    # would open the contacts half a second before the ring was done.
    relay_release("BEADS_C2_END", "+", RELAY_C2_TAIL_S + 1.2,
                  "Open every contact after the C2 burst")
    announce("C2", "-", 0, "C2", "Second contact - filters off, totality has begun")
    if inside(4.0, target_s):
        picture(EOS, "C2", "+", 4.0, chromo, ISO_BEADS, "Chromosphere")
    if inside(5.5, target_s):
        picture(EOS, "C2", "+", 5.5, prom, ISO_BEADS, "Prominences")

    # The file is read before the day, so it is written in the order things
    # happen.  How many ladders there are depends on the duration, so the cues
    # that fall among them are placed by offset rather than by hand.  MAX sits
    # in the middle of totality wherever you stand, which is what orders the
    # frames anchored to it.
    middle = target_s / 2.0
    timeline = []
    for index, offset in enumerate(offsets, start=1):
        timeline.append((offset, _ladder, (index, len(offsets), offset)))

    # A deep-corona single in each gap between ladders.  Measured on the
    # 4 August rehearsal: ladders of 6.4-6.9 s on the old pitch left 6.6 s of
    # idle shutter between each pair - 47% duty across the part of the eclipse
    # that cannot be repeated.  The single sits one second after the worst
    # measured ladder end, which leaves the camera free again well before the
    # next ladder loads.
    fill = 0
    for first, second in zip(offsets, offsets[1:]):
        # Anchored to the NEXT ladder, so however long the previous one runs
        # the singles are finished before it needs the camera; each exists
        # only when the model - not the happier measurement - leaves room.
        slot = second - GAP_SINGLE_COST_S - 0.5
        while slot > first + LADDER_RUN_S + 0.3:
            speed = GAP_SINGLE_LADDER[fill % len(GAP_SINGLE_LADDER)]
            fill += 1
            timeline.append((slot, picture,
                             (XT4, "C2", "+", slot, speed, ISO_LADDER,
                              "Corona single at %s, filling between the "
                              "ladder's stops" % speed)))
            slot -= 1.0
    # And one after the last ladder, in the stretch before the C3 sequence
    # needs the camera - the tail was the widest untouched gap left.
    if offsets:
        tail_at = offsets[-1] + LADDER_RUN_S + 0.5
        while tail_at + 1.5 < target_s - totality_tail_s():
            speed = GAP_SINGLE_LADDER[fill % len(GAP_SINGLE_LADDER)]
            fill += 1
            timeline.append((tail_at, picture,
                             (XT4, "C2", "+", tail_at, speed, ISO_LADDER,
                              "Corona single at %s before the C3 sequence"
                              % speed)))
            tail_at += 1.0
    if inside(30.0, target_s):
        timeline.append((30.0, announce,
                         ("C2", "+", 30, "C2_PLUS_30_SECONDS", "Thirty seconds into totality")))
    # Five seconds out, then the moment itself.  The count from ten down to one
    # filled the middle of totality with talking, and mid-totality is the one
    # stretch where nothing needs saying: nothing is about to change, the
    # frames are already scheduled, and the observer is looking up.
    timeline.append((middle - 5.0, announce,
                     ("MAX", "-", 5, "MAX_IN_5_SECONDS", "Five seconds to maximum eclipse")))
    timeline.append((middle, announce, ("MAX", "-", 0, "MAX", "Maximum eclipse")))
    # The parked body's mid-totality singles hang off MAX rather than off C2, so
    # they stay centred on totality instead of walking off the end of a short one.
    for delta, exposure, iso, what in ((-5.0, deep, ISO_DEEP, "Deep outer corona"),
                                       (-2.5, deeper, ISO_DEEP, "Deepest outer corona / earthshine"),
                                       (0.5, inner, ISO_CORONA, "Inner corona at maximum eclipse"),
                                       (3.0, deeper, ISO_DEEP, "Deepest outer corona / earthshine"),
                                       (5.5, deep, ISO_DEEP, "Deep outer corona")):
        sign = "+" if delta >= 0 else "-"
        timeline.append((middle + delta, picture,
                         (EOS, "MAX", sign, abs(delta), exposure, iso, what)))
    if inside(45.0, target_s):
        timeline.append((target_s - 45.0, announce,
                         ("C3", "-", 45, "C3_IN_45_SECONDS", "Forty-five seconds of totality left")))
    for offset in (7.0, 26.0, 58.5):
        if inside(offset, target_s):
            timeline.append((offset, hdr,
                             (EOS, "C2", "+", offset, hdr_start, ISO_CORONA, hdr_stops,
                              "Corona ladder")))
    for before, exposure, iso, what in ((23.0, deep, ISO_DEEP, "Deep outer corona"),
                                        (20.5, inner, ISO_CORONA, "Inner corona"),
                                        (16.0, prom, ISO_BEADS, "Prominences before C3"),
                                        (14.0, chromo, ISO_BEADS, "Chromosphere before C3")):
        if inside(before, target_s):
            timeline.append((target_s - before, picture,
                             (EOS, "C3", "-", before, exposure, iso, what)))
    if inside(20.0, target_s):
        timeline.append((target_s - 20.0, announce,
                         ("C3", "-", 20, "C3_IN_20_SECONDS", "Twenty seconds to third contact")))
    for _, call, arguments in sorted(timeline, key=lambda event: event[0]):
        call(*arguments)

    picture(XT4, "BEADS_C3", "-", 6.0, beads_x, ISO_BEADS, "Load the beads exposure before the relay burst")
    if inside(8.0, target_s):
        announce("C3", "-", 8, "C3_IN_8_SECONDS", "Eight seconds - look away from the eyepiece")
    relay_arm("BEADS_C3", "-", 4.0, "Pre-arm S1 for the C3 burst")
    burst(EOS, "BEADS_C3", "-", EOS_C3_BURST_S / 2, beads_e, ISO_BEADS, EOS_C3_BURST_S,
          int(EOS_C3_BURST_S * EOS_BURST_FPS), "Baily's beads and diamond ring at C3")
    relay_burst("BEADS_C3_START", "-", RELAY_LATENCY_S + RELAY_C3_HEAD_S,
                RELAY_C3_S, RELAY_C3_N,
                "Baily's beads and diamond ring at C3, relay at %.0f fps" % XT4_RELAY_FPS)
    bracket(XT4, "BEADS_C3_START", "+", RELAY_C3_S + 5.5, "1/250", ISO_BEADS,
            ring_ladder, 3, "Framed diamond ring, big diamond fading")
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


def _ladder(index: int, count: int, offset: float) -> None:
    bracket(XT4, "C2", "+", offset, CORONA_LADDER.split(";")[0], ISO_LADDER,
            CORONA_LADDER, CORONA_FRAMES,
            "Corona ladder %d of %d, inner edge to outer streamers" % (index, count))

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


POSTLUDE = LINES
POSTLUDE_FRAMES = dict(FRAMES)

# --------------------------------------------------------------------------
# Output, one file per totality duration
# --------------------------------------------------------------------------
# The X-T4 file keeps every shared line - the comments, the voice prompts and the
# camera sync - because it is the one that runs.  The 800D file carries only its own
# commands: Solar Eclipse Workbench loads a single script at a time, so if the pair
# is ever run again it is on a second machine, and doubling the voice prompts across
# two laptops standing next to each other would be worse than having none.


def assemble(target_s: float) -> tuple:
    """The whole script for a totality of `target_s`, and what it costs in frames."""
    middle, middle_frames = capture(totality_block, target_s)
    lines = PREAMBLE + middle + POSTLUDE
    frames = {cam: PREAMBLE_FRAMES[cam] + middle_frames[cam] + POSTLUDE_FRAMES[cam]
              for cam in middle_frames}
    return lines, frames


def site_script() -> str:
    """The file the planned site's own totality calls for."""
    fits = [d for d in DURATIONS if d <= totality] or [DURATIONS[0]]
    return "20260812_production_%03ds.txt" % max(fits)


def banner(target_s: float, ladders: int) -> list:
    """The first thing you see on opening the file, because picking wrong is silent."""
    return [
        "# " + "#" * 74,
        "# THIS SCRIPT FILLS %.0f SECONDS OF TOTALITY." % target_s,
        "#",
        "# Load it only if Solar Eclipse Workbench reports totality of at least %.0f s"
        % target_s,
        "# where you are standing.  Of the scripts in this directory, take the LARGEST",
        "# that does not exceed the totality you have.",
        "#",
        "#   too small  you lose a few seconds of corona at the end of totality",
        "#   too large  the last ladder is still exposing after C3, filter off",
        "#",
        "# %d corona ladder%s.  The planned site has %.0f s, which makes"
        % (ladders, "" if ladders == 1 else "s", totality),
        "# %s the one to load unless you have moved." % site_script(),
        "# " + "#" * 74,
        "#",
    ]


written = []
for target in DURATIONS:
    offsets = ladder_offsets(target)
    if not offsets:
        sys.stderr.write("skipped %3.0fs: no room for a corona ladder between the bursts\n"
                         % target)
        continue
    lines, frames = assemble(target)
    production = banner(target, len(offsets)) + [text for owner, text in lines
                                                 if owner in (None, XT4)]
    path = OUTPUT_DIR / ("20260812_production_%03ds.txt" % target)
    path.write_text("\n".join(production) + "\n")
    written.append((target, path, len(offsets), frames[XT4]))

print("\n%-34s  %8s  %7s  %6s" % ("script", "totality", "ladders", "frames"))
for target, path, ladders, frames in written:
    print("%-34s  %6.0f s  %7d  %6d" % (path.name, target, ladders, frames))
print("\nAt %s totality is %.0f s, so the one to load is %s."
      % (SITE, totality, site_script()))

_stale = OUTPUT_DIR / "20260812_production.txt"
if _stale.exists():
    print("\n%s carries no duration in its name and is not regenerated here.\n"
          "Remove it: a file in this directory that does not say what it fits is\n"
          "the one you will load by mistake in the dark." % _stale)

# The parked body's half is written once, for the planned site's own duration.
_site_lines, _site_frames = assemble(totality)
parked = [text for owner, text in _site_lines if owner == EOS]
FRAMES = _site_frames
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
