"""Per-frame timing, and the scheduler faults that used to vanish.

Two silences this closes.  A command knew its camera but not the time it was
meant to run, so a frame four seconds late looked exactly like one on its mark.
And APScheduler discards a job that raises or misses its grace window without
telling anyone, which during totality is the eclipse going wrong quietly.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from solareclipseworkbench import frame_log, hardware_problems


@pytest.fixture(autouse=True)
def _fresh(tmp_path):
    frame_log.reset(tmp_path / "frames.csv")
    hardware_problems.clear()
    yield
    hardware_problems.clear()


def _intended(seconds_ago=0.0):
    return datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)


def test_a_frame_records_what_it_was_meant_to_do_and_when():
    frame_log.begin("Corona ladder 1", _intended())

    frame_log.record("ok")

    summary = frame_log.summarise()
    assert summary["rows"] == 1
    assert summary["ok"] == 1
    assert summary["lost"] == 0


def test_a_late_frame_is_visible_as_late():
    # The whole point: this is the case that used to be indistinguishable from
    # an on-time frame once the run was over.
    frame_log.begin("Corona ladder 1", _intended(seconds_ago=4.0))

    frame_log.record("ok")

    assert frame_log.summarise()["worst_delta_s"] >= 3.9


def test_a_dropped_frame_is_recorded_against_the_command_that_lost_it():
    # _serialised_on_camera drops a shot rather than take it late.  Deliberate,
    # but the log line alone was joined to nothing.
    frame_log.begin("C3 beads", _intended())

    frame_log.record("dropped", "camera busy for more than 1.5s")

    summary = frame_log.summarise()
    assert summary["lost"] == 1
    assert summary["outcomes"]["dropped"] == 1


def test_only_the_first_outcome_for_a_frame_is_kept():
    # The wrapper writes an outcome on the way out and the lock writes one when
    # it gives up; the specific one must win, not be doubled.
    frame_log.begin("C3 beads", _intended())

    frame_log.record("dropped", "camera busy")
    frame_log.record("ok")

    summary = frame_log.summarise()
    assert summary["rows"] == 1
    assert summary["outcomes"] == {"dropped": 1}


def test_recording_without_a_frame_in_progress_is_harmless():
    # A listener can fire for a job that never reached the wrapper.
    frame_log.record("error", "nothing was running")


def test_a_run_is_summarised_rather_than_read():
    for n, outcome in enumerate(("ok", "ok", "dropped", "error")):
        frame_log.begin(f"frame {n}", _intended(seconds_ago=n))
        frame_log.record(outcome)

    summary = frame_log.summarise()
    assert summary["rows"] == 4
    assert summary["ok"] == 2
    assert summary["lost"] == 2
    assert summary["worst_delta_s"] >= 2.9


def test_bookkeeping_never_costs_a_frame(monkeypatch):
    # A frame is worth more than its own record: if the file cannot be written,
    # the shot still happens.
    frame_log.begin("Corona ladder 1", _intended())
    monkeypatch.setattr(frame_log, "_append",
                        lambda row: (_ for _ in ()).throw(OSError("read-only")))

    frame_log.record("ok")          # must not raise


# ------------------------------------------------- the scheduler's own faults

def test_a_job_that_raises_is_reported_rather_than_swallowed():
    from solareclipseworkbench import utils

    frame_log.begin("Corona ladder 1", _intended())
    event = SimpleNamespace(job_id="job-1", exception=RuntimeError("USB gone"),
                            job=SimpleNamespace(name="Corona ladder 1"))

    utils._on_job_problem(event)

    problems = hardware_problems.peek()
    assert any("Corona ladder 1" in str(p) for p in problems), problems
    assert frame_log.summarise()["outcomes"] == {"error": 1}


def test_a_job_that_never_ran_is_reported_too():
    from solareclipseworkbench import utils

    frame_log.begin("C3 beads", _intended())
    event = SimpleNamespace(job_id="job-2", exception=None,
                            job=SimpleNamespace(name="C3 beads"))

    utils._on_job_problem(event)

    assert any("C3 beads" in str(p) for p in hardware_problems.peek())
    assert frame_log.summarise()["outcomes"] == {"missed": 1}
