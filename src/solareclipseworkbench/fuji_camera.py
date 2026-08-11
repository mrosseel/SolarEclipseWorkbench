"""Fuji X Series camera adapter for Solar Eclipse Workbench.

Bridges the fujixsdk Python bindings into the workbench's BaseCamera
abstraction (defined in ``camera.py``) so Fuji cameras appear alongside
gphoto2 cameras.  Fuji bodies are driven through Fujifilm's native Shooting
SDK rather than gphoto2/libgphoto2, which does not support tethered control of
the X series.

Detection (:func:`detect_fuji_cameras`) runs before gphoto2 claims the USB
device; :func:`find_fuji_sdk_path` locates the redistributable SDK libraries.
"""

from __future__ import annotations

import logging
import math
import os
import ctypes
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, NamedTuple, Optional

from . import exposure_limits, exposure_trim, hardware_problems
from .camera import BaseCamera, CameraError
from .hardware_registry import HARDWARE, register_hardware

# Lazy import — fujixsdk may not be installed / the SDK libs may be absent.
try:
    import fujixsdk
    from fujixsdk import (
        Camera as SDKCamera,
        CameraIssue,
        EclipseShooter,
        ensure_ld_library_path,
        validate_for_eclipse,
    )
    from fujixsdk._constants import (
        AE_MODE_NAMES,
        ITEM_MEDIASLOT1 as SDK_ITEM_MEDIASLOT1,
        ITEM_MEDIASLOT2 as SDK_ITEM_MEDIASLOT2,
        MEDIASTATUS_CANNOT_WRITE,
        MEDIASTATUS_NAMES,
        AE_OFF,
        FOCUS_MODE_NAMES,
        ISO_100,
        SHUTTER_SPEED_NAMES,
        SDK_FOCUS_MANUAL,
    )
    from fujixsdk import recovery as sdk_recovery
    from fujixsdk._constants import PRIORITY_CAMERA, PRIORITY_PC
    from fujixsdk._errors import BusyError, CommunicationError
    # The buffer belongs to the body, so its size and the fraction of it
    # that may fill live with the SDK wrapper rather than being restated here.
    from fujixsdk.camera import BUFFER_SLOTS, DRAIN_AT
    FUJIXSDK_AVAILABLE = True
    FUJIXSDK_IMPORT_ERROR = None
except ImportError as _exc:
    FUJIXSDK_AVAILABLE = False
    FUJIXSDK_IMPORT_ERROR = str(_exc)

    class BusyError(Exception):
        """Stand-in so the retry helpers below still import without the SDK."""

    class CommunicationError(Exception):
        """Stand-in for the lost-session error, likewise."""

    BUFFER_SLOTS = 32
    DRAIN_AT = 0.75


# ======================================================================
# Relay-driven shooting
#
# With the drive dial on CH — which a total eclipse needs for its Baily's
# beads bursts — the SDK cannot fire the shutter at all (0x1008).  A relay
# on the release jack can, at the body's native 15 fps, so when one is
# connected the relay does the firing and the SDK is left to do what it is
# good at: exposure and draining.  Measured on the bench, 1 August 2026.
# ======================================================================

# How many frames one held contact may ask for.
#
# The limit is a number of frames, not a length of time: it is the body's own
# buffer and how fast the card drains it.  Measured 4 August, a held burst with
# nothing draining took 61 frames in 2.15 s and every one reached the card.  60
# is that, less one, and it is the figure to revisit if the card or the image
# quality changes - not the seconds below.
#
# It used to be expressed as 1.9 seconds, which tied the cap to a frame rate
# that was never checked.  Against bead windows of 3.25 s and 4.05 s that
# photographed less than half of them however well the contacts were solved:
# the cap decided what was on the card, not the eclipse.
MAX_BURST_FRAMES = 60

#: The fastest speed this body will actually take.  Its mechanical shutter stops
#: at 1/8000; the SDK's table goes to 1/180000 because it covers every model and
#: the electronic shutter, and nothing in the table says which apply here.
FASTEST_SHUTTER_S = 1.0 / 8000
FASTEST_SHUTTER_NAME = '1/8000"' 

# Frames per second under a held contact, measured on this body on 4 August:
#
#     CL, menu set to 8 fps    32 frames in 4.15 s   7.7 fps
#     CH, menu as found        61 frames in 2.15 s  28.4 fps
#
# CL is what the eclipse scripts are built for: at 28 fps a bead window wants
# more than a hundred frames and the body hits its own buffer partway through,
# so the burst slows exactly where the diamond ring is.
#
# The dial cannot be read back - GetDriveMode answers 0x0004 wherever it is
# pointing - so this number cannot be checked against the camera.  It is the
# figure a frame count is converted with, and it is only right if the dial is on
# CL and CL LOW SPEED BURST is set to 8 fps.  Both are pre-flight eye checks.
RELAY_FPS = 7.7

#: The old name, kept because a burst is still a burst at whatever the dial says.
CH_FPS = RELAY_FPS

#: The longest hold, derived rather than chosen: the frames the body will take,
#: at the rate it takes them.  Changing the drive speed moves this by itself,
#: which is the point - the two were free to disagree while both were constants,
#: and a cap that quietly contradicts the rate is how a burst ends up covering
#: half of what it was asked to.
MAX_BURST_S = MAX_BURST_FRAMES / RELAY_FPS


# Closing and opening the relay costs this much on top of whatever hold is asked
# for - +0.15s at every duration from 0.2s to 1.9s, measured 3 August.  A burst
# that ignores it under-counts the frames it is about to queue by two.
RELAY_HOLD_OVERHEAD_S = 0.15

# Contact closure per frame.  Settled on the bench, 3 August: nothing below
# 80 ms fires reliably, so the width cannot be tuned down to stop a fast rung
# firing twice.  The doubling is inherent to CH and is handled by draining, not
# avoided - see DRAIN_AT.  This number is measured and closed; do not sweep it
# again looking for a value that gives one frame per tap, there isn't one.
# 0.03 s, not 0.05: on CL a longer tap sometimes fires twice - eight frames
# for seven rungs - and a ladder wants exactly one frame per rung.  Proven on
# the body over ten consecutive seven-rung ladders, 70 actuations: every rung
# fired, every speed landed, never a short round.  The two failures are not
# equal, which is why this needed proving rather than assuming - an extra
# frame costs a buffer slot, a tap too short to fire costs a corona exposure
# that cannot be retaken.
TAP_S = 0.03

# Shortest useful gap between taps; long exposures extend it (see _tap_gap).
TAP_GAP_S = 0.35

# The session survives a drain only once the camera has genuinely stopped.
SETTLE_BEFORE_DRAIN_S = 1.0

# Frames appear in the buffer count as they are written, not as they are shot,
# and a burst takes far longer to finish than a tap: measured 3 August, a 2.05s
# burst was still arriving 2.5s after the contact opened (13 counted at 0.0s, 20
# at 1.0s, 29 at 2.5s), while a single tap is complete inside 0.75s.  One drain a
# second after a burst therefore leaves eight or nine frames behind.  Draining
# repeats until a round comes back empty, with a shorter settle after the first.
DRAIN_ROUNDS = 4
SETTLE_BETWEEN_DRAINS_S = 0.6


# For about a second after a frame the body refuses exposure changes with
# 0x1006 while it writes to the card.  The busy clears by itself.
BUSY_BACKOFF_S = 0.3

# Waiting out a busy body is worth it only while there is still time to use the
# result, so every retry loop is bounded by a deadline rather than by a count of
# attempts: a frame at the previous exposure still records the corona, a frame
# taken after the moment has passed records nothing.  Totality is not repeatable
# and no setting is worth a missed contact.
EXPOSURE_BUDGET_S = 1.5      # the whole of `configure`, every setting together

# Below this there is no point starting a catch-up write: a single exposure
# write on a free body measures ~0.3 s, so less budget than that is a skip.
MIN_CATCHUP_BUDGET_S = 0.25
# The most a pre-burst speed verification may spend.  A read is ~20 ms and a
# rewrite ~200 ms, so 0.6 s allows the read and two rewrites and still leaves
# the relay's 0.8 s arm budget - and the 1.2 s of slack behind it - intact.
# Nothing waits on this that is worth a late burst.
VERIFY_SPEED_BUDGET_S = 0.6
# One speed change between two taps.  Measured on the body, 40 writes: median
# 178 ms, 90th 206 ms, worst 256 ms, and up to 490 ms with taps interleaved.
# The old 0.3 s against a 0.3 s backoff was one attempt, and it was under the
# cost of a healthy write - so a ladder could fail even with a willing body.
BRACKET_STEP_BUDGET_S = 0.8

# A frame is not finished when `capture` returns: the relay contact lasts 80ms
# and the shutter stays open for the exposure, after which the body writes.  Two
# long singles in a row therefore collide - measured 3 August, a 4" frame asked
# for straight after a 2" one was refused for the whole 1.5s budget and taken at
# 2" instead, which looks entirely normal until the card is read.  The budget is
# extended to cover the frame already in flight, and this is how long the body
# needs after the shutter closes before it will accept the next setting.
FRAME_WRITE_S = 1.5

# However long the body claims to need, the wait is capped here: a script that
# asks for something impossible must not swallow the rest of totality.
MAX_EXPOSURE_WAIT_S = 8.0

