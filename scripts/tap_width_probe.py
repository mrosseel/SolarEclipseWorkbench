"""Find the contact width that fires exactly one frame per tap.

The relay closes the release jack for ``TAP_S`` seconds.  Too short and the body
ignores the tap; too long and, at a fast shutter, the body fits a second frame
inside the same contact — 13 taps came back as 28 frames on 2 August, filling
half the 32-slot buffer with images nobody asked for.  Both failures are silent
in the frames themselves, so the width has to be measured rather than guessed.

40 ms doubles too, so the usable width is somewhere below it, and it may be that
nothing is both short enough not to double and long enough to be seen — in which
case the fix is not a width at all and this probe says so.

Each width is tried at a deliberately fast shutter speed, where doubling is
easiest to provoke: the taps are counted going out, the frames counted coming
back off the transfer queue, and the ratio is the answer.

    ./run.sh scripts/tap_width_probe.py
    ./run.sh scripts/tap_width_probe.py --widths 40 55 70 --taps 20
"""

import argparse
import logging
import sys
import time
from pathlib import Path

# fujixsdk lives in the repo root, which is not on sys.path when this file is
# run as scripts/tap_width_probe.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solareclipseworkbench.fuji_camera import (detect_fuji_cameras, find_fuji_sdk_path,
                                               maybe_reexec_for_fuji_sdk)
from solareclipseworkbench.hardware_registry import register_hardware
from solareclipseworkbench.relay_trigger import open_trigger

maybe_reexec_for_fuji_sdk()

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m")

# 40 ms already doubles at a fast shutter (bench, 2 August), so the whole 40-80
# range is settled and the answer is below it.  The bottom of the sweep is where
# the relay itself gives out: a contact shorter than the switching time never
# reaches the body at all, and that floor is worth seeing rather than assuming.
DEFAULT_WIDTHS_MS = [5, 10, 15, 20, 25, 30, 35, 40]

# Fast enough that a second frame fits inside a long contact.  The whole point
# is to provoke the doubling, not to avoid it.
FAST_SPEED = "1/1000"

# Long enough that the body has finished the frame before the next tap, so a
# missed tap means the width was too short and nothing else.
GAP_S = 0.6

# The queue holds 32 frames and a full buffer stops the body dead, so a run that
# could double must stay well inside it.
BUFFER_SLOTS = 32

# Drained this often mid-run, so the tap count is free to be large enough to say
# something: 10 taps that could double is a third of the buffer.
TAPS_PER_DRAIN = 10


def connect():
    sdk_path = find_fuji_sdk_path()
    if not sdk_path:
        print(f"{RED}No Fuji SDK found.{RESET}  Set FUJI_SDK_PATH and rerun.")
        return None
    cameras = detect_fuji_cameras(sdk_path)
    if not cameras:
        print(f"{RED}The SDK cannot open the body.{RESET}")
        print("  - powered on, USB connected?")
        print("  - MENU > wrench > CONNECTION SETTING > PC CONNECTION MODE")
        print("    must be USB TETHER SHOOTING, not card reader")
        return None
    name, camera = next(iter(cameras.items()))
    print(f"{GREEN}Connected:{RESET} {name}")
    return camera


def drain_fully(camera) -> int:
    """Empty the queue and say how much came out, however many drains it takes."""
    total = 0
    for _ in range(4):
        drained = camera.drain()
        total += drained
        if drained == 0:
            break
    return total


def measure(camera, relay, width_ms: int, taps: int, prearm: bool = True) -> tuple[int, float, list]:
    """Fire `taps` taps of `width_ms`.

    Returns (frames queued, frames per tap, actual contact durations in ms).

    The durations are the point of the exercise as much as the frames: a relay
    whose closure is dominated by USB latency does not honour the width it is
    given, and then no amount of sweeping the commanded width means anything.

    The queue is emptied every ``TAPS_PER_DRAIN`` taps rather than at the end,
    so a run can be long enough to say something statistically without the 32
    slots overflowing halfway through.

    With ``prearm`` — how the eclipse path shoots — S1 is closed once and held,
    and each tap pulses S2 alone.  Without it every tap is a full press and
    release, which costs the 120 ms settle per frame but leaves the body no
    half-pressed state to carry between taps.  Whether that changes the frame
    count is exactly the open question.
    """
    drain_fully(camera)          # start from a queue known to be empty

    frames = 0
    actual_ms = []
    if prearm:
        relay.half_press()
    try:
        for n in range(taps):
            started = time.perf_counter()
            relay.shoot(pulse=width_ms / 1000)
            actual_ms.append((time.perf_counter() - started) * 1000)
            time.sleep(GAP_S)
            if (n + 1) % TAPS_PER_DRAIN == 0:
                relay.release_all()      # never drain with the contact closed
                frames += drain_fully(camera)
                if prearm:
                    relay.half_press()
    finally:
        relay.release_all()

    frames += drain_fully(camera)
    return frames, frames / taps if taps else 0.0, actual_ms


