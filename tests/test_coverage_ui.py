"""The coverage timeline reads the schedule, not the file.

What is scheduled is what will run: reference moments resolved, and any line
naming a moment this eclipse does not have already dropped.
"""

import datetime
from types import SimpleNamespace

from solareclipseworkbench import hardware_registry
from solareclipseworkbench.coverage_ui import (FRAME_COMMANDS, CoverageDock,
                                               CoverageView, _duration)


def _job(job_id, when, command):
    hardware_registry.note_job_command(job_id, command)
    return SimpleNamespace(id=job_id, next_run_time=when)


def _schedule():
    base = datetime.datetime(2026, 8, 12, 18, 28, 56, tzinfo=datetime.timezone.utc)
    jobs = [
        _job("a", base - datetime.timedelta(minutes=30), "take_picture"),
        _job("b", base - datetime.timedelta(seconds=4), "relay_burst"),
        _job("c", base + datetime.timedelta(seconds=20), "take_bracket"),
        _job("d", base + datetime.timedelta(seconds=40), "voice_prompt"),
        _job("e", base + datetime.timedelta(minutes=30), "take_picture"),
    ]
    moments = {
        "C1": SimpleNamespace(time_utc=base - datetime.timedelta(minutes=55)),
        "C2": SimpleNamespace(time_utc=base),
        "C3": SimpleNamespace(time_utc=base + datetime.timedelta(seconds=104)),
        "C4": SimpleNamespace(time_utc=base + datetime.timedelta(minutes=57)),
    }
    return jobs, moments


def test_the_summary_counts_frames_and_not_announcements():
    jobs, moments = _schedule()

    summary = CoverageDock._summary(jobs, moments)

    assert "take picture" in summary
    assert "relay burst" in summary
    assert "voice prompt" not in summary, "a prompt is not a photograph"


def test_a_voice_prompt_is_not_a_frame():
    assert "voice_prompt" not in FRAME_COMMANDS
    assert "take_bracket" in FRAME_COMMANDS
    assert "relay_burst" in FRAME_COMMANDS


def test_nothing_loaded_says_so_rather_than_drawing_an_empty_axis():
    assert CoverageDock._summary([], {}) == "No script loaded"


def test_the_view_keeps_only_jobs_it_can_place():
    jobs, moments = _schedule()
    jobs.append(SimpleNamespace(id="unknown", next_run_time=None))
    view = CoverageView()

    view.set_schedule(jobs, moments)

    assert len(view._events) == 5, "a job with no time cannot be drawn"
    assert view._events == sorted(view._events), "events are drawn in time order"


def test_durations_read_as_people_say_them():
    assert _duration(104) == "104 s"
    assert _duration(6720) == "1 h 52 m"
    assert _duration(180) == "3 m 00 s"


def test_the_totality_row_takes_in_the_bead_bursts():
    """The C2 burst fires before second contact - the diamond ring is the last
    bead before totality - so a row starting exactly at C2 leaves out the most
    important command in the script.
    """
    import datetime

    from solareclipseworkbench.coverage_ui import CoverageView

    base = datetime.datetime(2026, 8, 12, 18, 28, 56, tzinfo=datetime.timezone.utc)
    c3 = base + datetime.timedelta(seconds=104)
    view = CoverageView()
    view._moments = {
        "C2": SimpleNamespace(time_utc=base),
        "C3": SimpleNamespace(time_utc=c3),
        "BEADS_C2_START": SimpleNamespace(time_utc=base - datetime.timedelta(seconds=3.6)),
        "BEADS_C3_END": SimpleNamespace(time_utc=c3 + datetime.timedelta(seconds=4.4)),
    }

    start, end = view._totality_span()

    assert start < base - datetime.timedelta(seconds=3.6), "the C2 burst is off the row"
    assert end > c3 + datetime.timedelta(seconds=4.4), "the C3 burst is off the row"


def test_the_axis_ticks_are_round_numbers():
    import datetime

    from solareclipseworkbench.coverage_ui import _ticks

    start = datetime.datetime(2026, 8, 12, 18, 28, 56, tzinfo=datetime.timezone.utc)
    ticks = _ticks(start, start + datetime.timedelta(seconds=120))

    assert ticks, "an axis with no ticks is a line"
    assert all(t.second % 10 == 0 for t in ticks), \
        "ticks land on round seconds so the axis reads as a clock"
    assert len(ticks) <= 14


# ------------------------------------------------------------- the new numbers

def _moment(when):
    return SimpleNamespace(time_utc=when)


def test_the_symlog_axis_is_monotonic_and_centred():
    from solareclipseworkbench.coverage_ui import _symlog

    offsets = [-7200, -600, -30, 0, 30, 600, 7200]
    mapped = [_symlog(o) for o in offsets]
    assert mapped == sorted(mapped), "time must not change order on the axis"
    assert _symlog(0) == 0
    assert _symlog(-60) == -_symlog(60), "the compression is symmetric"
    # The point of the scale: an hour is nowhere near 120x wider than 30 s.
    assert _symlog(3600) / _symlog(30) < 8


def test_utilization_counts_busy_time_once():
    """A burst and a bracket overlapping into its drain must not claim the
    same seconds twice - that is how a plan reads over 100%."""
    from solareclipseworkbench.coverage_ui import analyse

    base = datetime.datetime(2026, 8, 12, 18, 28, 56, tzinfo=datetime.timezone.utc)
    moments = {"C2": _moment(base), "C3": _moment(base + datetime.timedelta(seconds=100))}
    burst = [(base + datetime.timedelta(seconds=i * 0.13), 1 / 500)
             for i in range(60)]                      # ~7.75 s held
    overlapping = [(base + datetime.timedelta(seconds=6 + i), 0.5)
                   for i in range(3)]

    numbers = analyse([(burst[0][0], "relay_burst", burst),
                       (overlapping[0][0], "take_bracket", overlapping)],
                      moments)

    assert numbers["frames_totality"] == 63
    assert 0 < numbers["utilization"] <= 1.0
    # Hold ~7.8 + 4.5 tail, bracket runs 6..8.5 then drains: merged ~13 s.
    assert 0.10 < numbers["utilization"] < 0.20


def test_a_plan_with_no_contacts_still_counts_frames():
    from solareclipseworkbench.coverage_ui import analyse

    base = datetime.datetime(2026, 8, 12, 18, 0, tzinfo=datetime.timezone.utc)
    numbers = analyse([(base, "take_picture", [(base, 0.01)])], {})

    assert numbers["frames_total"] == 1
    assert numbers["utilization"] is None, "no C2-C3, no denominator"


def test_merged_intervals_swallow_overlap():
    from solareclipseworkbench.coverage_ui import merged_intervals

    a = datetime.datetime(2026, 8, 12, 18, 0, tzinfo=datetime.timezone.utc)
    s = lambda n: a + datetime.timedelta(seconds=n)
    merged = merged_intervals([(s(0), s(10)), (s(5), s(12)), (s(20), s(21))])

    assert merged == [(s(0), s(12)), (s(20), s(21))]


def test_the_dock_wears_the_numbers(app=None):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])

    jobs, moments = _schedule()
    moments["MAX"] = _moment(moments["C2"].time_utc
                             + datetime.timedelta(seconds=52))
    dock = CoverageDock()
    scheduler = SimpleNamespace(get_jobs=lambda: jobs)

    dock.set_schedule(scheduler, moments)

    assert dock.stat_frames.value.text() not in ("--", "0")
    assert dock.stat_util.value.text().endswith("%")
    assert dock.stat_totality.value.text() == "104 s"
