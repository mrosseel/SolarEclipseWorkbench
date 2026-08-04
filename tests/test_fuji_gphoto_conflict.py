"""gphoto2 must not claim a Fuji body the SDK can see.

4 August, on the bench: the SDK detected the X-T4 and failed to open it, so
detection returned an empty dict; gphoto2 read that as "no Fuji here", claimed
the USB device one second later, and every SDK call from then on answered
0x2001 until the body was power-cycled.  The failure caused the claim and the
claim guaranteed the failure.
"""

import logging

import pytest

from solareclipseworkbench import camera as camera_module
from solareclipseworkbench.fuji_camera import FujiDetection


@pytest.fixture
def no_gphoto_claims(monkeypatch):
    """Record every body gphoto2 would open, without opening anything."""
    claimed = []

    monkeypatch.setattr(camera_module, 'get_cameras',
                        lambda: [('Fuji Fujifilm X-T4', 'usb:001,001')])

    def _claim(model_name, port):
        claimed.append(model_name)
        raise AssertionError(f"gphoto2 claimed {model_name}")

    monkeypatch.setattr(camera_module, 'get_camera_by_port', _claim)
    return claimed


def _detection(cameras, bodies_seen, monkeypatch):
    import solareclipseworkbench.fuji_camera as fuji
    monkeypatch.setattr(fuji, 'find_fuji_sdk_path', lambda: '/nonexistent/sdk')
    monkeypatch.setattr(fuji, 'detect_fuji',
                        lambda path: FujiDetection(cameras, bodies_seen))


def test_a_body_the_sdk_saw_but_could_not_open_is_left_alone(
        no_gphoto_claims, monkeypatch, caplog):
    # The regression: seen, not opened.  gphoto2 cannot drive an X series body
    # tethered, so claiming it gains nothing and costs the SDK the device.
    _detection({}, 1, monkeypatch)

    with caplog.at_level(logging.INFO):
        result = camera_module.get_camera_dict()

    assert no_gphoto_claims == [], "gphoto2 claimed a body the SDK can see"
    assert result == {}
    assert any('Fuji SDK' in r.message for r in caplog.records), \
        "nothing in the log says why the body is missing"


def test_a_body_the_sdk_opened_is_also_left_alone(no_gphoto_claims, monkeypatch):
    # The case that already worked, kept honest.
    sentinel = object()
    _detection({'Fuji Fujifilm X-T4': sentinel}, 1, monkeypatch)

    result = camera_module.get_camera_dict()

    assert no_gphoto_claims == []
    assert result['Fuji Fujifilm X-T4'] is sentinel


def test_a_non_fuji_body_is_still_gphoto2s(monkeypatch):
    # The skip must stay narrow: everything else goes on being claimed.
    _detection({}, 1, monkeypatch)
    monkeypatch.setattr(camera_module, 'get_cameras',
                        lambda: [('Canon EOS 80D', 'usb:001,002')])
    opened = []
    monkeypatch.setattr(camera_module, 'get_camera_by_port',
                        lambda m, p: opened.append(m) or type('C', (), {'name': m})())

    camera_module.get_camera_dict()

    assert opened == ['Canon EOS 80D']
