"""Fuji failures must be loud.

Each of these pins a place where the old code returned normally after something
had gone wrong, leaving the eclipse to be photographed at the wrong settings or
not at all.
"""

from unittest.mock import MagicMock

import pytest

from solareclipseworkbench import fuji_camera, hardware_problems
from solareclipseworkbench.camera import CameraError
from solareclipseworkbench.fuji_camera import FujiCamera
from solareclipseworkbench.hardware_registry import register_hardware


@pytest.fixture(autouse=True)
def _clean():
    hardware_problems.clear()
    yield
    hardware_problems.clear()


def _camera(**sdk):
    sdk_cam = MagicMock(**sdk)
    # An empty queue unless a test says otherwise, so `drain` stops after one
    # round instead of looping for the burst tail it is there to catch.
    sdk_cam.drain_buffer.return_value = 0
    camera = FujiCamera.__new__(FujiCamera)      # bypass SDK-dependent __init__
    camera._sdk_cam = sdk_cam
    camera.name = "X-T4"
    camera._applied_iso = None
    camera._applied_speed = None
    import threading
    camera._lock = threading.RLock()
    return camera, sdk_cam


# ------------------------------------------------------------------ configure


def test_configure_raises_when_a_setting_is_rejected():
    camera, sdk = _camera()
    sdk.set_shutter_speed.side_effect = RuntimeError("busy")

    with pytest.raises(CameraError, match="shutter speed"):
        camera.configure(shutter_speed="1/2000")


def test_configure_reports_every_failure_not_just_the_first():
    camera, sdk = _camera()
    sdk.set_iso.side_effect = RuntimeError("nope")
    sdk.set_shutter_speed.side_effect = RuntimeError("busy")

    with pytest.raises(CameraError) as exc:
        camera.configure(iso=200, shutter_speed="1/2000")

    assert "ISO" in str(exc.value)
    assert "shutter speed" in str(exc.value)


def test_configure_tries_the_remaining_settings_after_one_fails():
    camera, sdk = _camera()
    sdk.set_iso.side_effect = RuntimeError("nope")

    with pytest.raises(CameraError):
        camera.configure(iso=200, shutter_speed="1/2000")

    # The shutter speed must still have been attempted.
    assert sdk.set_shutter_speed.called


def test_configure_rejects_a_value_the_camera_cannot_express():
    camera, _ = _camera()

    with pytest.raises(CameraError, match="not a value"):
        camera.configure(iso="not-a-number")


def test_configure_succeeds_quietly_when_everything_applies():
    camera, sdk = _camera()

    camera.configure(iso=200, aperture="5.6")

    assert sdk.set_iso.called and sdk.set_aperture.called


# ------------------------------------------------------------------- bracket


def test_bracket_raises_instead_of_returning_no_frames():
    # The old code returned [] here, so the bracket silently took zero frames.
    camera, sdk = _camera()
    sdk.get_shutter_speed.side_effect = RuntimeError("no reply")

    with pytest.raises(CameraError, match="bracket"):
        camera.parse_bracket_speeds("+/- 1")


def test_bracket_spans_the_requested_range():
    camera, sdk = _camera()
    supported = list(range(100, 130))
    sdk.get_shutter_speed.return_value = (115, 0)
    sdk.get_supported_shutter_speeds.return_value = supported

    speeds = camera.parse_bracket_speeds("+/- 1")

    # +/- 1 EV at 1/3 EV per position is three either side, so seven frames.
    assert len(speeds) == 7
    assert 115 in speeds


