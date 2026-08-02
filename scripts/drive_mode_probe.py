"""Ask whether the SDK can take the drive mode off the dial.

The eclipse needs CH on the drive dial: a relay burst holds the contacts closed
and lets the body free-run at 15 fps, which is how Baily's beads are caught.  But
CH is also why a bracket tap fires twice at a fast shutter — the body keeps
shooting for as long as the contact is closed, and the relay cannot close it
briefly enough to guarantee one frame (5-20 ms never reached the body at all,
25-40 ms fired anywhere between 0.2 and 1.6 frames per tap).

If XSDK_SetDriveMode can put the body on Single for the length of a bracket and
hand it back to CH afterwards, both phases get what they need from one dial
position.  The X-T4's drive dial is mechanical, so it may well refuse.  This
probe asks the body directly rather than guessing.

    ./run.sh scripts/drive_mode_probe.py
"""

import argparse
import logging
import sys
import time
from pathlib import Path

# fujixsdk lives in the repo root, which is not on sys.path when this file is
# run as scripts/drive_mode_probe.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fujixsdk._constants import DRIVE_MODE_CH, DRIVE_MODE_NAMES, DRIVE_MODE_S
from fujixsdk._errors import BusyError
from solareclipseworkbench.fuji_camera import (detect_fuji_cameras, find_fuji_sdk_path,
                                               maybe_reexec_for_fuji_sdk)
from solareclipseworkbench.hardware_registry import register_hardware
from solareclipseworkbench.relay_trigger import open_trigger

maybe_reexec_for_fuji_sdk()

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m")

# The width the eclipse path uses today, and the one that doubles in CH.  If
# Single takes, this same tap must produce exactly one frame.
TAP_S = 0.08
FAST_SPEED = "1/1000"
TAPS = 5
GAP_S = 0.6


# A body that has just opened a session is still busy for about a second, and a
# probe that reads 0x1006 as "refused" answers the wrong question — which is
# exactly what this script did on its first run.
BUSY_WAIT_S = 5.0
BUSY_BACKOFF_S = 0.25


def name_of(mode: int) -> str:
    return DRIVE_MODE_NAMES.get(mode, f"0x{mode:04X}")


def through_busy(action, deadline_s: float = BUSY_WAIT_S):
    """Run a call, waiting out 0x1006 for up to `deadline_s`.

    Returns (result, seconds_waited).  Busy is not an answer, it is the body
    asking to be asked again.
    """
    deadline = time.monotonic() + deadline_s
    started = time.monotonic()
    while True:
        try:
            return action(), time.monotonic() - started
        except BusyError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(BUSY_BACKOFF_S)


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


def read_mode(sdk_cam):
    try:
        return sdk_cam.get_drive_mode()
    except Exception as exc:
        print(f"  {YELLOW}drive mode unreadable: {exc}{RESET}")
        return None


def drain_fully(camera) -> int:
    total = 0
    for _ in range(4):
        drained = camera.drain()
        total += drained
        if drained == 0:
            break
    return total


