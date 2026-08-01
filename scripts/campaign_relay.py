"""Bench campaign: what the X-T4 does when the relay drives its release jack.

Answers, in one sitting and without touching USB, the questions the production
eclipse script depends on:

  * how long after the command the shutter actually opens, and how much that varies
  * whether the drive mode alone decides what a held contact does
  * the sustained frame rate in CH, and where the buffer knee is
  * whether the rate follows the shutter speed, as it must during totality
  * what the electronic shutter buys over the mechanical one
  * whether an auto-bracket sequence repeats while the contact stays closed

Nothing here can read the card, so every frame is identified afterwards from its
EXIF.  Each block opens with a "slate" of single frames -- block number one, two,
three and so on, a second and a half apart -- and blocks are separated by a long
quiet gap, so block boundaries are unmistakable even at EXIF's one-second
resolution.  Fuji also records Image Count, an absolute shutter actuation number,
which pins every frame in sequence and exposes any that went missing.

Run it at the bench, on the machine holding the relay:

    .venv/bin/python scripts/campaign_relay.py
"""

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from solareclipseworkbench import relay_trigger as rt

BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"

SLATE_GAP_S = 1.5
BLOCK_GAP_S = 8.0
CLOCK_PATH = Path("/tmp/sew_timing_clock.html")

CLOCK_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>timing clock</title>
<style>
  html,body{margin:0;height:100%;background:#000;color:#fff;
    font-family:"SF Mono",Menlo,monospace;overflow:hidden}
  #wrap{height:100%;display:flex;flex-direction:column;
    align-items:center;justify-content:center;gap:2vh}
  #clock{font-size:16vw;font-weight:700;letter-spacing:-0.02em;line-height:1}
  #ms{font-size:26vw;font-weight:700;line-height:1;color:#0f0}
  #bar{width:90vw;height:6vh;background:#111;position:relative}
  #pip{position:absolute;top:0;bottom:0;width:4vw;background:#f00}
</style></head><body><div id="wrap">
  <div id="clock">--:--:--</div><div id="ms">---</div>
  <div id="bar"><div id="pip"></div></div>
</div><script>
  const clock = document.getElementById('clock');
  const ms = document.getElementById('ms');
  const pip = document.getElementById('pip');
  function pad(n, w) { return String(n).padStart(w, '0'); }
  function tick() {
    const now = new Date();
    clock.textContent = pad(now.getHours(),2) + ':' + pad(now.getMinutes(),2)
      + ':' + pad(now.getSeconds(),2);
    const milli = now.getMilliseconds();
    ms.textContent = '.' + pad(milli, 3);
    pip.style.left = (milli / 1000 * 96) + 'vw';
    requestAnimationFrame(tick);
  }
  tick();