def test_a_bracket_frame_whose_speed_was_refused_is_still_taken(monkeypatch):
    # There is no second chance at a contact: a frame at the previous speed
    # beats no frame at all.  But it will look normal until it is reviewed, so
    # the failure has to be reported rather than swallowed.
    monkeypatch.setattr(fuji_camera, "BUSY_BACKOFF_S", 0.0)
    monkeypatch.setattr(fuji_camera, "TAP_GAP_S", 0.0)
    monkeypatch.setattr(fuji_camera, "SETTLE_BEFORE_DRAIN_S", 0.0)

    camera, sdk = _camera()
    sdk.set_shutter_speed.side_effect = RuntimeError("busy")
    relay = MagicMock()
    register_hardware("relay", relay)
    try:
        taken = fuji_camera._RelayShooter(camera).bracket_no_download([100, 200, 300])
    finally:
        register_hardware("relay", None)

    assert taken == 3
    assert relay.shoot.call_count == 3
    assert hardware_problems.count() == 1
    assert "wrong shutter speed" in hardware_problems.peek()[0].message


def test_a_single_frame_does_not_stop_to_drain_a_queue_with_room(monkeypatch):
    # A drain is a settle plus the deletes, well over a second, to reclaim one of
    # 32 slots.  Paying it after every frame spends the gap between two scripted
    # frames on nothing, which during totality is frames not taken.
    monkeypatch.setattr(fuji_camera, "SETTLE_BEFORE_DRAIN_S", 0.0)

    camera, sdk = _camera()
    sdk.get_buffer_capacity.return_value = (3, 32)
    register_hardware("relay", MagicMock())
    try:
        camera.capture()
    finally:
        register_hardware("relay", None)

    assert not sdk.drain_buffer.called


def test_a_single_frame_does_drain_a_queue_that_has_filled(monkeypatch):
    monkeypatch.setattr(fuji_camera, "SETTLE_BEFORE_DRAIN_S", 0.0)

    camera, sdk = _camera()
    sdk.get_buffer_capacity.return_value = (30, 32)
    register_hardware("relay", MagicMock())
    try:
        camera.capture()
    finally:
        register_hardware("relay", None)

    assert sdk.drain_buffer.called


def test_a_bracket_fires_its_shortest_exposure_first():
    # At C2 the first rungs go off while the calculated contact time may still be
    # wrong by a second or two, and a trailing bead is only caught by a short
    # exposure.  Shortest-first is therefore insurance, not presentation: it must
    # survive any future change to how the ladder is built.
    camera, sdk = _camera()
    sdk.get_shutter_speed.return_value = (500_000, 0)     # 1/2"
    sdk.get_supported_shutter_speeds.return_value = []    # the X-T4 answers empty

    speeds = camera.parse_bracket_speeds("+/- 1")

    assert speeds == sorted(speeds), 'the ladder must climb from short to long'
    assert speeds[0] < 500_000 < speeds[-1]


def test_the_bracket_shoots_the_ladder_in_the_order_it_was_built(monkeypatch):
    # The order only buys anything if it reaches the shutter: a ladder built
    # short-first and fired long-first would look identical in the log.
    monkeypatch.setattr(fuji_camera, "TAP_GAP_S", 0.0)
    monkeypatch.setattr(fuji_camera, "SETTLE_BEFORE_DRAIN_S", 0.0)

    camera, sdk = _camera()
    sdk.get_buffer_capacity.return_value = (0, 32)
    register_hardware("relay", MagicMock())
    try:
        fuji_camera._RelayShooter(camera).bracket_no_download([100, 200, 300])
    finally:
        register_hardware("relay", None)

    written = [call.args[0] for call in sdk.set_shutter_speed.call_args_list]
    assert written == [100, 200, 300]


def _bracket(monkeypatch, capacity, speeds=(100, 200, 300)):
    """Run a relay bracket against a body whose queue reports `capacity`."""
    monkeypatch.setattr(fuji_camera, "TAP_GAP_S", 0.0)
    monkeypatch.setattr(fuji_camera, "SETTLE_BEFORE_DRAIN_S", 0.0)

    camera, sdk = _camera()
    sdk.get_buffer_capacity.return_value = capacity
    relay = MagicMock()
    register_hardware("relay", relay)
    try:
        fuji_camera._RelayShooter(camera).bracket_no_download(list(speeds))
    finally:
        register_hardware("relay", None)
    return camera, sdk, relay


