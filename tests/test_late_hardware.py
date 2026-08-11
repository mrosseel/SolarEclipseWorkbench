"""A relay connected after the script is loaded must still fire its jobs.

The scheduler used to decide "no relay, skip" once, at load time.  On 8 August
the relay registered 34 seconds after the script - held up by the serial port's
own reopen lock - and every relay command had already been marked skipped, so
both contact bursts were dead before the run started.  The device is now looked
up when the job fires.
"""

from unittest.mock import MagicMock

import pytest

from solareclipseworkbench.hardware_registry import register_hardware
from solareclipseworkbench.utils import _DeviceAtRuntime


@pytest.fixture(autouse=True)
def _clean_registry():
    register_hardware("relay", None)
    yield
    register_hardware("relay", None)


def test_a_device_connected_after_scheduling_is_the_one_used():
    proxy = _DeviceAtRuntime("relay", "relay_burst")

    relay = MagicMock()
    register_hardware("relay", relay)

    proxy.release_all()
    relay.release_all.assert_called_once()


def test_a_device_swapped_after_scheduling_is_picked_up():
    """A reconnect replaces the registered device; jobs must follow it."""
    proxy = _DeviceAtRuntime("relay", "relay_burst")
    first, second = MagicMock(), MagicMock()

    register_hardware("relay", first)
    proxy.shoot()
    register_hardware("relay", second)
    proxy.shoot()

    first.shoot.assert_called_once()
    second.shoot.assert_called_once()


def test_a_device_still_missing_when_the_job_fires_raises_with_the_command_name():
    proxy = _DeviceAtRuntime("relay", "relay_burst")

    with pytest.raises(RuntimeError, match="relay_burst"):
        proxy.release_all()
