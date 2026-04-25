from unittest.mock import patch

import gphoto2

from solareclipseworkbench.camera import get_free_space, get_space


class _SonyNoStorageCamera:
    vendor = "Sony"
    name = "Sony ZV-E10 (Control)"

    def get_storageinfo(self):
        raise gphoto2.GPhoto2Error(-1)


class _CanonNoStorageCamera:
    vendor = "Canon"
    name = "Canon EOS 80D"

    def get_storageinfo(self):
        raise gphoto2.GPhoto2Error(-1)


def test_sony_storageinfo_minus_one_returns_unknown_without_reinit():
    camera = _SonyNoStorageCamera()

    with patch("solareclipseworkbench.camera.get_camera") as get_camera_mock:
        free = get_free_space(camera)
        total = get_space(camera)

    assert free == -1.0
    assert total == -1.0
    get_camera_mock.assert_not_called()


def test_non_sony_minus_one_still_tries_reinit():
    camera = _CanonNoStorageCamera()

    with patch("solareclipseworkbench.camera.get_camera", side_effect=Exception("boom")) as get_camera_mock:
        try:
            get_free_space(camera)
        except Exception:
            pass

    assert get_camera_mock.called
