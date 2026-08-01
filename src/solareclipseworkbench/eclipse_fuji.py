"""Production composites for the X-T4 relay + SDK rig.

Every number here was measured on the bench of 1 August 2026 (see
``xt4-relay-bench-2026-08-01.md``), and each composite exists because of a
failure mode observed there:

  * every frame taken while an SDK session is open occupies one of 32
    volatile-buffer slots awaiting a PC transfer that never comes; at 32 the
    camera wedges hard enough to need a battery pull.  So shooting and
    draining are woven together, and no burst may exceed ~28 frames since the
    last drain,
  * a drain issued while anything is shooting (S1 held, tap in flight) drops
    the USB session permanently.  So every composite finishes its shooting,
    releases the relay, settles, and only then drains — and the module lock
    keeps two scheduled jobs from interleaving a drain into a burst,
  * SDK-triggered shooting fails under CH drive, which the eclipse requires
    for the burst phases.  So the relay does all triggering and the SDK is
    used only for exposure changes and drains,
  * a 40 ms relay tap misses roughly 8% of the time; 80 ms taps did not miss.

The composites are one scheduled job each, so the eclipse script stays a flat
list of moments and the shoot-then-drain choreography cannot be broken by
editing the script.
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)

# One job at a time: a drain arriving mid-burst kills the USB session, so every
# composite runs under this lock.  RLock so a composite may call another.
SHOOTING_LOCK = threading.RLock()

# 15 fps mechanical for longer than this risks reaching the 32-slot wedge
# before the drain that follows the burst can run.
MAX_BURST_S = 1.9

# Relay contact closure per frame.  40 ms missed ~8% on the bench; 80 ms never did.
TAP_S = 0.08

# The camera samples the release line slowly enough that back-to-back taps need
# daylight between them, and a fast exposure needs time to complete.
TAP_GAP_S = 0.35

# The session survives a drain only when the camera has genuinely stopped
# shooting; releasing the relay and waiting this long was proven safe.
SETTLE_BEFORE_DRAIN_S = 1.0


def _drain(camera) -> int:
    """Empty the pending-transfer queue.  Caller holds the lock and has settled."""
    sdk_cam = camera._sdk_cam
    try:
        captured, total = sdk_cam.get_buffer_capacity()
        started = time.perf_counter()
        drained = sdk_cam.drain_buffer()
        took = time.perf_counter() - started
        logger.info("Drained %d frame(s) in %.2f s (buffer was %d/%d)",
                    drained, took, captured, total)
        return drained
    except Exception:
        # A failed drain must never take the schedule down with it: the next
        # composite will try again, and the relay keeps working regardless.
        logger.exception("Drain failed; carrying on — the relay is unaffected")
        return 0


def _set_speed(camera, speed: str) -> bool:
    try:
        camera.configure(shutter_speed=speed)
        return True
    except Exception:
        logger.exception("Could not set %s; the next frames keep the previous speed", speed)
        return False


def fuji_speed(camera, speed: str) -> None:
    """Set the shutter speed over USB, nothing else."""
    with SHOOTING_LOCK:
        logger.info("fuji_speed %s", speed)
        _set_speed(camera, speed)


def fuji_drain(camera) -> None:
    """Drain the pending-transfer queue in a quiet moment."""
    with SHOOTING_LOCK:
        time.sleep(SETTLE_BEFORE_DRAIN_S)
        _drain(camera)


def fuji_partial(camera, trigger, speed: str = "") -> None:
    """One partial-phase shot, self-cleaning.

    Optionally sets the speed, taps the relay (2 frames at fast speeds — the CH
    quantum — 1 at slow), settles, drains its own frames.  Wedge-proof at any
    cadence because nothing accumulates.
    """
    with SHOOTING_LOCK:
        logger.info("fuji_partial%s", f" at {speed}" if speed else "")
        if speed:
            _set_speed(camera, speed)
        trigger.shoot(pulse=TAP_S)
        time.sleep(SETTLE_BEFORE_DRAIN_S)
        _drain(camera)


def fuji_beads_burst(camera, trigger, duration) -> None:
    """A beads/diamond-ring burst, clamped below the wedge line, then drained.

    S1 should already be held (``relay_arm``) so the burst starts within the
    body's ~45 ms release lag; ``pressed()`` leaves a pre-armed S1 closed on
    exit, so consecutive bursts keep the low latency.
    """
    duration = min(float(duration), MAX_BURST_S)
    with SHOOTING_LOCK:
        logger.info("fuji_beads_burst %.2f s", duration)
        with trigger.pressed():
            time.sleep(duration)
        time.sleep(SETTLE_BEFORE_DRAIN_S)
        _drain(camera)


def fuji_ladder(camera, trigger, speeds: str, taps_per_speed=1) -> None:
    """One corona exposure ladder: set each speed over USB, tap, then drain.

    ``speeds`` is semicolon-separated ("1/2000;1/500;...;2") because the script
    format uses commas.  Frames per tap follow the CH quantum: 2 at fast
    speeds, 1 once the exposure outlasts the tap window (~1/15 and slower).
    """
    steps = [s.strip() for s in speeds.split(";") if s.strip()]
    taps_per_speed = int(taps_per_speed)
    with SHOOTING_LOCK:
        logger.info("fuji_ladder: %s (%d tap(s) per speed)", steps, taps_per_speed)
        trigger.half_press()
        for speed in steps:
            _set_speed(camera, speed)
            for _ in range(taps_per_speed):
                trigger.shoot(pulse=TAP_S)
                time.sleep(TAP_GAP_S)
        trigger.release_all()
        time.sleep(SETTLE_BEFORE_DRAIN_S)
        _drain(camera)
        # Re-arm: during totality the next composite fires within seconds and
        # must not pay the ~130 ms unarmed wake.
        trigger.half_press()