def test_a_bracket_clears_the_queue_before_it_overflows(monkeypatch):
    # On CH — which the beads bursts need — a fast rung fires twice, so a 13-rung
    # bracket measured 28 frames against 32 slots.  A full buffer stops the body
    # dead and wants the battery pulled, so a filling queue cannot wait for the
    # end of the bracket.
    _, sdk, relay = _bracket(monkeypatch, capacity=(30, 32))

    assert sdk.drain_buffer.called
    # Draining with the half-press still held drops the USB session for good.
    assert relay.release_all.called
    order = [name for name, _, _ in relay.mock_calls if name in ('release_all', 'half_press')]
    assert order.index('release_all') < order.index('half_press', order.index('release_all'))


def test_a_bracket_with_room_left_does_not_stop_to_drain(monkeypatch):
    # The stop costs a settle per drain; a queue with room does not earn one.
    _, sdk, _ = _bracket(monkeypatch, capacity=(2, 32))

    # Only the drain that ends every bracket, none mid-sequence.
    assert sdk.drain_buffer.call_count == 1


def test_a_queue_that_cannot_be_read_does_not_cost_the_bracket(monkeypatch):
    # A buffer reading is not worth failing a bracket over: there is no second
    # chance at a contact, and the end-of-bracket drain still runs.
    monkeypatch.setattr(fuji_camera, "TAP_GAP_S", 0.0)
    monkeypatch.setattr(fuji_camera, "SETTLE_BEFORE_DRAIN_S", 0.0)

    camera, sdk = _camera()
    sdk.get_buffer_capacity.side_effect = RuntimeError("unreadable")
    register_hardware("relay", MagicMock())
    try:
        taken = fuji_camera._RelayShooter(camera).bracket_no_download([100, 200, 300])
    finally:
        register_hardware("relay", None)

    assert taken == 3


def test_a_bracket_does_not_reapply_the_iso_the_caller_just_set(monkeypatch):
    # The SDK shooter defaults to ISO 100 and writes it; the relay path must not,
    # or every bracket asked for at ISO 400 would be photographed at 100.
    monkeypatch.setattr(fuji_camera, "TAP_GAP_S", 0.0)
    monkeypatch.setattr(fuji_camera, "SETTLE_BEFORE_DRAIN_S", 0.0)

    camera, sdk = _camera()
    register_hardware("relay", MagicMock())
    try:
        fuji_camera._RelayShooter(camera).bracket_no_download([100], iso=400)
    finally:
        register_hardware("relay", None)

    assert not sdk.set_iso.called


# ----------------------------------------------------------------------- ISO


def test_the_same_iso_is_written_once_not_on_every_frame():
    # set_iso is refused unless the transfer queue is empty, so a write that
    # cannot change anything is a round-trip that can only fail.
    camera, sdk = _camera()

    for _ in range(4):
        camera.configure(iso=400)

    assert sdk.set_iso.call_count == 1


def test_a_changed_iso_is_written_again():
    camera, sdk = _camera()

    camera.configure(iso=100)
    camera.configure(iso=100)
    camera.configure(iso=800)

    assert sdk.set_iso.call_count == 2


def test_a_refused_iso_is_retried_on_the_next_frame():
    # A rejected write leaves the body at an unknown ISO, so the next frame must
    # try again rather than trust a value that never landed.
    camera, sdk = _camera()
    sdk.set_iso.side_effect = RuntimeError("XSDK error 0x00001006: Camera is busy")

    for _ in range(3):
        with pytest.raises(CameraError, match="ISO"):
            camera.configure(iso=400)

    assert sdk.set_iso.call_count == 3


