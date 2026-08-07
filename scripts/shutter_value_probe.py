"""Which of the dropdown's 64 shutter speeds does this body actually take?

On 5 August a speed picked from the live view dropdown came back 0x2003
"Invalid parameter combination".  The list is cut from the SDK's name table
because the X-T4 answers CapShutterSpeed with an empty list - and that table
is a mixed grid: third-stop values the command dial steps through, plus the
half-stop-only values (1/6000, 1/3000, 1/1500, 1/750, 1/350, 1/90, 1/45...)
other bodies use.  Which of them this body refuses is not written anywhere,
so this asks it: set each value, note the verdict, read back where the body
landed, restore the original.  No shutter fires and no GUI or scheduler is
involved.

Camera: shutter dial on T, drive as it will be on the day, USB connected.
Close the workbench first - the SDK takes one session.

    .venv/bin/python scripts/shutter_value_probe.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solareclipseworkbench.fuji_camera import (
    detect_fuji_cameras,
    find_fuji_sdk_path,
    maybe_reexec_for_fuji_sdk,
)

maybe_reexec_for_fuji_sdk()

from fujixsdk._constants import ERRCODE_COMBINATION, SHUTTER_SPEED_NAMES
from fujixsdk._errors import XSDKError
from solareclipseworkbench.liveview import dropdown_shutter_speeds

from bench_log import tee_console

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def _retry(action, budget_s: float = 1.5):
    """Run `action`, waiting out the transient busy the body answers with."""
    deadline = time.monotonic() + budget_s
    while True:
        try:
            return action()
        except XSDKError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def main() -> None:
    tee_console("shutter_value_probe")
    print("Camera: shutter dial on T, drive as on the day, USB connected.")
    print("Close the workbench first - the SDK takes one session.")
    if input("Ready? [y/n] > ").strip().lower() not in ("y", "yes"):
        return

    cameras = detect_fuji_cameras(find_fuji_sdk_path())
    if not cameras:
        print(f"{RED}SDK sees no camera - power cycle with the cable in, rerun.{RESET}")
        return
    name, camera = next(iter(cameras.items()))
    sdk_cam = camera._sdk_cam
    original, _bulb = sdk_cam.get_shutter_speed()
    print(f"{GREEN}Connected:{RESET} {name}, body on "
          f"{SHUTTER_SPEED_NAMES.get(original, original)}")

    # The manual says CapShutterSpeed answers for the current exposure mode
    # and shutter type; its answer here, mode already set, is the list the
    # dropdown should carry.  The set-loop below is the cross-check.
    try:
        cap = sorted(v for v in sdk_cam.get_supported_shutter_speeds() if v > 0)
    except XSDKError as exc:
        cap = []
        print(f"{YELLOW}CapShutterSpeed refused: {exc}{RESET}")
    if cap:
        print(f"CapShutterSpeed lists {len(cap)}: "
              + ", ".join(SHUTTER_SPEED_NAMES.get(v, str(v)) for v in cap))
    else:
        print(f"{YELLOW}CapShutterSpeed answers empty in this state{RESET}")

    records = []
    refused = []
    landed_off = []
    try:
        for value in dropdown_shutter_speeds():
            label = SHUTTER_SPEED_NAMES[value]
            verdict, code, read_back = "ok", None, None
            try:
                # Retried, not asked once.  The body answers 0x1006 for a
                # moment after a frame or a previous write, and a single
                # attempt records that as a refusal - which is how the first
                # run of this called 1/8000 unsupported, a speed the ladder
                # sets on every rung.
                _retry(lambda: sdk_cam.set_shutter_speed(value))
                read_back, _bulb = sdk_cam.get_shutter_speed()
                if read_back != value:
                    # Accepted in name only: the body said yes and sits on a
                    # different speed, which is worse than a refusal.
                    verdict = "landed elsewhere"
                    landed_off.append((label, SHUTTER_SPEED_NAMES.get(
                        read_back, read_back)))
            except XSDKError as exc:
                code = getattr(exc, "code", None)
                verdict = ("refused 0x2003" if code == ERRCODE_COMBINATION
                           else f"refused {exc}")
                refused.append(label)
            colour = GREEN if verdict == "ok" else (
                YELLOW if verdict == "landed elsewhere" else RED)
            print(f"  {label:>10s}  {colour}{verdict}{RESET}"
                  + (f" -> {SHUTTER_SPEED_NAMES.get(read_back, read_back)}"
                     if verdict == "landed elsewhere" else ""))
            records.append({"value": value, "label": label,
                            "verdict": verdict, "code": code,
                            "read_back": read_back})
            time.sleep(0.15)
    finally:
        try:
            sdk_cam.set_shutter_speed(original)
            print(f"Body restored to {SHUTTER_SPEED_NAMES.get(original, original)}")
        except XSDKError as exc:
            print(f"{RED}Could not restore {SHUTTER_SPEED_NAMES.get(original, original)}: "
                  f"{exc}{RESET}")
        camera.disconnect()

    log_path = Path.cwd() / f"shutter_value_probe_{int(time.time())}.json"
    log_path.write_text(json.dumps(records, indent=2))
    print(f"\n{len(records)} tried, {len(refused)} refused, "
          f"{len(landed_off)} landed elsewhere -> {log_path}")
    if refused:
        print(f"{RED}Refused:{RESET} " + ", ".join(refused))
    if landed_off:
        print(f"{YELLOW}Landed elsewhere:{RESET} "
              + ", ".join(f"{a} -> {b}" for a, b in landed_off))
    if not refused and not landed_off:
        print(f"{GREEN}Every value on the dropdown is real on this body.{RESET}")


if __name__ == "__main__":
    main()
