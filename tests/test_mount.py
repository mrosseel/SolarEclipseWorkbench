import pytest

from solareclipseworkbench.mounts import (
    Capabilities,
    MountDriver,
    MountError,
    MountNotSupported,
    connect,
    discover_drivers,
    format_dec,
    format_ra,
    get_driver,
    list_drivers,
    parse_dec,
    parse_ra,
    register_driver,
)
from solareclipseworkbench.mounts.onstepx import (
    LoopbackTransport,
    OnStepXMount,
    parse_status,
)


# ------------------------------------------------------------------ coordinates


@pytest.mark.parametrize("hours", [0.0, 5.5, 12.3456, 18.75])
def test_right_ascension_survives_a_round_trip(hours):
    assert parse_ra(format_ra(hours)) == pytest.approx(hours, abs=1 / 3600.0)


@pytest.mark.parametrize("degrees", [0.0, 45.0, -23.4567, 66.5])
def test_declination_survives_a_round_trip(degrees):
    assert parse_dec(format_dec(degrees)) == pytest.approx(degrees, abs=1 / 3600.0)


def test_right_ascension_wraps_at_24_hours():
    # 23.99999 h rounds up past 24 h, which is 0 h — not 24.
    assert format_ra(23.99999).startswith("00:")


def test_declination_keeps_the_sign_for_small_negatives():
    assert format_dec(-0.5).startswith("-00*30")


def test_parses_the_low_precision_reply_shape():
    # Controllers not in high-precision mode answer HH:MM.T instead.
    assert parse_ra("05:30.5") == pytest.approx(5.5083, abs=1e-3)


def test_rejects_unparseable_coordinates():
    with pytest.raises(MountError):
        parse_ra("not a coordinate")


# ---------------------------------------------------------------- the registry


def test_builtin_drivers_are_discovered():
    drivers = discover_drivers()

    assert "onstepx" in drivers
    assert "simulator" in drivers


def test_get_driver_reports_what_is_available_when_the_name_is_wrong():
    with pytest.raises(MountError, match="onstepx"):
        get_driver("no-such-mount")


def test_every_driver_declares_a_name_and_capabilities():
    for driver in list_drivers():
        assert driver.name
        assert driver.display_name
        assert isinstance(driver.capabilities, Capabilities)


def test_a_third_party_driver_can_register_itself():
    class Fake(MountDriver):
        name = "test-only-fake"
        display_name = "Fake"
        capabilities = Capabilities(park=False, tracking_rates=("sidereal",))

        def connect(self): pass
        def close(self): pass
        def describe(self): return "fake"
        def status(self): return None
        def get_radec(self): return (0.0, 0.0)
        def goto(self, ra_hours, dec_degrees, wait=False, timeout=180.0): pass
        def abort(self): pass
        def tracking_on(self): return True
        def tracking_off(self): return True

    try:
        register_driver(Fake)
        assert get_driver("test-only-fake") is Fake
    finally:
        discover_drivers()  # leave the registry populated for other tests


def test_registering_a_non_driver_is_refused():
    class NotADriver:
        name = "nope"

    with pytest.raises(TypeError):
        register_driver(NotADriver)


def test_unsupported_features_raise_rather_than_silently_doing_nothing():
    with connect(driver="simulator") as mount:
        # The simulator declares no alt/az readout, so asking must fail loudly.
        assert mount.capabilities.altaz_readout is False
        with pytest.raises(MountNotSupported):
            mount.get_altaz()


# ------------------------------------------------------------- simulator driver


def test_simulator_reports_identity_and_connects():
    with connect(driver="simulator") as mount:
        assert "simulated" in mount.describe()
        assert mount.status().connected is True


def test_simulator_goto_updates_the_reported_position():
    with connect(driver="simulator") as mount:
        mount.goto(6.0, 23.5)
        ra, dec = mount.get_radec()

    assert ra == pytest.approx(6.0)
    assert dec == pytest.approx(23.5)


def test_simulator_refuses_goto_while_parked():
    with connect(driver="simulator") as mount:
        mount.park()

        with pytest.raises(MountError, match="parked"):
            mount.goto(1.0, 2.0)


def test_simulator_solar_tracking():
    with connect(driver="simulator") as mount:
        mount.set_tracking_rate("solar")
        mount.tracking_on()
        status = mount.status()

    assert status.tracking is True
    assert status.tracking_rate == "solar"


def test_simulator_is_never_offered_by_discovery():
    # A real mount must never be silently replaced by a simulated one.
    from solareclipseworkbench.mounts.simulator import SimulatorMount

    assert SimulatorMount.discover() == []


# ---------------------------------------------------------------- OnStepX driver


def _loopback() -> OnStepXMount:
    mount = OnStepXMount(transport=LoopbackTransport())
    mount.connect()
    return mount


def test_status_treats_absent_flags_as_active_states():
    # 'n' means NOT tracking and 'N' means NO goto, so their absence is the
    # positive case — the easiest thing to get backwards in this protocol.
    status = parse_status("pNO#")

    assert status.tracking is True
    assert status.slewing is False
    assert status.parked is False
    assert status.tracking_rate == "solar"


def test_status_reports_parked_and_home():
    status = parse_status("nNPH")

    assert status.parked is True
    assert status.at_home is True
    assert status.tracking is False
    assert "parked" in status.summary()


def test_status_reads_the_tracking_rate():
    assert parse_status("nN(").tracking_rate == "lunar"
    assert parse_status("nNk").tracking_rate == "king"
    assert parse_status("nN").tracking_rate == "sidereal"


def test_onstepx_reports_identity():
    mount = _loopback()

    assert "OnStepX" in mount.product_name()
    assert mount.firmware_version()


def test_onstepx_goto_updates_the_reported_position():
    mount = _loopback()

    mount.goto(6.0, 23.5)
    ra, dec = mount.get_radec()

    assert ra == pytest.approx(6.0, abs=1e-3)
    assert dec == pytest.approx(23.5, abs=1e-3)


def test_onstepx_goto_raises_with_the_controllers_reason_when_parked():
    mount = _loopback()
    mount.park()

    with pytest.raises(MountError, match="parked"):
        mount.goto(1.0, 2.0)


def test_onstepx_commands_are_framed_even_when_given_bare():
    mount = _loopback()

    mount.send("GVP")

    assert mount.traffic[-1][1] == ":GVP#"


def test_onstepx_rejects_an_unknown_tracking_rate():
    mount = _loopback()

    with pytest.raises(MountError):
        mount.set_tracking_rate("warp")


def test_onstepx_requires_an_address():
    with pytest.raises(MountError, match="port"):
        OnStepXMount().connect()
