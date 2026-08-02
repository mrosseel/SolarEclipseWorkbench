"""Fire brackets at the X-T4 and report what the ladder actually was.

No GUI, no scheduler, no eclipse arithmetic: connect, run ``take_bracket`` at a
handful of base speeds, and print the ladder that was built against the frames
that came back.  That is the whole question — a bracket that returns short looks
the same in the images as one that was never asked for.

The frame count comes from the transfer queue, which holds every frame taken
with a session open until it is drained, so nothing has to be read off the card.

    ./run.sh scripts/probe_bracket.py
    ./run.sh scripts/probe_bracket.py --steps "+/- 1"
"""

import argparse
import logging
import sys
import time
from pathlib import Path

# fujixsdk lives in the repo root, which is not on sys.path when this file is
# run as scripts/probe_bracket.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solareclipseworkbench.camera import CameraSettings, take_bracket
from solareclipseworkbench.fuji_camera import (detect_fuji_cameras, find_fuji_sdk_path,
                                               maybe_reexec_for_fuji_sdk)
from solareclipseworkbench.hardware_registry import register_hardware
from solareclipseworkbench.relay_trigger import open_trigger

maybe_reexec_for_fuji_sdk()

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m")

# One fast, one middling, one slow.  The slow rung is where a decimal-second
# speed has to survive the round trip into an SDK constant.
DEFAULT_SPEEDS = ["1/200", "1/25", "0.5"]

SETTLE_S = 3.0


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


def queue(camera):
    try:
        return camera._sdk_cam.get_buffer_capacity()
    except Exception as exc:
        print(f"  {YELLOW}queue unreadable: {exc}{RESET}")
        return (None, None)


def probe(camera, speed: str, iso: int, steps: str) -> None:
    _, total = queue(camera)
    print(f"\n{BOLD}base {speed}  ISO {iso}  {steps}{RESET}")

    started = time.time()
    try:
        taken = take_bracket(camera, CameraSettings(camera.name, speed, "-", iso), steps)
    except Exception as exc:
        print(f"  {RED}take_bracket raised: {exc}{RESET}")
        return
    elapsed = time.time() - started

    # The count comes from take_bracket, not from the queue: the bracket drains
    # the queue before it returns, so reading it here reported 0 however much
    # was shot — which is what it did until 2 August.
    print(f"  {taken} tap(s) in {elapsed:.2f} s, buffer {total} slot(s)")

    try:
        camera.drain()
    except Exception as exc:
        print(f"  {YELLOW}drain failed: {exc}{RESET}")
    time.sleep(SETTLE_S)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--steps", default="+/- 2",
                        help="bracket width as an eclipse script writes it (default: +/- 2)")
    parser.add_argument("--iso", type=int, default=400)
    parser.add_argument("--speeds", nargs="*", default=DEFAULT_SPEEDS,
                        help="base shutter speeds to bracket around")
    parser.add_argument("--no-relay", action="store_true",
                        help="force the SDK shooter even if a relay is attached")
    args = parser.parse_args()

    # The ladder and the frame count are logged by take_bracket itself; without
    # this they go nowhere on a terminal run.
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

    print(f"{BOLD}Bracket probe{RESET}")
    print(f"{DIM}drive dial anywhere, focus and shutter on manual, RAW only.{RESET}")
    print(f"{DIM}Each bracket is drained afterwards, so the queue starts empty every time.{RESET}\n")

    # Which shooter runs decides what is being measured: with a relay attached
    # the frames come from taps on the release jack, without one from the SDK.
    # The GUI and sew.py both register the relay, so a probe that skips it tests
    # a path the eclipse never takes.
    if not args.no_relay:
        try:
            register_hardware('relay', open_trigger('auto', s1_channel=1, s2_channel=2))
        except Exception as exc:
            print(f"{YELLOW}No relay ({exc}) — falling back to the SDK shooter, "
                  f"which is not the path the eclipse script uses.{RESET}")

    camera = connect()
    if camera is None:
        return

    print(f"{DIM}shooter: {type(camera.shooter).__name__}{RESET}")

    captured, total = queue(camera)
    print(f"{DIM}queue at rest: {captured}/{total}{RESET}")
    if captured:
        camera.drain()

    try:
        for speed in args.speeds:
            probe(camera, speed, args.iso, args.steps)
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Interrupted.{RESET}")
    finally:
        try:
            camera.drain()
            camera.disconnect()
        except Exception:
            pass

    print(f"\n{DIM}A ladder of one or two speeds is parse_bracket_speeds; a full "
          f"ladder with fewer frames is the shooter.{RESET}")


if __name__ == "__main__":
    main()
