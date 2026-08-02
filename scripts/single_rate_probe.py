"""Measure what one scripted single frame costs, start to finish.

Brackets are the expensive part of totality and singles are what fills the gaps
between them, so the cost of a single is the resolution of the whole plan: at
1.5 s a 15 s gap holds ten frames, at 0.4 s it holds thirty-five.

Times ``configure`` and ``capture`` exactly as the scheduler calls them, over a
run long enough to include the occasional drain that clearing the queue costs.

    ./run.sh scripts/single_rate_probe.py
    ./run.sh scripts/single_rate_probe.py --frames 40 --speed 1/500
"""

import argparse
import logging
import statistics
import sys
import time
from pathlib import Path

# fujixsdk lives in the repo root, which is not on sys.path when this file is
# run as scripts/single_rate_probe.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solareclipseworkbench.camera import CameraSettings, take_picture
from solareclipseworkbench.fuji_camera import (detect_fuji_cameras, find_fuji_sdk_path,
                                               maybe_reexec_for_fuji_sdk)
from solareclipseworkbench.hardware_registry import register_hardware
from solareclipseworkbench.relay_trigger import open_trigger

maybe_reexec_for_fuji_sdk()

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m")

FAST_SPEED = "1/500"
FRAMES = 30


def connect():
    sdk_path = find_fuji_sdk_path()
    if not sdk_path:
        print(f"{RED}No Fuji SDK found.{RESET}  Set FUJI_SDK_PATH and rerun.")
        return None
    cameras = detect_fuji_cameras(sdk_path)
    if not cameras:
        print(f"{RED}The SDK cannot open the body.{RESET}")
        return None
    name, camera = next(iter(cameras.items()))
    print(f"{GREEN}Connected:{RESET} {name}")
    return camera


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--frames", type=int, default=FRAMES,
                        help=f"frames to time (default: {FRAMES})")
    parser.add_argument("--speed", default=FAST_SPEED,
                        help=f"shutter speed (default: {FAST_SPEED})")
    parser.add_argument("--iso", type=int, default=400)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

    print(f"{BOLD}Single frame rate probe{RESET}")
    print(f"{DIM}{args.frames} frames at {args.speed}, timed as the scheduler "
          f"issues them.{RESET}\n")

    camera = connect()
    if camera is None:
        return
    try:
        register_hardware('relay', open_trigger('auto', s1_channel=1, s2_channel=2))
    except Exception as exc:
        print(f"{RED}No relay ({exc}) — this measures the relay path.{RESET}")
        return

    settings = CameraSettings(camera.name, args.speed, "-", args.iso)
    times = []
    started_all = time.perf_counter()
    try:
        for n in range(args.frames):
            started = time.perf_counter()
            take_picture(camera, settings)
            times.append(time.perf_counter() - started)
            if (n + 1) % 10 == 0:
                print(f"{DIM}  {n + 1} frames, slowest so far {max(times):.2f}s{RESET}")
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Stopped early.{RESET}")
    finally:
        camera.relay.release_all()
        camera.drain()

    if not times:
        return
    elapsed = time.perf_counter() - started_all
    # The mean is what fills a gap; the slowest is what overruns one, and on this
    # body the slowest frame is the one that stopped to clear the queue.
    print(f"\n{BOLD}{len(times)} frames in {elapsed:.1f}s{RESET}")
    print(f"  mean    {statistics.mean(times):.2f}s")
    print(f"  median  {statistics.median(times):.2f}s")
    print(f"  fastest {min(times):.2f}s")
    print(f"  slowest {max(times):.2f}s")
    print(f"\n{DIM}A 15 s gap between brackets holds about "
          f"{int(15 / statistics.mean(times))} of these.{RESET}")


if __name__ == "__main__":
    main()
