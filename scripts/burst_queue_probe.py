"""Where does the mid-burst sag come from, and does pacing delay it?

Both rehearsal bursts ran at the body's 8 fps for the first few seconds and
then dropped to 4-6 fps, landing about twenty frames short of flat rate.  The
dip falls on the contact at C2 and just past the diamond at C3, which is the
worst place for it.

The model, inferred from card EXIF and NOT yet measured, is that the frames
crowd the deletes off the USB bus: shooting flat out starves the queue drain
to well under a frame a second, the 32 slots fill in about four and a half
seconds, and from there the body can only shoot as fast as slots come free.
If that is what happens, pacing the head of a burst below the drain rate keeps
the queue empty and leaves the full-rate budget for the moment that matters.

This measures it instead of assuming it, and measures it through the real
``relay_burst`` rather than a copy of it, so what the eclipse script will do is
what gets tested.  Two things are recorded through each burst:

  drain events   every ``owner.drain()`` the burst makes, with its timestamp
                 and how many slots it actually freed.  A drain returning ~0
                 while frames pour in IS the starvation, seen directly.

  queue depth    sampled on a second thread.  This costs a little of the very
                 bus contention being measured, so ``--no-sample`` turns it off
                 and leaves the drain record, which is the load-bearing half.

Run it both ways and compare when the queue reaches 32:

    .venv/bin/python scripts/burst_queue_probe.py                  # held, as today
    .venv/bin/python scripts/burst_queue_probe.py --mode both      # held vs paced

The camera fires for real - lens cap on is fine, the queue does not care.
Camera on, relay on S1=ch1 S2=ch2, workbench closed: the SDK takes one session.
"""

import argparse
import json
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

maybe_reexec_for_fuji_sdk()

from solareclipseworkbench import relay_trigger as rt
from solareclipseworkbench.hardware_registry import register_hardware
from bench_log import tee_console

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"

#: The X-T4's tether queue.  At 32 the body hard-stops the burst.
QUEUE_SLOTS = 32
SAMPLE_HZ = 4.0


class DrainSpy:
    """Stands in for the camera in the registry and records every drain.

    ``relay_burst`` reaches for ``owner.drain()`` and ``owner._usb_lock``;
    everything else falls through untouched, so the burst runs exactly as it
    will on the day and the only difference is that its drains are timed.
    """

    def __init__(self, camera):
        self._camera = camera
        self.events = []
        self.t0 = None

    def __getattr__(self, name):
        return getattr(self._camera, name)

    def drain(self, *args, **kwargs):
        started = time.monotonic()
        freed = self._camera.drain(*args, **kwargs)
        self.events.append({
            "t": round(started - self.t0, 3),
            "took_ms": round((time.monotonic() - started) * 1000),
            "freed": freed,
        })
        return freed


def sdk_of(camera):
    return getattr(camera, "_sdk", None) or getattr(camera, "_sdk_cam", None)


def sample_depth(sdk, stop, t0, into):
    """Read the queue depth at SAMPLE_HZ until ``stop`` is set."""
    period = 1.0 / SAMPLE_HZ
    while not stop.is_set():
        try:
            depth, _ = sdk.get_buffer_capacity()
        except Exception:
            depth = -1
        into.append({"t": round(time.monotonic() - t0, 3), "depth": depth})
        stop.wait(period)


def spark(samples, width=48):
    """A depth-over-time sparkline, so the shape is visible without a plot."""
    if not samples:
        return ""
    blocks = " .:-=+*#@"
    span = max(s["t"] for s in samples) or 1.0
    buckets = [[] for _ in range(width)]
    for s in samples:
        if s["depth"] >= 0:
            buckets[min(width - 1, int(s["t"] / span * width))].append(s["depth"])
    out = ""
    for b in buckets:
        if not b:
            out += " "
            continue
        peak = max(b)
        out += blocks[min(len(blocks) - 1,
                          int(peak / QUEUE_SLOTS * (len(blocks) - 1)))]
    return out


