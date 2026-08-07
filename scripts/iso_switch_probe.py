"""Can the ISO be changed during totality, or only between eclipses?

Every totality frame currently runs at ISO 1600 - one gain, one noise
character to match when they are stacked.  Photographically that is a
compromise: on this sensor the low end has its dynamic range at ISO 160-400,
and the bright rungs (1/8000, 1/2000) would be cleaner and deeper there,
while only the faint end really wants the gain.

Splitting the ISO is only worth discussing if the body will take the writes.
`fuji_camera` says it will not: "set_iso is refused with 0x1006 unless the
transfer queue is empty, while set_shutter_speed tolerates pending frames".
If that holds, a split ISO fails during totality exactly the way the ladder's
shutter writes did - the queue is never empty between C2 and C3 - and the
answer is settled by the body rather than by taste.

Three conditions, ISO written and read back each time:

  1  queue empty        the control
  2  queue part full    a few frames pending, as between ladders
  3  queue loaded       what totality actually looks like

    .venv/bin/python scripts/iso_switch_probe.py
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

#: What a split would actually ask for: down for the bright rungs, up for the
#: faint ones, back again for the next ladder.
ISO_PAIRS = ((200, 1600), (400, 1600), (1600, 200))


def sdk_of(camera):
    return getattr(camera, "_sdk", None) or getattr(camera, "_sdk_cam", None)


def try_iso(sdk, value, budget_s=0.8):
    """Write one ISO and read it back.  Returns (landed, ms, error)."""
    started, error = time.monotonic(), None
    deadline = started + budget_s
    while True:
        try:
            sdk.set_iso(value)
            error = None
            break
        except Exception as exc:
            error = exc
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    ms = (time.monotonic() - started) * 1000
    try:
        got = sdk.get_iso()
    except Exception as exc:
        return False, ms, f"read-back failed: {exc}"
    return got == value, ms, (str(error) if error else None)


def condition(name, sdk, camera, relay, load_frames):
    print(f"\n{name}")
    relay.release_all()
    camera.drain()
    if load_frames:
        with relay.pressed():
            time.sleep(load_frames)
        relay.release_all()
    try:
        pending, _ = sdk.get_buffer_capacity()
    except Exception:
        pending = -1
    print(f"   queue holds {pending} frame(s)")
    rows = []
    for low, high in ISO_PAIRS:
        for value in (low, high):
            landed, ms, error = try_iso(sdk, value)
            mark = f"{GREEN}landed{RESET}" if landed else f"{RED}REFUSED{RESET}"
            note = f"  {str(error)[:46]}" if error else ""
            print(f"   ISO {value:<5d} {mark}  {ms:5.0f} ms{note}")
            rows.append({"iso": value, "landed": landed, "ms": round(ms),
                         "error": error})
    ok = sum(1 for r in rows if r["landed"])
    print(f"   -> {ok}/{len(rows)} landed")
    return {"condition": name, "pending": pending, "landed": ok,
            "of": len(rows), "writes": rows}


def main():
    tee_console("iso_switch_probe")
    print("Camera on, relay connected, workbench closed.")
    print("The shutter fires a few times to load the queue.")
    if input("Ready? [y/n] > ").strip().lower() not in ("y", "yes"):
        return

    cameras = detect_fuji_cameras(find_fuji_sdk_path())
    if not cameras:
        print(f"{RED}SDK sees no camera.{RESET}")
        return
    name, camera = next(iter(cameras.items()))
    sdk = sdk_of(camera)
    original = sdk.get_iso()
    print(f"{GREEN}Connected:{RESET} {name}, ISO {original}")

    try:
        relay = rt.open_trigger("auto", s1_channel=1, s2_channel=2)
    except Exception as exc:
        print(f"{RED}No relay: {exc}{RESET}")
        camera.disconnect()
        return

    out = []
    try:
        out.append(condition("1. queue empty (the control)", sdk, camera, relay, 0))
        out.append(condition("2. queue part full, as between ladders",
                             sdk, camera, relay, 1.0))
        out.append(condition("3. queue loaded, as during totality",
                             sdk, camera, relay, 3.0))
    finally:
        try:
            relay.release_all()
            relay.close()
        except Exception:
            pass
        try:
            sdk.set_iso(original)
            camera.drain()
        except Exception:
            pass
        camera.disconnect()

    path = Path.cwd() / f"iso_switch_probe_{int(time.time())}.json"
    path.write_text(json.dumps(out, indent=2))
    print("\n" + "=" * 58)
    for r in out:
        colour = GREEN if r["landed"] == r["of"] else RED
        print(f"{colour}{r['landed']}/{r['of']}{RESET}  {r['condition']} "
              f"({r['pending']} pending)")
    loaded = out[-1]
    print()
    if loaded["landed"] == loaded["of"]:
        print(f"{GREEN}The ISO can be changed with the queue loaded.{RESET}  A split "
              f"ISO is affordable; decide it on the picture, not the plumbing.")
    elif loaded["landed"] == 0:
        print(f"{RED}The ISO cannot be changed while frames are pending.{RESET}  A "
              f"split ISO would fail through totality exactly as the ladder's "
              f"shutter writes did: one gain for the whole of it.")
    else:
        print(f"{YELLOW}The ISO changes sometimes with the queue loaded{RESET} - "
              f"which for frames that cannot be retaken is the same as no.")
    print(f"\n{path}")


if __name__ == "__main__":
    main()
