"""Check a script survives totality, at every duration it might turn out to be.

The duration depends on where you stand, and the script is written before that is
settled.  What makes that dangerous is not the script format - offsets from C2 and
C3 resolve fine at any duration - but what happens when two commands overlap:
``_serialised_on_camera`` waits 1.5s for the camera and then **drops** the job.
It does not queue it.  A bracket holds the camera for twelve to thirty-four
seconds, so anything scheduled inside one is silently lost, and at a shorter
totality than planned the C3 block starts inside the C2 block's last bracket.

This walks the script at a given duration, gives every command the wall-clock
cost measured on the X-T4 on 3 August, and reports what would be dropped.

    ./run.sh scripts/validate_totality.py myscript.txt --min 95 --max 115
    ./run.sh scripts/validate_totality.py myscript.txt --duration 108 --verbose
"""

import argparse
import csv
import io
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytz

# fujixsdk lives in the repo root, which is not on sys.path when this file is
# run as scripts/validate_totality.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solareclipseworkbench.fuji_camera import _parse_shutter_speed
from solareclipseworkbench.reference_moments import ReferenceMomentInfo
from solareclipseworkbench.scripts import convert_script

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m")

# Wall-clock cost of each command on the X-T4 over the relay, measured 3 August.
# A command holds the camera for this long, and anything scheduled inside that
# window is dropped rather than delayed.
#
#   single          0.52s mean, 3.29s worst (the drain when the queue fills)
#   burst           2.05s of contact, plus making room before and draining after
#   bracket +/- 2   12s fast, 18s slow      +/- 3   19s fast, 34s slow
#
# The slow figures are used for brackets because the cost is dominated by shutter
# time and the script cannot be read for which end of the ladder it lands on
# without knowing the base exposure - so this errs towards the expensive case.
COSTS = {
    "take_picture": 3.3,
    "take_burst": 12.0,       # 11.8s measured against a loaded queue, the worst case
    "sync_cameras": 1.0,
    "voice_prompt": 0.0,      # runs off-camera, takes no lock
    "play": 0.0,
}
DEFAULT_COST = 1.0

# On top of the exposure itself: the speed change over USB, the tap, and the
# body settling before the next command can have it.  Not yet measured against a
# long exposure - the body dropped off the bus before that run - so this is the
# per-frame overhead seen at 1/500 (0.60s) rounded up, and should be checked.
SINGLE_OVERHEAD_S = 1.0

# A bracket's cost is dominated by how long its shutter is open, so it cannot be
# read off the width alone: 19 rungs cost 19s at 1/1000 and 34s at 1/2.  Each rung
# waits max(TAP_GAP_S, exposure + 0.3) for the frame, plus about 0.35s to put the
# next speed over USB, and the bracket drains at the end.
TAP_GAP_S = 0.35
PER_RUNG_USB_S = 0.35
BRACKET_DRAIN_S = 3.0


def bracket_cost(base_speed: str, width: str) -> float:
    """Seconds a bracket holds the camera, from its base exposure and width.

    Against the four measured on 3 August, the model reads between 0.6s under
    and 3.1s over, and errs high on three of them - which is the direction a
    schedule check should be wrong in:

        base    width    model   measured
        1/1000  +/- 3    19.3s   19.9s
        1/2     +/- 3    34.5s   33.0s
        1/200   +/- 2    15.1s   12.0s
        1/2     +/- 2    20.7s   18.0s
    """
    base = _parse_shutter_speed(str(base_speed).strip())
    if base is None:
        return 34.0                      # unreadable: charge the worst measured
    base_s = base / 1_000_000.0

    try:
        clean = width.replace("+/-", "").strip()
        if " " in clean:
            whole, frac = clean.split()
            num, den = frac.split("/")
            ev = float(whole) + int(num) / int(den)
        else:
            ev = float(clean)
    except (ValueError, IndexError):
        ev = 1.0
    positions = int(round(ev * 3))

    seconds = 0.0
    frames = 0
    for step in range(-positions, positions + 1):
        exposure = base_s * (2.0 ** (step / 3.0))
        seconds += max(TAP_GAP_S, exposure + 0.3) + PER_RUNG_USB_S
        # On CH a contact fires twice at a short exposure and once at a long one.
        frames += 2 if exposure < 0.1 else 1

    drains = 1 + frames // 24            # the mid-bracket clear, plus the final one
    return seconds + drains * BRACKET_DRAIN_S

# What the camera lock waits before giving up on a job (camera._MAX_LOCK_WAIT_S).
LOCK_WAIT_S = 1.5


def cost_of(command: str, args: list) -> float:
    if command == "take_picture":
        # `capture` returns 80ms after the tap, but the body is not free until
        # the shutter closes: a 4" frame costs the scheduler nothing and blocks
        # the next command for four seconds.  Whichever is longer is the cost.
        speed = _parse_shutter_speed(str(args[1]).strip()) if len(args) > 1 else None
        exposure = (speed / 1_000_000.0) if speed else 0.0
        return max(COSTS["take_picture"], exposure + SINGLE_OVERHEAD_S)
    if command == "take_bracket":
        # camera, shutter, aperture, iso, steps, comment
        speed = args[1] if len(args) > 1 else ""
        width = args[4] if len(args) > 4 else "+/- 1"
        return bracket_cost(speed, width)
    return COSTS.get(command, DEFAULT_COST)


