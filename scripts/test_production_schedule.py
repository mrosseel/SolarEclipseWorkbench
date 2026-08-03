"""Dry-run of the production eclipse script.

No camera and no relay board: a stub SDK camera and the simulated relay
backend stand in, so this exercises the real path end to end — the script's
standard commands, take_picture/take_burst/take_bracket, the relay firing and
the drain that follows each of them.
"""

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from solareclipseworkbench import fuji_camera as fc
from solareclipseworkbench import relay_trigger as rt
from solareclipseworkbench.camera import CameraSettings, take_bracket, take_burst, take_picture
from solareclipseworkbench.fuji_camera import BUFFER_SLOTS, DRAIN_AT, FujiCamera
from solareclipseworkbench.hardware_registry import register_hardware
from solareclipseworkbench.relay_trigger import relay_arm, relay_release
from solareclipseworkbench.utils import schedule_commands, start_scheduler

REAL = Path(__file__).resolve().parent / "real"

# The simulated contacts, in hours from now.  C2 and C3 are what set how long
# totality lasts here, and that is what decides which production script fits.
CONTACT_OFFSETS_H = [("C1", 1), ("C2", 2.3), ("MAX", 2.31), ("C3", 2.33), ("C4", 3.6)]
SIM_TOTALITY_S = (dict(CONTACT_OFFSETS_H)["C3"] - dict(CONTACT_OFFSETS_H)["C2"]) * 3600.0


def script_for(duration_s: float) -> Path:
    """The production script to load for a totality this long: the longest that fits.

    There is one per duration, and one laid out for longer than you get is still
    exposing after C3 - so the choice is always downwards.
    """
    fits = [path for path in sorted(REAL.glob("20260812_production_*s.txt"))
            if float(path.stem.rsplit("_", 1)[1].rstrip("s")) <= duration_s]
    if not fits:
        raise SystemExit(f"no production script fits {duration_s:.0f}s of totality")
    return fits[-1]


# Pass a path to dry-run a specific one.
SCRIPT = Path(sys.argv[1]) if len(sys.argv) > 1 else script_for(SIM_TOTALITY_S)

fc.SETTLE_BEFORE_DRAIN_S = 0.0
fc.SETTLE_BETWEEN_DRAINS_S = 0.0
fc.TAP_GAP_S = 0.0


class StubSDK:
    """Just enough of fujixsdk.Camera for the relay path."""

    def __init__(self):
        self.pending = 0
        self.speeds = []
        self.isos = []
        self.drains = 0

    def get_buffer_capacity(self):
        return self.pending, 32

    def drain_buffer(self):
        drained, self.pending = self.pending, 0
        self.drains += 1
        return drained

    def set_shutter_speed(self, value):
        self.speeds.append(value)

    def set_iso(self, value):
        self.isos.append(value)

    def set_aperture(self, value):
        raise AssertionError("the telescope has no aperture to set")

    def shoot_no_af(self):
        raise AssertionError("the relay should be firing, not the SDK")


def main() -> None:
    trigger = rt.RelayTrigger(
        rt.SimulatedBackend(latency_s=0.0),
        rt.Wiring(s1_channel=1, s2_channel=2, settle_s=0.0, pulse_s=0.01),
    )
    register_hardware("relay", trigger)

    sdk = StubSDK()
    camera = FujiCamera(sdk, "Fuji Fujifilm X-T4", "/nonexistent")
    cameras = {camera.name: camera}

    register_hardware("sdk_camera", camera)

    now = datetime.now(timezone.utc)
    moments = {name: SimpleNamespace(time_utc=now + timedelta(hours=offset))
               for name, offset in CONTACT_OFFSETS_H}

    # The bead windows the production script schedules its contact bursts against.
    # Seconds relative to the contacts, from the real solve at the site in the
    # script header.  The contacts themselves are NOT invented here: at run time
    # the limb-corrected times take over the C2 and C3 names, so a script that
    # asks for C2 already gets the corrected one.  Inventing C2_LIMB/C3_LIMB - as
    # this did until 3 August - hid the fact that nothing produces those names any
    # more, and that every command scheduled against them was being dropped.
    for name, base, delta in (("BEADS_C2", "C2", -1.118),
                              ("BEADS_C2_START", "C2", -2.743), ("BEADS_C2_END", "C2", 0.507),
                              ("BEADS_C3", "C3", -1.471),
                              ("BEADS_C3_START", "C3", -3.496), ("BEADS_C3_END", "C3", 0.554)):
        moments[name] = SimpleNamespace(
            time_utc=moments[base].time_utc + timedelta(seconds=delta))

    scheduler = start_scheduler()
    schedule_commands(str(SCRIPT), scheduler, moments, cameras, None, None, None)
    jobs = scheduler.get_jobs()
    lines = [line for line in SCRIPT.read_text().splitlines()
             if line.strip() and not line.strip().startswith("#")]
    print(f"\nscript lines: {len(lines)}   scheduled jobs: {len(jobs)}")
    assert len(jobs) == len(lines), "every line must schedule"
    scheduler.shutdown(wait=False)

    settings = CameraSettings("Fuji Fujifilm X-T4", "1/1000", "-", 320)

    # A single no longer drains after every frame: the queue holds 32 and a drain
    # is a settle plus the deletes, so paying it to reclaim one slot spent the
    # whole gap between two scripted frames.  Measured 3 August, that alone took a
    # single from 2.03s to 0.52s.  It drains when the queue is actually filling.
    take_picture(camera, settings)
    assert sdk.drains == 0, "a single frame must not stop to drain an empty queue"
    assert trigger.closed_channels == set(), "contacts must be open again"

    # Draining repeats until a round comes back empty - a burst is still
    # arriving 2.5s after the contact opens - so the count here is rounds, not
    # drains.  What matters is that it happened and the queue is clear.
    sdk.pending = int(BUFFER_SLOTS * DRAIN_AT) + 1
    take_picture(camera, settings)
    assert sdk.drains >= 1, "a single frame must drain a queue that has filled"
    assert sdk.pending == 0

    drains_after_single = sdk.drains
    sdk.pending = 27
    take_burst(camera, settings, 27)
    assert sdk.drains > drains_after_single and sdk.pending == 0, \
        "a burst must drain after itself"
    drains_after_burst = sdk.drains

    sdk.pending = 8
    ladder = "1/2000;1/500;1/125;1/30;1/8;1/2;2;4"
    before = len(sdk.speeds)
    take_bracket(camera, settings, ladder)
    assert sdk.drains > drains_after_burst and sdk.pending == 0, \
        "a bracket must drain after itself"
    # The ladder itself, in order.  Not counted relative to `before`, because
    # configure stages a base speed only when it differs from what the body
    # already holds - it is skipped otherwise, and a count would move with it.
    wanted = [fc._parse_shutter_speed(part) for part in ladder.split(";")]
    assert sdk.speeds[-len(wanted):] == wanted, (
        f"one speed per rung in order, got {sdk.speeds[-len(wanted):]}")
    assert sdk.speeds[-1] == 4_000_000, "the last rung is the 4 s earthshine frame"
    assert 320 in sdk.isos, "ISO must reach the camera"

    relay_arm(trigger)
    assert trigger.closed_channels == {1}, "arm must hold S1"
    take_picture(camera, settings)
    assert trigger.closed_channels == {1}, "a frame must not drop the arm"
    relay_release(trigger)
    assert trigger.closed_channels == set(), "release must open everything"

    trigger.close()
    print("ALL OK — standard commands drive the relay, and every one drains")


if __name__ == "__main__":
    main()