</script></body></html>
"""


@dataclass
class Record:
    """One thing the campaign did, stamped on the clock the photos will show."""

    block: str
    action: str
    wall_start: float
    wall_end: float
    detail: dict = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return self.wall_end - self.wall_start


class Campaign:
    """Drives the relay and keeps a timestamped record of every contact change."""

    def __init__(self, trigger: rt.RelayTrigger, log_path: Path):
        self.trigger = trigger
        self.log_path = log_path
        self.records: list = []
        self.notes: dict = {}
        self.block = "preflight"
        self.expected_frames = 0

    # ------------------------------------------------------------------ record

    def add(self, action: str, wall_start: float, wall_end: float, **detail) -> None:
        self.records.append(Record(self.block, action, wall_start, wall_end, detail))
        self.save()

    def save(self) -> None:
        payload = {
            "started": self.records[0].wall_start if self.records else None,
            "notes": self.notes,
            "expected_frames": self.expected_frames,
            "records": [
                {
                    "block": r.block, "action": r.action,
                    "wall_start": r.wall_start, "wall_end": r.wall_end,
                    "duration_s": round(r.duration_s, 4), **r.detail,
                }
                for r in self.records
            ],
        }
        self.log_path.write_text(json.dumps(payload, indent=2))

    # ------------------------------------------------------------------ actions

    def pulse(self, pulse_ms: int, label: str = "pulse") -> None:
        """One frame, S1 then S2, with S2 closed for pulse_ms."""
        started = time.time()
        self.trigger.shoot(pulse=pulse_ms / 1000.0)
        self.add(label, started, time.time(), pulse_ms=pulse_ms)
        self.expected_frames += 1

    def hold(self, seconds: float, label: str = "hold") -> None:
        """Close S1, settle, close S2 for exactly `seconds`, release both."""
        started = time.time()
        with self.trigger.pressed():
            s2_at = time.time()
            time.sleep(seconds)
        ended = time.time()
        self.add(label, started, ended, hold_s=seconds, s2_closed_at=s2_at,
                 s2_open_for=round(ended - s2_at, 4))
        print(f"    {DIM}S2 held {ended - s2_at:.3f} s{RESET}")

    def quiet(self, seconds: float, why: str) -> None:
        started = time.time()
        print(f"    {DIM}{why} — {seconds:.0f} s{RESET}", flush=True)
        time.sleep(seconds)
        self.add("quiet", started, time.time(), why=why)

    def slate(self, number: int) -> None:
        """Single frames counting out the block number, to mark it on the card."""
        print(f"    {DIM}slate: {number} frame(s){RESET}", flush=True)
        for index in range(number):
            self.pulse(80, label="slate")
            if index < number - 1:
                time.sleep(SLATE_GAP_S)

    # ------------------------------------------------------------------- blocks

    def begin(self, number: int, title: str, setup: list) -> bool:
        self.block = f"{number}:{title}"
        print(f"\n{BOLD}{'=' * 70}{RESET}")
        print(f"{BOLD}Block {number} — {title}{RESET}\n")
        print(f"{CYAN}Set on the camera:{RESET}")
        for line in setup:
            print(f"  - {line}")

        answer = input(f"\n  Enter when set, or 's' to skip this block > ").strip().lower()
        if answer == "s":
            self.add("skipped", time.time(), time.time())
            print(f"  {YELLOW}skipped{RESET}")
            return False

        self.quiet(BLOCK_GAP_S, "settling gap so the block boundary is visible")
        self.slate(number)
        time.sleep(2.0)
        return True

    def observation(self) -> None:
        seen = input("\n  What did you see or hear?  (Enter for nothing notable) > ").strip()
        if seen:
            self.notes[self.block] = seen
            self.save()


def say(text: str) -> None:
    subprocess.run(["say", text], check=False)


def open_clock() -> None:
    CLOCK_PATH.write_text(CLOCK_HTML)
    subprocess.run(["open", str(CLOCK_PATH)], check=False)


def preflight() -> bool:
    print(f"\n{BOLD}{'=' * 70}{RESET}")
    print(f"{BOLD}X-T4 relay campaign{RESET}\n")
    print("This runs once and everything gets read off the card afterwards, so the")
    print("setup below has to be right before the first frame.\n")
    print(f"{CYAN}Camera:{RESET}")
    print("  - card freshly formatted")
    print("  - RAW only, no JPEG alongside it (a JPEG triples the cost per frame)")
    print("  - manual focus, focused on the clock digits")
    print("  - manual exposure, f/2, ISO 800")
    print("  - image stabilisation off, so nothing moves between frames")
    print("  - aimed at the browser window, clock digits filling the frame")
    print(f"\n{CYAN}Screen:{RESET}")
    print("  - the clock page opens in your browser; put that window on the top half")
    print("    of the screen and this terminal on the bottom half")
    print(f"\n{CYAN}Expect{RESET} roughly 900 frames and 25 GB, and about 25 minutes.")

    open_clock()
    answer = input(f"\n  Everything set?  [y/n] > ").strip().lower()
    return answer in ("y", "yes")


def main() -> None:
    if not preflight():
        print("Nothing done.")
        return

    log_path = Path.cwd() / f"campaign_relay_{int(time.time())}.json"
    trigger = rt.open_trigger("auto", s1_channel=1, s2_channel=2)
    run = Campaign(trigger, log_path)
    print(f"\n{trigger.describe()}\nlogging to {log_path}")

    try:
        if run.begin(1, "release latency, single drive", [
            "DRIVE dial to S (single)",
            "shutter 1/125",
        ]):
            say("block one, latency")
            for pulse_ms in (40, 40, 40, 100, 100, 100, 200, 200, 200):
                run.pulse(pulse_ms)
                time.sleep(3.0)
            print(f"\n  {DIM}now two long holds — single drive should give one frame each{RESET}")
            for _ in range(2):
                run.hold(2.0, label="hold_single_drive")
                time.sleep(4.0)
            run.observation()

        if run.begin(2, "sustained rate and the buffer knee", [
            "DRIVE dial to CH",
            "CH speed at its maximum",
            "shutter 1/1000",
            "mechanical shutter (MS)",
        ]):
            say("block two, burst rate")
            for seconds in (5.0, 10.0, 20.0):
                say(f"{seconds:.0f} second burst")
                run.hold(seconds, label=f"burst_{seconds:.0f}s")
                run.quiet(45.0, "letting the buffer drain")
            run.observation()

        if run.begin(3, "does the rate follow the shutter speed", [
            "still CH, mechanical",
            "shutter 1/15",
        ]):
            say("block three, slow shutter")
            run.hold(6.0, label="burst_slow_15")
            run.quiet(30.0, "letting the buffer drain")
            input("\n  Now set shutter to 1/2, then press Enter > ")
            run.hold(8.0, label="burst_slow_2")
            run.observation()

        if run.begin(4, "electronic shutter", [
            "still CH",
            "shutter type ES (electronic)",
            "shutter 1/1000",
        ]):
            say("block four, electronic shutter")
            run.hold(5.0, label="burst_electronic")
            run.observation()

        if run.begin(5, "auto bracket while the contact is held", [
            "shutter type back to MS (mechanical)",
            "DRIVE to BKT, AE bracketing",
            "BKT set to 9 frames, 3 EV steps — the widest the body offers",
            "shutter 1/500 as the middle of the ladder",
        ]):
            say("block five, bracketing")
            print(f"\n  {DIM}one short pulse — does a single press run the whole bracket?{RESET}")
            run.pulse(40, label="bkt_single_pulse")
            time.sleep(8.0)
            print(f"  {DIM}three second hold{RESET}")
            run.hold(3.0, label="bkt_hold_3s")
            run.quiet(20.0, "letting the buffer drain")
            print(f"  {DIM}fifteen second hold — does the sequence repeat?{RESET}")
            run.hold(15.0, label="bkt_hold_15s")
            run.observation()

    except KeyboardInterrupt:
        print(f"\n\n{YELLOW}Interrupted — releasing contacts.{RESET}")
    finally:
        trigger.close()
        run.save()

    say("campaign finished")
    print(f"\n{BOLD}{'=' * 70}{RESET}")
    print(f"{GREEN}Done.{RESET}  {len(run.records)} actions logged to\n  {log_path}")
    print(f"\nRoughly {run.expected_frames} frames were commanded as single shots; the")
    print("held bursts add however many the camera decided to take, which is the")
    print("whole point of measuring.\n")
    print("Now take the card out and put it in the reader.")


if __name__ == "__main__":
    main()
