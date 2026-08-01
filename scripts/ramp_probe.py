"""The one missing experiment: USB exposure ramp while the relay fires the jack.

Campaign two proved the jack works with an SDK session open — a 24-frame burst
at 8 fps — right up until a SetPriorityMode call (which itself failed with
"camera busy") killed the jack for the rest of the session.  So: connect, touch
NOTHING but shutter speed, and fire the relay between changes.  If frames land
with the commanded exposures, plan A (dial on CH, exposure ramped over USB,
frames from the relay) is proven end to end.

Run locally in Terminal, camera aimed at the clock, drive CH, shutter dial T:

    .venv/bin/python scripts/ramp_probe.py
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
from solareclipseworkbench import relay_trigger as rt

maybe_reexec_for_fuji_sdk()

GREEN = "\033[32m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"

# Distinct enough that EXIF alone says which command each frame obeyed.
LADDER = ["1/1000", "1/250", "1/60", "1/15", "1/4"]

log_path = Path.cwd() / f"ramp_probe_{int(time.time())}.json"
records = []


def note(action, **detail):
    records.append({"action": action, "at": time.time(), **detail})
    log_path.write_text(json.dumps(records, indent=2))


def main() -> None:
    print("Camera: drive CH, shutter dial T, aimed at the clock, USB connected.")
    if input("Ready? [y/n] > ").strip().lower() not in ("y", "yes"):
        return

    cameras = detect_fuji_cameras(find_fuji_sdk_path())
    if not cameras:
        print(f"{RED}SDK sees no camera — power cycle, replug, retry.{RESET}")
        return
    name, camera = next(iter(cameras.items()))
    print(f"{GREEN}Connected:{RESET} {name}   {DIM}(no priority call will be made){RESET}")
    note("connected", camera=name)

    trigger = rt.open_trigger("auto", s1_channel=1, s2_channel=2)
    try:
        print(f"\n{DIM}baseline: two relay taps before any USB command{RESET}")
        for _ in range(2):
            started = time.time()
            trigger.shoot(pulse=0.05)
            note("baseline_tap", started=started)
            time.sleep(2.0)

        for speed in LADDER:
            started = time.time()
            error = None
            try:
                camera.configure(shutter_speed=speed)
            except Exception as exc:
                error = str(exc)
            note("set_speed", speed=speed, error=error, started=started)
            marker = f"{RED}{error}{RESET}" if error else f"{GREEN}ok{RESET}"
            print(f"  set {speed:>7} {marker}; two taps")
            for _ in range(2):
                started = time.time()
                trigger.shoot(pulse=0.05)
                note("ramp_tap", speed=speed, started=started)
                time.sleep(1.5)

        heard = input("\nDid the camera fire throughout? What did you hear? > ").strip()
        note("observation", text=heard)
    finally:
        trigger.close()
        try:
            camera.exit()
        except Exception:
            print(f"{RED}Camera did not close cleanly — power cycle it.{RESET}")

    print(f"\nDone — log at {log_path}")
    print("If the camera fired at every step, plan A is proven.")


if __name__ == "__main__":
    main()
