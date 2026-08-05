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


def test_commands_in_the_past_are_skipped_once_not_warned_per_job(caplog):
    """Starting a simulation two minutes before C2 used to open with a hundred
    missed-job warnings about the partials of the last hour.  A wall of WARN
    that means "everything is fine" teaches the reader to skim WARN, which is
    the one thing it must never teach."""
    import logging as logging_mod
    from datetime import datetime, timedelta

    import pytz

    from solareclipseworkbench.utils import schedule_commands, start_scheduler

    now = datetime.now(pytz.utc)
    moments = {"C1": SimpleNamespace(time_utc=now - timedelta(hours=1)),
               "C2": SimpleNamespace(time_utc=now + timedelta(seconds=90)),
               "C3": SimpleNamespace(time_utc=now + timedelta(seconds=194)),
               "C4": SimpleNamespace(time_utc=now + timedelta(hours=1))}

    import io, textwrap
    script = io.StringIO(textwrap.dedent("""\
        voice_prompt, C1, +, 0:00:10.0, C2_IN_50_MINUTES, "long gone"
        voice_prompt, C1, +, 0:10:00.0, C2_IN_40_MINUTES, "also gone"
        voice_prompt, C2, -, 0:00:20.0, C2_IN_10_MINUTES, "still to come"
    """))

    from unittest.mock import patch
    scheduler = start_scheduler()
    try:
        with patch("solareclipseworkbench.utils.scripts.convert_script",
                   return_value=script):
            with caplog.at_level(logging_mod.INFO):
                schedule_commands("ignored", scheduler, moments, {}, None,
                                  None, None)
        jobs = scheduler.get_jobs()
        assert len(jobs) == 1, "the future command must still be scheduled"
        missed = [r for r in caplog.records if "missed" in r.getMessage()]
        assert not missed, "the past still warns per job"
        summaries = [r for r in caplog.records
                     if "Skipped" in r.getMessage() and "before" in r.getMessage()]
        assert len(summaries) == 1, "the past should be said once"
        assert summaries[0].levelno == logging_mod.WARNING
        assert "Skipped 2 of 3" in summaries[0].getMessage()
    finally:
        scheduler.shutdown(wait=False)