def verdict(per_tap: float) -> str:
    if per_tap == 0:
        return f"{RED}nothing fired{RESET}"
    if per_tap < 0.98:
        return f"{RED}misses taps{RESET}"
    if per_tap <= 1.02:
        return f"{GREEN}one frame per tap{RESET}"
    return f"{YELLOW}doubling{RESET}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--widths", type=int, nargs="*", default=DEFAULT_WIDTHS_MS,
                        metavar="MS", help="contact widths to try, in milliseconds")
    parser.add_argument("--taps", type=int, default=20,
                        help="taps per width (default: 20)")
    parser.add_argument("--speed", default=FAST_SPEED,
                        help=f"shutter speed to measure at (default: {FAST_SPEED})")
    parser.add_argument("--iso", type=int, default=400)
    parser.add_argument("--no-prearm", action="store_true",
                        help="release S1 between taps instead of holding it closed "
                             "across the sequence, as the eclipse path does")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

    print(f"{BOLD}Tap width probe{RESET}")
    print(f"{DIM}focus and shutter on manual, RAW only, lens cap on is fine.{RESET}")
    print(f"{DIM}{args.taps} taps per width at {args.speed} — a frame every "
          f"{GAP_S:.1f}s, so a miss means the contact was too short.{RESET}")
    print(f"{DIM}S1 is {'released between taps' if args.no_prearm else 'held closed across the sequence, as the eclipse path does'}.{RESET}\n")

    camera = connect()
    if camera is None:
        return

    try:
        register_hardware('relay', open_trigger('auto', s1_channel=1, s2_channel=2))
    except Exception as exc:
        print(f"{RED}No relay ({exc}) — this probe measures the relay path and "
              f"has nothing to do without one.{RESET}")
        return
    relay = camera.relay

    try:
        camera.configure(shutter_speed=args.speed, iso=args.iso)
    except Exception as exc:
        # Worth saying but not worth stopping for: the widths still compare
        # against each other, they just compare at whatever speed the body kept.
        print(f"{YELLOW}Could not set {args.speed} ISO {args.iso}: {exc}{RESET}")
        print(f"{YELLOW}Measuring at the body's own exposure instead.{RESET}")

    print(f"\n{BOLD}{'asked':>8}  {'actual contact':>18}  {'frames':>7}  "
          f"{'per tap':>8}  verdict{RESET}")
    results = []
    contact = []
    try:
        for width_ms in args.widths:
            frames, per_tap, actual_ms = measure(camera, relay, width_ms,
                                                 args.taps, prearm=not args.no_prearm)
            results.append((width_ms, per_tap))
            lo, hi = min(actual_ms), max(actual_ms)
            contact.append((width_ms, sum(actual_ms) / len(actual_ms), lo, hi))
            print(f"{width_ms:>6} ms  {sum(actual_ms) / len(actual_ms):>7.1f} "
                  f"({lo:.0f}-{hi:.0f}) ms  {frames:>7}  "
                  f"{per_tap:>8.2f}  {verdict(per_tap)}")
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Stopped early.{RESET}")
    finally:
        relay.release_all()
        drain_fully(camera)

    print()

    # Does the width asked for reach the body at all?  Each tap is two USB
    # writes around a sleep, and if the writes cost more than the sleep then the
    # commanded width stopped meaning anything and no sweep of it can help.
    if contact:
        overhead = [mean - asked for asked, mean, _, _ in contact]
        print(f"{DIM}Contact overhead above the width asked for: "
              f"{min(overhead):.0f}-{max(overhead):.0f} ms.{RESET}")
        if min(overhead) > 10:
            print(f"{YELLOW}The relay adds more than it is being asked to hold, "
                  f"so the commanded width is not what the body sees.{RESET}")

    # Wider must never fire less.  Where it does, something other than the width
    # is deciding the outcome and a finer sweep would only chase noise.
    ordered = sorted(results)
    inversions = [(a, b) for (a, pa), (b, pb) in zip(ordered, ordered[1:])
                  if pb < pa - 0.15]
    if inversions:
        a, b = inversions[0]
        print(f"{YELLOW}Not monotonic: {b} ms fired less than {a} ms.{RESET}  "
              f"Contact width is not what decides this — more taps per width "
              f"will tighten the numbers, but the cause is elsewhere.")

    clean = [w for w, per_tap in results if 0.98 <= per_tap <= 1.02]
    if clean:
        # The widest clean width has the most margin against a missed tap, which
        # is the failure that costs a frame outright.
        print(f"{GREEN}One frame per tap at:{RESET} "
              f"{', '.join(f'{w} ms' for w in clean)}")
        print(f"{DIM}Widest clean width is {max(clean)} ms — set TAP_S in "
              f"fuji_camera.py to {max(clean) / 1000:.3f} to use it.{RESET}")
    elif results:
        print(f"{YELLOW}No width fired exactly one frame per tap.{RESET}")
        missed = [w for w, per_tap in results if 0 < per_tap < 0.98]
        doubled = [w for w, per_tap in results if per_tap > 1.02]
        if missed and doubled and max(missed) < min(doubled) and not inversions:
            print(f"  The crossover is between {max(missed)} ms and "
                  f"{min(doubled)} ms; sweep that gap finer.")
        elif doubled and min(doubled) == min(w for w, _ in results):
            # Even the shortest contact doubles: no width can fix this, and the
            # drive mode or a half-press that never opens is the real cause.
            print(f"  Even {min(doubled)} ms doubles, so no contact width will "
                  f"fix it.  Either the body is firing continuously while the "
                  f"contact is closed — check the drive dial, and whether S1 is "
                  f"left closed between taps — or the relay's own switching time "
                  f"is longer than anything asked for here, in which case the "
                  f"commanded width stopped mattering some time ago.")


if __name__ == "__main__":
    main()