# A lost session reports itself once per setting per frame, so rebuilding on
# every one would spend totality reconnecting.  One attempt per this long.
RECOVERY_INTERVAL_S = 10.0


def _through_busy(action, what: str, deadline: float, recover=None):
    """Run a camera call, waiting out the 0x1006 raised while the body writes.

    ``deadline`` is a :func:`time.monotonic` instant past which the call is
    abandoned, so the wait can never eat into the next scheduled frame.  Only
    busy is retried: sitting out the backoffs cannot make an unsupported ISO
    supported, and the seconds spent are seconds of totality.
    """
    while True:
        try:
            return action()
        except CommunicationError:
            # The session is gone, not busy - 0x2001.  Retrying it changes
            # nothing: on 3 August a session lost at C2 left every ladder for the
            # rest of totality writing to a dead handle, taking frames at
            # whatever speed the body was last on and reporting success.  One
            # rebuild, then try again; if that fails the caller reports it.
            if recover is None or not recover():
                raise
            logging.warning('%s: the camera session was rebuilt; retrying', what)
            return action()
        except BusyError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # The exact code and what the body says is blocking it, because
                # this is the line that gets read the next day: 0x1006 while
                # live view runs is a different fault from 0x1006 with a frame
                # still transferring, and "busy" alone cannot tell them apart.
                logging.warning('%s: body still busy at its deadline (%s%s); '
                                'moving on rather than delaying the next frame',
                                what, exc, _busy_detail(recover))
                raise
            logging.debug('%s: body busy (%s), %.1fs of budget left',
                          what, exc, remaining)
            time.sleep(min(BUSY_BACKOFF_S, remaining))


def _busy_detail(recover) -> str:
    """What the body says is blocking it, for the log.  Never raises.

    Diagnosis only - nothing is cleared here.  A frame is in flight and the
    remedies cost time this path does not have.
    """
    camera = getattr(recover, '__self__', None)
    sdk_cam = getattr(camera, '_sdk_cam', None)
    if sdk_cam is None:
        return ''
    try:
        return ', blocked by %s' % sdk_recovery.read_blockers(sdk_cam).describe()
    except Exception:
        return ''


def _retry_busy(action, what: str, deadline: float, recover=None) -> bool:
    """As :func:`_through_busy`, but reports the failure instead of raising."""
    try:
        _through_busy(action, what, deadline, recover)
        return True
    except CommunicationError as exc:
        # One line, not a traceback per rung: a lost session produces one of
        # these for every setting of every frame that follows, and eight
        # tracebacks a bracket buries the one message that matters.
        logging.error('%s failed - the camera session is gone (%s)', what, exc)
        return False
    except Exception:
        logging.exception('%s failed', what)
        return False


class _RelayShooter:
    """Relay-driven stand-in for EclipseShooter.

    Exposes the same two methods ``take_burst`` and ``take_bracket`` call, so
    neither needs to know which mechanism is firing the shutter.
    """

    def __init__(self, camera: FujiCamera):
        self.camera = camera

    def burst_no_download(self, count: int, min_interval_ms: int = 0) -> int:
        """Hold the release long enough for ``count`` frames at the CH rate."""
        seconds = min(count / CH_FPS, MAX_BURST_S)

        # Singles no longer drain after every frame, so the queue reaching here
        # can be most of the way full — and a burst at the cap adds 30 of 32
        # slots.  Nothing checks the buffer once the contact is closed, so the
        # room has to be made first or the beads fill it and the body stops.
        self.camera.ensure_room_for(
            math.ceil(CH_FPS * (seconds + RELAY_HOLD_OVERHEAD_S)))

        with self.camera.relay.pressed():
            time.sleep(seconds)
        # `pressed()` leaves S1 closed when the caller pre-armed, and draining
        # with S1 still held drops the session for good (0x2001).
        self.camera.relay.release_all()
        # One round: the tail of a burst keeps arriving for two and a half
        # seconds and chasing it costs six, which at C2 buys nothing.  Slots are
        # what is needed, and whatever is left behind is cleared by the next
        # bracket's own check or by `ensure_room_for` before the next burst.
        self.camera.drain(rounds=1)
        return int(seconds * CH_FPS)

    def bracket_no_download(self, speeds: list, iso=None, aperture=None) -> int:
        """One tap per speed, the speed set over USB between taps.

        ISO and aperture are deliberately ignored: ``configure`` has already
        applied them, and re-applying them here — as the SDK shooter does —
        would silently undo the ISO the caller asked for.
        """
        relay = self.camera.relay
        taken = 0
        # S1 is deliberately NOT held across the ladder.  Holding it keeps the
        # CL drive running, so the body is permanently mid-exposure and
        # refuses every setting change: measured 7 August, six rungs with S1
        # held landed 1 speed of 6 and fired 32 frames, while the same six
        # rungs with the contact left open landed 6 of 6 and fired exactly 6.
        # The rehearsal that morning lost all eight corona ladders to this.
        relay.release_all()
        try:
            for speed in speeds:
                # Budgeted against the tap gap below: a speed that will not go on
                # in time must not push the whole bracket off its schedule.
                # Written straight to the SDK rather than through `configure`, so
                # the speed `configure` thinks is on the body has to be corrected
                # here — otherwise the next single at the bracket's last rung
                # would be skipped as already applied and shot at the wrong speed.
                self.camera._applied_speed = speed
                if not _retry_busy(lambda s=speed: self.camera._sdk.set_shutter_speed(s),
                                   f'{self.camera.name}: set shutter speed {speed}',
                                   time.monotonic() + BRACKET_STEP_BUDGET_S,
                                   self.camera.recover_session):
                    self.camera._applied_speed = None
                    # Fire anyway: a frame at the previous speed beats no frame at
                    # all, and there is no second chance at a contact.  But the
                    # frame will look perfectly normal until it is reviewed, so the
                    # failure has to reach the user — quietly, through the
                    # indicator, never through a dialog while the script runs.
                    hardware_problems.report(
                        self.camera.name,
                        'Bracket frames were taken at the wrong shutter speed',
                        detail=f'{speed} could not be set; the frame was taken anyway',
                        severity='warning',
                    )
                relay.shoot(pulse=TAP_S)
                taken += 1
                # A slow frame must finish before the next speed is sent.
                time.sleep(max(TAP_GAP_S, speed / 1_000_000 + 0.3))
                relay = self._keep_buffer_clear(relay)
        finally:
            relay.release_all()
        # The last rung is still being written, and whatever `configure` is
        # asked for next has to wait for it like any other frame.
        self.camera._note_frame_fired()
        # One round, not four.  A full drain chases the tail of the ladder, and
        # the tail keeps arriving for seconds after the last rung: measured
        # 8 August, seven rungs cost about 6.5 s and the drain behind them took
        # the ladder to 16 s against a 10.6 s pitch.  Every ladder therefore
        # overran into the next job, and the run lost four of its eight corona
        # ladders and all four gap singles to "camera was still busy".
        #
        # Slots are what the next job needs, not an empty queue, and
        # `_keep_buffer_clear` above already drains mid-ladder when the queue
        # is genuinely filling.  Same reasoning as the C2 burst, same evening.
        self.camera.drain(rounds=1)
        return taken

    def _keep_buffer_clear(self, relay):
        """Empty the transfer queue mid-bracket if it is filling.

        With the drive dial on CH — which the beads bursts require — one tap
        fires as many frames as fit inside the contact, so a fast rung costs two
        slots rather than one.  A 13-rung bracket measured 28 frames against 32
        slots on 2 August, and a wider bracket would have filled the buffer and
        stopped the body dead mid-sequence, recoverable only by pulling the
        battery.  The dial cannot be moved over USB (the body refuses
        SetDriveMode for as long as it is asked), so the frames cannot be
        prevented — only cleared before they accumulate.

        Draining needs the contacts open: doing it with S1 still held drops the
        USB session for good (0x2001).  The half-press is therefore dropped and
        retaken around the drain, which costs one settle, and only when the queue
        is actually filling.
        """
        if not self.camera.buffer_is_filling():
            return relay

        logging.info('%s: draining mid-bracket', self.camera.name)
        relay.release_all()
        # One round: this runs inside a ladder that has to finish within its
        # pitch, and the rungs still to come need slots rather than an empty
        # queue.  A full drain here put the ladder over 16 s against a 10.6 s
        # pitch on 8 August.
        self.camera.drain(rounds=1)
        # No re-press: the ladder no longer holds S1 between rungs, and
        # re-closing it here would restart the very drive that made the body
        # refuse every speed.
        return relay


# ======================================================================
# Shutter speed / aperture / ISO mapping (workbench string -> SDK int)
# ======================================================================

def _build_speed_reverse_map() -> dict[str, int]:
    """Build a reverse lookup from human-readable speed string to SDK constant.

    The workbench passes shutter speeds as strings like "1/2000".
    SHUTTER_SPEED_NAMES maps int->str with trailing quotes (e.g. '1/2000"').
    We strip the quote and also handle bare values.
    """
    if not FUJIXSDK_AVAILABLE:
        return {}
    rmap: dict[str, int] = {}
    for val, name in SHUTTER_SPEED_NAMES.items():
        if val == 0:
            continue
        clean = name.rstrip('"').strip()
        rmap[clean] = val
        # Also map without spaces
        rmap[clean.replace(" ", "")] = val
    return rmap


