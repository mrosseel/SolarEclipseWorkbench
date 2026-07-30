import pytest

from solareclipseworkbench.mount import (
    MountError,
    MountStatus,
    OnStepXMount,
    SimulatedTransport,
    _parse_dec,
    _parse_ra,
    connect,
    format_dec,
    format_ra,
)


@pytest.mark.parametrize("hours", [0.0, 5.5, 12.3456, 18.75])
def test_right_ascension_survives_a_round_trip(hours):
    assert _parse_ra(format_ra(hours)) == pytest.approx(hours, abs=1 / 3600.0)


@pytest.mark.parametrize("degrees", [0.0, 45.0, -23.4567, 66.5])
def test_declination_survives_a_round_trip(degrees):
    assert _parse_dec(format_dec(degrees)) == pytest.approx(degrees, abs=1 / 3600.0)


def test_right_ascension_wraps_at_24_hours():
    # 23.99999 h rounds up past 24 h, which is 0 h — not 24.
    assert format_ra(23.99999).startswith("00:")


def test_declination_keeps_the_sign_for_small_negatives():
    assert format_dec(-0.5).startswith("-00*30")


def test_parses_the_low_precision_reply_shape():
    # Controllers not in high-precision mode answer HH:MM.T instead.
    assert _parse_ra("05:30.5") == pytest.approx(5.5083, abs=1e-3)


def test_rejects_unparseable_coordinates():
    with pytest.raises(MountError):
        _parse_ra("not a coordinate")


def test_status_treats_absent_flags_as_active_states():
    # 'n' means NOT tracking and 'N' means NO goto, so their absence is the
    # positive case — the easiest thing to get backwards in this protocol.
    status = MountStatus.parse("pNO#")

    assert status.tracking is True
    assert status.slewing is False
    assert status.parked is False
    assert status.tracking_rate == "solar"


def test_status_reports_parked_and_home():
    status = MountStatus.parse("nNPH")

    assert status.parked is True
    assert status.at_home is True
    assert status.tracking is False
    assert "parked" in status.summary()


def test_status_reads_the_tracking_rate():
    assert MountStatus.parse("nN(").tracking_rate == "lunar"
    assert MountStatus.parse("nNk").tracking_rate == "king"
    assert MountStatus.parse("nN").tracking_rate == "sidereal"


def test_simulated_mount_reports_identity():
    with connect(simulated=True) as mount:
        assert "OnStepX" in mount.product_name()
        assert mount.firmware_version()


def test_goto_updates_the_reported_position():
    with connect(simulated=True) as mount:
        mount.goto(6.0, 23.5)
        ra, dec = mount.get_radec()

    assert ra == pytest.approx(6.0, abs=1e-3)
    assert dec == pytest.approx(23.5, abs=1e-3)


def test_goto_raises_with_the_controllers_reason_when_parked():
    with connect(simulated=True) as mount:
        mount.park()

        with pytest.raises(MountError, match="parked"):
            mount.goto(1.0, 2.0)


def test_solar_tracking_is_selectable():
    with connect(simulated=True) as mount:
        mount.set_tracking_rate("solar")
        mount.tracking_on()
        status = mount.status()

    assert status.tracking is True
    assert status.tracking_rate == "solar"


def test_unknown_tracking_rate_is_rejected():
    with connect(simulated=True) as mount:
        with pytest.raises(MountError):
            mount.set_tracking_rate("warp")


def test_commands_are_framed_even_when_given_bare():
    mount = OnStepXMount(SimulatedTransport())

    mount.send("GVP")

    assert mount.traffic[-1][1] == ":GVP#"
