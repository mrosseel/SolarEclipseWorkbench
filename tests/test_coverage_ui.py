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
