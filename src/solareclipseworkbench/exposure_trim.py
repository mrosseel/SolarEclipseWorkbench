"""One correction applied to every exposure the script asks for.

The exposures in a generated script come from tables evaluated at the sun's
altitude, through an extinction coefficient that is a guess: 0.15 in clear air,
0.40 in haze, and 0.25 assumed.  Being wrong about it does not shift one frame,
it shifts all of them by the same amount - and the error grows towards the
horizon, which for a sunset eclipse is exactly where totality is.

Nothing on the day can measure that coefficient.  What an observer can do is
look at the sky, or at live view, and say "everything is a stop under".  This is
where that judgement goes: one number, in stops, applied to every exposure as it
is sent to the camera.

Applied at the moment a frame is taken rather than when the script is loaded, so
it can be changed while the eclipse runs - haze is not constant, and the value
that was right at first contact may not be right an hour later.

Positive is brighter.  +1 doubles every exposure time, -1 halves it.
"""

from __future__ import annotations

import logging
import threading

#: Half a stop, the step the observer dials in.  Finer than this is beyond what
#: anyone can judge by eye, and coarser leaves the correction visibly short.
STEP_STOPS = 0.5

#: The most that can be dialled in either direction.  Past three stops the
#: exposures were not approximately right to begin with and the script wants
#: regenerating with a better coefficient, not correcting.
LIMIT_STOPS = 3.0

_lock = threading.Lock()
_stops = 0.0


def stops() -> float:
    """The correction currently applied, in stops.  Positive is brighter."""
    return _stops


def set_stops(value: float) -> float:
    """Set the correction, clamped to +/- LIMIT_STOPS.  Returns what was set."""
    global _stops
    clamped = max(-LIMIT_STOPS, min(LIMIT_STOPS, float(value)))
    with _lock:
        changed = clamped != _stops
        _stops = clamped
    if changed:
        logging.info('Exposure trim %s - every scheduled exposure %s',
                     describe(), 'lengthened' if clamped > 0 else
                     ('shortened' if clamped < 0 else 'left alone'))
    return clamped


def describe() -> str:
    """The correction as it should appear on screen."""
    if not _stops:
        return "0 EV"
    return f"{_stops:+.1f} EV"


def apply_seconds(seconds: float) -> float:
    """Correct an exposure given in seconds."""
    if not _stops:
        return seconds
    return seconds * (2.0 ** _stops)


def apply_microseconds(microseconds: int) -> int:
    """Correct an exposure given in the SDK's microseconds.

    Rounded to the nearest microsecond; the camera snaps it to its own scale
    afterwards, so nothing here needs to know what that scale is.
    """
    if not _stops:
        return microseconds
    return int(round(microseconds * (2.0 ** _stops)))