_SPEED_REVERSE: dict[str, int] = {}
_SPEED_BY_SECONDS: list[tuple[float, int]] = []


def _get_speed_reverse() -> dict[str, int]:
    global _SPEED_REVERSE
    if not _SPEED_REVERSE and FUJIXSDK_AVAILABLE:
        _SPEED_REVERSE = _build_speed_reverse_map()
    return _SPEED_REVERSE


def _speed_name_seconds(name: str) -> Optional[float]:
    """Seconds for one SHUTTER_SPEED_NAMES entry, or None if it is not a duration.

    Covers the three forms the table uses: '1/2000"', '1.6"' and '4min'.
    """
    clean = name.rstrip('"').strip()
    try:
        if clean.endswith("min"):
            return float(clean[:-3]) * 60.0
        if "/" in clean:
            num, _, den = clean.partition("/")
            return float(num) / float(den)
        return float(clean)
    except (ValueError, ZeroDivisionError):
        return None


def _get_speeds_by_seconds() -> list[tuple[float, int]]:
    global _SPEED_BY_SECONDS
    if not _SPEED_BY_SECONDS and FUJIXSDK_AVAILABLE:
        _SPEED_BY_SECONDS = sorted(
            (secs, val)
            for val, name in SHUTTER_SPEED_NAMES.items()
            if val > 0 and (secs := _speed_name_seconds(name)) is not None
        )
    return _SPEED_BY_SECONDS


# The camera's speeds are 1/3 EV apart, so anything closer than 1/6 EV to a rung
# is that rung written a different way.  Beyond that it is a value the schedule
# asked for and the body does not have, which the caller has to hear about.
_SPEED_MATCH_TOLERANCE = 2.0 ** (1.0 / 6.0)


def _parse_shutter_speed(speed_str: str) -> Optional[int]:
    """Map a workbench shutter speed string to a fujixsdk constant.

    The same exposure has several spellings — the table calls half a second
    '1/2"', a schedule of decimal-second corona frames calls it '0.5' — so an
    unmatched string falls back to matching on duration.
    """
    rmap = _get_speed_reverse()
    clean = speed_str.strip().rstrip('"')
    val = rmap.get(clean)
    if val is not None:
        # An exact name still has to be a speed this body owns.  The table is
        # every model's: "1/32000" and "1/750" both spell correctly and are
        # both refused here, so a script naming one directly must not take the
        # fast path around the scale and get a frame at the wrong exposure.
        seconds = _speed_name_seconds(clean)
        if seconds is None:
            return val
        lim = exposure_limits.limits()
        if lim.fastest_s <= seconds <= lim.slowest_s and _on_accepted_scale(seconds):
            return val

    wanted = _speed_name_seconds(clean)
    if wanted is None or wanted <= 0:
        return None
    lim = exposure_limits.limits()
    if wanted < lim.fastest_s:
        wanted = lim.fastest_s
    elif wanted > lim.slowest_s:
        wanted = lim.slowest_s

    grid = _get_speeds_by_seconds()
    if not grid:
        return None

    # The SDK's table runs to 1/180000, covering every model; matching "nearest
    # on the scale" walks off the end into speeds this body will refuse with
    # 0x2003, and the frame is then taken at whatever was last set.  Clamping to
    # the configured limits is the honest response: a trim that cannot be fully
    # applied is applied as far as it goes.
    lim = exposure_limits.limits()
    if wanted < lim.fastest_s:
        logging.warning(
            'Shutter speed %s is faster than %s; using that instead',
            speed_str, exposure_limits.format_speed(lim.fastest_s))
        wanted = lim.fastest_s
    elif wanted > lim.slowest_s:
        logging.warning(
            'Shutter speed %s is longer than the %s cap; using that instead',
            speed_str, exposure_limits.format_speed(lim.slowest_s))
        wanted = lim.slowest_s

    grid = _accepted_grid()
    secs, val = min(grid, key=lambda pair: abs(math.log(pair[0] / wanted)))
    ratio = max(secs, wanted) / min(secs, wanted)
    if ratio > _SPEED_MATCH_TOLERANCE:
        return None
    if ratio > 1.0001:
        logging.warning(
            'Shutter speed %s is not on this camera\'s scale; using %s instead',
            speed_str, SHUTTER_SPEED_NAMES.get(val, val),
        )
    return val


def _distinct(speeds) -> list:
    """Drop rungs that have become copies of one already in the ladder.

    Clamping at the fast end turns everything past the limit into the same
    speed, so a bracket around a body already at 1/8000 came out as thirteen
    frames of which seven were identical - and reported "took all 13 frames".

    Duplicates are not free during totality: each is a slot in the transfer
    queue, a card write, and about a second of a hundred that cannot be had
    again.  Better six useful frames and a line saying why than thirteen of
    which half say the same thing.
    """
    kept, seen, asked = [], set(), 0
    for speed in speeds:
        asked += 1
        if speed in seen:
            continue
        seen.add(speed)
        kept.append(speed)
    if asked != len(kept):
        logging.warning(
            'Bracket: %d of %d rungs are past the fastest speed this body has '
            'and would have been identical frames; taking %d',
            asked - len(kept), asked, len(kept))
    return kept


def snap_to_scale(microseconds: int) -> int:
    """Put a computed exposure back onto a speed the body actually has.

    A trim multiplies microseconds directly, so -0.5 EV turns 1/8000 into
    88 us, which is not a shutter speed: the SDK takes it, the body answers
    0x2003, and the frame is taken at whatever was set before.

    Clamped at both ends, through the same limits the validator reports, so a
    trim that cannot be fully applied is applied as far as it goes.
    """
    if microseconds <= 0:
        return microseconds
    lim = exposure_limits.limits()
    wanted = microseconds / 1_000_000.0
    if wanted < lim.fastest_s:
        # Debug, not warning: a bracket that runs off the end produces one of
        # these per rung, and seven identical warnings say less than the one
        # line _distinct writes about how many rungs were lost.
        logging.debug('Exposure %.0f us is faster than %s; clamping',
                      microseconds, exposure_limits.format_speed(lim.fastest_s))
        wanted = lim.fastest_s
    elif wanted > lim.slowest_s:
        logging.debug('Exposure %.0f us is longer than %s; clamping',
                      microseconds, exposure_limits.format_speed(lim.slowest_s))
        wanted = lim.slowest_s

    grid = _accepted_grid()
    if not grid:
        return microseconds
    secs, val = min(grid, key=lambda pair: abs(math.log(pair[0] / wanted)))
    return val


def _accepted_grid() -> list:
    """(seconds, sdk value) for the speeds this body actually takes.

    The SDK's table carries every model's scale at once, so a half-stop series
    sits interleaved with this body's third-stop one; measured on the X-T4,
    every one of those half-stop speeds is refused with 0x2003.  Snapping to
    the whole table can therefore land on a speed the body will not take, and
    a refused write leaves the frame at the previous exposure.
    """
    lim = exposure_limits.limits()
    grid = [pair for pair in _get_speeds_by_seconds()
            if lim.fastest_s <= pair[0] <= lim.slowest_s]
    scale = lim.accepted_speeds
    if not scale:
        return grid
    low, high = min(scale), max(scale)
    kept = []
    for secs, value in grid:
        if secs < low or secs > high:
            # No measurement out here; the limits already bounded the grid.
            kept.append((secs, value))
            continue
        nearest = min(scale, key=lambda s: abs(math.log(s / secs)))
        # Within a per-cent it is that speed, written to more decimal places.
        if abs(math.log(nearest / secs)) < 0.01:
            kept.append((secs, value))
    return kept or grid


def _on_accepted_scale(seconds: float) -> bool:
    """Whether this exposure is one the body has been measured to take."""
    scale = exposure_limits.limits().accepted_speeds
    if not scale or seconds <= 0:
        return True
    # Outside the measured range there is nothing to judge against, so the
    # limits decide; inside it, the scale is the whole point.
    if seconds < min(scale) or seconds > max(scale):
        return True
    nearest = min(scale, key=lambda s: abs(math.log(s / seconds)))
    return abs(math.log(nearest / seconds)) < 0.01


def _snap_seconds(seconds: float) -> Optional[float]:
    """Nearest speed on the body's scale, in seconds.  For the validator."""
    grid = _accepted_grid()
    if not grid or seconds <= 0:
        return None
    return min(grid, key=lambda pair: abs(math.log(pair[0] / seconds)))[0]


exposure_limits.set_snapper(_snap_seconds)


def _parse_aperture(aperture_str: str) -> Optional[int]:
    """Map workbench aperture string (e.g. "5.6") to SDK int (f-number * 100)."""
    try:
        f_num = float(str(aperture_str))
        return int(round(f_num * 100))
    except (ValueError, TypeError):
        return None


def _parse_iso(iso_val) -> Optional[int]:
    """Map workbench ISO value (int or string) to SDK int."""
    try:
        return int(iso_val)
    except (ValueError, TypeError):
        return None


# ======================================================================
# GPhoto-compatible stubs
# ======================================================================

class _FujiWidgetStub:
    """Mimics gphoto2 widget's get_value()/set_value()/get_type() interface."""

    def __init__(self, name: str, value: Any):
        self._name = name
        self._value = value

    def get_value(self):
        return self._value

    def set_value(self, v: Any):
        self._value = v

    def get_type(self):
        try:
            import gphoto2 as gp
            return gp.GP_WIDGET_TEXT
        except ImportError:
            return 0


