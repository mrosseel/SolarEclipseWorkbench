"""The USB half of campaign two, standalone: blocks 9 to 11 and nothing else.

For when the relay-only blocks are already on the card and the SDK connection
needs coaxing.  Answers the questions the production script's whole shape
depends on:

   9. does the release jack still fire while the SDK holds a session — in both
      priority modes, since a July run suggested PC priority may kill it
  10. does a shutter speed set over USB reach the camera between relay frames
  11. can the speed be walked mid-burst, the way totality would need it

Connection failures are diagnosed in order: is the body even enumerating on
USB (cable, power, PC CONNECTION MODE menu), then can the SDK open it.  Each
stage offers a retry, so a replug or power cycle does not mean starting over.

    .venv/bin/python scripts/campaign_sdk_only.py
"""

import json
import subprocess
import time
from pathlib import Path

from solareclipseworkbench.fuji_camera import (
    detect_fuji_cameras,
    find_fuji_sdk_path,
    maybe_reexec_for_fuji_sdk,
)
from solareclipseworkbench import relay_trigger as rt

maybe_reexec_for_fuji_sdk()

BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"

SPEED_LADDER = ["1/1000", "1/250", "1/60", "1/15", "1/4", "1"]


class Campaign:
    """Relay and SDK together, every command stamped for later EXIF matching."""

    def __init__(self, trigger: rt.RelayTrigger, log_path: Path):
        self.trigger = trigger
        self.camera = None
        self.log_path = log_path
        self.records: list = []
        self.notes: dict = {}
        self.block = "preflight"

    def add(self, action: str, wall_start: float, wall_end: float, **detail) -> None:
        self.records.append({
            "block": self.block, "action": action,
            "wall_start": wall_start, "wall_end": wall_end,
            "duration_s": round(wall_end - wall_start, 4), **detail,
        })
        self.log_path.write_text(json.dumps(
            {"notes": self.notes, "records": self.records}, indent=2))

    def pulse(self, pulse_ms: int = 80, label: str = "pulse") -> None:
        started = time.time()
        self.trigger.shoot(pulse=pulse_ms / 1000.0)
        self.add(label, started, time.time(), pulse_ms=pulse_ms)

    def hold(self, seconds: float, label: str = "hold") -> None:
        started = time.time()
        with self.trigger.pressed():
            s2_at = time.time()
            time.sleep(seconds)
        ended = time.time()
        self.add(label, started, ended, hold_s=seconds, s2_closed_at=s2_at)
        print(f"    {DIM}S2 held {ended - s2_at:.3f} s{RESET}")

    def set_speed(self, speed: str) -> bool:
        started = time.time()
        error = None
        try:
            self.camera.configure(shutter_speed=speed)
        except Exception as exc:
            error = str(exc)
        ended = time.time()
        self.add("set_shutter_speed", started, ended, speed=speed, error=error)
        marker = f"{RED}failed: {error}{RESET}" if error else f"{GREEN}ok{RESET}"
        print(f"    set {speed:>7}  {(ended - started) * 1000:6.1f} ms  {marker}")
        return error is None

    def clock_sync(self, label: str) -> None:
        """Frames whose photographed digits pin the camera-to-laptop clock offset."""
        print(f"    {DIM}{label}: 3 frames{RESET}", flush=True)
        for index in range(3):
            self.pulse(80, label=label)
            if index < 2:
                time.sleep(2.0)

    def begin(self, number: int, title: str, setup: list) -> bool:
        self.block = f"{number}:{title}"
        print(f"\n{BOLD}{'=' * 70}{RESET}")
        print(f"{BOLD}Block {number} — {title}{RESET}\n")
        print(f"{CYAN}Set on the camera:{RESET}")
        for line in setup:
            print(f"  - {line}")
        if input(f"\n  Enter when set, 's' to skip > ").strip().lower() == "s":
            print(f"  {YELLOW}skipped{RESET}")
            return False
        time.sleep(6.0)
        self.pulse(80, label="slate")
        time.sleep(2.0)
        return True

    def observation(self) -> None:
        seen = input("\n  What did you see or hear?  (Enter for nothing notable) > ").strip()
        if seen:
            self.notes[self.block] = seen
            self.log_path.write_text(json.dumps(
                {"notes": self.notes, "records": self.records}, indent=2))


def say(text: str) -> None:
    subprocess.run(["say", text], check=False)


def usb_sees_fuji() -> bool:
    """Whether the body enumerates on the USB bus at all, SDK aside."""
    try:
        result = subprocess.run(["system_profiler", "SPUSBDataType"],
                                capture_output=True, text=True, timeout=20)
        return "fuji" in result.stdout.lower()
    except Exception:
        return False


def connect_camera():
    """Get the body enumerating, then get the SDK holding it — with retries."""
    while not usb_sees_fuji():
        print(f"\n{RED}The X-T4 is not on the USB bus at all{RESET} — this is not an "
              "SDK problem yet.")
        print("  - camera powered on?")
        print("  - MENU > wrench > CONNECTION SETTING > PC CONNECTION MODE")
        print("    must be USB TETHER SHOOTING (AUTO or FIXED), not card reader")
        print("  - try another cable or port; hubs have caused trouble before")
        print("  - if it was connected earlier and wedged: power cycle AND replug")
        if input(f"\n  Fixed?  Enter to re-check, 'q' to give up > ").strip().lower() == "q":
            return None
    print(f"{GREEN}The body enumerates on USB.{RESET}")

    sdk_path = find_fuji_sdk_path()
    if not sdk_path:
        print(f"{RED}No Fuji SDK found.{RESET}  Set FUJI_SDK_PATH and rerun.")
        return None
    print(f"{DIM}SDK at {sdk_path}{RESET}")

    while True:
        cameras = detect_fuji_cameras(sdk_path)
        if cameras:
            name, camera = next(iter(cameras.items()))
            print(f"{GREEN}Connected:{RESET} {name}")
            return camera
        print(f"\n{RED}USB sees the body but the SDK cannot open it.{RESET}")
        print("  Power cycle the camera, replug USB, wait for the screen to come back.")
        if input(f"\n  Enter to retry, 'q' to give up > ").strip().lower() == "q":
            return None


