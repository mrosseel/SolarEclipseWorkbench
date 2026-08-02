"""Measure exactly how far the buffer count moves per tap, and whether it lags.

Two numbers decide DRAIN_AT, and both have so far been guessed at:

  jump  - how many slots one tap can add.  The queue is only checked between
          frames, so the headroom below full has to cover the largest jump or
          the body reaches 32 and stops dead mid-sequence.
  lag   - how far the count is behind reality at the moment it is read.  If the
          body reports frames only once they are written, a reading taken right
          after a tap understates the queue, and the threshold is testing a
          number that is already stale.

Both are measured here by reading GetBufferCapacity immediately after each tap
and again as it settles, filling the queue to a stopping point and draining, for
as many rounds as asked.  Nothing is inferred: every reading is printed.

    ./run.sh scripts/buffer_jump_probe.py
    ./run.sh scripts/buffer_jump_probe.py --speed 1/4000 --rounds 5
"""

import argparse
import logging
import statistics
import sys
import time
from pathlib import Path

# fujixsdk lives in the repo root, which is not on sys.path when this file is
# run as scripts/buffer_jump_probe.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solareclipseworkbench.fuji_camera import (TAP_S, detect_fuji_cameras,
                                               find_fuji_sdk_path,
                                               maybe_reexec_for_fuji_sdk)
from solareclipseworkbench.hardware_registry import register_hardware
from solareclipseworkbench.relay_trigger import open_trigger

maybe_reexec_for_fuji_sdk()

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m")

# The fastest rungs double most reliably, so this is where the largest jump is.
FAST_SPEED = "1/1000"

# When the settled count reaches this, the round stops and drains.  Two slots
# short of full: the point is to measure the approach, not to wedge the body.
STOP_AT = 30

# How long after a tap the count is watched.  A frame at 1/1000 is written well
# inside this, so anything still arriving after it is genuine lag.
SETTLE_READS_S = [0.0, 0.15, 0.35, 0.75, 1.5]

ROUNDS = 3


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


def captured(sdk_cam):
    return sdk_cam.get_buffer_capacity()[0]


def drain_fully(camera) -> int:
    total = 0
    for _ in range(5):
        drained = camera.drain()
        total += drained
        if drained == 0:
            break
    return total


def one_round(camera, relay, n_round: int) -> tuple[list, list]:
    """Fill the queue tap by tap, reading the count as it moves.

    Returns (jumps, lags): slots added per tap by the settled count, and how far
    the immediate reading was behind the settled one.
    """
    sdk_cam = camera._sdk_cam
    drain_fully(camera)
    settled_before = captured(sdk_cam)
    print(f"\n{BOLD}Round {n_round}{RESET}  {DIM}queue starts at {settled_before}{RESET}")
    print(f"{DIM}   tap   reads at {' '.join(f'{t:.2f}s' for t in SETTLE_READS_S)}"
          f"   jump   lag{RESET}")

    jumps, lags = [], []
    tap = 0
    relay.half_press()
    try:
        while settled_before < STOP_AT and tap < 40:
            tap += 1
            relay.shoot(pulse=TAP_S)
            fired = time.perf_counter()

            reads = []
            for at in SETTLE_READS_S:
                remaining = at - (time.perf_counter() - fired)
                if remaining > 0:
                    time.sleep(remaining)
                reads.append(captured(sdk_cam))

            settled = reads[-1]
            jump = settled - settled_before
            lag = settled - reads[0]
            jumps.append(jump)
            lags.append(lag)
            flag = f"  {YELLOW}<-{RESET}" if lag > 0 else ""
            print(f"  {tap:>4}   {' '.join(f'{r:>5}' for r in reads)}"
                  f"   {jump:>4}  {lag:>4}{flag}")
            settled_before = settled
    finally:
        relay.release_all()

    emptied = drain_fully(camera)
    print(f"{DIM}  drained {emptied} at {settled_before} reported{RESET}")
    if emptied != settled_before:
        print(f"  {YELLOW}drained {emptied} but the count said {settled_before} "
              f"— the count was off by {emptied - settled_before}{RESET}")
    return jumps, lags


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--speed", default=FAST_SPEED,
                        help=f"shutter speed (default: {FAST_SPEED})")
    parser.add_argument("--iso", type=int, default=400)
    parser.add_argument("--rounds", type=int, default=ROUNDS)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

    print(f"{BOLD}Buffer jump probe{RESET}")
    print(f"{DIM}filling to {STOP_AT}/32 at {args.speed}, {args.rounds} round(s), "
          f"{TAP_S * 1000:.0f} ms taps.{RESET}")

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

    jumps, lags = [], []
    try:
        for n in range(1, args.rounds + 1):
            j, l = one_round(camera, relay, n)
            jumps += j
            lags += l
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Stopped early.{RESET}")
    finally:
        relay.release_all()
        drain_fully(camera)

    if not jumps:
        return

    print(f"\n{BOLD}{len(jumps)} taps{RESET}")
    print(f"  jump per tap:  max {max(jumps)}  mean {statistics.mean(jumps):.2f}  "
          f"distribution {dict(sorted({j: jumps.count(j) for j in set(jumps)}.items()))}")
    print(f"  lag on read:   max {max(lags)}  mean {statistics.mean(lags):.2f}  "
          f"distribution {dict(sorted({l: lags.count(l) for l in set(lags)}.items()))}")

    # A tap's frames appear in the count all at once, some tenths of a second
    # later, so `lag` and `jump` measure the same event and must not be added
    # together.  The headroom is one tap the reading has not seen yet, plus the
    # one that fires before the next reading.
    unseen = max(lags)
    headroom = unseen + max(jumps)
    print(f"\n{BOLD}Headroom needed: {headroom} slot(s){RESET}  "
          f"{DIM}(a tap of {unseen} not yet counted, plus the next tap of "
          f"{max(jumps)} before the following check){RESET}")
    print(f"  DRAIN_AT at most {(32 - headroom) / 32:.3f} "
          f"({32 - headroom}/32 slots)")
    if unseen == 0:
        print(f"  {DIM}The count is current the instant it is read.{RESET}")


if __name__ == "__main__":
    main()
