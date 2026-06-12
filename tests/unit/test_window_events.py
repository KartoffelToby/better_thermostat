"""Tests for the window sensor event handler.

The accepted state vocabulary matches what the invalid_window_state
repair issue promises: on/true/open count as open, off/false/closed as
closed, unknown/unavailable as open (the safe direction).
"""

import asyncio
from unittest.mock import MagicMock, Mock, patch

import pytest

from custom_components.better_thermostat.events.window import (
    trigger_window_change,
    window_queue,
)

_WINDOW = "custom_components.better_thermostat.events.window"


def _make_bt(*, sensor_state="off", window_open=False):
    bt = MagicMock()
    bt.device_name = "Test BT"
    bt.window_id = "binary_sensor.window"
    bt.window_open = window_open
    bt.async_write_ha_state = Mock()
    bt.window_queue_task = asyncio.Queue()

    state = Mock()
    state.state = sensor_state
    bt.hass.states.get.return_value = state
    return bt


def _event(state_value):
    new_state = Mock()
    new_state.state = state_value
    event = Mock()
    event.data = {"new_state": new_state}
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize("reading", ["on", "true", "open", "unknown", "unavailable"])
async def test_open_readings_queue_an_open_event(reading):
    """Every documented open synonym is accepted as open."""
    bt = _make_bt(sensor_state=reading)
    await trigger_window_change(bt, _event(reading))
    assert bt.window_queue_task.get_nowait() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("reading", ["off", "false", "closed"])
async def test_closed_readings_queue_a_close_event(reading):
    """Every documented closed synonym is accepted as closed."""
    bt = _make_bt(sensor_state=reading, window_open=True)
    await trigger_window_change(bt, _event(reading))
    assert bt.window_queue_task.get_nowait() is False


@pytest.mark.asyncio
async def test_unrecognized_state_raises_an_issue():
    """Anything outside the documented vocabulary raises a repair issue."""
    bt = _make_bt(sensor_state="banana")
    with patch(f"{_WINDOW}.ir.async_create_issue") as issue:
        await trigger_window_change(bt, _event("banana"))
    issue.assert_called_once()
    assert bt.window_queue_task.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("final_state", ["unknown", "unavailable"])
async def test_queue_treats_unknown_and_unavailable_as_open(final_state):
    """A sensor that turns unknown/unavailable during debounce confirms an open event."""
    bt = _make_bt(sensor_state=final_state)
    bt.window_delay = 0
    bt.window_delay_after = 0
    bt.in_maintenance = False
    bt.control_queue_task = asyncio.Queue()

    task = asyncio.create_task(window_queue(bt))
    await bt.window_queue_task.put(True)
    await asyncio.wait_for(bt.window_queue_task.join(), timeout=1)

    assert bt.window_open is True
    bt.async_write_ha_state.assert_called_once()
    assert bt.control_queue_task.qsize() == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_queue_survives_a_sensor_that_vanished_during_debounce():
    """A sensor removed during the debounce delay drops the event, not the task."""
    bt = _make_bt(sensor_state="on")
    bt.window_delay = 0
    bt.window_delay_after = 0
    bt.hass.states.get.return_value = None

    task = asyncio.create_task(window_queue(bt))
    await bt.window_queue_task.put(True)
    await asyncio.wait_for(bt.window_queue_task.join(), timeout=1)

    assert not task.done()
    bt.async_write_ha_state.assert_not_called()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
