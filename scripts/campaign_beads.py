"""Bench campaign: how many frames the beads can actually have.

The production schedule holds the release for 1.9 s at each contact, which is
MAX_BURST_S: an estimate from 15 fps against a 32-slot transfer queue, not a
measurement.  The beads run 3.25 s at C2 and 4.05 s at C3 for this site, so the
burst covers barely half of what there is to photograph.

Three questions, one sitting, one card:

  1. where the queue knee really is — hold the release with an SDK session open
     and watch the buffer fill, releasing before it saturates
  2. whether a pulsed burst is faithful — S2 tapped at a fixed interval gives a
     known frame count and dodges the hold cap, but only if every tap lands
  3. how soon a second burst can follow the first, once the queue is drained

Nothing here lets the buffer fill.  A full queue is not a failed measurement,
it is a camera that has to have its battery pulled, and on eclipse day there is
no second contact to try again at.  Every block polls the buffer and opens the
contact while there are still slots to spare.

The buffer count is itself the frame count: with a session open, frames queue
until they are drained, so `captured` straight after a burst says exactly how
many the body took.  No card reading, no EXIF, answers on the screen.

Run it at the bench, on the machine holding the relay, camera on USB:

    .venv/bin/python scripts/campaign_beads.py
"""

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# fujixsdk lives in the repo root, which is not on sys.path when this file is
# run as scripts/campaign_beads.py — without this the SDK is silently
# unavailable and every camera looks absent.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solareclipseworkbench import relay_trigger as rt
from solareclipseworkbench.fuji_camera import (
    detect_fuji_cameras,
    find_fuji_sdk_path,
    maybe_reexec_for_fuji_sdk,
)

maybe_reexec_for_fuji_sdk()

BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"

# Open the contact once the queue has this many slots left.  Two frames at
# 15 fps is 133 ms, and a poll costs a USB round-trip, so anything tighter risks
# discovering the ceiling by hitting it.
SAFETY_SLOTS = 6

# No hold may outlive this, whatever the buffer says.  If the poll thread dies
# or the SDK stops answering, the contact still opens.
HOLD_CEILING_S = 6.0

# How often to ask the body how full it is.  At 15 fps a frame arrives every
# 67 ms, so this samples two to three times per frame.
POLL_S = 0.025

# The real bead windows for the production site, from the limb solution.
C2_WINDOW_S = 3.25
C3_WINDOW_S = 4.05

SETTLE_AFTER_DRAIN_S = 2.0
BLOCK_GAP_S = 6.0
SLATE_GAP_S = 1.5


@dataclass
class Record:
    """One thing the campaign did, and what the buffer did while it happened."""

    block: str
    action: str
    wall_start: float
    wall_end: float
    detail: dict = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return self.wall_end - self.wall_start


class BufferWatch:
    """Polls the transfer queue on its own thread while the contact is closed.

    The relay holds S2 in the main thread, so the buffer has to be read from
    somewhere else — and the reading is the whole safety mechanism, not a
    diagnostic.  ``stop_requested`` goes true the moment the queue is within
    SAFETY_SLOTS of full, and the caller opens the contact.
    """

    def __init__(self, sdk_cam):
        self.sdk_cam = sdk_cam
        self.samples: list = []
        self.total = None
        self.peak = 0
        self.stop_requested = False
        self.read_failures = 0
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        return False

    def _run(self) -> None:
        started = time.perf_counter()
        while not self._stop.is_set():
            try:
                captured, total = self.sdk_cam.get_buffer_capacity()
            except Exception:
                # The body refuses reads while it is busy.  A failed poll is not
                # a reason to keep holding: if we cannot see the queue we cannot
                # promise it has room, so a run of failures ends the burst too.
                self.read_failures += 1
                if self.read_failures >= 8:
                    self.stop_requested = True
                time.sleep(POLL_S)
                continue
            self.read_failures = 0
            self.total = total
            self.peak = max(self.peak, captured)
            self.samples.append((round(time.perf_counter() - started, 4), captured))
            if total - captured <= SAFETY_SLOTS:
                self.stop_requested = True
                return
            time.sleep(POLL_S)


