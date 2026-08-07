"""Why every corona ladder shot at the wrong exposure, tested on the body.

The rehearsal of 7 August ran eight ladders inside totality and every one of
the 45 shutter-speed writes was refused with 0x1006 "Camera is busy" - not
one succeeded, and 225 bracket frames were taken at whatever speed happened
to be on the body.

The suspicion is the half-press.  `bracket_no_download` holds S1 closed for
the whole ladder and writes each rung's speed with the contact still down;
"the shutter is half pressed" is the SDK's own first blocker, and this file
already knows that draining with S1 held kills the session outright.  If that
is right, the condition lasts the whole ladder and no budget can help - which
matches 45 failures and zero successes exactly.

Six phases.  Every speed is read back from the body rather than trusting a
return code, because a write that is accepted and lands somewhere else is the
failure that looks like success.

  1  contacts open           the control: writes should all land
  2  S1 held                 what the ladder does now
  3  released around write   the proposed fix, timed so the cost is known
  4  as 3, queue loaded      because at C2 a ladder always follows a burst

Then two that look for seconds rather than for the bug:

  5  write latency           BRACKET_STEP_BUDGET_S is 0.3 s against a 0.3 s
                             backoff, which is one attempt per rung, and
                             nobody has measured what a write costs
  6  gap sweep               the ladder waits max(0.35, exposure + 0.3)
                             between taps; neither number was measured.  Six
                             rungs at 100 ms saved is 0.6 s a ladder, and
                             eight ladders is most of a ninth

Phase 6 proves a speed *landed*, not that the frame *used* it - only EXIF
from the card can say that, so its answer is a candidate to confirm on the
card, never a value to ship straight into the generator.

The shutter fires: this taps between rungs exactly as the ladder does, so the
timings mean something.  Indoors, lens cap on, no filter needed.

    .venv/bin/python scripts/ladder_write_probe.py
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

from solareclipseworkbench import relay_trigger as rt
from bench_log import tee_console

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"

#: The production corona ladder as the body actually receives it, in
#: microseconds.  Seven rungs - 1/8000 to 1/2 - on the body's power-of-two
#: scale, which is why they are not the round numbers the script writes.
LADDER_US = (122, 488, 1953, 7812, 31250, 125000, 500000)

TAP_S = 0.05          # fuji_camera.TAP_S, after the 7 August fix
TAP_GAP_S = 0.35      # fuji_camera.TAP_GAP_S


def sdk_of(camera):
    return getattr(camera, "_sdk", None) or getattr(camera, "_sdk_cam", None)


def write_and_verify(sdk, speed_us, budget_s=0.3):
    """Set one speed and ask the body what it ended up on.

    Returns (landed, seconds_taken, error).  `landed` is the read-back
    comparison, not the return code: a write that is accepted and lands
    somewhere else is the failure that looks like success.
    """
    started = time.monotonic()
    error = None
    deadline = started + budget_s
    while True:
        try:
            sdk.set_shutter_speed(speed_us)
            error = None
            break
        except Exception as exc:
            error = exc
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    elapsed = time.monotonic() - started
    try:
        got, _bulb = sdk.get_shutter_speed()
    except Exception as exc:
        return False, elapsed, f"read-back failed: {exc}"
    return got == speed_us, elapsed, (str(error) if error else None)


def phase(name, sdk, relay, mode, tap=True, camera=None):
    """One pass over the ladder in one of the three modes.

    Frames are counted as well as writes.  With the body on CL, a tap taken
    while S1 is held is not one frame - the contact keeps the drive running -
    and that changes both what lands on the card and how fast the queue
    fills.
    """
    print(f"\n{name}")
    before = None
    if camera is not None:
        try:
            camera.drain()
            before, _ = sdk.get_buffer_capacity()
        except Exception:
            before = None
    results = []
    if mode == "held":
        relay.half_press()
    try:
        for speed in LADDER_US:
            if mode == "released":
                relay.release_all()
            landed, took, error = write_and_verify(sdk, speed)
            if mode == "released":
                relay.half_press()
            if tap:
                relay.shoot(pulse=TAP_S)
                time.sleep(max(TAP_GAP_S, speed / 1e6 + 0.3))
            results.append((speed, landed, took, error))
            mark = f"{GREEN}landed{RESET}" if landed else f"{RED}REFUSED{RESET}"
            note = f"  {str(error)[:52]}" if error else ""
            print(f"   1/{round(1e6 / speed):<6d} {mark}  {took * 1000:5.0f} ms{note}")
    finally:
        relay.release_all()
    ok = sum(1 for _, landed, _, _ in results if landed)
    total_ms = sum(t for _, _, t, _ in results) * 1000
    frames = None
    if before is not None:
        time.sleep(1.2)
        try:
            after, _ = sdk.get_buffer_capacity()
            frames = after - before
        except Exception:
            frames = None
    tail = f", {frames} frame(s) for {len(results)} taps" if frames is not None else ""
    print(f"   -> {ok}/{len(results)} landed, {total_ms:.0f} ms of writes{tail}")
    return {"phase": name, "landed": ok, "of": len(results), "frames": frames,
            "write_ms": round(total_ms),
            "rungs": [{"us": s, "landed": l, "ms": round(t * 1000), "error": e}
                      for s, l, t, e in results]}


def no_hold_ladder(sdk, relay, camera):
    """The ladder with S1 never held, and a budget big enough for a real write.

    Phases 1-4 say the blocker is not the half-press itself but the body still
    shooting: holding S1 keeps the CL drive running - six taps produced 32
    frames - and a body mid-exposure refuses settings.  With the queue full,
    where it physically cannot shoot, every write landed.

    So: no hold at all, one short tap per rung, and a budget set from the
    measured cost of a write rather than from 0.3 s.  Frames are counted
    because a ladder wants exactly one per rung, and in CL a long pulse can
    fire twice.
    """
    print("\n7. no S1 hold, one tap a rung, budget from the measurements")
    results = []
    for pulse, budget in ((0.08, 0.8), (0.05, 0.8), (0.03, 0.8)):
        relay.release_all()
        camera.drain()
        try:
            before, _ = sdk.get_buffer_capacity()
        except Exception:
            before = None
        landed, started = 0, time.monotonic()
        for speed in LADDER_US:
            ok, _, _ = write_and_verify(sdk, speed, budget_s=budget)
            landed += 1 if ok else 0
            relay.shoot(pulse=pulse)
            time.sleep(max(TAP_GAP_S, speed / 1e6 + 0.3))
        elapsed = time.monotonic() - started
        time.sleep(1.2)
        try:
            after, _ = sdk.get_buffer_capacity()
            frames = after - before if before is not None else None
        except Exception:
            frames = None
        mark = GREEN if landed == len(LADDER_US) else RED
        want = GREEN if frames == len(LADDER_US) else YELLOW
        print(f"   pulse {pulse:.2f}s  {mark}{landed}/{len(LADDER_US)} landed{RESET}"
              f"  {want}{frames} frame(s){RESET} for {len(LADDER_US)} rungs"
              f"  {elapsed:.2f}s")
        results.append({"pulse_s": pulse, "budget_s": budget, "landed": landed,
                        "frames": frames, "elapsed_s": round(elapsed, 2)})
        camera.drain()
    return {"phase": "7. no hold", "results": results}


def latency_profile(sdk, relay, samples=40):
    """How long a write really takes, so the budget can be set from evidence.

    BRACKET_STEP_BUDGET_S is 0.3 s with a 0.3 s backoff, which is one attempt
    per rung.  If a write lands in 20 ms the budget is wildly generous and can
    both shrink and be retried; if it takes 250 ms the ladder has no slack at
    all.  Nobody has measured it.
    """
    print("\n5. how long one write actually takes (S1 open, no taps)")
    relay.release_all()
    times = []
    for i in range(samples):
        speed = LADDER_US[i % len(LADDER_US)]
        landed, took, _ = write_and_verify(sdk, speed, budget_s=1.0)
        if landed:
            times.append(took * 1000)
    if not times:
        print(f"   {RED}no write landed{RESET}")
        return {"phase": "5. write latency", "samples": 0}
    times.sort()
    med = times[len(times) // 2]
    p90 = times[int(len(times) * 0.9)]
    print(f"   {len(times)} writes: median {med:.0f} ms, 90th {p90:.0f} ms, "
          f"worst {times[-1]:.0f} ms")
    print(f"   -> a budget of {max(0.1, p90 * 3 / 1000):.2f} s would allow three "
          f"tries at the 90th percentile")
    return {"phase": "5. write latency", "samples": len(times),
            "median_ms": round(med), "p90_ms": round(p90),
            "worst_ms": round(times[-1])}


def gap_sweep(sdk, relay, camera):
    """The shortest gap between rungs that still lets every speed land.

    The ladder waits max(0.35, exposure + 0.3) between taps.  That 0.3 s
    settle and the 0.35 s floor were never measured; at six rungs a saving of
    100 ms each is 0.6 s a ladder, and eight ladders is most of another one.

    A speed that reads back correctly is not proof the *frame* used it - only
    EXIF from the card can say that - so the shortest gap that passes here is
    a candidate to confirm on the card, not a value to ship.
    """
    print("\n6. how short the gap between rungs can be")
    results = []
    for gap in (0.35, 0.25, 0.15, 0.10):
        relay.release_all()
        camera.drain()
        try:
            before, _ = sdk.get_buffer_capacity()
        except Exception:
            before = None
        landed = 0
        started = time.monotonic()
        for speed in LADDER_US:
            # The fixed ladder: S1 is never closed, the budget is the measured
            # one.  The first sweep ran the old held-S1 pattern and returned
            # 1-2 of 6 at every gap, which said nothing about the gap.
            ok, _, _ = write_and_verify(sdk, speed, budget_s=0.8)
            landed += 1 if ok else 0
            relay.shoot(pulse=TAP_S)
            time.sleep(max(gap, speed / 1e6 + gap))
        relay.release_all()
        elapsed = time.monotonic() - started
        time.sleep(1.0)
        try:
            after, _ = sdk.get_buffer_capacity()
            frames = (after - before) if before is not None else None
        except Exception:
            frames = None
        mark = GREEN if landed == len(LADDER_US) else RED
        print(f"   gap {gap:.2f}s  {mark}{landed}/{len(LADDER_US)} landed{RESET}"
              f"  ladder took {elapsed:.2f}s"
              + (f", {frames} frame(s) captured" if frames is not None else ""))
        results.append({"gap_s": gap, "landed": landed, "elapsed_s": round(elapsed, 2),
                        "frames": frames})
        camera.drain()
    good = [r for r in results if r["landed"] == len(LADDER_US)]
    if good:
        best = min(good, key=lambda r: r["elapsed_s"])
        slowest = max(results, key=lambda r: r["elapsed_s"])
        saved = slowest["elapsed_s"] - best["elapsed_s"]
        print(f"   -> shortest gap that still lands every speed: {best['gap_s']:.2f}s, "
              f"saving {saved:.2f}s a ladder ({saved * 8:.1f}s over eight)")
        print(f"   {YELLOW}confirm on the card before shipping: EXIF says which "
              f"speed each frame really used{RESET}")
    return {"phase": "6. gap sweep", "results": results}


def main():
    tee_console("ladder_write_probe")
    print("Camera on, USB connected, relay on S1=ch1 S2=ch2.")
    print("The shutter WILL fire about 25 times.  Lens cap on is fine.")
    print("Close Solar Eclipse Workbench first - the SDK takes one session.")
    if input("Ready? [y/n] > ").strip().lower() not in ("y", "yes"):
        return

    cameras = detect_fuji_cameras(find_fuji_sdk_path())
    if not cameras:
        print(f"{RED}SDK sees no camera - power cycle with the cable in.{RESET}")
        return
    name, camera = next(iter(cameras.items()))
    sdk = sdk_of(camera)
    if sdk is None:
        print(f"{RED}No SDK handle on the adapter.{RESET}")
        return
    original, _ = sdk.get_shutter_speed()
    print(f"{GREEN}Connected:{RESET} {name}, body on {original} us")

    try:
        relay = rt.open_trigger("auto", s1_channel=1, s2_channel=2)
    except Exception as exc:
        print(f"{RED}No relay: {exc}{RESET}")
        print("Phases 2-6 need it to hold S1 and tap.  Plug it in and rerun.")
        camera.disconnect()
        return
    out = []
    try:
        relay.release_all()
        camera.drain()
        out.append(phase("1. contacts open (the control)", sdk, relay, "open", camera=camera))

        relay.release_all()
        camera.drain()
        out.append(phase("2. S1 held for the whole ladder (what it does now)",
                         sdk, relay, "held", camera=camera))

        relay.release_all()
        camera.drain()
        out.append(phase("3. S1 released around each write (proposed fix)",
                         sdk, relay, "released", camera=camera))

        # And again with the queue loaded, because at C2 a ladder always
        # follows a burst that has just filled it.
        print(f"\n{YELLOW}Loading the transfer queue with a 3 s hold"
              f"{RESET}")
        with relay.pressed():
            time.sleep(3.0)
        relay.release_all()
        try:
            captured, _ = sdk.get_buffer_capacity()
            print(f"   buffer now holds {captured} frame(s)")
        except Exception:
            pass
        out.append(phase("4. S1 released around each write, queue loaded",
                         sdk, relay, "released"))
        out.append(no_hold_ladder(sdk, relay, camera))
        out.append(latency_profile(sdk, relay))
        out.append(gap_sweep(sdk, relay, camera))
    finally:
        try:
            relay.release_all()
            relay.close()
        except Exception:
            pass
        try:
            sdk.set_shutter_speed(original)
            camera.drain()
        except Exception:
            pass
        camera.disconnect()

    path = Path.cwd() / f"ladder_write_probe_{int(time.time())}.json"
    path.write_text(json.dumps(out, indent=2))

    print("\n" + "=" * 62)
    for r in out:
        if "landed" not in r:
            continue
        colour = GREEN if r["landed"] == r["of"] else RED
        frames = f"  {r['frames']} frame(s)" if r.get("frames") is not None else ""
        print(f"{colour}{r['landed']}/{r['of']}{RESET}  {r['phase']}{frames}")
    held = next((r for r in out if r["phase"].startswith("2.")), None)
    open_ = next((r for r in out if r["phase"].startswith("1.")), None)
    fixed = next((r for r in out if r["phase"].startswith("3.")), None)
    print()
    if held and open_ and fixed:
        if held["landed"] < open_["landed"] and fixed["landed"] >= open_["landed"]:
            extra = fixed["write_ms"] - held["write_ms"]
            print(f"{GREEN}The half-press is the cause.{RESET}  Releasing S1 around "
                  f"the write recovers it and costs {extra:+.0f} ms per ladder "
                  f"({extra / len(LADDER_US):+.0f} ms a rung).")
        elif held["landed"] == open_["landed"]:
            print(f"{YELLOW}The half-press is NOT the cause{RESET} - writes landed "
                  f"with S1 held too.  Look at the queue and the 0.3 s budget.")
        else:
            print(f"{YELLOW}Mixed result - read the per-rung lines above.{RESET}")
    print(f"\n{path}")


if __name__ == "__main__":
    main()