def moments_for(duration_s: float) -> dict:
    """Reference moments for a synthetic eclipse of the given totality."""
    tz = pytz.UTC
    base = datetime(2027, 8, 2, 12, 0, 0, tzinfo=tz)

    class _Angle:                      # ReferenceMomentInfo wants .degrees
        degrees = 0.0

    def at(seconds: float) -> ReferenceMomentInfo:
        return ReferenceMomentInfo(base + timedelta(seconds=seconds), _Angle(), 45.0, tz)

    half = duration_s / 2
    return {
        "C1": at(-3600),
        "C2": at(-half),
        "MAX": at(0),
        "C3": at(half),
        "C4": at(3600),
        "duration": timedelta(seconds=duration_s),
    }


def scheduled_commands(script: Path, duration_s: float) -> list:
    """(offset from C2 in seconds, command, args, description) for one duration."""
    moments = moments_for(duration_s)
    converted = convert_script(str(script), moments)
    converted.seek(0)

    c2 = moments["C2"].time_utc
    out = []
    for line in converted:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = next(csv.reader([line], skipinitialspace=True))
        if len(parts) < 4:
            continue
        command, ref, sign, delta = parts[0], parts[1], parts[2], parts[3]
        if ref not in moments:
            continue
        try:
            h, m, s = (float(x) for x in delta.split(":"))
        except ValueError:
            continue
        seconds = h * 3600 + m * 60 + s
        when = moments[ref].time_utc + timedelta(seconds=-seconds if sign == "-" else seconds)
        out.append(((when - c2).total_seconds(), command, parts[4:], line))
    return sorted(out, key=lambda row: row[0])


def simulate(script: Path, duration_s: float, verbose: bool = False) -> tuple[int, int, list]:
    """Walk the schedule, dropping what the camera lock would drop."""
    commands = scheduled_commands(script, duration_s)
    busy_until = float("-inf")
    dropped, ran, notes = 0, 0, []

    for at, command, args, raw in commands:
        # Only what happens inside totality is at stake here.
        if at < -5 or at > duration_s + 5:
            continue
        cost = cost_of(command, args)
        if cost == 0:
            # Off-camera: a voice prompt is not wrapped in _serialised_on_camera,
            # so it never waits for the lock and cannot be dropped by a bracket.
            ran += 1
            continue

        wait = busy_until - at
        if wait > LOCK_WAIT_S:
            dropped += 1
            notes.append((at, command, f"dropped — camera busy for another "
                                       f"{wait:.1f}s (waits only {LOCK_WAIT_S}s)"))
        else:
            start = max(at, busy_until)
            busy_until = start + cost
            ran += 1
            if verbose:
                notes.append((at, command, f"runs {start - at:+.1f}s late, "
                                           f"holds until C2{busy_until:+.1f}s"))
    if busy_until > duration_s:
        notes.append((duration_s, "—", f"still shooting {busy_until - duration_s:.1f}s "
                                       f"after C3"))
    return ran, dropped, notes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("script", type=Path)
    parser.add_argument("--duration", type=float,
                        help="single totality duration to check, in seconds")
    parser.add_argument("--min", type=float, default=95.0,
                        help="shortest totality to check (default: 95)")
    parser.add_argument("--max", type=float, default=115.0,
                        help="longest totality to check (default: 115)")
    parser.add_argument("--step", type=float, default=5.0)
    parser.add_argument("--verbose", action="store_true",
                        help="show every command, not only the losses")
    args = parser.parse_args()

    if not args.script.exists():
        print(f"{RED}No such script: {args.script}{RESET}")
        return

    durations = ([args.duration] if args.duration
                 else [args.min + i * args.step
                       for i in range(int((args.max - args.min) / args.step) + 1)])

    print(f"{BOLD}{args.script.name}{RESET}")
    print(f"{DIM}a command that cannot take the camera within {LOCK_WAIT_S}s is "
          f"dropped, not delayed.{RESET}\n")

    worst = None
    for duration in durations:
        ran, dropped, notes = simulate(args.script, duration, args.verbose)
        mark = f"{GREEN}ok{RESET}" if not dropped else f"{RED}{dropped} dropped{RESET}"
        print(f"{BOLD}{duration:>6.0f}s totality{RESET}  {ran:>3} run  {mark}")
        for at, command, note in notes:
            colour = RED if "dropped" in note or "after C3" in note else DIM
            print(f"    {colour}C2{at:+7.1f}s  {command:<14} {note}{RESET}")
        if dropped and worst is None:
            worst = duration

    print()
    if worst is not None:
        print(f"{RED}Loses commands at {worst:.0f}s.{RESET}  Anchor the late block "
              f"to C3 rather than C2, or take a bracket out of the middle: at a "
              f"shorter totality the two ends grow into each other.")
    else:
        print(f"{GREEN}Fits at every duration checked "
              f"({durations[0]:.0f}-{durations[-1]:.0f}s).{RESET}")


if __name__ == "__main__":
    main()