class Campaign:
    """Drives relay and camera together, keeping a timestamped record."""

    def __init__(self, trigger: rt.RelayTrigger, log_path: Path):
        self.trigger = trigger
        self.camera = None
        self.log_path = log_path
        self.records: list = []
        self.notes: dict = {}
        self.block = "preflight"

    # ------------------------------------------------------------------ record

    def add(self, action: str, wall_start: float, wall_end: float, **detail) -> None:
        self.records.append(Record(self.block, action, wall_start, wall_end, detail))
        self.save()

    def save(self) -> None:
        payload = {
            "started": self.records[0].wall_start if self.records else None,
            "notes": self.notes,
            "safety_slots": SAFETY_SLOTS,
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

    # ------------------------------------------------------------------ camera

    @property
    def sdk(self):
        return self.camera._sdk_cam

    def buffer(self) -> tuple:
        try:
            return self.sdk.get_buffer_capacity()
        except Exception as exc:
            print(f"    {YELLOW}buffer unreadable: {exc}{RESET}")
            return (None, None)

    def drain_and_settle(self, why: str = "") -> int:
        """Empty the queue and wait for the body to finish writing the card.

        Draining discards the PC transfers, not the images: those are on the
        card either way.  The wait afterwards is for the card, which is slower
        than the queue and is what actually paces a second burst.
        """
        started = time.time()
        self.trigger.release_all()
        time.sleep(0.3)
        try:
            dropped = self.camera.drain()
        except Exception as exc:
            print(f"    {RED}drain failed: {exc}{RESET}")
            dropped = -1
        time.sleep(SETTLE_AFTER_DRAIN_S)
        captured, total = self.buffer()
        print(f"    {DIM}drained {dropped}, queue now {captured}/{total}{RESET}")
        self.add("drain", started, time.time(), dropped=dropped,
                 captured_after=captured, total=total, why=why)
        return dropped

    # ------------------------------------------------------------------ actions

    def guarded_hold(self, target_s: float, label: str) -> dict:
        """Hold S2 for up to target_s, opening early if the queue fills up."""
        target_s = min(target_s, HOLD_CEILING_S)
        before, total = self.buffer()
        started = time.time()
        with BufferWatch(self.sdk) as watch:
            self.trigger.half_press()
            time.sleep(self.trigger.wiring.settle_s)
            s2_at = time.perf_counter()
            self.trigger._set(self.trigger.wiring.s2_channel, True)
            stopped_by = "target"
            while time.perf_counter() - s2_at < target_s:
                if watch.stop_requested:
                    stopped_by = "buffer"
                    break
                time.sleep(0.005)
            held = time.perf_counter() - s2_at
            self.trigger._set(self.trigger.wiring.s2_channel, False)
            self.trigger.release_all()
        after, _ = self.buffer()

        result = {
            "target_s": target_s, "held_s": round(held, 4), "stopped_by": stopped_by,
            "captured_before": before, "captured_after": after,
            "peak_captured": watch.peak, "total": watch.total or total,
            "frames": (after - before) if None not in (after, before) else None,
            "read_failures": watch.read_failures,
            "samples": watch.samples,
        }
        self.add(label, started, time.time(), **result)

        colour = GREEN if stopped_by == "target" else YELLOW
        print(f"    {colour}held {held:.3f} s ({stopped_by}){RESET}, "
              f"queue {before} -> {after} of {result['total']}, "
              f"{result['frames']} frames")
        return result

    def guarded_pulse_train(self, duration_s: float, interval_s: float, label: str) -> dict:
        """Tap S2 at a fixed interval, stopping early if the queue fills up.

        This is the measurement that decides whether the whole bead window can be
        covered: a held burst runs at the body's 15 fps and fills the queue in
        about two seconds, but a pulse train at a chosen rate spends the same 32
        slots over as long as the beads last — if every tap actually lands.
        """
        before, total = self.buffer()
        started = time.time()
        pulses = 0
        with BufferWatch(self.sdk) as watch:
            self.trigger.half_press()
            time.sleep(self.trigger.wiring.settle_s)
            deadline = time.perf_counter() + duration_s
            stopped_by = "duration"
            while time.perf_counter() < deadline:
                if watch.stop_requested:
                    stopped_by = "buffer"
                    break
                frame_started = time.perf_counter()
                self.trigger._set(self.trigger.wiring.s2_channel, True)
                time.sleep(self.trigger.wiring.pulse_s)
                self.trigger._set(self.trigger.wiring.s2_channel, False)
                pulses += 1
                # Busy-wait the remainder: sleep() drifts at these intervals.
                while time.perf_counter() - frame_started < interval_s:
                    if time.perf_counter() >= deadline:
                        break
            ran = time.perf_counter() - (deadline - duration_s)
            self.trigger.release_all()
        after, _ = self.buffer()

        frames = (after - before) if None not in (after, before) else None
        result = {
            "duration_s": duration_s, "interval_s": interval_s,
            "ran_s": round(ran, 4), "pulses": pulses, "stopped_by": stopped_by,
            "captured_before": before, "captured_after": after,
            "frames": frames, "total": watch.total or total,
            "fidelity": round(frames / pulses, 3) if frames is not None and pulses else None,
            "samples": watch.samples,
        }
        self.add(label, started, time.time(), **result)

        if frames is None:
            verdict = f"{YELLOW}frame count unknown{RESET}"
        elif frames == pulses:
            verdict = f"{GREEN}every tap landed{RESET}"
        else:
            verdict = f"{RED}{pulses - frames} tap(s) lost{RESET}"
        print(f"    {pulses} pulses at {1 / interval_s:.1f} fps over {ran:.2f} s "
              f"-> {frames} frames — {verdict}")
        return result

    def slate(self, number: int) -> None:
        """Single frames counting out the block number, to mark it on the card."""
        print(f"    {DIM}slate: {number} frame(s){RESET}", flush=True)
        for index in range(number):
            self.trigger.shoot(pulse=0.08)
            if index < number - 1:
                time.sleep(SLATE_GAP_S)
        time.sleep(1.0)
        self.drain_and_settle("after slate")

    def begin(self, number: int, title: str, setup: list) -> bool:
        self.block = f"{number}:{title}"
        print(f"\n{BOLD}{'=' * 70}{RESET}")
        print(f"{BOLD}Block {number} — {title}{RESET}\n")
        print(f"{CYAN}Set on the camera:{RESET}")
        for line in setup:
            print(f"  - {line}")
        answer = input("\n  Enter when set, or 's' to skip this block > ").strip().lower()
        if answer == "s":
            self.add("skipped", time.time(), time.time())
            print(f"  {YELLOW}skipped{RESET}")
            return False
        time.sleep(BLOCK_GAP_S)
        self.slate(number)
        return True

    def observation(self) -> None:
        seen = input("\n  What did you see or hear?  (Enter for nothing notable) > ").strip()
        if seen:
            self.notes[self.block] = seen
            self.save()


# ---------------------------------------------------------------------- blocks

def block_buffer_knee(run: Campaign) -> None:
    """Question 1: how long can the release be held before the queue fills?

    Walks the hold up until the buffer guard trips.  The last hold that finished
    on its target is the honest MAX_BURST_S; the first one cut short says where
    the knee is and how many frames fit under it.
    """
    if not run.begin(1, "where the transfer queue knee is", [
        "drive dial on CH",
        "shutter 1/1000 or faster, ISO 100",
        "RAW only — RAW+JPEG costs three times the write and moves the knee",
        "a card with room for ~250 frames",
    ]):
        return

    ladder = [1.9, 2.5, 3.0, 3.5, 4.5]
    knee = None
    for target in ladder:
        print(f"\n  {CYAN}hold {target:.1f} s{RESET}")
        result = run.guarded_hold(target, f"hold_{target:.1f}s")
        run.drain_and_settle(f"after {target:.1f} s hold")
        if result["stopped_by"] == "buffer":
            knee = result
            print(f"\n  {YELLOW}The queue filled before {target:.1f} s — "
                  f"that is the knee.{RESET}")
            break

    if knee is None:
        print(f"\n  {GREEN}Every hold up to {ladder[-1]:.1f} s finished on target.{RESET}")
        print(f"  {DIM}MAX_BURST_S can be raised; the beads want "
              f"{C3_WINDOW_S:.2f} s.{RESET}")
    else:
        safe = knee["held_s"]
        print(f"  {DIM}Frames under the knee: {knee['frames']}.  A hold of "
              f"{safe:.2f} s is the most this body will take.{RESET}")
    run.observation()


def block_pulse_fidelity(run: Campaign) -> None:
    """Question 2: does a tapped burst put a frame on the card for every tap?

    A held burst runs at whatever the body decides.  A tapped one runs at the
    rate we choose, which is the only way ~30 frames can be spread across a
    4 s bead window — but only if the taps are not too fast to register.  The
    bench already knows 40 ms pulses miss about 8% and 80 ms never do; what is
    unknown is how short the gap between pulses may be.
    """
    if not run.begin(2, "whether a tapped burst loses frames", [
        "drive dial still on CH",
        "shutter 1/1000 or faster, ISO 100",
    ]):
        return

    # Each pair is chosen to stay under the queue: rate x duration must leave
    # slots to spare, or the guard trips and the fidelity number means nothing.
    trials = [
        (0.090, 2.40, "11.1 fps, the fastest tap worth trying"),
        (0.110, 2.90, "9.1 fps, what covering the C2 window would need"),
        (0.135, 3.60, "7.4 fps, what covering the C3 window would need"),
    ]
    for interval, duration, why in trials:
        print(f"\n  {CYAN}{why}{RESET}")
        run.guarded_pulse_train(duration, interval, f"pulse_{int(interval * 1000)}ms")
        run.drain_and_settle(f"after {interval * 1000:.0f} ms train")

    print(f"\n  {DIM}A rate whose taps all land, run over the full window, is what "
          f"replaces the held burst.{RESET}")
    run.observation()


def block_second_burst(run: Campaign) -> None:
    """Question 3: how soon can a second burst follow the first?

    If the turnaround is shorter than a bead window, two short bursts beat one
    long one: the pair can be pinned to both edges of the window with the gap in
    the middle, where the beads are least interesting.
    """
    if not run.begin(3, "how soon a second burst can follow", [
        "drive dial still on CH",
        "shutter 1/1000 or faster, ISO 100",
    ]):
        return

    for gap in (0.5, 1.0, 2.0, 3.0):
        print(f"\n  {CYAN}burst, drain, wait {gap:.1f} s, burst again{RESET}")
        first = run.guarded_hold(1.9, f"pair_first_gap{gap:.1f}")
        run.trigger.release_all()
        try:
            run.camera.drain()
        except Exception as exc:
            print(f"    {RED}drain failed: {exc}{RESET}")
        time.sleep(gap)
        second = run.guarded_hold(1.9, f"pair_second_gap{gap:.1f}")
        run.drain_and_settle(f"after pair with {gap:.1f} s gap")

        if second["frames"] is None or first["frames"] is None:
            print(f"    {YELLOW}frame counts unknown for this pair{RESET}")
        elif second["frames"] >= first["frames"] * 0.9:
            print(f"    {GREEN}a {gap:.1f} s gap is enough{RESET} "
                  f"({first['frames']} then {second['frames']} frames)")
        else:
            print(f"    {RED}a {gap:.1f} s gap is not enough{RESET} "
                  f"({first['frames']} then {second['frames']} frames)")

    run.observation()


# ------------------------------------------------------------------ connection

def connect_camera():
    """Get the SDK holding the body, with a retry rather than a restart."""
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
        print(f"\n{RED}The SDK cannot open the body.{RESET}")
        print("  - camera powered on, USB connected?")
        print("  - MENU > wrench > CONNECTION SETTING > PC CONNECTION MODE")
        print("    must be USB TETHER SHOOTING, not card reader")
        print("  - if it was connected earlier and wedged: power cycle AND replug")
        if input("\n  Enter to retry, 'q' to give up > ").strip().lower() == "q":
            return None


def main() -> None:
    print(f"{BOLD}Beads campaign — how many frames the bead windows can have{RESET}\n")
    print("This measures the transfer-queue ceiling that caps the bead bursts at")
    print(f"1.9 s, against bead windows of {C2_WINDOW_S:.2f} s at C2 and "
          f"{C3_WINDOW_S:.2f} s at C3.\n")
    print(f"{CYAN}Before starting:{RESET}")
    print("  - relay wired to the release jack, both channels")
    print("  - camera on USB, PC CONNECTION MODE = USB TETHER SHOOTING")
    print("  - drive dial on CH, focus and shutter on manual")
    print("  - RAW only, and a card with room for ~250 frames")
    print(f"\n{CYAN}Expect{RESET} roughly 15 minutes and 250 frames.")
    print(f"{DIM}Nothing here fills the buffer: every burst opens the contact with "
          f"{SAFETY_SLOTS} slots still free.{RESET}")
    if input("\n  Ready?  [y/n] > ").strip().lower() not in ("y", "yes"):
        print("Nothing done.")
        return

    log_path = Path.cwd() / f"campaign_beads_{int(time.time())}.json"
    trigger = rt.open_trigger("auto", s1_channel=1, s2_channel=2)
    run = Campaign(trigger, log_path)
    print(f"\n{trigger.describe()}\nlogging to {log_path}")

    if trigger.wiring.is_single_channel:
        print(f"\n{YELLOW}Single-channel wiring: block 2 cannot run — a tapped "
              f"burst needs S1 and S2 on separate channels.{RESET}")

    try:
        run.camera = connect_camera()
        if run.camera is None:
            print(f"{YELLOW}No SDK session, and without one there is no transfer "
                  f"queue to measure.  Nothing done.{RESET}")
            return

        captured, total = run.buffer()
        print(f"{DIM}queue at rest: {captured}/{total}{RESET}")
        if captured:
            print(f"{YELLOW}The queue is not empty — draining before we start.{RESET}")
            run.drain_and_settle("preflight")

        block_buffer_knee(run)
        if not trigger.wiring.is_single_channel:
            block_pulse_fidelity(run)
        block_second_burst(run)

    except KeyboardInterrupt:
        print(f"\n{YELLOW}Interrupted — opening every contact and draining.{RESET}")
        try:
            trigger.release_all()
            if run.camera:
                run.camera.drain()
        except Exception:
            pass
    finally:
        try:
            trigger.release_all()
        except Exception:
            pass
        run.save()
        print(f"\n{GREEN}Log written to {log_path}{RESET}")
        print(f"{DIM}Frame counts come from the transfer queue, so they are in the "
              f"log already — the card is only needed to look at the pictures.{RESET}")


if __name__ == "__main__":
    main()