class _FujiConfigStub:
    """Mimics gphoto2 config's get_child_by_name() pattern.

    Maps widget names to real SDK values so existing helper functions
    (get_battery_level, get_focus_mode, etc.) work without modification.
    """

    def __init__(self, fuji_camera: FujiCamera):
        self._cam = fuji_camera

    def get_child_by_name(self, name: str) -> _FujiWidgetStub:
        name_lower = name.lower()
        sdk_cam = self._cam._sdk_cam

        if name_lower == 'batterylevel':
            # The body reports a coarse state, not a percentage.  This used to
            # print it as one - "3%" for a half-full battery - which is worse
            # than saying nothing.  And the X-T4's SDK module does not
            # implement the battery API at all: GetDeviceInfoEx does not list
            # 0x4055, so it answers 0x1013 whatever is passed.  Check the body
            # by eye before an eclipse; there is no reading it from here.
            try:
                return _FujiWidgetStub(name, sdk_cam.get_battery_info().describe())
            except Exception:
                return _FujiWidgetStub(name, "Unknown")

        if name_lower == 'focusmode':
            try:
                fm = sdk_cam.get_focus_mode()
                fm_name = FOCUS_MODE_NAMES.get(fm, f"0x{fm:04X}")
                # Map to workbench-expected values
                if fm == SDK_FOCUS_MANUAL:
                    return _FujiWidgetStub(name, "Manual")
                return _FujiWidgetStub(name, fm_name)
            except Exception:
                return _FujiWidgetStub(name, "Manual")

        if name_lower in ('autoexposuremodedial', 'expprogram'):
            try:
                ae = sdk_cam.get_ae_mode()
                ae_name = AE_MODE_NAMES.get(ae, f"0x{ae:04X}")
                return _FujiWidgetStub(name, ae_name)
            except Exception:
                return _FujiWidgetStub(name, "Manual")

        if name_lower == 'shutterspeed':
            try:
                speed, _ = sdk_cam.get_shutter_speed()
                speed_name = SHUTTER_SPEED_NAMES.get(speed, str(speed))
                return _FujiWidgetStub(name, speed_name)
            except Exception:
                return _FujiWidgetStub(name, "")

        if name_lower == 'iso':
            try:
                iso = sdk_cam.get_iso()
                return _FujiWidgetStub(name, str(iso))
            except Exception:
                return _FujiWidgetStub(name, "")

        if name_lower in ('aperture', 'f-number'):
            try:
                ap = sdk_cam.get_aperture()
                return _FujiWidgetStub(name, f"{ap / 100:.1f}")
            except Exception:
                return _FujiWidgetStub(name, "")

        if name_lower in ('datetime', 'datetimeutc', 'd034'):
            return _FujiWidgetStub(name, time.strftime('%Y-%m-%d %H:%M:%S'))

        return _FujiWidgetStub(name, '')


class _FujiStorageEntry:
    """Mimics gphoto2 storage info entry with freekbytes/capacitykbytes."""

    def __init__(self, free_kb: float, capacity_kb: float):
        self.freekbytes = free_kb
        self.capacitykbytes = capacity_kb


# ======================================================================
# FujiCamera adapter
# ======================================================================

