"""Measure what a relay burst actually shoots, against what it claims.

``burst_no_download`` holds the release for ``count / CH_FPS`` seconds and then
returns ``seconds * CH_FPS`` — the same arithmetic back again, never the frames
that landed.  Both constants are assumptions, and everything measured on this rig
so far has come in above its assumed rate: a bracket tap was taken to fire one
frame and fires 2.30.

Two things are wrong if CH_FPS is low.  The beads burst is short of frames at the
only moment that cannot be reshot, and MAX_BURST_S — sized to keep 15 fps inside
a 32-slot buffer — lets a faster body overrun it, which stops the camera dead
until the battery comes out.

The rate is therefore approached from below: short holds first, and the next
hold is attempted only if the rate measured so far says it will stay inside the
buffer.  The count comes from the drain, which has been verified to delete
exactly what the buffer reports.

    ./run.sh scripts/burst_rate_probe.py
    ./run.sh scripts/burst_rate_probe.py --speed 1/2000
"""

import argparse
import logging
import sys
import time
from pathlib import Path

# fujixsdk lives in the repo root, which is not on sys.path when this file is
# run as scripts/burst_rate_probe.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solareclipseworkbench.fuji_camera import (CH_FPS, MAX_BURST_S,
                                               detect_fuji_cameras,
                                               find_fuji_sdk_path,
                                               maybe_reexec_for_fuji_sdk)
from solareclipseworkbench.hardware_registry import register_hardware
from solareclipseworkbench.relay_trigger import open_trigger

maybe_reexec_for_fuji_sdk()

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m")

FAST_SPEED = "1/2000"

# Climbing, so an overrun is predicted from the rate already measured rather
# than discovered by wedging the body.
HOLDS_S = [0.2, 0.4, 0.7, 1.0, 1.4, 1.9]

BUFFER_SLOTS = 32

# Refuse a hold whose projected frames come within this of filling the buffer.
# A full buffer is recoverable only by pulling the battery.
SAFE_CEILING = 28


def connect():
    sdk_path = find_fuji_sdk_path()
    if not sdk_path:
        print(f"{RED}No Fuji SDK found.{RESET}  Set FUJI_SDK_PATH and rerun.")
        return None
    cameras = detect_fuji_cameras(sdk_path)
    if not cameras:
        print(f"{RED}The SDK cannot open the body.{RESET}")
        return None
    name, camera = next(iter(cameras.items()))
    print(f"{GREEN}Connected:{RESET} {name}")
    return camera


def drain_fully(camera) -> int:
    total = 0
    for _ in range(5):
        drained = camera.drain()
        total += drained
        if drained == 0:
            break
    return total