def run_sdk_blocks(run: Campaign) -> None:
    if run.begin(9, "does the jack still fire with a USB session open", [
        "DRIVE dial on CH, mechanical, shutter 1/2000 — as the relay blocks left it",
    ]):
        say("block nine, does the jack still fire")
        for mode_name in ("CAMERA", "PC"):
            error = None
            try:
                from fujixsdk._constants import PRIORITY_CAMERA, PRIORITY_PC
                run.camera._sdk_cam.set_priority(
                    PRIORITY_CAMERA if mode_name == "CAMERA" else PRIORITY_PC)
            except Exception as exc:
                error = str(exc)
            run.add("set_priority", time.time(), time.time(),
                    mode=mode_name, error=error)
            if error:
                print(f"  {YELLOW}priority {mode_name} failed: {error}{RESET}")
            print(f"\n  {DIM}priority {mode_name}: three singles, then a 3 s burst{RESET}")
            for _ in range(3):
                run.pulse(80, label=f"relay_shot_prio_{mode_name.lower()}")
                time.sleep(3.0)
            run.hold(3.0, label=f"relay_burst_prio_{mode_name.lower()}")
            run.observation()
        # Leave the body in camera priority, the mode the jack is believed to like.
        try:
            from fujixsdk._constants import PRIORITY_CAMERA
            run.camera._sdk_cam.set_priority(PRIORITY_CAMERA)
        except Exception:
            pass

    if run.begin(10, "shutter speed set over USB, frames driven by the relay", [
        "shutter speed dial to T — the SDK sets speeds from here on, as it must\n"
        "    during the eclipse when no dial can be touched",
    ]):
        say("block ten, exposure ramp")
        print(f"\n  {DIM}each speed is set, then two frames fired 500 ms apart.{RESET}")
        print(f"  {DIM}EXIF then says which frame the change actually reached.{RESET}\n")
        for speed in SPEED_LADDER:
            if not run.set_speed(speed):
                continue
            for _ in range(2):
                run.pulse(80, label="ramp_frame")
                time.sleep(0.5)
            time.sleep(1.5)
        run.observation()

    if run.begin(11, "ramping mid-burst, the way totality would need it", [
        "nothing to change",
    ]):
        say("block eleven, ramping mid burst")
        print(f"\n  {DIM}S1 stays closed throughout; S2 pulses while the speed moves{RESET}")
        started = time.time()
        run.trigger.half_press()
        try:
            for speed in SPEED_LADDER:
                run.set_speed(speed)
                for _ in range(3):
                    pulse_at = time.time()
                    run.trigger.shoot(pulse=0.05)
                    run.add("ramp_burst_frame", pulse_at, time.time(), speed=speed)
                    time.sleep(0.35)
        finally:
            run.trigger.release_all()
        run.add("ramp_burst_total", started, time.time())
        run.observation()


def main() -> None:
    print(f"\n{BOLD}{'=' * 70}{RESET}")
    print(f"{BOLD}X-T4 campaign two, USB half only — blocks 9 to 11{RESET}\n")
    print(f"{CYAN}Before starting:{RESET}")
    print("  - the relay blocks already ran; this adds only the SDK questions")
    print("  - relay wired to the release jack, camera aimed at the clock page")
    print("  - RAW only, manual focus, DRIVE on CH, mechanical, shutter 1/2000")
    print("  - USB cable connected")
    print(f"\n{CYAN}Expect{RESET} roughly 10 minutes and 60 frames.")
    if input(f"\n  Ready?  [y/n] > ").strip().lower() not in ("y", "yes"):
        print("Nothing done.")
        return

    log_path = Path.cwd() / f"campaign_sdkonly_{int(time.time())}.json"
    trigger = rt.open_trigger("auto", s1_channel=1, s2_channel=2)
    run = Campaign(trigger, log_path)
    print(f"\n{trigger.describe()}\nlogging to {log_path}")

    try:
        run.clock_sync("clock_sync_start")

        run.camera = connect_camera()
        if run.camera is None:
            print(f"{YELLOW}No SDK session — nothing to test.  The sync frames are on "
                  f"the card; the log is saved.{RESET}")
            return

        run_sdk_blocks(run)

        # Blocks 10 and 11 end on a 1 s exposure, which photographs the clock
        # as pure white — bring the speed back somewhere readable first.
        run.set_speed("1/125")
        run.clock_sync("clock_sync_end")

    except KeyboardInterrupt:
        print(f"\n\n{YELLOW}Interrupted — releasing contacts.{RESET}")
    finally:
        trigger.close()
        if run.camera is not None:
            try:
                run.camera.exit()
            except Exception:
                print(f"{YELLOW}Camera did not close cleanly — power cycle it.{RESET}")

    say("campaign finished")
    print(f"\n{GREEN}Done.{RESET}  {len(run.records)} actions logged to\n  {log_path}")
    print("\nCOPY THE WHOLE CARD to disk before it ever goes back in the camera.")


if __name__ == "__main__":
    main()