def run_burst(spy, sdk, relay, duration, interval, sample):
    """One burst through the production path.  Returns a result dict."""
    relay.release_all()
    spy._camera.drain()
    time.sleep(0.5)
    try:
        start_depth, _ = sdk.get_buffer_capacity()
    except Exception:
        start_depth = -1

    spy.events = []
    spy.t0 = time.monotonic()
    samples, stop = [], threading.Event()
    watcher = None
    if sample:
        watcher = threading.Thread(target=sample_depth,
                                   args=(sdk, stop, spy.t0, samples),
                                   name="depth_sampler", daemon=True)
        watcher.start()

    # Time the contact, not the call.  The tail drain after the contact opens
    # is deliberate and costs the schedule nothing that is still firing, but
    # counting it as hold time makes an on-time burst look 5 s late - which is
    # exactly how the first run of this misread the fix.
    opened = {}
    real_release = relay.release_all

    def timed_release():
        opened.setdefault("t", time.monotonic())
        return real_release()

    relay.release_all = timed_release
    wall = time.monotonic()
    try:
        rt.relay_burst(relay, duration, interval)
    finally:
        relay.release_all = real_release
    held = opened.get("t", time.monotonic()) - wall
    wall = time.monotonic() - wall

    stop.set()
    if watcher:
        watcher.join(timeout=2.0)

    time.sleep(1.5)                    # the body reports frames as it writes
    try:
        end_depth, _ = sdk.get_buffer_capacity()
    except Exception:
        end_depth = -1
    tail = spy._camera.drain()

    # Everything the burst put in the queue: what the drains took out while it
    # ran, plus whatever was still sitting there when it stopped.  `tail` is
    # that same remainder collected a second time, so it is reported but not
    # added - counting both would double the end of every burst.
    freed = sum(e["freed"] for e in spy.events)
    remaining = max(0, end_depth - max(0, start_depth))
    frames = freed + remaining
    saturated = next((s["t"] for s in samples if s["depth"] >= QUEUE_SLOTS), None)
    return {
        "interval": interval,
        "duration_s": duration,
        "held_s": round(held, 2),
        "wall_s": round(wall, 2),
        "frames": frames,
        # Frames over the time the contact was actually closed: that is the
        # rate the eclipse gets, and the only figure worth comparing.
        "fps": round(frames / held, 2) if held else 0,
        "drained_during": freed,
        "drained_after": tail,
        "saturated_at_s": saturated,
        "peak_depth": max((s["depth"] for s in samples), default=-1),
        "drains": spy.events,
        "samples": samples,
    }