class FujiCamera(BaseCamera):
    """Adapter wrapping a fujixsdk.Camera into the workbench's BaseCamera interface.

    Exposure settings are applied through :meth:`configure`; single shots through
    :meth:`capture`; bursts and brackets through the :attr:`shooter`
    (:class:`fujixsdk.EclipseShooter`).  Gphoto-style ``get_config`` /
    ``get_storageinfo`` stubs let the workbench's vendor-agnostic helper
    functions (battery level, focus mode, free space, ...) work unmodified.
    """

    vendor = 'Fuji'
    connection_type = 'Fuji SDK'

    def __init__(self, sdk_cam: SDKCamera, name: str, sdk_path: str, device_name: str = "ENUM:0"):
        super().__init__(name=name)
        self._sdk_cam = sdk_cam
        self._sdk_path = sdk_path
        self._device_name = device_name
        self._shooter: Optional[EclipseShooter] = None
        # The same lock, under both names.  BaseCamera makes _usb_lock and this
        # class used to make a second one of its own, so the two names were two
        # objects: every method here serialised on one while live view - which
        # takes the camera's _usb_lock - serialised on the other.  Neither
        # excluded the other, and the SDK is not thread-safe.
        #
        # That is a frame worker inside read_image while a scheduled command is
        # inside set_shutter_speed, and it is the only condition under which the
        # USB link has ever stalled: twice, both times with live view running,
        # each time ending in 0x2001 with the session gone.
        #
        # An RLock, so a public method calling another still works.
        self._lock = self._usb_lock
        self._connected = True
        # The ISO this session last wrote successfully; see configure().
        self._applied_iso: Optional[int] = None
        self._applied_speed: Optional[int] = None
        # Settings a busy body refused, latest value per key; they ride along
        # with the next configure and with apply_pending().
        self._pending: dict = {}
        # When the frame currently in flight should be written and the body will
        # take settings again.  See FRAME_WRITE_S.
        self._frame_busy_until: float = 0.0

    def connect(self) -> None:
        self._connected = True

    @property
    def max_relay_hold_s(self) -> float:
        """Longest the release may be held before the transfer queue overruns.

        Published so a bare ``relay_burst`` in a script — which knows nothing
        about the body on the other end of the cable — can be held to the same
        limit the Fuji burst path already respects.
        """
        return MAX_BURST_S

    def disconnect(self) -> None:
        if HARDWARE.get('sdk_camera') is self:
            register_hardware('sdk_camera', None)
        try:
            self._sdk_cam.close()
        except Exception:
            pass
        self._connected = False

    def _remember(self, **kwargs: Any) -> None:
        """Keep the last requested exposure, to restore after a reconnect."""
        self._requested = {k: v for k, v in kwargs.items() if v is not None}

    def apply_pending(self, budget_s: Optional[float] = None) -> bool:
        """Apply the settings earlier configures could not put on the body.

        Called by live view as it lets go of the camera, and by the relay
        commands just before contacts close - there with a hard ``budget_s``,
        since a catch-up that delays the burst it serves is no rescue.  Below
        MIN_CATCHUP_BUDGET_S nothing is attempted.

        Returns True when nothing is left pending.
        """
        pending = dict(getattr(self, '_pending', None) or {})
        if not pending:
            # Nothing was blocked - but a write that reported success is not
            # proof the body took it.  On the 11 August rehearsal both beads
            # loads wrote 1/6400, raised nothing, and the body exposed 1/8000:
            # the load frame and the 164 burst frames behind it, a third of a
            # stop dark.  So the speed is read back here, in the slack before
            # the contacts close, and put right if it drifted.  Silent by
            # design: this runs seconds before totality, where a raised error
            # helps nobody and a lost frame cannot be retaken.
            self._verify_speed(budget_s)
            return True
        if budget_s is not None and budget_s < MIN_CATCHUP_BUDGET_S:
            logging.info("%s: %d setting(s) still blocked, and %.2fs of budget "
                         "is not enough to try", self.name, len(pending), budget_s)
            return False
        logging.info("%s: applying the setting(s) a busy body blocked earlier: %s",
                     self.name, pending)
        try:
            self.configure(budget_s=budget_s, **pending)
        except CameraError:
            logging.warning("%s: some blocked settings still did not go on",
                            self.name, exc_info=True)
        return not getattr(self, '_pending', None)

    def _verify_speed(self, budget_s: Optional[float] = None) -> None:
        """Check the body is on the speed last asked for, and put it back if not.

        Never raises and never spends more than it is given.  Both are
        deliberate: this runs in the seconds before a contact burst, where the
        only thing that matters is that the next frames are exposed correctly.

        The read costs about 20 ms and the rewrite about 200 ms, against the
        0.8 s the relay's arm allows and the 1.2 s of schedule slack behind it.
        """
        wanted = self._applied_speed
        if wanted is None:
            return
        deadline = time.monotonic() + min(
            budget_s if budget_s is not None else VERIFY_SPEED_BUDGET_S,
            VERIFY_SPEED_BUDGET_S)
        try:
            with self._lock:
                got, _bulb = self._sdk.get_shutter_speed()
                if got == wanted:
                    return
                logging.warning(
                    '%s: the body is on %s but %s was set; putting it back',
                    self.name, SHUTTER_SPEED_NAMES.get(got, got),
                    SHUTTER_SPEED_NAMES.get(wanted, wanted))
                while time.monotonic() < deadline:
                    try:
                        self._sdk.set_shutter_speed(wanted)
                    except Exception:
                        time.sleep(0.05)
                        continue
                    got, _bulb = self._sdk.get_shutter_speed()
                    if got == wanted:
                        logging.info('%s: speed restored to %s', self.name,
                                     SHUTTER_SPEED_NAMES.get(wanted, wanted))
                        return
                logging.warning('%s: could not put the speed back inside %.2fs; '
                                'the frames run at what the body has',
                                self.name, deadline - time.monotonic())
        except Exception:
            # A verification that throws would be worse than no verification.
            logging.debug('%s: speed verification failed', self.name, exc_info=True)

    def configure(self, budget_s: Optional[float] = None, **kwargs: Any) -> None:
        """Apply camera settings via SDK.

        Accepts keyword arguments:
            shutter_speed: str (e.g. "1/2000")
            aperture: str (e.g. "5.6")
            iso: int or str (e.g. 100)

        Raises:
            CameraError: if any requested setting could not be applied, naming
                every one that failed.

        A setting that silently fails to apply is worse than one that fails
        loudly: the next frame is then taken at the previous exposure and looks
        perfectly normal until the images are reviewed.  Every failure is
        collected here — one bad value must not stop the others being tried —
        and reported together.

        A failure is also *cached*: the setting stays in ``self._pending``
        (latest value per key) and rides along with the next configure, so a
        write blocked by live view's 0x1006 heals itself on the first frame
        after the window closes instead of leaving the body stale.  A key this
        call asks for supersedes the cached one.

        The settings share one ``EXPOSURE_BUDGET_S`` deadline between them, so a
        body that stays busy costs the schedule that much once, not once per
        setting.  Whatever has not gone on by then is reported as a failure and
        the frame is taken regardless.  ``budget_s`` overrides that deadline as
        a hard cap — the pre-burst catch-up path may never run long, whatever
        frame the body is still flushing.
        """
        self._remember(**kwargs)
        pending = getattr(self, '_pending', None)
        if pending is None:
            pending = self._pending = {}
        wanted = {**pending, **{k: v for k, v in kwargs.items() if v is not None}}
        failures: list = []

        def _apply(name: str, parse, setter, raw, deadline: float) -> bool:
            value = parse(raw)
            if value is None:
                failures.append(f"{name}={raw!r} is not a value this camera understands")
                return False
            try:
                _through_busy(lambda: setter(value), f'{self.name}: set {name}',
                              deadline, self.recover_session)
                return True
            except Exception as exc:
                failures.append(f"{name}={raw!r} rejected by the camera ({exc})")
                return False

        def _apply_iso(raw, deadline: float) -> bool:
            """Write the ISO only when it is not the one already on the body.

            A script holds one ISO for a whole phase, so writing it on every
            frame is dozens of USB round-trips that cannot help.

            This used to say the body refuses ``set_iso`` unless the transfer
            queue is empty.  Measured 7 August and it does not: eighteen
            writes with eighteen frames pending all landed, 46-239 ms, the
            same as with an empty queue.  The skip is worth keeping for the
            round trips it saves, but it is not protecting against a refusal
            that happens - and a split ISO through totality is therefore
            affordable, which that claim had ruled out.  The
            remembered value is cleared whenever a write fails or the session is
            rebuilt, so a skip never outlives its evidence.
            """
            value = _parse_iso(raw)
            if value is None:
                failures.append(f"ISO={raw!r} is not a value this camera understands")
                return False
            capped = exposure_limits.clamp_iso(value)
            if capped != value:
                logging.info('%s: ISO %s outside the configured range; using %s',
                             self.name, value, capped)
                value = capped
            if value == self._applied_iso:
                logging.debug('%s: ISO already %s, not writing it again', self.name, raw)
                return True
            try:
                _through_busy(lambda: self._sdk.set_iso(value),
                              f'{self.name}: set ISO {raw}', deadline, self.recover_session)
                self._applied_iso = value
                return True
            except Exception as exc:
                self._applied_iso = None
                failures.append(f"ISO={raw!r} rejected by the camera ({exc})")
                return False

        def _apply_speed(raw, deadline: float) -> bool:
            """Write the shutter speed only when it is not the one already set.

            The body refuses the write with 0x1006 for the second or so it spends
            flushing the previous frame to the card, so a run of singles at one
            exposure spent about 1.7s per frame waiting to write a value the body
            already had — measured at 2.0s a frame against 0.3s when the write is
            skipped.  Between brackets that is the difference between filling a
            gap with seven frames and filling it with forty.

            Kept honest the same way the ISO is: the remembered value is dropped
            whenever a write fails, whenever the bracket path writes a speed of
            its own, and whenever the session is rebuilt.
            """
            value = _parse_shutter_speed(str(raw))
            if value is None:
                failures.append(f"shutter speed={raw!r} is not a value this camera understands")
                return False
            if value == self._applied_speed:
                logging.debug('%s: shutter speed already %s, not writing it again',
                              self.name, raw)
                return True
            try:
                _through_busy(lambda: self._sdk.set_shutter_speed(value),
                              f'{self.name}: set shutter speed {raw}', deadline,
                              self.recover_session)
                self._applied_speed = value
                return True
            except Exception as exc:
                self._applied_speed = None
                failures.append(f"shutter speed={raw!r} rejected by the camera ({exc})")
                return False

        with self._lock:
            # Started after the lock, so a queued caller inherits a full budget
            # rather than one already spent waiting its turn.
            started = time.monotonic()
            if budget_s is not None:
                # A hard cap from the caller, frame-in-flight or not: the
                # pre-burst catch-up must never delay the burst it serves.
                deadline = started + budget_s
            else:
                # Long enough for the body to finish the frame already in
                # flight, since it refuses every setting until it has.
                deadline = min(max(started + EXPOSURE_BUDGET_S, self._frame_busy_until),
                               started + MAX_EXPOSURE_WAIT_S)

            if wanted.get('iso') is not None:
                if _apply_iso(wanted['iso'], deadline):
                    pending.pop('iso', None)
                else:
                    pending['iso'] = wanted['iso']

            # A telescope has no electronic aperture, so a script says "-" and
            # the setting is skipped rather than failing every single frame.
            if wanted.get('aperture') not in (None, '', '-'):
                if _apply('aperture', _parse_aperture, self._sdk.set_aperture,
                          wanted['aperture'], deadline):
                    pending.pop('aperture', None)
                else:
                    pending['aperture'] = wanted['aperture']

            if wanted.get('shutter_speed') is not None:
                if _apply_speed(wanted['shutter_speed'], deadline):
                    pending.pop('shutter_speed', None)
                else:
                    pending['shutter_speed'] = wanted['shutter_speed']

            elapsed = time.monotonic() - started
            allowed = deadline - started

        # Measured against the deadline actually in force, not the base budget:
        # after a long exposure the deadline is deliberately longer, and warning
        # about a wait that was planned for would be noise.
        if elapsed > allowed:
            logging.warning('%s: applying settings took %.1fs (allowed %.1fs)',
                            self.name, elapsed, allowed)

        if failures:
            raise CameraError(
                f"{self.name}: could not apply " + "; ".join(failures)
            )

    @property
    def relay(self):
        """The relay trigger driving this body, or None if none is connected."""
        return HARDWARE.get('relay')

    def drain(self, rounds: int = None, settle_s: float = None,
              budget_s: float = None) -> int:
        """Discard the queued PC transfers once shooting has stopped.

        Every frame taken with an SDK session open holds one of 32 buffer
        slots until it is drained, and a full buffer stops the body dead —
        recoverable only by pulling the battery.  The images themselves are
        already on the card; only the transfer nobody asked for is discarded.
        Callers must have released the relay first: draining while the camera
        is still shooting drops the USB session for good.

        Repeats until a round finds nothing, because the body reports frames as
        it writes them: after a burst the count is still climbing two seconds
        later, and a single drain leaves the tail of the burst queued.  A bracket
        or a single settles inside the first round, so this costs them one extra
        capacity read and one short settle.

        ``rounds=1`` takes whatever has arrived and leaves the rest, for callers
        that only need slots back rather than an empty queue.  Chasing the tail
        of a burst costs six seconds, and at C2 that is worth more than a clean
        buffer nobody is waiting on.

        ``settle_s`` and ``budget_s`` are for the one caller draining while the
        shutter is still going: a burst.  The settle exists because frames take
        up to 0.75 s to appear in the count, which matters when shooting has
        stopped and the tail is still arriving — mid-burst the queue is never
        empty and the wait is pure cost.  The budget bounds how long the pass
        may run, so the burst can check its own deadline between drains rather
        than being held open by one.  Defaults keep the after-the-fact
        behaviour every other caller wants.
        """
        drained = 0
        settle = SETTLE_BEFORE_DRAIN_S if settle_s is None else settle_s
        for _ in range(max(1, DRAIN_ROUNDS if rounds is None else rounds)):
            if settle > 0:
                time.sleep(settle)
            try:
                this_round = self._sdk.drain_buffer(budget_s=budget_s)
            except Exception:
                logging.exception('%s: drain failed; shooting is unaffected', self.name)
                break
            drained += this_round
            if this_round == 0:
                break
            if settle_s is None:
                settle = SETTLE_BETWEEN_DRAINS_S
        return drained

    def buffer_is_filling(self) -> bool:
        """True when the transfer queue is close enough to full to want clearing.

        A queue that cannot be read counts as fine: a buffer reading is not worth
        losing a frame over, and every path here drains unconditionally somewhere
        further on.

        Measured against BUFFER_SLOTS rather than the total the SDK returns: that
        total sits three above the count while frames are being written, so
        ``captured >= total * DRAIN_AT`` would have fired at 13 frames as readily
        as at 25.
        """
        try:
            captured, _ = self._sdk.get_buffer_capacity()
        except Exception:
            logging.debug('%s: buffer unreadable', self.name, exc_info=True)
            return False
        return captured >= BUFFER_SLOTS * DRAIN_AT

    def ensure_ready(self, priority: int = None, allow_shot: bool = True,
                     why: str = "") -> bool:
        """Clear whatever is stopping the body and take the given priority.

        The generic form of what used to happen only on session open.  Live
        view left running by a crashed process, a half press that never
        released, frames still in the buffer - each is a different remedy, and
        the body is asked which applies before any of them is tried.  See
        fujixsdk.recovery.

        Worth calling anywhere "camera is busy" would otherwise be reported to
        the user as a dead camera: bringing the body up, starting live view,
        or after a scheduled command was refused.

        allow_shot=False forbids the last-resort flush shot, which actuates the
        shutter.  Pass it whenever a frame firing unbidden would be worse than
        failing - during an eclipse, that is always.
        """
        if priority is None:
            priority = PRIORITY_CAMERA
        return sdk_recovery.unblock(self._sdk, priority, allow_shot=allow_shot,
                                    why=why or self.name)

    def recover_session(self) -> bool:
        """Rebuild the USB session after it has been lost, and say whether it worked.

        0x2001 means the handle is dead: every call on it fails from then on.
        Without this a session lost at second contact took the whole of totality
        with it - eleven ladders writing to a dead handle, each frame taken at
        whatever speed the body was last set to, each reporting success.

        Rate limited, because the failure arrives once per setting per frame and
        rebuilding on every one of them would spend totality reconnecting.  The
        exposure the caller last asked for is put back by `_reconnect`, which
        clears what it believed was applied.
        """
        now = time.monotonic()
        if now - getattr(self, '_last_recovery', 0.0) < RECOVERY_INTERVAL_S:
            return False
        self._last_recovery = now

        logging.warning('%s: the camera session was lost; rebuilding it', self.name)
        if not self._reconnect():
            hardware_problems.report(
                self.name,
                'The camera connection was lost and could not be rebuilt',
                detail='frames from here on will be at whatever the body is set to',
            )
            return False
        hardware_problems.report(
            self.name, 'The camera connection was lost and rebuilt',
            detail='check the frames around this moment for the wrong exposure',
            severity='warning',
        )
        return True

    def _note_frame_fired(self) -> None:
        """Record when the body should be free again after the frame just fired.

        The exposure is whatever was last applied; when that is unknown — no
        `configure` since the session was rebuilt — only the write time is
        assumed, which is the same as the behaviour before any of this.
        """
        exposure_s = (self._applied_speed or 0) / 1_000_000.0
        self._frame_busy_until = time.monotonic() + exposure_s + FRAME_WRITE_S

    def ensure_room_for(self, frames: int) -> int:
        """Clear the queue unless it can already hold ``frames`` more.

        Used before shooting that cannot stop to check — a relay burst holds the
        contact closed and the body free-runs, so the only chance to make room is
        before it starts.  A queue that cannot be read is drained rather than
        trusted: a wasted second beats a buffer that fills mid-burst, which stops
        the body until the battery is pulled.

        The free slots are worked out from the count alone, never from the total
        the SDK reports beside it.  That total is not the buffer size: while
        frames are being written it tracks three above the count — 13/16, 18/21,
        20/23, 23/26, 27/30 through one burst on 3 August — and only settles at
        32 once the body is idle.  Subtracting one from the other would read as
        three free slots however empty the buffer really was.
        """
        try:
            captured, _ = self._sdk.get_buffer_capacity()
        except Exception:
            logging.warning('%s: buffer unreadable before a burst; draining to be '
                            'sure there is room', self.name, exc_info=True)
            return self.drain()

        if BUFFER_SLOTS - captured >= frames:
            return 0
        logging.info('%s: draining before a burst — %d slot(s) free, %d needed',
                     self.name, BUFFER_SLOTS - captured, frames)
        return self.drain()

    def drain_if_filling(self) -> int:
        """Drain, but only when the queue has actually filled up.

        A drain costs a settle plus the deletes — well over a second — and a
        frame occupies one of 32 slots.  Paying that after every single frame
        spends the entire gap between two scripted frames to reclaim a slot that
        was not needed, which during totality is frames not taken.

        One round when it does run.  A full drain chases frames that are still
        arriving, which costs 3-9 s: measured 8 August, that is longer than the
        1.6 s a corona single has before the next ladder, so the drain a single
        pays for drops the ladder behind it.  A pass frees the slots that exist
        now, which is what the next job needs.
        """
        if not self.buffer_is_filling():
            return 0
        return self.drain(rounds=1)

    def capture(self):
        """Fire the shutter, through the relay when one is connected.

        Retries once after reconnect on failure.
        """
        with self._lock:
            if self.relay is not None:
                self.relay.shoot(pulse=TAP_S)
                self._note_frame_fired()
                # `shoot` leaves both contacts open, so the queue can be cleared
                # here — but only when it has actually filled.
                self.drain_if_filling()
                return
            try:
                self._sdk.shoot_no_af()
            except Exception as first_err:
                logging.warning('Fuji capture failed (%s), attempting reconnect...', first_err)
                if not self._reconnect():
                    raise
                # A reconnected body knows nothing of what the old session set,
                # so put the exposure back before firing.  Reconnecting and then
                # shooting at whatever the camera defaults to is not a recovery:
                # the frame looks normal and is wrong.
                requested = getattr(self, '_requested', None)
                if requested:
                    try:
                        self.configure(**requested)
                    except Exception as exc:
                        hardware_problems.report(
                            self.name,
                            'Exposure could not be restored after reconnect; '
                            'this frame may be at the wrong exposure',
                            detail=str(exc),
                        )
                try:
                    self._sdk.shoot_no_af()
                except Exception:
                    logging.exception('Fuji capture failed again after reconnect')
                    raise

    @property
    def _sdk(self):
        """The SDK handle, or a clear failure if the session is gone.

        Never reach for `_sdk_cam` directly: after a failed reconnect it is None
        precisely so that a call cannot walk into a closed handle.
        """
        if self._sdk_cam is None:
            raise CameraError(
                f"{self.name}: the camera connection is gone - power-cycle the "
                f"body and detect it again")
        return self._sdk_cam

    def _reconnect(self) -> bool:
        """Attempt to close and reopen the SDK camera connection."""
        try:
            self._sdk_cam.close()
        except Exception:
            pass
        try:
            self._sdk_cam = SDKCamera(self._sdk_path, self._device_name)
            self._shooter = None
            # A new session knows nothing about what the old one wrote.
            self._applied_iso = None
            self._applied_speed = None
            logging.info('Fuji camera reconnected successfully')
            return True
        except Exception as e:
            # The old handle was closed above, so what is left points at freed
            # SDK memory.  Calling into it answers 0x1003 and then, a few calls
            # later, segfaults the process - which on 4 August took a rehearsal
            # down six seconds into totality.  Dropping it means every later call
            # raises a CameraError that says what is wrong, and the run keeps
            # going on the relay instead of dying.
            self._sdk_cam = None
            logging.error('Fuji reconnect failed: %s', e)
            return False

    def sync_clock(self) -> None:
        """Report that this body's clock cannot be written from here.

        The Shooting SDK's model headers list a SetDateTime API code, but the
        public headers declare no entry point and XAPI exports none, so there is
        nothing to call.  ``set_config`` below is a no-op, so the gphoto2 path is
        not an alternative either.

        Frames therefore carry whatever the body's own clock says: set it by hand
        and note the residual offset.
        """
        hardware_problems.report(
            self.name,
            'Camera clock cannot be set from the computer — set it on the body by hand',
            detail='the Fuji Shooting SDK exposes no date/time call, so frame '
                   'timestamps follow the camera clock, not this computer',
            severity='warning',
        )

    # gphoto-compatible stubs
    def get_config(self) -> _FujiConfigStub:
        return _FujiConfigStub(self)

    def set_config(self, config) -> None:
        pass

    def get_storageinfo(self) -> list:
        """Free and total space per card, or nothing when the body will not say.

        It used to invent both numbers: the total was "free x 2" because there
        was no total to read, and a body that refused the call reported 999.9 GB
        free.  A card that is nearly full then looks empty right up to the
        moment it stops taking frames.

        An empty list is what the vendor-agnostic helper already reads as
        "unknown" - it returns -1.0 - so saying nothing is both honest and
        already handled.
        """
        entries = []
        for slot in (SDK_ITEM_MEDIASLOT1, SDK_ITEM_MEDIASLOT2):
            try:
                capacity = self._sdk.get_media_capacity(slot)
            except Exception:
                continue
            free_kb = capacity.free_bytes / 1024.0
            total_kb = capacity.card_size / 1024.0 if capacity.card_size else free_kb
            entries.append(_FujiStorageEntry(free_kb, total_kb))
        return entries

    def get_card_status(self) -> list:
        """Whether each card can be written to, as (slot, status, name).

        Unlike the capacity, this one the X-T4 does answer.  It is the pre-flight
        question that matters: a write-protected or full card takes no frames at
        all, and that is not something to discover at second contact.
        """
        status = []
        for slot in (SDK_ITEM_MEDIASLOT1, SDK_ITEM_MEDIASLOT2):
            try:
                value = self._sdk.get_media_status(slot)
            except Exception:
                continue
            status.append((slot, value,
                           MEDIASTATUS_NAMES.get(value, "0x%04x" % value)))
        return status

    def exit(self):
        self.disconnect()

    # Fuji-specific
    @property
    def shooter(self):
        """Whatever can fire this body: the relay if one is connected, else the SDK."""
        if self.relay is not None:
            return _RelayShooter(self)
        if self._shooter is None:
            self._shooter = EclipseShooter(self._sdk_cam)
        return self._shooter

    def validate(self) -> list[CameraIssue]:
        return validate_for_eclipse(self._sdk_cam)

    def parse_bracket_speeds(self, steps_str: str) -> list[int]:
        """Parse a bracket steps string into SDK shutter speed constants.

        The workbench passes bracket steps like "+/- 1 2/3" for Canon AEB.
        For Fuji, we interpret this as EV steps around the current speed
        and return a list of SDK shutter speed constants.

        A semicolon-separated list of speeds ("1/2000;1/125;1/8;2") is taken
        literally instead.  A corona ladder spans some twelve stops in 2 EV
        steps, which the symmetric 1/3-EV form cannot express without firing
        dozens of redundant frames.
        """
        if ";" in steps_str:
            speeds, unknown = [], []
            for text in (part.strip() for part in steps_str.split(";")):
                if not text:
                    continue
                value = _parse_shutter_speed(text)
                (speeds if value is not None else unknown).append(value or text)
            if unknown:
                raise CameraError(
                    f"{self.name}: bracket lists shutter speeds this camera does "
                    f"not have: {', '.join(unknown)}"
                )
            # The rungs are exposures too, so the observer's correction moves
            # the whole ladder rather than only the frames outside it - and each
            # trimmed rung goes back onto the body's own scale, or the ladder
            # dies on its first rung the moment a trim is dialled in.
            return _distinct(snap_to_scale(exposure_trim.apply_microseconds(v))
                             for v in speeds)

        # The base of the ladder is whatever is on the body: the caller has just
        # dialled in the exposure this bracket is meant to straddle.
        try:
            current_speed, _ = self._sdk.get_shutter_speed()
        except Exception as exc:
            raise CameraError(
                f"{self.name}: cannot build a bracket — the camera would not report its "
                f"shutter speed ({exc})"
            ) from exc

        # Parse the step size.  Positions are 1/3 EV apart, so "+/- 1" spans 3
        # positions either side of the base and yields 7 frames.
        try:
            clean = steps_str.replace("+/-", "").strip()
            if " " in clean:
                parts = clean.split()
                whole = int(parts[0])
                frac_parts = parts[1].split("/")
                frac = int(frac_parts[0]) / int(frac_parts[1])
                ev_steps = whole + frac
            else:
                ev_steps = float(clean)
            positions = int(round(ev_steps * 3))
        except (ValueError, IndexError):
            positions = 3  # default: +/- 1 EV

        # A body that reports its own scale knows best: its list is already the
        # 1/3 EV grid this bracket wants, so step along it.
        try:
            supported = self._sdk.get_supported_shutter_speeds()
        except Exception:
            supported = []

        if current_speed in supported:
            idx = supported.index(current_speed)
            speeds = [supported[i] for i in range(idx - positions, idx + positions + 1)
                      if 0 <= i < len(supported)]
            self._warn_if_short(speeds, positions, current_speed, steps_str)
            return speeds

        return self._ladder_around(current_speed, positions, steps_str)

    def _ladder_around(self, current_speed: int, positions: int, steps_str: str) -> list[int]:
        """Shutter constants 1/3 EV apart, centred on ``current_speed``.

        Used when the body will not say what speeds it has: the X-T4's SDK module
        does not implement CapShutterSpeed and answers with an empty list, which
        is not a reason to give up on bracketing.

        The rungs are computed as exposure times — base x 2**(k/3) — and snapped
        to the nearest constant the SDK defines, rather than counted off in list
        positions.  The constant table is not uniformly 1/3 EV apart across its
        whole range, so stepping by index would drift.
        """
        grid = _get_speeds_by_seconds()
        if not grid:
            return [current_speed]

        base = next((secs for secs, value in grid if value == current_speed), None)
        if base is None:
            base = current_speed / 1_000_000.0   # the constants are microseconds

        speeds = []
        for step in range(-positions, positions + 1):
            target = base * (2.0 ** (step / 3.0))
            _, value = min(grid, key=lambda pair: abs(math.log(pair[0] / target)))
            if value not in speeds:
                speeds.append(value)

        self._warn_if_short(speeds, positions, current_speed, steps_str)
        return _distinct(snap_to_scale(exposure_trim.apply_microseconds(v))
                         for v in speeds)

    def _warn_if_short(self, speeds: list, positions: int, current_speed: int,
                       steps_str: str) -> None:
        """Fewer rungs than asked for means the ladder ran off the end of the
        scale, leaving it lopsided around the base exposure."""
        wanted = 2 * positions + 1
        if len(speeds) < wanted:
            logging.warning(
                '%s: bracketing %s around %s wanted %d frames, the scale reaches %d',
                self.name, steps_str, SHUTTER_SPEED_NAMES.get(current_speed, current_speed),
                wanted, len(speeds),
            )

    def describe_speeds(self, speeds: list[int]) -> str:
        """The human-readable ladder behind a list of SDK shutter constants."""
        return ', '.join(str(SHUTTER_SPEED_NAMES.get(s, s)) for s in speeds)


