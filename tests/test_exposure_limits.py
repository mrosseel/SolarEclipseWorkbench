"""What the body will take, and what a script's exposures become.

The X-T4's SDK module does not implement CapShutterSpeed, so the limits are an
override rather than a discovery — and the validator has to resolve exposures
through the very code the run uses, or a pre-flight check is a second opinion
rather than a preview.
"""

import pytest

from solareclipseworkbench import exposure_limits as limits


@pytest.fixture(autouse=True)
def _defaults():
    """Every test starts from the shipped defaults and no haze trim.

    Both are global: the trim especially, which a GUI test loads from the
    real settings file, so a saved -3 EV followed this file around and
    quietly rewrote every exposure it resolved.
    """
    from solareclipseworkbench import exposure_trim

    limits.set_limits(**vars(limits.Limits()))
    exposure_trim.set_stops(0.0)
    yield
    limits.set_limits(**vars(limits.Limits()))
    exposure_trim.set_stops(0.0)


def test_speeds_read_back_the_way_a_photographer_writes_them():
    assert limits.format_speed(1 / 4000) == "1/4000"
    assert limits.format_speed(6.0) == '6"'
    assert limits.parse_speed('1/2000"') == pytest.approx(1 / 2000)
    assert limits.parse_speed("0.5") == 0.5
    assert limits.parse_speed("nonsense") is None


def test_the_haze_trim_is_applied_then_the_limits():
    # +3 EV on a 4 s corona frame asks for 32 s; the cap is what actually goes
    # on the body, and the report has to say so rather than showing 32.
    r = limits.resolve("4", 800, trim_stops=3.0)

    assert r.requested_text == '4" ISO 800'
    assert r.speed_s == 6.0
    assert "capped" in r.note_text and "trim" in r.note_text


def test_the_fast_end_is_where_the_body_was_measured_to_stop():
    """Probed on the body: 1/32000 with the shutter type on MS+ES, and
    everything past 1/8000 refused with 0x2003 on MS alone.  Both have to
    hold, because the dial can move and software cannot read it."""
    fast = limits.resolve("1/16000", 100)
    assert fast.speed_s == pytest.approx(1 / 16000, rel=0.05)
    assert fast.needs_electronic_shutter
    assert not limits.resolve("1/4000", 100).needs_electronic_shutter

    # Back on the mechanical shutter, the same ask must be clamped rather
    # than sent to a body that will refuse it.
    limits.set_limits(fastest_s=1 / 8000)
    assert limits.resolve("1/16000", 100).speed_s == pytest.approx(1 / 8000, rel=0.05)


def test_the_half_stop_speeds_the_body_refuses_are_never_sent():
    """The SDK's table carries every model's scale at once, so a half-stop
    series sits interleaved with this body's third-stop one - and the probe
    found every one of them refused with 0x2003.  Snapping to the nearest
    entry in that table put a frame on a speed the body would not take, which
    is a refusal that reports success."""
    for refused in (1 / 45, 1 / 90, 1 / 180, 1 / 350, 1 / 750, 1 / 1500,
                    1 / 3000, 1 / 6000, 1 / 12000, 1 / 24000):
        landed = limits.nearest_accepted(refused)
        assert landed is not None
        assert landed != pytest.approx(refused, rel=1e-3), \
            f"{limits.format_speed(refused)} is not a speed this body takes"
        assert landed in limits.limits().accepted_speeds


def test_outside_the_measured_range_the_limits_decide():
    # The scale was measured over one range; past either end there is nothing
    # to judge against, so raising fastest_s after a re-probe is enough on its
    # own rather than needing the scale extended by hand as well.
    assert limits.nearest_accepted(1 / 64000) is None
    assert limits.nearest_accepted(30.0) is None
    assert limits.nearest_accepted(1 / 750) is not None


def test_iso_is_capped_both_ways():
    assert limits.resolve("1/500", 6400).iso == limits.limits().iso_max
    assert limits.resolve("1/500", 50).iso == limits.limits().iso_min


def test_the_limits_can_be_overridden():
    limits.set_limits(slowest_s=2.0, iso_max=800)

    r = limits.resolve("4", 3200)
    assert r.speed_s == 2.0
    assert r.iso == 800


def test_an_unusable_exposure_is_named_rather_than_guessed():
    r = limits.resolve("banana", 100)

    assert r.speed_s is None
    assert "not a shutter speed" in r.note_text


def test_validation_reads_the_script_the_scheduler_reads(tmp_path):
    script = tmp_path / "eclipse.txt"
    script.write_text(
        "# a comment line\n"
        "take_picture,C2,-,10.0,X-T4,1/4000,-,100,corona\n"
        "take_picture,C2,+,20.0,X-T4,4,-,3200,deep corona\n"
        "take_bracket,C3,-,5.0,X-T4,1/1000,-,200,1/2000;1/1000;1/500,ladder\n"
        "voice_prompt,C1,-,60.0,ready.wav\n"
    )

    rows = limits.validate_script(str(script), trim_stops=0.0)

    # The voice prompt has no exposure and must not appear.
    assert [r.command for r in rows] == ["take_picture", "take_picture", "take_bracket"]
    assert rows[0].resolved.requested_text == "1/4000 ISO 100"
    # ISO 3200 is over the cap, so that line is reported as changed.
    assert not rows[1].ok and "ISO capped" in rows[1].resolved.note_text
    assert len(rows[2].rungs) == 3


def test_the_report_names_what_changes_and_what_needs_the_electronic_shutter(tmp_path):
    script = tmp_path / "eclipse.txt"
    script.write_text("take_picture,C2,-,1.0,X-T4,1/16000,-,6400,beads\n")

    text = limits.report(limits.validate_script(str(script), trim_stops=0.0))

    assert "1 exposure(s) checked" in text
    assert "ISO capped" in text
    assert "MS+ES" in text