def report(r):
    kind = "held" if r["interval"] is None else f"paced {r['interval']:.2f}s"
    print(f"\n{'=' * 60}\n{kind}: {r['frames']} frames in {r['held_s']:.2f}s "
          f"on the contact = {r['fps']:.2f} fps  "
          f"({r['wall_s']:.2f}s including the tail drain)")
    print(f"  drained {r['drained_during']} during the burst, "
          f"{r['drained_after']} after")
    if r["samples"]:
        print(f"  peak queue depth {r['peak_depth']}/{QUEUE_SLOTS}")
        if r["saturated_at_s"] is not None:
            print(f"  {RED}queue hit {QUEUE_SLOTS} at "
                  f"{r['saturated_at_s']:.2f}s{RESET} - the body throttles here")
        else:
            print(f"  {GREEN}queue never saturated{RESET}")
        print(f"  depth 0-{QUEUE_SLOTS}: |{spark(r['samples'])}|")

    overrun = r["held_s"] - r["duration_s"]
    if overrun > 0.5:
        print(f"  {RED}contact held {overrun:.2f}s past the {r['duration_s']:.1f}s "
              f"asked for{RESET} - the burst does not stop when the script says")
    else:
        print(f"  {GREEN}contact opened on time{RESET} "
              f"({overrun:+.2f}s against the {r['duration_s']:.1f}s asked for)")

    # Rates need a span to divide by, and a span needs two calls.  Reporting
    # "81 frames/s" off a single 14 s call, as this did on the first run, is
    # an artefact of dividing by a default.
    if len(r["drains"]) < 2:
        print(f"  {YELLOW}only {len(r['drains'])} drain call(s) in the whole "
              f"burst{RESET} - too few to rate; check how long each one took")
    for e in r["drains"]:
        print(f"    drain t={e['t']:6.2f}s  took {e['took_ms']:6d} ms  "
              f"freed {e['freed']:3d}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("held", "paced", "both"), default="held")
    ap.add_argument("--duration", type=float, default=8.0,
                    help="seconds per burst (default 8, near the C2 hold)")
    ap.add_argument("--interval", type=float, default=0.20,
                    help="paced pulse interval, seconds (default 0.20 = 5 fps)")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--no-sample", action="store_true",
                    help="skip the depth sampler; leaves only the drain record")
    args = ap.parse_args()

    tee_console("burst_queue_probe")
    modes = ([None] if args.mode == "held"
             else [args.interval] if args.mode == "paced"
             else [None, args.interval])
    print(f"Camera on, relay on S1=ch1 S2=ch2, workbench closed.")
    print(f"{args.rounds} x {len(modes)} burst(s) of {args.duration:.1f}s - "
          f"the shutter fires for real.")
    if input("Ready? [y/n] > ").strip().lower() not in ("y", "yes"):
        return

    cameras = detect_fuji_cameras(find_fuji_sdk_path())
    if not cameras:
        print(f"{RED}SDK sees no camera - power cycle with the cable in.{RESET}")
        return
    name, camera = next(iter(cameras.items()))
    sdk = sdk_of(camera)
    print(f"{GREEN}Connected:{RESET} {name}")

    try:
        relay = rt.open_trigger("auto", s1_channel=1, s2_channel=2)
    except Exception as exc:
        print(f"{RED}No relay: {exc}{RESET}")
        camera.disconnect()
        return

    spy = DrainSpy(camera)
    register_hardware("sdk_camera", spy)

    results = []
    try:
        for n in range(1, args.rounds + 1):
            for interval in modes:
                kind = "held" if interval is None else f"paced {interval:.2f}s"
                print(f"\nround {n}, {kind} ...")
                r = run_burst(spy, sdk, relay, args.duration, interval,
                              not args.no_sample)
                r["round"] = n
                report(r)
                results.append(r)
                time.sleep(3.0)         # let the card and the queue settle
    finally:
        register_hardware("sdk_camera", None)
        try:
            relay.release_all()
            relay.close()
        except Exception:
            pass
        try:
            camera.drain()
        except Exception:
            pass
        camera.disconnect()

    path = Path.cwd() / f"burst_queue_probe_{int(time.time())}.json"
    path.write_text(json.dumps(results, indent=2))

    print("\n" + "=" * 60)
    held = [r for r in results if r["interval"] is None]
    paced = [r for r in results if r["interval"] is not None]
    for label, group in (("held", held), ("paced", paced)):
        if not group:
            continue
        sat = [r["saturated_at_s"] for r in group if r["saturated_at_s"] is not None]
        print(f"{label:>6s}: {sum(r['frames'] for r in group) / len(group):5.1f} "
              f"frames avg, saturated "
              + (f"at {sum(sat) / len(sat):.2f}s" if sat else "never"))

    if held and paced:
        h = [r["saturated_at_s"] for r in held if r["saturated_at_s"] is not None]
        p = [r["saturated_at_s"] for r in paced if r["saturated_at_s"] is not None]
        print()
        if not h:
            # The premise, not the answer.  Pacing was proposed to postpone a
            # ceiling the held burst never reached, so there is nothing here
            # for it to buy - whatever limits the rate, it is not the queue.
            print(f"{YELLOW}The held burst never saturated the queue.{RESET}  The "
                  f"sag is not the 32-slot ceiling, so an ease-in has nothing to "
                  f"postpone. Compare the fps instead: "
                  f"held {sum(r['fps'] for r in held) / len(held):.2f}, "
                  f"paced {sum(r['fps'] for r in paced) / len(paced):.2f}.")
        elif not p:
            print(f"{GREEN}Pacing keeps the queue off the ceiling.{RESET}  An "
                  f"ease-in head is worth building into the schedule.")
        elif sum(p) / len(p) > sum(h) / len(h) * 1.5:
            print(f"{GREEN}Pacing delays saturation substantially.{RESET}  An "
                  f"ease-in head buys full rate later in the burst.")
        else:
            print(f"{RED}Pacing does not delay saturation.{RESET}  The ease-in "
                  f"idea is dead; shift the burst window instead.")
    print(f"\n{path}")


if __name__ == "__main__":
    main()
