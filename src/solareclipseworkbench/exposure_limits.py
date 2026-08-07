"""What the body will actually take, and what a script's exposure becomes.

Two jobs, one path.

The camera cannot be asked.  The X-T4's SDK module does not implement
CapShutterSpeed - it answers with an empty list - so the limits have to be
stated here rather than read from the body.  They are an override, not a
discovery, and they are per-body: a different camera wants different numbers.

And every exposure the schedule asks for goes through :func:`resolve` on its
way to the camera: the observer's haze trim, then the body's own limits.  The
script validator calls the same function, so what a pre-flight check reports
is what the run will do - not a second implementation that agrees until it
does not.

The fast end is not the mechanical shutter's 1/8000.  Reaching past it needs
the shutter type on MS+ES (or ES) so the body switches to the electronic
shutter of its own accord; on MS alone the write is refused.  That is a dial
on the camera, not a setting here, which is why the pre-flight report names
the speeds that depend on it.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, replace
from typing import Optional

from solareclipseworkbench import exposure_trim

logger = logging.getLogger(__name__)


#: The speeds an X-T4 actually accepts between 1/32000 and 6", measured with
#: scripts/exposure_probe.py: each one written, read back, and confirmed to be
#: what the body ended up on.  54 of the 65 the SDK offers in that range.
#:
#: This is not the SDK's table.  That table carries every model's scale at
#: once, so a half-stop series (1/45, 1/90, 1/180, 1/350, 1/750, 1/1500,
#: 1/3000, 1/6000, 1/12000, 1/24000, 1/1.5) sits interleaved with this body's
#: third-stop one - and every one of those is refused with 0x2003.  Snapping
#: to "nearest on the table" can land on a speed the body will not take, which
#: leaves the frame at the previous exposure and reports success.
#:
#: The six fastest need the shutter type on MS+ES or ES; on MS the body stops
#: at 1/8000 and refuses them.  Re-probe after changing that dial.
XT4_SPEEDS_S = (
    0.00003125, 0.0000390625, 0.00005, 0.0000625, 0.000078125, 0.0001,
    0.000125, 0.00015625, 0.0002, 0.00025, 0.0003125, 0.0004, 0.0005,
    0.000625, 0.0008, 0.001, 0.00125, 0.0015625, 0.002, 0.0025, 0.003125,
    0.004, 0.005, 0.00625, 0.008, 0.01, 0.0125, 0.016666667, 0.02, 0.025,
    0.033333333, 0.04, 0.05, 0.066666667, 0.076923077, 0.1, 0.125,
    0.166666667, 0.2, 0.25, 0.333333333, 0.4, 0.5, 0.625, 0.769230769,
    1.0, 1.3, 1.6, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0,
)


@dataclass(frozen=True)
class Limits:
    """The exposures and sensitivities one body will accept."""

    #: Fastest shutter, in seconds.  Measured, not assumed: 1/32000 with the
    #: shutter type on MS+ES.  On MS alone the body stops at 1/8000 and
    #: refuses everything past it, so put this back to 0.000125 if that dial
    #: moves - scripts/exposure_probe.py will say which is true.
    fastest_s: float = 1.0 / 32000
    #: Past this the mechanical shutter is required, so a speed between here
    #: and ``fastest_s`` is only usable with MS+ES or ES selected.
    mechanical_fastest_s: float = 1.0 / 8000
    #: Slowest shutter the schedule may ask for.  Not the body's limit - the
    #: body goes far longer - but the longest a frame can be while the sun is
    #: moving and the mount is tracking at solar rate.
    slowest_s: float = 6.0
    #: Sensitivity range.  Above the cap the frames are noise, and a haze
    #: trim must not walk into it.
    iso_min: int = 100
    iso_max: int = 1600
    #: Every speed this body takes, in seconds.  Anything computed - a trim, a
    #: bracket rung - is put onto one of these rather than onto the nearest
    #: entry in the SDK's all-models table.
    accepted_speeds: tuple = XT4_SPEEDS_S


#: The limits in force.  Module level so the validator, the shooting path and
#: the interface all read one answer.
_limits = Limits()
_lock = threading.Lock()

#: Puts a computed exposure back onto a speed the body actually owns.  The
#: camera module registers it, because the scale lives in the SDK's table; a
#: run without that module validates against the limits alone.
_snapper = None


def set_snapper(fn) -> None:
    """Register seconds -> seconds snapping onto the body's own scale."""
    global _snapper
    _snapper = fn


