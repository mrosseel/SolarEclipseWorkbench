"""Platform differences that are easy to break from the other platform.

Every test here fakes the platform, so the whole file runs anywhere.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import fujixsdk._library as sdk_lib
import solareclipseworkbench.fuji_camera as fuji
import solareclipseworkbench.serial_ports as serial_ports


def _port(device, vid=0x1A86, pid=0x7523, description="USB serial"):
    return SimpleNamespace(device=device, vid=vid, pid=pid, description=description)


# ------------------------------------------------------------- serial nodes


def test_macos_prefers_the_call_up_node():
    # Opening /dev/tty.* blocks waiting for carrier detect, which a relay board
    # never asserts, so the call-up node is the only usable one.
    ports = [_port("/dev/tty.usbserial-A1"), _port("/dev/cu.usbserial-A1")]

    with patch.object(serial_ports.serial.tools.list_ports, "comports", return_value=ports), \
         patch.object(serial_ports.sys, "platform", "darwin"):
        found = [p.device for p in serial_ports.usb_serial_ports()]

    assert found == ["/dev/cu.usbserial-A1"]


def test_macos_does_not_list_the_same_adapter_twice():
    ports = [_port("/dev/cu.usbserial-A1"), _port("/dev/tty.usbserial-A1"),
             _port("/dev/cu.usbmodem14201"), _port("/dev/tty.usbmodem14201")]

    with patch.object(serial_ports.serial.tools.list_ports, "comports", return_value=ports), \
         patch.object(serial_ports.sys, "platform", "darwin"):
        found = serial_ports.usb_serial_ports()

    assert len(found) == 2


def test_ports_without_a_usb_id_are_dropped():
    # On macOS these are the built-in Bluetooth serial devices.
    ports = [_port("/dev/cu.usbserial-A1"), _port("/dev/tty.Bluetooth-Incoming-Port", vid=None)]

    with patch.object(serial_ports.serial.tools.list_ports, "comports", return_value=ports), \
         patch.object(serial_ports.sys, "platform", "darwin"):
        found = [p.device for p in serial_ports.usb_serial_ports()]

    assert found == ["/dev/cu.usbserial-A1"]


def test_linux_node_list_is_untouched():
    ports = [_port("/dev/ttyUSB0"), _port("/dev/ttyACM0")]

    with patch.object(serial_ports.serial.tools.list_ports, "comports", return_value=ports), \
         patch.object(serial_ports.sys, "platform", "linux"):
        found = [p.device for p in serial_ports.usb_serial_ports()]

    assert found == ["/dev/ttyUSB0", "/dev/ttyACM0"]


# ----------------------------------------------------------------- Fuji SDK


@pytest.mark.parametrize("system,expected", [
    ("Linux", "XAPI.so"),
    ("Darwin", "XAPI.bundle"),
    ("Windows", "XAPI.dll"),
])
def test_sdk_marker_matches_the_platform(system, expected):
    # Searching for the Linux name on a Mac finds nothing, which silently
    # disables the whole Fuji integration rather than failing loudly.
    with patch.object(fuji.platform, "system", return_value=system):
        assert fuji._sdk_marker() == expected


def _sdk_root():
    root = fuji.find_fuji_sdk_path()
    if not root:
        pytest.skip("Fuji SDK not present in this checkout")
    return root


def test_sdk_is_found_when_looking_for_the_macos_bundle():
    with patch.object(fuji.platform, "system", return_value="Darwin"):
        assert fuji.find_fuji_sdk_path() is not None


def test_macos_loader_finds_the_bundle_below_the_sdk_root():
    # Callers pass the SDK root; the redistributables sit several levels down.
    root = _sdk_root()

    with patch.object(sdk_lib.platform, "system", return_value="Darwin"):
        binary = sdk_lib.find_library(root)

    assert binary.exists()
    assert binary.name == "XAPI"
    assert "XAPI.bundle" in str(binary)


def test_macos_loader_finds_the_model_libraries():
    root = _sdk_root()

    with patch.object(sdk_lib.platform, "system", return_value="Darwin"):
        libraries = sdk_lib.find_model_libraries(root)

    # A shallow glob from the SDK root finds none of these.
    assert len(libraries) > 10
    assert all(path.exists() for path in libraries)


def test_macos_needs_no_library_path_juggling():
    # The bundles load through @rpath, and SIP strips DYLD_* across exec, so
    # the re-exec dance must not happen on macOS.
    with patch.object(sdk_lib.platform, "system", return_value="Darwin"):
        assert sdk_lib.ensure_ld_library_path("/anything") is True
