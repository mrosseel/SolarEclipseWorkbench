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

Three phases, each writing the same seven speeds and reading every one back
from the body rather than trusting a return code:

  1  contacts open          the control: writes should all land
  2  S1 held                what the ladder does now
  3  released around write   the proposed fix, timed so the cost is known

A fourth phase repeats 1 and 3 with the transfer queue deliberately loaded,
because at C2 the ladder always runs behind a burst.

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

#: The corona ladder as the body actually receives it, in microseconds -
#: taken from the rehearsal log, where every one of these was refused.
LADDER_US = (488, 1953, 7812, 31250, 125000, 500000)

TAP_S = 0.08          # fuji_camera.TAP_S
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


def phase(name, sdk, relay, mode, tap=True):
    """One pass over the ladder in one of the three modes."""
    print(f"\n{name}")
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
    print(f"   -> {ok}/{len(results)} landed, {total_ms:.0f} ms of writes")
    return {"phase": name, "landed": ok, "of": len(results),
            "write_ms": round(total_ms),
            "rungs": [{"us": s, "landed": l, "ms": round(t * 1000), "error": e}
                      for s, l, t, e in results]}


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

    relay = rt.open_trigger("auto", s1_channel=1, s2_channel=2)
    out = []
    try:
        relay.release_all()
        camera.drain()
        out.append(phase("1. contacts open (the control)", sdk, relay, "open"))

        relay.release_all()
        camera.drain()
        out.append(phase("2. S1 held for the whole ladder (what it does now)",
                         sdk, relay, "held"))

        relay.release_all()
        camera.drain()
        out.append(phase("3. S1 released around each write (proposed fix)",
                         sdk, relay, "released"))

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
        colour = GREEN if r["landed"] == r["of"] else RED
        print(f"{colour}{r['landed']}/{r['of']}{RESET}  {r['phase']}")
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
