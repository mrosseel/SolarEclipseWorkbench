"""Ask the body which exposures it will actually take, one at a time.

The X-T4's SDK module does not implement CapShutterSpeed - it answers with an
empty list - so the limits in :mod:`exposure_limits` are a stated override
rather than anything read from the camera.  This is how that override is
checked against the hardware instead of assumed.

Every speed is written to the body and read back.  A write that returns
without error but reads back as something else is a refusal the SDK did not
report, and that is the failure this exists to catch: it is what leaves a
frame at the previous exposure while the log says the setting was applied.

No frame is ever taken.  The shutter is not fired, the card is not touched,
and whatever the body was set to when the probe started is put back at the
end.

The answer depends on the shutter-type dial, which software cannot read.  On
MS the body stops at 1/8000; on MS+ES or ES it reaches 1/32000.  So run it
once per dial position and compare - that is the whole point of the exercise.

    ./run.sh scripts/exposure_probe.py                     # the configured range
    ./run.sh scripts/exposure_probe.py --script FILE       # what a script needs
    ./run.sh scripts/exposure_probe.py --script FILE --trim +1.0
    ./run.sh scripts/exposure_probe.py --iso-only

Close Solar Eclipse Workbench first: it holds the camera, and two programs in
this SDK at once is what kills the session.
"""

import argparse
import logging
import sys
import time
from pathlib import Path

# fujixsdk lives in the repo root, which is not on sys.path when this file is
# run directly rather than as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fujixsdk._constants import SHUTTER_SPEED_NAMES  # noqa: E402
from solareclipseworkbench import exposure_limits  # noqa: E402
from solareclipseworkbench.fuji_camera import (  # noqa: E402
    _get_speeds_by_seconds, _parse_shutter_speed, detect_fuji_cameras,
    find_fuji_sdk_path)

#: How long to let the body settle before reading a setting back.  A read taken
#: too soon returns the previous value and looks like a refusal.
SETTLE_S = 0.15

#: ISOs worth asking about, inside whatever range is configured.
ISO_LADDER = [100, 125, 160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250,
              1600, 2000, 2500, 3200, 6400, 12800]


def _speeds_in_range():
    """Every speed on the SDK's scale inside the configured limits."""
    lim = exposure_limits.limits()
    return [(secs, value) for secs, value in _get_speeds_by_seconds()
            if lim.fastest_s <= secs <= lim.slowest_s]


def _speeds_for_script(path, trim):
    """The distinct speeds a script will actually ask the body for.

    Resolved through exposure_limits, so this is the set the run will use -
    after the haze trim and after the caps - rather than what the file says.
    """
    wanted = {}
    for row in exposure_limits.validate_script(path, trim_stops=trim):
        for resolved in (row.resolved,) + row.rungs:
            if resolved.speed_s is None:
                continue
            value = _parse_shutter_speed(exposure_limits.format_speed(resolved.speed_s))
            if value is not None:
                wanted.setdefault(resolved.speed_s, value)
    return sorted(wanted.items())


def probe_speeds(camera, speeds) -> list:
    """Write each speed, read it back, and record what happened."""
    results = []
    for secs, value in speeds:
        name = SHUTTER_SPEED_NAMES.get(value, str(value))
        error = None
        try:
            camera._sdk.set_shutter_speed(value)
        except Exception as exc:
            error = str(exc)
        readback = None
        if error is None:
            time.sleep(SETTLE_S)
            try:
                readback, _ = camera._sdk.get_shutter_speed()
            except Exception as exc:
                error = f"could not read back: {exc}"
        # Accepted only when the body says it is on the speed it was given.
        # A silent mismatch is the dangerous case: no error, wrong exposure.
        accepted = error is None and readback == value
        results.append({
            "seconds": secs, "value": value, "name": name,
            "accepted": accepted, "error": error,
            "readback": SHUTTER_SPEED_NAMES.get(readback, readback),
        })
        mark = "ok " if accepted else "NO "
        detail = "" if accepted else f"  <- {error or 'body stayed on ' + str(results[-1]['readback'])}"
        print(f"  {mark} {name:>12}{detail}")
    return results


def probe_isos(camera, isos) -> list:
    results = []
    for iso in isos:
        error = None
        try:
            camera._sdk.set_iso(iso)
        except Exception as exc:
            error = str(exc)
        readback = None
        if error is None:
            time.sleep(SETTLE_S)
            try:
                readback = camera._sdk.get_iso()
            except Exception as exc:
                error = f"could not read back: {exc}"
        accepted = error is None and readback == iso
        results.append({"iso": iso, "accepted": accepted, "error": error,
                        "readback": readback})
        mark = "ok " if accepted else "NO "
        detail = "" if accepted else f"  <- {error or f'body stayed on {readback}'}"
        print(f"  {mark} ISO {iso:<6}{detail}")
    return results


