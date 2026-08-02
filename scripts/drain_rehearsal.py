"""Full dress rehearsal for plan A: relay frames + USB ramp + a live drain loop.

Campaign two established that every frame taken while an SDK session is open
occupies a volatile-buffer slot until drained, and that 32 undrained slots wedge
the camera hard enough to need a battery pull.  fujixsdk's drain discards the
queued PC transfer with DeleteImage and *believes* the image is already on the
card — a claim July explicitly left unverified.

This deliberately shoots ~190 frames, six times the wedge threshold, with the
drain running.  One run, one card swap: taps, bursts, relay ramp, SDK singles,
the C2 handover, an ISO ramp, a three-ladder mini-totality, and battery drain
bookends.  It proves, or
disproves, in one sitting:

  - the drain keeps the body alive indefinitely under production fire
  - a drained frame still reaches the card (count them afterwards)
  - speed changes over USB land between relay-driven frames

Run locally in Terminal, camera on CH, shutter dial T, aimed at the clock:

    .venv/bin/python scripts/drain_rehearsal.py
"""

import json
import logging
import subprocess
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
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"

LADDER = ["1/1000", "1/250", "1/60", "1/15", "1/4"]


class Drainer:
    """Drains only in explicit quiet windows — never while anything is shooting.

    A previous run proved that a drain issued mid-shooting (S1 held, tap in
    flight) drops the USB session with 0x2001, permanently.  So the production
    rule under test here is: shoot, stop, drain, resume — and time every drain
    so the cost of the pause is a measured number.
    """

    def __init__(self, sdk_cam, trigger, log):
        self.sdk_cam = sdk_cam
        self.trigger = trigger
        self.log = log
        self.total_drained = 0
        self.peak = 0
        self.errors = 0

    def quiet_drain(self, label: str) -> bool:
        """Release everything, settle, then drain with the camera idle."""
        self.trigger.release_all()
        time.sleep(1.0)
        started = time.time()
        try:
            captured, total = self.sdk_cam.get_buffer_capacity()
            self.peak = max(self.peak, captured)
            drained = self.sdk_cam.drain_buffer()
            self.total_drained += drained
            took = time.time() - started
            per = took / drained if drained else 0.0
            self.log("drain", label=label, captured=captured, total=total,
                     drained=drained, took_s=round(took, 3),
                     per_frame_s=round(per, 4))
            print(f"    {DIM}drain[{label}]: {drained} frames in {took:.2f} s "
                  f"({per * 1000:.0f} ms/frame), was {captured}/{total}{RESET}")
            return True
        except Exception as exc:
            self.errors += 1
            self.log("drain_error", label=label, error=str(exc))
            print(f"    {RED}drain[{label}] failed: {exc}{RESET}")
            return False


class Tee:
    """Mirror a stream into the session log so nothing needs copy-pasting."""

    def __init__(self, stream, handle):
        self.stream = stream
        self.handle = handle

    def write(self, text):
        self.stream.write(text)
        self.handle.write(text)
        self.handle.flush()

    def flush(self):
        self.stream.flush()
        self.handle.flush()