def count_frames(camera, relay) -> float:
    """Fire TAPS taps at the eclipse's own width, returning frames per tap."""
    drain_fully(camera)
    relay.half_press()
    try:
        for _ in range(TAPS):
            relay.shoot(pulse=TAP_S)
            time.sleep(GAP_S)
    finally:
        relay.release_all()
    return drain_fully(camera) / TAPS


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--speed", default=FAST_SPEED,
                        help=f"shutter speed to measure at (default: {FAST_SPEED})")
    parser.add_argument("--iso", type=int, default=400)
    parser.add_argument("--no-shoot", action="store_true",
                        help="only ask whether the mode can be set, fire nothing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

    print(f"{BOLD}Drive mode probe{RESET}")
    print(f"{DIM}leave the drive dial on CH — that is the whole point.{RESET}\n")

    camera = connect()
    if camera is None:
        return
    sdk_cam = camera._sdk_cam

    # The session opens with the body still finishing its own startup, and every
    # write in that window comes back busy.  Wait it out before asking anything.
    try:
        through_busy(sdk_cam.get_buffer_capacity)
    except Exception:
        pass

    before = read_mode(sdk_cam)
    print(f"  drive mode reads: {name_of(before) if before is not None else 'unreadable'}"
          f"  {DIM}(the SDK cannot tell CH from Single on this body — trust the "
          f"dial, not this){RESET}")

    # 1. Can Single be written at all?
    print(f"\n{BOLD}Setting Single{RESET}")
    try:
        _, waited = through_busy(lambda: sdk_cam.set_drive_mode(DRIVE_MODE_S))
        print(f"  {GREEN}SetDriveMode(Single) accepted{RESET}"
              + (f" {DIM}after waiting {waited:.1f}s for the body{RESET}"
                 if waited > 0.05 else ""))
    except BusyError:
        # Busy for five seconds with nothing else happening is not a passing
        # state; this body will not take the write.
        print(f"  {RED}SetDriveMode(Single) still busy after {BUSY_WAIT_S:.0f}s{RESET}")
        print(f"\n{YELLOW}The dial wins.{RESET}  The body refuses the write for as "
              f"long as it is asked, which on a mechanical dial means it is not "
              f"going to take it.")
        _explain_fallback()
        return
    except Exception as exc:
        print(f"  {RED}SetDriveMode(Single) refused: {exc}{RESET}")
        print(f"\n{YELLOW}The dial wins.{RESET}  Brackets cannot be shot on Single "
              f"while the eclipse needs CH, so the doubling has to be handled "
              f"rather than prevented — see the note at the end.")
        _explain_fallback()
        return

    # 2. Did it stick, or was it accepted and ignored?
    after = read_mode(sdk_cam)
    print(f"  reads back as: {name_of(after) if after is not None else 'unreadable'}")
    if after is not None and before is not None and after == before:
        print(f"  {YELLOW}Accepted but unchanged — the SDK reports success and the "
              f"body kept the dial.{RESET}")

    # 3. The only answer that counts: does one tap now make one frame?
    if not args.no_shoot:
        try:
            register_hardware('relay', open_trigger('auto', s1_channel=1, s2_channel=2))
        except Exception as exc:
            print(f"\n{RED}No relay ({exc}) — cannot test what the body actually "
                  f"does.{RESET}")
            return
        relay = camera.relay
        try:
            camera.configure(shutter_speed=args.speed, iso=args.iso)
        except Exception as exc:
            print(f"  {YELLOW}Could not set {args.speed}: {exc}{RESET}")

        print(f"\n{BOLD}Firing {TAPS} taps of {TAP_S * 1000:.0f} ms at {args.speed}{RESET}")
        per_tap = count_frames(camera, relay)
        print(f"  {per_tap:.2f} frame(s) per tap")
        if per_tap <= 1.02:
            print(f"\n{GREEN}Single takes and holds.{RESET}  A bracket can switch to "
                  f"Single, shoot, and hand the body back to CH for the bursts.")
        else:
            print(f"\n{YELLOW}Still free-running.{RESET}  The write was accepted but "
                  f"the body is behaving as CH regardless.")
            _explain_fallback()

    # 4. Whatever happened, give the dial back — the next burst depends on it.
    print(f"\n{BOLD}Restoring CH{RESET}")
    try:
        through_busy(lambda: sdk_cam.set_drive_mode(DRIVE_MODE_CH))
        print(f"  now reads: {name_of(read_mode(sdk_cam))}")
    except Exception as exc:
        print(f"  {RED}could not restore: {exc}{RESET}")
        print(f"  {YELLOW}Check the drive dial by hand before shooting.{RESET}")


def _explain_fallback() -> None:
    print(f"{DIM}\nThen the choice is between shooting brackets over the SDK with "
          f"the dial on Single and losing relay bursts, or keeping CH and "
          f"accepting ~2 frames per bracket rung — correct exposures, duplicated, "
          f"at twice the buffer cost.{RESET}")


if __name__ == "__main__":
    main()