# ======================================================================
# LD_LIBRARY_PATH startup handling
# ======================================================================

def _reexec_process() -> None:
    """Restart the current process so the dynamic linker picks up an updated
    LD_LIBRARY_PATH (it is only read once, at process startup)."""
    logging.info('LD_LIBRARY_PATH updated for the Fuji SDK, restarting process')
    # Reconstruct the command, preserving a `python -m <module>` invocation.
    main_spec = getattr(sys.modules.get('__main__'), '__spec__', None)
    if main_spec and main_spec.name:
        args = [sys.executable, '-m', main_spec.name] + sys.argv[1:]
    else:
        args = [sys.executable] + sys.argv
    os.execvp(sys.executable, args)


def ensure_fuji_library_path() -> bool:
    """Add the Fuji SDK libraries (and their NixOS dependencies) to
    LD_LIBRARY_PATH if they are missing.

    Returns True if the environment was changed and the process must be
    re-exec'd for the dynamic linker to see it, False if nothing was needed.
    """
    if not FUJIXSDK_AVAILABLE:
        return False
    sdk_path = find_fuji_sdk_path()
    if not sdk_path:
        return False
    try:
        # ensure_ld_library_path returns True when the path was already
        # complete, False when it modified the environment.
        return not ensure_ld_library_path(sdk_path)
    except Exception:
        logging.debug('Fuji ensure_ld_library_path failed', exc_info=True)
        return False


