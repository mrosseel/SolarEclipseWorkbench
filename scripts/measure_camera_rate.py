"""Measure the sustained single-frame rate of a connected camera.

The same harness for every body, so the numbers can be compared: five runs of ten
frames through the workbench's own capture path, timing the whole run.

Comparing across different harnesses is how several wrong conclusions got drawn
about the X-T4 - a rate from one script is not a rate from another.

    python scripts/measure_camera_rate.py            # first camera found
    python scripts/measure_camera_rate.py "Canon EOS 800D"

Measured so far, five runs each:
    Fujifilm X-T4, macOS   0.52 fps
    Fujifilm X-T4, Linux   0.53 fps
"""

import logging
import sys
import time

from solareclipseworkbench.camera import get_camera_dict, take_picture, CameraSettings

RUNS = 5
FRAMES = 10
SHUTTER, APERTURE, ISO = "1/1000", "8", 400


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    wanted = sys.argv[1] if len(sys.argv) > 1 else None

    cameras = get_camera_dict()
    if not cameras:
        print("no cameras detected")
        return 1
    print("detected:", ", ".join(cameras))

    name = wanted or next(iter(cameras))
    if name not in cameras:
        print("camera %r not found" % name)
        return 1
    camera = cameras[name]
    settings = CameraSettings(name, SHUTTER, APERTURE, ISO)

    print("measuring %s at %s f/%s ISO %d" % (name, SHUTTER, APERTURE, ISO))
    rates = []
    for run in range(RUNS):
        time.sleep(1.0)
        failures = 0
        start = time.perf_counter()
        for _ in range(FRAMES):
            try:
                take_picture(camera, settings)
            except Exception:
                failures += 1
        elapsed = time.perf_counter() - start
        taken = FRAMES - failures
        rates.append(taken / elapsed)
        print("run %d: %d/%d taken, %5.2f s -> %.2f fps"
              % (run + 1, taken, FRAMES, elapsed, taken / elapsed))

    print()
    print("min %.2f  max %.2f  mean %.2f fps"
          % (min(rates), max(rates), sum(rates) / len(rates)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
