"""Dry-run of the production eclipse script: parse, schedule, and fire composites.

No camera and no relay board needed — a stub camera and the simulated relay
backend stand in, so this verifies the wiring end to end: DSL parsing, argument
resolution, the composites' choreography, and that every line schedules.
"""

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from solareclipseworkbench import relay_trigger as rt
from solareclipseworkbench import eclipse_fuji
from solareclipseworkbench.hardware_registry import register_hardware
from solareclipseworkbench.utils import schedule_commands, start_scheduler

# Make composite sleeps fast for the test.
eclipse_fuji.SETTLE_BEFORE_DRAIN_S = 0.01
eclipse_fuji.TAP_GAP_S = 0.01


class StubSDK:
    def __init__(self):
        self.pending = 0
        self.drains = 0

    def get_buffer_capacity(self):
        return self.pending, 32

    def drain_buffer(self):
        drained, self.pending = self.pending, 0
        self.drains += 1
        return drained


class StubCamera:
    def __init__(self):
        self._sdk_cam = StubSDK()
        self.speeds = []

    def configure(self, **kwargs):
        if "shutter_speed" in kwargs:
            self.speeds.append(kwargs["shutter_speed"])


def main() -> None:
    trigger = rt.RelayTrigger(
        rt.SimulatedBackend(latency_s=0.0),
        rt.Wiring(s1_channel=1, s2_channel=2, settle_s=0.01, pulse_s=0.01),
    )
    register_hardware("relay", trigger)
    camera = StubCamera()
    cameras = {"Fuji Fujifilm X-T4": camera}

    now = datetime.now(timezone.utc)
    offsets = [("C1", 1), ("C2", 2.3), ("MAX", 2.31), ("C3", 2.33), ("C4", 3.6)]
    moments = {name: SimpleNamespace(time_utc=now + timedelta(hours=offset))
               for name, offset in offsets}

    scheduler = start_scheduler()
    script = Path(__file__).resolve().parent.parent / (
        "scripts/xt4_relay_eclipse.txt")
    schedule_commands(str(script), scheduler, moments, cameras, None, None, None)

    jobs = scheduler.get_jobs()
    lines = [l for l in script.read_text().splitlines()
             if l.strip() and not l.strip().startswith("#")]
    print(f"\nscript lines: {len(lines)}   scheduled jobs: {len(jobs)}")
    assert len(jobs) == len(lines), "every line must schedule"

    # Fire each composite once, directly, against the stubs.
    camera._sdk_cam.pending = 2
    eclipse_fuji.fuji_partial(camera, trigger, "1/1000")
    eclipse_fuji.fuji_speed(camera, "1/2000")
    camera._sdk_cam.pending = 27
    eclipse_fuji.fuji_beads_burst(camera, trigger, "1.8")
    camera._sdk_cam.pending = 10
    eclipse_fuji.fuji_ladder(camera, trigger, "1/2000;1/500;1/125;1/30;1/8;1/2;2", 1)
    eclipse_fuji.fuji_drain(camera)

    from solareclipseworkbench.relay_trigger import relay_arm, relay_release
    relay_arm(trigger)
    assert trigger.closed_channels == {1}, "arm must hold S1"
    trigger.shoot(pulse=0.01)
    assert trigger.closed_channels == {1}, "a shot must not drop the arm"
    relay_release(trigger)
    assert trigger.closed_channels == set(), "release must open everything"

    assert camera.speeds[0] == "1/1000" and "1/2000" in camera.speeds
    assert camera._sdk_cam.drains >= 4, "each composite must have drained"

    scheduler.shutdown(wait=False)
    trigger.close()
    print("ALL OK — script parses, schedules, and every composite behaves")


if __name__ == "__main__":
    main()
