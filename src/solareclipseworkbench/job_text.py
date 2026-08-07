"""What a scheduled line actually does, in words, at the exposure it will use.

The jobs table showed the command as it is written in the script.  That is
exact and unreadable at a glance: ``take_bracket("Fuji Fujifilm X-T4",
1/8000, -, 100, +/- 2)`` is eight fields of punctuation to say "a bracket at
1/8000, ISO 100, two stops either side".  During totality nobody parses
commas.

Two things this does that reading the script cannot:

*It applies the trim.*  The exposure dialled into the EV box is applied to
every scheduled frame, so the speed in the script is not the speed the body
will be set to.  The text here shows what will actually be used - and shows
the clamp when a trimmed speed runs past what the shutter can do.

*It drops the times.*  Generated descriptions carry a time - "Focus check,
uneclipsed disc @ 17:15:56" - worked out when the file was written, from the
contact times of the site it was generated for.  Load it anywhere else, or
with a simulation offset, and that time is simply wrong while the columns
beside it are right.  Two clocks disagreeing in one row is worse than one
clock, so the baked one goes.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: The time a generator baked into a description, with the anchor form the
#: relay lines use ("@ BEADS_C2_END-5.2 s") alongside the plain clock one.
_BAKED_TIME = re.compile(
    r"\s*@\s*(?:[A-Z][A-Z0-9_]*\s*[-+]\s*[\d.]+\s*s|\d{1,2}:\d{2}(?::\d{2})?)")

#: What each contact prompt is announcing, so a prompt reads as speech rather
#: than as a constant.
_MOMENTS = {
    "C1": "first contact", "C2": "second contact", "C3": "third contact",
    "C4": "fourth contact", "MAX": "maximum eclipse",
    "BEADS_C2_START": "the beads starting", "BEADS_C2_END": "totality",
    "BEADS_C3_START": "the beads at third contact",
    "BEADS_C3_END": "the beads ending", "SUNRISE": "sunrise", "SUNSET": "sunset",
}


def strip_baked_time(text: str) -> str:
    """A description without the clock reading the generator wrote into it.

    Never raises; a description that cannot be cleaned is shown as it came.
    """
    if not text:
        return text
    try:
        return _BAKED_TIME.sub("", str(text)).strip().strip(",").strip()
    except Exception:
        logger.debug("Could not clean %r", text, exc_info=True)
        return text


def _speed(value, trim=True) -> str:
    """A shutter speed as it will be set, trim included.

    Deliberately routed through the shooting path's own conversion, so what
    is shown is what will be sent - including the clamp when a trimmed speed
    runs past the fastest the shutter can do.
    """
    text = str(value).strip()
    if not text or text == "-":
        return ""
    if not trim:
        return text
    try:
        from solareclipseworkbench import exposure_trim
        from solareclipseworkbench.camera import _speed_to_seconds, _usable_speed
        if not exposure_trim.stops():
            return text
        seconds = _speed_to_seconds(text)
        if seconds is None:
            return text
        return _usable_speed(exposure_trim.apply_seconds(seconds))
    except Exception:            # never let a label break a run
        logger.debug("Could not trim %r for display", value, exc_info=True)
        return text


def _iso(value) -> str:
    text = str(value).strip()
    return "" if not text or text == "-" else f"ISO {text}"


def _seconds(value) -> str:
    try:
        s = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{s:.0f} s" if s >= 10 else f"{s:.1f} s"


def _prompt(name) -> str:
    """A voice prompt as the sentence it will speak."""
    key = str(name).strip().upper()
    moment = _MOMENTS.get(key)
    if moment:
        return f"Say: {moment}"
    m = re.match(r"^([A-Z0-9_]+?)_(IN|PLUS)_(\d+)_(SECONDS?|MINUTES?)$", key)
    if m:
        moment = _MOMENTS.get(m.group(1), m.group(1))
        unit = "min" if m.group(4).startswith("MINUTE") else "s"
        if m.group(2) == "PLUS":
            return f"Say: {m.group(3)} {unit} after {moment}"
        return f"Say: {m.group(3)} {unit} to {moment}"
    return f"Say: {str(name).replace('_', ' ').lower()}"


def describe(command: str, fields: list, trim: bool = True) -> str:
    """One short line for a scheduled command.

    `fields` are the script's own arguments after the timing columns, which
    is what both the file and a built job can supply.

    Never raises.  This runs once per row while the jobs table is built, and
    a label is not worth a table: during a run that table is how anyone sees
    what is about to happen, so a command with arguments nobody anticipated
    degrades to its own name rather than taking the view down with it.
    """
    try:
        return _describe(command, fields, trim)
    except Exception:
        logger.debug("Could not describe %r %r", command, fields, exc_info=True)
        return (command or "").replace("_", " ").strip().capitalize()


def _describe(command: str, fields: list, trim: bool = True) -> str:
    command = (command or "").strip()
    f = [str(x).strip() for x in fields]

    def at(i):
        return f[i] if i < len(f) else ""

    if command == "take_picture":
        parts = ["Photo", _speed(at(1), trim), _iso(at(3))]
    elif command == "take_bracket":
        # camera, speed, aperture, iso, steps - where steps is either a
        # symmetric "+/- 2" or an explicit ladder "1/8000;1/2000;...".  A
        # ladder names its own rungs, so the base speed says nothing and the
        # rungs are summarised by their ends rather than listed.
        steps = at(4)
        if ";" in steps:
            rungs = [r for r in (x.strip() for x in steps.split(";")) if r]
            ends = f"{_speed(rungs[0], trim)} to {_speed(rungs[-1], trim)}" \
                if rungs else ""
            parts = ["Bracket", f"{len(rungs)} frames" if rungs else "",
                     ends, _iso(at(3))]
        else:
            steps = steps.replace("+/-", "±").replace(" ", "")
            parts = ["Bracket", _speed(at(1), trim), _iso(at(3)),
                     f"{steps} EV" if steps else ""]
    elif command == "take_burst":
        parts = ["Burst", _seconds(at(4)), _speed(at(1), trim), _iso(at(3))]
    elif command == "take_hdr":
        parts = ["HDR", _speed(at(1), trim), _iso(at(3)),
                 f"{at(4)} stops" if at(4) else ""]
    elif command == "relay_burst":
        parts = ["Relay burst", _seconds(at(0))]
    elif command == "relay_shoot":
        parts = ["Relay shot"]
    elif command == "relay_arm":
        parts = ["Arm relay"]
    elif command == "relay_release":
        parts = ["Release relay"]
    elif command == "relay_bulb":
        parts = ["Relay bulb", _seconds(at(0))]
    elif command == "sync_cameras":
        parts = ["Sync cameras"]
    elif command == "sync_camera_time":
        parts = ["Sync camera clock"]
    elif command == "voice_prompt":
        parts = [_prompt(at(0))]
    elif command == "mount_goto_sun":
        parts = ["Mount to the sun"]
    elif command == "mount_track_sun":
        parts = ["Track at solar rate"]
    elif command == "mount_tracking":
        parts = ["Tracking " + ("on" if at(0).lower() in
                                ("on", "1", "true", "yes") else "off")]
    elif command == "mount_park":
        parts = ["Park the mount"]
    elif command == "mount_unpark":
        parts = ["Unpark the mount"]
    elif command == "execute_command":
        parts = ["Run " + " ".join(f)]
    else:
        parts = [command.replace("_", " ").capitalize()]

    return " ".join(p for p in parts if p)


def describe_job(job, trim: bool = True) -> str:
    """The same line, from a built APScheduler job rather than a script line.

    Never raises, for the same reason describe() does not.
    """
    try:
        return _describe_job(job, trim)
    except Exception:
        logger.debug("Could not describe a job", exc_info=True)
        return ""


def _describe_job(job, trim: bool = True) -> str:
    name = getattr(getattr(job, "func", None), "__name__", "") or ""
    args = list(getattr(job, "args", None) or ())
    settings = next((a for a in args if hasattr(a, "shutter_speed")), None)
    extra = [a for a in args[1:] if a is not settings]

    if settings is not None:
        fields = [getattr(settings, "camera_name", ""),
                  getattr(settings, "shutter_speed", ""),
                  getattr(settings, "aperture", ""),
                  getattr(settings, "iso", "")]
        fields += [str(x) for x in extra]
    else:
        fields = [str(a) for a in args
                  if not hasattr(a, "close") and not hasattr(a, "capture")]
    return describe(name, fields, trim)