def summarise(speed_results, iso_results) -> None:
    """What the body accepted, and the configuration that matches it."""
    print("\n" + "=" * 68)
    taken = [r for r in speed_results if r["accepted"]]
    refused = [r for r in speed_results if not r["accepted"]]
    print(f"Shutter: {len(taken)} of {len(speed_results)} accepted")
    if refused:
        print("  refused: " + ", ".join(r["name"] for r in refused))
    if taken:
        fastest = min(taken, key=lambda r: r["seconds"])
        slowest = max(taken, key=lambda r: r["seconds"])
        print(f"  range:   {fastest['name']} to {slowest['name']}")

    if iso_results:
        iso_taken = [r for r in iso_results if r["accepted"]]
        print(f"ISO: {len(iso_taken)} of {len(iso_results)} accepted")
        if iso_taken:
            print(f"  range:   {iso_taken[0]['iso']} to {iso_taken[-1]['iso']}")

    if taken:
        fastest = min(taken, key=lambda r: r["seconds"])
        slowest = max(taken, key=lambda r: r["seconds"])
        print("\nMatching [exposure_limits] for ~/.SolarEclipseWorkbench.ini:")
        print(f"  fastest_s={fastest['seconds']:.9g}")
        print(f"  slowest_s={slowest['seconds']:.9g}")
        if iso_results:
            iso_taken = [r for r in iso_results if r["accepted"]]
            if iso_taken:
                print(f"  iso_min={iso_taken[0]['iso']}")
                print(f"  iso_max={iso_taken[-1]['iso']}")
        print("\nThese hold for the shutter type the dial is on right now.  "
              "Run it again after changing MS / MS+ES / ES.")
    # The point of the exercise: does the configured scale match the body?
    scale = exposure_limits.limits().accepted_speeds
    if scale and speed_results:
        import math as _math

        def _on_scale(secs):
            nearest = min(scale, key=lambda s: abs(_math.log(s / secs)))
            return abs(_math.log(nearest / secs)) < 0.01

        wrongly_allowed = [r for r in refused if _on_scale(r["seconds"])]
        wrongly_excluded = [r for r in taken if not _on_scale(r["seconds"])]
        print()
        if not wrongly_allowed and not wrongly_excluded:
            print("The configured scale matches the body exactly: every speed "
                  "the workbench may use was accepted, and every speed it "
                  "excludes was refused.")
        if wrongly_allowed:
            print("REFUSED but on the configured scale - the workbench would "
                  "send these and the frame would keep the previous exposure:")
            print("  " + ", ".join(r["name"] for r in wrongly_allowed))
        if wrongly_excluded:
            print("Accepted but excluded from the configured scale - usable "
                  "speeds the workbench is needlessly avoiding:")
            print("  " + ", ".join(r["name"] for r in wrongly_excluded))

    if refused:
        print("\nA refused speed is not a bug in the schedule - it is a speed "
              "this body will not take in its current mode.  Either the dial "
              "allows it or the limits must exclude it, because a refused "
              "write leaves the frame at the previous exposure.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--script", help="test only the speeds this script needs")
    parser.add_argument("--trim", type=float, default=0.0,
                        help="haze trim in stops, applied as the run would")
    parser.add_argument("--fastest", type=float,
                        help="sweep this far up the fast end, in seconds "
                             "(e.g. 3.125e-05 for 1/32000 on MS+ES); the probe "
                             "cannot test past the configured limit otherwise")
    parser.add_argument("--slowest", type=float, help="sweep this far down, in seconds")
    parser.add_argument("--iso-only", action="store_true")
    parser.add_argument("--no-iso", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(message)s")

    overrides = {}
    if args.fastest:
        overrides["fastest_s"] = args.fastest
    if args.slowest:
        overrides["slowest_s"] = args.slowest
    if overrides:
        exposure_limits.set_limits(**overrides)

    sdk_path = find_fuji_sdk_path()
    if sdk_path is None:
        print("No Fuji SDK found.")
        return 2
    cameras = detect_fuji_cameras(sdk_path)
    if not cameras:
        print("No Fuji camera found.  Is it on, tethered, and is Solar Eclipse "
              "Workbench closed?")
        return 2
    name, camera = next(iter(cameras.items()))
    print(f"Camera: {name}")
    print(f"Limits under test: {exposure_limits.describe()}")

    # Put back whatever the body was on: this is a probe, not a change.
    try:
        original_speed, _ = camera._sdk.get_shutter_speed()
        original_iso = camera._sdk.get_iso()
    except Exception as exc:
        print(f"Could not read the camera's current exposure: {exc}")
        return 2
    print(f"Currently: {SHUTTER_SPEED_NAMES.get(original_speed, original_speed)} "
          f"ISO {original_iso}  (restored at the end)\n")

    speed_results, iso_results = [], []
    try:
        if not args.iso_only:
            if args.script:
                speeds = _speeds_for_script(args.script, args.trim)
                print(f"Speeds {args.script} needs at {args.trim:+.1f} EV "
                      f"({len(speeds)}):")
            else:
                speeds = _speeds_in_range()
                print(f"Every speed inside the configured limits ({len(speeds)}):")
            speed_results = probe_speeds(camera, speeds)

        if not args.no_iso:
            lim = exposure_limits.limits()
            isos = [i for i in ISO_LADDER if lim.iso_min <= i <= lim.iso_max]
            if args.iso_only:
                isos = ISO_LADDER
            print(f"\nISO ({len(isos)}):")
            iso_results = probe_isos(camera, isos)
    finally:
        try:
            camera._sdk.set_shutter_speed(original_speed)
            camera._sdk.set_iso(original_iso)
            print("\nCamera put back where it was.")
        except Exception as exc:
            print(f"\nCould not restore the camera's exposure: {exc}")

    summarise(speed_results, iso_results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