def limits() -> Limits:
    return _limits


def set_limits(**fields) -> Limits:
    """Override one or more limits.  Returns what is now in force."""
    global _limits
    with _lock:
        _limits = replace(_limits, **fields)
    logger.info("Exposure limits: %s", describe())
    return _limits


def describe() -> str:
    lim = _limits
    text = (f"{format_speed(lim.fastest_s)}-{format_speed(lim.slowest_s)}, "
            f"ISO {lim.iso_min}-{lim.iso_max}")
    # The trim rides on every exposure and is remembered between runs, so a
    # correction dialled in days ago is otherwise invisible until the frames
    # come back wrong.
    stops = exposure_trim.stops()
    if stops:
        text += f", haze trim {exposure_trim.describe()}"
    return text


def nearest_accepted(seconds: float) -> Optional[float]:
    """The closest speed this body will actually take, or None if unknown.

    Only inside the range the scale was measured over.  Past either end there
    is no measurement, so the limits are the authority: raising ``fastest_s``
    after re-probing on MS+ES has to be enough on its own, or the override
    would be a scale nobody remembered to extend.
    """
    scale = _limits.accepted_speeds
    if not scale or not seconds or seconds <= 0:
        return None
    if seconds < min(scale) or seconds > max(scale):
        return None
    return min(scale, key=lambda s: abs(math.log(s / seconds)))


def clamp_iso(value: int) -> int:
    """An ISO put inside the configured range."""
    return max(_limits.iso_min, min(_limits.iso_max, int(value)))


#: Where the override lives between runs.  A plain ini section rather than a
#: constant in the source: the numbers are per-body and belong to whoever is
#: shooting, not to the program.
SETTINGS_SECTION = "exposure_limits"


def load_from_settings(settings) -> Limits:
    """Read the override from a QSettings, keeping the defaults it omits."""
    fields = {}
    for name, cast in (("fastest_s", float), ("mechanical_fastest_s", float),
                       ("slowest_s", float), ("iso_min", int), ("iso_max", int)):
        raw = settings.value(f"{SETTINGS_SECTION}/{name}", None)
        if raw in (None, ""):
            continue
        try:
            fields[name] = cast(raw)
        except (TypeError, ValueError):
            logger.warning("Ignoring unreadable exposure limit %s=%r", name, raw)
    return set_limits(**fields) if fields else _limits


def save_to_settings(settings) -> None:
    for name, value in vars(_limits).items():
        settings.setValue(f"{SETTINGS_SECTION}/{name}", value)


# ------------------------------------------------------------------ formatting


def parse_speed(text) -> Optional[float]:
    """Seconds from a script's shutter speed: "1/2000", "0.5", "2", '1/2"'."""
    if text is None:
        return None
    clean = str(text).strip().rstrip('"').strip()
    if not clean:
        return None
    try:
        if "/" in clean:
            top, bottom = clean.split("/", 1)
            top, bottom = float(top), float(bottom)
            return top / bottom if bottom else None
        return float(clean)
    except (ValueError, ZeroDivisionError):
        return None


def format_speed(seconds: float) -> str:
    """Seconds as a photographer writes them: 1/2000, 0.5", 6"."""
    if seconds is None or seconds <= 0:
        return "?"
    if seconds >= 1.0:
        return f'{seconds:g}"'
    denominator = 1.0 / seconds
    nearest = round(denominator)
    # Within a per-cent of a whole denominator it is that speed written out.
    if nearest and abs(denominator - nearest) / nearest < 0.01:
        return f"1/{nearest}"
    return f'{seconds:.4g}"'


# --------------------------------------------------------------------- resolve


