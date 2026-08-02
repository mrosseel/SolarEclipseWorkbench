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
from solareclipseworkbench.fuji_camera import FujiCamera
from solareclipseworkbench.hardware_registry import register_hardware
from solareclipseworkbench.relay_trigger import relay_arm, relay_release
from solareclipseworkbench.utils import schedule_commands, start_scheduler

SCRIPT = Path(__file__).resolve().parent / "20260812_production.txt"

fc.SETTLE_BEFORE_DRAIN_S = 0.0
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
    offsets = [("C1", 1), ("C2", 2.3), ("MAX", 2.31), ("C3", 2.33), ("C4", 3.6)]
    moments = {name: SimpleNamespace(time_utc=now + timedelta(hours=offset))
               for name, offset in offsets}

    # The limb-corrected moments the production script schedules its contacts and
    # its countdown against.  Seconds relative to the mean contacts, taken from
    # the real solve at the site in the script header - without these the whole
    # totality sequence would silently not be scheduled at all.
    for name, base, delta in (("C2_LIMB", "C2", 0.507), ("BEADS_C2", "C2", -1.118),
                              ("BEADS_C2_START", "C2", -2.743), ("BEADS_C2_END", "C2", 0.507),
                              ("C3_LIMB", "C3", -3.496), ("BEADS_C3", "C3", -1.471),
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

    take_picture(camera, settings)
    assert sdk.drains == 1, "a single frame must drain after itself"
    assert trigger.closed_channels == set(), "contacts must be open again"

    sdk.pending = 27
    take_burst(camera, settings, 27)
    assert sdk.drains == 2 and sdk.pending == 0, "a burst must drain after itself"

    sdk.pending = 8
    ladder = "1/2000;1/500;1/125;1/30;1/8;1/2;2;4"
    before = len(sdk.speeds)
    take_bracket(camera, settings, ladder)
    assert sdk.drains == 3 and sdk.pending == 0, "a bracket must drain after itself"
    # One set from configure staging the base speed, then one per rung.
    rungs = len(sdk.speeds) - before - 1
    assert rungs == 8, f"one speed per rung, got {rungs}"
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
