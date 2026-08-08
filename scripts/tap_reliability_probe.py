"""Does a 0.03 s tap ever fail to fire a frame?

The ladder wants exactly one frame per rung.  At 0.05 s a tap sometimes
fires twice - eight frames for seven rungs - and at 0.03 s the one sample so
far gave exactly seven.  One sample is not a reliability case, and the two
failures are not equal: an extra frame costs a buffer slot, while a tap too
short to fire costs a corona exposure that cannot be retaken.

So this runs the seven production rungs ten times over and counts the frames
each round.  The bar is not "exactly seven every time" - a double is
tolerable - it is **never fewer than seven**.  One miss in ten rounds and
0.03 s is not safe for totality.

    .venv/bin/python scripts/tap_reliability_probe.py [--pulse 0.03] [--rounds 10]
"""

import argparse
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

#: The production corona ladder on the body's own scale, in microseconds.
LADDER_US = (122, 488, 1953, 7812, 31250, 125000, 500000)
SPEED_LABEL = {122: "1/8000", 488: "1/2000", 1953: "1/500", 7812: "1/125",
               31250: "1/30", 125000: "1/8", 500000: "0.5"}
TAP_GAP_S = 0.35
WRITE_BUDGET_S = 0.8


def sdk_of(camera):
    return getattr(camera, "_sdk", None) or getattr(camera, "_sdk_cam", None)


def write(sdk, speed_us, budget_s=WRITE_BUDGET_S):
    deadline = time.monotonic() + budget_s
    while True:
        try:
            sdk.set_shutter_speed(speed_us)
            break
        except Exception:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
    try:
        got, _ = sdk.get_shutter_speed()
        return got == speed_us
    except Exception:
        return False


#: A frame takes 0.35-0.75 s to appear in the buffer count, so a per-rung read
#: has to wait for it.  This lengthens the ladder and is therefore diagnostic
#: timing, not production timing - which is why it is opt-in.
RUNG_SETTLE_S = 0.9