def hold_and_count(camera, relay, seconds: float) -> tuple[int, float]:
    """Hold the release for `seconds`, returning (frames, actual hold time)."""
    drain_fully(camera)
    started = time.perf_counter()
    with relay.pressed():
        time.sleep(seconds)
    held = time.perf_counter() - started
    # `pressed()` leaves S1 closed when pre-armed, and draining with S1 held
    # drops the session for good.
    relay.release_all()
    return drain_fully(camera), held


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--speed", default=FAST_SPEED,
                        help=f"shutter speed (default: {FAST_SPEED})")
    parser.add_argument("--iso", type=int, default=400)
    parser.add_argument("--holds", type=float, nargs="*", default=HOLDS_S,
                        metavar="S", help="hold durations to try, in seconds")
    parser.add_argument("--ceiling", type=int, default=SAFE_CEILING,
                        help=f"most frames this probe will risk queueing against "
                             f"{BUFFER_SLOTS} slots (default: {SAFE_CEILING})")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

    print(f"{BOLD}Burst rate probe{RESET}")
    print(f"{DIM}the code assumes {CH_FPS} fps and caps a burst at "
          f"{MAX_BURST_S}s, which is {CH_FPS * MAX_BURST_S:.0f} frames of "
          f"{BUFFER_SLOTS} slots.{RESET}\n")

    camera = connect()
    if camera is None:
        return
    try:
        register_hardware('relay', open_trigger('auto', s1_channel=1, s2_channel=2))
    except Exception as exc:
        print(f"{RED}No relay ({exc}) — this measures the relay path.{RESET}")
        return
    relay = camera.relay

    try:
        camera.configure(shutter_speed=args.speed, iso=args.iso)
    except Exception as exc:
        print(f"{YELLOW}Could not set {args.speed}: {exc}{RESET}")

    print(f"\n{BOLD}{'asked':>7}  {'held':>7}  {'frames':>7}  {'fps':>7}{RESET}")
    rows = []
    # CH_FPS is only the prior for the first hold; once the body has been
    # measured, the measurement is what the projection uses.  Keeping the
    # assumption as a floor forever would refuse to test the production cap on
    # the strength of the very number being checked.
    measured_max = None
    try:
        for seconds in sorted(args.holds):
            projected = (measured_max if measured_max is not None else CH_FPS) * seconds
            if projected > args.ceiling:
                source = ("measured" if measured_max is not None else "assumed")
                print(f"{YELLOW}  skipping {seconds:.1f}s: {source} "
                      f"{measured_max or CH_FPS:.1f} fps projects "
                      f"{projected:.0f} frames, past the {args.ceiling} this "
                      f"probe will risk against {BUFFER_SLOTS} slots.{RESET}")
                continue
            frames, held = hold_and_count(camera, relay, seconds)
            fps = frames / held if held else 0.0
            measured_max = fps if measured_max is None else max(measured_max, fps)
            rows.append((seconds, held, frames, fps))
            print(f"{seconds:>6.1f}s  {held:>6.2f}s  {frames:>7}  {fps:>7.1f}")
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Stopped early.{RESET}")
    finally:
        relay.release_all()
        drain_fully(camera)

    if not rows:
        return

    # The rate from the longest hold is the one that matters: the short holds
    # include the body's release lag, which a burst pays once however long it is.
    longest = max(rows, key=lambda r: r[1])
    measured_fps = longest[2] / longest[1]
    print(f"\n{BOLD}Measured {measured_fps:.1f} fps{RESET} "
          f"{DIM}(from the {longest[0]:.1f}s hold: {longest[2]} frames in "
          f"{longest[1]:.2f}s){RESET}")
    print(f"  CH_FPS says {CH_FPS}")

    # Projected from the time the contact is really closed, not the time asked
    # for: the relay adds a consistent overhead on top of every hold, and
    # ignoring it under-states the frames a burst is about to queue.
    overhead = sum(held - asked for asked, held, _, _ in rows) / len(rows)
    at_cap = measured_fps * (MAX_BURST_S + overhead)
    print(f"\n{DIM}Relay adds {overhead:+.2f}s to every hold.{RESET}")
    print(f"{BOLD}A full {MAX_BURST_S}s burst holds "
          f"{MAX_BURST_S + overhead:.2f}s and queues {at_cap:.0f} frames "
          f"of {BUFFER_SLOTS} slots.{RESET}")
    if at_cap > BUFFER_SLOTS:
        print(f"  {RED}That overruns the buffer and stops the body dead.{RESET}  "
              f"MAX_BURST_S should be at most "
              f"{BUFFER_SLOTS / measured_fps:.2f}s at this rate.")
    elif at_cap > SAFE_CEILING:
        print(f"  {YELLOW}Inside the buffer, but with little to spare.{RESET}")
    else:
        print(f"  {GREEN}Comfortably inside the buffer.{RESET}")

    # What take_burst promises a script, against what the body delivers.
    print(f"\n{BOLD}What a script asking for N frames would actually get{RESET}")
    for asked in (10, 20, 28):
        seconds = min(asked / CH_FPS, MAX_BURST_S)
        claimed = int(seconds * CH_FPS)
        real = measured_fps * seconds
        print(f"  asked {asked:>3}  ->  holds {seconds:.2f}s  claims {claimed:>3}"
              f"  really {real:.0f}")


if __name__ == "__main__":
    main()
