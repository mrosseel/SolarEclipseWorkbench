"""Changing the simulator must move the countdowns with it.

The offset was only ever set when a schedule was started, so picking a
different reference moment left every countdown on the previous one - or, with
no run yet, on the real time to an eclipse days away.
"""

import datetime
from types import SimpleNamespace

import pytz

from solareclipseworkbench.utils import simulation_offset


def _moments(c2_utc):
    return {"C2": SimpleNamespace(time_utc=c2_utc)}


def test_the_offset_puts_the_reference_moment_where_it_was_asked_for():
    now = datetime.datetime(2026, 8, 4, 12, 0, tzinfo=pytz.utc)
    c2 = datetime.datetime(2026, 8, 12, 18, 30, tzinfo=pytz.utc)

    offset = simulation_offset(_moments(c2), "C2", 2, now)

    # Two minutes from now, the simulated clock should read C2.
    assert now + datetime.timedelta(minutes=2) + offset == c2


def test_a_moment_after_the_reference_works_the_same_way():
    now = datetime.datetime(2026, 8, 4, 12, 0, tzinfo=pytz.utc)
    c2 = datetime.datetime(2026, 8, 12, 18, 30, tzinfo=pytz.utc)

    offset = simulation_offset(_moments(c2), "C2", -5, now)

    assert now - datetime.timedelta(minutes=5) + offset == c2


def test_no_simulation_means_no_offset():
    now = datetime.datetime(2026, 8, 4, 12, 0, tzinfo=pytz.utc)
    c2 = datetime.datetime(2026, 8, 12, 18, 30, tzinfo=pytz.utc)

    assert simulation_offset(_moments(c2), None, 2, now) == datetime.timedelta(0)
    assert simulation_offset(_moments(c2), "", 2, now) == datetime.timedelta(0)


def test_a_moment_the_eclipse_does_not_have_is_not_an_offset():
    # C3 exists in the combo for every eclipse; the moments dict may not have
    # it if the calculation did not produce one.
    now = datetime.datetime(2026, 8, 4, 12, 0, tzinfo=pytz.utc)
    c2 = datetime.datetime(2026, 8, 12, 18, 30, tzinfo=pytz.utc)

    assert simulation_offset(_moments(c2), "C3", 2, now) == datetime.timedelta(0)


def test_the_scheduler_and_the_countdowns_use_the_same_formula():
    """They disagreeing is the bug this was extracted for."""
    import inspect

    from solareclipseworkbench import utils

    source = inspect.getsource(utils.observe_solar_eclipse)
    assert "simulation_offset(" in source, \
        "the scheduler computes its own offset again"