def one_round(sdk, relay, camera, pulse, order=LADDER_US, per_rung=False):
    """One ladder.  Returns (speeds_landed, frames_fired, seconds, per_rung).

    ``per_rung`` reads the buffer after every rung instead of once at the end,
    which is the only way to see WHICH rung fires twice.  It costs a settle per
    rung, so the ladder no longer runs at production speed.
    """
    relay.release_all()
    camera.drain()
    time.sleep(0.4)
    try:
        before, _ = sdk.get_buffer_capacity()
    except Exception:
        before = 0
    landed, started, rungs = 0, time.monotonic(), []
    running = before
    for position, speed in enumerate(order, start=1):
        landed += 1 if write(sdk, speed) else 0
        relay.shoot(pulse=pulse)
        time.sleep(max(TAP_GAP_S, speed / 1e6 + 0.3))
        if not per_rung:
            continue
        time.sleep(RUNG_SETTLE_S)
        try:
            now, _ = sdk.get_buffer_capacity()
        except Exception:
            now = running
        rungs.append({"position": position, "speed_us": speed,
                      "frames": now - running})
        running = now
    elapsed = time.monotonic() - started
    time.sleep(1.5)                       # let the last frame reach the queue
    try:
        after, _ = sdk.get_buffer_capacity()
        frames = after - before
    except Exception:
        frames = -1
    return landed, frames, elapsed, rungs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pulse", type=float, default=0.03)
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--per-rung", action="store_true",
                    help="count frames after every rung, not just per ladder")
    ap.add_argument("--order", choices=("forward", "reverse", "both"),
                    default="forward",
                    help="'both' alternates, which is what separates a "
                         "first-rung effect from a fastest-rung one")
    args = ap.parse_args()
    if args.order != "forward":
        # The card cannot tell the two candidate causes apart because 1/8000 is
        # both the first rung and the fastest.  Running the ladder backwards
        # separates them: the doubles either follow position 1 or they follow
        # 1/8000, and they cannot follow both.
        args.per_rung = True

    tee_console("tap_reliability_probe")
    total = args.rounds * len(LADDER_US)
    print(f"Camera on, relay on S1=ch1 S2=ch2, workbench closed.")
    print(f"{args.rounds} ladders of {len(LADDER_US)} rungs at a "
          f"{args.pulse:.2f} s tap - about {total} actuations.")
    if input("Ready? [y/n] > ").strip().lower() not in ("y", "yes"):
        return

    cameras = detect_fuji_cameras(find_fuji_sdk_path())
    if not cameras:
        print(f"{RED}SDK sees no camera.{RESET}")
        return
    name, camera = next(iter(cameras.items()))
    sdk = sdk_of(camera)
    original, _ = sdk.get_shutter_speed()
    print(f"{GREEN}Connected:{RESET} {name}\n")

    try:
        relay = rt.open_trigger("auto", s1_channel=1, s2_channel=2)
    except Exception as exc:
        print(f"{RED}No relay: {exc}{RESET}")
        camera.disconnect()
        return

    rounds = []
    try:
        for n in range(1, args.rounds + 1):
            if args.order == "reverse" or (args.order == "both" and n % 2 == 0):
                order, sense = tuple(reversed(LADDER_US)), "reverse"
            else:
                order, sense = LADDER_US, "forward"
            landed, frames, elapsed, rungs = one_round(
                sdk, relay, camera, args.pulse, order=order,
                per_rung=args.per_rung)
            missed = frames < len(LADDER_US)
            mark = f"{RED}MISSED{RESET}" if missed else f"{GREEN}ok{RESET}"
            print(f"  round {n:2d}  {sense:7s}  {landed}/{len(LADDER_US)} speeds  "
                  f"{frames} frames  {elapsed:.2f}s  {mark}")
            for rung in rungs:
                label = SPEED_LABEL.get(rung["speed_us"], f"{rung['speed_us']}us")
                extra = rung["frames"] - 1
                colour = (GREEN if extra == 0 else
                          YELLOW if extra > 0 else RED)
                note = ("" if extra == 0 else
                        f"  <- {'DOUBLE' if extra > 0 else 'MISSED'}")
                print(f"        pos {rung['position']}  {label:>8s}  "
                      f"{colour}{rung['frames']} frame(s){RESET}{note}")
            rounds.append({"round": n, "order": sense, "landed": landed,
                           "frames": frames, "elapsed_s": round(elapsed, 2),
                           "rungs": rungs})
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

    got = [r["frames"] for r in rounds if r["frames"] >= 0]
    misses = [r for r in rounds if 0 <= r["frames"] < len(LADDER_US)]
    speeds_ok = all(r["landed"] == len(LADDER_US) for r in rounds)
    path = Path.cwd() / f"tap_reliability_{args.pulse:.2f}_{int(time.time())}.json"
    path.write_text(json.dumps({"pulse_s": args.pulse, "rounds": rounds}, indent=2))

    print("\n" + "=" * 58)

    doubles = [(r["order"], g) for r in rounds for g in r.get("rungs", [])
               if g["frames"] > 1]
    if any(r.get("rungs") for r in rounds):
        by_position, by_speed = {}, {}
        for _sense, g in doubles:
            by_position[g["position"]] = by_position.get(g["position"], 0) + 1
            by_speed[g["speed_us"]] = by_speed.get(g["speed_us"], 0) + 1
        print(f"{len(doubles)} double(s) across "
              f"{sum(len(r.get('rungs', [])) for r in rounds)} rungs")
        if doubles:
            print("  by position: " + ", ".join(
                f"pos {p}: {n}" for p, n in sorted(by_position.items())))
            print("  by speed:    " + ", ".join(
                f"{SPEED_LABEL.get(s, s)}: {n}"
                for s, n in sorted(by_speed.items())))
            # The discriminator.  1/8000 is both the first rung and the fastest
            # in a forward ladder, so only a reversed one can separate them.
            if len(by_position) == 1 and len(by_speed) > 1:
                print(f"\n{YELLOW}Every double is at position "
                      f"{next(iter(by_position))}, across different speeds"
                      f"{RESET} - a first-tap effect, not the shutter speed. "
                      f"Fix by priming: one throwaway tap before the ladder.")
            elif len(by_speed) == 1 and len(by_position) > 1:
                print(f"\n{YELLOW}Every double is at "
                      f"{SPEED_LABEL.get(next(iter(by_speed)))}, at different "
                      f"positions{RESET} - the speed, not the order. "
                      f"An electronic-shutter re-arm at the fastest rung.")
            elif args.order == "forward":
                print(f"\n{YELLOW}Forward only: position and speed still move "
                      f"together{RESET} - rerun with --order both to separate "
                      f"them.")
            else:
                print(f"\n{YELLOW}Doubles spread over both position and speed"
                      f"{RESET} - neither candidate explains it alone.")
        else:
            print(f"{GREEN}No rung fired twice{RESET} - the card's 11 frames at "
                  f"1/8000 did not reproduce here.")

    if got:
        print(f"frames per ladder: min {min(got)}, max {max(got)}, "
              f"want at least {len(LADDER_US)}")
    print(f"every speed landed in every round: "
          f"{GREEN + 'yes' + RESET if speeds_ok else RED + 'NO' + RESET}")
    if misses:
        print(f"{RED}{len(misses)} round(s) fired short - "
              f"{args.pulse:.2f} s is NOT safe for totality{RESET}")
        for m in misses:
            print(f"   round {m['round']}: {m['frames']} frames")
    else:
        print(f"{GREEN}no round ever fired short: {args.pulse:.2f} s never "
              f"missed a rung in {len(rounds)} ladders{RESET}")
    print(f"\n{path}")
    sys.exit(1 if misses or not speeds_ok else 0)


if __name__ == "__main__":
    main()
