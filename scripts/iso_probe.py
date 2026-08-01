"""Does ISO set over USB apply with the ISO dial on C?

The drain rehearsal showed set_iso returning success while every frame stayed
at the dial's ISO — the physical dial wins unless it is on C, and C was never
tried.  Ten frames, one minute, rides the next card read: each ISO is set over
USB and a relay tap takes the evidence.  EXIF verdicts the dial.

Camera: ISO dial on C, drive CH, shutter dial T, USB connected.

    .venv/bin/python scripts/iso_probe.py
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

from bench_log import tee_console

GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

ISOS = [160, 800, 3200, 12800, 320]


def main() -> None:
    tee_console("iso_probe")
    print("Camera: ISO dial on C, drive CH, shutter dial T, USB connected.")
    if input("Ready? [y/n] > ").strip().lower() not in ("y", "yes"):
        return

    cameras = detect_fuji_cameras(find_fuji_sdk_path())
    if not cameras:
        print(f"{RED}SDK sees no camera — power cycle with the cable in, rerun.{RESET}")
        return
    name, camera = next(iter(cameras.items()))
    sdk_cam = camera._sdk_cam
    sdk_cam.get_buffer_capacity()  # proof of life
    print(f"{GREEN}Connected:{RESET} {name}")

    log_path = Path.cwd() / f"iso_probe_{int(time.time())}.json"
    records = []
    trigger = rt.open_trigger("auto", s1_channel=1, s2_channel=2)
    try:
        camera.configure(shutter_speed="1/500")
        trigger.half_press()
        for iso in ISOS:
            error = None
            try:
                camera.configure(iso=iso)
            except Exception as exc:
                error = str(exc)
            marker = f"{RED}{error}{RESET}" if error else f"{GREEN}ok{RESET}"
            print(f"  set ISO {iso:>6} {marker}; tap")
            started = time.time()
            trigger.shoot(pulse=0.08)
            records.append({"iso": iso, "error": error, "tapped_at": started})
            log_path.write_text(json.dumps(records, indent=2))
            time.sleep(1.5)
    finally:
        trigger.release_all()
        time.sleep(1.0)
        try:
            drained = sdk_cam.drain_buffer()
            print(f"drained {drained} frame(s); buffer clean")
        except Exception as exc:
            print(f"{RED}drain failed: {exc}{RESET}")
        trigger.close()
        try:
            camera.configure(iso=320)
            camera.exit()
            print(f"{GREEN}Session closed cleanly.{RESET}")
        except Exception:
            print(f"{RED}Camera did not close cleanly — power cycle it.{RESET}")

    print(f"\nLog: {log_path}")
    print("Verdict comes from EXIF at the next card read: frames should step")
    print("160, 800, 3200, 12800, 320.  If they all read the same, C did not help")
    print("and the eclipse runs at fixed ISO 320 with a shutter-only ramp.")


if __name__ == "__main__":
    main()
