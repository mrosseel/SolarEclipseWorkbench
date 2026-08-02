"""Draining the volatile buffer, where an undercount is invisible until it bites.

Every frame shot with an SDK session open holds one of 32 slots until it is
drained, and a full buffer stops the body dead mid-eclipse.  GetBufferCapacity
counts only the frames the body has finished writing, so the count a drain works
from can be short of what is really queued.
"""

from unittest.mock import MagicMock

import pytest

from fujixsdk import camera as camera_mod
from fujixsdk._constants import ERRCODE_BUSY, IMAGEFORMAT_NONE, IMAGEFORMAT_RAW
from fujixsdk._errors import BusyError
from fujixsdk.camera import Camera


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    monkeypatch.setattr(camera_mod, 'DRAIN_SETTLE_S', 0.001)


def _camera(capacities, formats=None):
    """A camera whose buffer reports `capacities`, one entry per capacity read."""
    cam = Camera.__new__(Camera)              # bypass the SDK-dependent __init__
    cam._closed = True                        # nothing to close down at collection
    cam.get_buffer_capacity = MagicMock(side_effect=[(c, 32) for c in capacities])
    cam.delete_image = MagicMock()
    info = MagicMock(format=IMAGEFORMAT_RAW, data_size=1024)
    cam.read_image_info = MagicMock(
        side_effect=formats if formats is not None else lambda: info)
    return cam


def test_frames_still_being_written_are_caught_by_a_second_pass():
    # On 2 August a 13-frame bracket drained "13/13" while 8 frames were still
    # being written; they turned up in the next drain, credited to the wrong
    # bracket.  The buffer must be re-read until it is genuinely empty.
    cam = _camera([13, 8, 0])

    assert cam.drain_buffer() == 21


def test_a_buffer_that_reads_empty_costs_nothing():
    cam = _camera([0])

    assert cam.drain_buffer() == 0
    cam.delete_image.assert_not_called()


def test_a_pass_that_stops_early_is_not_followed_by_another():
    # An empty queue mid-pass means the body has nothing more to give; asking
    # again only spends the settle.
    none_at_third = [MagicMock(format=IMAGEFORMAT_RAW, data_size=1024)] * 2 + \
                    [MagicMock(format=IMAGEFORMAT_NONE, data_size=0)]
    cam = _camera([13], formats=none_at_third)

    assert cam.drain_buffer() == 2
    assert cam.get_buffer_capacity.call_count == 1


def test_one_pass_is_honoured_where_the_caller_asks_for_it():
    # Mid-burst the point is to free slots before the next frame, not to leave
    # the buffer spotless — the settle between passes is time not spent shooting.
    cam = _camera([13, 8, 0])

    assert cam.drain_buffer(passes=1) == 13
    assert cam.get_buffer_capacity.call_count == 1


def test_draining_stays_inside_its_budget_however_much_the_body_holds(monkeypatch):
    # A body that keeps handing back a full buffer must not hold the schedule
    # for as long as it likes: what is left goes with the next drain.
    monkeypatch.setattr(camera_mod, 'DRAIN_PASSES', 50)
    monkeypatch.setattr(camera_mod, 'DRAIN_BUDGET_S', 0.02)
    monkeypatch.setattr(camera_mod, 'DRAIN_SETTLE_S', 0.01)
    cam = Camera.__new__(Camera)
    cam._closed = True
    cam.get_buffer_capacity = MagicMock(return_value=(4, 32))
    cam.delete_image = MagicMock()
    cam.read_image_info = MagicMock(
        return_value=MagicMock(format=IMAGEFORMAT_RAW, data_size=1024))

    drained = cam.drain_buffer()

    # Bounded well short of the 200 an unbudgeted 50 passes would have deleted.
    assert 0 < drained <= 12


def test_a_busy_read_is_waited_out_and_the_image_still_drained():
    cam = _camera([1, 0])
    cam.read_image_info.side_effect = [BusyError(ERRCODE_BUSY, 'Camera is busy'),
                                       MagicMock(format=IMAGEFORMAT_RAW, data_size=1024)]

    assert cam.drain_buffer() == 1
    cam.delete_image.assert_called_once()


def test_nothing_is_deleted_or_counted_on_a_read_that_never_clears(monkeypatch):
    # Until 2 August a busy read deleted blind and counted it, inflating the
    # drain figure with images that may never have existed — and that figure is
    # the only evidence of how many frames one tap really queues.
    monkeypatch.setattr(camera_mod, 'DRAIN_BUDGET_S', 0.02)
    monkeypatch.setattr(camera_mod, 'DRAIN_BUSY_BACKOFF_S', 0.005)
    cam = _camera([13, 0])
    cam.read_image_info.side_effect = BusyError(ERRCODE_BUSY, 'Camera is busy')

    assert cam.drain_buffer() == 0
    cam.delete_image.assert_not_called()


def test_a_delete_refused_while_busy_is_retried_not_abandoned(monkeypatch):
    # The read already confirmed an image is there, so the slot has to come back
    # or the body stops dead when the buffer fills.
    monkeypatch.setattr(camera_mod, 'DRAIN_BUSY_BACKOFF_S', 0.001)
    cam = _camera([1, 0])
    cam.delete_image.side_effect = [BusyError(ERRCODE_BUSY, 'Camera is busy'), None]

    assert cam.drain_buffer() == 1
    assert cam.delete_image.call_count == 2


def test_the_log_shows_how_the_drain_split_across_passes(caplog):
    # 13 taps that drain as "13 + 8" say the body queued 8 more frames than it
    # had finished writing when the first pass looked.
    cam = _camera([13, 8, 0])

    with caplog.at_level('INFO', logger=camera_mod.log.name):
        cam.drain_buffer()

    assert '13 + 8' in caplog.text