def main() -> None:
    console_log = Path.cwd() / f"drain_rehearsal_{int(time.time())}.log"
    handle = open(console_log, "w")
    sys.stdout = Tee(sys.stdout, handle)
    sys.stderr = Tee(sys.stderr, handle)
    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("matplotlib", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    print(f"console log: {console_log}")
    print("Camera: drive CH, shutter dial T, aimed at the clock, USB connected.")
    print(f"{DIM}This takes ~190 frames with the drain loop live — three times the\n"
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

    # Detect can succeed on a stale stack and hand back a phantom handle whose
    # every call fails 0x2001 — one dead run clicked through 100 frames before
    # anyone noticed.  A buffer poll is the cheapest proof of life.
    try:
        captured, total = sdk_cam.get_buffer_capacity()
    except Exception as exc:
        print(f"{RED}Connected but the session is dead ({exc}).{RESET}")
        print("Power cycle the camera with the cable in and rerun.  If it repeats,")
        print("kill ptpcamerad/mscamerad-xpc and power cycle again.")
        try:
            camera.exit()
        except Exception:
            pass
        return
    print(f"{GREEN}Connected:{RESET} {name}  buffer {captured}/{total}  "
          f"{DIM}(no priority calls){RESET}")
    note("connected", camera=name, buffer=[captured, total])

    try:
        level, _, _ = sdk_cam.get_battery_info()
        note("battery", level=level, when="start")
        print(f"{DIM}battery at start: {level}%{RESET}")
    except Exception:
        note("battery", level=None, when="start")

    trigger = rt.open_trigger("auto", s1_channel=1, s2_channel=2)
    drain = Drainer(sdk_cam, trigger, note)

    try:
        print(f"\n{DIM}phase 1: twelve 80 ms taps with a quiet drain after every six{RESET}")
        trigger.half_press()
        for tap in range(12):
            started = time.time()
            trigger.shoot(pulse=0.08)
            note("tap", n=tap + 1, started=started)
            time.sleep(2.0)
            if (tap + 1) % 6 == 0:
                if not drain.quiet_drain(f"after_tap_{tap + 1}"):
                    raise RuntimeError("session lost during quiet drain")
                trigger.half_press()

        print(f"{DIM}phase 2: two 3 s bursts — the beads pattern, ~35 frames each{RESET}")
        for burst in range(2):
            started = time.time()
            with trigger.pressed():
                time.sleep(3.0)
            note("burst", n=burst + 1, started=started)
            time.sleep(2.0)
            if not drain.quiet_drain(f"after_burst_{burst + 1}"):
                raise RuntimeError("session lost during quiet drain")

        print(f"{DIM}phase 3: the corona pattern — speed ramp over USB, taps between{RESET}")
        trigger.half_press()
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
        if not drain.quiet_drain("after_ramp"):
            raise RuntimeError("session lost during quiet drain")

        print(f"{DIM}phase 4: the hybrid pattern — SDK-triggered singles, speeds over USB{RESET}")
        for speed in ("1/500", "1/30", "1/500", "1/30", "1/500"):
            started = time.time()
            error = None
            try:
                camera.configure(shutter_speed=speed)
                camera.capture()
            except Exception as exc:
                error = str(exc)
            note("sdk_shot", speed=speed, error=error, started=started,
                 took_s=round(time.time() - started, 3))
            marker = f"{RED}{error}{RESET}" if error else f"{GREEN}ok{RESET}"
            print(f"    sdk shot at {speed:>6} {marker}  "
                  f"{DIM}{time.time() - started:.2f} s{RESET}")
            time.sleep(0.8)

        print(f"{DIM}phase 5: relay burst straight after SDK shots — the C2 handover{RESET}")
        started = time.time()
        with trigger.pressed():
            time.sleep(2.0)
        note("handover_burst", started=started)

        print(f"{DIM}phase 6: ISO over USB — the other ramp axis, never yet tested{RESET}")
        drain.quiet_drain("before_iso")
        for iso in (160, 800, 3200, 320):
            started = time.time()
            error = None
            try:
                camera.configure(iso=iso, shutter_speed="1/500")
                camera.capture()
            except Exception as exc:
                error = str(exc)
            note("iso_shot", iso=iso, error=error, started=started)
            marker = f"{RED}{error}{RESET}" if error else f"{GREEN}ok{RESET}"
            print(f"    ISO {iso:>5} {marker}")
            time.sleep(0.8)

        print(f"{DIM}phase 7: mini-totality — C2 burst, three SDK ladders, C3 burst{RESET}")
        started = time.time()
        with trigger.pressed():
            time.sleep(2.5)
        note("totality_c2_burst", started=started)
        time.sleep(2.0)
        for round_no in range(3):
            for speed in ("1/1000", "1/125", "1/15", "1/4", "1"):
                shot_at = time.time()
                error = None
                try:
                    camera.configure(shutter_speed=speed)
                    camera.capture()
                except Exception as exc:
                    error = str(exc)
                note("totality_ladder_shot", round=round_no + 1, speed=speed,
                     error=error, started=shot_at)
                if error:
                    print(f"    {RED}round {round_no + 1} {speed}: {error}{RESET}")
            if not drain.quiet_drain(f"totality_round_{round_no + 1}"):
                raise RuntimeError("session lost during quiet drain")
        started = time.time()
        with trigger.pressed():
            time.sleep(2.5)
        note("totality_c3_burst", started=started)
        time.sleep(2.0)
        drain.quiet_drain("after_c3")

        try:
            camera.configure(shutter_speed="1/125", iso=320)
        except Exception:
            pass

        try:
            level, _, _ = sdk_cam.get_battery_info()
            note("battery", level=level, when="end")
            print(f"{DIM}battery at end: {level}%{RESET}")
        except Exception:
            note("battery", level=None, when="end")

        heard = input("\nDid it fire throughout? Anything odd? > ").strip()
        note("observation", text=heard)

    finally:
        trigger.release_all()
        trigger.close()
        time.sleep(1.0)
        drain.quiet_drain("final")
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
