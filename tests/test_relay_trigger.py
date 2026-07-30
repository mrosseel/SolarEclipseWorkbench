import pytest

from solareclipseworkbench.relay_trigger import (
    RelayError,
    RelayTrigger,
    SimulatedBackend,
    Wiring,
    relay_bulb,
    relay_burst,
)


class _FailingBackend(SimulatedBackend):
    """Closes fine, but refuses to open — the worst case for a shutter."""

    def __init__(self):
        super().__init__(latency_s=0.0)
        self.open_attempts = 0

    def set_channel(self, channel, closed):
        if not closed:
            self.open_attempts += 1
            raise RelayError("contact stuck")
        super().set_channel(channel, closed)


def _trigger(single=True):
    wiring = Wiring(s2_channel=1) if single else Wiring(s2_channel=2, s1_channel=1)
    wiring.settle_s = 0.0
    wiring.pulse_s = 0.0
    return RelayTrigger(SimulatedBackend(latency_s=0.0), wiring)


def test_single_channel_wiring_reports_one_channel():
    trigger = _trigger(single=True)

    assert trigger.wiring.is_single_channel
    assert trigger.wiring.channels == [1]


def test_dual_channel_wiring_reports_both_channels():
    trigger = _trigger(single=False)

    assert not trigger.wiring.is_single_channel
    assert trigger.wiring.channels == [1, 2]


def test_shoot_leaves_every_contact_open():
    trigger = _trigger()

    trigger.shoot()

    assert trigger.closed_channels == set()


def test_single_channel_burst_holds_one_contact():
    trigger = _trigger(single=True)

    pulses = trigger.burst(0.05)

    # The camera free-runs while the contact is held, so one closure covers the
    # whole burst and the pulse count says nothing about the frame count.
    assert pulses == 1
    assert trigger.closed_channels == set()


def test_dual_channel_burst_pulses_repeatedly():
    trigger = _trigger(single=False)

    pulses = trigger.burst(0.25, interval_s=0.05)

    assert pulses >= 3
    assert trigger.closed_channels == set()


def test_dual_channel_burst_ignores_interval_on_single_channel():
    trigger = _trigger(single=True)

    assert trigger.burst(0.05, interval_s=0.01) == 1


def test_contacts_open_when_the_block_raises():
    trigger = _trigger()

    with pytest.raises(ZeroDivisionError):
        with trigger.pressed():
            raise ZeroDivisionError("something failed mid-exposure")

    assert trigger.closed_channels == set()


def test_release_all_survives_a_backend_that_refuses():
    trigger = RelayTrigger(_FailingBackend(), Wiring(s2_channel=1))

    # Must not raise: this runs from atexit and signal handlers, where an
    # exception would stop the remaining contacts from being released.
    trigger.release_all()

    assert trigger.backend.open_attempts == 1


def test_bulb_releases_after_the_exposure():
    trigger = _trigger()

    trigger.bulb(0.02)

    assert trigger.closed_channels == set()


def test_events_record_each_transition():
    trigger = _trigger()

    trigger.shoot()

    assert len(trigger.events) == 2
    assert trigger.events[0].closed is True
    assert trigger.events[-1].closed is False


def test_scheduler_commands_accept_string_arguments():
    trigger = _trigger()

    # Commands come out of the script as strings, never as floats.
    relay_burst(trigger, "0.05")
    relay_burst(trigger, "0.05", "")
    relay_bulb(trigger, "0.02")

    assert trigger.closed_channels == set()


def test_timing_sample_reports_a_spread():
    trigger = _trigger()

    stats = trigger.timing_sample(samples=5)

    assert stats["samples"] == 5
    assert stats["max_ms"] >= stats["min_ms"]
    assert stats["spread_ms"] == pytest.approx(stats["max_ms"] - stats["min_ms"])