@dataclass(frozen=True)
class Resolved:
    """One exposure, as asked for and as it will actually be taken."""

    requested_speed_s: Optional[float]
    requested_iso: Optional[int]
    speed_s: Optional[float]
    iso: Optional[int]
    #: What happened, in the order it happened.
    notes: tuple = ()

    @property
    def requested_text(self) -> str:
        speed = format_speed(self.requested_speed_s)
        return f"{speed} ISO {self.requested_iso}" if self.requested_iso else speed

    @property
    def applied_text(self) -> str:
        speed = format_speed(self.speed_s)
        return f"{speed} ISO {self.iso}" if self.iso else speed

    @property
    def changed(self) -> bool:
        return bool(self.notes)

    @property
    def note_text(self) -> str:
        return "; ".join(self.notes)

    @property
    def needs_electronic_shutter(self) -> bool:
        return (self.speed_s is not None
                and self.speed_s < _limits.mechanical_fastest_s)


def resolve(speed, iso=None, trim_stops: Optional[float] = None) -> Resolved:
    """What the camera will be asked for, given a script line.

    The haze trim first, then the limits: the trim is the observer correcting
    the exposure, and the limits are what the body can do about it.  Both are
    reported, so a frame that comes out at the cap says so rather than looking
    like the one the script asked for.
    """
    lim = _limits
    stops = exposure_trim.stops() if trim_stops is None else trim_stops

    requested_s = parse_speed(speed)
    requested_iso = None
    if iso not in (None, "", "-"):
        try:
            requested_iso = int(str(iso).strip())
        except ValueError:
            return Resolved(requested_s, None, requested_s, None,
                            (f"ISO {iso!r} is not a number",))

    notes = []
    seconds, sensitivity = requested_s, requested_iso

    if seconds is None:
        return Resolved(None, requested_iso, None, requested_iso,
                        (f"{speed!r} is not a shutter speed",))

    if stops:
        seconds = seconds * (2.0 ** stops)
        notes.append(f"{stops:+.1f} EV trim")

    if seconds > lim.slowest_s:
        notes.append(f"capped at {format_speed(lim.slowest_s)}")
        seconds = lim.slowest_s
    elif seconds < lim.fastest_s:
        notes.append(f"capped at {format_speed(lim.fastest_s)}")
        seconds = lim.fastest_s

    seconds = nearest_accepted(seconds) or seconds

    if _snapper is not None:
        snapped = _snapper(seconds)
        if snapped and abs(snapped - seconds) / seconds > 0.01:
            notes.append(f"on the body's scale: {format_speed(snapped)}")
        if snapped:
            seconds = snapped

    if sensitivity is not None:
        if sensitivity > lim.iso_max:
            notes.append(f"ISO capped at {lim.iso_max}")
            sensitivity = lim.iso_max
        elif sensitivity < lim.iso_min:
            notes.append(f"ISO raised to {lim.iso_min}")
            sensitivity = lim.iso_min

    return Resolved(requested_s, requested_iso, seconds, sensitivity,
                    tuple(notes))


# ------------------------------------------------------------------ validation


@dataclass(frozen=True)
class Row:
    """One exposure a script asks for, resolved."""

    line_no: int
    command: str
    camera: str
    when: str
    resolved: "Resolved"
    #: A bracket's rungs, each resolved, when the line carries a ladder.
    rungs: tuple = ()

    @property
    def ok(self) -> bool:
        return self.resolved.speed_s is not None and not self.resolved.changed \
            and not any(r.changed for r in self.rungs)


#: Which comma-separated field holds the exposure and the ISO, per command.
#: Taken from scripts.parse_command so the validator reads what the scheduler
#: reads rather than a second guess at the format.
_FIELDS = {
    "take_picture": (5, 7), "TAKEPIC": (5, 7),
    "take_burst": (5, 7), "TAKEBST": (5, 7),
    "take_bracket": (5, 7), "TAKEBKT": (5, 7),
    "take_hdr": (5, 7),
}

#: Where a bracket line keeps its ladder, when it spells one out.
_BRACKET_FIELD = {"take_bracket": 8}


