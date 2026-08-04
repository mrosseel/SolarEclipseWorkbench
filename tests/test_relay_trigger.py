from unittest.mock import MagicMock

import pytest

from solareclipseworkbench import hardware_problems
from solareclipseworkbench.hardware_registry import register_hardware
from solareclipseworkbench.relay_trigger import (
    Backend,
    RelayError,
    RelayTrigger,
    SimulatedBackend,
    Wiring,
    discover_backends,
    discover_relays,
    get_backend,
    list_backends,
    make_backend,
    register_backend,
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
    # Building the trigger already opens the channels once, and that must not
    # raise either - the constructor meets whatever the last run left behind.
    trigger = RelayTrigger(_FailingBackend(), Wiring(s2_channel=1))
    already = trigger.backend.open_attempts

    # Must not raise: this runs from atexit and signal handlers, where an
    # exception would stop the remaining contacts from being released.
    trigger.release_all()

    assert trigger.backend.open_attempts == already + 1


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


# ------------------------------------------------------------ backend registry


def test_builtin_backends_are_registered():
    backends = discover_backends()

    assert {"lcus", "numato", "dsd", "hid", "simulated"} <= set(backends)


def test_dsd_backend_speaks_at_commands_without_a_terminator(monkeypatch):
    # The SH-UR firmware misparses a trailing CR/LF, so none may be sent.
    import serial as serial_mod
    from solareclipseworkbench.relay_trigger import DsdSerialBackend

    written = []

    class _FakeSerial:
        def __init__(self, *args, **kwargs): pass
        def write(self, data): written.append(data)
        def flush(self): pass
        def close(self): pass

    monkeypatch.setattr(serial_mod, "Serial", _FakeSerial)
    backend = DsdSerialBackend(port="/dev/fake")
    backend.set_channel(1, True)
    backend.set_channel(4, False)

    assert written == [b"AT+CH1=1", b"AT+CH4=0"]


def test_get_backend_reports_what_is_available_when_the_name_is_wrong():
    with pytest.raises(RelayError, match="lcus"):
        get_backend("no-such-board")


def test_every_backend_declares_a_name():
    for backend in list_backends():
        assert backend.name
        assert backend.name != "backend"


def test_a_third_party_backend_can_register_itself():
    @register_backend
    class Fake(Backend):
        name = "test-only-fake-relay"

        def set_channel(self, channel, closed): pass
        def describe(self): return "fake"

    assert get_backend("test-only-fake-relay") is Fake


def test_registering_a_non_backend_is_refused():
    class NotABackend:
        name = "nope"

    with pytest.raises(TypeError):
        register_backend(NotABackend)


def test_a_backend_without_its_own_name_is_refused():
    class Unnamed(Backend):
        def set_channel(self, channel, closed): pass
        def describe(self): return "unnamed"

    with pytest.raises(ValueError):
        register_backend(Unnamed)


def test_simulated_backend_is_never_offered_by_discovery():
    # A real board must never be silently replaced by a simulated one, or a
    # script would appear to run while firing nothing.
    assert SimulatedBackend.discover() == []


def test_make_backend_honours_an_explicit_name():
    assert isinstance(make_backend("simulated"), SimulatedBackend)


def test_discovered_candidates_are_tagged_as_relays():
    for candidate in discover_relays():
        assert candidate.kind == "relay"
        assert candidate.driver


def test_a_held_burst_is_capped_to_what_the_open_session_can_buffer():
    # 15 fps fills the 32-slot transfer queue in a little over two seconds, and a
    # full queue stops the body dead - in the middle of totality, if the script
    # asked for a long hold at a contact.
    trigger = _trigger()
    camera = MagicMock(max_relay_hold_s=1.9, name="X-T4")
    register_hardware("sdk_camera", camera)
    try:
        relay_burst(trigger, "2.5")
    finally:
        register_hardware("sdk_camera", None)
        hardware_problems.clear()

    held = trigger.events[-1].at - trigger.events[0].at
    assert held == pytest.approx(1.9, abs=0.2)
    assert camera.drain.called


def test_a_burst_drains_the_queue_it_filled():
    trigger = _trigger()
    camera = MagicMock(max_relay_hold_s=1.9)
    register_hardware("sdk_camera", camera)
    try:
        relay_burst(trigger, "0.05")
    finally:
        register_hardware("sdk_camera", None)

    assert camera.drain.called
    assert trigger.closed_channels == set()


def test_a_burst_without_an_open_session_is_left_alone():
    # No SDK session means no transfer queue, so there is nothing to cap or drain
    # and the script gets exactly the hold it asked for.
    trigger = _trigger()

    relay_burst(trigger, "0.05")

    assert trigger.closed_channels == set()


def test_a_burst_opens_every_contact_before_it_drains():
    # Pre-arming leaves S1 closed on the way out of the burst, and the bench
    # proved twice that draining with S1 still held drops the USB session for
    # good.  The order matters more than the arm does.
    order = []
    trigger = _trigger(single=False)
    trigger.half_press()                       # pre-armed, as a contact burst is

    camera = MagicMock(max_relay_hold_s=1.9)
    camera.drain.side_effect = lambda: order.append(("drain", trigger.closed_channels.copy()))
    register_hardware("sdk_camera", camera)
    try:
        relay_burst(trigger, "0.05")
    finally:
        register_hardware("sdk_camera", None)

    assert order, "the burst never drained"
    _, closed_when_draining = order[0]
    assert closed_when_draining == set()


def test_a_latched_contact_is_opened_when_the_trigger_is_built():
    # 3 August: a segfault ran none of the shutdown guards, the board held the
    # contact through the crash and the replug, and the next run met a camera
    # that answered 0x1006 to everything - a shutter held down.
    backend = make_backend("simulated")
    wiring = Wiring()
    for channel in wiring.channels:
        backend.set_channel(channel, True)

    trigger = RelayTrigger(backend, wiring)

    assert not any(backend.state[c] for c in wiring.channels), \
        "the trigger adopted a latched board"
    assert trigger.closed_channels == set()
    assert trigger.events == [], "inherited state was logged as if commanded"


def test_the_signal_handler_does_no_work_of_its_own():
    """A Python signal handler runs on the main thread between bytecodes, so it
    can interrupt any critical section.  This one iterated a WeakSet and did USB
    I/O.  On 4 August a Ctrl-C landed inside a lock in hardware_problems and the
    handler raised while another exception was pending:

        SystemError: WeakSet.__iter__ returned a result with an exception set

    The lock was never released, the interface blocked forever in count(), and
    SIGTERM could not get through either - the process had to be killed.

    Releasing at exit instead keeps the guarantee and takes the work off the
    handler's stack; that contacts are still opened on a signal is covered by
    scripts checking a real subprocess, since it cannot be observed from here.
    """
    import inspect

    from solareclipseworkbench import relay_trigger

    source = inspect.getsource(relay_trigger._install_shutdown_guards)
    handler = source[source.index("def handler"):]

    assert "_release_all_contacts" not in handler, \
        "the handler does USB work; that is what wedged the interface"
    assert "atexit.register(_release_all_contacts)" in source, \
        "nothing would open the contacts at all"