def maybe_reexec_for_fuji_sdk() -> None:
    """Call once at application startup, before building the GUI or reading any
    session state.

    Ensures the Fuji SDK libraries are on LD_LIBRARY_PATH and re-execs the
    process immediately if they had to be added — so the one-time restart
    happens at launch rather than mid-session during camera detection, where it
    would discard the user's unsaved settings.
    """
    if ensure_fuji_library_path():
        _reexec_process()


# ======================================================================
# Detection
# ======================================================================

def _reset_mac_camera_stack() -> None:
    """Kill macOS's camera daemons so they respawn with fresh state.

    Not a pre-detect reflex — the daemons are the SDK's transport and must
    normally be left alone.  But a stale ICA session (from a crashed or
    just-closed connection) makes detect return zero cameras or a phantom
    handle whose every call fails 0x2001, and every recovery that has worked
    on the bench involved forcing fresh daemons.  They respawn on demand.
    """
    if platform.system() != "Darwin":
        return
    try:
        subprocess.run(["killall", "-9", "ptpcamerad", "mscamerad-xpc"],
                       capture_output=True, timeout=5)
        logging.info("Reset macOS camera daemons; they respawn on demand")
    except Exception:
        logging.debug("Camera daemon reset failed", exc_info=True)


def _preload_mac_transport(sdk_path: str) -> None:
    """Load the SDK's PTP transport dylibs before XAPI goes looking for them.

    FTLPTP.dylib carries the install name /usr/local/lib/FTLPTP.dylib, where it
    is typically not installed.  Loading it by full path first means XAPI's own
    dlopen resolves to the already-loaded image instead of the missing path.

    Note that ptpcamerad must be left alive on macOS: FTLPTP links
    ImageCaptureCore, whose broker that daemon is — the SDK talks to the camera
    *through* it.  Killing it (the reflex carried over from Linux, where gvfs
    really does steal the device) is self-sabotage here.
    """
    if platform.system() != "Darwin":
        return
    for hit in sorted(Path(sdk_path).rglob("FTLPTP.dylib")):
        for name in ("FTLPTP.dylib", "FTLPTPIP.dylib"):
            candidate = hit.parent / name
            if candidate.exists():
                try:
                    ctypes.CDLL(str(candidate), mode=ctypes.RTLD_GLOBAL)
                    logging.debug("Preloaded %s", candidate)
                except OSError:
                    logging.debug("Could not preload %s", candidate, exc_info=True)
        return