def validate_script(path, trim_stops: Optional[float] = None) -> list:
    """Every exposure in a script, resolved exactly as the run will resolve it.

    Reads the raw script rather than the converted one: this runs before the
    reference moments are known, and the exposure fields do not move.
    """
    import csv

    rows = []
    with open(path) as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                parts = next(csv.reader([line], skipinitialspace=True))
            except (StopIteration, csv.Error):
                continue
            if not parts:
                continue
            command = parts[0].strip()
            fields = _FIELDS.get(command)
            if fields is None:
                continue
            speed_at, iso_at = fields
            if len(parts) <= max(speed_at, iso_at):
                continue

            speed, iso = parts[speed_at].strip(), parts[iso_at].strip()
            camera = parts[4].strip() if len(parts) > 4 else ""
            when = " ".join(p.strip() for p in parts[1:4])

            rungs = ()
            ladder_at = _BRACKET_FIELD.get(command)
            if ladder_at is not None and len(parts) > ladder_at:
                ladder = parts[ladder_at].strip()
                if ";" in ladder:
                    rungs = tuple(resolve(rung.strip(), iso, trim_stops)
                                  for rung in ladder.split(";") if rung.strip())

            rows.append(Row(line_no, command, camera, when,
                            resolve(speed, iso, trim_stops), rungs))
    return rows


def report(rows: list) -> str:
    """The validation as a person reads it: what was asked, what will happen."""
    lim = _limits
    out = [f"Exposure limits in force: {describe()}",
           f"(past {format_speed(lim.mechanical_fastest_s)} the body needs its "
           f"shutter type on MS+ES or ES)",
           ""]
    changed = [r for r in rows if not r.ok]
    out.append(f"{len(rows)} exposure(s) checked, {len(changed)} will be changed")
    if changed:
        out.append("")
        width = max(len(r.resolved.requested_text) for r in changed)
        for row in changed:
            out.append(f"  line {row.line_no:<5} {row.resolved.requested_text:<{width}}"
                       f"  ->  {row.resolved.applied_text}   {row.resolved.note_text}")
            for n, rung in enumerate(row.rungs, start=1):
                if rung.changed:
                    out.append(f"        rung {n}  {rung.requested_text}"
                               f"  ->  {rung.applied_text}   {rung.note_text}")
    electronic = [r for r in rows if r.resolved.needs_electronic_shutter]
    if electronic:
        out.append("")
        out.append(f"{len(electronic)} exposure(s) are faster than "
                   f"{format_speed(lim.mechanical_fastest_s)} and need MS+ES or ES:")
        for row in electronic[:10]:
            out.append(f"  line {row.line_no:<5} {row.resolved.applied_text}")
        if len(electronic) > 10:
            out.append(f"  ... and {len(electronic) - 10} more")
    return "\n".join(out)


def _main(argv=None) -> int:
    """Check a script from the command line: ``python -m ...exposure_limits FILE``."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Resolve every exposure in an eclipse script through the "
                    "limits and the haze trim the run will use.")
    parser.add_argument("script")
    parser.add_argument("--trim", type=float, default=0.0,
                        help="haze trim in stops, as the observer would dial it")
    parser.add_argument("--fastest", type=float, help="fastest shutter, seconds")
    parser.add_argument("--slowest", type=float, help="slowest shutter, seconds")
    parser.add_argument("--iso-max", type=int)
    args = parser.parse_args(argv)

    overrides = {}
    if args.fastest:
        overrides["fastest_s"] = args.fastest
    if args.slowest:
        overrides["slowest_s"] = args.slowest
    if args.iso_max:
        overrides["iso_max"] = args.iso_max
    if overrides:
        set_limits(**overrides)

    # Imported for its side effect: it registers the snapper, so the report
    # resolves onto the body's real scale exactly as a run would.
    try:
        from solareclipseworkbench import fuji_camera  # noqa: F401
    except Exception:
        logger.debug("No Fuji SDK here; validating against the limits alone")

    print(report(validate_script(args.script, trim_stops=args.trim)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
