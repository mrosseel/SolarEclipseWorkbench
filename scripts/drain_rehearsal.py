"""Full dress rehearsal for plan A: relay frames + USB ramp + a live drain loop.

Campaign two established that every frame taken while an SDK session is open
occupies a volatile-buffer slot until drained, and that 32 undrained slots wedge
the camera hard enough to need a battery pull.  fujixsdk's drain discards the
queued PC transfer with DeleteImage and *believes* the image is already on the
card — a claim July explicitly left unverified.

This deliberately shoots ~90 frames, three times the wedge threshold, with the
drain running, while walking the exposure ladder over USB.  It proves, or
disproves, in one sitting:

  - the drain keeps the body alive indefinitely under production fire
  - a drained frame still reaches the card (count them afterwards)
  - speed changes over USB land between relay-driven frames

Run locally in Terminal, camera on CH, shutter dial T, aimed at the clock:

    .venv/bin/python scripts/drain_rehearsal.py
"""

import json
import subprocess
import sys
import threading
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
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"

LADDER = ["1/1000", "1/250", "1/60", "1/15", "1/4"]
DRAIN_AT_FRACTION = 0.5
DRAIN_POLL_S = 0.4


class DrainLoop(threading.Thread):
    """Keeps the volatile buffer below the wedge line, recording every level seen."""

    def __init__(self, sdk_cam, log):
        super().__init__(daemon=True)
        self.sdk_cam = sdk_cam
        self.log = log
        self.stop_flag = threading.Event()
        self.total_drained = 0
        self.peak = 0
        self.errors = 0

    def run(self) -> None:
        while not self.stop_flag.is_set():
            try:
                captured, total = self.sdk_cam.get_buffer_capacity()
                self.peak = max(self.peak, captured)
                if total > 0 and captured >= total * DRAIN_AT_FRACTION:
                    drained = self.sdk_cam.drain_buffer()
                    self.total_drained += drained
                    self.log("drain", captured=captured, total=total, drained=drained)
            except Exception as exc:
                self.errors += 1
                self.log("drain_error", error=str(exc))
                time.sleep(1.0)
            time.sleep(DRAIN_POLL_S)

    def final_drain(self) -> None:
        """Empty the buffer completely so the session can close and the body power off."""
        try:
            drained = self.sdk_cam.drain_buffer()
            self.total_drained += drained
            self.log("final_drain", drained=drained)
        except Exception as exc:
            self.log("final_drain_error", error=str(exc))


def main() -> None:
    print("Camera: drive CH, shutter dial T, aimed at the clock, USB connected.")
    print(f"{DIM}This takes ~90 frames with the drain loop live — three times the\n"
          f"wedge threshold on purpose.{RESET}")
    if input("Ready? [y/n] > ").strip().lower() not in ("y", "yes"):
        return

    log_path = Path.cwd() / f"drain_rehearsal_{int(time.time())}.json"
    records = []

    def note(action, **detail):
        records.append({"action": action, "at": time.time(), **detail})
        log_path.write_text(json.dumps(records, indent=2))

    cameras = detect_fuji_cameras(find_fuji_sdk_path())
    if not cameras:
        print(f"{RED}SDK sees no camera.{RESET}")
        return
    name, camera = next(iter(cameras.items()))
    sdk_cam = camera._sdk_cam
    print(f"{GREEN}Connected:{RESET} {name}  {DIM}(no priority calls){RESET}")
    note("connected", camera=name)

    drain = DrainLoop(sdk_cam, note)
    drain.start()
    trigger = rt.open_trigger("auto", s1_channel=1, s2_channel=2)

    try:
        print(f"\n{DIM}phase 1: twelve 80 ms taps, 2 s apart (CH pairs ~24 frames){RESET}")
        trigger.half_press()
        for tap in range(12):
            started = time.time()
            trigger.shoot(pulse=0.08)
            note("tap", n=tap + 1, started=started)
            time.sleep(2.0)

        print(f"{DIM}phase 2: two 3 s bursts — the beads pattern, ~35 frames each{RESET}")
        for burst in range(2):
            started = time.time()
            with trigger.pressed():
                time.sleep(3.0)
            note("burst", n=burst + 1, started=started)
            print(f"    burst {burst + 1} done, buffer peak so far {drain.peak}")
            time.sleep(6.0)

        print(f"{DIM}phase 3: the corona pattern — speed ramp over USB, taps between{RESET}")
        for speed in LADDER:
            started = time.time()
            error = None
            try:
                camera.configure(shutter_speed=speed)
            except Exception as exc:
                error = str(exc)
            note("set_speed", speed=speed, error=error, started=started)
            marker = f"{RED}{error}{RESET}" if error else f"{GREEN}ok{RESET}"
            print(f"    set {speed:>7} {marker}")
            for _ in range(2):
                started = time.time()
                trigger.shoot(pulse=0.08)
                note("ramp_tap", speed=speed, started=started)
                time.sleep(1.2)

        try:
            camera.configure(shutter_speed="1/125")
        except Exception:
            pass

        heard = input("\nDid it fire throughout? Anything odd? > ").strip()
        note("observation", text=heard)

    finally:
        trigger.release_all()
        trigger.close()
        drain.stop_flag.set()
        drain.join(timeout=5.0)
        drain.final_drain()
        try:
            captured, total = sdk_cam.get_buffer_capacity()
            note("buffer_at_exit", captured=captured, total=total)
            print(f"\nbuffer at exit: {captured}/{total}  "
                  f"drained in total: {drain.total_drained}  peak: {drain.peak}  "
                  f"drain errors: {drain.errors}")
        except Exception:
            pass
        try:
            camera.exit()
            print(f"{GREEN}Session closed cleanly — the camera should power off normally.{RESET}")
        except Exception:
            print(f"{YELLOW}Camera did not close cleanly.{RESET}")

    note("done", drained=drain.total_drained, peak=drain.peak, errors=drain.errors)
    print(f"\nLog: {log_path}")
    print("\nNow the verification that decides plan A: put the card in the reader and")
    print("count the frames from this run.  Every commanded frame must be there —")
    print("if drained frames are missing from the card, the drain is a shredder,")
    print("not a queue-clearer, and plan A is dead.")
    subprocess.run(["say", "rehearsal finished"], check=False)


if __name__ == "__main__":
    main()