def _report_validation_issues(camera: FujiCamera) -> None:
    """Run the eclipse pre-flight check and surface anything it objects to.

    Done at detection rather than at first shot, which is the whole point: a
    camera left in AF, or on JPEG, or with exposure compensation dialled in, is
    trivial to fix while setting up and impossible to fix afterwards.
    """
    try:
        issues = camera.validate()
    except Exception:
        logging.debug('Fuji validation failed for %s', camera.name, exc_info=True)
        return

    for issue in issues or []:
        # "info" issues are statements of fact (the aperture in use, and so on),
        # not things to fix, so they stay in the log.
        if issue.severity == 'info':
            logging.info('%s: %s is %s', camera.name, issue.setting, issue.current)
            continue
        # The setting, what it needs, what it is - and nothing else.  The
        # sentence in issue.message is written for a dialog that has room to
        # explain; in a log line it buries the three words that matter behind
        # advice the reader did not ask for at that moment.  The popup carries
        # the actionable list; this carries the fact.
        hardware_problems.report(
            camera.name,
            '%s: needs %s, is %s' % (issue.setting, issue.expected, issue.current),
            severity=issue.severity,
        )

    if issues:
        logging.info('Fuji validation raised %d issue(s) for %s', len(issues), camera.name)


class FujiDetection(NamedTuple):
    """What the SDK found, kept apart from what it managed to open.

    These are not the same thing and the difference matters: gphoto2 must be
    kept away from a Fuji body that is merely *present*, not only from one the
    SDK holds open.  See :func:`detect_fuji`.
    """

    cameras: dict
    bodies_seen: int


def detect_fuji(sdk_path: str) -> FujiDetection:
    """Detect Fuji bodies via the SDK, reporting sightings and opens apart.

    A body that is seen but will not open is the dangerous case.  It used to
    leave the caller with an empty dict, indistinguishable from "no Fuji here",
    so gphoto2 went on to claim the device over PTP - which it cannot drive
    tethered anyway.  The claim then guaranteed the SDK could never open it:
    every later attempt answered 0x2001 and detect fell to zero.  Measured on
    4 August: SDK open failed, gphoto2 claimed the X-T4 one second later, and
    the body stayed unreachable until it was power-cycled.

    Retries detection up to 3 times with a delay after killing ptpcamerad,
    because the USB device needs time to become available.
    """
    if not FUJIXSDK_AVAILABLE:
        # Returning silently here once cost a whole bench sitting: the script
        # reported "no camera" when the truth was "the wrapper never imported".
        logging.error("fujixsdk is not importable (%s) — the SDK was never tried. "
                      "Run from the repo root or put it on sys.path.",
                      FUJIXSDK_IMPORT_ERROR)
        return FujiDetection({}, 0)

    _preload_mac_transport(sdk_path)

    # A body that has just powered on or enumerated often needs a few seconds
    # before the SDK sees it - that is patience, not a stuck session.  So the
    # first retry only waits.  The daemon reset is the LAST resort, and after
    # one the transport needs time to respawn: the 5 August 00:16 failure was
    # detect killing the daemons and the open starting three seconds later,
    # straight into the window where the transport was dead - the same
    # sabotage removed from the open path earlier, one step up the chain.
    cameras = []
    reset_at = None
    for attempt in range(3):
        if attempt == 1:
            time.sleep(4.0)
        elif attempt == 2:
            _reset_mac_camera_stack()
            reset_at = time.monotonic()
            time.sleep(3.0)
        try:
            # Names only.  Opening each body to read its product string, then
            # closing it, then opening it again to use it, is three sessions
            # where one will do - and every extra open/close is another chance
            # for the SDK to corrupt the heap if the device moves.  The product
            # name comes off the session that is kept, below.
            cameras = SDKCamera.detect(sdk_path, with_info=False)
            logging.info('Fuji SDK detect attempt %d returned %d camera(s)',
                         attempt + 1, len(cameras))
            if cameras:
                break
        except fujixsdk.LDPathError:
            # Safety net: the SDK signalled LD_LIBRARY_PATH needs updating
            # mid-run.  maybe_reexec_for_fuji_sdk() at startup normally prevents
            # ever reaching this point.
            _reexec_process()
        except Exception as e:
            logging.debug('Fuji SDK detect attempt %d failed: %s', attempt + 1, e)
        time.sleep(2)

    if not cameras:
        logging.warning('Fuji SDK found no cameras after retries')
        return FujiDetection({}, 0)

    # If the daemons were reset, nothing opens until the transport has had ten
    # seconds to come back.  An OpenEx that starts too early does not fail
    # fast - it dies a fifteen second handshake death and reports -1.
    if reset_at is not None:
        settle = 10.0 - (time.monotonic() - reset_at)
        if settle > 0:
            logging.info('Waiting %.0f s for the camera transport to respawn '
                         'before opening', settle)
            time.sleep(settle)

    result = {}
    for info in cameras:
        # Use "Fuji Fujifilm <model>" to match gphoto2's naming convention
        name = f"Fuji Fujifilm {info.product}" if info.product != "(unknown)" else f"Fuji Camera ({info.device_name})"
        try:
            sdk_cam = _open_through_the_daemon(sdk_path, info.device_name)
            # The product name from the session just opened, so the camera is
            # still called "Fuji Fujifilm X-T4" - the name scripts use - without
            # a second session having been opened to find that out.
            try:
                product = sdk_cam.device_info.product
                if product:
                    name = f"Fuji Fujifilm {product}"
            except Exception:
                logging.debug('Could not read the product name for %s',
                              info.device_name, exc_info=True)
            fuji_cam = FujiCamera(sdk_cam, name, sdk_path, info.device_name)
            result[name] = fuji_cam
            # The relay commands in a script get a trigger, not a camera, so the
            # open SDK session has to be findable from there: it is the session
            # that queues a transfer per frame, and so the session that decides
            # how long the release may be held and has to be drained afterwards.
            register_hardware('sdk_camera', fuji_cam)
            logging.info('Detected Fuji camera: %s (device=%s)', name, info.device_name)
            _report_validation_issues(fuji_cam)
        except Exception as e:
            logging.warning('Failed to open Fuji camera %s: %s', info.device_name, e)
            hardware_problems.report(
                'Fuji SDK',
                f'Found {name} but could not open it',
                detail=str(e),
            )

    return FujiDetection(result, len(cameras))


def _open_through_the_daemon(sdk_path: str, device_name: str) -> "SDKCamera":
    """Open the session.  The daemons are left alone: they ARE the transport.

    This function used to kill ptpcamerad immediately before the open, on the
    theory that macOS was holding the device.  The log convicted it:

        23:14:22  detect attempt 2 returned 1 camera(s)   <- transport working
        23:14:22  Reset macOS camera daemons               <- this function
        23:14:23  Failed to open ... SDK call returned error

    The Fuji SDK talks to the body through the ImageCapture daemons, which is
    what the comment on _reset_mac_camera_stack has said all along.  Killing
    them one second before OpenEx is sabotage: the open lands in the window
    where the transport is dead and respawning.  Every app-path open failure
    since the kill was added has this shape, and reproducing the app's entry
    point on the bench failed with the kill and opened without it.

    Recovery from a genuinely stuck ICA session stays where it always was:
    detect_fuji resets the daemons when detection itself finds nothing.
    """
    return SDKCamera(sdk_path, device_name)






def detect_fuji_cameras(sdk_path: str) -> dict[str, FujiCamera]:
    """The bodies the SDK opened, for callers that need nothing more."""
    return detect_fuji(sdk_path).cameras


# ======================================================================
# SDK path resolution
# ======================================================================

def _sdk_marker() -> str:
    """The name of the SDK's main library on this platform.

    Linux ships XAPI.so, Windows XAPI.dll, and macOS a XAPI.bundle directory —
    so searching for the Linux name alone finds nothing on a Mac.
    """
    system = platform.system()
    if system == "Darwin":
        return "XAPI.bundle"
    if system == "Windows":
        return "XAPI.dll"
    return "XAPI.so"


def find_fuji_sdk_path() -> Optional[str]:
    """Find the Fuji SDK library path.

    Checks in order:
    1. FUJI_SDK_PATH environment variable
    2. ConfigManager fuji_sdk_path setting (if available)
    3. Auto-detect: look for SDK dirs containing the platform's XAPI library
    """
    # 1. Environment variable
    env_path = os.environ.get('FUJI_SDK_PATH')
    if env_path and Path(env_path).is_dir():
        return env_path

    # 2. ConfigManager setting (optional — older configs may not store one)
    try:
        from solareclipseworkbench.location_ui import ConfigManager
        cfg = ConfigManager()
        cfg_path = cfg.get_fuji_sdk_path()
        if cfg_path and Path(cfg_path).is_dir():
            return cfg_path
    except Exception:
        pass

    # 3. Auto-detect in common locations
    search_dirs = [
        Path.home() / "fujixsdk",
        Path.home() / "FujiSDK",
        Path("/opt/fujixsdk"),
        Path("/usr/local/lib/fujixsdk"),
    ]
    # Also look relative to the workbench install
    try:
        import solareclipseworkbench
        pkg_dir = Path(solareclipseworkbench.__file__).parent
        search_dirs.extend([
            pkg_dir.parent.parent / "SDK",
            pkg_dir.parent.parent / "fujixsdk",
            pkg_dir.parent.parent.parent / "SDK",
            pkg_dir.parent.parent.parent / "fujixsdk",
        ])
    except Exception:
        pass

    marker = _sdk_marker()
    for base in search_dirs:
        if not base.is_dir():
            continue
        # Look for SDK* dirs containing the shared lib
        for sdk_dir in sorted(base.glob("SDK*")):
            if sdk_dir.is_dir() and list(sdk_dir.glob(f"**/{marker}")):
                return str(sdk_dir)
        # Or the base dir itself
        if list(base.glob(f"**/{marker}")):
            return str(base)

    return None
