"""Bench campaign two: bracketing that fits, trigger latency, and whether USB coexists.

The first campaign established that the relay fires the X-T4 in about 170 ms, that the
card and not the shutter caps the frame rate at 3 fps once a 28-frame buffer fills, and
that one contact closure runs an entire auto-bracket sequence which completes even after
the contact opens.  Three questions it could not answer are the ones the production
script actually turns on:

  1. does a held contact *repeat* the bracket, or is one tap one sequence?  Every ladder
     tried last time took longer than the hold it ran inside, so nothing was learned.
  2. which ladder fits?  9 frames at 3 EV spans 24 EV and the body has about 18, so three
     frames piled up against the 1/8000 wall.
  3. does the release jack still fire while the SDK holds a USB session, and can a shutter
     speed set over USB reach the camera between relay-driven frames?

Blocks 1 to 5 need no USB and run first, so a wedged session cannot cost them.  The SDK
has been seen to leave the body enumerating as "(unknown)", needing a power cycle.

    .venv/bin/python scripts/campaign_sdk.py
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

# Ladders worth knowing about, from the widest that still fits the body's range down to
# one fast enough to repeat several times inside totality.
BRACKETS = [
    {
        "label": "bkt_fast_1ev",
        "title": "fast ladder — does a held contact repeat it",
        "said": "block one, fast bracket",
        "setup": ["BKT to 9 frames, 1 EV steps", "shutter 1/500"],
        "expect": "1/8000 to 1/30, a whole sequence in about a second",
        "hold_s": 15.0,
        "recover_s": 12.0,
    },
    {
        "label": "bkt_corona_2ev",
        "title": "the corona ladder — 9 frames at 2 EV",
        "said": "block two, corona bracket",
        "setup": ["BKT to 9 frames, 2 EV steps", "shutter 1/30"],
        "expect": "1/8000, 1/2000, 1/500, 1/125, 1/30, 1/8, 1/2, 2s, 8s — nothing clamped",
        "hold_s": 30.0,
        "recover_s": 20.0,
    },
    {
        "label": "bkt_working_7f",
        "title": "a working ladder — 7 frames at 2 EV, no long tail",
        "said": "block three, working bracket",
        "setup": ["BKT to 7 frames, 2 EV steps", "shutter 1/125"],
        "expect": "1/8000 to 1/2, about a second and a half, repeatable through totality",
        "hold_s": 20.0,
        "recover_s": 12.0,
    },
]

# Walked during the ramp tests.  Wide enough that a frame's ExposureTime says without
# ambiguity which command it belongs to, which is the whole measurement.
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

    def s2_only(self, pulse_ms: int, label: str) -> None:
        """Close S2 without ever asserting S1, which this body accepts."""
        channel = self.trigger.wiring.s2_channel
        started = time.time()
        self.trigger.backend.set_channel(channel, True)
        time.sleep(pulse_ms / 1000.0)
        self.trigger.backend.set_channel(channel, False)
        self.add(label, started, time.time(), pulse_ms=pulse_ms)

    def quiet(self, seconds: float, why: str) -> None:
        started = time.time()
        print(f"    {DIM}{why} — {seconds:.0f} s{RESET}", flush=True)
        time.sleep(seconds)
        self.add("quiet", started, time.time(), why=why)

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

    def slate(self, number: int) -> None:
        print(f"    {DIM}slate: {number} frame(s){RESET}", flush=True)
        for index in range(number):
            self.pulse(80, label="slate")
            if index < number - 1:
                time.sleep(1.5)

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
        self.quiet(8.0, "settling gap so the block boundary is visible")
        self.slate(number)
        time.sleep(2.0)
        return True

    def observation(self) -> None:
        seen = input("\n  What did you see or hear?  (Enter for nothing notable) > ").strip()
        if seen:
            self.notes[self.block] = seen


def say(text: str) -> None:
    subprocess.run(["say", text], check=False)


def connect_camera():
    """Open the first X-T4 the SDK can see, or explain why it cannot."""
    sdk_path = find_fuji_sdk_path()
    if not sdk_path:
        print(f"{RED}No Fuji SDK found.{RESET}  Set FUJI_SDK_PATH and try again.")
        return None
    print(f"{DIM}SDK at {sdk_path}{RESET}")

    cameras = detect_fuji_cameras(sdk_path)
    if not cameras:
        print(f"{RED}The SDK sees no camera.{RESET}")
        print("  USB connected?  Body in tether shooting rather than card reader mode?")
        print("  If it enumerates as '(unknown)', power cycle and replug.")
        return None

    name, camera = next(iter(cameras.items()))
    print(f"{GREEN}Connected:{RESET} {name}")
    return camera


def run_bracket_blocks(run: Campaign) -> None:
    for number, bracket in enumerate(BRACKETS, start=1):
        if not run.begin(number, bracket["title"],
                         ["DRIVE to BKT, AE bracketing", *bracket["setup"]]):
            continue
        say(bracket["said"])
        print(f"\n  {DIM}expect {bracket['expect']}{RESET}")
        print(f"  {DIM}one pulse first — one sequence, and how long it takes{RESET}")
        run.pulse(40, label=f"{bracket['label']}_single")
        run.quiet(bracket["recover_s"], "letting the sequence finish and the buffer drain")
        print(f"  {DIM}{bracket['hold_s']:.0f} s hold — repeats show as several sequences{RESET}")
        run.hold(bracket["hold_s"], label=f"{bracket['label']}_hold")
        run.observation()
        run.quiet(bracket["recover_s"], "letting the buffer drain")


def run_latency_block(run: Campaign) -> None:
    """Three ways to fire one frame, to price the S1 pre-arm."""
    if not run.begin(4, "what the S1 pre-arm actually costs", [
        "DRIVE dial back to S (single)",
        "shutter 1/125",
        "still aimed at the clock — this block is read off the digits in frame",
    ]):
        return
    say("block four, trigger latency")

    print(f"\n  {DIM}cold: 30 s idle, then S2 alone with no pre-arm at all{RESET}")
    for _ in range(3):
        run.quiet(30.0, "letting the body go idle")
        run.s2_only(80, label="latency_cold_s2_only")

    print(f"\n  {DIM}pre-armed: S1 closed and held, then S2 pulsed{RESET}")
    run.trigger.half_press()
    time.sleep(3.0)
    for _ in range(3):
        run.pulse(80, label="latency_prearmed")
        time.sleep(3.0)
    run.trigger.release_all()

    print(f"\n  {DIM}normal: the full shoot(), settle and all{RESET}")
    time.sleep(3.0)
    for _ in range(3):
        run.pulse(80, label="latency_normal")
        time.sleep(3.0)
    run.observation()


def run_max_rate_block(run: Campaign) -> None:
    """The burst window is where the high frame rate lives, and beads live there too."""
    if not run.begin(5, "maximum burst rate, mechanical then electronic", [
        "DRIVE dial to CH",
        "CH speed to 15 fps",
        "mechanical shutter, shutter 1/2000",
    ]):
        return
    say("block five, maximum rate")
    run.hold(4.0, label="max_rate_mech_15")
    run.quiet(45.0, "letting the buffer drain")

    input("\n  Now set electronic shutter and CH speed to 20 fps, then Enter > ")
    run.hold(4.0, label="max_rate_elec_20")
    run.observation()
    run.quiet(45.0, "letting the buffer drain")


def run_duty_cycle_block(run: Campaign) -> None:
    """Clustered bursts against one long hold, for the same wall-clock time.

    A pause lets the card drain but idles the sensor, so the expectation is fewer
    frames than holding throughout — bought in exchange for tight clusters.  Worth
    measuring rather than assuming, because it turns on how fast the buffer really
    recovers, which no datasheet states.
    """
    if not run.begin(6, "clustered bursts against one continuous hold", [
        "still CH at 15 fps, mechanical, shutter 1/2000",
    ]):
        return
    say("block six, duty cycle")

    print(f"\n  {DIM}three cycles of 2.5 s burst then 10 s drain{RESET}")
    for cycle in range(3):
        run.hold(2.5, label=f"duty_burst_{cycle + 1}")
        run.quiet(10.0, "draining between clusters")

    run.quiet(45.0, "full drain before the control run")
    print(f"  {DIM}control: one continuous 37.5 s hold, same wall clock{RESET}")
    run.hold(37.5, label="duty_control_continuous")
    run.observation()
    run.quiet(60.0, "letting the buffer drain")


def run_sdk_blocks(run: Campaign) -> None:
    if run.begin(7, "does the jack still fire with a USB session open", [
        "nothing to change — the SDK session is now open",
    ]):
        say("block seven, does the jack still fire")
        print(f"\n  {DIM}three single frames through the relay{RESET}")
        for _ in range(3):
            run.pulse(80, label="relay_shot_sdk_open")
            time.sleep(3.0)
        print(f"  {DIM}three second burst through the relay{RESET}")
        run.hold(3.0, label="relay_burst_sdk_open")
        run.observation()

    if run.begin(8, "shutter speed set over USB, frames driven by the relay", [
        "nothing to change",
    ]):
        say("block eight, exposure ramp")
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

    if run.begin(9, "ramping mid-burst, the way totality would need it", [
        "nothing to change",
    ]):
        say("block nine, ramping mid burst")
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
    print(f"{BOLD}X-T4 campaign two — brackets, latency, USB coexistence{RESET}\n")
    print(f"{CYAN}Before starting:{RESET}")
    print("  - relay wired to the release jack, camera aimed at the clock page")
    print("  - RAW only, manual focus, f/2, ISO 320 as before")
    print("  - USB cable NOT connected yet — the first five blocks run on the relay alone")
    print(f"\n{CYAN}Expect{RESET} roughly 35 minutes and 700 frames.")
    if input(f"\n  Ready?  [y/n] > ").strip().lower() not in ("y", "yes"):
        print("Nothing done.")
        return

    log_path = Path.cwd() / f"campaign_sdk_{int(time.time())}.json"
    trigger = rt.open_trigger("auto", s1_channel=1, s2_channel=2)
    run = Campaign(trigger, log_path)
    print(f"\n{trigger.describe()}\nlogging to {log_path}")

    try:
        run_bracket_blocks(run)
        run_latency_block(run)
        run_max_rate_block(run)
        run_duty_cycle_block(run)

        print(f"\n{CYAN}Now connect the USB cable{RESET}, body in tether shooting mode,")
        print("and leave the DRIVE dial on CH.")
        input("  Enter when done, or Ctrl-C to stop here > ")

        run.camera = connect_camera()
        if run.camera is None:
            print(f"{YELLOW}Stopping — everything before this is saved.{RESET}")
        else:
            run_sdk_blocks(run)

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
    print("\nTake the card out and put it in the reader.")


if __name__ == "__main__":
    main()