def test_a_burst_makes_room_before_it_closes_the_contact(monkeypatch):
    # A burst at the cap queues 30 of 32 slots, measured, and nothing checks the
    # buffer once the contact is closed.  Singles no longer drain after every
    # frame, so the queue arriving here can be most of the way full: a run of
    # singles before the C3 burst would otherwise fill it and stop the body.
    monkeypatch.setattr(fuji_camera, "SETTLE_BEFORE_DRAIN_S", 0.0)

    camera, sdk = _camera()
    sdk.get_buffer_capacity.return_value = (24, 32)     # what singles can leave
    register_hardware("relay", MagicMock())
    try:
        fuji_camera._RelayShooter(camera).burst_no_download(28)
    finally:
        register_hardware("relay", None)

    # Once to make room, once after the burst.
    assert sdk.drain_buffer.call_count == 2


def test_a_burst_against_an_empty_queue_does_not_stop_first(monkeypatch):
    # The room-making drain costs a settle; an empty queue has not earned one,
    # and this runs at C2 where the seconds are the whole point.
    monkeypatch.setattr(fuji_camera, "SETTLE_BEFORE_DRAIN_S", 0.0)

    camera, sdk = _camera()
    sdk.get_buffer_capacity.return_value = (0, 32)
    register_hardware("relay", MagicMock())
    try:
        fuji_camera._RelayShooter(camera).burst_no_download(28)
    finally:
        register_hardware("relay", None)

    assert sdk.drain_buffer.call_count == 1      # only the one after the burst


def test_a_burst_drains_when_the_queue_cannot_be_read(monkeypatch):
    # A wasted second beats a buffer that fills mid-burst.
    monkeypatch.setattr(fuji_camera, "SETTLE_BEFORE_DRAIN_S", 0.0)

    camera, sdk = _camera()
    sdk.get_buffer_capacity.side_effect = RuntimeError("unreadable")
    register_hardware("relay", MagicMock())
    try:
        fuji_camera._RelayShooter(camera).burst_no_download(28)
    finally:
        register_hardware("relay", None)

    assert sdk.drain_buffer.call_count == 2


def test_a_drain_keeps_going_until_a_round_comes_back_empty(monkeypatch):
    # The body reports frames as it writes them, and a burst is still arriving
    # 2.5s after the contact opens: one drain a second later left 8 of 29 queued,
    # measured 3 August.  They then sit there until something else clears them.
    monkeypatch.setattr(fuji_camera, "SETTLE_BEFORE_DRAIN_S", 0.0)
    monkeypatch.setattr(fuji_camera, "SETTLE_BETWEEN_DRAINS_S", 0.0)

    camera, sdk = _camera()
    sdk.drain_buffer.side_effect = [20, 7, 2, 0]

    assert camera.drain() == 29


def test_a_drain_stops_at_its_round_limit(monkeypatch):
    # A body that hands back frames indefinitely must not hold the schedule.
    monkeypatch.setattr(fuji_camera, "SETTLE_BEFORE_DRAIN_S", 0.0)
    monkeypatch.setattr(fuji_camera, "SETTLE_BETWEEN_DRAINS_S", 0.0)
    monkeypatch.setattr(fuji_camera, "DRAIN_ROUNDS", 3)

    camera, sdk = _camera()
    sdk.drain_buffer.return_value = 5

    assert camera.drain() == 15
    assert sdk.drain_buffer.call_count == 3


def test_an_empty_queue_costs_one_round(monkeypatch):
    # A bracket or a single settles inside the first round; it must not pay for
    # the burst tail this is here to catch.
    monkeypatch.setattr(fuji_camera, "SETTLE_BEFORE_DRAIN_S", 0.0)
    monkeypatch.setattr(fuji_camera, "SETTLE_BETWEEN_DRAINS_S", 0.0)

    camera, sdk = _camera()

    assert camera.drain() == 0
    assert sdk.drain_buffer.call_count == 1


def test_free_slots_ignore_the_total_the_sdk_reports_beside_the_count():
    # While frames are written the SDK returns total = captured + 3, so a burst
    # against an empty-but-still-writing queue would see three free slots and
    # drain for nothing — or worse, a filling one would look fine.
    camera, sdk = _camera()
    sdk.get_buffer_capacity.return_value = (2, 5)      # mid-write shape

    assert not camera.buffer_is_filling()
    assert camera.ensure_room_for(28) == 0
